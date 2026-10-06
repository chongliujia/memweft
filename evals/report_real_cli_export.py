#!/usr/bin/env python3
"""Read-only evidence audit and path-redacted report for the existing CLI task."""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re

import real_cli_export_task as task


def rows(path):
    return [task.strict_json_loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []


def _inside(root, name):
    """Saved paths are data; reject traversal and symlink-based redirection."""
    relative = PurePosixPath(name)
    if (not isinstance(name, str) or '\\' in name or relative.is_absolute()
            or PureWindowsPath(name).drive or '..' in relative.parts
            or any(':' in part or part.endswith((' ', '.')) for part in relative.parts)):
        raise ValueError('Unsafe evidence path')
    root = Path(root).resolve()
    target = root.joinpath(*relative.parts)
    if not target.resolve().is_relative_to(root):
        raise ValueError('Evidence path leaves the task directory')
    for part in [target, *target.parents]:
        if part == root:
            break
        if part.is_symlink():
            raise ValueError('Evidence path contains a symlink')
    return target


def _snapshot(output):
    """Audit saved trial code without executing it or requiring current transport."""
    freeze = task.load(output / 'freeze.json')
    manifest = task.load(output / 'source-manifest.json')
    if task.sha(output / 'source-manifest.json') != freeze['source_manifest_sha256'] or task.sha(output / 'initial.json') != freeze['initial_sha256']:
        raise ValueError('Frozen source manifest or initial messages changed')
    expected_sources = {'evals/real_cli_export_task.py', 'evals/output_contract.py',
                        'evals/multistep_kimi.py', 'examples/handoff_app/kimi_client.py'}
    if set(freeze['source_sha256']) != expected_sources:
        raise ValueError('Missing frozen source snapshots')
    for name, digest in freeze['source_sha256'].items():
        if task.sha(_inside(output, 'frozen-sources/' + name)) != digest:
            raise ValueError('Frozen source snapshot changed: ' + name)
    if not manifest['runner_sha256'] == freeze['runner_sha256'] == freeze['source_sha256']['evals/real_cli_export_task.py']:
        raise ValueError('Frozen runner identities disagree')
    limits = {'model': 'kimi-k2.6', 'max_rounds': 4, 'max_actions': 3, 'max_completion_tokens': 1024,
              'max_requests': 4, 'token_stop_threshold': 30000, 'retries': 0,
              'interval_seconds': 21, 'request_byte_cap': 64000,
              'pre_run_cooldown_seconds': 60, 'pre_run_cooldown_owner': 'caller'}
    if any(freeze.get(name) != value for name, value in limits.items()):
        raise ValueError('Unexpected frozen request limits')
    for entry in manifest['original_sources'].values():
        if task.sha(entry['path']) != entry['sha256']:
            raise ValueError('Original source changed')
    for name, digest in manifest['archive_sha256'].items():
        if task.sha(_inside(output, name)) != digest:
            raise ValueError('Archived input changed: ' + name)
    for name, digest in manifest['fixed_work_sha256'].items():
        if task.sha(_inside(output, 'work/' + name)) != digest:
            raise ValueError('Fixed working input changed: ' + name)
    for name, digest in manifest['installed_sdk_sha256'].items():
        if task.sha(name) != digest:
            raise ValueError('Pinned installed SDK changed')
    if task.sha(output / 'oracle.json') != manifest['oracle_sha256']:
        raise ValueError('Independent oracle changed')
    return manifest, freeze


def _grade(output, manifest):
    """Read actual artifacts and receipts; no current run-time freeze dependency."""
    errors = []
    expected = task.load(output / 'oracle.json')
    actual = task.load(_inside(output, 'work/handoff.json'))
    encode = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    if encode(actual) != encode(expected):
        errors.append('Export does not match all three independently recorded project decisions and provenance')
    plan = task._plan(task.load(_inside(output, 'work/export-plan.json')))
    scope = {'project': 'memweft-public', 'user': 'maintainer'}
    if plan['keys'] != [row['key'] for row in expected['records']] or {k: plan[k] for k in scope} != scope or plan['include_sources'] is not True:
        errors.append('Plan omitted current decisions/sources or selected the wrong scope')
    receipts = sorted((output / 'cli-runs').glob('*.json'))
    receipt = task.load(_inside(output, receipts[-1].relative_to(output).as_posix())) if receipts else {}
    if receipt.get('success') is not True or receipt.get('plan_sha256') != task.sha(output / 'work/export-plan.json') or receipt.get('handoff_sha256') != task.sha(output / 'work/handoff.json'):
        errors.append('No matching successful trusted CLI receipt after the current plan')
    if not rows(output / 'discovery.jsonl'):
        errors.append('Actual CLI discovery was not performed')
    if task.sha(_inside(output, 'work/data/project-decisions.db')) != manifest['work_db_sha256_after_baseline']:
        errors.append('Working ledger changed')
    return {'task_success': not errors, 'errors': errors, 'external_humans': 0,
            'real_project_continuation_tasks': 1, 'model_comparison_claim': False}


def redactor(output, manifest):
    mappings = {str(Path(output).resolve()): '<task>', str(task.ROOT): '<repo>', str(Path.home()): '<home>',
                manifest['interpreter']: '<fixed-sdk-python>'}
    original = Path(manifest['original_sources']['decision_log.py']['path']).parent
    mappings[str(original)] = '<original-task>'
    for name in manifest['installed_sdk_sha256']:
        path = Path(name)
        package = next((p for p in path.parents if p.name == 'memweft'), None)
        if package:
            mappings[str(package)] = '<fixed-sdk-package>'
    def clean(value):
        if isinstance(value, str):
            for source, replacement in sorted(mappings.items(), key=lambda item: -len(item[0])):
                value = value.replace(source, replacement)
            return value
        if isinstance(value, list):
            return [clean(item) for item in value]
        if isinstance(value, dict):
            return {clean(key): clean(item) for key, item in value.items()}
        return value
    return clean


def audit(output):
    if not __debug__:
        raise ValueError('Evidence audit requires Python assertions; do not run with -O')
    output = Path(output).resolve()
    manifest, freeze = _snapshot(output)
    initial = task.load(output / 'initial.json')
    status = task.load(output / 'status.json')
    result = task.load(output / 'result.json')
    assert status['status'] == 'completed', 'Only completed results supported by this report'
    attempts, responses, turns = (rows(output / (name + '.jsonl')) for name in ('attempts', 'responses', 'turns'))
    assert len(attempts) == len(responses) == len(turns) == result['model_calls']
    assert len(attempts) <= freeze['max_requests']
    messages = initial['messages']
    total = {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}
    gaps, model_ms = [], []
    previous = None
    for index, (attempt, response_row, turn) in enumerate(zip(attempts, responses, turns)):
        assert attempt['round'] == response_row['round'] == turn['round'] == index
        started = dt.datetime.fromisoformat(attempt['started_at'])
        if previous is not None:
            gap = (started - previous).total_seconds()
            assert gap >= freeze['interval_seconds'] - .05
            gaps.append(gap)
        previous = started
        response = response_row['response']
        payload = task.request_payload(messages, freeze['max_completion_tokens'])
        assert response['request'] == payload
        size = len(json.dumps(payload, ensure_ascii=False).encode('utf-8'))
        assert size == attempt['request_bytes'] <= freeze['request_byte_cap']
        assert response['model'] == freeze['model']
        usage = response['usage']
        assert all(type(usage.get(name)) is int and usage[name] >= 0 for name in total)
        assert usage['total_tokens'] == usage['prompt_tokens'] + usage['completion_tokens']
        for name in total:
            total[name] += usage[name]
        model_ms.append(response['latency_ms'])
        execution = turn['execution']
        if not execution['protocol_error']:
            body = task.strict_json_loads(response['content'])
            assert len(body['actions']) == len(execution['results']) <= freeze['max_actions']
            assert body['done'] == execution['done']
            for action, observation in zip(body['actions'], execution['results']):
                assert action['tool'] == observation['tool']
        messages = messages + [{'role': 'assistant', 'content': response['content']},
            {'role': 'user', 'content': json.dumps({'tool_results': execution['results'],
                 'rounds_remaining': 3 - index}, ensure_ascii=False, sort_keys=True)}]
    assert total == result['usage'] and total['total_tokens'] == result['reported_tokens']
    independent_grade = _grade(output, manifest)
    assert independent_grade['task_success'] == result['task_success']
    historical = task.load(output / 'original-inputs/previous-cli-list.json')
    discoveries = rows(output / 'discovery.jsonl')
    receipts = [(path, task.load(path)) for path in sorted((output / 'cli-runs').glob('*.json'))]
    commands = discoveries + [command for _, receipt in receipts for command in receipt['commands']]
    for command in commands:
        argv = command['argv']
        assert argv[:3] == [manifest['interpreter'], '-I', '-B']
        assert Path(argv[3]) == output / 'work/cli/decision_log.py'
        assert argv[4] == '--db' and Path(argv[5]) == output / 'work/data/project-decisions.db'
        assert argv[6] == '--project' and argv[8] == '--user'
        assert argv[10] in ('list', 'get') and len(argv) == (11 if argv[10] == 'list' else 12)
        if command['returncode'] == 0:
            answer = task.strict_json_loads(command['stdout'])
            assert answer['status'] == 'ok' and answer['schema_version'] == 1
            assert answer['scope'] == {'project': argv[7], 'user': argv[9]}
            if answer['scope'] == historical['scope']:
                expected = historical['decisions']
                if argv[10] == 'list':
                    assert answer['decisions'] == expected
                else:
                    assert answer['decision'] == next(record for record in expected if record['key'] == argv[11])
    last_write = last_export = -1
    observations = [observation for turn in turns for observation in turn['execution']['results']]
    for index, observation in enumerate(observations):
        if 'error' not in observation and observation['tool'] == 'write':
            last_write = index
        if observation['tool'] == 'run' and observation.get('result', {}).get('phase') == 'export' and observation['result'].get('ok'):
            last_export = index
    if result['task_success']:
        assert last_export > last_write >= 0
        assert task.load(output / 'work/handoff.json')['records'] == historical['decisions']
        # A matching artifact hash alone does not establish that the saved CLI
        # receipt contains the list/get/readback processes claimed by the export.
        final_receipt = receipts[-1][1]
        plan = task.load(output / 'work/export-plan.json')
        expected_commands = [('list', None), *[('get', key) for key in plan['keys']], ('list', None)]
        actual_commands = [(command['argv'][10], command['argv'][11] if len(command['argv']) == 12 else None)
                           for command in final_receipt['commands']]
        assert actual_commands == expected_commands, 'CLI receipt does not cover the export plan and readback'
        assert final_receipt['errors'] == []
        for command in final_receipt['commands']:
            assert command['returncode'] == 0, 'Successful export contains a failed CLI process'
            assert {'project': command['argv'][7], 'user': command['argv'][9]} == historical['scope']
    preserved = all(task.sha(entry['path']) == entry['sha256'] for entry in manifest['original_sources'].values())
    assert preserved
    work_equal = task.sha(output / 'work/data/project-decisions.db') == manifest['original_sources']['project-decisions.db']['sha256']
    assert work_equal
    clean = redactor(output, manifest)
    traces = [{'kind': 'initial', **initial}, {'kind': 'freeze', **freeze}]
    for attempt, response, turn in zip(attempts, responses, turns):
        traces.extend([{'kind': 'attempt', **attempt}, {'kind': 'response', **response}, {'kind': 'turn', **turn}])
    traces.extend({'kind': 'cli_discovery', 'observation': row} for row in discoveries)
    traces.extend({'kind': 'cli_export_receipt', 'path': path.relative_to(output).as_posix(),
                   'sha256': task.sha(path), 'receipt': receipt} for path, receipt in receipts)
    traces.append({'kind': 'source_manifest', 'manifest': manifest})
    for name in ('export-plan.json', 'handoff.json'):
        path = output / 'work' / name
        if path.exists():
            traces.append({'kind': 'artifact', 'path': 'work/' + name, 'sha256': task.sha(path), 'value': task.load(path)})
    traces = clean(traces)
    report = {'schema_version': 1, 'date': '2026-10-06', 'task': 'existing-project-decision-ledger-export',
        'task_success': result['task_success'], 'real_project_continuation_tasks': 1,
        'external_humans': 0, 'human_reviews': 0,
        'reviewer_relationship': 'Separate collaborating agent reviewed logs; this agent also implemented the task driver. Not an independent external evaluation.',
        'evidence_root': output.relative_to(task.ROOT).as_posix(), 'source_snapshot_date': '2026-10-02',
        'frozen_at': freeze['frozen_at'], 'run_started_at': attempts[0]['started_at'],
        'run_completed_at': result['completed_at'], 'model': freeze['model'], 'model_calls': len(responses),
        'model_usage': total, 'request_start_gaps_seconds': gaps, 'model_latency_ms': model_ms,
        'request_byte_cap': freeze['request_byte_cap'], 'max_completion_tokens': freeze['max_completion_tokens'],
        'pre_run_cooldown': 'Caller-owned 60s minimum; preceding experiment timing is outside this task evidence.',
        'automatic_retries': 0, 'tool_operations': len(observations),
        'protocol_errors': sum(turn['execution']['protocol_error'] for turn in turns),
        'tool_errors': sum('error' in observation for observation in observations),
        'cli_processes_during_model_task': len(commands),
        'cli_processes_during_offline_preparation': 1,
        'cli_commands': {name: sum(c['argv'][10] == name for c in commands) for name in ('list', 'get')},
        'cli_failures': sum(c['returncode'] != 0 for c in commands), 'export_runs': len(receipts),
        'records': historical['decisions'], 'scope': historical['scope'], 'sdk_pin': manifest['sdk_pin'],
        'original_source_hashes_unchanged': preserved, 'working_database_bytes_equal_original': work_equal,
        'independent_grade': independent_grade, 'source_sha256': freeze['source_sha256'],
        'current_audit_source_sha256': {path.relative_to(task.ROOT).as_posix(): task.sha(path) for path in
            (Path(__file__).resolve(), Path(task.__file__).resolve(), task.ROOT / 'evals/output_contract.py')},
        'audit_code_policy': 'Saved trial source snapshots are verified; current transport/native outside the fixed SDK are not required to match. No saved source is executed.',
        'raw_evidence_sha256': {name: task.sha(output / name) for name in
            ('freeze.json', 'source-manifest.json', 'initial.json', 'oracle.json', 'attempts.jsonl',
             'responses.jsonl', 'turns.jsonl', 'result.json')},
        'limitations': ['One bounded export-plan task over an existing three-record ledger.',
            'The trusted driver generated the export and hashes from actual CLI output; the model authored the plan.',
            'A frozen local copy was read; the original database and pinned SDK were not modified.',
            'Historical record contents were preserved, not revalidated as current project policy.',
            'No external system comparison, human trial, causal memory-quality claim, or new CLI implementation.']}
    public = json.dumps({'report': report, 'traces': traces}, ensure_ascii=False)
    task._scan_public({'report': report, 'traces': traces})
    assert not re.search(r'/Users/|/private/(?:tmp|var)/', public), 'Unredacted local path'
    assert all(not entry['path'] in public for entry in manifest['original_sources'].values())
    return report, traces


def write_report(output, stem):
    report, traces = audit(output)
    stem = Path(stem)
    stem.parent.mkdir(parents=True, exist_ok=True)
    task.dump(stem.with_suffix('.json'), report)
    stem.with_suffix('.traces.jsonl').write_text(''.join(json.dumps(row, ensure_ascii=False, sort_keys=True) + '\n' for row in traces), encoding='utf-8')
    usage, pin = report['model_usage'], report['sdk_pin']
    keys = '、'.join('`' + record['key'] + '`' for record in report['records'])
    verdict = '通过' if report['task_success'] else '未通过'
    markdown = f'''# 已有项目决策账本的真实 CLI 导出续作

2026-10-06：Kimi 通过受限 read/write/run 工具，为此前实际交付的项目决策账本编写 `export-plan.json`，
再由可信驱动器调用原 CLI，在新进程中读取决策、导出 `handoff.json` 并重新读取核对。最终验收**{verdict}**。
这是一项已有项目的导出续作，与三种构造工作流的系统对照分开报告；外部真人参与和人工评估均为 0。

## 实际工件与操作

输入来自 2026-10-02 已完成的 `decision_log.py` 和持久账本。模型起初获得任务与工具说明，
先执行实际 CLI 查询，随后根据返回的项目、用户和键编写导出计划。模型写计划，可信驱动器根据计划生成
包含真实记录、SDK 固定版本和代码哈希的导出；这不等同于模型实现了新的 CLI 或导出程序。

导出保留三条既有项目记录：{keys}，包括原值与来源。记录反映原账本快照，
本轮没有重新查证其中陈述是否仍是当前项目政策。数据库先复制到隔离目录；原始输入文件哈希保持一致，
工作数据库在执行前后也与原始字节相同。

| 观察项 | 结果 |
| --- | --- |
| 真实模型请求 | {report['model_calls']} |
| 模型输入 / 输出 tokens | {usage['prompt_tokens']} / {usage['completion_tokens']} |
| 工具操作 | {report['tool_operations']} |
| 任务中独立 CLI 进程 | {report['cli_processes_during_model_task']}（list {report['cli_commands']['list']}，get {report['cli_commands']['get']}） |
| CLI 失败 | {report['cli_failures']} |
| 协议 / 工具错误 | {report['protocol_errors']} / {report['tool_errors']} |
| 保留的真实决策 | 3 |

准备阶段另有一次离线 CLI 读取，与先前已记录的独立验收输出对照；没有将它算成模型调用或新任务。
模型为 `{report['model']}`，关闭思考，每次最多 {report['max_completion_tokens']} 输出 tokens；最多四轮，
请求开始间隔至少 21 秒，不自动重试。请求、响应、工具结果及 CLI 原始 stdout/stderr 均保留。
该报告仅汇总 tokens；未从未冻结的单价推算费用。

## 固定依赖与验收

本轮实际解释器为 CPython **{pin['python']} / macOS arm64**，SDK 为 `{pin['version']}`，
固定源码 `{pin['source_commit']}`；wheel SHA256 为
`{pin['wheel_sha256']}`。准备时将安装包文件与该 wheel 逐一比对，执行前后继续核对哈希。
这与 2026-10-02 首次参与者使用的 Python 3.11.13 属于不同解释器实例，SDK wheel 保持同一固定身份。

最终只读验收以此前保存的真实 CLI 输出作为独立答案来源，检查完整记录、来源、scope、SDK 与代码哈希，
以及当前计划对应的成功 CLI 回执。实际进程只调用 `list/get`，不允许任意命令或修改账本。
模型省略来源或记录时驱动器不会自动补齐，最终验收会失败。

审计验证保存的实验源码快照，记录当前审计代码哈希；后续 transport 代码更新不使既有实验失去可核对性。
审计不执行保存的源码，仍核对原始输入、固定 SDK、工作账本与实际导出工件。

复核由另一个协作代理完成，但该代理也参与了任务驱动器开发，因此不属于独立外部评估。
单个三条记录的导出任务支持的是这条受限工具工作流能够完成已有项目的具体续作，
不能证明长期 Agent 可靠性、生命周期机制带来因果提升或外部开发者可用性。

[机器报告]({stem.name}.json)包含哈希与逐项结果；[脱敏轨迹]({stem.name}.traces.jsonl)保留模型和 CLI 行为。
公开轨迹将个人绝对路径替换为固定占位符，业务记录与证据哈希保留；原始证据位于 Git 忽略的
`{report['evidence_root']}/`。冻结时间为 `{report['frozen_at']}`，实际模型运行开始于 `{report['run_started_at']}`。
'''
    stem.with_suffix('.md').write_text(markdown, encoding='utf-8')
    return {'task_success': report['task_success'], 'model_calls': report['model_calls'],
            'usage': usage, 'cli_processes': report['cli_processes_during_model_task'], 'report': str(stem.with_suffix('.md'))}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=task.OUTPUT)
    parser.add_argument('--report', type=Path, default=task.ROOT / 'evals/reports/2026-10-06-real-cli-export')
    args = parser.parse_args()
    print(json.dumps(write_report(args.output, args.report), ensure_ascii=False, indent=2))
