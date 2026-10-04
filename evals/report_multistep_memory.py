#!/usr/bin/env python3
"""Audit every recorded tool turn and publish complete constructed-task traces.

The saved source snapshots must match their prospective manifest. Replaying does
not require today's native binary or repository files to match that manifest;
the current replay implementation is recorded separately and must reproduce
every saved tool result, grade and actual artifact byte for byte.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
import math
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import statistics
import tempfile

from multistep_fixture import create_fixture, grade
from output_contract import strict_json_loads
from run_lifecycle_tasks import accounted_usage, dump, sha
from run_multistep_memory import ARMS, execute_actions, final_grade, initial_messages

USAGE = ('prompt_tokens', 'completion_tokens', 'total_tokens')
GROUP = ('case_id', 'arm', 'domain', 'event')
PUBLISHED_RATE_LIMIT = 'Kimi HTTP 429: provider rate limit (account identifiers omitted)'
LIMITS = [
    'Three constructed workflows, five related lifecycle events each; not 15 independent applications.',
    'One model and one sample per case/arm; no independent external developers or human participants.',
    'Trusted programs execute JSON configurations; no model-authored arbitrary code is executed.',
    'Mem0 uses native structured CRUD and local embeddings with adapter addressing; semantic extraction/search and hosted Mem0 are outside this contract.',
    'Current source tools replay prepared storage snapshots; their calls do not measure online retrieval latency.',
    'Sequential lifecycle schedules precede each model session; no mid-turn concurrency or production safety claim.',
    'Task success and tool cost are descriptive outcomes; no statistical significance or broad quality advantage is established.',
]


def _read(path):
    return strict_json_loads(path.read_text(encoding='utf-8'))


def _rows(path, optional=False):
    if optional and not path.exists():
        return []
    return [strict_json_loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def _same(actual, expected, message):
    # Unlike Python equality this keeps booleans distinct from numeric fields.
    encode = lambda v: json.dumps(v, ensure_ascii=False, sort_keys=True, allow_nan=False)
    if encode(actual) != encode(expected):
        raise ValueError(message)


def _key(row, attempt=False):
    fields = ('case_id', 'arm', 'round', 'attempt') if attempt else ('case_id', 'arm', 'round')
    for field in fields[2:]:
        if type(row.get(field)) is not int or row[field] < 0:
            raise ValueError('Invalid round/attempt identity')
    return tuple(row[field] for field in fields)


def _index(rows, key, description):
    result = {}
    for row in rows:
        identity = key(row)
        if identity in result:
            raise ValueError('Duplicate ' + description)
        result[identity] = row
    return result


def _inventory(root):
    result = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('Artifact inventory contains a symlink')
        if path.is_file():
            result[path.relative_to(root).as_posix()] = sha(path)
    return result


def _snapshot(output):
    freeze = _read(output / 'freeze.json')
    # Older prepares serialized Path with the host separator. Normalize only
    # this in-memory lookup; the frozen manifest and snapshot bytes stay intact.
    sources, aliases = {}, set()
    for relative, digest in freeze['source_sha256'].items():
        path = PurePosixPath(relative.replace('\\', '/'))
        if (path.is_absolute() or PureWindowsPath(relative).drive or not path.parts
                or '..' in path.parts or any(':' in part or part.endswith((' ', '.')) for part in path.parts)):
            raise ValueError('Unsafe snapshot path')
        normalized = path.as_posix()
        if normalized.casefold() in aliases:
            raise ValueError('Duplicate normalized snapshot path')
        aliases.add(normalized.casefold())
        sources[normalized] = digest
    required = {'evals/run_multistep_memory.py', 'evals/multistep_fixture.py',
                'evals/multistep_kimi.py', 'evals/external_lifecycle_baseline.py',
                'evals/output_contract.py', 'docs/multistep_memory_protocol.md'}
    if not required <= set(sources):
        raise ValueError('Missing frozen source snapshots')
    for relative, digest in sources.items():
        path = Path(relative)
        if sha(output / 'sources' / path) != digest:
            raise ValueError('Frozen source snapshot changed: ' + relative)
    for name, suffix in (('suite', '.json'), ('inputs', '.jsonl')):
        if sha(output / (name + suffix)) != freeze[name + '_sha256']:
            raise ValueError('Frozen ' + name + ' changed')
    if not re.fullmatch(r'[a-f0-9]{64}', freeze['native_sha256']):
        raise ValueError('Missing recorded native binary digest')
    limits = {'model': 'kimi-k2.6', 'max_rounds': 4, 'max_actions': 3,
              'max_completion_tokens': 1024, 'max_requests': 720,
              'max_successful_calls': 240, 'reported_token_stop_threshold': 500000,
              'request_byte_cap': 64000, 'rate_limit_retries': 2,
              'rate_limit_cooldown_seconds': 60, 'other_retries': 0}
    for field, expected in limits.items():
        _same(freeze[field], expected, 'Unexpected frozen limit: ' + field)
    return freeze


def audit(output):
    """Reject incomplete or inconsistent evidence; return a replayed summary."""
    output = Path(output)
    freeze = _snapshot(output)
    suite = _read(output / 'suite.json')['cases']
    cases = _index(suite, lambda c: c['id'], 'case')
    if len(cases) != 15 or any(not re.fullmatch(r'[a-z][a-z0-9_-]*', k) for k in cases):
        raise ValueError('Expected fifteen safe distinct case identities')
    events = {'update', 'forget', 'delete_recreate', 'rollback', 'unrelated_update'}
    if {c['domain'] for c in suite} != {'deploy', 'export', 'handoff'} or any(
            {c['event'] for c in suite if c['domain'] == d} != events for d in ('deploy', 'export', 'handoff')):
        raise ValueError('Expected the complete three-workflow/five-event matrix')
    inputs = _rows(output / 'inputs.jsonl')
    results = _rows(output / 'results.jsonl')
    attempts = _rows(output / 'attempts.jsonl')
    responses = _rows(output / 'responses.jsonl')
    turns = _rows(output / 'turns.jsonl')
    rate_limits = _rows(output / 'rate_limits.jsonl', optional=True)
    if _rows(output / 'errors.jsonl', optional=True):
        raise ValueError('Cannot publish an error-stopped run as complete')
    task_key = lambda r: (r['case_id'], r['arm'])
    input_map = _index(inputs, task_key, 'input task')
    result_map = _index(results, task_key, 'result task')
    wanted = {(c, arm) for c in cases for arm in ARMS}
    if set(input_map) != wanted or set(result_map) != wanted:
        raise ValueError('Incomplete case/arm coverage; expected all 60 tasks')
    response_map = _index(responses, _key, 'response turn')
    turn_map = _index(turns, _key, 'execution turn')
    attempt_map = _index(attempts, lambda r: _key(r, True), 'request attempt')
    rate_map = _index(rate_limits, lambda r: _key(r, True), 'rate limit')
    success_attempts = {_key(r, True) for r in responses}
    if (set(response_map) != set(turn_map) or success_attempts & set(rate_map)
            or set(attempt_map) != success_attempts | set(rate_map)):
        raise ValueError('Request accounting must equal successful responses plus recorded 429s')
    if len(responses) > freeze['max_successful_calls'] or len(attempts) > freeze['max_requests']:
        raise ValueError('Frozen request budget exceeded')
    if any('HTTP 429:' not in r.get('error', '') for r in rate_limits):
        raise ValueError('Non-429 failure claimed as a retryable rate limit')
    for record in attempts + responses + turns + rate_limits + results:
        case = cases.get(record['case_id'])
        if case is None or record['arm'] not in ARMS or any(record[f] != case[f] for f in ('domain', 'event')):
            raise ValueError('Mismatched case grouping')
    expected_turns, expected_attempts, artifacts = [], [], {}
    usage_before = 0
    with tempfile.TemporaryDirectory(prefix='memweft-multistep-audit-') as temp:
        for row in inputs:
            key = task_key(row)
            case, result = cases[key[0]], result_map[key]
            if any(row[f] != case[f] for f in ('domain', 'event')):
                raise ValueError('Mismatched input grouping')
            rounds = result['rounds']
            if type(rounds) is not int or not 1 <= rounds <= 4:
                raise ValueError('Task has an invalid number of rounds')
            replay = Path(temp) / key[0] / key[1]
            project = output / 'tasks' / key[0] / key[1] / 'project'
            files = create_fixture(replay, case)
            _same(row['files'], files, 'File permissions differ from fixture contract')
            initial_hashes = {p: sha(replay / p) for p in files['readable']}
            _same(row['initial_file_sha256'], initial_hashes, 'Initial fixture hashes do not reproduce')
            _same({p: sha(project / p) for p in files['readable']}, initial_hashes, 'Read-only project input changed')
            source = row['current_sources'].get('policy')
            expected_sources = {} if source is None else {'policy': source}
            _same(row['current_sources'], expected_sources, 'Unexpected extra source exposed')
            _same(None if source is None else source['value'], case['expected']['policy'], 'Source disagrees with frozen oracle')
            active = row['active']
            stale = active is not None and (source is None or active['dependencies']['policy'] != source['revision'])
            _same(row['stale_returned'], stale, 'Incorrect stale exposure label')
            messages = initial_messages(case, row)
            _same(row['messages'], messages, 'Initial messages differ from public input contract')
            executions = []
            for number in range(rounds):
                turn_key = (*key, number)
                expected_turns.append(turn_key)
                if turn_key not in response_map:
                    raise ValueError('Missing response for recorded round')
                record, turn = response_map[turn_key], turn_map[turn_key]
                attempt = record['attempt']
                if type(attempt) is not int or not 0 <= attempt <= freeze['rate_limit_retries']:
                    raise ValueError('Invalid successful retry number')
                expected_attempts.extend((*turn_key, i) for i in range(attempt + 1))
                if any((*turn_key, i) not in rate_map for i in range(attempt)):
                    raise ValueError('Retry has no corresponding prior 429')
                response = record['response']
                request = {'model': freeze['model'], 'messages': messages, 'stream': False,
                           'thinking': {'type': 'disabled'}, 'max_completion_tokens': 1024,
                           'response_format': {'type': 'json_object'}}
                _same(response['request'], request, 'Actual request or message history differs from replay')
                if len(json.dumps(request, ensure_ascii=False).encode()) > freeze['request_byte_cap']:
                    raise ValueError('Frozen request byte cap exceeded')
                if (not accounted_usage(response) or response['model'] != freeze['model']
                        or response['usage']['completion_tokens'] > 1024):
                    raise ValueError('Invalid response model or usage accounting')
                if usage_before >= freeze['reported_token_stop_threshold']:
                    raise ValueError('Model request made after token stop threshold')
                usage_before += response['usage']['total_tokens']
                latency = response['latency_ms']
                if type(latency) not in (float, int) or not math.isfinite(latency) or latency < 0:
                    raise ValueError('Invalid response latency')
                if not isinstance(response['content'], str) or not response['content'].strip():
                    raise ValueError('Empty successful model response')
                execution = execute_actions(replay, case, row, response['content'], response['finish_reason'])
                _same(turn['execution'], execution, 'Tool execution does not reproduce')
                _same(turn['grade'], grade(replay, case), 'Intermediate grade does not reproduce')
                executions.append(execution)
                if execution['done'] and number != rounds - 1:
                    raise ValueError('Task continued after done=true')
                if number == rounds - 1 and not execution['done'] and rounds != 4:
                    raise ValueError('Task stopped before done=true or round limit')
                messages.extend([{'role': 'assistant', 'content': response['content']},
                    {'role': 'user', 'content': json.dumps({'tool_results': execution['results'],
                        'rounds_remaining': 3 - number}, ensure_ascii=False, sort_keys=True)}])
            identity = {f: row[f] for f in GROUP}
            expected_result = {**identity, 'rounds': rounds, **final_grade(replay, case, executions)}
            _same(result, expected_result, 'Final result does not reproduce')
            _same(final_grade(project, case, executions), final_grade(replay, case, executions), 'On-disk final grade changed')
            artifacts['/'.join(key)] = _inventory(project)
            _same(artifacts['/'.join(key)], _inventory(replay), 'On-disk artifact bytes differ from replay')
    if ([_key(r) for r in responses] != expected_turns or [_key(r) for r in turns] != expected_turns
            or [_key(r, True) for r in attempts] != expected_attempts):
        raise ValueError('Unexpected, reordered or extra recorded model turns/attempts')
    for case_id in cases:
        sources = [input_map[case_id, arm]['current_sources'] for arm in ARMS]
        for source in sources[1:]:
            _same(source, sources[0], 'Arms received different current sources')
        _same(input_map[case_id, 'versions']['messages'], input_map[case_id, 'full']['messages'],
              'Versions/full initial messages differ')
        _same(input_map[case_id, 'plain']['messages'], input_map[case_id, 'mem0']['messages'],
              'Plain/Mem0 initial messages differ')
    if (output / 'status.json').exists():
        status = _read(output / 'status.json')
        expected = {'status': 'completed', 'calls': len(responses), 'requests': len(attempts), 'tokens': usage_before}
        _same(status, expected, 'Final status is not complete or disagrees with accounting')
    summary = {'completed_tasks': 60, 'model_calls': len(responses), 'requests': len(attempts),
               'rate_limits': len(rate_limits), 'by_arm': {}, 'case_results': results,
               'usage': {k: sum(r['response']['usage'][k] for r in responses) for k in USAGE},
               'paired_success': {}, 'artifact_sha256': artifacts,
               'metric_definitions': {
                   'tool_action_errors': 'Count of execution.results entries containing error, including the synthetic error entry for a rejected envelope; can overlap protocol_errors.',
                   'round_limit_without_done': 'Tasks with four recorded turns and done=false in the final execution, independent of task success.',
               },
               'audit': {'replayed_tasks': 60, 'replayed_turns': len(turns),
                         'request_accounting_complete': True, 'equal_current_sources': True,
                         'versions_full_initial_messages_identical': True,
                         'plain_mem0_initial_messages_identical': True,
                         'saved_source_snapshots_verified': True, 'actual_artifact_bytes_verified': True,
                         'current_native_digest_required': False}}
    for arm in ARMS:
        rr, ii = [r for r in results if r['arm'] == arm], [i for i in inputs if i['arm'] == arm]
        tt = [t for t in turns if t['arm'] == arm]
        calls = [r['response'] for r in responses if r['arm'] == arm]
        summary['by_arm'][arm] = {
            'tasks': len(rr), 'success': sum(r['task_success'] for r in rr),
            'stale_returned': sum(i['stale_returned'] for i in ii),
            'tasks_with_wrong_writes': len({t['case_id'] for t in tt if t['execution']['wrong_writes']}),
            'source_reads': sum(a.get('tool') == 'read_source' for t in tt for a in t['execution']['results']),
            'tool_operations': sum(len(t['execution']['results']) for t in tt),
            'model_calls': len(calls), 'protocol_errors': sum(t['execution']['protocol_error'] for t in tt),
            'tool_action_errors': sum('error' in action for t in tt for action in t['execution']['results']),
            'round_limit_without_done': sum(r['rounds'] == 4 and not turn_map[(r['case_id'], arm, 3)]['execution']['done'] for r in rr),
            'usage': {k: sum(r['usage'][k] for r in calls) for k in USAGE},
            'http_median_ms': statistics.median(r['latency_ms'] for r in calls),
            'events': {event: {'tasks': 3, 'success': sum(r['task_success'] for r in rr if r['event'] == event)} for event in sorted(events)},
        }
    for arm in ('plain', 'versions', 'mem0'):
        pairs = [(result_map[c, arm]['task_success'], result_map[c, 'full']['task_success']) for c in cases]
        summary['paired_success'][arm + '_vs_full'] = {'pairs': 15,
            'a_only': sum(a and not b for a, b in pairs), 'b_only': sum(b and not a for a, b in pairs)}
    usage = summary['usage']
    summary['estimated_cny_uncached'] = (usage['prompt_tokens'] * 6.5 + usage['completion_tokens'] * 27) / 1e6
    external = [r for r in inputs if r['arm'] == 'mem0' and 'manifest' in r]
    if external:
        summary['external_baseline'] = {
            'prepared_tasks': len(external), 'manifest': external[0]['manifest'],
            'init_median_ms': statistics.median(r['manifest']['init_ms'] for r in external),
            'reopen_init_median_ms': statistics.median(r['reopen_init_ms'] for r in external),
            'native_operation_count': sum(len(r['operations']) for r in external),
            'native_operation_total_ms': sum(op['ms'] for r in external for op in r['operations']),
            'scope': 'Native structured CRUD with local embedding; exact addressing and strategy lifecycle are adapter operations.',
        }
    return summary


def publish(output, prefix):
    output, prefix = Path(output), Path(prefix)
    summary = audit(output)
    cases = {c['id']: c for c in _read(output / 'suite.json')['cases']}
    inputs, results = _rows(output / 'inputs.jsonl'), _rows(output / 'results.jsonl')
    result_map = {(r['case_id'], r['arm']): r for r in results}
    grouped = {}
    for name in ('attempts', 'responses', 'turns', 'rate_limits'):
        groups = defaultdict(list)
        for row in _rows(output / (name + '.jsonl'), optional=name == 'rate_limits'):
            if name == 'rate_limits':
                row = {**row, 'error': PUBLISHED_RATE_LIMIT}
            groups[row['case_id'], row['arm']].append(row)
        grouped[name] = groups
    traces = []
    for row in inputs:
        key = row['case_id'], row['arm']
        traces.append({'case': cases[key[0]], 'input': row,
                       **{name: groups[key] for name, groups in grouped.items()},
                       'result': result_map[key], 'artifact_sha256': summary['artifact_sha256']['/'.join(key)]})
    prefix.parent.mkdir(parents=True, exist_ok=True)
    trace_path = prefix.with_suffix('.traces.jsonl')
    trace_path.write_text(''.join(json.dumps(t, ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n'
                                  for t in traces), encoding='utf-8')
    evidence_names = ['suite.json', 'inputs.jsonl', 'attempts.jsonl', 'responses.jsonl',
                      'turns.jsonl', 'results.jsonl']
    if (output / 'rate_limits.jsonl').exists():
        evidence_names.append('rate_limits.jsonl')
    report = {'freeze': _read(output / 'freeze.json'), 'summary': summary,
              'trace_sha256': sha(trace_path), 'reporter_sha256': sha(Path(__file__)),
              'replay_source_sha256': {p.name: sha(p) for p in (Path(__file__).with_name('run_multistep_memory.py'),
                  Path(__file__).with_name('multistep_fixture.py'), Path(__file__).with_name('output_contract.py'))},
              'evidence_sha256': {name: sha(output / name) for name in evidence_names},
              'publication_redactions': {'rate_limit_error_details': PUBLISHED_RATE_LIMIT,
                  'all_rate_limit_events_retained': True, 'raw_local_logs_unchanged': True},
              'pricing': {'source': 'https://platform.kimi.com/docs/pricing/chat', 'checked': '2026-10-04',
                  'cny_per_million_uncached_input': 6.5, 'cny_per_million_output': 27,
                  'estimated_cny_uncached': summary['estimated_cny_uncached'], 'not_billing_statement': True},
              'limits': LIMITS}
    dump(prefix.with_suffix('.json'), report)
    lines = ['# 多步文件任务：构造实验报告', '',
        '三个构造工作流 × 五种生命周期事件 × 四组，共 60 项任务。'
        '全部请求、工具动作、逐轮判分和磁盘工件已重放核对；完整模型输入输出见配套 traces.jsonl。', '',
        '| 组别 | 成功/15 | 过期策略暴露 | 有错误写入的任务 | 来源查询 | 模型调用 | tokens |',
        '| --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    for arm, value in summary['by_arm'].items():
        lines.append(f"| {arm} | {value['success']}/15 | {value['stale_returned']} | {value['tasks_with_wrong_writes']} | {value['source_reads']} | {value['model_calls']} | {value['usage']['total_tokens']} |")
    lines += ['', f"共 {summary['requests']} 次 HTTP 尝试、{summary['model_calls']} 个完成响应、"
              f"{summary['rate_limits']} 次已记录的 429。按未命中缓存价格估算 ¥{summary['estimated_cny_uncached']:.4f}，"
              '不等于账单。使用 [Kimi 官方价格](https://platform.kimi.com/docs/pricing/chat)，核查日期 2026-10-04。', '',
        '任务质量、错误实际写入和查询成本分开报告。相同成功率只能说明本次样本未观察到质量差异；'
        '不以过期策略暴露直接推导最终任务失败。Mem0 对照实际执行原生结构化 CRUD 与本地 embedding，'
        '活动策略和回滚由适配器实现，本轮不评测其语义抽取、相似度搜索或托管产品。', '',
        'versions/full 初始上下文逐例相同，plain/mem0 也逐例相同；这些成对组别的单次模型结果差异'
        '不能单独证明存储机制导致的质量差异。', '',
        '冻结源码快照与输入哈希已验证；报告使用当前执行器逐轮重放，并记录执行器哈希，'
        '不要求报告生成时的 native 二进制仍与实验相同。各任务最终磁盘文件与重放的字节哈希一致。', '',
        '公开 traces 保留每次 429 的事件身份与尝试序号，将错误正文统一替换以省略账户标识。'
        '本地原始日志保持不变；若有 rate_limits.jsonl，其原始 SHA-256 记录在 JSON 报告中。', '',
        '限制：三个模板的相关事件、每组每例一次采样、无独立外部开发者或真人参与，'
        '不构成统计显著性、生产环境收益或一般性系统优劣证据。各事件结果和配对差异保留在 JSON 报告中。', '']
    prefix.with_suffix('.md').write_text('\n'.join(lines), encoding='utf-8')
    return {'report': str(prefix.with_suffix('.md')), 'json': str(prefix.with_suffix('.json')),
            'traces': str(trace_path), **summary['audit'], 'estimated_cny': summary['estimated_cny_uncached']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--prefix', '--stem', required=True, dest='prefix', type=Path)
    args = parser.parse_args()
    print(json.dumps(publish(args.input, args.prefix), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
