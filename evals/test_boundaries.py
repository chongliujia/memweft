import copy
import json
from pathlib import Path
import unittest

from run_local import paired_effect, proposal_messages, training_cases, validate_suite


class BoundaryLearningTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).parent / 'scenarios'
        self.suite = json.loads((root / 'common-v4.json').read_text())
        self.old = json.loads((root / 'common-v3.json').read_text())

    def test_fresh_holdouts_and_unchanged_memory_controls(self):
        validate_suite(self.suite)
        self.assertEqual(self.suite['cases'], self.old['cases'])
        previous = {c['prompt'] for split in ('train', 'validation', 'test') for c in self.old['learning'][split]}
        heldout = [c for split in ('validation', 'test') for c in self.suite['learning'][split]]
        self.assertTrue(all(c['prompt'] not in previous for c in heldout))
        families = {c['family'] for c in self.suite['learning']['train']}
        for split in ('validation', 'test'):
            self.assertEqual({c['family'] for c in self.suite['learning'][split]}, families)

    def test_legacy_control_uses_exact_original_examples(self):
        selected = training_cases(self.suite['learning'], 'legacy')
        self.assertEqual([{k:v for k,v in c.items() if k not in ('family','category')} for c in selected],
                         self.old['learning']['train'])
        self.assertEqual(len(training_cases(self.suite['learning'], 'boundaries')), 12)

    def test_proposer_receives_training_feedback_only(self):
        spec = self.suite['learning']
        feedback = [{'task': c['prompt'], 'expected': c['expected']} for c in training_cases(spec, 'boundaries')]
        messages = proposal_messages(spec['system'], feedback, 'boundaries')
        text = json.dumps(messages, ensure_ascii=False)
        for c in spec['train']:
            self.assertIn(c['prompt'], text)
        for split in ('validation', 'test'):
            for c in spec[split]:
                self.assertNotIn(c['prompt'], text)
                self.assertNotIn(c['id'], text)
        payload = json.loads(messages[1]['content'])
        self.assertEqual(set(payload), {'task_instruction','feedback'})

    def test_ablation_cannot_select_holdout_for_training(self):
        suite = copy.deepcopy(self.suite)
        suite['learning']['legacy_train_ids'][0] = suite['learning']['test'][0]['id']
        with self.assertRaises(ValueError):
            validate_suite(suite)

    def test_paired_report_counts_regressions_and_excludes_other_splits(self):
        rows = []
        for case_id, before, after in [('gain',0,1),('loss',1,0),('same',1,1)]:
            for mode,score in [('memory',before),('memory_learning',after)]:
                rows.append(dict(case_id=case_id,split='test',repeat=0,mode=mode,score=score))
        rows.append(dict(case_id='gain',split='validation',repeat=0,mode='memory_learning',score=0))
        effect = paired_effect(rows, {'gain','loss','same'})
        self.assertEqual((effect['n'],effect['improved'],effect['regressed']), (3,1,1))
        self.assertEqual((effect['baseline_passed'],effect['adopted_passed']), (2,2))
        with self.assertRaises(ValueError):
            paired_effect(rows[1:], {'gain','loss','same'})


class ComparisonTests(unittest.TestCase):
    def test_comparison_requires_matching_settings_and_complete_runs(self):
        import tempfile
        from unittest.mock import patch
        from compare_learning import compare
        fields = ('suite_sha256','binary_sha256','runner_sha256','contract_module_sha256',
                  'repeats','seed','temperature','max_tokens','enable_thinking','evaluator',
                  'model','base_url','output_contract')
        with tempfile.TemporaryDirectory() as temp:
            roots = [Path(temp) / 'a', Path(temp) / 'b']
            for root in roots:
                root.mkdir()
                (root/'calls.jsonl').write_text('{"usage":{"total_tokens":1}}\n')
            changed = False
            incomplete = False
            def fixture(path):
                if path.name == 'metadata.json':
                    meta = dict.fromkeys(fields, 'same')
                    meta['learning_profile'] = 'legacy' if path.parent == roots[0] else 'boundaries'
                    if changed and path.parent == roots[1]:
                        meta['suite_sha256'] = 'different'
                    return meta
                return {
                    'status.json': {'status': 'running' if incomplete else 'completed'},
                    'learning.json': {'job': {'status':'rejected','reason':'test','policy':{},
                        'evaluation':{'cases':[]},'proposal':{'content':'rule'}}},
                    'summary.json': {'groups': []}, 'proposal_input.json': {'training_case_ids':[]},
                    'learning_effect.json': {}, 'adoption_check.json': {'verified':True},
                    'task_scope_check.json': {'verified':True},
                }[path.name]
            with patch('compare_learning.load', side_effect=fixture):
                self.assertEqual(len(compare(roots)['runs']), 2)
                changed = True
                with self.assertRaisesRegex(ValueError, 'differ'):
                    compare(roots)
                changed = False
                incomplete = True
                with self.assertRaisesRegex(ValueError, 'incomplete'):
                    compare(roots)


class RefinementTests(unittest.TestCase):
    def test_v5_holdouts_are_fresh_and_training_is_unchanged(self):
        root = Path(__file__).parent / 'scenarios'
        new = json.loads((root/'common-v5.json').read_text())
        validate_suite(new)
        previous = []
        for name in ('common-v3.json','common-v4.json'):
            suite = json.loads((root/name).read_text())
            previous.extend(c['prompt'] for split in ('train','validation','test') for c in suite['learning'][split])
        self.assertEqual(new['learning']['train'], suite['learning']['train'])
        for split in ('validation','test'):
            self.assertTrue(all(c['prompt'] not in previous for c in new['learning'][split]))

    def test_training_refinement_is_bounded_and_selects_before_validation(self):
        import tempfile
        from types import SimpleNamespace
        from unittest.mock import patch
        from run_local import propose_from_training
        with tempfile.TemporaryDirectory() as folder:
            calls = []
            def call(messages, **kwargs):
                calls.append(messages)
                return {'call_id':len(calls),'content':json.dumps({'content':f'rule-{len(calls)}'}),'finish_reason':'stop'}
            model = SimpleNamespace(output=Path(folder),call=call)
            case = {'id':'train','prompt':'TRAIN ONLY','expected':{'answer':1}}
            def check(*args, **kwargs):
                score = 1 if len(calls) == 2 else 0
                return {'content':'{}','score':score,'reason':'pass' if score else 'wrong_fields','call_id':100+len(calls)}
            with patch('run_local.evaluate', side_effect=check):
                proposal, call_id = propose_from_training(model,'task',[],[(case,None)],{},1,'boundaries_refined')
            self.assertEqual((proposal['content'],call_id,len(calls)),('rule-2',2,2))
            self.assertIn('TRAIN ONLY',calls[1][-1]['content'])
            saved = json.loads((Path(folder)/'training_rounds.json').read_text())
            self.assertEqual(saved['selected_round'],2)
            self.assertEqual(saved['max_rounds'],3)
            calls.clear()
            with patch('run_local.evaluate', return_value={'content':'{}','score':0,'reason':'wrong','call_id':100}):
                proposal, _ = propose_from_training(model,'task',[],[(case,None)],{},1,'boundaries_refined')
            self.assertEqual(len(calls),3)
            self.assertEqual(proposal['content'],'rule-1')

    def test_grouped_rules_use_observed_training_actions_and_contrast_examples(self):
        import tempfile
        from types import SimpleNamespace
        from unittest.mock import patch
        from run_local import propose_from_training
        cases = [({'id':'a','prompt':'TRAIN A','expected':{'queue':'account','priority':'P2'}},None),
                 ({'id':'b','prompt':'TRAIN B','expected':{'queue':'security','priority':'P0'}},None)]
        with tempfile.TemporaryDirectory() as folder:
            requests = []
            def call(messages, **kwargs):
                requests.append(json.loads(messages[1]['content']))
                return {'call_id':len(requests),'finish_reason':'stop',
                        'content':json.dumps({'when':'Training condition','unless':'Contrasting condition'})}
            model = SimpleNamespace(output=Path(folder),call=call)
            with patch('run_local.evaluate', return_value={'content':'{}','score':1,'reason':'pass','call_id':100}):
                proposal, _ = propose_from_training(model,'task',[],cases,{},1,'grouped_rules')
            self.assertEqual(len(requests),2)
            for data in requests:
                self.assertEqual(len(data['positive_examples']),1)
                self.assertEqual(len(data['counterexamples']),1)
                self.assertNotEqual(data['target_action'],data['counterexamples'][0]['action'])
                self.assertNotEqual(data['positive_examples'][0],data['counterexamples'][0]['task'])
                self.assertIn(json.dumps(data['target_action'],ensure_ascii=False),proposal['content'])
            saved = json.loads((Path(folder)/'training_rounds.json').read_text())
            self.assertEqual((saved['max_rounds'],saved['selected_round']), (2,1))
            requests.clear()
            with patch('run_local.evaluate', return_value={'content':'{}','score':0,'reason':'wrong','call_id':100}):
                propose_from_training(model,'task',[],cases,{},1,'grouped_rules')
            self.assertEqual(len(requests),4)


class GeneralizationGateTests(unittest.TestCase):
    def test_mean_improvement_does_not_hide_heldout_regression(self):
        from compare_learning import generalization_verdict
        job = {'status':'accepted','policy':{'min_gain':0.05}}
        effect = {'n':36,'baseline_passed':30,'adopted_passed':32,'regressed':2}
        self.assertFalse(generalization_verdict(job,effect)['passed'])
        effect['regressed'] = 0
        self.assertTrue(generalization_verdict(job,effect)['passed'])
        effect['adopted_passed'] = 31
        self.assertFalse(generalization_verdict(job,effect)['passed'])
        job['status'] = 'rejected'
        self.assertFalse(generalization_verdict(job,effect)['passed'])
