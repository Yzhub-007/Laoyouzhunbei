"""Source-bound emergency response drafts; no model calls or operational approval."""
import copy
import hashlib
import html
import json
import math
import re
import uuid
from datetime import datetime, timezone

from engine import ValidationError, timestamp


DETAIL_FIELDS = ('title', 'contact_roster', 'resources', 'activation', 'escalation', 'evacuation', 'recheck')
DETAIL_LABELS = {'contact_roster': '责任岗位、联系人与联系方式', 'resources': '物资、设备与可用情况',
                 'activation': '启动条件与有权决定岗位', 'escalation': '报告渠道与升级条件',
                 'evacuation': '经管理方确认的疏散路线及适用条件', 'recheck': '复查责任、周期与结束条件'}
STATE_LABELS = {'unknown': '现状未知', 'clear': '当前证据未检出积水（不等于安全通行）',
                'suspected': '积水信号待确认', 'pooling_suspected': '画面疑似积水',
                'pooling': '水深监测达到内部关注阈值', 'high_water': '水深监测达到内部较高关注阈值'}
LIMITATIONS = [
    '本文件是应急预案草案；内部审阅不代表管理方正式批准、发布或生效。所有任务均待人工核验。',
    '监测记录仅反映生成时可用证据；保存或导出的快照不是实时状态，执行前须重新获取并核验。',
    '图像识别只提供疑似积水线索，不能据此估计水深、通行安全或官方预警等级。缺测与低质量画面不等于无积水。',
    '城市气象模型与机场观测不代表校园点位实测；未接入官方预警，不能据此自行发布预警。',
    '联系人、资源、启动条件、报告及疏散路线仅引用管理人员补充资料，系统不编造缺失内容，也不自动下达处置命令。',
]


class PlanConflict(ValidationError):
    pass


def iso(now):
    return datetime.fromtimestamp(now, timezone.utc).isoformat()


def epoch(value):
    try:
        return timestamp(value).timestamp()
    except (ValidationError, OverflowError, OSError):
        return None


def text(value, maximum=4000):
    return value[:maximum] if isinstance(value, str) else ''


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def validated_input(payload, dataset):
    if not isinstance(payload, dict) or set(payload) - {'point_ids', 'details', 'expected_revision'}:
        raise ValidationError('预案请求仅接受 point_ids、details 与保存版本号。')
    ids = payload.get('point_ids')
    available = {p['id']: p for p in dataset.get('points', [])}
    if (not isinstance(ids, list) or not 1 <= len(ids) <= 30
            or not all(isinstance(p, str) and p in available for p in ids) or len(set(ids)) != len(ids)):
        raise ValidationError('请选择 1—30 个已保存且不重复的点位。')
    details = payload.get('details', {})
    if not isinstance(details, dict) or set(details) - set(DETAIL_FIELDS):
        raise ValidationError('预案补充资料字段不符合约定。')
    result = {}
    for name in DETAIL_FIELDS:
        value = details.get(name, '')
        limit = 160 if name == 'title' else 4000
        if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 and c not in '\r\n\t' for c in value):
            raise ValidationError('预案补充资料需为有效文字；标题限 160 字，其余每项限 4000 字。')
        result[name] = value.strip()
    site = dataset.get('site', {})
    if any(available[p].get('region') != site.get('region')
           or (site.get('campus') and available[p].get('campus') != site['campus']) for p in ids):
        raise ValidationError('所选点位必须与已保存场地的地区和校区一致。')
    return ids, result


def checked(row, now):
    when = epoch(row.get('verified_at'))
    return (row.get('status') == 'verified' and bool(text(row.get('verified_by')).strip())
            and when is not None and when <= now
            and not any(marker in text(row.get(key)) for key in ('name', 'source', 'role')
                        for marker in ('待填', '采集槽位', '待确认', '尚非浙江大学正式')))


def authorization_ok(site, now):
    auth = site.get('authorization', {})
    if not isinstance(auth, dict):
        return False
    when = epoch(auth.get('confirmed_at'))
    return (auth.get('status') == 'confirmed' and when is not None and when <= now
            and all(text(auth.get(k)).strip() for k in ('approver_role', 'reference')))


def safe_observations(dataset, point_ids, monitor, now):
    monitor = monitor if isinstance(monitor, dict) else {}
    served = epoch(monitor.get('served_at'))
    snapshot_fresh = served is not None and 0 <= now - served <= 60
    source_rows = monitor.get('sources', []) if snapshot_fresh else []
    assessed = {p.get('point_id'): p for p in monitor.get('points', []) if isinstance(p, dict)} if snapshot_fresh else {}
    points = []
    for p in dataset['points']:
        if p['id'] not in point_ids:
            continue
        raw = assessed.get(p['id'], {})
        sources, valid_times, aggregate_expired = [], [], False
        for s in source_rows:
            if not isinstance(s, dict) or s.get('point_id') != p['id']:
                continue
            observed = epoch(s.get('observed_at'))
            max_age = s.get('max_age_seconds', 300)
            valid_age = type(max_age) in (int, float) and math.isfinite(max_age) and 0 < max_age <= 86400
            status = text(s.get('status'), 30)
            if status == 'fresh' and (not valid_age or observed is None or not 0 <= now - observed < max_age):
                status = 'stale' if observed is not None and observed <= now else 'invalid_time'
                aggregate_expired = True
            if status == 'fresh':
                valid_times.append(observed)
            sources.append({'id': text(s.get('id'), 60), 'name': text(s.get('name'), 160),
                            'kind': text(s.get('kind'), 60), 'status': status,
                            'observed_at': iso(observed) if observed is not None else None})
        correction = raw.get('correction') or {}
        correction_at, correction_until = epoch(correction.get('created_at')), epoch(correction.get('expires_at'))
        corrected = (correction.get('active') is True and correction_at is not None and correction_until is not None
                     and correction_at <= now < correction_until and not correction.get('invalidated_reason'))
        # A correction can expire between snapshot acquisition and composition.
        # Never retain its reassuring state once its validity has ended.
        automatic = raw.get('automatic_state', raw.get('state', 'unknown'))
        if aggregate_expired:
            # The aggregation may have relied on a source that expired after
            # snapshot acquisition. A different fresh source does not prove it.
            automatic = 'unknown'
        state = raw.get('state', 'unknown') if corrected else automatic if valid_times else 'unknown'
        if state not in STATE_LABELS:
            state = 'unknown'
        basis = []
        if valid_times:
            basis.append('仅采用生成时仍在有效期内的已登记来源；请在监测页核验原始依据。')
        if corrected:
            basis.append('包含有效人工校正；校正到期时间：' + iso(correction_until))
        if state == 'unknown':
            basis.append('未取得有效点位判断；不能解释为没有积水。')
        if aggregate_expired:
            basis.append('部分来源在编制时已到期，原自动汇总已撤回，需重新获取监测快照。')
        if raw.get('conflict') is True:
            basis.append('来源结论冲突，需要人工核验。')
        points.append({'point_id': p['id'], 'name': text(p.get('name'), 500), 'type': text(p.get('type'), 500),
                       'state': state, 'label': STATE_LABELS[state] + ('（人工校正）' if corrected else ''),
                       'observed_at': iso(max(valid_times + ([correction_at] if corrected else []))) if valid_times or corrected else None,
                       'verified': checked(p, now) and dataset['site'].get('status') == 'verified',
                       'conflict': bool(raw.get('conflict')) if snapshot_fresh else False,
                       'sources': sources, 'basis': basis})
    weather_raw = monitor.get('weather', {}) if snapshot_fresh else {}
    providers = [p for p in weather_raw.get('providers', []) if isinstance(p, dict) and p.get('status') == 'fresh'
                 and epoch(p.get('observed_at')) is not None and epoch(p.get('observed_at')) <= now]
    weather_valid = bool(providers) and weather_raw.get('region') == dataset['site'].get('region')
    weather = {'state': text(weather_raw.get('state'), 40) if weather_valid else 'unknown',
               'label': text(weather_raw.get('label'), 120) if weather_valid else '天气参考未知',
               'observed_at': iso(max(epoch(p['observed_at']) for p in providers)) if weather_valid else None,
               'status': 'fresh' if weather_valid else 'unknown',
               'hazards': [h for h in weather_raw.get('hazards', []) if h in ('rain', 'wind')] if weather_valid else []}
    return {'captured_at': iso(now), 'weather': weather, 'points': points}


def build_document(dataset, monitor, payload, actor, now, pilot_revision=0):
    ids, details = validated_input(payload, dataset)
    site = dataset['site']
    observations = safe_observations(dataset, ids, monitor, now)
    original = {p['id']: p for p in dataset['points']}
    missing, sources, tasks = [], {}, []
    scope_verified = site.get('status') == 'verified' and bool(text(site.get('scope')).strip())
    if not scope_verified:
        missing.append('场地名称、管理边界及适用范围尚待核实。')
    if not authorization_ok(site, now):
        missing.append('尚缺管理方授权记录或有效确认时间。')
    for key, label in DETAIL_LABELS.items():
        if not details[key]:
            missing.append('待补充：' + label + '。')
    if observations['weather']['status'] != 'fresh':
        missing.append('没有可用的同地区新近天气参考；历史/模拟天气未作为当前观测。')
    for point in observations['points']:
        pid, p = point['point_id'], original[point['point_id']]
        if not point['verified']:
            missing.append(pid + '：点位身份、归属或现场资料尚未完成核验。')
        if point['state'] == 'unknown':
            missing.append(pid + '：当前洪涝现状未知，需补充有效监测或有权人员核验。')
        if point['conflict']:
            missing.append(pid + '：监测证据冲突。')
        matched = 0
        for plan in dataset.get('plans', []):
            if (p.get('type') not in plan.get('point_types', [])
                    or (plan.get('point_ids') and pid not in plan['point_ids'])
                    or (plan.get('campus') and plan['campus'] != site.get('campus'))
                    or (plan.get('region') and plan['region'] != site.get('region'))):
                continue
            valid_until = epoch(plan.get('valid_until'))
            if plan.get('valid_until') and (valid_until is None or now >= valid_until):
                missing.append(pid + '：条目 ' + plan['id'] + ' 已过期或有效期无效，未列入建议任务。')
                continue
            matched += 1
            verified = checked(plan, now)
            if plan['id'] not in sources:
                sources[plan['id']] = {'id': plan['id'], 'title': text(plan.get('title'), 2000),
                    'source': text(plan.get('source'), 2000), 'status': 'verified' if verified else 'draft',
                    'recorded_status': text(plan.get('status'), 40), 'verified_at': plan.get('verified_at') if verified else None,
                    'valid_until': iso(valid_until) if valid_until is not None else None, 'point_ids': []}
                if not verified:
                    missing.append('条目 ' + plan['id'] + ' 为草稿或缺少有效审核凭据，需管理方确认。')
                if not plan.get('valid_until'):
                    missing.append('条目 ' + plan['id'] + ' 未注明有效期限，需确认仍为现行版本。')
            sources[plan['id']]['point_ids'].append(pid)
            hazards = set(observations['weather']['hazards'])
            if point['state'] not in ('unknown', 'clear'):
                hazards.add('rain')
            triggered = bool(hazards.intersection(plan.get('hazards', [])))
            tasks.append({'id': pid + ':' + plan['id'], 'point_id': pid, 'point_name': point['name'],
                'plan_id': plan['id'], 'title': text(plan.get('title'), 2000), 'action': text(plan.get('action'), 2000),
                'role': text(plan.get('role'), 2000), 'status': 'pending_review', 'source_status': 'verified' if verified else 'draft',
                'trigger_state': 'signal_present' if triggered else 'standby',
                'basis': ['按点位类型、适用范围及有效期匹配保存条目。',
                          '当前参考信号涉及条目灾种，需人工核验启动条件。' if triggered else '备用条目；尚无有效信号证明已达到启动条件。']})
        if not matched:
            missing.append(pid + '：没有适用且有效的预案条目，需管理方补录；不能据此判断安全。')
    title = details['title'] or text(site.get('name'), 110) + ' · 应急预案草案'
    return {'schema_version': 'response-plan-1', 'id': None, 'revision': None, 'title': title,
            'status': 'draft', 'status_label': '应急预案草案 · 待人工核验', 'created_at': iso(now), 'created_by': actor,
            'parent_id': None, 'publishable': False, 'scope': {'site_name': text(site.get('name'), 200),
                'region': text(site.get('region'), 200), 'campus': text(site.get('campus'), 1000),
                'description': text(site.get('scope'), 1000), 'verified': scope_verified, 'point_ids': ids},
            'details': details, 'observations': observations, 'sources': list(sources.values()), 'tasks': tasks,
            'missing_items': list(dict.fromkeys(missing)), 'limitations': list(LIMITATIONS), 'review': None,
            'input_fingerprint': digest(dataset), 'pilot_revision': pilot_revision,
            'update_status': {'checked_at': iso(now), 'needs_refresh': False,
                              'messages': ['本次草案采用生成时可用证据；执行前必须重新核验。']}}


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS response_plan_versions '
               '(id TEXT PRIMARY KEY, revision INTEGER NOT NULL UNIQUE, created_at TEXT NOT NULL, document_json TEXT NOT NULL)')


def current_revision(db):
    return db.execute('SELECT COALESCE(MAX(revision),0) FROM response_plan_versions').fetchone()[0]


def listing_options(query):
    allowed = {'limit', 'before', 'q', 'point_id', 'status'}
    if set(query) - allowed or any(not isinstance(v, list) or len(v) != 1 for v in query.values()):
        raise ValidationError('版本查询参数无效或重复。')
    result = {k: v[0] for k, v in query.items()}
    for name in ('limit', 'before'):
        if name in result:
            value = result[name]
            if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,19}', value):
                raise ValidationError('分页数量与版本游标必须为正整数。')
            result[name] = int(value)
    return result


def list_versions(db, *, limit=30, before=None, q='', point_id='', status=''):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValidationError('每页版本数须为 1—100。')
    if before is not None and (type(before) is not int or not 1 <= before <= 9223372036854775807):
        raise ValidationError('版本游标必须为有效正整数。')
    for value, maximum in ((q, 100), (point_id, 100), (status, 30)):
        if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 for c in value):
            raise ValidationError('版本筛选条件过长或包含无效字符。')
    if status not in ('', 'draft', 'reviewed_draft'):
        raise ValidationError('版本状态只能为待复核草案或内部复核草案。')
    clauses, values = [], []
    if q.strip():
        clauses.append("instr(lower(json_extract(document_json, '$.title')), lower(?)) > 0")
        values.append(q.strip())
    if point_id:
        clauses.append("EXISTS (SELECT 1 FROM json_each(document_json, '$.scope.point_ids') WHERE value = ?)")
        values.append(point_id)
    if status:
        clauses.append("json_extract(document_json, '$.status') = ?")
        values.append(status)
    where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
    total = db.execute('SELECT COUNT(*) FROM response_plan_versions' + where, values).fetchone()[0]
    if before is not None:
        clauses.append('revision < ?')
        values.append(before)
    where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
    rows = db.execute('SELECT document_json FROM response_plan_versions' + where +
                      ' ORDER BY revision DESC LIMIT ?', [*values, limit + 1]).fetchall()
    records = []
    for row in rows[:limit]:
        d = json.loads(row[0])
        records.append({**{k: d[k] for k in ('id', 'revision', 'title', 'created_at', 'created_by', 'status', 'status_label', 'parent_id')},
                        'point_ids': d['scope']['point_ids']})
    has_more = len(rows) > limit
    return {'revision': current_revision(db), 'records': records, 'total': total,
            'has_more': has_more, 'next_before': records[-1]['revision'] if has_more else None}


def load_version(db, record_id):
    if not isinstance(record_id, str) or not re.fullmatch(r'[a-f0-9]{32}', record_id):
        raise ValidationError('预案版本编号无效。')
    row = db.execute('SELECT document_json FROM response_plan_versions WHERE id=?', (record_id,)).fetchone()
    if not row:
        raise ValidationError('预案版本不存在。')
    return json.loads(row[0])


def append_version(db, document, expected_revision):
    revision = current_revision(db)
    if type(expected_revision) is not int or expected_revision != revision:
        raise PlanConflict('预案版本已更新，请刷新版本列表后重新保存或审阅。')
    result = copy.deepcopy(document)
    result.update(id=uuid.uuid4().hex, revision=revision + 1)
    db.execute('INSERT INTO response_plan_versions VALUES (?,?,?,?)',
               (result['id'], result['revision'], result['created_at'], json.dumps(result, ensure_ascii=False)))
    return result


def review_version(db, payload, actor, dataset, monitor, now):
    if set(payload) != {'id', 'expected_revision', 'review_note'}:
        raise ValidationError('审阅需要版本编号、当前版本号及审阅意见。')
    note = payload.get('review_note')
    if not isinstance(note, str) or not 1 <= len(note.strip()) <= 4000:
        raise ValidationError('请填写 1—4000 字的内部审阅意见。')
    original = load_version(db, payload.get('id'))
    result = with_update_status(original, dataset, monitor, now)
    result.update(parent_id=original['id'], created_at=iso(now), created_by=actor, status='reviewed_draft',
                  status_label='已内部审阅的草案 · 尚非正式发布',
                  review={'by': actor, 'at': iso(now), 'note': note.strip()})
    return append_version(db, result, payload.get('expected_revision'))


def with_update_status(document, dataset, monitor, now):
    result = copy.deepcopy(document)
    messages = ['这是保存时的历史快照，不代表当前现场状态。执行前必须重新获取监测并核验。']
    changed = digest(dataset) != document.get('input_fingerprint')
    if changed:
        messages.append('场地、点位或预案资料已变更，请重新编制并保存新版本。')
    ids = document['scope']['point_ids']
    available = {p['id'] for p in dataset.get('points', [])}
    if not set(ids) <= available:
        changed = True
        messages.append('已保存版本中的部分点位现已不存在。')
    current = safe_observations(dataset, ids, monitor, now)
    previous = copy.deepcopy(document['observations'])
    previous.pop('captured_at', None)
    compare = copy.deepcopy(current)
    compare.pop('captured_at', None)
    if previous != compare:
        changed = True
        messages.append('监测时间、状态或来源有效性已变化，请重新预览获取当前证据。')
    if any(epoch(s.get('valid_until')) is not None and now >= epoch(s['valid_until']) for s in document.get('sources', [])):
        changed = True
        messages.append('此版本引用的部分预案条目现已过期。')
    result['update_status'] = {'checked_at': iso(now), 'needs_refresh': changed, 'messages': messages}
    return result


def export_html(document):
    e = lambda v: html.escape(str(v if v is not None else '待补充'), quote=True)
    def paragraphs(values):
        return '<ul>' + ''.join('<li>' + e(v) + '</li>' for v in values) + '</ul>'
    d, scope = document, document['scope']
    blocks = ['<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">',
        '<meta name="referrer" content="no-referrer"><title>' + e(d['title']) + '</title>',
        '<style>body{font:16px/1.7 sans-serif;max-width:960px;margin:40px auto;padding:24px;color:#172b2a}h1,h2{line-height:1.3}section{margin:28px 0}p,li{white-space:pre-wrap;overflow-wrap:anywhere}table{width:100%;border-collapse:collapse}td,th{padding:10px;border:1px solid #b7c9c6;text-align:left;vertical-align:top}.notice{padding:16px;background:#fff3d8;border:2px solid #926013}@media print{body{margin:0}section{break-inside:avoid}}</style></head><body>',
        '<h1>' + e(d['title']) + '</h1><p class="notice">应急预案草案 · ' + e(d['status_label']) + '。不是正式发布文件，任务均待人工核验。</p>',
        '<p>版本：' + e(d.get('revision')) + ' ｜ 生成时间：' + e(d['created_at']) + ' ｜ 编制人：' + e(d['created_by']) + '</p>',
        '<section><h2>一、适用范围</h2><p>' + e(scope['site_name']) + '\n' + e(scope['region']) + ' / ' + e(scope['campus']) + '\n' + e(scope['description']) + '</p>',
        '<p>点位编号：' + e('、'.join(scope['point_ids'])) + '</p></section>',
        '<section><h2>二、保存时观测与更新提示</h2>' + paragraphs(d['update_status']['messages']),
        '<p>观测整理时间：' + e(d['observations']['captured_at']) + '\n天气参考：' + e(d['observations']['weather']['label']) + '；来源时间：' + e(d['observations']['weather']['observed_at']) + '</p>']
    for p in d['observations']['points']:
        blocks.append('<h3>' + e(p['point_id']) + ' · ' + e(p['name']) + '</h3><p>' + e(p['label']) + '；观测时间：' + e(p['observed_at']) + '</p>' + paragraphs(p['basis']))
        blocks.append(paragraphs([s['id'] + ' · ' + s['name'] + ' · ' + s['status'] + ' · ' + str(s['observed_at'] or '无有效时间') for s in p['sources']]))
    blocks.append('</section><section><h2>三、管理人员补充资料</h2>')
    for key, label in DETAIL_LABELS.items():
        blocks.append('<h3>' + e(label) + '</h3><p>' + e(d['details'][key] or '待补充，系统不作推断。') + '</p>')
    blocks.append('</section><section><h2>四、待核验任务</h2>')
    for t in d['tasks']:
        blocks.append('<h3>' + e(t['point_name']) + ' · ' + e(t['title']) + '</h3><p>条目编号：' + e(t['plan_id']) + '；责任岗位：' + e(t['role']) + '；来源审核状态：' + e(t['source_status']) + '</p><p>' + e(t['action']) + '</p>' + paragraphs(t['basis']))
    if not d['tasks']:
        blocks.append('<p>没有匹配任务，不代表场地安全；需管理方补充适用条目。</p>')
    blocks.append('</section><section><h2>五、来源条目</h2>')
    blocks.append(paragraphs([s['id'] + ' · ' + s['title'] + ' · ' + s['source'] + '；审核状态：' + s['status'] + '；有效期：' + str(s['valid_until'] or '未注明') for s in d['sources']]))
    blocks.append('</section><section><h2>六、待补充与待核验</h2>' + paragraphs(d['missing_items'] or ['仍需管理方核验实际适用性。']) + '</section>')
    if d.get('review'):
        blocks.append('<section><h2>七、内部审阅记录</h2><p>' + e(d['review']['by']) + ' · ' + e(d['review']['at']) + '\n' + e(d['review']['note']) + '</p></section>')
    blocks.append('<section><h2>使用限制</h2>' + paragraphs(d['limitations']) + '</section></body></html>')
    return ''.join(blocks).encode('utf-8')
