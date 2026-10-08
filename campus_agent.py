"""A bounded campus action agent: inspect, retrieve, select, validate, hand off."""
import hashlib
import json
from datetime import datetime, timezone
from engine import ValidationError, generate, timestamp, validate_dataset


def fingerprint(data):
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def verified(row):
    if any(marker in str(row.get(key, '')) for key in ('name', 'source', 'role') for marker in ('待填', '采集槽位', '待确认', '尚非浙江大学正式')):
        return False
    if row.get('status') != 'verified' or not str(row.get('verified_by', '')).strip():
        return False
    try:
        return timestamp(row.get('verified_at')) <= datetime.now(timezone.utc)
    except ValidationError:
        return False


def readiness(data):
    validate_dataset(data)
    site = data['site']
    authorization = site.get('authorization', {})
    if not isinstance(authorization, dict):
        authorization = {}
    authorized = authorization.get('status') == 'confirmed' and all(
        isinstance(authorization.get(k), str) and authorization[k].strip() for k in ('approver_role', 'reference', 'confirmed_at'))
    try:
        authorized = bool(authorized and timestamp(authorization.get('confirmed_at')) <= datetime.now(timezone.utc))
    except ValidationError:
        authorized = False
    checks = [
        ('authorization', '管理方授权记录', authorized, '记录授权角色、范围凭据与确认时间'),
        ('scope', '楼宇与边界确认', site.get('status') == 'verified' and bool(site.get('scope', '').strip()), '将楼宇名称、边界及场地状态核实为已确认'),
        ('points', '5—8个点位已核验', 5 <= len(data['points']) <= 8 and all(verified(p) for p in data['points']), '逐项填写真实名称、来源、核验人角色与时间'),
        ('plans', '工作条目经管理方审核', all(verified(r) for r in data['plans']), '以场地现行预案或获准清单替换团队草稿，并记录审核'),
        ('campus', '校区和地区一致', bool(site.get('campus')) and all(p.get('campus') == site.get('campus') and p['region'] == site['region'] for p in data['points']), '首期限定同一校区及城市，不跨校区混用资料')]
    rows = [{'id': i, 'label': label, 'passed': bool(ok), 'next_action': action} for i, label, ok, action in checks]
    return {'checks': rows, 'passed': sum(r['passed'] for r in rows), 'total': len(rows),
            'shadow_ready': all(r['passed'] for r in rows), 'note': '依据用户录入的凭据校验完整性，不等于第三方认证或学校批准。'}


def run_agent(data, mode='rules', config=None, scope_confirmed=False, operation_mode='tabletop'):
    if operation_mode not in ('tabletop', 'shadow'):
        raise ValidationError('仅支持桌面演练或影子运行。')
    report = readiness(data)
    if operation_mode == 'shadow':
        if not report['shadow_ready']:
            raise ValidationError('影子运行尚未就绪：' + '；'.join(r['label'] for r in report['checks'] if not r['passed']))
        if data['weather'].get('mode') != 'live_reference':
            raise ValidationError('影子运行须载入本机新近气象快照；模拟和历史材料请使用桌面演练。')
    result = generate(data, mode, config, scope_confirmed)
    result.update(input_fingerprint=fingerprint(data), operation_mode=operation_mode, readiness=report)
    result['agent'].update(version='0.4-zju', name='紫金港行动编排Agent', contract='campus-selection-v1',
                           calls=1 if mode == 'ai' else 0, tools=['validate_dataset', 'readiness', 'candidate_pairs', 'select_with_model' if mode == 'ai' else 'rule_selector', 'reference_validator'],
                           next_action='管理人员逐项核验任务；未知现场状态不得生成关闭、开放或安全通行结论')
    result['agent']['steps'].insert(0, {'title': '检查试点准入', 'detail': f"{report['passed']}/{report['total']}项资料就绪；{'影子运行' if operation_mode == 'shadow' else '桌面演练'}"})
    if not report['shadow_ready']:
        result['warnings'].insert(0, '当前仅可桌面演练；管理授权、真实点位或条目审核仍有缺口。')
    for card in result['cards']:
        card['evidence_refs'] = [f"weather:{data['weather'].get('id', 'input')}", f"point:{card['point_id']}", f"plan:{card['plan_id']}"]
    return result
