import json
from pathlib import Path
import tempfile
import unittest

from multistep_fixture import cases, create_fixture, grade
from verified_multistep import feedback, make_session


class VerifiedFileTaskTests(unittest.TestCase):
    def task(self, event='forget', domain='deploy', **options):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / 'project'
        case = next(c for c in cases() if c['event'] == event and c['domain'] == domain)
        files = create_fixture(root, case)
        source = {} if event == 'forget' else {'policy': {'value': case['expected']['policy']}}
        return root, case, make_session(root, domain, files, source, **options)

    def step(self, session, actions, done=True):
        return session.step(json.dumps({'actions': actions, 'done': done}))

    def test_rejected_done_returns_feedback_then_explicit_run_can_finish(self):
        root, case, session = self.task()
        action = {'tool': 'write', 'path': 'blocked.json', 'value': case['expected']['files']['blocked.json']}
        denied = self.step(session, [action])
        self.assertTrue(grade(root, case)['task_success'])
        self.assertFalse(denied['done_accepted'])
        self.assertEqual(denied['status'], 'active')
        self.assertFalse(denied['completion']['verified'])
        self.assertEqual(denied['rounds_remaining'], 3)
        public = feedback(denied)
        self.assertEqual(public['completion'], denied['completion'])
        self.assertNotIn('expected', json.dumps(public))
        self.assertNotIn('run', [r['tool'] for r in denied['results']])
        accepted = self.step(session, [{'tool': 'run'}])
        self.assertTrue(accepted['done_accepted'])
        self.assertEqual(accepted['status'], 'completed')
        self.assertTrue(grade(root, case)['task_success'])

    def test_all_fifteen_public_workflows_can_complete_with_explicit_validation(self):
        for case in cases():
            with self.subTest(case=case['id']):
                root, _, session = self.task(case['event'], case['domain'])
                actions = [{'tool': 'write', 'path': name, 'value': value}
                           for name, value in case['expected']['files'].items()
                           if not name.startswith('output/')]
                outcome = self.step(session, actions + [{'tool': 'run'}])
                self.assertEqual(outcome['status'], 'completed')
                self.assertTrue(grade(root, case)['task_success'])

    def test_final_round_never_creates_an_extra_recovery_round(self):
        root, case, session = self.task(max_rounds=1)
        outcome = self.step(session, [{'tool': 'write', 'path': 'blocked.json',
                                      'value': case['expected']['files']['blocked.json']}])
        self.assertEqual(outcome['status'], 'budget_exhausted')
        after = self.step(session, [{'tool': 'run'}])
        self.assertFalse(after['done_accepted'])
        self.assertEqual(after['results'], [])
        self.assertEqual(after['rounds_remaining'], 0)

    def test_successful_run_then_same_bytes_write_requires_another_run(self):
        root, case, session = self.task()
        write = {'tool': 'write', 'path': 'blocked.json', 'value': case['expected']['files']['blocked.json']}
        denied = self.step(session, [write, {'tool': 'run'}, write])
        self.assertFalse(denied['done_accepted'])
        self.assertTrue(self.step(session, [{'tool': 'run'}])['done_accepted'])

    def test_external_file_change_invalidates_previous_verification(self):
        root, case, session = self.task()
        self.step(session, [{'tool': 'write', 'path': 'blocked.json',
                             'value': case['expected']['files']['blocked.json']}, {'tool': 'run'}], done=False)
        (root / 'unexpected.json').write_text('{}', encoding='utf-8')
        denied = self.step(session, [])
        self.assertFalse(denied['done_accepted'])
        self.assertFalse(denied['completion']['verified'])

    def test_public_success_is_not_business_oracle_success(self):
        root, case, session = self.task('update', 'deploy')
        expected = case['expected']['files']
        route = dict(expected['route.json'], region='business-wrong-region')
        outcome = self.step(session, [
            {'tool': 'write', 'path': 'service.json', 'value': expected['service.json']},
            {'tool': 'write', 'path': 'route.json', 'value': route}, {'tool': 'run'}])
        self.assertTrue(outcome['done_accepted'])
        self.assertFalse(grade(root, case)['task_success'])
        self.assertEqual(json.loads((root / 'route.json').read_text())['region'], 'business-wrong-region')

    def test_tools_reject_forged_verification_and_readonly_or_escaping_writes(self):
        root, _, session = self.task()
        original = (root / 'README.txt').read_bytes()
        denied = self.step(session, [
            {'tool': 'run', 'ok': True},
            {'tool': 'write', 'path': 'README.txt', 'value': {}},
            {'tool': 'write', 'path': '../escaped.json', 'value': {}}])
        self.assertFalse(denied['done_accepted'])
        self.assertTrue(all('error' in r for r in denied['results']))
        self.assertEqual((root / 'README.txt').read_bytes(), original)
        self.assertFalse((root.parent / 'escaped.json').exists())


if __name__ == '__main__':
    unittest.main()
