"""Meaningful oracle/runner boundary tests for constructed file tasks."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

import multistep_fixture as fixtures


class MultistepFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write_configs(self, path, case, *, expected=None):
        expected = expected or case['expected']
        for name, value in expected['files'].items():
            if not name.startswith('output/'):
                (path / name).write_text(json.dumps(value), encoding='utf-8')

    def test_all_fifteen_fixtures_execute_and_pass_independent_oracle(self):
        cases = fixtures.cases()
        self.assertEqual(len(cases), 15)
        self.assertEqual(len({c['id'] for c in cases}), 15)
        self.assertEqual(json.loads(json.dumps(cases)), cases)
        for case in cases:
            path = self.root / case['id']
            contract = fixtures.create_fixture(path, case)
            self.assertFalse(fixtures.grade(path, case)['task_success'])
            self.write_configs(path, case)
            before = fixtures.grade(path, case)
            self.assertEqual(before['task_success'], case['event'] == 'forget')
            result = fixtures.smoke(path, case)
            self.assertTrue(result['ok'], (case['id'], result))
            self.assertTrue(fixtures.grade(path, case)['task_success'], case['id'])
            self.assertEqual(set(contract['writable']), set(fixtures.WRITABLE[case['domain']] + ['blocked.json']))

    def test_stale_policy_is_executed_not_repaired_or_blocked_by_oracle(self):
        for case in [c for c in fixtures.cases() if c['event'] == 'update']:
            path = self.root / case['id']
            fixtures.create_fixture(path, case)
            old = {'files': fixtures._expected_files(case['domain'], case['old'])}
            self.write_configs(path, case, expected=old)
            self.assertTrue(fixtures.smoke(path, case)['ok'])
            self.assertFalse(fixtures.grade(path, case)['task_success'])
            output = json.loads((path / fixtures.GENERATED[case['domain']]).read_text())
            self.assertEqual(output, old['files'][fixtures.GENERATED[case['domain']]])

    def test_public_executor_and_inputs_are_independent_of_oracle_and_policy(self):
        for original in [c for c in fixtures.cases() if c['event'] == 'update']:
            changed = deepcopy(original)
            changed.update(expected={'canary': 'secret-answer'}, old={'canary': 1}, new={'canary': 2})
            path = self.root / original['id']
            other = self.root / (original['id'] + '-oracle-change')
            contract = fixtures.create_fixture(path, original)
            self.assertEqual(contract, fixtures.create_fixture(other, changed))
            for name in contract['readable']:
                self.assertEqual((path / name).read_bytes(), (other / name).read_bytes())
                self.assertNotIn(b'secret-answer', (other / name).read_bytes())
            self.write_configs(path, original)
            self.write_configs(other, original)
            self.assertEqual(fixtures.smoke(path, original), fixtures.smoke(other, changed))
            self.assertEqual((path / contract['generated'][0]).read_bytes(), (other / contract['generated'][0]).read_bytes())

    def test_withdrawal_rejects_stale_release_and_unnecessary_blocking(self):
        for case in [c for c in fixtures.cases() if c['event'] == 'forget']:
            path = self.root / case['id']
            fixtures.create_fixture(path, case)
            self.write_configs(path, case, expected={'files': fixtures._expected_files(case['domain'], case['old'])})
            self.assertTrue(fixtures.smoke(path, case)['ok'])
            self.assertFalse(fixtures.grade(path, case)['task_success'])
            (path / 'blocked.json').write_text(json.dumps(fixtures.BLOCKED))
            self.assertFalse(fixtures.smoke(path, case)['ok'])
            self.assertFalse(fixtures.grade(path, case)['task_success'])
        case = fixtures.cases()[0]
        path = self.root / 'unnecessary-block'
        fixtures.create_fixture(path, case)
        (path / 'blocked.json').write_text(json.dumps(fixtures.BLOCKED))
        self.assertTrue(fixtures.smoke(path, case)['ok'])
        self.assertFalse(fixtures.grade(path, case)['task_success'])

    def test_changed_input_missing_execution_and_forged_receipt_fail(self):
        case = next(c for c in fixtures.cases() if c['domain'] == 'handoff' and c['event'] == 'update')
        path = self.root / 'handoff'
        fixtures.create_fixture(path, case)
        self.write_configs(path, case)
        self.assertFalse(fixtures.grade(path, case)['task_success'])
        manifest = path / 'manifest.json'
        value = json.loads(manifest.read_text())
        value['sha256'] = '0' * 64
        manifest.write_text(json.dumps(value))
        self.assertFalse(fixtures.smoke(path, case)['ok'])
        self.assertFalse((path / 'output').exists())
        self.write_configs(path, case)
        payload = path / 'inputs/package.txt'
        payload.write_text('changed')
        self.assertFalse(fixtures.smoke(path, case)['ok'])
        self.assertFalse(fixtures.grade(path, case)['task_success'])

    def test_oracle_is_read_only_and_rejects_extra_files_and_type_confusion(self):
        case = next(c for c in fixtures.cases() if c['domain'] == 'export' and c['event'] == 'update')
        path = self.root / 'export'
        fixtures.create_fixture(path, case)
        self.write_configs(path, case)
        self.assertTrue(fixtures.smoke(path, case)['ok'])
        before = {p.relative_to(path).as_posix(): p.read_bytes() for p in path.rglob('*') if p.is_file()}
        self.assertTrue(fixtures.grade(path, case)['task_success'])
        self.assertEqual(before, {p.relative_to(path).as_posix(): p.read_bytes() for p in path.rglob('*') if p.is_file()})
        (path / 'invented.txt').write_text('not a permitted output')
        self.assertFalse(fixtures.grade(path, case)['task_success'])
        (path / 'invented.txt').unlink()
        value = json.loads((path / 'export.json').read_text())
        value['include_pending'] = 1
        (path / 'export.json').write_text(json.dumps(value))
        self.assertFalse(fixtures.smoke(path, case)['ok'])
        self.assertFalse(fixtures.grade(path, case)['task_success'])

    def test_symlink_output_does_not_write_outside_fixture(self):
        case = fixtures.cases()[0]
        path = self.root / 'deploy'
        fixtures.create_fixture(path, case)
        self.write_configs(path, case)
        outside = self.root / 'outside'
        outside.mkdir()
        (path / 'output').symlink_to(outside, target_is_directory=True)
        self.assertFalse(fixtures.smoke(path, case)['ok'])
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse(fixtures.grade(path, case)['task_success'])

    def test_error_paths_are_stable_across_fixture_locations(self):
        case = fixtures.cases()[0]
        roots = [self.root / 'first-run', self.root / 'second-run']
        for root in roots:
            fixtures.create_fixture(root, case)
        results = [fixtures.smoke(root, case) for root in roots]
        self.assertFalse(results[0]['ok'])
        self.assertEqual(results[0], results[1])
        self.assertIn('<project>', results[0]['errors'][0])
        self.assertIn('service.json', results[0]['errors'][0])
        for root in roots:
            (root / 'inputs/service.json').unlink()
        grades = [fixtures.grade(root, case) for root in roots]
        self.assertEqual(grades[0], grades[1])
        self.assertIn('<project>', grades[0]['errors'][0])
        self.assertIn('inputs', grades[0]['errors'][0])
        for result in [*results, *grades]:
            self.assertNotIn(str(self.root.resolve()), json.dumps(result))


if __name__ == '__main__':
    unittest.main()
