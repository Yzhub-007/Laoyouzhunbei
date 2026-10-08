"""Local human review workflow. All notices are exercise drafts, never published."""
from copy import deepcopy
from datetime import datetime, timezone
from engine import ValidationError, required_text

STATES = {'pending': '待人工核验', 'confirmed': '已确认', 'processing': '处理中',
          'completed': '已完成', 'rejected': '已排除'}
TRANSITIONS = {'pending': {'pending', 'confirmed', 'rejected'},
               'confirmed': {'confirmed', 'pending', 'processing', 'rejected'},
               'processing': {'processing', 'pending', 'completed'},
               'completed': {'completed', 'pending'}, 'rejected': {'rejected', 'pending'}}


def initialize(result):
    result.setdefault('revision', 0)
    result.setdefault('audit', [])
    result.setdefault('notices', [])
    for index, card in enumerate(result['cards']):
        card.setdefault('card_id', f'task-{index + 1}')
        card.setdefault('workflow_state', 'pending')
        card.setdefault('original_action', card['action'])
        card.setdefault('original_role', card['role'])
        card.setdefault('field_status', 'unknown')
    return result


def mutate(result, operation, payload):
    result = initialize(deepcopy(result))
    if type(payload.get('revision')) is not int or payload['revision'] != result['revision']:
        raise ValidationError('记录已更新，请重新打开历史记录后再保存，避免覆盖他人的修改。')
    actor = required_text(payload, 'actor', 100)
    now = datetime.now(timezone.utc).isoformat()
    card = next((c for c in result['cards'] if c['card_id'] == payload.get('card_id')), None)
    if operation in ('review', 'notice') and card is None:
        raise ValidationError('任务不存在。')
    if operation == 'review':
        state = payload.get('state')
        if not isinstance(state, str) or state not in TRANSITIONS[card['workflow_state']]:
            raise ValidationError('不允许此状态流转；需先确认任务，修改已确认内容请退回待核验。')
        action, role = required_text(payload, 'action', 2000), required_text(payload, 'role', 200)
        field = payload.get('field_status')
        if field not in ('unknown', 'closed', 'restricted'):
            raise ValidationError('现场状态不受支持。')
        note = required_text(payload, 'note', 2000)
        changed = action != card['action'] or role != card['role'] or field != card['field_status']
        if changed and card['workflow_state'] != 'pending' and state != 'pending':
            raise ValidationError('修改已确认的内容前，请先退回待核验。')
        before = deepcopy(card)
        card.update(action=action, role=role, workflow_state=state, status=STATES[state],
                    field_status=field, reviewed_by=actor, reviewed_at=now, review_note=note)
        for notice in result['notices']:
            if notice['card_id'] == card['card_id']:
                notice['revoked'] = True
        detail = {'before': before, 'after': deepcopy(card), 'note': note}
    elif operation == 'notice':
        if card['workflow_state'] not in ('confirmed', 'processing', 'completed'):
            raise ValidationError('只有人工确认后的任务才能生成公众提示草稿。')
        if card['field_status'] == 'unknown':
            raise ValidationError('现场状态尚未确认，请在审核中明确关闭或限制使用的依据。')
        if any(n['card_id'] == card['card_id'] and not n.get('revoked') for n in result['notices']):
            raise ValidationError('该任务已有有效草稿，请查看公众提示。')
        phrase = {'closed': '暂时关闭，请按现场指引使用其他开放入口。',
                  'restricted': '限制使用，请遵从现场管理人员指引。'}[card['field_status']]
        notice = {'id': f"notice-{result['revision'] + 1}", 'card_id': card['card_id'],
                  'text': f"【演练草稿】{card['point_name']}{phrase}", 'created_at': now,
                  'actor': actor, 'confirmed_by': card['reviewed_by'],
                  'confirmation_note': card['review_note'], 'confirmed_at': card['reviewed_at'],
                  'source': f"{card['point_id']} / {card['plan_id']} · {card['plan_source']}",
                  'revoked': False, 'published': False, 'feedback': []}
        result['notices'].append(notice)
        detail = {'notice_id': notice['id']}
    elif operation == 'feedback':
        notice = next((n for n in result['notices'] if n['id'] == payload.get('notice_id')), None)
        if notice is None or notice.get('revoked'):
            raise ValidationError('提示不存在或已失效，请先核对最新任务。')
        status = payload.get('status')
        if status not in ('received', 'question', 'handled'):
            raise ValidationError('反馈类型不受支持。')
        entry = {'actor': actor, 'status': status, 'note': required_text(payload, 'note', 2000), 'at': now}
        notice['feedback'].append(entry)
        detail = {'notice_id': notice['id'], **entry}
    else:
        raise ValidationError('不支持的工作流操作。')
    result['revision'] += 1
    result['audit'].append({'operation': operation, 'actor': actor, 'at': now,
                            'card_id': payload.get('card_id'), 'revision': result['revision'], 'detail': detail})
    result['publishable'] = False
    return result
