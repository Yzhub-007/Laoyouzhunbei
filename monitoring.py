"""Local automatic monitoring with source evidence, expiry and review-only tasks.

No sensor data is invented. Weather estimates are not campus observations. Camera
water presence is not a centimetre measurement. Management decisions remain in
the existing reviewed workflow. This module uses only the Python standard library.
"""
import copy
import hashlib
import json
import math
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request

from engine import ValidationError, timestamp
import secure_storage
from network_policy import NetworkPolicy, validate_url

DEFAULT_CONFIG = {'poll_seconds': 60, 'depth_watch_cm': 2, 'depth_alert_cm': 15,
                  'camera_confidence': 0.8, 'confirm_samples': 2, 'sources': []}
LABELS = {'unknown': '状态未知', 'clear': '设备范围内未检出积水',
          'suspected': '水深异常待确认', 'pooling_suspected': '画面疑似积水待核验',
          'pooling': '监测到积水', 'high_water': '积水达到关注阈值'}
WEATHER_LABELS = {'unknown': '天气状态未知', 'clear': '晴', 'cloudy': '多云或阴',
                  'fog': '雾', 'rain': '降雨', 'snow': '降雪', 'thunderstorm': '雷暴',
                  'wind': '风速达到关注阈值', 'rain_wind': '降雨及风速关注'}
SCORES = {'unknown': 0, 'clear': 0, 'pooling_suspected': 1, 'suspected': 1,
          'pooling': 2, 'high_water': 3}
MAX_BYTES = 262144
MAX_ROWS_PER_SOURCE = 120
MAX_EVENTS = 1000
WIND_WATCH_MS = 10.8  # A configurable-policy extension, not an official warning.


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec='microseconds')


def number(value, low, high, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValidationError(f'{name} 必须为 {low}—{high} 范围内的有限数字。')
    return value


def text_value(value, name, maximum=1000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValidationError(f'{name} 必须为非空文字，最多 {maximum} 字。')
    return value.strip()


def fresh_status(observed, now, max_age):
    # Device clocks ahead of the server are not evidence of the current state.
    # ISO datetimes have microsecond precision; the clock float can be finer.
    # Treat round-trip rounding as the same instant, without allowing future seconds.
    if observed > now + 0.000001:
        return 'future'
    return 'fresh' if now - observed <= max_age else 'stale'


def contained_json(input_dir, relative):
    relative = text_value(relative, 'path', 500)
    candidate = Path(relative)
    if candidate.is_absolute() or candidate.drive or '..' in candidate.parts or ':' in relative:
        raise ValidationError('JSON 路径必须相对于 monitor-input 目录且不能越界。')
    target = (input_dir / candidate).resolve()
    if not target.is_relative_to(input_dir.resolve()) or target.suffix.lower() != '.json':
        raise ValidationError('只允许 monitor-input 目录内的 JSON 文件。')
    return target


def strict_json(raw):
    def invalid_constant(value):
        raise ValidationError('JSON 不能含 NaN 或 Infinity。')
    try:
        return json.loads(raw, parse_constant=invalid_constant)
    except (ValueError, UnicodeError) as exc:
        raise ValidationError('监测资料不是有效 JSON。') from exc


def fetch_source(source, input_dir, config=None):
    if source['transport'] == 'file':
        with contained_json(input_dir, source['path']).open('rb') as handle:
            raw = handle.read(MAX_BYTES + 1)
    else:
        request = Request(source['url'], headers={'Accept': 'application/json', 'User-Agent': 'CampusMonitor/1.0'})
        # No ambient proxy credentials, cookies or authorization headers; no redirects.
        with NetworkPolicy(config or {}).open(request, purpose='monitor', timeout=5) as response:
            content_type = response.headers.get_content_type()
            if content_type != 'application/json' and not content_type.endswith('+json'):
                raise ValidationError('监测接口必须返回 application/json。')
            raw = response.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValidationError('单次监测响应超过 256 KiB 限制。')
    data = strict_json(raw)
    if isinstance(data, dict) and 'observations' in data:
        if data.get('source_id', source['id']) != source['id']:
            raise ValidationError('响应的 source_id 与配置不一致。')
        return data['observations']
    return [data]


class MonitorService:
    def __init__(self, db_path, live_service, pilot_loader, config_path=None, clock=time.time, vision_config=None):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.input_dir = self.db_path.parent / 'monitor-input'
        self.input_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = Path(config_path) if config_path else self.db_path.parent / 'monitor-config.json'
        self.live_service, self.pilot_loader, self.clock = live_service, pilot_loader, clock
        self.vision_config = dict(vision_config or {})
        self.retention_days = {}
        for key, default, maximum in (('OBSERVATION', 7, 30), ('EVENT', 30, 90), ('CORRECTION', 30, 90)):
            value = self.vision_config.get('SECURITY_' + key + '_DAYS', default)
            if isinstance(value, str) and re.fullmatch(r'[0-9]+', value):
                value = int(value)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValidationError(f'SECURITY_{key}_DAYS 必须为 1—{maximum} 的整数。')
            self.retention_days[key] = value
        self.lock = threading.RLock()
        self.poll_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.thread = None
        self.last_poll_at = None
        self.last_error = None
        self.live_snapshot = None
        self.source_health = {}
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        self.revision = 0
        self._init_db()
        if self.config_path.exists():
            try:
                saved = secure_storage.read_json(self.config_path)
                if not isinstance(saved, dict) or type(saved.get('revision')) is not int or saved['revision'] < 0:
                    raise ValidationError('监测配置版本无效。')
                self.config = self._validate_config(saved.get('config'))
                self.revision = saved['revision']
            except (OSError, ValueError, TypeError, KeyError):
                self.last_error = '已保存的监测配置无效，当前未启用设备；请检查并重新保存配置。'
        with self._db() as db:
            self._sync_camera_contexts(db)

    def _db(self):
        return secure_storage.database(self.db_path)

    def _init_db(self):
        with self._db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS monitor_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL,
                    observed_epoch REAL NOT NULL, received_epoch REAL NOT NULL,
                    payload TEXT NOT NULL, UNIQUE(source_id, observed_epoch));
                CREATE INDEX IF NOT EXISTS monitor_observations_time ON monitor_observations(source_id, observed_epoch DESC);
                CREATE TABLE IF NOT EXISTS monitor_corrections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, point_id TEXT NOT NULL,
                    created_epoch REAL NOT NULL, expires_epoch REAL NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS monitor_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, entity TEXT NOT NULL, created_epoch REAL NOT NULL,
                    payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS monitor_states (entity TEXT PRIMARY KEY, state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS monitor_security_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS monitor_observations_received ON monitor_observations(received_epoch);
                CREATE INDEX IF NOT EXISTS monitor_events_created ON monitor_events(created_epoch);
                CREATE INDEX IF NOT EXISTS monitor_corrections_created ON monitor_corrections(created_epoch);
            ''')
            if not db.execute("SELECT 1 FROM monitor_security_meta WHERE key='privacy_templates_v1'").fetchone():
                for row in db.execute('SELECT id,payload FROM monitor_observations').fetchall():
                    payload = json.loads(row['payload'])
                    if 'water_detected' in payload or 'frame_sha256' in payload:
                        payload = self._camera_payload(payload)
                        db.execute('UPDATE monitor_observations SET payload=? WHERE id=?',
                                   (json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False), row['id']))
                # Older event basis could contain a model-generated description.
                for row in db.execute('SELECT id,payload FROM monitor_events').fetchall():
                    payload = json.loads(row['payload'])
                    if payload.get('kind') == 'point_state':
                        payload['basis'] = ['历史状态变更；原自由文字描述已按隐私策略移除。']
                        db.execute('UPDATE monitor_events SET payload=? WHERE id=?',
                                   (json.dumps(payload, ensure_ascii=False), row['id']))
                db.execute("INSERT INTO monitor_security_meta(key,value) VALUES('privacy_templates_v1','1')")
            self._prune(db, self.clock())

    @staticmethod
    def _camera_payload(payload):
        """Only retain typed camera fields and a locally selected fixed template."""
        allowed = {'observed_at', 'quality', 'water_detected', 'confidence', 'frame_sha256'}
        clean = {key: value for key, value in payload.items() if key in allowed}
        if clean.get('quality') != 'good' or clean.get('water_detected') is None:
            clean['summary'] = '画面质量不足，积水状态未知；请人工核验。'
        elif clean['water_detected']:
            clean['summary'] = '画面疑似存在积水；请人工核验，不能推断水深。'
        else:
            clean['summary'] = '画面范围内未检出积水；仍需结合连续样本与现场核验。'
        return clean

    def _prune(self, db, now):
        db.execute('DELETE FROM monitor_observations WHERE received_epoch < ? OR observed_epoch < ?',
                   (now - self.retention_days['OBSERVATION'] * 86400,) * 2)
        db.execute('DELETE FROM monitor_events WHERE created_epoch < ?',
                   (now - self.retention_days['EVENT'] * 86400,))
        db.execute('DELETE FROM monitor_corrections WHERE created_epoch < ?',
                   (now - self.retention_days['CORRECTION'] * 86400,))

    def _camera_context(self, source, vision_config=None):
        """A digest binds evidence to the privacy area, camera and model settings."""
        config = self.vision_config if vision_config is None else vision_config
        context = {'source': source, 'pipeline': 'private-camera-v2'}
        if source['transport'] == 'vision':
            from camera_vision import SYSTEM_PROMPT
            context['vision'] = {key: value for key, value in config.items()
                                 if key.startswith('VISION_') or key.startswith('CAMERA_')}
            context['prompt'] = SYSTEM_PROMPT
        raw = json.dumps(context, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':'))
        # The fingerprint is stored only in the encrypted database, never the UI.
        return hashlib.sha256(raw.encode('utf-8')).hexdigest()

    def _sync_camera_contexts(self, db, config=None):
        config = self.config if config is None else config
        row = db.execute("SELECT value FROM monitor_security_meta WHERE key='camera_contexts_v1'").fetchone()
        previous = json.loads(row['value']) if row else {}
        current = {source['id']: self._camera_context(source) for source in config['sources']
                   if source['kind'] == 'camera'}
        changed = {sid for sid in previous.keys() | current.keys() if previous.get(sid) != current.get(sid)}
        for sid in changed:
            db.execute('DELETE FROM monitor_observations WHERE source_id=?', (sid,))
            self.source_health.pop(sid, None)
        if current != previous or row is None:
            db.execute("INSERT OR REPLACE INTO monitor_security_meta(key,value) VALUES('camera_contexts_v1',?)",
                       (json.dumps(current, sort_keys=True),))

    def _dataset(self):
        data = self.pilot_loader()
        if not isinstance(data, dict) or not isinstance(data.get('points'), list):
            raise ValidationError('当前保存资料缺少点位表。')
        return data

    def _validate_config(self, config):
        if not isinstance(config, dict) or set(config) != set(DEFAULT_CONFIG):
            raise ValidationError('监测配置字段不完整或包含未知字段。')
        result = copy.deepcopy(config)
        number(result['poll_seconds'], 10, 3600, 'poll_seconds')
        number(result['depth_watch_cm'], 0.1, 1000, 'depth_watch_cm')
        number(result['depth_alert_cm'], 0.2, 3000, 'depth_alert_cm')
        if result['depth_alert_cm'] <= result['depth_watch_cm']:
            raise ValidationError('关注水深必须高于积水识别水深。')
        number(result['camera_confidence'], 0.5, 1, 'camera_confidence')
        if type(result['confirm_samples']) is not int or not 2 <= result['confirm_samples'] <= 10:
            raise ValidationError('confirm_samples 必须为 2—10 的整数。')
        if not isinstance(result['sources'], list) or len(result['sources']) > 60:
            raise ValidationError('sources 必须为最多 60 项的数组。')
        points = {point['id'] for point in self._dataset()['points']}
        seen = set()
        for source in result['sources']:
            if not isinstance(source, dict):
                raise ValidationError('每个监测来源必须为对象。')
            allowed = {'id', 'name', 'point_id', 'kind', 'transport', 'path', 'url', 'enabled', 'max_age_seconds',
                       'input_type', 'vision_enabled', 'url_env', 'timestamp_header', 'roi', 'privacy_masks'}
            if set(source) - allowed:
                raise ValidationError('来源包含未知配置字段。')
            sid = text_value(source.get('id'), '来源 id', 60)
            if not re.fullmatch(r'[A-Za-z0-9_-]+', sid) or sid in seen:
                raise ValidationError('来源 id 必须唯一，且只含英文字母、数字、短横线或下划线。')
            source['id'] = sid
            seen.add(sid)
            source['name'] = text_value(source.get('name'), '来源名称', 120)
            if not isinstance(source.get('point_id'), str) or source['point_id'] not in points:
                raise ValidationError('来源必须关联当前建档点位。')
            if source.get('kind') not in ('water_depth', 'camera') or source.get('transport') not in ('push', 'file', 'http', 'vision'):
                raise ValidationError('不支持的来源种类或接入方式。')
            source.setdefault('enabled', True)
            source.setdefault('max_age_seconds', 300)
            if type(source['enabled']) is not bool:
                raise ValidationError('enabled 必须为布尔值。')
            number(source['max_age_seconds'], 1, 86400, 'max_age_seconds')
            if source['transport'] == 'file':
                contained_json(self.input_dir, source.get('path'))
            elif source['transport'] == 'http':
                url = text_value(source.get('url'), 'url', 2000)
                parsed = urlparse(url)
                if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                    raise ValidationError('监测地址须为不含账户信息或片段的 HTTP(S) 地址。')
                validate_url(url, purpose='monitor', config=self.vision_config, resolve=False)
            elif source['transport'] == 'vision':
                if source['kind'] != 'camera':
                    raise ValidationError('只有 camera 来源可以使用 vision 接入。')
                from camera_vision import validate_camera_source
                validate_camera_source(source, self.input_dir, config=self.vision_config)
        return result

    def get_config(self):
        with self.lock:
            return {'revision': self.revision, 'config': copy.deepcopy(self.config)}

    def save_config(self, payload):
        if not isinstance(payload, dict) or set(payload) != {'revision', 'config'}:
            raise ValidationError('配置保存需要 revision 和 config。')
        with self.lock:
            if type(payload['revision']) is not int or payload['revision'] != self.revision:
                raise ValidationError('监测配置已被更新，请刷新后重试。')
            validated = self._validate_config(payload['config'])
            revision = self.revision + 1
            output = {'revision': revision, 'config': validated}
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                secure_storage.write_json(self.config_path, output)
            except OSError as exc:
                raise ValidationError('监测配置未能写入磁盘。') from exc
            # A reused ID with changed location/kind must never inherit old evidence.
            old = {s['id']: s for s in self.config['sources']}
            changed = {s['id'] for s in validated['sources'] if s['id'] in old and
                       any(s.get(k) != old[s['id']].get(k) for k in ('point_id', 'kind', 'transport', 'path', 'url', 'url_env', 'input_type', 'timestamp_header', 'roi', 'privacy_masks', 'vision_enabled'))}
            removed = set(old) - {s['id'] for s in validated['sources']}
            with self._db() as db:
                for sid in changed | removed:
                    db.execute('DELETE FROM monitor_observations WHERE source_id=?', (sid,))
                    self.source_health.pop(sid, None)
                self._sync_camera_contexts(db, validated)
            self.config, self.revision = validated, revision
            self.wake_event.set()
            return copy.deepcopy(output)

    def _observation(self, source, item):
        if not isinstance(item, dict):
            raise ValidationError('观测记录必须为对象。')
        allowed = {'observed_at', 'depth_cm', 'depth_reference', 'water_detected', 'confidence',
                   'quality', 'evidence', 'summary', 'event_id', 'frame_sha256'}
        if set(item) - allowed:
            raise ValidationError('观测记录包含未知字段。')
        observed = timestamp(item.get('observed_at')).timestamp()
        if not math.isfinite(observed):
            raise ValidationError('观测时间无效。')
        quality = item.get('quality', 'good')
        if quality not in ('good', 'poor'):
            raise ValidationError('quality 必须为 good 或 poor。')
        result = {'observed_at': iso(observed), 'quality': quality}
        if source['kind'] == 'water_depth':
            if item.get('depth_reference') != 'ground':
                raise ValidationError('水深必须经过地面基准标定，depth_reference 应为 ground。')
            result.update(depth_cm=number(item.get('depth_cm'), 0, 3000, 'depth_cm'), depth_reference='ground')
            if any(key in item for key in ('water_detected', 'confidence')):
                raise ValidationError('水深来源不能混入视觉判断字段。')
        else:
            detected = item.get('water_detected')
            if type(detected) is not bool and not (quality == 'poor' and detected is None):
                raise ValidationError('water_detected 必须为布尔值；画质差时可以为 null。')
            confidence = item.get('confidence')
            if confidence is None and quality == 'poor':
                confidence = 0
            result.update(water_detected=detected, confidence=number(confidence, 0, 1, 'confidence'))
            if any(key in item for key in ('depth_cm', 'depth_reference')):
                raise ValidationError('视觉结果不能推断厘米水深。')
        for key in ('evidence', 'summary', 'event_id'):
            if key in item:
                result[key] = text_value(item[key], key, 120 if key == 'event_id' else 1000)
        if 'frame_sha256' in item:
            if not isinstance(item['frame_sha256'], str) or not re.fullmatch('[a-f0-9]{64}', item['frame_sha256']):
                raise ValidationError('frame_sha256 必须为 64 位小写十六进制图像摘要。')
            result['frame_sha256'] = item['frame_sha256']
        if source['kind'] == 'camera':
            result = self._camera_payload(result)
        return observed, result

    def _ingest(self, source, observations):
        if not isinstance(observations, list) or not 1 <= len(observations) <= 100:
            raise ValidationError('observations 必须含 1—100 条记录。')
        prepared = [self._observation(source, item) for item in observations]
        now, inserted = self.clock(), 0
        with self._db() as db:
            self._prune(db, now)
            self._sync_camera_contexts(db)
            for observed, item in prepared:
                if item.get('frame_sha256'):
                    old_frames = db.execute('SELECT payload FROM monitor_observations WHERE source_id=? ORDER BY observed_epoch DESC LIMIT ?',
                                            (source['id'], MAX_ROWS_PER_SOURCE)).fetchall()
                    if any(json.loads(row['payload']).get('frame_sha256') == item['frame_sha256'] for row in old_frames):
                        continue  # An unchanged/stuck camera image cannot refresh evidence age.
                payload = json.dumps(item, ensure_ascii=False, sort_keys=True, allow_nan=False)
                existing = db.execute('SELECT payload FROM monitor_observations WHERE source_id=? AND observed_epoch=?',
                                      (source['id'], observed)).fetchone()
                if existing and existing['payload'] != payload:
                    raise ValidationError('同来源同一观测时刻的内容冲突；未覆盖已有证据。')
                if not existing:
                    db.execute('INSERT INTO monitor_observations(source_id,observed_epoch,received_epoch,payload) VALUES(?,?,?,?)',
                               (source['id'], observed, now, payload))
                    inserted += 1
            db.execute('''DELETE FROM monitor_observations WHERE source_id=? AND id NOT IN
                          (SELECT id FROM monitor_observations WHERE source_id=? ORDER BY observed_epoch DESC LIMIT ?)''',
                       (source['id'], source['id'], MAX_ROWS_PER_SOURCE))
        self.source_health[source['id']] = {'last_success_at': iso(now), 'error': None}
        return {'source_id': source['id'], 'accepted': inserted, 'duplicates': len(prepared) - inserted}

    def ingest(self, payload):
        if not isinstance(payload, dict) or set(payload) != {'source_id', 'observations'}:
            raise ValidationError('推送需要 source_id 和 observations。')
        with self.lock:
            source = next((s for s in self.config['sources'] if s['id'] == payload['source_id']), None)
            if not source or not source['enabled'] or source['transport'] != 'push':
                raise ValidationError('来源未配置、已停用或不允许推送。')
            return self._ingest(source, payload['observations'])

    def correct(self, payload):
        if not isinstance(payload, dict) or set(payload) != {'point_id', 'state', 'actor', 'note', 'ttl_minutes'}:
            raise ValidationError('人工校正需要点位、状态、操作人、依据和有效分钟数。')
        with self.lock:
            if (not isinstance(payload['point_id'], str) or not isinstance(payload['state'], str)
                    or payload['point_id'] not in {p['id'] for p in self._dataset()['points']} or payload['state'] not in LABELS):
                raise ValidationError('校正点位或状态无效。')
            actor = text_value(payload['actor'], '操作人', 120)
            note = text_value(payload['note'], '校正依据', 1000)
            ttl = number(payload['ttl_minutes'], 1, 120, 'ttl_minutes')
            now = self.clock()
            result = {'point_id': payload['point_id'], 'state': payload['state'], 'label': LABELS[payload['state']],
                      'actor': actor, 'note': note, 'created_at': iso(now), 'expires_at': iso(now + ttl * 60), 'active': True}
            with self._db() as db:
                self._prune(db, now)
                db.execute('INSERT INTO monitor_corrections(point_id,created_epoch,expires_epoch,payload) VALUES(?,?,?,?)',
                           (payload['point_id'], now, now + ttl * 60, json.dumps(result, ensure_ascii=False)))
                db.execute('DELETE FROM monitor_corrections WHERE id NOT IN (SELECT id FROM monitor_corrections ORDER BY id DESC LIMIT 500)')
            return result

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.stop_event.clear()
            self.thread = threading.Thread(target=self._run, name='campus-monitor', daemon=True)
            self.thread.start()

    def stop(self):
        # Serialize shutdown with local commits, but never wait on upstream I/O.
        # A fetch that finishes later must see this signal before touching stores.
        with self.lock:
            self.stop_event.set()
            self.wake_event.set()
            thread = self.thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=1)

    def request_poll(self):
        """Coalesce refresh requests onto the existing worker; never spawn a fetch."""
        with self.lock:
            running = bool(self.thread and self.thread.is_alive() and not self.stop_event.is_set())
            if running:
                self.wake_event.set()
            return {'queued': running}

    def _run(self):
        while not self.stop_event.is_set():
            with self.lock:
                if self.stop_event.is_set():
                    break
                # Requests received before this point are covered by this poll;
                # requests arriving during it stay set and trigger the next one.
                self.wake_event.clear()
            try:
                self.poll_once()
            except Exception:
                with self.lock:
                    if not self.stop_event.is_set():
                        self.last_error = '本轮监测失败，下一周期将重试。'
            with self.lock:
                if self.stop_event.is_set():
                    break
                seconds = self.config['poll_seconds']
            self.wake_event.wait(seconds)

    def poll_once(self):
        # No upstream requests while holding the snapshot/config lock.
        with self.poll_lock:
            with self.lock:
                if self.stop_event.is_set():
                    return None
                sources = copy.deepcopy(self.config['sources'])
                vision_config = copy.deepcopy(self.vision_config)
            try:
                cancellable = getattr(self.live_service, 'snapshot_cancelable', None)
                live = (cancellable(self.stop_event, self.lock) if callable(cancellable) else
                        self.live_service.snapshot() if self.live_service else None)
                with self.lock:
                    if self.stop_event.is_set():
                        return None
                    self.live_snapshot = copy.deepcopy(live)
                    self.last_error = None
            except Exception:
                with self.lock:
                    if self.stop_event.is_set():
                        return None
                    self.last_error = '天气来源本轮不可用；请检查来源详情与时效。'
                    self.live_snapshot = None
            for source in sources:
                if self.stop_event.is_set() or not source['enabled'] or source['transport'] == 'push':
                    continue
                with self.lock:
                    if self.stop_event.is_set():
                        return None
                    if source not in self.config['sources'] or vision_config != self.vision_config:
                        continue  # Settings may have changed while weather was being fetched.
                try:
                    if source['transport'] == 'vision':
                        from camera_vision import capture_and_analyze
                        with self.lock:
                            if self.stop_event.is_set():
                                return None
                            with self._db() as db:
                                self._sync_camera_contexts(db)
                                previous = db.execute('SELECT payload FROM monitor_observations WHERE source_id=? ORDER BY observed_epoch DESC LIMIT 1',
                                                      (source['id'],)).fetchone()
                            previous_hash = json.loads(previous['payload']).get('frame_sha256') if previous else None
                        observation = capture_and_analyze(source, self.input_dir, vision_config,
                                                          clock=self.clock, previous_sha256=previous_hash)
                        rows = [observation] if observation is not None else None
                    else:
                        rows = fetch_source(source, self.input_dir, vision_config)
                    with self.lock:
                        if self.stop_event.is_set():
                            return None
                        if source in self.config['sources'] and vision_config == self.vision_config:
                            if rows is None:
                                # Capture succeeded but the image was unchanged; its original
                                # observed_at remains authoritative, including eventual expiry.
                                self.source_health[source['id']] = {'last_success_at': iso(self.clock()), 'error': None}
                            else:
                                self._ingest(source, rows)
                except Exception as exc:
                    # Do not reflect an upstream URL, credentials or response into the UI.
                    error = str(exc) if isinstance(exc, ValidationError) else '来源读取失败，请检查设备、网络或响应格式。'
                    with self.lock:
                        if self.stop_event.is_set():
                            return None
                        if source in self.config['sources'] and vision_config == self.vision_config:
                            self.source_health.setdefault(source['id'], {})['error'] = error[:500]
            with self.lock:
                if self.stop_event.is_set():
                    return None
                self.last_poll_at = iso(self.clock())
                return self.snapshot()

    def _weather(self, now):
        snapshot = self.live_snapshot or {}
        providers, hazards, basis = [], set(), []
        for provider_id in ('model', 'station'):
            source = snapshot.get('providers', {}).get(provider_id, {})
            observed, max_age = source.get('data_epoch'), source.get('max_age_seconds')
            valid_time = (type(observed) in (int, float) and math.isfinite(observed) and
                          type(max_age) in (int, float) and math.isfinite(max_age) and max_age > 0)
            status = fresh_status(observed, now, max_age) if valid_time else 'missing'
            if status == 'fresh' and (source.get('error') or source.get('usable') is False):
                status = 'error'
            state, local_hazards, why = 'unknown', [], []
            rain, wind = source.get('precipitation_mm'), source.get('wind_speed_ms')
            code = source.get('weather_code')
            if status == 'fresh':
                if provider_id == 'model':
                    if code == 0:
                        state = 'clear'
                    elif code in (1, 2, 3):
                        state = 'cloudy'
                    elif code in (45, 48):
                        state = 'fog'
                    elif code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82):
                        state = 'rain'
                    elif code in (71, 73, 75, 77, 85, 86):
                        state = 'snow'
                    elif code in (95, 96, 99):
                        state = 'thunderstorm'
                    if code is not None:
                        why.append(f'Open-Meteo WMO 天气代码 {code}')
                    if type(rain) in (int, float) and math.isfinite(rain) and rain > 0:
                        if state not in ('snow', 'thunderstorm'):
                            state = 'rain'
                        why.append(f'模型时段降水 {rain:g} mm（非雨强等级）')
                else:
                    # Present-weather groups only; no parsing of unrelated report numbers.
                    code_text = code if isinstance(code, str) else ''
                    tokens = re.findall(r'[-+]?(?:VC)?(?:MI|PR|BC|DR|BL|SH|TS|FZ)?(?:DZ|RA|SN|SG|IC|PL|GR|GS|UP|BR|FG|FU|VA|DU|SA|HZ|PY|SQ|FC|SS|DS)+', code_text)
                    if any('TS' in token for token in tokens):
                        state = 'thunderstorm'
                    elif any('RA' in token or 'DZ' in token for token in tokens):
                        state = 'rain'
                    elif any('SN' in token for token in tokens):
                        state = 'snow'
                    elif any('FG' in token or 'BR' in token for token in tokens):
                        state = 'fog'
                    if tokens:
                        why.append('机场天气现象代码 ' + code_text)
                if state in ('rain', 'thunderstorm'):
                    local_hazards.append('rain')
                if type(wind) in (int, float) and math.isfinite(wind) and wind >= WIND_WATCH_MS:
                    local_hazards.append('wind')
                    why.append(f'风速 {wind:g} m/s 达到内部关注阈值 {WIND_WATCH_MS:g} m/s')
                    state = 'rain_wind' if 'rain' in local_hazards else 'wind'
            label_prefix = '城市模型估计' if provider_id == 'model' else '机场站观测'
            row = {'id': provider_id, 'name': source.get('provider', label_prefix),
                   'kind': 'model_estimate' if provider_id == 'model' else 'station_observation',
                   'state': state, 'label': WEATHER_LABELS[state], 'status': status,
                   'observed_at': source.get('data_time'), 'source_url': source.get('source_url'),
                   'hazards': local_hazards, 'basis': why, 'weather_code': code,
                   'precipitation_mm': rain, 'wind_speed_ms': wind,
                   'note': '城市模型估计，不是校园实测。' if provider_id == 'model' else '机场站实测，不代表校园点位。',
                   'error': source.get('error')}
            providers.append(row)
            hazards.update(local_hazards)
            basis.extend(label_prefix + '：' + reason for reason in why)
        if {'rain', 'wind'} <= hazards:
            state = 'rain_wind'
        elif 'rain' in hazards:
            state = 'thunderstorm' if any(p['state'] == 'thunderstorm' for p in providers) else 'rain'
        elif 'wind' in hazards:
            state = 'wind'
        else:
            state = next((p['state'] for p in providers if p['state'] != 'unknown'), 'unknown')
        if not basis:
            basis = ['尚无可用的新近天气证据；缺测不能解释为晴天。']
        return {'state': state, 'label': WEATHER_LABELS[state], 'hazards': sorted(hazards),
                'region': snapshot.get('location', {}).get('region'), 'providers': providers, 'basis': basis,
                'official_alert_status': 'not_connected', 'wind_watch_ms': WIND_WATCH_MS,
                'note': '天气识别不代表现场已发生积水，也不产生官方预警级别。'}

    def _depth_state(self, records):
        config = self.config
        current, candidate, run = 'unknown', None, 0
        watch_run = alert_run = clear_run = below_alert_run = 0
        for row in reversed(records):
            item = row['data']
            if item['quality'] != 'good':
                current, candidate, run = 'unknown', None, 0
                watch_run = alert_run = clear_run = below_alert_run = 0
                continue
            depth = item['depth_cm']
            watch_run = watch_run + 1 if depth >= config['depth_watch_cm'] else 0
            alert_run = alert_run + 1 if depth >= config['depth_alert_cm'] else 0
            clear_run = clear_run + 1 if depth < config['depth_watch_cm'] else 0
            below_alert_run = below_alert_run + 1 if depth < config['depth_alert_cm'] else 0
            candidate, run = (('high_water', alert_run) if depth >= config['depth_alert_cm'] else
                              ('pooling', watch_run) if depth >= config['depth_watch_cm'] else ('clear', clear_run))
            if alert_run >= config['confirm_samples']:
                current = 'high_water'
            elif clear_run >= config['confirm_samples']:
                current = 'clear'
            elif watch_run >= config['confirm_samples'] and (current != 'high_water' or below_alert_run >= config['confirm_samples']):
                current = 'pooling'
            elif current in ('unknown', 'clear') and candidate != 'clear':
                current = 'suspected'
        return current, run, candidate

    def _camera_state(self, records):
        current, candidate, run = 'unknown', None, 0
        for row in reversed(records):
            item = row['data']
            if item['quality'] != 'good' or item['confidence'] < self.config['camera_confidence']:
                current, candidate, run = 'unknown', None, 0
                continue
            detected = item['water_detected']
            run = run + 1 if detected is candidate else 1
            candidate = detected
            if detected:
                current = 'pooling_suspected'
            elif run >= self.config['confirm_samples']:
                current = 'clear'
        return current, run

    def _assess(self, dataset, now, db):
        sources, assessments = [], []
        by_point = {}
        point_ids = {p['id'] for p in dataset['points']}
        for source in self.config['sources']:
            records = [dict(row) for row in db.execute('SELECT * FROM monitor_observations WHERE source_id=? ORDER BY observed_epoch DESC LIMIT ?',
                                                     (source['id'], MAX_ROWS_PER_SOURCE))]
            for row in records:
                row['data'] = json.loads(row['payload'])
            latest = records[0] if records else None
            status = fresh_status(latest['observed_epoch'], now, source['max_age_seconds']) if latest else 'missing'
            if not source['enabled']:
                status = 'disabled'
            if source['point_id'] not in point_ids:
                status = 'orphaned'
            health = self.source_health.get(source['id'], {})
            if status == 'fresh' and health.get('error'):
                status = 'error'
            if status == 'fresh' and (latest['data']['quality'] == 'poor' or
                    (source['kind'] == 'camera' and latest['data']['confidence'] < self.config['camera_confidence'])):
                status = 'poor'
            view = {**source, 'status': status, 'observed_at': latest['data']['observed_at'] if latest else None,
                    'last_success_at': health.get('last_success_at', iso(latest['received_epoch']) if latest else None),
                    'error': health.get('error'), 'latest': latest['data'] if latest else None}
            sources.append(view)
            state, why = 'unknown', []
            if status == 'fresh':
                current_records = [r for r in records if fresh_status(r['observed_epoch'], now, source['max_age_seconds']) == 'fresh']
                if source['kind'] == 'water_depth':
                    state, run, candidate = self._depth_state(current_records)
                    why = [f"{source['name']}：地面基准水深 {latest['data']['depth_cm']:g} cm；同类连续样本 {run}/{self.config['confirm_samples']}"]
                    if state != candidate:
                        why.append('新状态尚未完成连续确认，保留最近确认状态或标记待确认。')
                else:
                    detected = latest['data']['water_detected']
                    state, run = self._camera_state(current_records)
                    why = [f"{source['name']}：画面{'疑似有水' if detected else '未检出水面'}，置信度 {latest['data']['confidence']:.0%}；独立有效同类画面 {run}/{self.config['confirm_samples']}；不能推断水深。"]
                    if not detected and run < self.config['confirm_samples']:
                        why.append('无水画面尚未达到连续确认次数，保持未知或此前疑似状态。')
                    if latest['data'].get('summary'):
                        why.append(latest['data']['summary'])
            else:
                translated = {'missing': '尚无观测', 'stale': '观测已过期', 'future': '观测时间在未来',
                              'disabled': '来源已停用', 'poor': '质量或置信度不足', 'error': '来源读取失败', 'orphaned': '关联点位已不存在'}
                why = [f"{source['name']}：{translated.get(status, status)}，不用于当前判断。"]
            by_point.setdefault(source['point_id'], []).append({'source': view, 'state': state, 'basis': why})
        for point in dataset['points']:
            rows = by_point.get(point['id'], [])
            usable = [row for row in rows if row['source']['status'] == 'fresh']
            states = {row['state'] for row in usable}
            state = max((row['state'] for row in usable), key=lambda value: (SCORES[value], value == 'clear'), default='unknown')
            conflict = 'clear' in states and any(SCORES[s] > 0 for s in states)
            basis = [reason for row in rows for reason in row['basis']] or ['尚未接入本点位监测来源，洪涝现状未知。']
            if conflict:
                basis.append('不同来源的有水/无水证据冲突，需现场核验；无水画面不抵消水深异常。')
            verification = point.get('status') == 'verified' and dataset.get('site', {}).get('status') == 'verified'
            if not verification:
                basis.append('场地或点位尚未核验，结果仅适用于已登记的测试/待核验监测范围。')
            observed = max((row['source']['observed_at'] for row in usable if row['source']['observed_at']), default=None)
            result = {'point_id': point['id'], 'point_name': point.get('name', point['id']),
                      'state': state, 'label': LABELS[state], 'automatic_state': state, 'automatic_label': LABELS[state],
                      'point_status': point.get('status', 'pending'), 'verified': verification,
                      'conflict': conflict, 'basis': basis, 'observed_at': observed,
                      'sources': [row['source']['id'] for row in rows], 'correction': None}
            correction = db.execute('SELECT * FROM monitor_corrections WHERE point_id=? ORDER BY id DESC LIMIT 1', (point['id'],)).fetchone()
            if correction:
                saved = json.loads(correction['payload'])
                saved['active'] = (correction['created_epoch'] <= now < correction['expires_epoch']
                                   and not saved.get('invalidated_reason'))
                # New stronger evidence ends a reassuring override immediately.
                if saved['active'] and observed and timestamp(observed).timestamp() > correction['created_epoch'] and SCORES[state] > SCORES[saved['state']]:
                    saved['active'] = False
                    saved['invalidated_reason'] = '校正后出现更高关注程度的新观测，已回到自动判断。'
                    saved['invalidated_at'] = iso(now)
                    db.execute('UPDATE monitor_corrections SET payload=? WHERE id=?',
                               (json.dumps(saved, ensure_ascii=False), correction['id']))
                result['correction'] = saved
                if saved['active']:
                    result['state'], result['label'] = saved['state'], saved['label'] + '（人工校正）'
            assessments.append(result)
        return sources, assessments

    def _tasks(self, dataset, weather, points, now):
        tasks = []
        originals = {p['id']: p for p in dataset['points']}
        site = dataset.get('site', {})
        for point in points:
            original = originals[point['point_id']]
            hazards = set(weather['hazards']) if weather.get('region') == site.get('region') else set()
            if SCORES[point['automatic_state']] > 0 or SCORES[point['state']] > 0:
                hazards.add('rain')
            for plan in dataset.get('plans', []):
                if (original.get('region') != site.get('region') or original.get('type') not in plan.get('point_types', [])
                        or not hazards.intersection(plan.get('hazards', []))
                        or (site.get('campus') and original.get('campus') != site['campus'])
                        or (plan.get('campus') and plan['campus'] != site.get('campus'))
                        or (plan.get('region') and plan['region'] != site.get('region'))
                        or (plan.get('point_ids') and point['point_id'] not in plan['point_ids'])):
                    continue
                try:
                    if plan.get('valid_until') and timestamp(plan['valid_until']).timestamp() < now:
                        continue
                except ValidationError:
                    continue
                basis = []
                if hazards.intersection(weather['hazards']) and weather.get('region') == site.get('region'):
                    basis.append('同地区天气参考触发资料匹配，不代表该点位已发生积水。')
                if SCORES[point['automatic_state']] > 0 or SCORES[point['state']] > 0:
                    basis.append('点位有积水关注信号，需核验来源证据和现场状态。')
                tasks.append({'id': point['point_id'] + ':' + plan['id'], 'point_id': point['point_id'],
                              'point_name': point['point_name'], 'plan_id': plan['id'], 'title': plan['title'],
                              'action': plan['action'], 'role': plan['role'], 'basis': basis,
                              'status': 'pending_review', 'plan_status': plan.get('status', 'draft'),
                              'point_status': point['point_status'], 'source': plan.get('source'),
                              'note': '自动生成的待核验建议；不自动发布或更改关闭、限行措施。'})
        return tasks

    def _events(self, db, points, sources, weather, now):
        entities = [('point:' + p['point_id'], p['state'] + ('|conflict' if p['conflict'] else ''),
                     {'kind': 'point_state', 'point_id': p['point_id'], 'point_name': p['point_name'],
                      'state': p['state'], 'label': p['label'], 'basis': p['basis']}) for p in points]
        entities += [('source:' + s['id'], s['status'], {'kind': 'source_status', 'source_id': s['id'],
                      'state': s['status'], 'label': s['name'] + ' · ' + s['status']}) for s in sources]
        entities.append(('weather', weather['state'], {'kind': 'weather_state', 'state': weather['state'], 'label': weather['label']}))
        for entity, state, data in entities:
            previous = db.execute('SELECT state FROM monitor_states WHERE entity=?', (entity,)).fetchone()
            if previous is None or previous['state'] != state:
                data.update(created_at=iso(now), previous_state=previous['state'] if previous else None)
                db.execute('INSERT INTO monitor_events(entity,created_epoch,payload) VALUES(?,?,?)', (entity, now, json.dumps(data, ensure_ascii=False)))
                db.execute('INSERT OR REPLACE INTO monitor_states(entity,state) VALUES(?,?)', (entity, state))
        keys = {entity for entity, _, _ in entities}
        for row in db.execute('SELECT entity FROM monitor_states').fetchall():
            if row['entity'] not in keys:
                db.execute('DELETE FROM monitor_states WHERE entity=?', (row['entity'],))
        db.execute('DELETE FROM monitor_events WHERE id NOT IN (SELECT id FROM monitor_events ORDER BY id DESC LIMIT ?)', (MAX_EVENTS,))
        return [dict(json.loads(row['payload']), id=row['id']) for row in db.execute('SELECT id,payload FROM monitor_events ORDER BY id DESC LIMIT 50')]

    def snapshot(self):
        """Bounded local work only: never fetch weather, files, cameras or HTTP here."""
        with self.lock:
            now, dataset = self.clock(), self._dataset()
            weather = self._weather(now)
            with self._db() as db:
                self._prune(db, now)
                self._sync_camera_contexts(db)
                sources, points = self._assess(dataset, now, db)
                tasks = self._tasks(dataset, weather, points, now)
                events = self._events(db, points, sources, weather, now)
            summary = {'total_points': len(points), 'unknown_points': sum(p['state'] == 'unknown' for p in points),
                       'clear_points': sum(p['state'] == 'clear' for p in points),
                       'suspected_points': sum(p['state'] in ('suspected', 'pooling_suspected') for p in points),
                       'alert_points': sum(p['state'] in ('pooling', 'high_water') for p in points),
                       'active_corrections': sum(bool(p['correction'] and p['correction']['active']) for p in points),
                       'task_count': len(tasks), 'connected_sources': sum(s['enabled'] for s in sources),
                       'fresh_sources': sum(s['status'] == 'fresh' for s in sources)}
            try:
                from camera_vision import vision_configured
                vision_ready = vision_configured(self.vision_config)
            except ImportError:
                vision_ready = False
            return {'served_at': iso(now), 'service': {'running': bool(self.thread and self.thread.is_alive() and not self.stop_event.is_set()),
                    'last_poll_at': self.last_poll_at, 'last_error': self.last_error, 'poll_seconds': self.config['poll_seconds'],
                    'vision_configured': vision_ready,
                    'retention_days': {key.lower(): value for key, value in self.retention_days.items()},
                    'storage_encryption': 'Windows DPAPI CurrentUser', 'camera_free_text_stored': False},
                    'weather': weather, 'points': points, 'tasks': tasks, 'events': events, 'sources': sources,
                    'summary': summary, 'config_revision': self.revision}
