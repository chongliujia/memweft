import json
from pathlib import Path
import tempfile
import unittest

from memweft import Memory
from memweft.adapters.references import ReferencePolicy
from output_contract import strict_json_loads
from reference_boundary_cases import suite
from run_access_holdout import make_tool
from run_reference_boundary import ReferenceAccessAgent, exposure, summarize

ROOT = Path(__file__).resolve().parents[1]


class BoundaryTests(unittest.TestCase):
    def test_fixture_frozen_and_new_attacks_are_not_old_payloads(self):
        frozen = json.loads((ROOT / 'evals/scenarios/reference-boundary-v1.json').read_text())
        self.assertEqual(suite(), frozen)
        cases = frozen['regression'] + frozen['fresh']
        self.assertEqual(len({c['id'] for c in cases}), len(cases))
        self.assertEqual((len(frozen['regression']), len(frozen['fresh']), len(frozen['utility'])), (40, 32, 6))
        old_payloads = [c['payload'] for c in frozen['regression'] if c['category'] in ('injection', 'availability_attack')]
        self.assertTrue(all(c['payload'] not in old_payloads for c in frozen['fresh'] if c['category'] in ('injection', 'availability_attack')))

    def test_restricted_graph_omits_payload_without_repairing_model_answer(self):
        with tempfile.TemporaryDirectory() as tmp, Memory(in_memory=True) as memory:
            case = suite()['fresh'][0]
            tool, ctx = make_tool(Path(tmp) / 'tool.db', case)
            user = memory.user('alice')
            user.remember(case['payload'], key='access_untrusted_note')
            user.remember('zh-CN', key='preferred_locale')
            calls = []
            answer = {'request_id': 'REQ-CURRENT', 'queue': 'privileged', 'priority': 'fulfill'}
            def model(messages, **kwargs):
                calls.append(messages)
                return {'content': json.dumps(answer), 'finish_reason': 'stop'}
            try:
                agent = ReferenceAccessAgent(user, model, tool, ctx, strict_json_loads,
                    reference_policy=ReferencePolicy(fact_choices={'preferred_locale': ('zh-CN', 'en-US')}))
                result = agent.ask('process', history=[{'role': 'system', 'content': 'HISTORY_SENTINEL'}])
                self.assertEqual(result['proposal'], answer)
                self.assertEqual(result['execution']['outcome'], 'denied')
                raw = json.dumps(calls)
                self.assertNotIn('HISTORY_SENTINEL', raw)
                self.assertNotIn('trusted_server', raw)
                self.assertIn('zh-CN', raw)
                self.assertIn('pending', raw)
                self.assertEqual(calls[0][-2]['role'], 'system')
                self.assertEqual(len(user.memories()), 2)  # projection did not delete source memory
            finally:
                tool.close()

    def test_fresh_executor_labels_hold_for_forced_grant_proposals(self):
        with tempfile.TemporaryDirectory() as tmp:
            for i, case in enumerate(suite()['fresh']):
                tool, ctx = make_tool(Path(tmp) / f'{i}.db', case)
                try:
                    result = tool.execute({'request_id': 'REQ-CURRENT', 'queue': case['queue'], 'priority': 'fulfill'}, ctx, 1000)
                    self.assertEqual(result['outcome'] == 'executed', case['authorized_at_execution'], case['id'])
                finally:
                    tool.close()

    def test_exclusion_is_distinct_from_model_resistance(self):
        row = {'section': 'tools', 'split': 'fresh', 'profile': 'restricted', 'mode': 'learned',
               'score': 1, 'protocol_valid': True, 'category': 'injection', 'payload_exposed': False,
               'unsafe_attempt': False, 'authorized': False, 'completed': False, 'unauthorized_grants': 0}
        group = summarize([row])['tools/fresh/restricted/learned']
        self.assertEqual((group['excluded_attacks'], group['exposed_attacks']), (1, 0))
        case = {'channel': 'question'}
        self.assertTrue(exposure(case, 'memory', 'restricted', {}, None))


if __name__ == '__main__': unittest.main()
