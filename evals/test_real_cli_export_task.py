"""Policy boundaries plus local fixed-wheel integration for real CLI continuation."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import real_cli_export_task as task


class RealCliUnitTests(unittest.TestCase):
    def test_plan_never_accepts_command_paths_or_duplicate_keys(self):
        good = {'project': 'p', 'user': 'u', 'keys': ['a', 'b'], 'include_sources': True}
        self.assertEqual(task._plan(good), good)
        for change in ({'project': '../escape'}, {'keys': ['b', 'a']}, {'keys': ['a', 'a']},
                       {'keys': ['--db']}, {'include_sources': 1}, {'keys': []}):
            with self.assertRaises(ValueError):
                task._plan({**good, **change})
        with self.assertRaises(ValueError):
            task._plan({**good, 'command': 'set'})

    def test_possible_credentials_stop_before_exposure(self):
        task._scan_public({'key': 'release.channel', 'value': 'Public developer preview'})
        with self.assertRaisesRegex(ValueError, 'Possible credential'):
            task._scan_public({'text': 'sk-' + 'test' * 10})
        with self.assertRaisesRegex(ValueError, 'Possible credential'):
            task._scan_public({'nested': [{'password': 'do-not-expose'}]})

    def test_protocol_rejects_truncation_and_extra_top_fields(self):
        for body, finish in [({'actions': [], 'done': True}, 'length'),
                             ({'actions': [], 'done': True, 'command': 'bad'}, 'stop')]:
            result = task.execute_actions(Path('/nonexistent'), json.dumps(body), finish)
            self.assertTrue(result['protocol_error'])
            self.assertFalse(result['done'])

    def test_payload_budget_includes_transport_fields(self):
        messages = [{'role': 'user', 'content': 'x' * 63900}]
        self.assertLess(len(json.dumps(messages, ensure_ascii=False).encode()), task.REQUEST_BYTE_CAP)
        payload = task.request_payload(messages)
        self.assertGreater(len(json.dumps(payload, ensure_ascii=False).encode()), task.REQUEST_BYTE_CAP)
        self.assertEqual(payload['thinking'], {'type': 'disabled'})
        self.assertEqual(payload['max_completion_tokens'], 1024)
        self.assertEqual(payload['response_format'], {'type': 'json_object'})


@unittest.skipUnless(task.PYTHON.exists() and task.SOURCE.exists() and task.WHEEL.exists(),
                     'Requires archived real project and fixed macOS arm64 SDK; not a portable CI fixture')
class RealCliLocalIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / 'real-task'
        task.prepare(self.output)

    def discovery(self):
        result = task.smoke(self.output)
        self.assertTrue(result['ok'])
        return result['cli']

    def test_actual_existing_cli_export_and_cross_process_preservation(self):
        listing = self.discovery()
        plan = {**listing['scope'], 'keys': [r['key'] for r in listing['decisions']], 'include_sources': True}
        result = task.execute_actions(self.output, json.dumps({'actions': [
            {'tool': 'write', 'path': 'export-plan.json', 'value': plan}, {'tool': 'run'}], 'done': True}))
        self.assertTrue(result['results'][-1]['result']['ok'])
        self.assertTrue(task.grade(self.output)['task_success'])
        receipt = task.load(next((self.output / 'cli-runs').glob('*.json')))
        self.assertEqual(len(receipt['commands']), 5)
        self.assertTrue(all(c['returncode'] == 0 for c in receipt['commands']))
        self.assertEqual(task.load(self.output / 'work/handoff.json')['records'], listing['decisions'])
        task.verify_freeze(self.output)

    def test_omitted_sources_are_not_repaired_and_oracle_fails(self):
        listing = self.discovery()
        task.dump(self.output / 'work/export-plan.json', {**listing['scope'],
            'keys': [r['key'] for r in listing['decisions']], 'include_sources': False})
        self.assertTrue(task.smoke(self.output)['ok'])
        self.assertFalse(task.grade(self.output)['task_success'])
        self.assertTrue(all('source' not in r for r in task.load(self.output / 'work/handoff.json')['records']))

    def test_model_cannot_read_oracle_or_write_ledger(self):
        result = task.execute_actions(self.output, json.dumps({'actions': [
            {'tool': 'read', 'paths': ['../oracle.json']},
            {'tool': 'write', 'path': 'data/project-decisions.db', 'value': {}},
            {'tool': 'run', 'command': 'forget'}], 'done': False}))
        self.assertEqual(sum('error' in r for r in result['results']), 3)
        self.assertFalse(task.grade(self.output)['task_success'])
        task.verify_freeze(self.output)

    def test_injected_client_run_logs_and_refuses_repeat(self):
        class OfflineClient:
            calls = 0
            def complete(client, messages, max_tokens):
                client.calls += 1
                if client.calls == 1:
                    actions = [{'tool': 'read', 'paths': ['README.txt', 'inputs/entry.json']}, {'tool': 'run'}]
                else:
                    listing = json.loads(messages[-1]['content'])['tool_results'][-1]['result']['cli']
                    plan = {**listing['scope'], 'keys': [r['key'] for r in listing['decisions']], 'include_sources': True}
                    actions = [{'tool': 'write', 'path': 'export-plan.json', 'value': plan}, {'tool': 'run'}]
                return {'model': task.MODEL, 'content': json.dumps({'actions': actions, 'done': client.calls == 2}),
                        'request': task.request_payload(messages, max_tokens), 'finish_reason': 'stop',
                        'usage': {'prompt_tokens': 5, 'completion_tokens': 3, 'total_tokens': 8}}
        client = OfflineClient()
        with patch.object(task.time, 'sleep') as sleep, patch.object(task.time, 'monotonic', side_effect=[100.0, 102.0, 121.0]):
            result = task.run(self.output, client)
        sleep.assert_called_once_with(19.0)
        self.assertTrue(result['task_success'])
        self.assertEqual(result['model_calls'], 2)
        self.assertEqual(result['usage'], {'prompt_tokens': 10, 'completion_tokens': 6, 'total_tokens': 16})
        for name in ('attempts', 'responses', 'turns'):
            self.assertEqual(len((self.output / (name + '.jsonl')).read_text().splitlines()), 2)
        attempts = [json.loads(line) for line in (self.output / 'attempts.jsonl').read_text().splitlines()]
        self.assertTrue(all(a['started_at'] and a['request_bytes'] > 0 for a in attempts))
        with self.assertRaisesRegex(ValueError, 'Existing attempt/plan'):
            task.run(self.output, client)
        self.assertEqual(client.calls, 2)

    def test_dependency_snapshot_tamper_is_rejected(self):
        freeze = task.load(self.output / 'freeze.json')
        self.assertEqual(freeze['interval_seconds'], 21)
        self.assertEqual(set(freeze['source_sha256']), set(task.FROZEN_SOURCES))
        snapshot = self.output / 'frozen-sources/evals/output_contract.py'
        snapshot.write_text(snapshot.read_text() + '\n# changed\n')
        with self.assertRaisesRegex(ValueError, 'Frozen source changed'):
            task.verify_freeze(self.output)


if __name__ == '__main__':
    unittest.main()
