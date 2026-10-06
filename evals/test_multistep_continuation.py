"""Synthetic three-cohort preparation; no credentials or model calls."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import prepare_multistep_continuation as continuation
import prepare_multistep_supplement as supplement
import report_multistep_memory as report
from multistep_fixture import cases, create_fixture
from run_multistep_memory import append, dump, native, read_rows, sha
from test_report_multistep_memory import fixture_logs, replace_rows


def stop_after(full, target, completed_count):
    """Retain a completed prefix, one read-only partial turn, and one timeout."""
    inputs = read_rows(target / 'inputs.jsonl')
    completed, pending = inputs[:completed_count], inputs[completed_count]
    completed_keys = {continuation.key(row) for row in completed}
    pending_key = continuation.key(pending)
    keep = lambda row: continuation.key(row) in completed_keys or continuation.key(row) == pending_key and row['round'] == 0
    for name in ('attempts', 'responses', 'turns'):
        replace_rows(target / (name + '.jsonl'), [row for row in read_rows(full / (name + '.jsonl')) if keep(row)])
    results = [row for row in read_rows(full / 'results.jsonl') if continuation.key(row) in completed_keys]
    replace_rows(target / 'results.jsonl', results)
    append(target / 'attempts.jsonl', {**{field: pending[field] for field in ('case_id', 'arm', 'domain', 'event')},
                                      'round': 1, 'attempt': 0, 'started_at': 'synthetic-timeout'})
    suite = {case['id']: case for case in cases()}
    for row in inputs:
        relative = Path('tasks') / row['case_id'] / row['arm'] / 'project'
        project = target / relative
        shutil.rmtree(project)
        if continuation.key(row) in completed_keys:
            shutil.copytree(full / relative, project)
        else:
            create_fixture(project, suite[row['case_id']])
    # The retained partial response only reads public files and source; it does
    # not modify the project. Its execution is checked by the real report audit.
    calls = completed_count * 2 + 1
    dump(target / 'status.json', {'status': 'stopped_error', 'calls': calls,
                                  'requests': calls + 1, 'tokens': calls * 20})
    (target / 'rate_limits.jsonl').write_text('', encoding='utf-8')
    append(target / 'errors.jsonl', {'error': 'Kimi connection failed or timed out; no automatic retry was made.',
                                    'at': 'synthetic-stop'})


class ContinuationPreparationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template_temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.template_temp.cleanup)
        template = Path(cls.template_temp.name).resolve()
        full, original, second = (template / name for name in ('full', 'original', 'supplement'))
        full.mkdir()
        fixture_logs(full)
        manifest_path = full / 'freeze.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        manifest.update(native_sha256=sha(Path(native.__file__)), interval_seconds=1.1)
        dump(manifest_path, manifest)
        shutil.copytree(full, original)
        stop_after(full, original, 41)
        with patch.object(report, 'ROOT', template), patch.object(supplement, 'ROOT', template):
            supplement.prepare_supplement(original, second, expected_unfinished=19)
            stop_after(full, second, 9)
            assert report.audit(original, allow_partial=True)['completed_tasks'] == 41
            assert report.audit(second, allow_partial=True)['completed_tasks'] == 9

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.priors = [self.root / name for name in ('original', 'supplement')]
        self.output = self.root / 'continuation'
        for prior in self.priors:
            shutil.copytree(Path(self.template_temp.name) / prior.name, prior)
        for module in (continuation, report):
            mock = patch.object(module, 'ROOT', self.root)
            mock.start()
            self.addCleanup(mock.stop)

    def windows_manifest_keys(self):
        """Change only synthetic test manifests, preserving their parent link."""
        for prior in self.priors:
            path = prior / 'freeze.json'
            manifest = json.loads(path.read_text(encoding='utf-8'))
            manifest['source_sha256'] = {name.replace('/', '\\'): digest
                                         for name, digest in manifest['source_sha256'].items()}
            if 'supplement' in manifest:
                manifest['supplement']['original_freeze_sha256'] = sha(self.priors[0] / 'freeze.json')
            dump(path, manifest)
        return json.loads((self.priors[0] / 'freeze.json').read_text(encoding='utf-8'))['source_sha256']

    def test_two_audited_cohorts_select_only_remaining_ten_and_verify_runtime(self):
        before = [continuation.inventory(path) for path in self.priors]
        manifest = continuation.prepare_continuation(self.priors, self.output)
        declared = manifest['continuation']
        self.assertEqual(len(declared['previous_completed_tasks']), 50)
        self.assertEqual(len(declared['selected_tasks']), 10)
        self.assertEqual((manifest['max_successful_calls'], manifest['max_requests'], manifest['interval_seconds']), (40, 120, 21))
        original_lines = (self.priors[0] / 'inputs.jsonl').read_bytes().splitlines(keepends=True)
        self.assertEqual((self.output / 'inputs.jsonl').read_bytes(), b''.join(original_lines[50:]))
        for row in read_rows(self.output / 'inputs.jsonl'):
            project = self.output / 'tasks' / row['case_id'] / row['arm'] / 'project'
            self.assertEqual(continuation.inventory(project), row['initial_file_sha256'])
        accounting = json.loads((self.output / 'prior-accounting.json').read_text(encoding='utf-8'))
        self.assertEqual(accounting['unknown_billing_attempts'], 2)
        self.assertEqual([row['partial_response_count'] for row in accounting['cohorts']], [1, 1])
        self.assertFalse(accounting['usage_complete'])
        validation = json.loads((self.output / 'preparation-validation.json').read_text(encoding='utf-8'))
        self.assertTrue(validation['original_runtime_verify_freeze_passed'])
        runtime = self.output / declared['runtime_root']
        self.assertEqual(Path(validation['runtime_imports']['root']).resolve(), runtime.resolve())
        self.assertEqual(Path(validation['runtime_imports']['native']).resolve(), (self.output / declared['runtime_native_file']).resolve())
        self.assertEqual(declared['runtime_native_sha256'], manifest['native_sha256'])
        for name, digest in manifest['source_sha256'].items():
            self.assertEqual(sha(runtime / name), digest)
        self.assertEqual(sha(self.output / declared['launcher']), declared['launcher_sha256'])
        self.assertFalse((self.output / 'attempts.jsonl').exists())
        self.assertEqual([continuation.inventory(path) for path in self.priors], before)

    def test_deleted_second_cohort_result_rejected_before_selection(self):
        path = self.priors[1] / 'results.jsonl'
        replace_rows(path, read_rows(path)[:-1])
        with self.assertRaisesRegex(ValueError, 'next unfinished task'):
            continuation.prepare_continuation(self.priors, self.output)
        self.assertFalse(self.output.exists())

    def test_changed_original_input_rejected(self):
        path = self.priors[0] / 'inputs.jsonl'
        path.write_bytes(path.read_bytes() + b'\n')
        with self.assertRaisesRegex(ValueError, 'Frozen inputs changed'):
            continuation.prepare_continuation(self.priors, self.output)
        self.assertFalse(self.output.exists())

    def test_duplicate_priors_or_existing_destination_rejected(self):
        with self.assertRaisesRegex(ValueError, 'distinct ordered'):
            continuation.prepare_continuation([self.priors[0]] * 2, self.output)
        self.output.mkdir()
        with self.assertRaisesRegex(ValueError, 'new directory disjoint'):
            continuation.prepare_continuation(self.priors, self.output)

    def test_wrong_remaining_count_rejected(self):
        with self.assertRaisesRegex(ValueError, 'unfinished task count'):
            continuation.prepare_continuation(self.priors, self.output, expected_unfinished=9)
        self.assertFalse(self.output.exists())

    def test_installed_native_digest_mismatch_rejected_portably(self):
        actual_sha = continuation.sha
        native_path = Path(native.__file__).resolve()
        def changed(path):
            return '0' * 64 if Path(path).resolve() == native_path else actual_sha(path)
        with patch.object(continuation, 'sha', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'Installed native binary differs'):
                continuation.prepare_continuation(self.priors, self.output)
        self.assertFalse(self.output.exists())

    def test_installed_sdk_source_mismatch_rejected(self):
        actual_sha = continuation.sha
        sdk_path = Path(continuation.memweft.__file__).resolve().parent / 'api.py'
        def changed(path):
            return '0' * 64 if Path(path).resolve() == sdk_path else actual_sha(path)
        with patch.object(continuation, 'sha', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'Installed SDK source differs'):
                continuation.prepare_continuation(self.priors, self.output)
        self.assertFalse(self.output.exists())

    def test_unexpected_runtime_import_path_cannot_pass_validation(self):
        fake = SimpleNamespace(stdout=json.dumps({'root': str(self.root / 'wrong-runtime'),
                                                  'native': native.__file__, 'native_sha256': sha(Path(native.__file__))}))
        with patch.object(continuation.subprocess, 'run', return_value=fake):
            with self.assertRaisesRegex(ValueError, 'unexpected path'):
                continuation.prepare_continuation(self.priors, self.output)
        self.assertFalse((self.output / 'preparation-validation.json').exists())
        self.assertFalse((self.output / 'attempts.jsonl').exists())


    def test_windows_keys_map_fixture_sdk_and_runtime_without_rewriting_identity(self):
        frozen_hashes = self.windows_manifest_keys()
        before = [continuation.inventory(path) for path in self.priors]
        runtime = self.output / 'isolated-runtime'
        copied_native = runtime / 'python/src/memweft' / Path(native.__file__).name
        fake = SimpleNamespace(stdout=json.dumps({'root': str(runtime), 'native': str(copied_native),
                                                  'native_sha256': sha(Path(native.__file__))}))
        # Exercise real prior audit, fixture/SDK checks, and source copying. Only
        # the terminal old verifier is mocked: on POSIX it cannot interpret the
        # preserved Windows keys. The existing unmocked test runs on each CI OS.
        with patch.object(continuation.subprocess, 'run', return_value=fake) as verifier:
            manifest = continuation.prepare_continuation(self.priors, self.output)
        verifier.assert_called_once()
        self.assertEqual(manifest['source_sha256'], frozen_hashes)
        self.assertEqual(manifest['continuation']['runtime_source_sha256'], frozen_hashes)
        self.assertIn('evals\\multistep_fixture.py', manifest['source_sha256'])
        for name, digest in frozen_hashes.items():
            canonical = name.replace('\\', '/')
            self.assertEqual(sha(runtime / canonical), digest)
            self.assertEqual(sha(self.output / 'sources' / canonical), digest)
        self.assertEqual([continuation.inventory(path) for path in self.priors], before)

    def test_windows_sdk_prefix_still_enforces_installed_source_digest(self):
        self.windows_manifest_keys()
        actual_sha = continuation.sha
        sdk_path = Path(continuation.memweft.__file__).resolve().parent / 'api.py'
        def changed(path):
            return '0' * 64 if Path(path).resolve() == sdk_path else actual_sha(path)
        with patch.object(continuation, 'sha', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'Installed SDK source differs'):
                continuation.prepare_continuation(self.priors, self.output)
        self.assertFalse(self.output.exists())


class SourceManifestPathTests(unittest.TestCase):
    def test_windows_lookup_is_canonical_without_mutating_input(self):
        frozen = {'evals\\multistep_fixture.py': 'fixture', 'python\\src\\memweft\\api.py': 'sdk'}
        before = dict(frozen)
        self.assertEqual(continuation.source_paths(frozen),
                         {'evals/multistep_fixture.py': 'fixture', 'python/src/memweft/api.py': 'sdk'})
        self.assertEqual(frozen, before)

    def test_unsafe_windows_paths_and_ambiguous_aliases_are_rejected(self):
        for name in ('..\\fixture.py', 'C:\\fixture.py', '\\\\server\\share\\fixture.py'):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'Unsafe snapshot path'):
                continuation.source_paths({name: 'digest'})
        for alias in ('evals/fixture.py', 'EVALS\\fixture.py'):
            with self.subTest(alias=alias), self.assertRaisesRegex(ValueError, 'Duplicate normalized'):
                continuation.source_paths({'evals\\fixture.py': 'first', alias: 'second'})


if __name__ == '__main__':
    unittest.main()
