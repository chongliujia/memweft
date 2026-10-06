"""Synthetic logs exercise complete audit/replay without any API or Mem0 call."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from multistep_fixture import cases, create_fixture, grade
import report_multistep_memory as report
from run_lifecycle_tasks import append, dump, sha, strategy_text
from run_multistep_memory import (ARMS, ROOT, execute_actions, final_grade,
                                 initial_messages, paths_to_freeze)


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def replace_rows(path, values):
    path.write_text(''.join(json.dumps(v, ensure_ascii=False) + '\n' for v in values))


def fixture_logs(output, rate_limited=False, diagnostic_case=False, blocked_without_run=False):
    """Write known action outcomes as test fixtures, never as live experiment data."""
    suite, inputs = cases(), []
    dump(output / 'suite.json', {'cases': suite})
    snapshot_hashes = {}
    for path in paths_to_freeze():
        relative = path.relative_to(ROOT)
        target = output / 'sources' / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
        snapshot_hashes[str(relative)] = sha(target)
    for case in suite:
        for arm in ARMS:
            policy = case['expected']['policy']
            revision = 1 if case['event'] == 'unrelated_update' else 3 if case['event'] == 'delete_recreate' else 2
            source = {} if policy is None else {'policy': {'value': policy, 'revision': revision}}
            active = {'content': strategy_text(case['old']), 'dependencies': {'policy': 1}}
            if arm in ('versions', 'full') and case['event'] != 'unrelated_update':
                active = None
                if case['event'] == 'rollback':
                    active = {'content': strategy_text(case['new']), 'dependencies': {'policy': revision}}
            project = output / 'tasks' / case['id'] / arm / 'project'
            files = create_fixture(project, case)
            row = {'case_id': case['id'], 'arm': arm, 'domain': case['domain'], 'event': case['event'],
                   'files': files, 'active': active, 'current_sources': source,
                   'stale_returned': active is not None and (policy is None or active['dependencies']['policy'] != revision),
                   'initial_file_sha256': {p: sha(project / p) for p in files['readable']}}
            row['messages'] = initial_messages(case, row)
            inputs.append(row)
            append(output / 'inputs.jsonl', row)
    dump(output / 'freeze.json', {'source_sha256': snapshot_hashes, 'native_sha256': 'a' * 64,
        'inputs_sha256': sha(output / 'inputs.jsonl'), 'suite_sha256': sha(output / 'suite.json'),
        'model': 'kimi-k2.6', 'max_rounds': 4, 'max_actions': 3, 'max_completion_tokens': 1024,
        'max_requests': 720, 'max_successful_calls': 240, 'reported_token_stop_threshold': 500000,
        'request_byte_cap': 64000, 'rate_limit_retries': 2, 'rate_limit_cooldown_seconds': 60, 'other_retries': 0})
    by_id = {c['id']: c for c in suite}
    for index, row in enumerate(inputs):
        case = by_id[row['case_id']]
        project = output / 'tasks' / case['id'] / row['arm'] / 'project'
        identity = {f: row[f] for f in ('case_id', 'arm', 'domain', 'event')}
        actions = [
            [{'tool': 'read', 'paths': row['files']['readable']}, {'tool': 'read_source', 'key': 'policy'}],
            [{'tool': 'write', 'path': name, 'value': case['expected']['files'][name]}
             for name in row['files']['writable'] if name in case['expected']['files']] + [{'tool': 'run'}],
        ]
        if blocked_without_run and case['id'] == 'deploy-forget':
            actions[1] = [a for a in actions[1] if a['tool'] != 'run']
        diagnostic = diagnostic_case and index == 0
        if diagnostic:
            actions[0].append({'tool': 'unsupported'})
            actions.extend([[], []])
        messages, executions = list(row['messages']), []
        for number, action in enumerate(actions):
            turn = {**identity, 'round': number}
            retry = int(rate_limited and index == 0 and number == 0)
            if retry:
                append(output / 'attempts.jsonl', {**turn, 'attempt': 0, 'started_at': 'synthetic'})
                append(output / 'rate_limits.jsonl', {**turn, 'attempt': 0, 'error': 'Kimi HTTP 429: synthetic'})
            append(output / 'attempts.jsonl', {**turn, 'attempt': retry, 'started_at': 'synthetic'})
            content = '{broken' if diagnostic and number == 2 else json.dumps(
                {'actions': action, 'done': not diagnostic and number == len(actions) - 1})
            request = {'model': 'kimi-k2.6', 'messages': messages, 'stream': False,
                       'thinking': {'type': 'disabled'}, 'max_completion_tokens': 1024,
                       'response_format': {'type': 'json_object'}}
            response = {'request': request, 'model': 'kimi-k2.6', 'content': content,
                        'finish_reason': 'stop', 'response_id': 'synthetic', 'latency_ms': 1.0,
                        'usage': {'prompt_tokens': 10, 'completion_tokens': 10, 'total_tokens': 20}}
            append(output / 'responses.jsonl', {**turn, 'attempt': retry, 'response': response})
            execution = execute_actions(project, case, row, content)
            executions.append(execution)
            append(output / 'turns.jsonl', {**turn, 'execution': execution, 'grade': grade(project, case)})
            messages.extend([{'role': 'assistant', 'content': content}, {'role': 'user',
                'content': json.dumps({'tool_results': execution['results'], 'rounds_remaining': 3 - number},
                                      ensure_ascii=False, sort_keys=True)}])
        append(output / 'results.jsonl', {**identity, 'rounds': len(actions), **final_grade(project, case, executions)})
    calls = len(rows(output / 'responses.jsonl'))
    dump(output / 'status.json', {'status': 'completed', 'calls': calls,
                                 'requests': calls + int(rate_limited), 'tokens': calls * 20})


def truncate_after_unanswered_request(output, completed_count=1):
    """Preserve one completed task, one answered partial turn, then a timeout."""
    inputs = rows(output / 'inputs.jsonl')
    completed, pending = inputs[:completed_count], inputs[completed_count]
    task_key = lambda r: (r['case_id'], r['arm'])
    finished = {task_key(r) for r in completed}
    keep = lambda r: task_key(r) in finished or task_key(r) == task_key(pending) and r['round'] == 0
    for name in ('responses', 'turns', 'attempts'):
        path = output / (name + '.jsonl')
        replace_rows(path, [r for r in rows(path) if keep(r)])
    append(output / 'attempts.jsonl', {**{f: pending[f] for f in ('case_id', 'arm', 'domain', 'event')},
        'round': 1, 'attempt': 0, 'started_at': 'synthetic-unanswered'})
    replace_rows(output / 'results.jsonl', rows(output / 'results.jsonl')[:completed_count])
    by_id = {c['id']: c for c in cases()}
    for row in inputs[completed_count:]:
        project = output / 'tasks' / row['case_id'] / row['arm'] / 'project'
        shutil.rmtree(project)
        create_fixture(project, by_id[row['case_id']])
        if task_key(row) == task_key(pending):
            response = next(r['response'] for r in rows(output / 'responses.jsonl') if task_key(r) == task_key(row))
            execute_actions(project, by_id[row['case_id']], row, response['content'], response['finish_reason'])
    calls = len(rows(output / 'responses.jsonl'))
    dump(output / 'status.json', {'status': 'stopped_error', 'calls': calls, 'requests': calls + 1, 'tokens': calls * 20})
    append(output / 'errors.jsonl', {'error': 'Kimi connection failed or timed out; no automatic retry was made.',
                                   'at': 'synthetic-stop'})


def fixture_supplement(original, output):
    """Synthetic separate cohort whose selection is anchored to the old logs."""
    fixture_logs(output)
    original_inputs = rows(original / 'inputs.jsonl')
    completed = [{k: r[k] for k in ('case_id', 'arm')} for r in rows(original / 'results.jsonl')]
    keys = {(r['case_id'], r['arm']) for r in completed}
    selected = [r for r in original_inputs if (r['case_id'], r['arm']) not in keys]
    selected_keys = {(r['case_id'], r['arm']) for r in selected}
    for name in ('inputs', 'results', 'attempts', 'responses', 'turns'):
        path = output / (name + '.jsonl')
        replace_rows(path, [r for r in rows(path) if (r['case_id'], r['arm']) in selected_keys])
    for row in selected:
        relative = Path('tasks') / row['case_id'] / row['arm']
        for child in (original / relative).iterdir():
            if child.name == 'project':
                continue
            if child.is_dir():
                shutil.copytree(child, output / relative / child.name)
            else:
                shutil.copy2(child, output / relative / child.name)
    if not (original / 'rate_limits.jsonl').exists():
        (original / 'rate_limits.jsonl').write_text('')
    preparation = output / 'preparation/test_prepare.py'
    preparation.parent.mkdir()
    preparation.write_text('# Synthetic preparation fixture; no API calls.\n')
    dump(output / 'supplement-accounting.json', {'billing_complete': False})
    supplement = {'cohort_id': 'synthetic-supplement', 'original_run': original.name,
        'original_completed_tasks': completed,
        'selected_tasks': [{k: r[k] for k in ('case_id', 'arm')} for r in selected],
        'original_unanswered_attempts': rows(original / 'attempts.jsonl')[-1:],
        'context_restarted': True, 'retained_original_partial_responses': True,
        'initial_messages_byte_equivalent': True, 'backend_preparation_copied_unchanged': True,
        'fixture_recreated_from_frozen_create_function': True,
        'preparation_script': 'preparation/test_prepare.py', 'preparation_script_sha256': sha(preparation),
        'accounting_file': 'supplement-accounting.json'}
    for name, suffix in [('freeze', '.json'), ('inputs', '.jsonl'), ('results', '.jsonl'),
                         ('status', '.json'), ('attempts', '.jsonl'), ('responses', '.jsonl'),
                         ('turns', '.jsonl'), ('rate_limits', '.jsonl'), ('errors', '.jsonl')]:
        supplement['original_' + name + '_sha256'] = sha(original / (name + suffix))
    freeze = json.loads((output / 'freeze.json').read_text())
    freeze.update(supplement=supplement, inputs_sha256=sha(output / 'inputs.jsonl'),
                  interval_seconds=21, max_requests=len(selected) * 12, max_successful_calls=len(selected) * 4)
    dump(output / 'freeze.json', freeze)
    count = len(rows(output / 'responses.jsonl'))
    dump(output / 'status.json', {'status': 'completed', 'calls': count, 'requests': count, 'tokens': count * 20})


def fixture_continuation(original, supplement, output):
    """Third synthetic cohort keeps all prior finals, including failed ones."""
    output.mkdir()
    fixture_logs(output)
    prior_paths = [original, supplement]
    completed = [{k: row[k] for k in ('case_id', 'arm')} for prior in prior_paths for row in rows(prior / 'results.jsonl')]
    completed_keys = {(row['case_id'], row['arm']) for row in completed}
    selected = [r for r in rows(original / 'inputs.jsonl') if (r['case_id'], r['arm']) not in completed_keys]
    selected_keys = {(r['case_id'], r['arm']) for r in selected}
    for name in ('inputs', 'results', 'attempts', 'responses', 'turns'):
        path = output / (name + '.jsonl')
        replace_rows(path, [r for r in rows(path) if (r['case_id'], r['arm']) in selected_keys])
    script = output / 'preparation/test_continuation.py'
    script.parent.mkdir()
    script.write_text('# Synthetic continuation preparation.\n')
    dump(output / 'continuation-accounting.json', {'prior_total_cost_unknown': True})
    original_freeze = json.loads((original / 'freeze.json').read_text())
    shutil.copytree(original / 'sources', output / 'isolated-runtime')
    support = output / 'isolated-runtime/python/src/memweft/support_fixture.py'
    support.write_text('# Synthetic support fixture.\n')
    launcher = output / 'run_frozen.py'
    launcher.write_text('# Synthetic launch fixture.\n')
    freeze = json.loads((output / 'freeze.json').read_text())
    freeze.update(inputs_sha256=sha(output / 'inputs.jsonl'), interval_seconds=21,
                  max_successful_calls=len(selected) * 4, max_requests=len(selected) * 12,
                  continuation={'cohort_id': 'synthetic-third-cohort', 'original_run': original.name,
                      'root_freeze_sha256': sha(original / 'freeze.json'),
                      'prior_cohorts': [{'run': p.name, 'evidence_sha256':
                          {n: sha(p / n) if (p / n).exists() else None for n in report.COHORT_EVIDENCE}} for p in prior_paths],
                      'previous_completed_tasks': completed,
                      'selected_tasks': [{k: r[k] for k in ('case_id', 'arm')} for r in selected],
                      'context_restarted': True, 'retained_prior_partial_responses': True,
                      'initial_messages_byte_equivalent': True, 'backend_preparation_copied_unchanged': True,
                      'fixture_recreated_from_frozen_create_function': True,
                      'preparation_script': script.relative_to(output).as_posix(), 'preparation_script_sha256': sha(script),
                      'accounting_file': 'continuation-accounting.json', 'runtime_root': 'isolated-runtime',
                      'runtime_source_sha256': original_freeze['source_sha256'],
                      'runtime_support_sha256': {'support_fixture.py': sha(support)},
                      'launcher': 'run_frozen.py', 'launcher_sha256': sha(launcher)})
    dump(output / 'freeze.json', freeze)
    count = len(rows(output / 'responses.jsonl'))
    dump(output / 'status.json', {'status': 'completed', 'calls': count, 'requests': count, 'tokens': count * 20})


class MultistepReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        fixture_logs(self.root)

    def test_complete_replay_and_publish_include_all_requests_and_artifacts(self):
        result = report.publish(self.root, self.root / 'report')
        self.assertEqual(result['replayed_tasks'], 60)
        self.assertEqual(result['replayed_turns'], 120)
        evidence = json.loads((self.root / 'report.json').read_text())
        self.assertEqual(evidence['summary']['usage']['total_tokens'], 2400)
        self.assertEqual(evidence['trace_sha256'], sha(self.root / 'report.traces.jsonl'))
        self.assertTrue(all(group['success'] == 15 for group in evidence['summary']['by_arm'].values()))
        traces = rows(self.root / 'report.traces.jsonl')
        self.assertEqual(len(traces), 60)
        self.assertIn('request', traces[0]['responses'][0]['response'])
        self.assertIn('expected', traces[0]['case'])
        self.assertTrue(traces[0]['artifact_sha256'])

    def test_saved_source_manifest_not_current_native_digest_is_audited(self):
        with patch('run_multistep_memory.verify_freeze', side_effect=AssertionError('must not use current native')):
            self.assertEqual(report.audit(self.root)['completed_tasks'], 60)
        target = self.root / 'sources/evals/multistep_fixture.py'
        target.write_text(target.read_text() + '\n# changed\n')
        with self.assertRaisesRegex(ValueError, 'snapshot changed'):
            report.audit(self.root)

    def test_windows_separator_manifest_audits_without_rewriting_evidence(self):
        freeze_path = self.root / 'freeze.json'
        freeze = json.loads(freeze_path.read_text())
        freeze['source_sha256'] = {key.replace('/', '\\'): value
                                   for key, value in freeze['source_sha256'].items()}
        dump(freeze_path, freeze)
        before_manifest = freeze_path.read_bytes()
        before_sources = report._inventory(self.root / 'sources')
        self.assertEqual(report.audit(self.root)['completed_tasks'], 60)
        self.assertEqual(freeze_path.read_bytes(), before_manifest)
        self.assertEqual(report._inventory(self.root / 'sources'), before_sources)

    def test_partial_is_explicit_and_never_counts_unfinished_as_failure(self):
        truncate_after_unanswered_request(self.root)
        with self.assertRaisesRegex(ValueError, 'error-stopped'):
            report.audit(self.root)
        report.publish(self.root, self.root / 'partial', allow_partial=True)
        evidence = json.loads((self.root / 'partial.json').read_text())
        summary = evidence['summary']
        self.assertEqual(summary['completed_tasks'], 1)
        self.assertEqual(summary['interrupted_tasks'], 1)
        self.assertEqual(summary['unstarted_tasks'], 58)
        self.assertEqual(summary['model_calls'], 3)
        self.assertEqual(summary['requests'], 4)
        self.assertEqual(summary['by_arm']['plain']['tasks'], 1)
        self.assertEqual(summary['by_arm']['plain']['success'], 1)
        self.assertEqual(summary['by_arm']['versions']['tasks'], 0)
        self.assertEqual(summary['by_arm']['versions']['planned_tasks'], 15)
        self.assertEqual(summary['paired_success']['plain_vs_full']['pairs'], 0)
        self.assertIsNone(summary['estimated_cny_uncached'])
        self.assertGreater(summary['known_reported_subtotal_cny_uncached'], 0)
        self.assertTrue(evidence['pricing']['total_cost_unknown'])
        traces = rows(self.root / 'partial.traces.jsonl')
        self.assertEqual(sum(t['result'] is None for t in traces), 59)
        self.assertEqual(traces[1]['task_status'], 'interrupted')
        self.assertEqual(len(traces[1]['responses']), 1)
        self.assertEqual(len(traces[1]['attempts']), 2)
        self.assertIn('总费用未知', (self.root / 'partial.md').read_text())
        self.assertIn('1/1（计划 15）', (self.root / 'partial.md').read_text())

    def test_partial_requires_unique_final_unanswered_request(self):
        truncate_after_unanswered_request(self.root)
        path = self.root / 'attempts.jsonl'
        original = rows(path)
        replace_rows(path, original[:-1])
        with self.assertRaisesRegex(ValueError, 'one final unanswered'):
            report.audit(self.root, allow_partial=True)
        replace_rows(path, original + [{**original[-1], 'round': 2}])
        with self.assertRaisesRegex(ValueError, 'one final unanswered'):
            report.audit(self.root, allow_partial=True)

    def test_partial_still_replays_interrupted_turn_and_untouched_suffix(self):
        truncate_after_unanswered_request(self.root)
        path = self.root / 'turns.jsonl'
        original = rows(path)
        changed = rows(path)
        changed[-1]['execution']['done'] = True
        replace_rows(path, changed)
        with self.assertRaisesRegex(ValueError, 'Tool execution'):
            report.audit(self.root, allow_partial=True)
        replace_rows(path, original)
        unstarted = rows(self.root / 'inputs.jsonl')[2]
        stray = self.root / 'tasks' / unstarted['case_id'] / unstarted['arm'] / 'project/extra.txt'
        stray.write_text('unexpected file in unstarted fixture')
        with self.assertRaisesRegex(ValueError, 'artifact bytes'):
            report.audit(self.root, allow_partial=True)

    def test_partial_refuses_result_selection_and_post_budget_unanswered_call(self):
        truncate_after_unanswered_request(self.root)
        path = self.root / 'results.jsonl'
        original = rows(path)
        replace_rows(path, [{**original[0], 'arm': 'versions'}])
        with self.assertRaisesRegex(ValueError, 'completed prefix'):
            report.audit(self.root, allow_partial=True)
        replace_rows(path, original)
        response_path = self.root / 'responses.jsonl'
        values = rows(response_path)
        values[-1]['response']['usage'] = {'prompt_tokens': 499990, 'completion_tokens': 10, 'total_tokens': 500000}
        replace_rows(response_path, values)
        with self.assertRaisesRegex(ValueError, 'Unanswered request.*budget'):
            report.audit(self.root, allow_partial=True)

    def test_supplement_is_a_separate_audited_cohort_with_original_unknown_cost(self):
        truncate_after_unanswered_request(self.root)
        with tempfile.TemporaryDirectory() as temp, patch.object(report, 'ROOT', self.root.parent):
            output = Path(temp)
            fixture_supplement(self.root, output)
            report.publish(output, output / 'supplement')
            summary = json.loads((output / 'supplement.json').read_text())['summary']
            self.assertEqual(summary['cohort_kind'], 'supplement')
            self.assertEqual(summary['planned_tasks'], 59)
            self.assertEqual(summary['completed_tasks'], 59)
            self.assertEqual(summary['by_arm']['plain']['tasks'], 14)
            self.assertEqual(summary['by_arm']['plain']['original_planned_tasks'], 15)
            self.assertEqual(summary['supplement_reference']['original_completed_tasks'], 1)
            self.assertTrue(summary['supplement_reference']['original_total_cost_unknown'])
            self.assertFalse(summary['supplement_reference']['combined_cohorts'])
            self.assertTrue(summary['usage_complete'])
            self.assertEqual(summary['model_calls'], 118)
            self.assertIn('独立补充 cohort', (output / 'supplement.md').read_text())

    def test_supplement_rejects_backend_tampering_and_result_based_reselection(self):
        truncate_after_unanswered_request(self.root)
        original_input = rows(self.root / 'inputs.jsonl')[1]
        relative = Path('tasks') / original_input['case_id'] / original_input['arm'] / 'backend.json'
        (self.root / relative).write_text('frozen backend fixture')
        with tempfile.TemporaryDirectory() as temp, patch.object(report, 'ROOT', self.root.parent):
            output = Path(temp)
            fixture_supplement(self.root, output)
            summary = report.audit(output)
            self.assertEqual(summary['supplement_reference']['backend_evidence_files'], 1)
            (output / relative).write_text('modified backend fixture')
            with self.assertRaisesRegex(ValueError, 'backend preparation evidence'):
                report.audit(output)
            (output / relative).write_bytes((self.root / relative).read_bytes())
            freeze = json.loads((output / 'freeze.json').read_text())
            freeze['supplement']['selected_tasks'].pop()
            freeze['max_requests'] -= 12
            freeze['max_successful_calls'] -= 4
            dump(output / 'freeze.json', freeze)
            with self.assertRaisesRegex(ValueError, 'all and only original unfinished'):
                report.audit(output)

    def continuation_fixture(self):
        original, supplement, continuation = [self.root / name for name in ('previous', 'prior-supplement', 'third')]
        original.mkdir(); supplement.mkdir()
        fixture_logs(original, blocked_without_run=True)
        truncate_after_unanswered_request(original, completed_count=5)
        fixture_supplement(original, supplement)
        truncate_after_unanswered_request(supplement)
        fixture_continuation(original, supplement, continuation)
        return original, supplement, continuation

    def test_continuation_preserves_failed_finals_and_each_prior_unknown_cost(self):
        original, supplement, continuation = self.continuation_fixture()
        with patch.object(report, 'ROOT', self.root):
            report.publish(continuation, continuation / 'third-report')
        summary = json.loads((continuation / 'third-report.json').read_text())['summary']
        self.assertEqual(summary['cohort_kind'], 'continuation')
        self.assertEqual(summary['completed_tasks'], 54)
        self.assertEqual(summary['model_calls'], 108)
        reference = summary['continuation_reference']
        self.assertEqual(reference['previous_completed_tasks'], 6)
        self.assertEqual(len(reference['prior_unanswered_attempts']), 2)
        self.assertEqual(reference['prior_known_reported_usage']['total_tokens'], 280)
        self.assertFalse(reference['prior_usage_complete'])
        self.assertFalse(reference['combined_cohorts'])
        self.assertTrue(summary['usage_complete'])
        self.assertFalse(rows(original / 'results.jsonl')[-1]['task_success'])
        self.assertNotIn(('deploy-forget', 'plain'), {(r['case_id'], r['arm']) for r in rows(continuation / 'inputs.jsonl')})
        self.assertIn('独立后续 cohort', (continuation / 'third-report.md').read_text())

    def test_continuation_rejects_duplicate_finals_and_runtime_tampering(self):
        original, supplement, continuation = self.continuation_fixture()
        frozen_path = continuation / 'freeze.json'
        frozen = json.loads(frozen_path.read_text())
        runtime_file = continuation / 'isolated-runtime/evals/multistep_kimi.py'
        before = runtime_file.read_bytes()
        runtime_file.write_bytes(before + b'\n# tampered\n')
        with patch.object(report, 'ROOT', self.root):
            with self.assertRaisesRegex(ValueError, 'Isolated continuation runtime changed'):
                report.audit(continuation)
            runtime_file.write_bytes(before)
            for relative, pattern in [('run_frozen.py', 'launcher changed'),
                                      ('isolated-runtime/python/src/memweft/support_fixture.py', 'SDK support changed')]:
                target = continuation / relative
                saved = target.read_bytes()
                target.write_bytes(saved + b'# tampered\n')
                with self.assertRaisesRegex(ValueError, pattern):
                    report.audit(continuation)
                target.write_bytes(saved)
            records = rows(supplement / 'results.jsonl')
            records[0] = rows(original / 'results.jsonl')[0]
            replace_rows(supplement / 'results.jsonl', records)
            frozen['continuation']['prior_cohorts'][1]['evidence_sha256']['results.jsonl'] = sha(supplement / 'results.jsonl')
            dump(frozen_path, frozen)
            with self.assertRaisesRegex(ValueError, 'Duplicate final result across prior cohorts'):
                report.audit(continuation)

    def test_snapshot_rejects_unsafe_platform_paths(self):
        freeze_path = self.root / 'freeze.json'
        original = json.loads(freeze_path.read_text())
        unsafe = (r'C:\outside.py', r'C:relative.py', '/tmp/outside.py', r'\rooted.py',
                  r'\\server\share\outside.py', r'evals\..\outside.py', 'evals/../outside.py',
                  'evals/file.py:stream', 'evals/file.py.', 'evals/file.py ')
        for name in unsafe:
            with self.subTest(name=name):
                freeze = {**original, 'source_sha256': {**original['source_sha256'], name: 'a' * 64}}
                dump(freeze_path, freeze)
                with self.assertRaisesRegex(ValueError, 'Unsafe snapshot path'):
                    report._snapshot(self.root)

    def test_snapshot_rejects_normalized_path_aliases(self):
        freeze_path = self.root / 'freeze.json'
        original = json.loads(freeze_path.read_text())
        normalized = {key.replace('\\', '/'): value for key, value in original['source_sha256'].items()}
        name = 'evals/multistep_fixture.py'
        for alias in (name.replace('/', '\\'), 'evals/./multistep_fixture.py',
                      'evals//multistep_fixture.py', 'EVALS/multistep_fixture.py'):
            with self.subTest(alias=alias):
                freeze = {**original, 'source_sha256': {**normalized, alias: normalized[name]}}
                dump(freeze_path, freeze)
                with self.assertRaisesRegex(ValueError, 'Duplicate normalized snapshot path'):
                    report._snapshot(self.root)

    def test_missing_or_duplicate_accounting_rejected(self):
        path = self.root / 'attempts.jsonl'
        original = rows(path)
        replace_rows(path, original[1:])
        with self.assertRaisesRegex(ValueError, 'Request accounting'):
            report.audit(self.root)
        replace_rows(path, original + original[:1])
        with self.assertRaisesRegex(ValueError, 'Duplicate request'):
            report.audit(self.root)

    def test_missing_task_rejected(self):
        path = self.root / 'results.jsonl'
        replace_rows(path, rows(path)[:-1])
        with self.assertRaisesRegex(ValueError, 'coverage'):
            report.audit(self.root)

    def test_bad_usage_rejected(self):
        path = self.root / 'responses.jsonl'
        values = rows(path)
        values[0]['response']['usage']['total_tokens'] = -1
        replace_rows(path, values)
        with self.assertRaisesRegex(ValueError, 'usage accounting'):
            report.audit(self.root)

    def test_later_request_cannot_hide_oracle_or_alter_history(self):
        path = self.root / 'responses.jsonl'
        values = rows(path)
        values[1]['response']['request']['messages'][-1]['content'] += ' hidden correct answer'
        replace_rows(path, values)
        with self.assertRaisesRegex(ValueError, 'message history'):
            report.audit(self.root)

    def test_tool_result_and_final_artifact_tampering_rejected(self):
        path = self.root / 'turns.jsonl'
        original = rows(path)
        changed = rows(path)
        changed[0]['execution']['results'][0]['result']['README.txt'] += '\nchanged'
        replace_rows(path, changed)
        with self.assertRaisesRegex(ValueError, 'Tool execution'):
            report.audit(self.root)
        replace_rows(path, original)
        artifact = self.root / 'tasks/deploy-update/plain/project/service.json'
        artifact.write_text('{"tampered":true}')
        with self.assertRaisesRegex(ValueError, 'On-disk final grade'):
            report.audit(self.root)

    def test_fifth_round_rejected(self):
        path = self.root / 'results.jsonl'
        values = rows(path)
        values[0]['rounds'] = 5
        replace_rows(path, values)
        with self.assertRaisesRegex(ValueError, 'number of rounds'):
            report.audit(self.root)

    def test_recorded_429_has_exactly_one_matching_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            fixture_logs(output, rate_limited=True)
            summary = report.audit(output)
            self.assertEqual(summary['rate_limits'], 1)
            self.assertEqual(summary['requests'], summary['model_calls'] + 1)
            records = rows(output / 'rate_limits.jsonl')
            records[0]['error'] = 'Kimi HTTP 500: synthetic'
            replace_rows(output / 'rate_limits.jsonl', records)
            with self.assertRaisesRegex(ValueError, 'Non-429'):
                report.audit(output)

    def test_published_rate_limit_details_redacted_without_dropping_events(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            fixture_logs(output, rate_limited=True)
            rate_path = output / 'rate_limits.jsonl'
            records = rows(rate_path)
            records[0]['error'] = 'Kimi HTTP 429: org-synthetic-123 ak-synthetic-456 maximum RPM 3'
            replace_rows(rate_path, records)
            original_hash = sha(rate_path)
            report.publish(output, output / 'public')
            self.assertEqual(sha(rate_path), original_hash)
            trace_text = (output / 'public.traces.jsonl').read_text()
            report_text = (output / 'public.json').read_text()
            for identifier in ('org-synthetic-123', 'ak-synthetic-456'):
                self.assertNotIn(identifier, trace_text)
                self.assertNotIn(identifier, report_text)
            published = [r for trace in rows(output / 'public.traces.jsonl') for r in trace['rate_limits']]
            self.assertEqual(published, [{**records[0], 'error': report.PUBLISHED_RATE_LIMIT}])
            evidence = json.loads(report_text)
            self.assertEqual(evidence['summary']['rate_limits'], 1)
            self.assertEqual(evidence['evidence_sha256']['rate_limits.jsonl'], original_hash)
            self.assertTrue(evidence['publication_redactions']['all_rate_limit_events_retained'])

    def test_action_errors_and_round_limit_are_distinct_from_task_success(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            fixture_logs(output, diagnostic_case=True)
            summary = report.audit(output)
            plain = summary['by_arm']['plain']
            self.assertEqual(plain['tool_action_errors'], 2)
            self.assertEqual(plain['protocol_errors'], 1)
            self.assertEqual(plain['round_limit_without_done'], 1)
            self.assertEqual(plain['success'], 15)
            self.assertEqual(summary['by_arm']['full']['tool_action_errors'], 0)
            self.assertEqual(summary['by_arm']['full']['round_limit_without_done'], 0)
            self.assertIn('synthetic error', summary['metric_definitions']['tool_action_errors'])

    def test_blocked_artifact_without_run_stays_primary_failure_in_all_arms(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            fixture_logs(output, blocked_without_run=True)
            report.publish(output, output / 'diagnostic')
            evidence = json.loads((output / 'diagnostic.json').read_text())
            summary = evidence['summary']
            for arm in ARMS:
                with self.subTest(arm=arm):
                    group = summary['by_arm'][arm]
                    self.assertEqual(group['success'], 14)
                    self.assertEqual(group['artifact_only_success'], 15)
                    self.assertEqual(group['missing_final_public_run'], 1)
                    result = next(r for r in summary['case_results'] if r['case_id'] == 'deploy-forget' and r['arm'] == arm)
                    self.assertFalse(result['task_success'])
                    self.assertEqual(result['errors'], ['Successful public execution required after final file modification'])
                    self.assertEqual(json.loads((output / 'tasks/deploy-forget' / arm / 'project/blocked.json').read_text()),
                                     {'status': 'blocked', 'reason': 'source_unavailable'})
            self.assertTrue(summary['post_hoc_diagnostics']['introduced_after_observing_failures'])
            self.assertTrue(summary['post_hoc_diagnostics']['primary_success_definition_unchanged'])
            self.assertIn('post-hoc diagnostic', (output / 'diagnostic.md').read_text())


if __name__ == '__main__':
    unittest.main()
