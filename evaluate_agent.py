"""Run labelled cases independently of the live workflow. No AI calls by default."""
import argparse
import copy
import json
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from campus_agent import fingerprint, run_agent
from engine import ValidationError

ROOT = Path(__file__).resolve().parent


def evaluate(cases, mode='rules', config=None):
    rows = []
    for case in cases:
        data = copy.deepcopy(case['input'])
        started = time.perf_counter()
        row = {'case_id': case['id'], 'input_fingerprint': fingerprint(data), 'mode': mode,
               'expected_error': case.get('expected_error', False), 'gold_author': case.get('gold_author', 'unspecified')}
        try:
            result = run_agent(data, mode, config, True)
            actual = {(c['point_id'], c['plan_id']) for c in result['cards']}
            expected = {tuple(p) for p in case.get('expected_pairs', [])}
            row.update(actual_pairs=sorted(actual), omissions=sorted(expected-actual), extra_pairs=sorted(actual-expected),
                       passed=not row['expected_error'] and actual == expected, output=result)
        except ValidationError as error:
            row.update(error=str(error), passed=bool(row['expected_error']))
        row['elapsed_ms'] = round((time.perf_counter()-started)*1000, 2)
        rows.append(row)
    return {'created_at': datetime.now(timezone.utc).isoformat(), 'mode': mode, 'cases': rows,
            'count': len(rows), 'passed': sum(r['passed'] for r in rows),
            'median_elapsed_ms': statistics.median(r['elapsed_ms'] for r in rows) if rows else None,
            'limitation': '匹配集合/拒绝行为测试；参考答案由团队构造，未经管理方审核。不等于用户效果或灾害预测准确率。'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--cases', type=Path, default=ROOT/'data/evaluation/development.json')
    parser.add_argument('--mode', choices=['rules', 'ai'], default='rules')
    parser.add_argument('--allow-external', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('输出文件已存在，请使用新文件名以保留此前失败结果。')
    if args.mode == 'ai' and not args.allow_external:
        parser.error('AI批量调用需要显式--allow-external，先核实资料授权与费用。')
    from server import read_config
    report = evaluate(json.loads(args.cases.read_text(encoding='utf-8-sig'))['cases'], args.mode, read_config())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"{report['passed']}/{report['count']} passed; mode={args.mode}; {args.output}")
    return 0 if report['passed'] == report['count'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
