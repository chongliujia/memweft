"""Synthetic logs exercise complete audit/replay without any API or Mem0 call."""
import json
from pathlib import Path
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


def fixture_logs(output, rate_limited=False, diagnostic_case=False):
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


if __name__ == '__main__':
    unittest.main()
