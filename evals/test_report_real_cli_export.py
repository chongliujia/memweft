"""Portable saved-evidence audit tests: no SDK, SQLite, subprocesses, or API."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import real_cli_export_task as task
import report_real_cli_export as reporter


class SavedCliEvidenceTests(unittest.TestCase):
    def setUp(self):
        # Keep evidence beneath ROOT because published reports use repo-relative
        # evidence locations. No files outside this temporary tree are changed.
        self.temp = tempfile.TemporaryDirectory(prefix='.real-report-test-', dir=task.ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.output = self.root / 'evidence'
        self.output.mkdir()
        self.scope = {'project': 'memweft-public', 'user': 'maintainer'}
        self.records = [{'key': 'decision.' + key, 'value': 'Synthetic ' + key,
                         'source': 'portable-test'} for key in ('a', 'b', 'c')]
        self.listing = {'schema_version': 1, 'status': 'ok', 'scope': self.scope,
                        'decisions': self.records}
        self.plan = {**self.scope, 'keys': [row['key'] for row in self.records], 'include_sources': True}
        original = self.root / 'original'
        sdk = self.root / 'sdk/memweft/_native.fake'
        self.bytes(sdk, b'not a native SDK; audit must only hash these bytes\n')
        sources = {'decision_log.py': b'raise AssertionError("do not execute saved CLI")\n',
                   'project-decisions.db': b'not a SQLite database\x00synthetic evidence',
                   'previous-cli-list.json': self.json_bytes(self.listing),
                   'public-provenance.json': self.json_bytes({'fixture': 'synthetic'})}
        for name, data in sources.items():
            self.bytes(original / name, data)
            self.bytes(self.output / 'original-inputs' / name, data)
        self.bytes(self.output / 'work/cli/decision_log.py', sources['decision_log.py'])
        self.bytes(self.output / 'work/data/project-decisions.db', sources['project-decisions.db'])
        self.write('work/export-plan.json', self.plan)
        frozen = {}
        for name in task.FROZEN_SOURCES:
            path = self.output / 'frozen-sources' / name
            self.bytes(path, ('raise AssertionError("never execute snapshot: ' + name + '")\n').encode())
            frozen[name] = task.sha(path)
        sdk_pin = {'version': '0.0.synthetic', 'python': '3.11.0', 'platform': 'darwin',
                   'machine': 'arm64', 'source_commit': '1' * 40, 'wheel_sha256': '2' * 64}
        self.oracle = {'schema_version': 1, 'scope': self.scope, 'records': self.records,
                       'record_count': 3, 'sdk_pin': sdk_pin,
                       'cli_sha256': task.sha(original / 'decision_log.py'),
                       'source_snapshot_sha256': task.sha(original / 'project-decisions.db'),
                       'readback_verified': True}
        self.write('oracle.json', self.oracle)
        self.write('work/handoff.json', self.oracle)
        self.manifest = {'original_sources': {name: {'path': str(original / name),
                         'sha256': task.sha(original / name)} for name in sources},
                         'interpreter': str(self.root / 'sdk/python'), 'sdk_pin': sdk_pin,
                         'installed_sdk_sha256': {str(sdk): task.sha(sdk)},
                         'archive_sha256': {'original-inputs/' + name: task.sha(original / name)
                                            for name in sources},
                         'fixed_work_sha256': {'cli/decision_log.py': task.sha(original / 'decision_log.py')},
                         'runner_sha256': frozen['evals/real_cli_export_task.py'],
                         'oracle_sha256': task.sha(self.output / 'oracle.json'),
                         'work_db_sha256_after_baseline': task.sha(original / 'project-decisions.db')}
        self.write('source-manifest.json', self.manifest)
        self.write('initial.json', {'messages': [{'role': 'user', 'content': 'Synthetic export audit'}]})
        self.write('freeze.json', {'frozen_at': '2026-10-06T00:00:00+00:00',
                   'source_manifest_sha256': task.sha(self.output / 'source-manifest.json'),
                   'initial_sha256': task.sha(self.output / 'initial.json'),
                   'runner_sha256': self.manifest['runner_sha256'], 'source_sha256': frozen,
                   'model': 'kimi-k2.6', 'max_rounds': 4, 'max_actions': 3,
                   'max_completion_tokens': 1024, 'max_requests': 4, 'token_stop_threshold': 30000,
                   'retries': 0, 'interval_seconds': 21, 'request_byte_cap': 64000,
                   'pre_run_cooldown_seconds': 60, 'pre_run_cooldown_owner': 'caller'})
        self.write('status.json', {'status': 'completed', 'model_calls': 2})
        self.write('result.json', {'model_calls': 2, 'task_success': True, 'reported_tokens': 32,
                   'usage': {'prompt_tokens': 22, 'completion_tokens': 10, 'total_tokens': 32},
                   'completed_at': '2026-10-06T00:00:30+00:00'})
        self.write_rows('discovery', [self.command('list')])
        self.write('cli-runs/0000.json', {'success': True, 'errors': [],
                   'plan_sha256': task.sha(self.output / 'work/export-plan.json'),
                   'handoff_sha256': task.sha(self.output / 'work/handoff.json'),
                   'commands': [self.command('list'), *[self.command('get', row) for row in self.records],
                                self.command('list')]})
        self.make_transcript()
        # Any accidental transition from read-only audit to the task runtime is
        # a test failure, even on a machine with a real SDK or API credentials.
        for name in ('verify_freeze', '_invoke', '_sdk_pin', 'run', 'prepare'):
            blocker = patch.object(task, name, side_effect=AssertionError('Audit invoked task runtime: ' + name))
            blocker.start()
            self.addCleanup(blocker.stop)

    @staticmethod
    def json_bytes(value):
        return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')

    @staticmethod
    def bytes(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)

    def write(self, name, value):
        self.bytes(self.output / name, self.json_bytes(value))

    def write_rows(self, name, values):
        self.bytes(self.output / (name + '.jsonl'), ''.join(json.dumps(row) + '\n' for row in values).encode())

    def command(self, operation, record=None):
        argv = [self.manifest['interpreter'], '-I', '-B', str(self.output / 'work/cli/decision_log.py'),
                '--db', str(self.output / 'work/data/project-decisions.db'),
                '--project', self.scope['project'], '--user', self.scope['user'], operation]
        answer = self.listing
        if operation == 'get':
            argv.append(record['key'])
            answer = {'schema_version': 1, 'status': 'ok', 'scope': self.scope, 'decision': record}
        return {'argv': argv, 'returncode': 0, 'stdout': json.dumps(answer), 'stderr': '', 'latency_ms': 1}

    def make_transcript(self):
        actions = [[{'tool': 'run'}], [{'tool': 'write', 'path': 'export-plan.json', 'value': self.plan}, {'tool': 'run'}]]
        results = [[{'tool': 'run', 'result': {'ok': True, 'phase': 'inspect', 'cli': self.listing}}],
                   [{'tool': 'write', 'result': {'written': 'export-plan.json'}},
                    {'tool': 'run', 'result': {'ok': True, 'phase': 'export', 'records_exported': 3}}]]
        messages = task.load(self.output / 'initial.json')['messages']
        attempts, responses, turns = [], [], []
        for index in range(2):
            payload = {'model': 'kimi-k2.6', 'messages': messages, 'stream': False,
                       'thinking': {'type': 'disabled'}, 'max_completion_tokens': 1024,
                       'response_format': {'type': 'json_object'}}
            content = json.dumps({'actions': actions[index], 'done': index == 1})
            attempts.append({'round': index, 'started_at': f'2026-10-06T00:00:{index * 21:02d}+00:00',
                             'request_bytes': len(json.dumps(payload, ensure_ascii=False).encode('utf-8'))})
            responses.append({'round': index, 'response': {'request': payload, 'model': 'kimi-k2.6',
                              'content': content, 'finish_reason': 'stop', 'latency_ms': 1,
                              'usage': {'prompt_tokens': 11, 'completion_tokens': 5, 'total_tokens': 16}}})
            turns.append({'round': index, 'execution': {'protocol_error': False, 'done': index == 1,
                          'results': results[index]}})
            messages = messages + [{'role': 'assistant', 'content': content},
                                   {'role': 'user', 'content': json.dumps({'tool_results': results[index],
                                    'rounds_remaining': 3 - index}, ensure_ascii=False, sort_keys=True)}]
        for name, data in (('attempts', attempts), ('responses', responses), ('turns', turns)):
            self.write_rows(name, data)

    def assert_rejected(self):
        with self.assertRaises((AssertionError, ValueError, OSError)):
            reporter.audit(self.output)

    def test_saved_transport_drift_audits_without_executing_or_mutating_evidence(self):
        freeze = task.load(self.output / 'freeze.json')
        for name, digest in freeze['source_sha256'].items():
            self.assertNotEqual(digest, task.sha(task.ROOT / name))
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        report, traces = reporter.audit(self.output)
        after = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertTrue(report['task_success'])
        self.assertEqual(report['model_calls'], 2)
        self.assertEqual(report['cli_commands'], {'list': 3, 'get': 3})
        self.assertEqual(report['source_sha256'], freeze['source_sha256'])
        self.assertEqual(report['current_audit_source_sha256']['evals/report_real_cli_export.py'], task.sha(reporter.__file__))
        public = json.dumps({'report': report, 'traces': traces}, ensure_ascii=False)
        for private_path in (self.output, self.root / 'original', self.root / 'sdk'):
            self.assertNotIn(str(private_path), public)
            self.assertNotIn(str(private_path).replace('\\', '\\\\'), public)
        self.assertEqual([row['value'] for row in traces if row['kind'] == 'artifact'], [self.plan, self.oracle])

    def test_report_writer_emits_consistent_public_artifacts(self):
        stem = self.root / 'reports/synthetic'
        summary = reporter.write_report(self.output, stem)
        self.assertTrue(summary['task_success'])
        self.assertEqual(summary['cli_processes'], 6)
        self.assertTrue(task.load(stem.with_suffix('.json'))['independent_grade']['task_success'])
        self.assertIn('CLI', stem.with_suffix('.md').read_text(encoding='utf-8'))
        self.assertEqual(len(reporter.rows(stem.with_suffix('.traces.jsonl'))), 13)

    def test_frozen_sources_and_pinned_inputs_reject_changed_bytes(self):
        paths = ['frozen-sources/' + name for name in task.FROZEN_SOURCES] + [
            'initial.json', 'source-manifest.json', 'oracle.json', 'original-inputs/previous-cli-list.json',
            'work/cli/decision_log.py', 'work/data/project-decisions.db']
        paths = [self.output / name for name in paths] + [self.root / 'original/decision_log.py', self.root / 'sdk/memweft/_native.fake']
        for path in paths:
            with self.subTest(path=path.relative_to(self.root)):
                original = path.read_bytes()
                path.write_bytes(original + b'\n ')
                self.assert_rejected()
                path.write_bytes(original)

    def test_artifact_tamper_fails_even_with_updated_receipt_digest(self):
        for name in ('export-plan.json', 'handoff.json'):
            with self.subTest(artifact=name):
                path = self.output / 'work' / name
                original = path.read_bytes()
                value = task.load(path)
                if name == 'export-plan.json':
                    value['include_sources'] = False
                else:
                    value['records'][0]['source'] = 'tampered'
                self.write('work/' + name, value)
                receipt = task.load(self.output / 'cli-runs/0000.json')
                receipt['plan_sha256' if name == 'export-plan.json' else 'handoff_sha256'] = task.sha(path)
                self.write('cli-runs/0000.json', receipt)
                self.assert_rejected()
                path.write_bytes(original)
                receipt['plan_sha256' if name == 'export-plan.json' else 'handoff_sha256'] = task.sha(path)
                self.write('cli-runs/0000.json', receipt)

    def test_usage_rejects_nonintegers_negative_and_inconsistent_totals(self):
        original = reporter.rows(self.output / 'responses.jsonl')
        changes = ({'prompt_tokens': True}, {'completion_tokens': -1}, {'total_tokens': 17},
                   {'prompt_tokens': 11.0}, {'prompt_tokens': 12, 'total_tokens': 17})
        for change in changes:
            with self.subTest(change=change):
                values = copy.deepcopy(original)
                values[0]['response']['usage'].update(change)
                self.write_rows('responses', values)
                self.assert_rejected()

    def test_recorded_request_and_spacing_tamper_is_rejected(self):
        for name, mutate in (
            ('responses', lambda values: values[1]['response']['request'].update(stream=True)),
            ('attempts', lambda values: values[0].update(request_bytes=1)),
            ('attempts', lambda values: values[1].update(started_at='2026-10-06T00:00:20+00:00')),
            ('turns', lambda values: values[1]['execution'].update(done=False)),
        ):
            with self.subTest(log=name, change=mutate):
                path = self.output / (name + '.jsonl')
                original = path.read_bytes()
                values = reporter.rows(path)
                mutate(values)
                self.write_rows(name, values)
                self.assert_rejected()
                path.write_bytes(original)

    def test_cli_receipt_hash_status_sequence_scope_and_stdout_tamper_is_rejected(self):
        receipt_path = self.output / 'cli-runs/0000.json'
        original = task.load(receipt_path)
        mutations = {
            'plan digest': lambda receipt: receipt.update(plan_sha256='0' * 64),
            'artifact digest': lambda receipt: receipt.update(handoff_sha256='0' * 64),
            'success flag': lambda receipt: receipt.update(success=False),
            'nonboolean success': lambda receipt: receipt.update(success='true'),
            'empty commands': lambda receipt: receipt.update(commands=[]),
            'missing get': lambda receipt: receipt['commands'].pop(1),
            'missing readback': lambda receipt: receipt['commands'].pop(),
            'failed process': lambda receipt: receipt['commands'][1].update(returncode=3),
            'claimed errors': lambda receipt: receipt.update(errors=['failed']),
            'mutated argv': lambda receipt: receipt['commands'][0]['argv'].__setitem__(0, '/different/python'),
            'mutated stdout': lambda receipt: receipt['commands'][1].update(stdout=json.dumps({
                'schema_version': 1, 'status': 'ok', 'scope': self.scope,
                'decision': {**self.records[0], 'value': 'tampered'}})),
        }
        def change_scope(receipt):
            for command in receipt['commands']:
                command['argv'][7] = 'wrong-project'
                value = json.loads(command['stdout'])
                value['scope']['project'] = 'wrong-project'
                command['stdout'] = json.dumps(value)
        mutations['matching wrong scope in argv and stdout'] = change_scope
        for name, mutate in mutations.items():
            with self.subTest(change=name):
                value = copy.deepcopy(original)
                mutate(value)
                self.write('cli-runs/0000.json', value)
                self.assert_rejected()

    def test_empty_discovery_cannot_claim_actual_cli_discovery(self):
        self.write_rows('discovery', [])
        self.assert_rejected()


if __name__ == '__main__':
    unittest.main()
