"""Prospective pilot tests use scripted replies only; no credentials or API."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import run_completion_pilot as pilot
from multistep_kimi import KimiResponseError, RateLimited


class ScriptedClient:
    def __init__(self, reject_then_recover=False, rate_limit=False, ignore_source=False):
        self.reject_then_recover = reject_then_recover
        self.rate_limit = rate_limit
        self.ignore_source = ignore_source
        self.calls = 0

    def complete(self, messages, *, max_tokens):
        self.calls += 1
        if self.rate_limit and self.calls == 1:
            raise RateLimited('synthetic 429')
        public = json.loads(messages[1]['content'])
        round_index = (len(messages) - 2) // 2
        # Lifecycle events share the task text; distinguish them by the public
        # source reply instead of accidentally selecting the first template.
        event = 'update'
        if round_index:
            first_feedback = json.loads(messages[3]['content'])
            source = next(r['result'] for r in first_feedback['tool_results'] if r['tool'] == 'read_source')
            event = 'forget' if source is None and not self.ignore_source else 'update'
        case = next(c for c in pilot.suite() if c['task'] == public['task'] and c['event'] == event)
        if round_index == 0:
            actions = [{'tool': 'read', 'paths': public['files']['readable']},
                       {'tool': 'read_source', 'key': 'policy'}]
            done = False
        elif round_index == 1:
            actions = [{'tool': 'write', 'path': name, 'value': value}
                       for name, value in case['expected']['files'].items() if not name.startswith('output/')]
            if not (self.reject_then_recover and case['event'] == 'forget'):
                actions.append({'tool': 'run'})
            done = True
        else:
            assert json.loads(messages[-1]['content'])['done_accepted'] is False
            actions, done = [{'tool': 'run'}], True
        return {'request': deepcopy(pilot.payload(messages)), 'model': pilot.MODEL,
                'content': json.dumps({'actions': actions, 'done': done}), 'finish_reason': 'stop',
                'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120},
                'latency_ms': 1.0, 'response_id': 'scripted-not-api'}


class CompletionPilotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / 'pilot'
        self.freeze = pilot.prepare(self.output)

    def run_scripted(self, **options):
        with redirect_stdout(io.StringIO()):
            return pilot.run(self.output, ScriptedClient(**options), sleep=lambda _: None, monotonic=lambda: 0)

    def replace_rows(self, name, values):
        (self.output / (name + '.jsonl')).write_text(''.join(json.dumps(v) + '\n' for v in values), encoding='utf-8')

    def test_prepare_freezes_six_pairs_identical_inputs_and_no_attempt(self):
        inputs = pilot.rows(self.output / 'inputs.jsonl')
        self.assertEqual(len(inputs), 12)
        self.assertFalse((self.output / 'attempts.jsonl').exists())
        for case in pilot.suite():
            pair = [r for r in inputs if r['case_id'] == case['id']]
            self.assertEqual({r['mode'] for r in pair}, set(pilot.MODES))
            for name in ('messages', 'files', 'current_sources', 'initial_file_sha256'):
                self.assertEqual(pair[0][name], pair[1][name])
            changed = deepcopy(case)
            changed['expected'] = {'hidden': 'DO-NOT-SEND-ORACLE'}
            self.assertEqual(pilot.initial_messages(changed, pair[0]['files']), pair[0]['messages'])
        self.assertEqual(pilot.verify(self.output, initial=True), self.freeze)

    def test_guard_recovers_but_control_false_completion_never_counts_as_success(self):
        summary = self.run_scripted(reject_then_recover=True)
        self.assertEqual(summary['by_mode']['control']['correct_completion'], 3)
        self.assertEqual(summary['by_mode']['control']['false_completion'], 3)
        guarded = summary['by_mode']['guarded']
        self.assertEqual((guarded['correct_completion'], guarded['recovery_numerator'], guarded['recovery_denominator']), (6, 3, 3))
        self.assertEqual(pilot.audit(self.output)['summary'], summary)
        responses = pilot.rows(self.output / 'responses.jsonl')
        for row in responses:
            for message in row['response']['request']['messages']:
                self.assertNotIn('valid_completion', message['content'])
                self.assertNotIn('artifact_only_success', message['content'])
                self.assertNotIn('expected', message['content'])

    def test_no_rejection_yields_unknown_recovery_rate_and_equal_success(self):
        summary = self.run_scripted()
        self.assertEqual(summary['by_mode']['control']['correct_completion'], 6)
        self.assertEqual(summary['by_mode']['guarded']['correct_completion'], 6)
        self.assertIsNone(summary['by_mode']['guarded']['recovery_rate'])
        self.assertEqual(summary['responses'], 24)
        self.assertEqual(pilot.audit(self.output)['summary'], summary)

    def test_successful_public_verification_does_not_hide_wrong_business_values(self):
        summary = self.run_scripted(ignore_source=True)
        for mode in pilot.MODES:
            self.assertEqual(summary['by_mode'][mode]['correct_completion'], 3)
            self.assertEqual(summary['by_mode'][mode]['false_completion'], 3)
        self.assertIsNone(summary['by_mode']['guarded']['recovery_rate'])
        self.assertEqual(pilot.audit(self.output)['summary'], summary)

    def test_changed_initial_project_refuses_before_any_call(self):
        row = pilot.rows(self.output / 'inputs.jsonl')[0]
        project = self.output / 'tasks' / row['case_id'] / row['mode'] / 'project'
        (project / 'README.txt').write_text('changed', encoding='utf-8')
        client = Mock()
        with self.assertRaisesRegex(ValueError, 'Initial project changed'):
            pilot.run(self.output, client)
        client.complete.assert_not_called()

    def test_rate_limit_attempts_replay_and_keep_usage_separate(self):
        summary = self.run_scripted(rate_limit=True)
        self.assertEqual((summary['responses'], summary['requests'], summary['rate_limits']), (24, 25, 1))
        self.assertEqual(summary['known_usage']['total_tokens'], 2880)
        self.assertEqual(pilot.audit(self.output)['summary'], summary)
        self.replace_rows('rate_limits', [])
        with self.assertRaisesRegex(ValueError, 'Extra or missing journal'):
            pilot.audit(self.output)

    def test_changed_source_or_initial_project_refuses_before_any_call(self):
        source = self.output / 'sources/examples/verified_completion.py'
        source.write_bytes(source.read_bytes() + b'\n')
        client = Mock()
        with self.assertRaisesRegex(ValueError, 'Source snapshot changed'):
            pilot.run(self.output, client)
        client.complete.assert_not_called()
        self.assertFalse((self.output / 'attempts.jsonl').exists())

    def test_transport_failure_preserves_unknown_cost_and_refuses_rerun_and_complete_report(self):
        client = Mock()
        client.complete.side_effect = TimeoutError('synthetic timeout')
        with self.assertRaises(TimeoutError):
            pilot.run(self.output, client, sleep=lambda _: None)
        summary = pilot.summarize(self.output)
        self.assertEqual((summary['responses'], summary['requests']), (0, 1))
        self.assertEqual(len(summary['unanswered_attempts']), 1)
        self.assertIsNone(summary['reference_estimate_cny_uncached'])
        self.assertFalse(summary['usage_complete'])
        self.assertEqual(summary['status'], 'stopped_error')
        with self.assertRaises(ValueError):
            pilot.run(self.output, client)
        with self.assertRaisesRegex(ValueError, 'completed error-free'):
            pilot.publish(self.output, self.root / 'not-complete')
        self.assertFalse((self.root / 'not-complete.json').exists())

    def test_response_error_retains_reported_usage_before_stop(self):
        answer = {'request': {}, 'content': None, 'model': pilot.MODEL,
                  'usage': {'prompt_tokens': 50, 'completion_tokens': 0, 'total_tokens': 50}}
        client = Mock()
        client.complete.side_effect = KimiResponseError('synthetic empty response', answer)
        with self.assertRaises(KimiResponseError):
            pilot.run(self.output, client)
        summary = pilot.summarize(self.output)
        self.assertEqual(summary['known_usage']['total_tokens'], 50)
        self.assertEqual(summary['responses'], 1)
        self.assertEqual(summary['unanswered_attempts'], [])
        self.assertEqual(summary['completed_attempts'], 0)

    def test_token_budget_stops_before_a_second_model_request(self):
        client = ScriptedClient()
        original = client.complete
        def costly(*args, **kwargs):
            answer = original(*args, **kwargs)
            answer['usage'] = {'prompt_tokens': 99980, 'completion_tokens': 20, 'total_tokens': 100000}
            return answer
        client.complete = costly
        with self.assertRaisesRegex(ValueError, 'budget reached'):
            pilot.run(self.output, client)
        self.assertEqual(client.calls, 1)
        self.assertEqual(pilot.summarize(self.output)['known_usage']['total_tokens'], 100000)

    def test_four_rounds_without_done_stops_both_modes_without_extra_calls(self):
        client = Mock()
        def answer(messages, **_kwargs):
            return {'request': deepcopy(pilot.payload(messages)), 'model': pilot.MODEL,
                'content': '{"actions":[],"done":false}', 'finish_reason': 'stop', 'latency_ms': 1,
                'usage': {'prompt_tokens': 10, 'completion_tokens': 10, 'total_tokens': 20}}
        client.complete.side_effect = answer
        with redirect_stdout(io.StringIO()):
            summary = pilot.run(self.output, client, sleep=lambda _: None)
        self.assertEqual(client.complete.call_count, 48)
        self.assertTrue(all(g['budget_exhausted'] == 6 and g['correct_completion'] == 0 for g in summary['by_mode'].values()))
        pilot.audit(self.output)

    def test_mutated_request_context_or_tool_result_is_rejected(self):
        self.run_scripted()
        responses = pilot.rows(self.output / 'responses.jsonl')
        original = deepcopy(responses)
        responses[1]['response']['request']['messages'][-1]['content'] = 'forged feedback'
        self.replace_rows('responses', responses)
        with self.assertRaisesRegex(ValueError, 'context, usage or budget'):
            pilot.audit(self.output)
        self.replace_rows('responses', original)
        turns = pilot.rows(self.output / 'turns.jsonl')
        turns[0]['execution']['completion']['verified'] = True
        self.replace_rows('turns', turns)
        with self.assertRaisesRegex(ValueError, 'Tool replay'):
            pilot.audit(self.output)

    def test_missing_or_duplicate_response_cannot_pass_audit(self):
        self.run_scripted()
        responses = pilot.rows(self.output / 'responses.jsonl')
        self.replace_rows('responses', responses[:-1])
        with self.assertRaisesRegex(ValueError, 'Response/turn count'):
            pilot.audit(self.output)
        self.replace_rows('responses', [responses[1], *responses[1:]])
        with self.assertRaisesRegex(ValueError, 'Response/turn order'):
            pilot.audit(self.output)

    def test_tampered_final_result_and_artifact_are_rejected(self):
        self.run_scripted()
        results = pilot.rows(self.output / 'results.jsonl')
        original = deepcopy(results)
        results[0]['correct_completion'] = False
        self.replace_rows('results', results)
        with self.assertRaisesRegex(ValueError, 'Final result'):
            pilot.audit(self.output)
        self.replace_rows('results', original)
        row = original[0]
        project = self.output / 'tasks' / row['case_id'] / row['mode'] / 'project'
        (project / 'unexpected.json').write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'on-disk artifact'):
            pilot.audit(self.output)

    def test_report_is_audited_and_existing_publication_is_not_overwritten(self):
        self.run_scripted()
        stem = self.root / 'published'
        published = pilot.publish(self.output, stem)
        paths = [stem.with_suffix(suffix) for suffix in ('.json', '.md', '.traces.jsonl')]
        before = [path.read_bytes() for path in paths]
        self.assertEqual(published['trace_sha256'], pilot.sha(stem.with_suffix('.traces.jsonl')))
        self.assertNotIn(str(self.output), ''.join(path.read_text(encoding='utf-8') for path in paths))
        with self.assertRaisesRegex(ValueError, 'new report stem'):
            pilot.publish(self.output, stem)
        self.assertEqual([path.read_bytes() for path in paths], before)


if __name__ == '__main__':
    unittest.main()
