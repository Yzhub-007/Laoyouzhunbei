"""Offline field-evaluation of water detection results; never opens images or APIs."""
import argparse
import json
import math
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


MAX_BYTES = 4 * 1024 * 1024
MAX_RECORDS = 20000
IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z')
RATE_THRESHOLDS = {
    'min_water_recall': ('water_detection_recall', 'min'),
    'min_water_precision': ('water_precision', 'min'),
    'max_clear_false_positive_rate': ('clear_false_positive_rate', 'max'),
    'max_usable_unknown_rate': ('usable_unknown_rate', 'max'),
    'max_unusable_definite_rate': ('unusable_definite_rate', 'max'),
}
COUNT_THRESHOLDS = {
    'min_test_events': ('event_count', 2),
    'min_water_samples': ('water_samples', 1),
    'min_clear_samples': ('clear_samples', 1),
    'min_unusable_samples': ('unusable_samples', 1),
}


class AcceptanceError(ValueError):
    """Invalid or leaking evaluation data. Messages never include source values."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AcceptanceError('JSON 包含重复字段。')
        result[key] = value
    return result


def _reject_constant(_value):
    raise AcceptanceError('JSON 不允许 NaN 或 Infinity。')


def load_document(path):
    """Bound memory consumption, reject ambiguous JSON and non-finite numbers."""
    try:
        with Path(path).open('rb') as stream:
            content = stream.read(MAX_BYTES + 1)
        if len(content) > MAX_BYTES:
            raise AcceptanceError('输入文件超过 4 MiB 限制。')
        document = json.loads(content.decode('utf-8-sig'),
                              object_pairs_hook=_unique_object,
                              parse_constant=_reject_constant)
    except AcceptanceError:
        raise
    except (OSError, UnicodeError, ValueError, RecursionError) as error:
        raise AcceptanceError('无法读取有效的 UTF-8 JSON 文件。') from error
    if not isinstance(document, dict):
        raise AcceptanceError('JSON 顶层必须是对象。')
    return document


def _number(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def validate_thresholds(thresholds):
    if thresholds is None:
        return None
    expected = set(RATE_THRESHOLDS) | set(COUNT_THRESHOLDS)
    if not isinstance(thresholds, dict) or set(thresholds) != expected:
        raise AcceptanceError('验收阈值必须完整包含文档规定的 9 个字段。')
    for key in RATE_THRESHOLDS:
        if not _number(thresholds[key]) or not 0 <= thresholds[key] <= 1:
            raise AcceptanceError('比例阈值必须是 0 到 1 的有限数值，不能使用布尔值。')
    for key, (_metric, minimum) in COUNT_THRESHOLDS.items():
        if type(thresholds[key]) is not int or not minimum <= thresholds[key] <= MAX_RECORDS:
            raise AcceptanceError('样本阈值必须是规定范围内的整数；独立事件至少 2 个。')
    return dict(thresholds)


def validate_document(document):
    if not isinstance(document, dict) or type(document.get('schema_version')) is not int or document['schema_version'] != 1:
        raise AcceptanceError('schema_version 必须为整数 1。')
    if document.get('dataset_kind') not in ('field', 'synthetic'):
        raise AcceptanceError('dataset_kind 必须为 field 或 synthetic。')
    if type(document.get('independent_events_confirmed')) is not bool:
        raise AcceptanceError('需要用布尔值声明 independent_events_confirmed。')
    rows = document.get('records')
    if not isinstance(rows, list) or len(rows) > MAX_RECORDS:
        raise AcceptanceError('records 必须是数组，最多 20000 条。')
    frames, event_splits = set(), {}
    normalized = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise AcceptanceError(f'第 {index} 条记录必须是对象。')
        for key in ('point_id', 'event_id', 'frame_id'):
            value = row.get(key)
            if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
                raise AcceptanceError(f'第 {index} 条记录的标识符格式无效；请使用匿名短编号。')
        if row.get('split') not in ('train', 'validation', 'test'):
            raise AcceptanceError(f'第 {index} 条记录缺少有效 split。')
        if row.get('label') not in ('water', 'clear', 'unusable'):
            raise AcceptanceError(f'第 {index} 条记录缺少有效人工标注。')
        if row.get('prediction') not in ('water', 'clear', 'unknown'):
            raise AcceptanceError(f'第 {index} 条记录缺少有效模型预测。')
        if 'confidence' in row and (not _number(row['confidence']) or not 0 <= row['confidence'] <= 1):
            raise AcceptanceError(f'第 {index} 条记录的 confidence 必须是 0 到 1 的有限数值。')
        if row['frame_id'] in frames:
            raise AcceptanceError(f'第 {index} 条记录出现重复 frame_id；禁止重复计数或跨 split 泄露。')
        frames.add(row['frame_id'])
        previous_split = event_splits.setdefault(row['event_id'], row['split'])
        if previous_split != row['split']:
            raise AcceptanceError(f'第 {index} 条记录发生事件跨 split 泄露；同一事件必须保持同一 split。')
        # Discard paths, personal data, model explanations, and confidence before reporting.
        normalized.append({key: row[key] for key in ('point_id', 'event_id', 'frame_id', 'split', 'label', 'prediction')})
    return normalized


def _rate(numerator, denominator):
    return numerator / denominator if denominator else None


def _metrics(rows):
    counts = defaultdict(int)
    events = defaultdict(list)
    for row in rows:
        counts[(row['label'], row['prediction'])] += 1
        events[row['event_id']].append(row)
    water = sum(counts[('water', result)] for result in ('water', 'clear', 'unknown'))
    clear = sum(counts[('clear', result)] for result in ('water', 'clear', 'unknown'))
    unusable = len(rows) - water - clear
    true_positive = counts[('water', 'water')]
    false_positive = counts[('clear', 'water')]
    unknown = sum(counts[(label, 'unknown')] for label in ('water', 'clear', 'unusable'))
    water_unknown = counts[('water', 'unknown')]
    unusable_definite = counts[('unusable', 'water')] + counts[('unusable', 'clear')]
    water_events = [event for event in events.values() if any(r['label'] == 'water' for r in event)]
    detected_events = sum(any(r['label'] == 'water' and r['prediction'] == 'water' for r in event) for event in water_events)
    return {
        'sample_count': len(rows), 'event_count': len(events),
        'water_samples': water, 'clear_samples': clear, 'unusable_samples': unusable,
        'true_water_detections': true_positive, 'false_positive_count': false_positive,
        'explicit_water_misses': counts[('water', 'clear')],
        'unknown_water_undetected': water_unknown,
        'total_water_undetected': water - true_positive,
        'correct_clear_count': counts[('clear', 'clear')],
        'unknown_clear_count': counts[('clear', 'unknown')],
        'unknown_count': unknown,
        'unusable_definite_count': unusable_definite,
        'unusable_predicted_water_count': counts[('unusable', 'water')],
        'unusable_predicted_clear_count': counts[('unusable', 'clear')],
        'water_detection_recall': _rate(true_positive, water),
        'water_precision': _rate(true_positive, true_positive + false_positive),
        'clear_false_positive_rate': _rate(false_positive, clear),
        'unknown_rate': _rate(unknown, len(rows)),
        'usable_unknown_rate': _rate(water_unknown + counts[('clear', 'unknown')], water + clear),
        'unusable_definite_rate': _rate(unusable_definite, unusable),
        'water_event_count': len(water_events),
        'detected_water_event_count': detected_events,
        'undetected_water_event_count': len(water_events) - detected_events,
        'water_event_detection_recall': _rate(detected_events, len(water_events)),
        'false_alarm_event_count': sum(any(r['label'] == 'clear' and r['prediction'] == 'water' for r in event) for event in events.values()),
    }


def _assessment(metrics, thresholds, dataset_kind, independence):
    reasons, checks = [], []
    if dataset_kind == 'synthetic':
        reasons.append('synthetic_data_is_not_field_acceptance')
    if thresholds is None:
        reasons.append('thresholds_not_configured')
    if not independence:
        reasons.append('independent_events_not_confirmed')
    minimums = thresholds or {key: minimum for key, (_metric, minimum) in COUNT_THRESHOLDS.items()}
    for key, (metric, _minimum) in COUNT_THRESHOLDS.items():
        if metrics[metric] < minimums[key]:
            reasons.append(key + '_not_met')
    if thresholds is not None:
        for key, (metric, direction) in RATE_THRESHOLDS.items():
            value = metrics[metric]
            passed = None if value is None else (value >= thresholds[key] if direction == 'min' else value <= thresholds[key])
            checks.append({'criterion': key, 'value': value, 'threshold': thresholds[key], 'passed': passed})
            if value is None:
                reasons.append(metric + '_undefined')
    if dataset_kind == 'synthetic' or thresholds is None:
        status = 'not_evaluated'
    elif reasons:
        status = 'insufficient'
    elif any(check['passed'] is False for check in checks):
        status = 'failed'
    else:
        status = 'passed_configured_thresholds'
    return {'status': status, 'reasons': reasons, 'checks': checks}


def evaluate(document, thresholds=None):
    """Evaluate only the held-out test split; confidence is never accuracy."""
    rows = validate_document(document)
    thresholds = validate_thresholds(thresholds)
    test_rows = [row for row in rows if row['split'] == 'test']
    point_rows, event_rows = defaultdict(list), defaultdict(list)
    for row in test_rows:
        point_rows[row['point_id']].append(row)
        event_rows[row['event_id']].append(row)
    kind, independent = document['dataset_kind'], document['independent_events_confirmed']
    overall = _metrics(test_rows)
    assessment = _assessment(overall, thresholds, kind, independent)
    points = []
    for index, values in enumerate(point_rows.values(), 1):
        metrics = _metrics(values)
        points.append({'point_ref': f'point_{index:03d}', 'metrics': metrics,
                       **_assessment(metrics, thresholds, kind, independent)})
    # A large, easy point must not conceal an unvalidated or failing smaller point.
    if assessment['status'] == 'passed_configured_thresholds':
        statuses = {point['status'] for point in points}
        if 'failed' in statuses:
            assessment['status'] = 'failed'
            assessment['reasons'].append('one_or_more_points_failed')
        elif 'insufficient' in statuses:
            assessment['status'] = 'insufficient'
            assessment['reasons'].append('one_or_more_points_insufficient')
    return {
        'schema_version': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
        'dataset_kind': kind, 'independent_events_confirmed': independent,
        **assessment,
        'thresholds': thresholds,
        'split_counts': {split: sum(row['split'] == split for row in rows) for split in ('train', 'validation', 'test')},
        'overall': overall, 'points': points,
        'events': [{'event_ref': f'event_{index:03d}', 'point_count': len({row['point_id'] for row in values}),
                    'metrics': _metrics(values)} for index, values in enumerate(event_rows.values(), 1)],
        'limitations': [
            '仅统计人工提供的标注与预测，不连接摄像头、不运行模型、不读取或保存图片。',
            '只计算 test 集；事件独立性依赖采集人员确认，编号检查无法发现改名或近重复图片。',
            'unknown 计入水样本未检出，不代表无积水；confidence 不等于准确率。',
            'unusable 无法确定水体真值，不进入水体 precision/recall，另行检查其肯定判定。',
            '事件检出表示该事件至少一张有水图片被检出，不代表及时告警或逐帧可靠。',
            '通过自定阈值仅适用于这批测试，不能代替管理方审核或正式安全认证。',
            '报告匿名引用按 test 数据首次出现顺序编号，不复制原始标识符、路径或自由文字。',
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description='离线评估积水识别结果；不发送数据、不调用模型。')
    parser.add_argument('--input', type=Path, required=True, help='人工标注与模型预测 JSON')
    parser.add_argument('--thresholds', type=Path, help='管理方自行确认的完整阈值 JSON；省略时仅统计')
    parser.add_argument('--output', type=Path, required=True, help='新报告文件，不覆盖已有文件')
    args = parser.parse_args(argv)
    try:
        report = evaluate(load_document(args.input), load_document(args.thresholds) if args.thresholds else None)
        # Do not silently create nested directories or overwrite previous failed reports.
        with args.output.open('x', encoding='utf-8') as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
    except AcceptanceError as error:
        parser.exit(2, f'数据校验失败：{error}\n')
    except OSError:
        parser.exit(2, '无法创建报告：请使用已有目录中的新文件名，并检查目录权限。\n')
    print(f"status={report['status']}; test_samples={report['overall']['sample_count']}; test_events={report['overall']['event_count']}")
    return 0 if report['status'] == 'passed_configured_thresholds' else 1


if __name__ == '__main__':
    raise SystemExit(main())
