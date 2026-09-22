import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from evidence_learning import build_strategy, training_gate
from run_local import propose_from_training, validate_suite


class EvidenceTests(unittest.TestCase):
    def test_preserves_quotes_and_actions_and_rejects_conflicting_labels(self):
        cases = [{'id':'a','prompt':'用户说“有安全一词”但本人操作', 'expected':{'queue':'account'}}]
        content = build_strategy(cases)
        self.assertIn(json.dumps(cases[0]['expected'],ensure_ascii=False,sort_keys=True), content)
        self.assertIn(cases[0]['prompt'],content)
        with self.assertRaises(ValueError):
            build_strategy(cases+[dict(cases[0],expected={'queue':'security'})])
        with self.assertRaises(ValueError):
            build_strategy([])

    def test_training_gate_requires_full_pairs_and_rejects_regression(self):
        baseline=[{'run_id':'a-0','success':True},{'run_id':'b-0','success':False}]
        checks=[{'case_id':'a','repeat':0,'score':0},{'case_id':'b','repeat':0,'score':1}]
        self.assertFalse(training_gate(checks,baseline)['passed'])
        checks[0]['score']=1
        self.assertTrue(training_gate(checks,baseline)['passed'])
        for bad in [checks[:1],checks+[checks[0]]]:
            with self.assertRaises(ValueError):
                training_gate(bad,baseline)

    def test_builder_calls_only_training_evaluation_and_records_gate(self):
        cases=[({'id':'a','prompt':'train-only','expected':{'queue':'account'}},None)]
        with tempfile.TemporaryDirectory() as temp:
            model=SimpleNamespace(output=Path(temp))
            def check(model,case,mode,split,repeat,*args,**kwargs):
                self.assertEqual((case['id'],split),('a','train_candidate'))
                return {'case_id':'a','repeat':repeat,'score':1}
            with patch('run_local.evaluate',side_effect=check):
                proposal,call=propose_from_training(model,'task',[{'run_id':'a-0','success':True}],cases,{},1,'evidence_table')
            self.assertIsNone(call)  # no invented proposer-model call
            self.assertIn('train-only',proposal['content'])
            self.assertTrue(json.loads((Path(temp)/'training_gate.json').read_text())['passed'])

    def test_v6_holdouts_are_new_and_retired_training_sources_are_explicit(self):
        root=Path(__file__).parent/'scenarios'
        suite=json.loads((root/'common-v6.json').read_text());validate_suite(suite)
        old=set()
        for name in ['common.json','common-v2.json','common-v3.json','common-v4.json','common-v5.json']:
            prev=json.loads((root/name).read_text())
            old.update(c['prompt'] for split in ('train','validation','test') for c in prev['learning'][split])
        for split in ('validation','test'):
            self.assertTrue(all(c['prompt'] not in old for c in suite['learning'][split]))
        retired=[c for c in suite['learning']['train'] if c.get('origin','').startswith('common-v5/test/')]
        self.assertEqual(len(retired),2)
        for split,size in [('train',18),('validation',18),('test',24)]:
            self.assertEqual(len(suite['learning'][split]),size)

    def test_training_regression_cancels_job_without_using_validation(self):
        from run_local import learn
        suite=json.loads((Path(__file__).parent/'scenarios/common-v6.json').read_text())
        operations=[]
        context={'text':'baseline','strategies':[]}
        class Core:
            def request(self,op,scope,**kwargs):
                operations.append(op)
                if op=='feedback': return kwargs['feedback']
                if op=='context': return context
                if op=='learning_start': return {'dataset_version':'v6/validation'}
                if op=='learning_cancel': return {'status':'cancelled','reason':kwargs['reason']}
                if op=='learning_submit': raise AssertionError('training regression reached adoption')
        with tempfile.TemporaryDirectory() as temp:
            output=Path(temp)
            def proposal(*args):
                (output/'training_gate.json').write_text('{"passed":false}')
                return {'content':'regressing candidate'},None
            seen=[]
            def evaluate(model,case,mode,split,*args,**kwargs):
                seen.append(split)
                return {'score':1,'content':'{}','reason':'pass'}
            model=SimpleNamespace(output=output,args=SimpleNamespace(learning_profile='evidence_table',model='test',timeout=1))
            with patch('run_local.propose_from_training',side_effect=proposal),patch('run_local.evaluate',side_effect=evaluate):
                baseline,active,job=learn(Core(),model,suite,1)
            self.assertEqual(job['status'],'cancelled')
            self.assertEqual(baseline,active)
            self.assertEqual(set(seen),{'train'})
            self.assertNotIn('learning_submit',operations)
