"""Resume must not bypass the token cap after an invalid usage response."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import run_lifecycle_tasks as pilot


class ResumeAccountingTests(unittest.TestCase):
    def test_resume_rejects_invalid_or_mismatched_accounting_before_calling_api(self):
        invalid = [
            {'prompt_tokens': 1, 'completion_tokens': 2, 'total_tokens': -3},
            {'prompt_tokens': -1, 'completion_tokens': 2, 'total_tokens': 1},
            {'prompt_tokens': 1, 'completion_tokens': True, 'total_tokens': 2},
            {'prompt_tokens': 1, 'completion_tokens': 2, 'total_tokens': 4},
            {'total_tokens': 3},
            None,
        ]
        for usage in invalid:
            with self.subTest(usage=usage), tempfile.TemporaryDirectory() as temp:
                output = Path(temp)
                case = json.loads(pilot.SUITE.read_text(encoding='utf-8'))['cases'][0]
                pilot.dump(output / 'suite.json', {'cases': [case]})
                row = {'case_id': case['id'], 'arm': 'plain'}
                pilot.append(output / 'inputs.jsonl', row)
                pilot.append(output / 'attempts.jsonl', row)
                pilot.append(output / 'responses.jsonl', {**row, 'response': {'usage': usage}})
                client = Mock()
                with patch.object(pilot, 'verify_freeze', return_value={}):
                    with self.assertRaisesRegex(ValueError, 'unaccounted attempt'):
                        pilot.run(output, client, resume=True)
                client.complete.assert_not_called()

    def test_response_cannot_account_for_a_different_attempt(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            pilot.dump(output / 'suite.json', {'cases': []})
            (output / 'inputs.jsonl').write_text('')
            pilot.append(output / 'attempts.jsonl', {'case_id': 'a', 'arm': 'plain'})
            pilot.append(output / 'responses.jsonl', {'case_id': 'b', 'arm': 'plain',
                'response': {'usage': {'prompt_tokens': 1, 'completion_tokens': 2, 'total_tokens': 3}}})
            client = Mock()
            with patch.object(pilot, 'verify_freeze', return_value={}):
                with self.assertRaisesRegex(ValueError, 'unaccounted attempt'):
                    pilot.run(output, client, resume=True)
            client.complete.assert_not_called()


if __name__ == '__main__':
    unittest.main()
