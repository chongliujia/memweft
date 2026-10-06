"""Post-trial patch tests: mocks only; never reads credentials or calls an API."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import multistep_kimi as client_module
import run_multistep_memory as runner
from multistep_fixture import cases, create_fixture
from multistep_kimi import BoundedKimiClient, KimiError, KimiResponseError, MODEL


USAGE = {'prompt_tokens': 16, 'completion_tokens': 8, 'total_tokens': 24}


def identity(case='case', arm='plain', round=0, attempt=0):
    return {'case_id': case, 'arm': arm, 'round': round, 'attempt': attempt}


def response(usage=USAGE, **ids):
    return {**identity(**ids), 'response': {'usage': usage, 'model': MODEL, 'latency_ms': 1}}


class AccountingTests(unittest.TestCase):
    def test_imports_patch_copies(self):
        here = Path(__file__).resolve().parent
        self.assertEqual(Path(runner.__file__).resolve().parent, here)
        self.assertEqual(Path(client_module.__file__).resolve().parent, here)

    def test_successful_accounting_retains_old_fields_and_values(self):
        result = runner.accounting_summary([response()], [identity()], [])
        self.assertEqual(result['usage'], USAGE)
        self.assertEqual(result['known_usage'], USAGE)
        self.assertEqual(result['known_usage_responses'], 1)
        self.assertTrue(result['usage_complete'])
        self.assertEqual(result['estimated_cny_uncached'], (16 * 6.5 + 8 * 27) / 1e6)

    def test_unknown_usage_is_not_zero_or_a_complete_price(self):
        rows = [response(), response(None, round=1)]
        result = runner.accounting_summary(rows, [identity(), identity(round=1)], [])
        self.assertEqual(result['known_usage'], USAGE)
        self.assertEqual(result['unknown_usage_responses'], 1)
        self.assertEqual(result['unanswered_attempts'], 0)
        self.assertFalse(result['usage_complete'])
        self.assertIsNone(result['usage'])
        self.assertIsNone(result['estimated_cny_uncached'])
        self.assertGreater(result['known_usage_estimated_cny_uncached'], 0)

    def test_invalid_quantities_are_unknown(self):
        for usage in ({}, {'prompt_tokens': True, 'completion_tokens': 0, 'total_tokens': 1},
                      {'prompt_tokens': -1, 'completion_tokens': 2, 'total_tokens': 1},
                      {'prompt_tokens': 16, 'completion_tokens': 8, 'total_tokens': 25},
                      'invalid'):
            with self.subTest(usage=usage):
                result = runner.accounting_summary([response(usage)], [identity()], [])
                self.assertEqual(result['known_usage_responses'], 0)
                self.assertEqual(result['unknown_usage_responses'], 1)
                self.assertIsNone(result['usage'])

    def test_response_less_attempt_is_unknown_but_recorded_429_is_excluded(self):
        attempts = [identity(attempt=0), identity(attempt=1), identity(round=1)]
        result = runner.accounting_summary([response(attempt=1)], attempts, [identity(attempt=0)])
        self.assertEqual(result['rate_limited_requests'], 1)
        self.assertEqual(result['unanswered_attempts'], 1)
        self.assertEqual(result['unknown_usage_responses'], 0)
        self.assertEqual(result['known_usage'], USAGE)
        self.assertIsNone(result['estimated_cny_uncached'])
        complete = runner.accounting_summary([response(attempt=1)], attempts[:2], [identity(attempt=0)])
        self.assertTrue(complete['usage_complete'])
        self.assertEqual(complete['unanswered_attempts'], 0)

    def test_unexpected_model_keeps_tokens_but_not_assumed_price(self):
        row = response()
        row['response']['model'] = 'unexpected-model'
        result = runner.accounting_summary([row], [identity()], [])
        self.assertEqual(result['usage'], USAGE)
        self.assertIsNone(result['estimated_cny_uncached'])
        self.assertEqual(result['known_usage_responses_with_unknown_pricing'], 1)

    def test_summary_persists_partial_accounting_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            runner.append(path / 'responses.jsonl', response(None))
            runner.append(path / 'attempts.jsonl', identity())
            result = runner.summarize(path)
            self.assertEqual(result['unknown_usage_responses'], 1)
            self.assertEqual(result['by_arm']['plain']['unknown_usage_responses'], 1)
            self.assertIsNone(result['estimated_cny_uncached'])
            self.assertEqual(json.loads((path / 'summary.json').read_text()), result)


class ErrorResponseTests(unittest.TestCase):
    def client(self, raw):
        client = BoundedKimiClient('local-audit-placeholder')
        client.opener = Mock()
        client.opener.open.return_value = io.BytesIO(json.dumps(raw).encode())
        return client

    def test_empty_http_200_preserves_safe_accounting_and_request(self):
        raw = {'id': 'provider-receipt', 'model': MODEL, 'usage': USAGE,
               'choices': [{'message': {'content': None}, 'finish_reason': 'stop'}],
               'authorization': 'local-audit-placeholder', 'arbitrary_provider_body': 'not-persisted'}
        client = self.client(raw)
        messages = [{'role': 'user', 'content': 'Synthetic local audit.'}]
        with self.assertRaises(KimiResponseError) as caught:
            client.complete(messages)
        error = caught.exception
        self.assertIsInstance(error, KimiError)
        self.assertEqual(error.usage, USAGE)
        self.assertEqual(error.response['response_id'], 'provider-receipt')
        self.assertEqual(error.response['model'], MODEL)
        self.assertEqual(error.response['finish_reason'], 'stop')
        self.assertEqual(error.response['request']['messages'], messages)
        self.assertEqual(error.response['request']['max_completion_tokens'], 1024)
        self.assertNotIn('local-audit-placeholder', json.dumps(error.response))
        self.assertNotIn('authorization', json.dumps(error.response))
        self.assertNotIn('arbitrary_provider_body', error.response)
        client.opener.open.assert_called_once()

    def test_bad_accounting_and_echoed_secret_remain_safe_and_unknown(self):
        raw = {'id': 'local-audit-placeholder', 'model': 'sk-another-secret\n',
               'usage': {'prompt_tokens': 'local-audit-placeholder', 'completion_tokens': 8,
                         'total_tokens': float('nan'), 'arbitrary_key': 'local-audit-placeholder'},
               'choices': []}
        with self.assertRaises(KimiResponseError) as caught:
            self.client(raw).complete([])
        evidence = caught.exception.response
        self.assertFalse(runner.accounted_usage(evidence))
        text = json.dumps(evidence, allow_nan=False)
        self.assertNotIn('local-audit-placeholder', text)
        self.assertNotIn('sk-another-secret', text)
        self.assertNotIn('arbitrary_key', text)

    def test_nonobject_response_and_missing_usage_are_preserved_as_unknown(self):
        for raw in (None, [], {}, {'choices': [], 'usage': None}):
            with self.subTest(raw=raw), self.assertRaises(KimiResponseError) as caught:
                self.client(raw).complete([])
            self.assertIsNone(caught.exception.usage)


class RunnerFailureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)
        self.case = cases()[0]
        self.row = {**identity(case=self.case['id']), 'domain': self.case['domain'],
                    'event': self.case['event'], 'messages': [{'role': 'user', 'content': 'audit'}],
                    'stale_returned': False}
        self.project = self.output / 'tasks' / self.case['id'] / 'plain' / 'project'
        self.row['files'] = create_fixture(self.project, self.case)
        runner.dump(self.output / 'suite.json', {'cases': [self.case]})
        runner.append(self.output / 'inputs.jsonl', self.row)
        self.manifest = {'max_rounds': 4, 'reported_token_stop_threshold': 500000,
                         'max_successful_calls': 240, 'max_completion_tokens': 1024,
                         'request_byte_cap': 64000, 'rate_limit_retries': 2,
                         'max_requests': 720, 'interval_seconds': 0,
                         'rate_limit_cooldown_seconds': 0}
        self.freeze = patch.object(runner, 'verify_freeze', return_value=self.manifest)
        self.freeze.start()
        self.addCleanup(self.freeze.stop)

    def test_empty_response_is_saved_before_runner_stops_without_actions(self):
        evidence = {'usage': USAGE, 'model': MODEL, 'content': None,
                    'request': {'messages': self.row['messages']}, 'latency_ms': 1,
                    'response_error': 'missing_or_empty_completion'}
        original = KimiResponseError('No completion; accounting retained.', evidence)
        client = Mock()
        client.complete.side_effect = original
        with self.assertRaises(KimiResponseError) as caught:
            runner.run(self.output, client)
        self.assertIs(caught.exception, original)
        client.complete.assert_called_once()
        self.assertEqual(runner.read_rows(self.output / 'responses.jsonl')[0]['response'], evidence)
        self.assertFalse((self.output / 'turns.jsonl').exists())
        summary = json.loads((self.output / 'summary.json').read_text())
        self.assertEqual(summary['usage'], USAGE)
        self.assertEqual(summary['completed_tasks'], 0)
        self.assertEqual(summary['unanswered_attempts'], 0)
        status = json.loads((self.output / 'status.json').read_text())
        self.assertEqual(status['tokens'], 24)
        self.assertEqual(status['calls'], 1)

    def test_network_failure_is_unknown_and_never_retried(self):
        client = Mock()
        client.complete.side_effect = KimiError('No response arrived.')
        with self.assertRaisesRegex(KimiError, 'No response arrived'):
            runner.run(self.output, client)
        client.complete.assert_called_once()
        summary = json.loads((self.output / 'summary.json').read_text())
        self.assertEqual(summary['unanswered_attempts'], 1)
        self.assertEqual(summary['model_calls'], 0)
        self.assertIsNone(summary['usage'])
        self.assertIsNone(summary['estimated_cny_uncached'])

    def test_invalid_usage_stops_with_summary_without_masking_original_error(self):
        client = Mock()
        client.complete.return_value = {'usage': None, 'model': MODEL, 'latency_ms': 1}
        with self.assertRaisesRegex(ValueError, 'Unexpected model or invalid usage'):
            runner.run(self.output, client)
        self.assertEqual(json.loads((self.output / 'summary.json').read_text())['unknown_usage_responses'], 1)

    def test_secondary_summary_failure_preserves_original_exception(self):
        original = KimiError('Original API failure.')
        client = Mock()
        client.complete.side_effect = original
        with patch.object(runner, 'summarize', side_effect=OSError('summary path unavailable')):
            with self.assertRaises(KimiError) as caught:
                runner.run(self.output, client)
        self.assertIs(caught.exception, original)
        self.assertEqual(runner.read_rows(self.output / 'errors.jsonl')[-1]['summary_error_type'], 'OSError')


if __name__ == '__main__':
    unittest.main(verbosity=2)
