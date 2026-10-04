"""The published report must reproduce grading and reject cherry-picked traces."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import run_lifecycle_tasks as pilot
from report_lifecycle_tasks import audit_traces, publish


class ReportLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.suite = json.loads(pilot.SUITE.read_text(encoding='utf-8'))
        cls.traces = []
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for case in cls.suite['cases']:
                for arm in pilot.ARMS:
                    row = pilot.prepare_case(root / (case['id'] + arm + '.db'), case, arm)
                    # Synthetic response solely for report validation, never live evidence.
                    response = {'content': json.dumps(case['expected']), 'finish_reason': 'stop',
                        'model': pilot.MODEL, 'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120},
                        'request': {'messages': row['messages'], 'model': pilot.MODEL, 'max_completion_tokens': 256, 'thinking': {'type': 'disabled'}}}
                    grade = pilot.execute_and_grade(case, row, response, root / 'artifacts' / case['id'] / arm)
                    result = {k: row[k] for k in ('case_id', 'arm', 'split', 'domain', 'event')}
                    cls.traces.append({'input': row, 'result': {**result, **grade, 'response': response}})

    def test_complete_trace_replays(self):
        self.assertEqual(audit_traces(self.suite, self.traces)['audited_traces'], 90)

    def test_missing_or_duplicate_trace_is_rejected(self):
        for traces in (self.traces[:-1], self.traces + [self.traces[0]]):
            with self.assertRaisesRegex(ValueError, 'coverage'):
                audit_traces(self.suite, traces)

    def test_changed_grade_is_rejected(self):
        traces = deepcopy(self.traces)
        traces[0]['result']['task_success'] = False
        with self.assertRaisesRegex(ValueError, 'grade'):
            audit_traces(self.suite, traces)

    def test_changed_prompt_and_usage_are_rejected(self):
        traces = deepcopy(self.traces)
        traces[0]['result']['response']['request']['max_completion_tokens'] = 4096
        with self.assertRaisesRegex(ValueError, 'budget'):
            audit_traces(self.suite, traces)
        traces = deepcopy(self.traces)
        traces[0]['result']['response']['usage']['total_tokens'] = 0
        with self.assertRaisesRegex(ValueError, 'usage'):
            audit_traces(self.suite, traces)

    def test_offline_complete_run_publishes_and_rejects_incomplete_status(self):
        from contextlib import redirect_stdout
        import io
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'run'
            pilot.prepare(output)
            class FixtureClient:
                calls = 0
                def complete(self, messages, *, max_tokens):
                    self.calls += 1
                    sources = json.loads(messages[1]['content'])['current_sources']
                    schema = json.loads(messages[0]['content'].split('\n')[-1])
                    answer = ({'action': 'write', **sources['policy']['value']} if 'policy' in sources else
                              {k: 'defer' if k == 'action' else None for k in schema['properties']})
                    return {'content': json.dumps(answer), 'finish_reason': 'stop', 'model': pilot.MODEL,
                        'latency_ms': 1, 'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120},
                        'request': {'messages': messages, 'model': pilot.MODEL, 'max_completion_tokens': max_tokens,
                                    'thinking': {'type': 'disabled'}}}
            client = FixtureClient()
            with patch.object(pilot.time, 'sleep'), redirect_stdout(io.StringIO()):
                pilot.run(output, client)
            self.assertEqual(client.calls, 90)
            stem = Path(temp) / 'report'
            self.assertEqual(publish(output, stem)['audited_traces'], 90)
            report = json.loads(stem.with_suffix('.json').read_text())
            self.assertEqual(report['summary']['completed_calls'], 90)
            self.assertEqual(report['trace_sha256'], pilot.sha(stem.with_suffix('.traces.jsonl')))
            for arm in pilot.ARMS:
                self.assertEqual(report['summary']['by_arm'][arm]['task_success'], 30)
            pilot.dump(output / 'status.json', {'status': 'stopped_error'})
            with self.assertRaisesRegex(ValueError, 'complete'):
                publish(output, Path(temp) / 'incomplete')


if __name__ == '__main__':
    unittest.main()
