"""Public current-data connectors. Model estimates and station observations stay separate."""
import copy
import hashlib
import json
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BEIJING = timezone(timedelta(hours=8))
POLL_SECONDS = 600
LOCATION = {'name': '福州城区参考坐标', 'region': '福州市', 'latitude': 26.0745, 'longitude': 119.2965,
            'note': '城市参考坐标，非真实校园或积水监测点。'}
ZJU_LOCATION = {'name': '杭州城区参考网格', 'region': '杭州市', 'latitude': 30.2741, 'longitude': 120.1551,
                'note': '杭州城市参考坐标，非紫金港测站或精确校区坐标。'}
MODEL_URL = 'https://api.open-meteo.com/v1/forecast?' + urlencode({
    'latitude': LOCATION['latitude'], 'longitude': LOCATION['longitude'],
    'current': 'temperature_2m,relative_humidity_2m,precipitation,wind_speed_10m,weather_code',
    'hourly': 'precipitation,wind_speed_10m', 'forecast_days': 2,
    'wind_speed_unit': 'ms', 'precipitation_unit': 'mm', 'timezone': 'Asia/Shanghai', 'timeformat': 'unixtime'})
METAR_URL = 'https://aviationweather.gov/api/data/metar?ids=ZSFZ&format=json'
OFFICIAL_SOURCE_REGISTRY = [
    {'id':'CMA-TYPHOON-PRODUCTS','name':'中国气象数据网·台风及相关气象产品',
     'url':'https://data.cma.cn/','status':'authorization_required',
     'use':'历史台风、站点降雨和气象产品申请；未调用账户内接口。'},
    {'id':'NMC-TYPHOON','name':'中央气象台台风网','url':'http://typhoon.nmc.cn/',
     'status':'manual_verification_only','use':'人工核对台风位置、路径和预报展示；未取得机器接口。'},
    {'id':'ZJ-CMA-PORTAL','name':'浙江省气象局','url':'http://zj.cma.gov.cn/',
     'status':'endpoint_required','use':'浙江属地预警和气象服务候选来源；待正式接口及授权。'},
    {'id':'ZJ-WATER-PORTAL','name':'浙江省水利厅','url':'https://slt.zj.gov.cn/',
     'status':'endpoint_required','use':'浙江雨量、水位、河道和水库候选来源；待水雨情接口及授权。'},
]

def iso(epoch):
    return datetime.fromtimestamp(epoch, BEIJING).isoformat(timespec='seconds')

def finite(value, low=None, high=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    if low is not None and value < low or high is not None and value > high:
        return None
    return value

def epoch(value):
    value = finite(value, 0, 4102444800)
    if value is None:
        raise ValueError('数据缺少有效时间戳。')
    return value

def freshness(data_epoch, now, max_age):
    age = now - data_epoch
    if age < -300:
        return 'future'
    return 'fresh' if age <= max_age else 'stale'

def model_url(location):
    return MODEL_URL.replace('latitude=26.0745', 'latitude=' + str(location['latitude'])).replace('longitude=119.2965', 'longitude=' + str(location['longitude']))

def parse_model(raw, location=None):
    location = location or LOCATION
    if not isinstance(raw, dict):
        raise ValueError('模型响应不是对象。')
    units, hourly_units = raw.get('current_units', {}), raw.get('hourly_units', {})
    for obj, required in ((units, {'time':'unixtime', 'temperature_2m':'°C', 'relative_humidity_2m':'%', 'precipitation':'mm', 'wind_speed_10m':'m/s'}),
                          (hourly_units, {'time':'unixtime', 'precipitation':'mm', 'wind_speed_10m':'m/s'})):
        if any(obj.get(key) != unit for key, unit in required.items()):
            raise ValueError('模型返回的单位与请求不符，未进行猜测换算。')
    current = raw.get('current', {})
    weather_code = current.get('weather_code')
    if weather_code is not None:
        # Older cached fixtures may omit this optional field; never guess a code.
        if (isinstance(weather_code, bool) or not isinstance(weather_code, int)
                or weather_code not in (0, 1, 2, 3, 45, 48, 51, 53, 55, 56, 57,
                                        61, 63, 65, 66, 67, 71, 73, 75, 77,
                                        80, 81, 82, 85, 86, 95, 96, 99)
                or units.get('weather_code') != 'wmo code'):
            raise ValueError('模型天气代码或单位无效。')
    data_epoch = epoch(current.get('time'))
    interval = finite(current.get('interval'), 1, 3600)
    if interval is None:
        raise ValueError('模型未提供有效时段长度。')
    lat, lon = finite(raw.get('latitude'), -90, 90), finite(raw.get('longitude'), -180, 180)
    if lat is None or lon is None or abs(lat-location['latitude']) > 0.5 or abs(lon-location['longitude']) > 0.5:
        raise ValueError('模型返回坐标与请求范围不符。')
    hours = raw.get('hourly', {})
    arrays = [hours.get(k) for k in ('time', 'precipitation', 'wind_speed_10m')]
    if not all(isinstance(a, list) for a in arrays) or len({len(a) for a in arrays}) != 1:
        raise ValueError('逐小时序列不完整。')
    rows, seen = [], set()
    for t, rain, wind in zip(*arrays):
        t = epoch(t)
        if t in seen:
            raise ValueError('逐小时序列有重复时间。')
        seen.add(t)
        rows.append({'time_epoch': t, 'time':iso(t), 'window_start':iso(t-3600),
                     'precipitation_mm':finite(rain,0,1000), 'wind_speed_ms':finite(wind,0,150)})
    return {'kind':'model_estimate', 'provider':'Open-Meteo', 'source_url':model_url(location),
            'docs_url':'https://open-meteo.com/en/docs', 'attribution':'Weather data by Open-Meteo.com · CC BY 4.0',
            'location': {'requested':location, 'grid_latitude':lat, 'grid_longitude':lon},
            'data_epoch':data_epoch, 'data_time':iso(data_epoch), 'max_age_seconds':3600,
            'model_run_time':None, 'interval_seconds':interval,
            'temperature_c':finite(current.get('temperature_2m'),-100,70),
            'humidity_percent':finite(current.get('relative_humidity_2m'),0,100),
            'precipitation_mm':finite(current.get('precipitation'),0,1000),
            'wind_speed_ms':finite(current.get('wind_speed_10m'),0,150),
            'weather_code':weather_code,
            'hourly':sorted(rows,key=lambda r:r['time_epoch']),
            'note':'当前值来自天气模型，非地面站实测；有效时刻不等于模型起报时间。中国区域的15分钟值可能由小时模型插值。'}

def parse_metar(raw, station_id='ZSFZ', station_name='福州长乐国际机场'):
    if not isinstance(raw, list):
        raise ValueError('站点响应不是数组。')
    rows = [r for r in raw if isinstance(r, dict) and r.get('icaoId') == station_id]
    if not rows:
        raise ValueError('未取得' + station_id + '的近期报文。')
    row = max(rows, key=lambda r:epoch(r.get('obsTime')))
    data_epoch = epoch(row.get('obsTime'))
    raw_text = row.get('rawOb')
    if not isinstance(raw_text, str) or len(raw_text) > 5000:
        raise ValueError('报文缺失或长度异常。')
    wind = re.search(r'\b(?P<direction>\d{3}|VRB)(?P<speed>\d{2,3})(?:G(?P<gust>\d{2,3}))?(?P<unit>MPS|KT)\b',raw_text)
    wind_speed = gust = direction = None
    if wind:
        factor = 1 if wind['unit'] == 'MPS' else 1852/3600
        wind_speed = finite(round(int(wind['speed'])*factor,2),0,150)
        gust = finite(round(int(wind['gust'])*factor,2),0,150) if wind['gust'] else None
        direction = finite(int(wind['direction']),0,360) if wind['direction'] != 'VRB' else None
    return {'kind':'station_observation','provider':'NOAA Aviation Weather Center · METAR',
            'source_url':METAR_URL.replace('ZSFZ', station_id), 'docs_url':'https://aviationweather.gov/data/api/',
            'station_id':station_id, 'station_name':station_name,
            'latitude':finite(row.get('lat'),-90,90), 'longitude':finite(row.get('lon'),-180,180),
            'data_epoch':data_epoch, 'data_time':iso(data_epoch), 'max_age_seconds':7200,
            'temperature_c':finite(row.get('temp'),-100,70), 'wind_speed_ms':wind_speed,
            'wind_gust_ms':gust, 'wind_direction_degrees':direction,
            'weather_code':str(row.get('wxString') or '未提供天气现象代码'),
            'precipitation_mm':None, 'raw_report':raw_text,
            'note':'机场站实测报文，不代表城区或校园点位。本接入未提供累计降水；空值不等于0。风速从原始MPS/KT报文换算。'}

def fetch_json(url):
    request = Request(url,headers={'User-Agent':'Laoyouzhunbei/0.2 (local educational prototype)', 'Accept':'application/json'})
    with urlopen(request,timeout=15) as response:
        raw = response.read(1024*1024+1)
        if response.status == 204 or not raw:
            raise ValueError('上游当前无资料。')
        if len(raw) > 1024*1024:
            raise ValueError('上游响应超过限制。')
        return json.loads(raw)

def fetch_provider(name, location=None, station_id='ZSFZ', station_name='福州长乐国际机场'):
    location = location or LOCATION
    url, parser = (model_url(location), lambda raw: parse_model(raw, location)) if name == 'model' else (METAR_URL.replace('ZSFZ', station_id), lambda raw: parse_metar(raw, station_id, station_name))
    try:
        payload = parser(fetch_json(url))
        fetched = time.time()
        payload.update({'fetched_epoch':fetched,'fetched_at':iso(fetched)})
        return payload, None
    except HTTPError as exc:
        return None, f'上游HTTP {exc.code}，未用模拟数据替代。'
    except (URLError, TimeoutError, OSError):
        return None, '网络或上游服务暂不可用，未用模拟数据替代。'
    except (ValueError, KeyError, TypeError, AttributeError):
        return None, '上游资料为空、结构异常或单位不符，未使用该响应。'

class LiveService:
    def __init__(self, cache_path=None, fetcher=fetch_provider, clock=time.time, location=None):
        self.location = location or LOCATION
        self.path = Path(cache_path) if cache_path else None
        self.fetcher, self.clock = fetcher, clock
        self.lock = threading.Lock()
        self.cache = {}
        if self.path and self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding='utf-8'))
                if isinstance(loaded,dict):
                    for name in ('model','station'):
                        entry = loaded.get(name)
                        if not isinstance(entry,dict) or finite(entry.get('checked_epoch')) is None:
                            continue
                        payload = entry.get('data')
                        if payload is not None and (not isinstance(payload,dict) or
                            any(finite(payload.get(k)) is None for k in ('data_epoch','fetched_epoch','max_age_seconds')) or
                            (name == 'model' and not isinstance(payload.get('hourly'),list))):
                            continue
                        self.cache[name] = entry
            except (OSError, ValueError):
                pass

    def snapshot_cancelable(self, cancel_event, commit_lock):
        """Allow the monitor to stop commits without waiting for upstream I/O."""
        return self.snapshot(cancel_event=cancel_event, commit_lock=commit_lock)

    def snapshot(self, *, cancel_event=None, commit_lock=None):
        with self.lock:
            if cancel_event is not None and cancel_event.is_set():
                return None
            now = self.clock()
            due = [name for name in ('model','station') if name not in self.cache or
                   now-self.cache[name].get('checked_epoch',0) >= POLL_SECONDS or
                   self.cache[name].get('checked_epoch',0) > now+300]
            if due:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    fetched = list(zip(due, pool.map(self.fetcher, due)))
                # Only the small local commit shares the monitor's stop lock.
                # Fetching never holds that lock, so stop() remains bounded.
                with commit_lock if commit_lock is not None else nullcontext():
                    if cancel_event is not None and cancel_event.is_set():
                        return None
                    for name, (payload,error) in fetched:
                        entry = self.cache.setdefault(name,{})
                        if payload is not None: entry['data'] = payload
                        entry.update({'checked_epoch':self.clock(),'error':error})
                    if self.path:
                        self.path.parent.mkdir(parents=True,exist_ok=True)
                        temp = self.path.with_suffix('.tmp')
                        try:
                            temp.write_text(json.dumps(self.cache,ensure_ascii=False),encoding='utf-8')
                            temp.replace(self.path)
                        except OSError:
                            pass  # An unwritable cache must not relabel live data as unavailable.
            now = self.clock()
            result = {'location':self.location, 'served_epoch':now, 'served_at':iso(now),
                      'poll_seconds':POLL_SECONDS, 'providers':{},
                      'official_sources':copy.deepcopy(OFFICIAL_SOURCE_REGISTRY),
                      'official_alerts':{'status':'not_connected','message':'官方预警尚未接入；不代表当前无预警。',
                                         'source_url':'http://zj.cma.gov.cn/'},
                      'cma_access':{'status':'authorization_required','message':'中国气象数据网实时接口待授权，未调用账户内接口。'}}
            for name in ('model','station'):
                entry = self.cache.get(name,{})
                payload = copy.deepcopy(entry.get('data'))
                if payload:
                    age = now - payload['data_epoch']
                    state = freshness(payload['data_epoch'],now,payload['max_age_seconds'])
                    if entry.get('error') and state == 'fresh': state = 'degraded'
                    payload.update({'status':state, 'age_minutes':round(age/60,1),
                                    'cache_age_seconds':max(0,round(now-payload['fetched_epoch'])),
                                    'error':entry.get('error'), 'usable':state == 'fresh'})
                    if name == 'model':
                        payload['hourly'] = [r for r in payload['hourly'] if r['time_epoch'] > now][:12]
                else:
                    payload = {'status':'unavailable','usable':False,'error':entry.get('error') or '尚未取得资料。'}
                payload.update({'last_checked_at':iso(entry.get('checked_epoch',now)),
                                'next_check_at':iso(entry.get('checked_epoch',now)+POLL_SECONDS)})
                result['providers'][name] = payload
            identity = json.dumps(result,sort_keys=True,ensure_ascii=False)
            result['snapshot_id'] = hashlib.sha256(identity.encode()).hexdigest()[:16]
            return result

def make_reference(snapshot):
    """Create an immutable, source-labelled reference for a manually selected exercise."""
    good = {name:p for name,p in snapshot['providers'].items() if p.get('usable')}
    if not good:
        raise ValueError('没有可用的新近数据，请等待连接恢复或使用历史演练。')
    lines = ['实时外部资料快照，用于人工选择的演练场景；不是官方预警，也不自动判定暴雨或大风。']
    fmt = lambda v: '未提供' if v is None else str(v)
    if 'model' in good:
        m = good['model']
        lines.append(f"Open-Meteo城区模型估计，有效时刻{m['data_time']}，{m['interval_seconds']/60:g}分钟时段降水{fmt(m['precipitation_mm'])}毫米、10米风速{fmt(m['wind_speed_ms'])}米/秒。非地面站实测，模型起报时间未提供。")
    if 'station' in good:
        s = good['station']
        lines.append(f"{s['station_name']}{s['station_id']}实测，经NOAA AWC取得；观测时刻{s['data_time']}，气温{fmt(s['temperature_c'])}摄氏度、风速{fmt(s['wind_speed_ms'])}米/秒。未提供累计降水，不代表城区或校园状态。")
    lines.append('官方预警接口未接入，现场积水状态未知。完整来源和获取时刻保存在live_sources中。')
    location = snapshot.get('location', LOCATION)
    return {'id':'LIVE-'+snapshot['snapshot_id'],'title':location['region'] + '最新气象快照 · 演练参考',
            'publisher':'Open-Meteo / NOAA AWC（可用来源见快照）',
            'source_url':good.get('model',good.get('station'))['source_url'],
            'published_at':snapshot['served_at'],'replay_at':snapshot['served_at'],
            'region':location['region'],'hazards':[],'level':'未说明','mode':'live_reference',
            'text_kind':'实时数据快照摘要（非官方预警）','text':'\n'.join(lines),
            'scope_note':'城市模型与机场分别展示；不得解释为具体校园已受灾。',
            'live_snapshot_id':snapshot['snapshot_id'],'live_sources':good,
            'official_alert_status':'not_connected'}
