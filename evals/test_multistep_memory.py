import json
from pathlib import Path
import tempfile
import unittest

from multistep_fixture import cases, create_fixture, grade
from run_multistep_memory import execute_actions, final_grade, initial_messages


class ToolExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'project'
        self.case = next(c for c in cases() if c['domain'] == 'deploy' and c['event'] == 'update')
        self.row = {'files': create_fixture(self.root, self.case), 'active': {'content': 'old strategy'},
                    'current_sources': {'policy': {'revision': 2, 'value': self.case['new']}}}

    def execute(self, actions, done=False):
        return execute_actions(self.root, self.case, self.row, json.dumps({'actions': actions, 'done': done}))

    def test_business_wrong_json_really_written(self):
        path = self.row['files']['writable'][0]
        result = self.execute([{'tool': 'write', 'path': path, 'value': {'wrong': 'business value'}}])
        self.assertEqual(json.loads((self.root / path).read_text()), {'wrong': 'business value'})
        self.assertEqual(result['wrong_writes'], [path])
        self.assertFalse(grade(self.root, self.case)['task_success'])
        self.assertNotIn('wrong', result['results'][0]['result'])

    def test_oracle_and_traversal_are_not_readable_or_writable(self):
        result = self.execute([{'tool': 'read', 'paths': ['../../suite.json']},
                               {'tool': 'write', 'path': '../escaped.json', 'value': {}},
                               {'tool': 'read', 'paths': ['expected.json']}])
        self.assertTrue(all('error' in row for row in result['results']))
        self.assertFalse((self.root.parent / 'escaped.json').exists())

    def test_current_source_not_in_initial_prompt(self):
        messages = initial_messages(self.case, self.row)
        payload = json.loads(messages[1]['content'])
        self.assertEqual(set(payload), {'task', 'files', 'memory'})
        result = self.execute([{'tool': 'read_source', 'key': 'policy'}])
        self.assertEqual(result['results'][0]['result']['value'], self.case['new'])

    def test_source_withdrawal_is_null(self):
        self.row['current_sources'] = {}
        result = self.execute([{'tool': 'read_source', 'key': 'policy'}])
        self.assertIsNone(result['results'][0]['result'])

    def test_invalid_or_truncated_reply_executes_nothing(self):
        action = {'tool': 'write', 'path': self.row['files']['writable'][0], 'value': {}}
        for content, reason in [('{bad', 'stop'), ('{"actions":[],"actions":[],"done":false}', 'stop'),
                                (json.dumps({'actions': [action] * 4, 'done': True}), 'stop'),
                                (json.dumps({'actions': [action], 'done': True}), 'length')]:
            result = execute_actions(self.root, self.case, self.row, content, reason)
            self.assertTrue(result['protocol_error'])
            self.assertFalse((self.root / action['path']).exists())

    def test_readonly_input_cannot_be_overwritten(self):
        path = self.row['files']['readable'][0]
        old = (self.root / path).read_bytes()
        result = self.execute([{'tool': 'write', 'path': path, 'value': {}}])
        self.assertIn('error', result['results'][0])
        self.assertEqual((self.root / path).read_bytes(), old)

    def test_written_blocked_requires_public_run_and_can_remove_error(self):
        case = next(c for c in cases() if c['domain'] == 'deploy' and c['event'] == 'forget')
        row = {**self.row, 'current_sources': {}}
        actions = [{'tool': 'write', 'path': 'service.json', 'value': {'incorrect': True}},
                   {'tool': 'remove', 'path': 'service.json'},
                   {'tool': 'write', 'path': 'blocked.json', 'value': case['expected']['files']['blocked.json']}]
        first = execute_actions(self.root, case, row, json.dumps({'actions': actions, 'done': False}))
        self.assertTrue(grade(self.root, case)['task_success'])
        self.assertFalse(final_grade(self.root, case, [first])['task_success'])
        second = execute_actions(self.root, case, row, json.dumps({'actions': [{'tool': 'run'}], 'done': True}))
        self.assertTrue(final_grade(self.root, case, [first, second])['task_success'])


if __name__ == '__main__':
    unittest.main()
