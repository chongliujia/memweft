"""Behavioral checks for the pilot: real SDK, independent oracle and accounting."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import run_lifecycle_tasks as pilot


class LifecyclePilotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.suite = json.loads(pilot.SUITE.read_text(encoding='utf-8'))

    def test_complete_event_matrix_real_sdk_and_equal_source_inputs(self):
        pilot.validate_suite(self.suite)
        with tempfile.TemporaryDirectory() as temp:
            for case in self.suite['cases']:
                by_arm = {}
                for arm in pilot.ARMS:
                    row = pilot.prepare_case(Path(temp) / (case['id'] + arm + '.db'), case, arm)
                    by_arm[arm] = row
                    changed = case['event'] != 'unrelated_update'
                    self.assertEqual(row['stale_returned'], changed and arm == 'plain', (case['id'], arm))
                    self.assertEqual(row['resident_stale_versions'] > 0, changed and arm != 'full')
                    self.assertEqual(row['blocked_lifecycle_operation'], arm != 'plain' and case['event'] in ('concurrent_update', 'rollback'))
                    if not changed:
                        self.assertIsNotNone(row['active'])
                        self.assertEqual(set(row['active']['dependencies']), {'policy', 'format'})
                    if case['event'] == 'delete_recreate':
                        self.assertGreater(row['current_sources']['policy']['revision'], 2)
                self.assertEqual(by_arm['plain']['current_sources'], by_arm['full']['current_sources'])
                self.assertEqual(by_arm['versions']['messages'], by_arm['full']['messages'])

    def test_oracle_not_used_to_construct_inputs(self):
        original = self.suite['cases'][0]
        changed = deepcopy(original)
        changed['expected'] = {'canary': 'must-never-appear'}
        with tempfile.TemporaryDirectory() as temp:
            a = pilot.prepare_case(Path(temp) / 'a.db', original, 'full')
            b = pilot.prepare_case(Path(temp) / 'b.db', changed, 'full')
            self.assertEqual(a['messages'], b['messages'])
            self.assertNotIn('canary', json.dumps(b['messages']))

    def test_guard_block_is_failure_and_valid_output_creates_real_artifact(self):
        case = self.suite['cases'][0]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            row = pilot.prepare_case(root / 'memory.db', case, 'plain')
            stale = {'content': json.dumps({'action': 'write', **case['old']}), 'finish_reason': 'stop'}
            grade = pilot.execute_and_grade(case, row, stale, root / 'bad')
            self.assertTrue(grade['stale_proposal'])
            self.assertTrue(grade['execution_blocked'])
            self.assertFalse(grade['task_success'])
            self.assertEqual(list((root / 'bad').iterdir()), [])
            good = {'content': json.dumps(case['expected']), 'finish_reason': 'stop'}
            grade = pilot.execute_and_grade(case, row, good, root / 'good')
            self.assertTrue(grade['task_success'])
            self.assertEqual(json.loads((root / 'good' / case['artifact']).read_text()), case['new'])

    def test_withdrawal_and_malformed_output(self):
        case = next(c for c in self.suite['cases'] if c['event'] == 'forget')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            row = pilot.prepare_case(root / 'memory.db', case, 'full')
            good = {'content': json.dumps(case['expected']), 'finish_reason': 'stop'}
            self.assertTrue(pilot.execute_and_grade(case, row, good, root / 'good')['task_success'])
            self.assertEqual(list((root / 'good').iterdir()), [])
            for index, content in enumerate(['{"action":"write","action":"defer"}', '{"action":NaN}', '[]']):
                grade = pilot.execute_and_grade(case, row, {'content': content, 'finish_reason': 'stop'}, root / str(index))
                self.assertTrue(grade['protocol_error'])
                self.assertFalse(grade['task_success'])

    def test_independent_oracle_detects_wrong_fixture_current_state(self):
        case = self.suite['cases'][0]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            row = pilot.prepare_case(root / 'memory.db', case, 'full')
            row['current_sources']['policy']['value'] = case['old']
            result = pilot.execute_and_grade(case, row, {'content': json.dumps({'action': 'write', **case['old']}),
                'finish_reason': 'stop'}, root / 'out')
            self.assertFalse(result['execution_blocked'])
            self.assertFalse(result['task_success'])

    def test_unexpected_sdk_error_is_not_counted_as_protection(self):
        from unittest.mock import Mock
        user = Mock()
        user.learning.submit.side_effect = RuntimeError('disk I/O error')
        with self.assertRaisesRegex(RuntimeError, 'disk I/O'):
            pilot.FullAdapter(user).submit('v1')

    def test_freeze_tamper_and_no_automatic_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'run'
            pilot.prepare(output)
            pilot.verify_freeze(output)
            class FailingClient:
                calls = 0
                def complete(self, *args, **kwargs):
                    self.calls += 1
                    raise RuntimeError('simulated transport error')
            client = FailingClient()
            with self.assertRaisesRegex(RuntimeError, 'simulated transport'):
                pilot.run(output, client)
            self.assertEqual(client.calls, 1)
            with self.assertRaisesRegex(ValueError, 'explicit --resume'):
                pilot.run(output, client)
            with self.assertRaisesRegex(ValueError, 'unaccounted attempt'):
                pilot.run(output, client, resume=True)
            self.assertEqual(client.calls, 1)
            with (output / 'inputs.jsonl').open('a') as f:
                f.write('\n')
            with self.assertRaisesRegex(ValueError, 'Frozen input'):
                pilot.verify_freeze(output)

    def test_missing_usage_is_saved_and_stops(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'run'
            pilot.prepare(output)
            class MissingUsageClient:
                def complete(self, *args, **kwargs):
                    return {'content': '{}', 'finish_reason': 'stop', 'model': pilot.MODEL}
            with self.assertRaisesRegex(ValueError, 'Missing token accounting'):
                pilot.run(output, MissingUsageClient())
            self.assertTrue((output / 'responses.jsonl').exists())
            self.assertEqual(pilot.summarize(output)['completed_calls'], 0)


if __name__ == '__main__':
    unittest.main()
