"""The published report must reproduce grading and reject cherry-picked traces."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

import run_lifecycle_tasks as pilot
from report_lifecycle_tasks import audit_traces


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


if __name__ == '__main__':
    unittest.main()
