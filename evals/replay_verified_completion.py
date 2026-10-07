#!/usr/bin/env python3
"""Offline regression replay of published replies through the new completion gate.

This is a fixed-response replay, not a model experiment. The recorded model never
saw this gate's feedback, and no follow-up replies or extra rounds are invented.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import tempfile

from multistep_fixture import create_fixture, grade
from verified_multistep import make_session

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def linked_file(parent, name):
    path = (parent / name).resolve()
    if not path.is_relative_to(parent.resolve()) or not path.is_file():
        raise ValueError('Report link must name a file inside the report directory')
    return path


def replay(overview):
    overview = Path(overview).resolve()
    catalog = read(overview)
    sources, rows, seen = [], [], set()
    for cohort in catalog['cohorts']:
        report_path = linked_file(overview.parent, cohort['report'])
        if sha(report_path) != cohort['sha256']:
            raise ValueError('Linked report hash changed')
        report = read(report_path)
        trace_path = report_path.with_suffix('.traces.jsonl')
        if sha(trace_path) != report['trace_sha256']:
            raise ValueError('Published trace hash changed')
        sources.append({'report': report_path.name, 'report_sha256': sha(report_path),
                        'trace': trace_path.name, 'trace_sha256': sha(trace_path)})
        finals = {(r['case_id'], r['arm']): r for r in report['summary']['case_results']}
        covered = set()
        for text in trace_path.read_text(encoding='utf-8').splitlines():
            trace = json.loads(text)
            if trace['result'] is None:
                continue
            case, original = trace['case'], trace['input']
            identity = original['case_id'], original['arm']
            if identity in seen or identity not in finals or finals[identity] != trace['result']:
                raise ValueError('Duplicate or inconsistent historical final result')
            seen.add(identity)
            covered.add(identity)
            with tempfile.TemporaryDirectory() as directory:
                project = Path(directory) / 'project'
                files = create_fixture(project, case)
                initial = {p.relative_to(project).as_posix(): sha(p)
                           for p in project.rglob('*') if p.is_file()}
                if files != original['files'] or initial != original['initial_file_sha256']:
                    raise ValueError('Public fixture differs from the historical initial state')
                session = make_session(project, case['domain'], files, original['current_sources'])
                steps = []
                responses = trace['responses']
                if len(responses) != trace['result']['rounds']:
                    raise ValueError('Historical round count differs from recorded responses')
                for index, item in enumerate(responses):
                    if item['round'] != index or session.status != 'active':
                        raise ValueError('Historical replies continue after a terminal gate state')
                    answer = item['response']
                    steps.append(session.step(answer['content'], answer['finish_reason']))
                last = steps[-1]
                rows.append({'case_id': identity[0], 'arm': identity[1], 'cohort': report_path.stem,
                    'historical_success': trace['result']['task_success'],
                    'historical_rounds': trace['result']['rounds'],
                    'gate_status': session.status, 'gate_done_accepted': last['done_accepted'],
                    'public_verification': last['completion'], 'rounds_remaining': last['rounds_remaining'],
                    'done_rejected_rounds': [i + 1 for i, step in enumerate(steps)
                                            if step['done_requested'] and not step['done_accepted']],
                    'artifact_only_success': grade(project, case)['task_success'],
                    'stop_reason': 'historical_trace_ended' if session.status == 'active' else session.status})
        if covered != set(finals):
            raise ValueError('Missing historical final trace')
    if len(rows) != catalog['completed_unique_tasks']:
        raise ValueError('Historical coverage count changed')
    return {'schema_version': 1, 'kind': 'offline_fixed_response_regression', 'model_api_calls': 0,
            'overview_sha256': sha(overview), 'sources': sources,
            'implementation_sha256': {str(p.relative_to(ROOT).as_posix()): sha(p) for p in (
                ROOT / 'examples/verified_completion.py', Path(__file__),
                ROOT / 'evals/verified_multistep.py', ROOT / 'evals/multistep_fixture.py')},
            'tasks': len(rows), 'historical_strict_success': sum(r['historical_success'] for r in rows),
            'gate_done_accepted': sum(r['gate_done_accepted'] for r in rows),
            'historical_failures_accepted': sum(not r['historical_success'] and r['gate_done_accepted'] for r in rows),
            'tasks_with_rejected_done': sum(bool(r['done_rejected_rounds']) for r in rows),
            'budget_exhausted': sum(r['gate_status'] == 'budget_exhausted' for r in rows),
            'verified_without_accepted_done': sum(r['public_verification']['verified']
                                                 and not r['gate_done_accepted'] for r in rows),
            'incomplete_when_historical_trace_ended': sum(r['gate_status'] == 'active' for r in rows),
            'case_results': rows,
            'limits': ['Recorded replies did not observe new feedback; no new model success-rate claim.',
                       'No automatic verification, repair, invented reply, or extra round.',
                       'Public verification completion does not establish business correctness.',
                       'Original v2 protocol, scores, reports and traces remain unchanged.']}


def publish(overview, output):
    output = Path(output).resolve()
    if output.exists():
        raise ValueError('Use a new output directory; previous results are retained')
    result = replay(overview)
    output.mkdir(parents=True, exist_ok=False)
    (output / 'report.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    text = f'''# 完成验证约束：历史回复离线回放

这是新执行约束的固定回复回归检查，**没有调用模型 API**。原回复没有看到新反馈；结果不代表新模型成功率，也不修改 v2 的 51/60 严格通过结果。

- 回放 {result['tasks']} 个历史最终结果；原严格通过 {result['historical_strict_success']} 项。
- 新约束接受完成声明 {result['gate_done_accepted']} 项；接受原失败任务 {result['historical_failures_accepted']} 项。
- {result['tasks_with_rejected_done']} 个任务的完成声明被拒绝。
- {result['budget_exhausted']} 项达到轮数上限仍未完成；{result['incomplete_when_historical_trace_ended']} 项在原回复结束时仍未完成且有剩余轮数。

其中 {result['verified_without_accepted_done']} 项已经通过公开验证，却没有在预算内提出可接受的完成请求。v2 的事后判分允许这类任务通过；新的交互会话要求显式完成请求，因此“接受完成数”与原严格成功数是不同指标。不能据此声称模型效果变好或变差，也不能为增加完成数悄悄补一轮。

没有自动补 run、修改文件或补造后续回答。验证成功仅说明应用绑定的公开检查通过且文件快照未变；业务正确性仍由应用规则或独立验收判断。回归测试另覆盖拒绝后在剩余预算内执行 run 并重新完成的脚本化恢复路径，不将其算作模型效果。

[逐例记录与来源哈希](report.json)
'''
    (output / 'report.md').write_text(text, encoding='utf-8')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--overview', type=Path, default=ROOT / 'evals/reports/2026-10-06-multistep-memory-overview.json')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = publish(args.overview, args.output)
    print(json.dumps({key: result[key] for key in ('tasks', 'gate_done_accepted', 'historical_failures_accepted',
                                                  'tasks_with_rejected_done', 'model_api_calls')}))


if __name__ == '__main__':
    main()
