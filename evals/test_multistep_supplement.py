import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from multistep_fixture import cases, create_fixture
from prepare_multistep_supplement import ledger, prepare_supplement, tree_hashes
from run_multistep_memory import (ROOT, append, dump, initial_messages, native,
                                  paths_to_freeze, read_rows, run, sha, verify_freeze)


class SupplementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.original = Path(self.temp.name) / 'original'
        self.output = Path(self.temp.name) / 'supplement'
        self.original.mkdir()
        self.suite = [case for case in cases() if case['id'] in ('export-forget', 'deploy-update')]
        dump(self.original / 'suite.json', {'cases': self.suite})
        self.rows = []
        for case, arm in [(self.suite[0], 'full'), (self.suite[1], 'mem0'), (self.suite[1], 'plain')]:
            task = self.original / 'tasks' / case['id'] / arm
            files = create_fixture(task / 'project', case)
            # Opaque backend bytes are evidence, not rerun or deserialized.
            (task / 'backend').mkdir()
            (task / 'backend' / 'fixture.db').write_bytes(b'original backend\x00')
            row = {'case_id': case['id'], 'arm': arm, 'domain': case['domain'], 'event': case['event'],
                   'active': {'content': 'unchanged original memory'}, 'current_sources': {},
                   'stale_returned': False, 'files': files,
                   'initial_file_sha256': {name: sha(task / 'project' / name) for name in files['readable']}}
            row['messages'] = initial_messages(case, row)
            self.rows.append(row)
            append(self.original / 'inputs.jsonl', row)
        # A completed failure is still completed and must not be selected again.
        append(self.original / 'results.jsonl', {**self.ident(self.rows[0]), 'task_success': False})
        row = self.rows[1]
        for round_index, attempt_index in [(0, 0), (0, 1), (1, 0)]:
            append(self.original / 'attempts.jsonl', {**self.ident(row), 'round': round_index, 'attempt': attempt_index})
        append(self.original / 'rate_limits.jsonl', {**self.ident(row), 'round': 0, 'attempt': 0})
        append(self.original / 'responses.jsonl', {**self.ident(row), 'round': 0, 'attempt': 1,
                                                   'response': self.response()})
        append(self.original / 'turns.jsonl', {**self.ident(row), 'round': 0, 'execution': {'results': []}})
        append(self.original / 'errors.jsonl', {'error': 'connection failed'})
        dump(self.original / 'status.json', {'status': 'stopped_error'})
        hashes = {}
        for source in paths_to_freeze():
            relative = source.relative_to(ROOT)
            target = self.original / 'sources' / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            hashes[relative.as_posix()] = sha(source)
        dump(self.original / 'freeze.json', {
            'frozen_at': 'original', 'source_sha256': hashes, 'native_sha256': sha(Path(native.__file__)),
            'suite_sha256': sha(self.original / 'suite.json'), 'inputs_sha256': sha(self.original / 'inputs.jsonl'),
            'model': 'kimi-k2.6', 'max_rounds': 4, 'max_actions': 3, 'max_completion_tokens': 1024,
            'max_requests': 720, 'max_successful_calls': 240, 'reported_token_stop_threshold': 500000,
            'request_byte_cap': 64000, 'interval_seconds': 1.1, 'rate_limit_retries': 2,
            'rate_limit_cooldown_seconds': 60, 'other_retries': 0,
        })
        project = self.original / 'tasks' / row['case_id'] / row['arm'] / 'project'
        (project / 'blocked.json').write_text('{"status":"blocked"}', encoding='utf-8')
        (project / 'output').mkdir()
        (project / 'output' / 'report.json').write_text('{"old":true}', encoding='utf-8')

    @staticmethod
    def ident(row):
        return {key: row[key] for key in ('case_id', 'arm', 'domain', 'event')}

    @staticmethod
    def response():
        return {'model': 'kimi-k2.6', 'content': '{"actions":[],"done":true}',
                'finish_reason': 'stop', 'usage': {'prompt_tokens': 2, 'completion_tokens': 1, 'total_tokens': 3},
                'latency_ms': 1}

    def test_selects_only_unfinished_exact_lines_and_restores_fresh_projects(self):
        before = tree_hashes(self.original)
        manifest = prepare_supplement(self.original, self.output, expected_unfinished=2)
        self.assertEqual(tree_hashes(self.original), before)
        original_lines = (self.original / 'inputs.jsonl').read_bytes().splitlines(keepends=True)
        self.assertEqual((self.output / 'inputs.jsonl').read_bytes(), b''.join(original_lines[1:]))
        self.assertEqual(manifest['max_successful_calls'], 8)
        self.assertEqual(manifest['max_requests'], 24)
        self.assertEqual(manifest['interval_seconds'], 21)
        self.assertEqual(manifest['source_sha256'], verify_freeze(self.original)['source_sha256'])
        for row in self.rows[1:]:
            task = self.output / 'tasks' / row['case_id'] / row['arm']
            self.assertEqual(tree_hashes(task / 'project'), row['initial_file_sha256'])
            self.assertFalse((task / 'project' / 'blocked.json').exists())
            self.assertFalse((task / 'project' / 'output').exists())
            self.assertEqual((task / 'backend' / 'fixture.db').read_bytes(), b'original backend\x00')
        self.assertFalse((self.output / 'tasks' / self.rows[0]['case_id'] / self.rows[0]['arm']).exists())
        self.assertFalse((self.output / 'attempts.jsonl').exists())
        record = json.loads((self.output / 'supplement-accounting.json').read_text(encoding='utf-8'))
        self.assertEqual(record['original_recorded_429'], 1)
        self.assertEqual(record['unknown_billing_attempts'], 1)
        self.assertEqual(record['partial_valid_responses'], 1)
        self.assertEqual(record['partial_known_usage']['total_tokens'], 3)
        self.assertFalse(record['billing_complete'])
        script = self.output / manifest['supplement']['preparation_script']
        self.assertEqual(sha(script), manifest['supplement']['preparation_script_sha256'])
        verify_freeze(self.output)

    def test_frozen_runner_runs_only_selected_rows_with_fresh_messages(self):
        prepare_supplement(self.original, self.output)
        seen = []
        class Client:
            def complete(_self, messages, max_tokens):
                seen.append(messages.copy())
                return SupplementTests.response()
        with patch('run_multistep_memory.time.sleep'):
            result = run(self.output, Client())
        self.assertEqual(result['completed_tasks'], 2)
        self.assertEqual(result['model_calls'], 2)
        self.assertEqual(seen, [row['messages'] for row in self.rows[1:]])
        self.assertEqual([(row['case_id'], row['arm']) for row in read_rows(self.output / 'results.jsonl')],
                         [(row['case_id'], row['arm']) for row in self.rows[1:]])

    def test_changed_frozen_source_rejected_before_destination_creation(self):
        source = self.original / 'sources/evals/multistep_fixture.py'
        source.write_bytes(source.read_bytes() + b'\n# changed\n')
        with self.assertRaisesRegex(ValueError, 'Frozen source changed'):
            prepare_supplement(self.original, self.output)
        self.assertFalse(self.output.exists())

    def test_changed_native_digest_rejected(self):
        path = self.original / 'freeze.json'
        manifest = json.loads(path.read_text(encoding='utf-8'))
        manifest['native_sha256'] = '0' * 64
        dump(path, manifest)
        with self.assertRaisesRegex(ValueError, 'Native module changed'):
            prepare_supplement(self.original, self.output)
        self.assertFalse(self.output.exists())

    def test_changed_input_or_readonly_project_rejected(self):
        path = self.original / 'inputs.jsonl'
        old = path.read_bytes()
        path.write_bytes(old + b'\n')
        with self.assertRaisesRegex(ValueError, 'Frozen input changed'):
            prepare_supplement(self.original, self.output)
        path.write_bytes(old)
        row = self.rows[1]
        project = self.original / 'tasks' / row['case_id'] / row['arm'] / 'project'
        (project / 'README.txt').write_text('changed', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Read-only fixture changed'):
            prepare_supplement(self.original, self.output)
        self.assertFalse(self.output.exists())

    def test_active_run_wrong_task_count_or_existing_output_rejected(self):
        dump(self.original / 'status.json', {'status': 'running'})
        with self.assertRaisesRegex(ValueError, 'stopped_error'):
            prepare_supplement(self.original, self.output)
        dump(self.original / 'status.json', {'status': 'stopped_error'})
        with self.assertRaisesRegex(ValueError, 'unfinished task count'):
            prepare_supplement(self.original, self.output, expected_unfinished=19)
        self.output.mkdir()
        with self.assertRaisesRegex(ValueError, 'must be new'):
            prepare_supplement(self.original, self.output)

    def test_nested_output_rejected_without_touching_original(self):
        before = tree_hashes(self.original)
        with self.assertRaisesRegex(ValueError, 'disjoint'):
            prepare_supplement(self.original, self.original / 'child')
        self.assertEqual(before, tree_hashes(self.original))

    def test_duplicate_results_or_attempts_rejected(self):
        results_path = self.original / 'results.jsonl'
        old = results_path.read_bytes()
        results_path.write_bytes(old + old)
        with self.assertRaisesRegex(ValueError, 'task identities'):
            prepare_supplement(self.original, self.output)
        results_path.write_bytes(old)
        row = read_rows(self.original / 'attempts.jsonl')[0]
        append(self.original / 'attempts.jsonl', row)
        with self.assertRaisesRegex(ValueError, 'request ledger'):
            ledger(self.original, {(self.rows[1]['case_id'], self.rows[1]['arm'])})

    def test_tree_hashes_use_portable_forward_slash_keys(self):
        hashes = tree_hashes(self.original)
        self.assertTrue(hashes)
        self.assertTrue(all('\\' not in name for name in hashes))
        self.assertIn('sources/evals/multistep_fixture.py', hashes)


if __name__ == '__main__':
    unittest.main()
