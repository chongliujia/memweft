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
from run_multistep_memory import ARMS, ROOT, execute_actions, final_grade, initial_messages

USAGE = ('prompt_tokens', 'completion_tokens', 'total_tokens')
GROUP = ('case_id', 'arm', 'domain', 'event')
PUBLISHED_RATE_LIMIT = 'Kimi HTTP 429: provider rate limit (account identifiers omitted)'
COHORT_EVIDENCE = ('freeze.json', 'inputs.jsonl', 'results.jsonl', 'status.json', 'attempts.jsonl',
                   'responses.jsonl', 'turns.jsonl', 'rate_limits.jsonl', 'errors.jsonl')
LIMITS = [
    'Three constructed workflows, five related lifecycle events each; not 15 independent applications.',
    'One model and one session per case/arm within each cohort; interrupted sessions may have a separately reported restarted supplement. No independent external developers or human participants.',
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


def _relative_path(relative):
    path = PurePosixPath(relative.replace('\\', '/'))
    if (path.is_absolute() or PureWindowsPath(relative).drive or not path.parts
            or '..' in path.parts or any(':' in part or part.endswith((' ', '.')) for part in path.parts)):
        raise ValueError('Unsafe snapshot path')
    return path.as_posix()


def _snapshot(output):
    freeze = _read(output / 'freeze.json')
    # Older prepares serialized Path with the host separator. Normalize only
    # this in-memory lookup; the frozen manifest and snapshot bytes stay intact.
    sources, aliases = {}, set()
    for relative, digest in freeze['source_sha256'].items():
        normalized = _relative_path(relative)
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
        snapshot = output / 'sources' / path
        source_root = output / 'sources'
        if any(p.is_symlink() for p in [snapshot, *snapshot.parents] if p == source_root or source_root in p.parents):
            raise ValueError('Frozen source snapshot follows a symlink')
        if sha(snapshot) != digest:
            raise ValueError('Frozen source snapshot changed: ' + relative)
    for name, suffix in (('suite', '.json'), ('inputs', '.jsonl')):
        if sha(output / (name + suffix)) != freeze[name + '_sha256']:
            raise ValueError('Frozen ' + name + ' changed')
    if not re.fullmatch(r'[a-f0-9]{64}', freeze['native_sha256']):
        raise ValueError('Missing recorded native binary digest')
    planned = 60
    if 'supplement' in freeze and 'continuation' in freeze:
        raise ValueError('A cohort cannot be both supplement and continuation')
    continuation = freeze.get('continuation', freeze.get('supplement'))
    if continuation is not None:
        planned = len(continuation['selected_tasks'])
        if not 0 < planned < 60:
            raise ValueError('Supplement must declare a nonempty proper task subset')
        _same(freeze['interval_seconds'], 21, 'Unexpected supplement pacing')
    limits = {'model': 'kimi-k2.6', 'max_rounds': 4, 'max_actions': 3,
              'max_completion_tokens': 1024, 'max_requests': planned * 12,
              'max_successful_calls': planned * 4, 'reported_token_stop_threshold': 500000,
              'request_byte_cap': 64000, 'rate_limit_retries': 2,
              'rate_limit_cooldown_seconds': 60, 'other_retries': 0}
    for field, expected in limits.items():
        _same(freeze[field], expected, 'Unexpected frozen limit: ' + field)
    return freeze


def _supplement_reference(output, freeze, inputs, all_tasks, ancestors=()):
    """Audit the original cohort separately before validating an explicit subset."""
    supplement = freeze['supplement']
    original = ROOT / _relative_path(supplement['original_run'])
    if original.resolve() == output.resolve():
        raise ValueError('Supplement cannot reference itself')
    for name, suffix in [('freeze', '.json'), ('inputs', '.jsonl'), ('results', '.jsonl'),
                         ('status', '.json'), ('attempts', '.jsonl'), ('responses', '.jsonl'),
                         ('turns', '.jsonl'), ('rate_limits', '.jsonl'), ('errors', '.jsonl')]:
        if sha(original / (name + suffix)) != supplement['original_' + name + '_sha256']:
            raise ValueError('Original cohort evidence changed: ' + name)
    original_freeze = _read(original / 'freeze.json')
    if 'supplement' in original_freeze or 'continuation' in original_freeze:
        raise ValueError('Chained supplements need a separate explicit protocol')
    original_summary = audit(original, allow_partial=True, _ancestors=ancestors)
    original_inputs = _rows(original / 'inputs.jsonl')
    original_results = _rows(original / 'results.jsonl')
    identity = lambda r: {k: r[k] for k in ('case_id', 'arm')}
    completed = [identity(r) for r in original_results]
    completed_keys = {(r['case_id'], r['arm']) for r in completed}
    selected_rows = [r for r in original_inputs if (r['case_id'], r['arm']) not in completed_keys]
    selected = [identity(r) for r in selected_rows]
    _same(supplement['original_completed_tasks'], completed, 'Supplement changed original completed-task selection')
    _same(supplement['selected_tasks'], selected, 'Supplement must select all and only original unfinished tasks')
    _same(inputs, selected_rows, 'Supplement inputs differ from original frozen contexts')
    _same(supplement['original_unanswered_attempts'], original_summary['unanswered_attempts'], 'Supplement omitted original unanswered accounting')
    if {(r['case_id'], r['arm']) for r in selected} | completed_keys != all_tasks:
        raise ValueError('Supplement and original results do not cover original task matrix')
    for field in ('context_restarted', 'retained_original_partial_responses', 'initial_messages_byte_equivalent',
                  'backend_preparation_copied_unchanged', 'fixture_recreated_from_frozen_create_function'):
        _same(supplement[field], True, 'Supplement restart/provenance declaration missing: ' + field)
    for field in ('suite_sha256', 'native_sha256', 'source_sha256'):
        _same(freeze[field], original_freeze[field], 'Supplement changed frozen runtime or suite: ' + field)
    preparation = output / _relative_path(supplement['preparation_script'])
    if sha(preparation) != supplement['preparation_script_sha256']:
        raise ValueError('Supplement preparation snapshot changed')
    backend_files = 0
    for row in selected:
        relative = Path('tasks') / row['case_id'] / row['arm']
        before = {name: digest for name, digest in _inventory(original / relative).items() if not name.startswith('project/')}
        copied = {name: digest for name, digest in _inventory(output / relative).items() if not name.startswith('project/')}
        _same(copied, before, 'Supplement backend preparation evidence differs from original')
        backend_files += len(copied)
    return {'cohort_id': supplement['cohort_id'], 'original_run': supplement['original_run'],
            'original_completed_tasks': original_summary['completed_tasks'],
            'original_unanswered_requests': len(original_summary['unanswered_attempts']),
            'original_known_reported_subtotal_cny_uncached': original_summary['known_reported_subtotal_cny_uncached'],
            'original_total_cost_unknown': not original_summary['usage_complete'],
            'selected_tasks_verified_against_original': True, 'original_partial_audit_replayed': True,
            'backend_evidence_bytes_verified': True, 'backend_evidence_files': backend_files,
            'context_restarted': True, 'combined_cohorts': False}


def _continuation_reference(output, freeze, inputs, all_tasks, ancestors):
    """Verify every prior cohort independently; only absent final results qualify."""
    continuation = freeze['continuation']
    priors = continuation['prior_cohorts']
    if not isinstance(priors, list) or len(priors) < 2:
        raise ValueError('Continuation must explicitly declare at least two prior cohorts')
    root = ROOT / _relative_path(continuation['original_run'])
    prior_paths = [ROOT / _relative_path(prior['run']) for prior in priors]
    if prior_paths[0].resolve() != root.resolve() or len({p.resolve() for p in prior_paths}) != len(prior_paths):
        raise ValueError('Continuation prior cohorts must start with the original and be distinct')
    if output.resolve() in {p.resolve() for p in prior_paths}:
        raise ValueError('Continuation cannot reference itself')
    completed, completed_keys, summaries, unknown = [], set(), [], []
    root_freeze = None
    for index, (prior, path) in enumerate(zip(priors, prior_paths)):
        evidence = prior['evidence_sha256']
        if set(evidence) != set(COHORT_EVIDENCE):
            raise ValueError('Continuation must declare each prior evidence file or its absence')
        for name in COHORT_EVIDENCE:
            target = path / name
            actual = sha(target) if target.exists() else None
            _same(actual, evidence[name], 'Prior cohort evidence changed: ' + prior['run'] + '/' + name)
        prior_freeze = _read(path / 'freeze.json')
        if index == 0:
            if 'supplement' in prior_freeze or 'continuation' in prior_freeze:
                raise ValueError('First prior cohort must be the original experiment')
            root_freeze = prior_freeze
            _same(continuation['root_freeze_sha256'], sha(path / 'freeze.json'), 'Original freeze anchor differs')
        else:
            for field in ('suite_sha256', 'native_sha256', 'source_sha256'):
                _same(prior_freeze[field], root_freeze[field], 'Prior cohorts do not share frozen runtime/suite')
            if 'supplement' in prior_freeze:
                _same(prior_freeze['supplement']['original_run'], continuation['original_run'], 'Prior supplement belongs to another root')
            elif 'continuation' in prior_freeze:
                _same(prior_freeze['continuation']['prior_cohorts'], priors[:index], 'Prior continuation omitted or reordered its history')
            else:
                raise ValueError('Later prior cohorts need explicit supplement/continuation provenance')
        result_rows = _rows(path / 'results.jsonl', optional=True)
        for result in result_rows:
            key = (result['case_id'], result['arm'])
            if key in completed_keys:
                raise ValueError('Duplicate final result across prior cohorts')
            completed_keys.add(key)
            completed.append({k: result[k] for k in ('case_id', 'arm')})
        summary = audit(path, allow_partial=True, _ancestors=ancestors)
        summaries.append({'run': prior['run'], 'cohort_status': summary['cohort_status'],
            'completed_tasks': summary['completed_tasks'], 'model_calls': summary['model_calls'],
            'requests': summary['requests'], 'known_reported_usage': summary['usage'],
            'known_reported_subtotal_cny_uncached': summary['known_reported_subtotal_cny_uncached'],
            'unanswered_requests': len(summary['unanswered_attempts']), 'usage_complete': summary['usage_complete']})
        unknown.extend({'cohort': prior['run'], **attempt} for attempt in summary['unanswered_attempts'])
    for field in ('suite_sha256', 'native_sha256', 'source_sha256'):
        _same(freeze[field], root_freeze[field], 'Continuation changed original frozen runtime/suite: ' + field)
    selected_rows = [r for r in _rows(root / 'inputs.jsonl') if (r['case_id'], r['arm']) not in completed_keys]
    selected = [{k: r[k] for k in ('case_id', 'arm')} for r in selected_rows]
    _same(continuation['previous_completed_tasks'], completed, 'Continuation changed prior completed-task selection')
    _same(continuation['selected_tasks'], selected, 'Continuation must select all and only tasks without prior final results')
    _same(inputs, selected_rows, 'Continuation inputs differ from original frozen contexts')
    if completed_keys | {(r['case_id'], r['arm']) for r in selected} != all_tasks:
        raise ValueError('Continuation selection does not cover original task matrix')
    for field in ('context_restarted', 'retained_prior_partial_responses', 'initial_messages_byte_equivalent',
                  'backend_preparation_copied_unchanged', 'fixture_recreated_from_frozen_create_function'):
        _same(continuation[field], True, 'Continuation restart/provenance declaration missing: ' + field)
    preparation = output / _relative_path(continuation['preparation_script'])
    if sha(preparation) != continuation['preparation_script_sha256']:
        raise ValueError('Continuation preparation snapshot changed')
    runtime = output / _relative_path(continuation['runtime_root'])
    _same(continuation['runtime_source_sha256'], root_freeze['source_sha256'], 'Isolated runtime source manifest differs from original')
    for name, digest in continuation['runtime_source_sha256'].items():
        if sha(runtime / _relative_path(name)) != digest:
            raise ValueError('Isolated continuation runtime changed: ' + name)
    if 'runtime_native_file' in continuation:
        if sha(output / _relative_path(continuation['runtime_native_file'])) != root_freeze['native_sha256']:
            raise ValueError('Isolated continuation native binary changed')
    for name, digest in continuation.get('runtime_support_sha256', {}).items():
        if sha(runtime / 'python/src/memweft' / _relative_path(name)) != digest:
            raise ValueError('Isolated continuation SDK support changed: ' + name)
    for name in ('launcher', 'auditor_script'):
        if name in continuation and sha(output / _relative_path(continuation[name])) != continuation[name + '_sha256']:
            raise ValueError('Continuation ' + name + ' changed')
    backend_files = 0
    for row in selected:
        relative = Path('tasks') / row['case_id'] / row['arm']
        original_backend = {n: h for n, h in _inventory(root / relative).items() if not n.startswith('project/')}
        actual_backend = {n: h for n, h in _inventory(output / relative).items() if not n.startswith('project/')}
        _same(actual_backend, original_backend, 'Continuation backend evidence differs from original')
        backend_files += len(actual_backend)
    return {'cohort_id': continuation['cohort_id'], 'original_run': continuation['original_run'],
            'prior_cohorts': summaries, 'previous_completed_tasks': len(completed),
            'prior_unanswered_attempts': unknown, 'prior_usage_complete': not unknown,
            'prior_known_reported_usage': {k: sum(s['known_reported_usage'][k] for s in summaries) for k in USAGE},
            'prior_known_reported_subtotal_cny_uncached': sum(s['known_reported_subtotal_cny_uncached'] for s in summaries),
            'duplicate_final_results_rejected': True, 'selected_tasks_verified_against_all_priors': True,
            'backend_evidence_bytes_verified': True, 'backend_evidence_files': backend_files,
            'prior_cohorts_independently_audited': True, 'isolated_runtime_bytes_verified': True,
            'context_restarted': True, 'combined_cohorts': False}


def audit(output, *, allow_partial=False, _ancestors=()):
    """Reject incomplete or inconsistent evidence; return a replayed summary."""
    output = Path(output)
    if output.resolve() in _ancestors:
        raise ValueError('Cyclic cohort reference')
    ancestors = (*_ancestors, output.resolve())
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
    results = _rows(output / 'results.jsonl', optional=allow_partial)
    attempts = _rows(output / 'attempts.jsonl')
    responses = _rows(output / 'responses.jsonl', optional=allow_partial)
    turns = _rows(output / 'turns.jsonl', optional=allow_partial)
    rate_limits = _rows(output / 'rate_limits.jsonl', optional=True)
    errors = _rows(output / 'errors.jsonl', optional=True)
    status = _read(output / 'status.json') if (output / 'status.json').exists() else None
    partial = allow_partial and status is not None and status.get('status') == 'stopped_error'
    if errors and not partial:
        raise ValueError('Cannot publish an error-stopped run as complete')
    if partial and len(errors) != 1:
        raise ValueError('Partial audit requires one recorded terminal error')
    task_key = lambda r: (r['case_id'], r['arm'])
    input_map = _index(inputs, task_key, 'input task')
    result_map = _index(results, task_key, 'result task')
    original_wanted = {(c, arm) for c in cases for arm in ARMS}
    supplement_reference = _supplement_reference(output, freeze, inputs, original_wanted, ancestors) if 'supplement' in freeze else None
    continuation_reference = _continuation_reference(output, freeze, inputs, original_wanted, ancestors) if 'continuation' in freeze else None
    selection = freeze.get('continuation', freeze.get('supplement'))
    wanted = ({(r['case_id'], r['arm']) for r in selection['selected_tasks']} if selection else original_wanted)
    planned = len(wanted)
    if set(input_map) != wanted or (not partial and set(result_map) != wanted):
        raise ValueError('Incomplete case/arm coverage; expected all planned cohort tasks')
    if partial and (len(results) >= planned or [task_key(r) for r in results] != [task_key(r) for r in inputs[:len(results)]]):
        raise ValueError('Partial results must be the completed prefix of frozen inputs')
    response_map = _index(responses, _key, 'response turn')
    turn_map = _index(turns, _key, 'execution turn')
    attempt_map = _index(attempts, lambda r: _key(r, True), 'request attempt')
    rate_map = _index(rate_limits, lambda r: _key(r, True), 'rate limit')
    success_attempts = {_key(r, True) for r in responses}
    unanswered = set(attempt_map) - success_attempts - set(rate_map)
    if (set(response_map) != set(turn_map) or success_attempts & set(rate_map)
            or not (success_attempts | set(rate_map)) <= set(attempt_map)
            or (not partial and unanswered)):
        raise ValueError('Request accounting must equal successful responses plus recorded 429s')
    if partial and (len(unanswered) != 1 or _key(attempts[-1], True) not in unanswered):
        raise ValueError('Partial audit requires exactly one final unanswered request attempt')
    terminal = next(iter(unanswered)) if unanswered else None
    interrupted_key = task_key(inputs[len(results)]) if partial else None
    if terminal is not None and terminal[:2] != interrupted_key:
        raise ValueError('Unanswered request must belong to the next unfinished task')
    if len(responses) > freeze['max_successful_calls'] or len(attempts) > freeze['max_requests']:
        raise ValueError('Frozen request budget exceeded')
    if any('HTTP 429:' not in r.get('error', '') for r in rate_limits):
        raise ValueError('Non-429 failure claimed as a retryable rate limit')
    for record in attempts + responses + turns + rate_limits + results:
        case = cases.get(record['case_id'])
        if case is None or record['arm'] not in ARMS or any(record[f] != case[f] for f in ('domain', 'event')):
            raise ValueError('Mismatched case grouping')
    expected_turns, expected_attempts, artifacts, task_states = [], [], {}, {}
    usage_before = 0
    with tempfile.TemporaryDirectory(prefix='memweft-multistep-audit-') as temp:
        for row in inputs:
            key = task_key(row)
            case, result = cases[key[0]], result_map.get(key)
            if any(row[f] != case[f] for f in ('domain', 'event')):
                raise ValueError('Mismatched input grouping')
            rounds = result['rounds'] if result is not None else sum(k[:2] == key for k in response_map)
            if type(rounds) is not int or not (1 if result is not None else 0) <= rounds <= 4:
                raise ValueError('Task has an invalid number of rounds')
            if result is None and key != interrupted_key and rounds:
                raise ValueError('Responses recorded after the interrupted task')
            task_states['/'.join(key)] = 'completed' if result is not None else 'interrupted' if key == interrupted_key else 'unstarted'
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
                if result is not None and number == rounds - 1 and not execution['done'] and rounds != 4:
                    raise ValueError('Task stopped before done=true or round limit')
                messages.extend([{'role': 'assistant', 'content': response['content']},
                    {'role': 'user', 'content': json.dumps({'tool_results': execution['results'],
                        'rounds_remaining': 3 - number}, ensure_ascii=False, sort_keys=True)}])
            if result is not None:
                identity = {f: row[f] for f in GROUP}
                expected_result = {**identity, 'rounds': rounds, **final_grade(replay, case, executions)}
                _same(result, expected_result, 'Final result does not reproduce')
                _same(final_grade(project, case, executions), final_grade(replay, case, executions), 'On-disk final grade changed')
            elif key == interrupted_key:
                if rounds >= 4 or (executions and executions[-1]['done']):
                    raise ValueError('Unanswered request followed an already finished task')
                if terminal[2] != rounds or not 0 <= terminal[3] <= freeze['rate_limit_retries']:
                    raise ValueError('Unanswered request has an invalid next round or retry')
                if any((*key, rounds, i) not in rate_map for i in range(terminal[3])):
                    raise ValueError('Unanswered retry has no prior recorded 429')
                expected_attempts.extend((*key, rounds, i) for i in range(terminal[3] + 1))
                pending_request = {'model': freeze['model'], 'messages': messages, 'stream': False,
                    'thinking': {'type': 'disabled'}, 'max_completion_tokens': 1024,
                    'response_format': {'type': 'json_object'}}
                if (usage_before >= freeze['reported_token_stop_threshold'] or len(responses) >= freeze['max_successful_calls']
                        or len(json.dumps(pending_request, ensure_ascii=False).encode()) > freeze['request_byte_cap']):
                    raise ValueError('Unanswered request was attempted beyond the frozen budget')
            artifacts['/'.join(key)] = _inventory(project)
            _same(artifacts['/'.join(key)], _inventory(replay), 'On-disk artifact bytes differ from replay')
    if ([_key(r) for r in responses] != expected_turns or [_key(r) for r in turns] != expected_turns
            or [_key(r, True) for r in attempts] != expected_attempts):
        raise ValueError('Unexpected, reordered or extra recorded model turns/attempts')
    for case_id in cases:
        sources = [input_map[case_id, arm]['current_sources'] for arm in ARMS if (case_id, arm) in input_map]
        for source in sources[1:]:
            _same(source, sources[0], 'Arms received different current sources')
        for left, right in [('versions', 'full'), ('plain', 'mem0')]:
            if (case_id, left) in input_map and (case_id, right) in input_map:
                _same(input_map[case_id, left]['messages'], input_map[case_id, right]['messages'],
                      left + '/' + right + ' initial messages differ')
    if status is not None:
        expected = {'status': 'stopped_error' if partial else 'completed', 'calls': len(responses), 'requests': len(attempts), 'tokens': usage_before}
        _same(status, expected, 'Final status is not complete or disagrees with accounting')
    started = {task_key(a) for a in attempts}
    summary = {'cohort_status': 'stopped_error_partial' if partial else 'completed',
               'cohort_kind': 'continuation' if continuation_reference else 'supplement' if supplement_reference else 'original',
               'supplement_reference': supplement_reference, 'continuation_reference': continuation_reference,
               'original_planned_tasks': 60,
               'planned_tasks': planned, 'completed_tasks': len(results), 'interrupted_tasks': int(partial),
               'unstarted_tasks': planned - len(started), 'incomplete_tasks': planned - len(results),
               'task_states': task_states, 'model_calls': len(responses), 'requests': len(attempts),
               'unanswered_attempts': [attempt_map[k] for k in sorted(unanswered)],
               'usage_complete': not unanswered, 'total_billing_cost_known': False,
               'rate_limits': len(rate_limits), 'by_arm': {}, 'case_results': results,
               'usage': {k: sum(r['response']['usage'][k] for r in responses) for k in USAGE},
               'paired_success': {}, 'artifact_sha256': artifacts,
               'metric_definitions': {
                   'stale_returned': 'Prepared stale context for tasks with a recorded request attempt; does not establish that an unanswered request reached the provider.',
                   'recorded_turn_metrics': 'Tool, response and token counts include all recorded turns, including interrupted tasks; success denominators include completed tasks only.',
                   'tool_action_errors': 'Count of execution.results entries containing error, including the synthetic error entry for a rejected envelope; can overlap protocol_errors.',
                   'round_limit_without_done': 'Tasks with four recorded turns and done=false in the final execution, independent of task success.',
                   'artifact_only_success': 'Post-hoc diagnostic: task_success from the final recorded turn.grade; checks artifacts without the final public-run requirement.',
                   'missing_final_public_run': 'Post-hoc diagnostic: final result contains the required successful public execution error; may overlap other failures.',
               },
               'post_hoc_diagnostics': {
                   'fields': ['artifact_only_success', 'missing_final_public_run'],
                   'introduced_after_observing_failures': True,
                   'primary_success_definition_unchanged': True,
               },
               'audit': {'replayed_tasks': len({task_key(t) for t in turns}), 'replayed_turns': len(turns),
                         'verified_fixture_tasks': planned, 'replayed_completed_tasks': len(results),
                         'partial_mode_explicit': bool(partial),
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
            'planned_tasks': len(ii), 'original_planned_tasks': 15, 'tasks': len(rr), 'success': sum(r['task_success'] for r in rr),
            'incomplete_tasks': len(ii) - len(rr), 'started_tasks': sum(task_key(i) in started for i in ii),
            'stale_returned': sum(i['stale_returned'] for i in ii if task_key(i) in started),
            'stale_prepared': sum(i['stale_returned'] for i in ii),
            'tasks_with_wrong_writes': len({t['case_id'] for t in tt if t['execution']['wrong_writes']}),
            'source_reads': sum(a.get('tool') == 'read_source' for t in tt for a in t['execution']['results']),
            'tool_operations': sum(len(t['execution']['results']) for t in tt),
            'model_calls': len(calls), 'protocol_errors': sum(t['execution']['protocol_error'] for t in tt),
            'tool_action_errors': sum('error' in action for t in tt for action in t['execution']['results']),
            'round_limit_without_done': sum(r['rounds'] == 4 and not turn_map[(r['case_id'], arm, 3)]['execution']['done'] for r in rr),
            'artifact_only_success': sum(turn_map[(r['case_id'], arm, r['rounds'] - 1)]['grade']['task_success'] for r in rr),
            'missing_final_public_run': sum(any('Successful public execution required' in error for error in r['errors']) for r in rr),
            'usage': {k: sum(r['usage'][k] for r in calls) for k in USAGE},
            'http_median_ms': statistics.median(r['latency_ms'] for r in calls) if calls else None,
            'events': {event: {'planned_tasks': sum(i['event'] == event for i in ii),
                              'tasks': sum(r['event'] == event for r in rr),
                              'success': sum(r['task_success'] for r in rr if r['event'] == event)} for event in sorted(events)},
        }
    for arm in ('plain', 'versions', 'mem0'):
        pairs = [(result_map[c, arm]['task_success'], result_map[c, 'full']['task_success']) for c in cases
                 if (c, arm) in result_map and (c, 'full') in result_map]
        planned_pairs = sum((c, arm) in input_map and (c, 'full') in input_map for c in cases)
        summary['paired_success'][arm + '_vs_full'] = {'original_planned_pairs': 15, 'planned_pairs': planned_pairs, 'pairs': len(pairs),
            'a_only': sum(a and not b for a, b in pairs), 'b_only': sum(b and not a for a, b in pairs)}
    usage = summary['usage']
    summary['known_reported_subtotal_cny_uncached'] = (usage['prompt_tokens'] * 6.5 + usage['completion_tokens'] * 27) / 1e6
    summary['estimated_cny_uncached'] = None if unanswered else summary['known_reported_subtotal_cny_uncached']
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


def publish(output, prefix, *, allow_partial=False):
    output, prefix = Path(output), Path(prefix)
    summary = audit(output, allow_partial=allow_partial)
    cases = {c['id']: c for c in _read(output / 'suite.json')['cases']}
    inputs, results = _rows(output / 'inputs.jsonl'), _rows(output / 'results.jsonl', optional=allow_partial)
    result_map = {(r['case_id'], r['arm']): r for r in results}
    grouped = {}
    for name in ('attempts', 'responses', 'turns', 'rate_limits'):
        groups = defaultdict(list)
        for row in _rows(output / (name + '.jsonl'), optional=name == 'rate_limits' or allow_partial):
            if name == 'rate_limits':
                row = {**row, 'error': PUBLISHED_RATE_LIMIT}
            groups[row['case_id'], row['arm']].append(row)
        grouped[name] = groups
    traces = []
    for row in inputs:
        key = row['case_id'], row['arm']
        traces.append({'case': cases[key[0]], 'input': row,
                       **{name: groups[key] for name, groups in grouped.items()},
                       'task_status': summary['task_states']['/'.join(key)],
                       'result': result_map.get(key), 'artifact_sha256': summary['artifact_sha256']['/'.join(key)]})
    prefix.parent.mkdir(parents=True, exist_ok=True)
    trace_path = prefix.with_suffix('.traces.jsonl')
    trace_path.write_text(''.join(json.dumps(t, ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n'
                                  for t in traces), encoding='utf-8')
    evidence_names = [name for name in ('freeze.json', 'suite.json', 'inputs.jsonl', 'attempts.jsonl',
        'responses.jsonl', 'turns.jsonl', 'results.jsonl', 'rate_limits.jsonl', 'status.json', 'errors.jsonl')
        if (output / name).exists()]
    freeze = _read(output / 'freeze.json')
    preparation_metadata = freeze.get('continuation', freeze.get('supplement'))
    if preparation_metadata:
        for name in (preparation_metadata['preparation_script'], preparation_metadata['accounting_file']):
            name = _relative_path(name)
            if (output / name).exists():
                evidence_names.append(name)
    report = {'freeze': _read(output / 'freeze.json'), 'summary': summary,
              'trace_sha256': sha(trace_path), 'reporter_sha256': sha(Path(__file__)),
              'replay_source_sha256': {p.name: sha(p) for p in (Path(__file__).with_name('run_multistep_memory.py'),
                  Path(__file__).with_name('multistep_fixture.py'), Path(__file__).with_name('output_contract.py'))},
              'evidence_sha256': {name: sha(output / name) for name in evidence_names},
              'publication_redactions': {'rate_limit_error_details': PUBLISHED_RATE_LIMIT,
                  'all_rate_limit_events_retained': True, 'raw_local_logs_unchanged': True,
                  'terminal_error_body_omitted': not summary['usage_complete']},
              'pricing': {'source': 'https://platform.kimi.com/docs/pricing/chat', 'checked': '2026-10-04',
                  'cny_per_million_uncached_input': 6.5, 'cny_per_million_output': 27,
                  'estimated_cny_uncached': summary['estimated_cny_uncached'],
                  'known_reported_subtotal_cny_uncached': summary['known_reported_subtotal_cny_uncached'],
                  'total_cost_unknown': not summary['usage_complete'], 'not_billing_statement': True},
              'limits': LIMITS}
    dump(prefix.with_suffix('.json'), report)
    lines = ['# 多步文件任务：构造实验报告', '',
        f"原设计为三个构造工作流 × 五种生命周期事件 × 四组，共 60 项；本报告计划覆盖 {summary['planned_tasks']} 项任务，"
        f"完成 {summary['completed_tasks']} 项、中断 {summary['interrupted_tasks']} 项、未启动 {summary['unstarted_tasks']} 项。"
        '已记录的模型响应、工具动作、逐轮判分和磁盘工件已重放核对；完整已保存模型输入输出见配套 traces.jsonl。', '',
        '| 组别 | 成功/已完成（本批计划） | 已启动过期上下文 | 有错误写入的任务 | 来源查询 | 模型调用 | 已报告 tokens |',
        '| --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    for arm, value in summary['by_arm'].items():
        lines.append(f"| {arm} | {value['success']}/{value['tasks']}（计划 {value['planned_tasks']}） | {value['stale_returned']} | {value['tasks_with_wrong_writes']} | {value['source_reads']} | {value['model_calls']} | {value['usage']['total_tokens']} |")
    if summary['supplement_reference']:
        reference = summary['supplement_reference']
        lines += ['', f"独立补充 cohort：{reference['cohort_id']}。原实验计划每组 15 项，已完成 "
                  f"{reference['original_completed_tasks']} 项；本批只重新开始原实验没有最终结果的 "
                  f"{summary['planned_tasks']} 项，初始会话和项目文件均重置。当前输入和后端证据已与原实验逐项核对。"
                  '调用间隔预先改为 21 秒，本表只统计本批；原中断任务已答轮次仍属于原批次费用和证据。'
                  '原超时请求用量及收费仍未知，两个批次没有被当作一次连续实验或合并重算成功率。']
    if summary['continuation_reference']:
        reference = summary['continuation_reference']
        lines += ['', f"独立后续 cohort：{reference['cohort_id']}。此前 {len(reference['prior_cohorts'])} 个批次各自重放审计，"
                  f"共保留 {reference['previous_completed_tasks']} 个最终结果（包括失败）；本批仅运行仍无最终结果的 "
                  f"{summary['planned_tasks']} 项。原提示、工具、评分规则及隔离运行代码逐文件核对一致，"
                  '各任务从初始会话和项目文件重新开始，已完成的失败任务未重试。', '',
                  f"此前各批次已报告费用小计之和为 ¥{reference['prior_known_reported_subtotal_cny_uncached']:.4f}，"
                  f"仍有 {len(reference['prior_unanswered_attempts'])} 次历史未答请求用量与收费未知。"
                  '这些费用和中断轮次保留在各自批次，未并入本表的模型调用或任务结果，也未描述为单次连续实验。']
    if not summary['usage_complete']:
        lines += ['', '本报告显式使用 --allow-partial 审计已停止实验。唯一最后请求没有响应，其服务端执行、'
                  'token 用量和收费未知。未完成及未启动任务不计作业务失败；各组已完成分母不同，'
                  '本次部分样本不能直接比较总体成功率。保留中断任务的全部已答轮次，未生成或补造最终结果。']
    lines += ['', '事后诊断（post-hoc diagnostic，观察到失败后增加）：以下区分工件内容正确与执行步骤齐全。'
              '只读工件验收通过但缺少最后修改后的成功公开执行，仍按预先冻结规则计为主任务失败；'
              '这些诊断未重定义上表成功率。缺少执行也可能与其他错误同时发生。', '',
              '| 组别 | 仅工件验收通过/已完成 | 缺少最终成功公开执行 |',
              '| --- | ---: | ---: |']
    for arm, value in summary['by_arm'].items():
        lines.append(f"| {arm} | {value['artifact_only_success']}/{value['tasks']} | {value['missing_final_public_run']} |")
    price_text = (f"总费用未知；仅已报告 token 的未命中缓存价格小计为 ¥{summary['known_reported_subtotal_cny_uncached']:.4f}。"
                  if not summary['usage_complete'] else f"按未命中缓存价格估算 ¥{summary['estimated_cny_uncached']:.4f}。")
    lines += ['', f"共 {summary['requests']} 次 HTTP 尝试、{summary['model_calls']} 个完成响应、"
              f"{summary['rate_limits']} 次已记录的 429、{len(summary['unanswered_attempts'])} 次未答请求。" + price_text +
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
    parser.add_argument('--allow-partial', action='store_true', help='Explicitly audit a stopped run with one final unanswered request')
    args = parser.parse_args()
    print(json.dumps(publish(args.input, args.prefix, allow_partial=args.allow_partial), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
