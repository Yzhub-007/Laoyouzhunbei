"""Authenticated Windows-local monitor. Camera privacy processing requires Pillow."""
import argparse
import json
import hmac
import os
import secrets
import socket
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote

from engine import ValidationError, validate_dataset, model_input
from live_data import LiveService, make_reference, freshness, fetch_provider, ZJU_LOCATION
from workflow import initialize, mutate
from campus_agent import run_agent, readiness, fingerprint
from experiments import start_trial, finish_trial, stop_trial, check_generation, save_trial
from monitoring import MonitorService
from auth import AuthService, AuthError
from secure_storage import database as protected_database, read_json, write_json, protect_file, StorageSecurityError
import response_plans
from instance_lock import running_instance, InstanceRunningError

ROOT = Path(__file__).resolve().parent
TOKEN = secrets.token_urlsafe(32)
CONFIG = {}
DB_PATH = ROOT / 'runtime' / 'runs.sqlite3'
LIVE = None
MONITOR = None
AUTH = None


VIEWER_DATASET_FIELDS = {
    'site': {'id', 'name', 'region', 'campus', 'scope', 'status'},
    'weather': {'id', 'title', 'publisher', 'source_url', 'published_at', 'replay_at', 'region',
                'level', 'hazards', 'mode', 'text', 'text_kind', 'scope_note', 'live_snapshot_id',
                'official_alert_status'},
    'points': {'id', 'name', 'type', 'region', 'campus', 'features', 'source', 'status', 'verified_at'},
    'plans': {'id', 'title', 'action', 'role', 'source', 'status', 'hazards', 'point_types', 'point_ids',
              'campus', 'valid_until', 'verified_at'},
}


def redact_viewer(value, field=None):
    """Viewer accounts receive operational state, never private audit/authorization."""
    private = {'authorization', 'verified_by', 'reviewed_by', 'actor', 'participant', 'evaluator',
               'audit', 'model_audit', 'review_note', 'confirmation_note', 'notes', 'note',
               'reference', 'url_env', 'path', 'url', 'setup_code', 'password', 'api_key',
               'confirmed_by', 'input_snapshot'}
    if isinstance(value, list):
        return [redact_viewer(item, field) for item in value]
    if isinstance(value, dict):
        # Input validation permits extension fields. Never send such arbitrary
        # fields from a saved dataset to a less-privileged account.
        if {'site', 'weather', 'points', 'plans'} <= value.keys():
            value = {key: value[key] for key in VIEWER_DATASET_FIELDS}
        elif ((field == 'site' and ('region' in value or 'name' in value))
              or (field == 'weather' and 'title' in value and 'published_at' in value)):
            value = {key: item for key, item in value.items() if key in VIEWER_DATASET_FIELDS[field]}
        elif field in ('points', 'plans') and 'id' in value:
            value = {key: item for key, item in value.items() if key in VIEWER_DATASET_FIELDS[field]}
        return {key: redact_viewer(item, key) for key, item in value.items()
                if key not in private and not key.upper().endswith('_API_KEY')}
    return value

class LocalServer(ThreadingHTTPServer):
    # Windows SO_REUSEADDR can let two versions listen on the same local port.
    allow_reuse_address = False
    # Complete accepted requests before releasing ownership of the runtime.
    daemon_threads = False
    block_on_close = True
    request_read_timeout = 15

    def server_bind(self):
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def get_request(self):
        connection, address = super().get_request()
        try:
            # Bound idle request/header/body reads, not upstream model calls.
            connection.settimeout(self.request_read_timeout)
        except Exception:
            connection.close()
            raise
        return connection, address

def read_config(runtime=None):
    runtime = Path(runtime) if runtime else ROOT / 'runtime'
    runtime.mkdir(parents=True, exist_ok=True)
    protect_file(runtime)
    secret_path = runtime / 'secrets.dpapi'
    values = read_json(secret_path, default={})
    path = ROOT / '.env'
    if path.exists():
        protect_file(path)
        imported = {}
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            if line.strip() and not line.lstrip().startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                if key.strip().startswith(('LLM_', 'VISION_', 'CAMERA_', 'MONITOR_', 'SECURITY_')):
                    imported[key.strip()] = value.strip().strip('"').strip("'")
        if imported:
            values.update(imported)
            write_json(secret_path, values)
            path.write_text('# Configuration migrated to runtime/secrets.dpapi. Use security_cli.py set-secret KEY.\n', encoding='utf-8')
    for key, value in os.environ.items():
        if key.startswith(('LLM_', 'VISION_', 'CAMERA_', 'MONITOR_', 'SECURITY_')) and value:
            values[key] = value
    return values

@contextmanager
def database():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with protected_database(DB_PATH) as connection:
        connection.execute('CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, mode TEXT NOT NULL, input_json TEXT NOT NULL, output_json TEXT NOT NULL)')
        connection.execute('CREATE TABLE IF NOT EXISTS live_snapshots (id TEXT PRIMARY KEY, weather_json TEXT NOT NULL, created_at TEXT NOT NULL)')
        connection.execute('CREATE TABLE IF NOT EXISTS pilot (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, data_json TEXT NOT NULL)')
        connection.execute('CREATE TABLE IF NOT EXISTS trials (id TEXT PRIMARY KEY, started_at TEXT NOT NULL, record_json TEXT NOT NULL)')
        response_plans.initialize(connection)
        yield connection

def load_pilot(db):
    row = db.execute('SELECT revision, data_json FROM pilot WHERE id=1').fetchone()
    return {'revision': row[0], 'data': json.loads(row[1])} if row else {'revision': 0, 'data': json.loads((ROOT / 'data/zju_pilot.json').read_text(encoding='utf-8'))}

def monitoring_pilot():
    with database() as db:
        return load_pilot(db)['data']


def response_plan_monitor():
    """A failed collection never falls back to an old saved document's facts."""
    try:
        return MONITOR.snapshot() if MONITOR is not None else None
    except Exception:
        return None

def verify_live_reference(data):
    weather = data.get('weather', {}) if isinstance(data, dict) else {}
    if weather.get('mode') != 'live_reference':
        return
    with database() as db:
        row = db.execute('SELECT weather_json FROM live_snapshots WHERE id=?', (weather.get('live_snapshot_id'),)).fetchone()
    if row is None:
        raise ValidationError('本机未记录该实时快照，请从“最新气象”重新载入。')
    saved = json.loads(row[0])
    actual = dict(weather)
    # Hazards are a user's exercise choice, never an inferred official alert.
    saved.pop('hazards', None)
    actual.pop('hazards', None)
    if actual != saved:
        raise ValidationError('实时快照来源或内容被修改，请重新载入快照。灾种选择可以单独修改。')
    for provider in saved['live_sources'].values():
        if freshness(provider['data_epoch'], time.time(), provider['max_age_seconds']) != 'fresh':
            raise ValidationError('已载入的实时快照过期或时间异常，请重新获取。')

class Handler(BaseHTTPRequestHandler):
    server_version = 'Laoyouzhunbei/0.8'
    session = None
    def log_message(self, format, *args):
        pass  # Do not log pasted documents, credentials, or API errors.

    def send(self, status, body, content_type='application/json; charset=utf-8', cookie=None, attachment=None):
        if status >= 400 and self.command == 'POST' and not getattr(self, '_body_consumed', False):
            # Windows may reset a connection closed with an unread request body,
            # discarding the intended 403 response. Discard only bounded local
            # request bodies, with a short timeout; never parse or act on them.
            self.close_connection = True
            rejected_header = self.headers.get('Content-Length', '')
            rejected_length = int(rejected_header) if (rejected_header.isascii() and rejected_header.isdecimal()
                                                        and len(rejected_header) <= 6) else 0
            if 0 < rejected_length <= 512000:
                previous_timeout = self.connection.gettimeout()
                try:
                    self.connection.settimeout(1)
                    self.rfile.read(rejected_length)
                except OSError:
                    pass
                finally:
                    self.connection.settimeout(previous_timeout)
                self._body_consumed = True
        if self.session and self.session['user']['role'] == 'viewer' and isinstance(body, (dict, list)):
            body = redact_viewer(body)
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
        if cookie:
            self.send_header('Set-Cookie', cookie)
        if attachment:
            self.send_header('Content-Disposition', attachment)
        self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'" if attachment else "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(body)

    def valid_host(self):
        return self.headers.get('Host') in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}')

    def cookie_name(self):
        return 'campus_session_' + str(self.server.server_port)

    def cookie(self, value='', clear=False):
        return f'{self.cookie_name()}={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={0 if clear else 28800}'

    def require_session(self, roles=None):
        if AUTH is None:
            raise AuthError('安全登录服务未启动。', 503)
        self.session = AUTH.authenticate(self.headers.get('Cookie'), self.cookie_name())
        if not self.session:
            raise AuthError('请先登录，或会话已过期。')
        if not self.matches_session_proof(self.session):
            raise AuthError('缺少有效的本页面会话证明，请重新登录。', 403)
        if roles and self.session['user']['role'] not in roles:
            raise AuthError('当前账号没有此操作权限。', 403)
        return self.session

    def matches_session_proof(self, session):
        # Cookies are shared across ports. The second secret lives in this
        # origin's sessionStorage and is sent only as a custom request header.
        return hmac.compare_digest(self.headers.get('X-App-Token', '').encode('utf-8'),
                                   session['csrf_token'].encode('ascii'))

    def do_GET(self):
        self.session = None
        if not self.valid_host() or self.headers.get('Sec-Fetch-Site') == 'cross-site':
            return self.send(403, {'error': '仅允许本机同源访问。'})
        path = urlparse(self.path).path
        try:
            if path == '/api/auth/status':
                if AUTH is None:
                    return self.send(503, {'error': '安全登录服务未启动。'})
                self.session = AUTH.authenticate(self.headers.get('Cookie'), self.cookie_name())
                if self.session and not self.matches_session_proof(self.session):
                    self.session = None
                return self.send(200, AUTH.status(self.session))
            if path.startswith('/api/') and path != '/api/health':
                roles = {'admin'} if path in ('/api/monitor/config', '/api/auth/users') else None
                if path == '/api/response-plans' or path.startswith('/api/response-plans/'):
                    roles = {'admin', 'operator'}
                self.require_session(roles)
            if path == '/api/auth/users':
                return self.send(200, AUTH.users())
            return self.authenticated_get()
        except AuthError as exc:
            return self.send(exc.status, {'error': str(exc)})
        except StorageSecurityError:
            return self.send(503, {'error': '受保护存储暂不可用，未降级为明文。'})
        except ValidationError as exc:
            return self.send(400, {'error': str(exc)})
        except Exception:
            return self.send(500, {'error': '读取未完成，请检查本机运行环境。'})

    def authenticated_get(self):
        if not self.valid_host():
            return self.send(403, {'error':'仅允许本机访问。'})
        path = urlparse(self.path).path
        if path == '/protocol.md':
            return self.send(200, (ROOT / 'docs/11_紫金港完整试验流程.md').read_bytes(), 'text/markdown; charset=utf-8')
        guides = {'/monitoring-guide.md': '14_自动监测与识别.md', '/camera-guide.md': '15_摄像头接入.md', '/security-guide.md': '17_安全配置与使用边界.md'}
        if path in guides:
            return self.send(200, (ROOT / 'docs' / guides[path]).read_bytes(), 'text/markdown; charset=utf-8')
        if path == '/api/bootstrap':
            configured = all(isinstance(CONFIG.get(k), str) and CONFIG[k].strip()
                             for k in ('LLM_ENDPOINT', 'LLM_API_KEY', 'LLM_MODEL'))
            if configured:
                from network_policy import validate_url
                try:
                    validate_url(CONFIG['LLM_ENDPOINT'], 'llm', CONFIG, resolve=False)
                except ValidationError:
                    configured = False
            with database() as db:
                pilot = load_pilot(db)
            role = self.session['user']['role']
            return self.send(200, {'token': self.session['csrf_token'], 'user': self.session['user'],
                                   'permissions': {'can_operate': role in ('admin', 'operator'), 'can_configure': role == 'admin', 'can_manage_users': role == 'admin'},
                                   'ai_configured': configured, 'ai_verified': False,
                                   'pilot_revision': pilot['revision'], 'demo': pilot['data'],
                                   'pilot_template': json.loads((ROOT / 'data/zju_pilot.json').read_text(encoding='utf-8')),
                                   'live_location': ZJU_LOCATION})
        if path == '/api/health':
            return self.send(200, {'ok': True, 'version': '0.8-reliability', 'local_only': True})
        if path == '/api/response-plans':
            options = response_plans.listing_options(parse_qs(urlparse(self.path).query, keep_blank_values=True))
            with database() as db:
                result = response_plans.list_versions(db, **options)
            return self.send(200, result)
        if path.startswith('/api/response-plans/'):
            parts = path.split('/')
            if len(parts) not in (4, 5) or (len(parts) == 5 and parts[4] != 'export'):
                return self.send(404, {'error': '接口不存在。'})
            monitor = response_plan_monitor()
            with database() as db:
                document = response_plans.load_version(db, parts[3])
                pilot = load_pilot(db)
            document = response_plans.with_update_status(document, pilot['data'], monitor, time.time())
            if len(parts) == 4:
                return self.send(200, {'document': document})
            formats = parse_qs(urlparse(self.path).query).get('format', ['html'])
            if len(formats) != 1 or formats[0] not in ('html', 'json'):
                raise ValidationError('导出格式仅支持 html 或 json。')
            fmt = formats[0]
            name = 'response-plan-draft-v' + str(document['revision']) + '.' + fmt
            display_name = '应急预案草案-v' + str(document['revision']) + '.' + fmt
            attachment = 'attachment; filename="' + name + '"; filename*=UTF-8\'\'' + quote(display_name)
            body = response_plans.export_html(document) if fmt == 'html' else json.dumps(document, ensure_ascii=False, indent=2).encode('utf-8')
            return self.send(200, body, 'text/html; charset=utf-8' if fmt == 'html' else 'application/json; charset=utf-8', attachment=attachment)
        if path in ('/api/monitor', '/api/monitor/config'):
            if MONITOR is None:
                return self.send(503, {'error': '自动监测服务尚未启动。'})
            try:
                result = MONITOR.get_config() if path.endswith('/config') else MONITOR.snapshot()
                return self.send(200, result)
            except Exception:
                return self.send(503, {'error': '监测状态暂不可用，请检查本机监测配置；未以旧状态替代。'})
        if path == '/api/live':
            try:
                return self.send(200, LIVE.snapshot())
            except Exception:
                return self.send(503, {'error':'最新数据暂未取得，请稍后重试；没有使用演练数据替代。'})
        files = {'/': ('index.html','text/html; charset=utf-8'), '/workflow.js': ('workflow.js','text/javascript; charset=utf-8'), '/app.js': ('app.js','text/javascript; charset=utf-8'), '/style.css': ('style.css','text/css; charset=utf-8')}
        files['/pilot.js'] = ('pilot.js', 'text/javascript; charset=utf-8')
        files['/monitor.js'] = ('monitor.js', 'text/javascript; charset=utf-8')
        files['/security.js'] = ('security.js', 'text/javascript; charset=utf-8')
        files['/response_plans.js'] = ('response_plans.js', 'text/javascript; charset=utf-8')
        files['/draft_guard.js'] = ('draft_guard.js', 'text/javascript; charset=utf-8')
        if path in files:
            filename, mime = files[path]
            return self.send(200, (ROOT / 'web' / filename).read_bytes(), mime)
        return self.send(404, {'error':'页面不存在。'})

    def do_POST(self):
        self.session = None
        self._body_consumed = False
        origin = self.headers.get('Origin')
        if not self.valid_host() or origin != 'http://' + self.headers.get('Host', '') or self.headers.get('Sec-Fetch-Site') == 'cross-site':
            return self.send(403, {'error':'同源请求校验失败。'})
        path = urlparse(self.path).path
        if path not in ('/api/generate', '/api/live/reference', '/api/runs', '/api/run', '/api/workflow', '/api/pilot', '/api/agent/inspect', '/api/trials/start', '/api/trials/stop', '/api/trials/finish', '/api/trials', '/api/monitor/config', '/api/monitor/ingest', '/api/monitor/correction', '/api/monitor/refresh', '/api/auth/login', '/api/auth/setup', '/api/auth/logout', '/api/auth/password', '/api/auth/users', '/api/response-plans/preview', '/api/response-plans/save', '/api/response-plans/review'):
            return self.send(404, {'error':'接口不存在。'})
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            return self.send(415, {'error':'仅支持JSON资料。'})
        try:
            public_auth = path in ('/api/auth/login', '/api/auth/setup')
            if AUTH is None:
                raise AuthError('安全登录服务未启动。', 503)
            if not public_auth:
                self.require_session()
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 512000:
                return self.send(413, {'error':'资料为空或超过500KB。'})
            raw_payload = self.rfile.read(length)
            self._body_consumed = True
            payload = json.loads(raw_payload)
            if not isinstance(payload, dict):
                raise ValidationError('请求必须为对象。')
            if public_auth:
                result = AUTH.setup(payload) if path.endswith('/setup') else AUTH.login(payload)
                if MONITOR is not None:
                    MONITOR.start()
                return self.send(200, {'user': result['user'], 'csrf_token': result['csrf_token']}, cookie=self.cookie(result['session_id']))
            role = self.session['user']['role']
            read_only = path in ('/api/runs', '/api/run', '/api/trials', '/api/agent/inspect') or (path == '/api/pilot' and payload.get('action') != 'save')
            if path in ('/api/monitor/config', '/api/auth/users', '/api/response-plans/review') or (path == '/api/pilot' and payload.get('action') == 'save'):
                if role != 'admin':
                    raise AuthError('此操作仅限管理员。', 403)
            elif not read_only and path not in ('/api/auth/logout', '/api/auth/password') and role == 'viewer':
                raise AuthError('只读账号不能修改资料或触发采集。', 403)
            if path == '/api/auth/logout':
                AUTH.logout(self.session)
                return self.send(200, {'ok': True}, cookie=self.cookie(clear=True))
            if path == '/api/auth/password':
                AUTH.change_password(self.session, payload)
                return self.send(200, {'ok': True}, cookie=self.cookie(clear=True))
            if path == '/api/auth/users':
                return self.send(200, AUTH.manage_user(self.session['user']['username'], payload))
            if path.startswith('/api/response-plans/'):
                monitor = response_plan_monitor()
                actor = self.session['user']['username']
                with database() as db:
                    if path != '/api/response-plans/preview':
                        db.execute('BEGIN IMMEDIATE')
                    pilot = load_pilot(db)
                    if path == '/api/response-plans/review':
                        document = response_plans.review_version(db, payload, actor, pilot['data'], monitor, time.time())
                    else:
                        document = response_plans.build_document(pilot['data'], monitor, payload, actor, time.time(), pilot['revision'])
                        if path == '/api/response-plans/save':
                            document = response_plans.append_version(db, document, payload.get('expected_revision'))
                    revision = response_plans.current_revision(db)
                return self.send(200, {'document': document, 'revision': revision})
            if path in ('/api/monitor/correction', '/api/workflow'):
                # User identity comes from the authenticated session, not the form.
                payload['actor'] = self.session['user']['username']
            if path.startswith('/api/monitor/'):
                if MONITOR is None:
                    return self.send(503, {'error': '自动监测服务尚未启动。'})
                if path == '/api/monitor/config':
                    return self.send(200, MONITOR.save_config(payload))
                if path == '/api/monitor/ingest':
                    return self.send(200, MONITOR.ingest(payload))
                if path == '/api/monitor/correction':
                    return self.send(200, MONITOR.correct(payload))
                result = MONITOR.request_poll()
                if not result['queued']:
                    return self.send(503, {'error': '后台采集线程未运行，请重新启动本机服务。'})
                return self.send(202, result)
            if path == '/api/agent/inspect':
                data = validate_dataset(payload.get('data'))
                return self.send(200, {'readiness': readiness(data), 'input_fingerprint': fingerprint(data),
                                       'outbound_preview': model_input(data), 'external_call_made': False})
            if path == '/api/pilot':
                with database() as db:
                    db.execute('BEGIN IMMEDIATE')
                    saved = load_pilot(db)
                    if payload.get('action') == 'save':
                        if type(payload.get('revision')) is not int or payload['revision'] != saved['revision']:
                            raise ValidationError('试点资料已更新，请刷新后重新编辑。')
                        data = validate_dataset(payload.get('data'))
                        if data['site'].get('campus') != '紫金港校区' or data['site']['region'] != '杭州市':
                            raise ValidationError('本试点资料限定杭州市紫金港校区；其他资料可临时导入演练。')
                        saved = {'revision': saved['revision'] + 1, 'data': data}
                        db.execute('INSERT OR REPLACE INTO pilot VALUES (1,?,?)', (saved['revision'], json.dumps(data, ensure_ascii=False)))
                saved['readiness'] = readiness(saved['data'])
                return self.send(200, saved)
            if path.startswith('/api/trials'):
                with database() as db:
                    db.execute('BEGIN IMMEDIATE')
                    if path == '/api/trials/start':
                        value = start_trial(db, payload)
                    elif path == '/api/trials/stop':
                        value = stop_trial(db, payload)
                    elif path == '/api/trials/finish':
                        value = finish_trial(db, payload)
                    else:
                        value = {'trials': [json.loads(row[0]) for row in db.execute('SELECT record_json FROM trials ORDER BY started_at DESC LIMIT 100')]}
                return self.send(200, value)
            if path == '/api/runs':
                with database() as db:
                    rows = db.execute('SELECT id, created_at, mode, output_json FROM runs ORDER BY created_at DESC LIMIT 50').fetchall()
                return self.send(200, {'runs': [{'run_id': r[0], 'created_at': r[1], 'mode': r[2],
                    'site': json.loads(r[3])['site']['name'], 'count': len(json.loads(r[3])['cards'])} for r in rows]})
            if path in ('/api/run', '/api/workflow'):
                if not isinstance(payload.get('run_id'), str):
                    raise ValidationError('运行编号必须为文字。')
                with database() as db:
                    # Serialize read-modify-write; revision rejects stale browser updates.
                    if path == '/api/workflow':
                        db.execute('BEGIN IMMEDIATE')
                    row = db.execute('SELECT output_json FROM runs WHERE id=?', (payload['run_id'],)).fetchone()
                    if row is None:
                        raise ValidationError('运行记录不存在。')
                    result = initialize(json.loads(row[0]))
                    if path == '/api/workflow':
                        result = mutate(result, payload.get('operation'), payload)
                        db.execute('UPDATE runs SET output_json=? WHERE id=?',
                                   (json.dumps(result, ensure_ascii=False), payload['run_id']))
                return self.send(200, result)
            if path == '/api/live/reference':
                snapshot = LIVE.snapshot()
                weather = make_reference(snapshot)
                with database() as db:
                    db.execute('INSERT OR REPLACE INTO live_snapshots VALUES (?,?,?)',
                               (weather['live_snapshot_id'], json.dumps(weather, ensure_ascii=False), snapshot['served_at']))
                return self.send(200, {'weather':weather, 'snapshot':snapshot})
            verify_live_reference(payload.get('data'))
            mode = payload.get('mode', 'rules')
            if mode == 'ai' and payload.get('external_data_confirmed') is not True:
                raise ValidationError('请先确认本次资料可发送给本地配置的模型服务。')
            start = time.perf_counter()
            if payload.get('trial_id'):
                with database() as db:
                    check_generation(db, payload['trial_id'], payload.get('data'), mode)
            result = run_agent(payload.get('data'), mode, CONFIG, payload.get('scope_confirmed'), payload.get('operation_mode', 'tabletop'))
            result.update({'run_id': uuid.uuid4().hex[:12], 'created_at': datetime.now(timezone.utc).isoformat(),
                           'elapsed_ms': round((time.perf_counter() - start) * 1000), 'input_snapshot': payload['data']})
            initialize(result)
            with database() as db:
                db.execute('BEGIN IMMEDIATE')
                if payload.get('trial_id'):
                    trial = check_generation(db, payload['trial_id'], payload['data'], mode)
                    trial['run_id'] = result['run_id']
                    save_trial(db, trial)
                db.execute('INSERT INTO runs VALUES (?,?,?,?,?)', (result['run_id'], result['created_at'], mode,
                    json.dumps(payload['data'], ensure_ascii=False), json.dumps(result, ensure_ascii=False)))
            return self.send(200, result)
        except AuthError as exc:
            return self.send(exc.status, {'error': str(exc)})
        except StorageSecurityError:
            return self.send(503, {'error': '受保护存储暂不可用，未降级为明文。'})
        except response_plans.PlanConflict as exc:
            return self.send(409, {'error': str(exc)})
        except (ValidationError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            return self.send(400, {'error': str(exc)})
        except Exception:
            return self.send(500, {'error':'处理未完成，结果没有保存。请检查资料或本地运行环境。'})

def main():
    global CONFIG, DB_PATH, LIVE, MONITOR, AUTH
    parser = argparse.ArgumentParser(description='Local automatic weather and water monitoring workbench')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--db', type=Path, default=DB_PATH)
    args = parser.parse_args()
    # Preserve the original path until link checks; resolve() would hide a linked
    # runtime ancestor or linked database from the storage boundary.
    DB_PATH = args.db.absolute()
    # Bind first: never migrate a database while an old version owns this port.
    server = LocalServer(('127.0.0.1', args.port), Handler)
    try:
        # Different ports still share accounts/configuration when runtime is the same.
        # Acquire the lifetime lock before reading or migrating any protected data.
        with running_instance(DB_PATH.parent):
            monitor = None
            try:
                CONFIG = read_config(DB_PATH.parent)
                AUTH = AuthService(DB_PATH.parent)
                LIVE = LiveService(DB_PATH.parent / 'live_cache_hangzhou.json', location=ZJU_LOCATION,
                                   fetcher=lambda name: fetch_provider(name, ZJU_LOCATION, 'ZSHC', '杭州萧山国际机场'))
                MONITOR = monitor = MonitorService(DB_PATH, LIVE, monitoring_pilot, vision_config=CONFIG)
                if not AUTH.status()['setup_required']:
                    MONITOR.start()
                print(f'Local prototype: http://127.0.0.1:{server.server_port}', flush=True)
                if AUTH.status()['setup_required']:
                    print('Create the first administrator in the browser. Local setup code: ' + str(AUTH.setup_path), flush=True)
                print('Authenticated local service. Camera data cannot be sent to public model endpoints. Stop with Ctrl+C.', flush=True)
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                try:
                    if monitor is not None:
                        monitor.stop()
                finally:
                    # ThreadingMixIn joins accepted request handlers here. Keep
                    # the instance lock until their legitimate writes finish.
                    server.server_close()
    except InstanceRunningError as exc:
        parser.exit(1, str(exc) + '\n')
    finally:
        server.server_close()

if __name__ == '__main__':
    main()
