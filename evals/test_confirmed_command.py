from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from memweft import Memory
from memweft.adapters.references import ReferencePolicy
from output_contract import strict_json_loads
from confirmed_command_cases import suite
from run_confirmed_command import make_tool, submission, ConfirmedAccessAgent
from confirmed_access_agent import material_digest

ROOT=Path(__file__).resolve().parents[1]


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.case=next(c for c in suite()['fresh'] if c['id']=='command-privileged-valid')
        self.tool,self.ctx=make_tool(Path(self.tmp.name)/'tool.db',self.case)
        self.proposal={'request_id':'REQ-CURRENT','queue':'privileged','priority':'fulfill'}
    def tearDown(self):self.tool.close();self.tmp.cleanup()
    def count(self,table):return self.tool.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]

    def test_confirmed_execute_retry_and_cancel_are_rechecked(self):
        self.assertEqual(self.tool.execute(self.proposal,self.ctx,1000)['outcome'],'executed')
        self.assertEqual(self.tool.execute(self.proposal,self.ctx,1000)['outcome'],'already_granted')
        self.tool.cancel_command(self.ctx)
        self.assertEqual(self.tool.execute(self.proposal,self.ctx,1000)['reason'],'command_not_confirmed')
        self.assertEqual(self.count('grants'),1)
        self.assertEqual(self.count('audit'),3);self.assertEqual(self.count('command_audit'),3)

    def test_pending_confirmation_binds_scope_digest_and_revision(self):
        self.tool.prepare_command(self.ctx,'PENDING',action='execute',material='new')
        pending=replace(self.ctx,command_id='PENDING',input_sha256=material_digest('new'))
        for wrong in [replace(pending,user='other'),replace(pending,input_sha256='0'*64)]:
            with self.assertRaises(ValueError):self.tool.confirm_command(wrong,expected_revision=1)
        with self.assertRaises(ValueError):self.tool.confirm_command(pending,expected_revision=2)
        self.tool.confirm_command(pending,expected_revision=1)
        self.assertEqual(self.tool.execute(self.proposal,pending,1000)['outcome'],'executed')
        with self.assertRaises(ValueError):self.tool.confirm_command(pending,expected_revision=1)

    def test_input_change_expiry_and_scope_cannot_reuse_command(self):
        cases=[(replace(self.ctx,input_sha256=material_digest('stop')),1000,'input_changed'),
               (self.ctx,2000,'command_expired'),(replace(self.ctx,command_id='UNKNOWN'),1000,'command_not_found')]
        for ctx,now,reason in cases:self.assertEqual(self.tool.execute(self.proposal,ctx,now)['reason'],reason)
        self.assertEqual(self.count('grants'),0)

    def test_forged_model_confirmation_is_invalid(self):
        p={**self.proposal,'confirmed':True,'command_id':'CMD-1'}
        self.assertEqual(self.tool.execute(p,self.ctx,1000)['outcome'],'invalid')
        self.assertEqual(self.count('grants'),0)

    def test_command_audit_failure_rolls_back_every_effect(self):
        self.tool.db.execute("CREATE TRIGGER fail_command_audit BEFORE INSERT ON command_audit BEGIN SELECT RAISE(ABORT, 'audit offline'); END")
        with self.assertRaisesRegex(Exception,'audit offline'):self.tool.execute(self.proposal,self.ctx,1000)
        self.assertEqual([self.count(t) for t in ('grants','audit','command_audit')],[0,0,0])

    def test_late_cancel_does_not_rewrite_model_proposal(self):
        calls=[]
        def model(messages,**kwargs):
            calls.append(messages);return {'content':json.dumps(self.proposal),'finish_reason':'stop'}
        with Memory(in_memory=True) as memory:
            agent=ConfirmedAccessAgent(memory.user('alice'),model,self.tool,self.ctx,strict_json_loads,
                reference_policy=ReferencePolicy(),before_execute=lambda t,c:t.cancel_command(c))
            result=agent.ask(submission(self.case))
            self.assertEqual(result['proposal'],self.proposal)
            self.assertEqual(result['execution']['reason'],'command_not_confirmed')
            self.assertNotIn(submission(self.case),json.dumps(calls,ensure_ascii=False))
            self.assertEqual(result['snapshot']['command']['state'],'confirmed')
            with self.assertRaises(ValueError):agent.ask('changed new text')
        self.assertEqual(self.count('grants'),0)

    def test_all_fixtures_force_grants_against_independent_labels(self):
        for i,case in enumerate(suite()['regression']+suite()['fresh']):
            tool,ctx=make_tool(Path(self.tmp.name)/f'{i}.db',case)
            try:
                if case['late_cancel']:tool.cancel_command(ctx)
                if case['late_revoke']:tool.set_approval(ctx,'security','revoked')
                p={'request_id':'REQ-CURRENT','queue':case['queue'],'priority':'fulfill'}
                result=tool.execute(p,ctx,1000)
                self.assertEqual(result['outcome']=='executed',case['effect_allowed'],case['id'])
                self.assertEqual(tool.db.execute('SELECT count(*) FROM grants').fetchone()[0],int(case['effect_allowed']))
            finally:tool.close()

    def test_fixture_matches_frozen_file(self):
        frozen=json.loads((ROOT/'evals/scenarios/confirmed-command-v1.json').read_text())
        self.assertEqual(frozen,suite())
        all_cases=frozen['regression']+frozen['fresh']
        self.assertEqual(len({c['id'] for c in all_cases}),100)
        self.assertEqual(len(frozen['fresh']),28)


if __name__=='__main__':unittest.main()
