import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from enterprise_cases import suites
from run_local import Model, validate_suite


class EnterpriseFixtures(unittest.TestCase):
    def test_frozen_fixtures_match_generator_and_splits(self):
        for name, suite in suites().items():
            validate_suite(suite)
            path=Path(__file__).parent/f'scenarios/enterprise-v1-{name}.json'
            self.assertEqual(json.loads(path.read_text()),suite)
            if name in ('incident','access'):
                self.assertEqual([len(suite['learning'][s]) for s in ('train','validation','test')],[18,18,36])
                for split in ('train','validation','test'):
                    self.assertEqual(len({c['family'] for c in suite['learning'][split]}),6)

    def test_poison_changes_training_labels_only(self):
        all_suites=suites()
        clean=all_suites['access']['learning']; poisoned=all_suites['access-poisoned']['learning']
        for split in ('validation','test'):
            self.assertEqual(clean[split],poisoned[split])
        changes=[]
        for original, changed in zip(clean['train'],poisoned['train']):
            self.assertEqual(original['prompt'],changed['prompt'])
            if original['expected']!=changed['expected']:
                self.assertEqual(original['expected']['priority'],'review')
                self.assertEqual(changed['expected']['priority'],'fulfill')
                changes.append(original['id'])
        self.assertEqual(len(changes),3)
        self.assertEqual(changes,[x['case_id'] for x in poisoned['dataset_notes']['poisoned_labels']])

    def test_seed_matching_and_temperature_in_actual_payload(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self):
                return json.dumps({'choices':[{'message':{'content':'{}'},'finish_reason':'stop'}],'usage':{'total_tokens':1}}).encode()
        args=SimpleNamespace(base_url='http://127.0.0.1:8002/v1',model='test',output_contract='none',
                             max_tokens=256,seed=42,seed_step=1,temperature=.2,timeout=1)
        with tempfile.TemporaryDirectory() as tmp:
            model=Model(args,Path(tmp)); requests=[]
            def respond(req,**kwargs): requests.append(json.loads(req.data)); return Response()
            with patch.object(model.opener,'open',side_effect=respond),patch('builtins.print'):
                for repeat in range(3):
                    for mode in ('memory','candidate','memory_learning'):
                        model.call([{'role':'user','content':'test'}],tag=mode,repeat=repeat)
            self.assertEqual([r['seed'] for r in requests],[42]*3+[43]*3+[44]*3)
            self.assertTrue(all(r['temperature']==.2 for r in requests))


if __name__=='__main__': unittest.main()
