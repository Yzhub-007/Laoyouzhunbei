"""Timestamped local experiments; human-scored outcomes are never inferred as truth."""
import json
import re
import uuid
from datetime import datetime, timezone
from engine import ValidationError, required_text, validate_dataset
from campus_agent import fingerprint


def now():
    return datetime.now(timezone.utc).isoformat()


def get_trial(db, trial_id):
    if not isinstance(trial_id, str):
        raise ValidationError('试验编号无效。')
    row = db.execute('SELECT record_json FROM trials WHERE id=?', (trial_id,)).fetchone()
    if row is None:
        raise ValidationError('试验记录不存在。')
    return json.loads(row[0])


def save_trial(db, trial):
    db.execute('INSERT OR REPLACE INTO trials VALUES (?,?,?)', (trial['id'], trial['started_at'], json.dumps(trial, ensure_ascii=False)))


def start_trial(db, payload):
    alias = required_text(payload, 'participant', 40)
    if not re.fullmatch(r'[A-Za-z0-9_-]{2,40}', alias):
        raise ValidationError('参与者请使用匿名编号，例如 M01，不记录姓名或学号。')
    condition = payload.get('condition')
    if condition not in ('manual', 'rules', 'ai'):
        raise ValidationError('请选择原始资料、规则模板或AI辅助条件。')
    data = validate_dataset(payload.get('data'))
    trial = {'id': uuid.uuid4().hex[:12], 'participant': alias, 'condition': condition,
             'scenario_id': required_text(payload, 'scenario_id', 80), 'started_at': now(), 'status': 'active',
             'input_fingerprint': fingerprint(data), 'input_snapshot': data, 'run_id': None,
             'scoring': None, 'duration_seconds': None, 'note': '耗时为服务器记录的墙钟时间，包含阅读、生成和审核；无暂停扣时。'}
    save_trial(db, trial)
    return trial


def check_generation(db, trial_id, data, mode):
    trial = get_trial(db, trial_id)
    if trial['status'] != 'active' or trial['condition'] != mode or trial.get('run_id'):
        raise ValidationError('试验状态或生成方式不匹配；每次试验只能绑定一次成功生成。')
    if fingerprint(data) != trial['input_fingerprint']:
        raise ValidationError('试验开始后输入已变化，请结束本次试验并按新资料重新开始。')
    return trial


def finish_trial(db, payload):
    trial = get_trial(db, payload.get('trial_id'))
    if trial['status'] not in ('active', 'awaiting_score'):
        raise ValidationError('试验已结束，不允许覆盖原始记录。')
    outcome = payload.get('outcome')
    if outcome not in ('completed', 'aborted'):
        raise ValidationError('请选择完成或中止。')
    if outcome == 'completed' and trial['status'] != 'awaiting_score':
        raise ValidationError('请先停止任务计时，再填写评分并封存，评分耗时不计入任务耗时。')
    if outcome == 'completed' and trial['condition'] != 'manual' and not trial.get('run_id'):
        raise ValidationError('尚未绑定该条件的生成记录，不能标记完成；可选择中止并说明原因。')
    scoring = {}
    if outcome == 'completed':
        for key in ('critical_errors', 'omissions', 'unsupported', 'help_count'):
            value = payload.get(key)
            if type(value) is not int or not 0 <= value <= 1000:
                raise ValidationError('错误、遗漏、无依据建议和求助次数必须为0—1000整数。')
            scoring[key] = value
    trial.update(status=outcome, finished_at=now(), evaluator=required_text(payload, 'evaluator', 80),
                 notes=required_text(payload, 'notes', 3000), scoring=scoring or None)
    task_end = trial.get('task_finished_at', trial['finished_at'])
    trial['duration_seconds'] = max(0, round((datetime.fromisoformat(task_end) - datetime.fromisoformat(trial['started_at'])).total_seconds(), 2))
    trial['total_wall_seconds'] = max(0, round((datetime.fromisoformat(trial['finished_at']) - datetime.fromisoformat(trial['started_at'])).total_seconds(), 2))
    if trial.get('run_id') and 'result_at_finish' not in trial:
        row = db.execute('SELECT output_json FROM runs WHERE id=?', (trial['run_id'],)).fetchone()
        result = json.loads(row[0])
        trial['run_revision_at_finish'] = result.get('revision', 0)
        trial['result_at_finish'] = result  # Later edits must not change the evidence for a timed trial.
    save_trial(db, trial)
    return trial


def stop_trial(db, payload):
    trial = get_trial(db, payload.get('trial_id'))
    if trial['status'] != 'active':
        raise ValidationError('该试验已经停止计时。')
    if trial['condition'] != 'manual' and not trial.get('run_id'):
        raise ValidationError('尚未绑定生成结果；请执行对应条件或中止试验。')
    trial.update(status='awaiting_score', task_finished_at=now())
    trial['duration_seconds'] = max(0, round((datetime.fromisoformat(trial['task_finished_at'])-datetime.fromisoformat(trial['started_at'])).total_seconds(), 2))
    if trial.get('run_id'):
        result = json.loads(db.execute('SELECT output_json FROM runs WHERE id=?', (trial['run_id'],)).fetchone()[0])
        trial['result_at_finish'] = result
        trial['run_revision_at_finish'] = result.get('revision', 0)
        trial['generation_elapsed_ms'] = result.get('elapsed_ms')
        trial['task_edit_count'] = sum(e['operation'] == 'review' and any(e['detail']['before'][k] != e['detail']['after'][k] for k in ('action', 'role')) for e in result.get('audit', []))
    save_trial(db, trial)
    return trial
