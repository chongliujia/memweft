#!/usr/bin/env python3
"""Real LangGraph + local Qwen replay of memory lifecycle and gated learning.

Business data are synthetic. This is an integration replay, not a production
deployment or an independent new generalization benchmark.
"""
import argparse
import json
from pathlib import Path
import sys

from benchmark_bounded import load_sdk, sha
from evidence_learning import build_strategy, training_gate
from run_local import Model, SYSTEM, append, dump, score_answer, case_schema

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'examples'))

CONFIG = {'read_pools':[{'pool_id':'private','access':'read_write'},
                        {'pool_id':'team','access':'read_write'}],
          'default_write_pool':'private','conflict_policy':'private_first'}
OPTIONS = {'background_checkpoint_ms':1000,'wal_reclaim_threshold_bytes':16*1024*1024}


def evaluate(agent, model, case, split, mode, repeat, schema, task=None, candidate=None, forbidden=(), role=None):
    result = agent.ask(case['prompt'],schema=schema,session_id=f'{split}-{repeat}',
                       mode=mode,task_type=task,candidate=candidate,
                       tag=f'{split}/{case["id"]}/{role or mode}/{repeat}',repeat=repeat)
    context = result['context']
    for value in forbidden:
        assert value not in json.dumps(context,ensure_ascii=False), (case['id'],'stale or foreign context')
    answer = result['answer']
    grade = score_answer(answer['content'],case['expected'],schema)
    if answer['finish_reason'] != 'stop':
        grade.update(score=0.0,reason='incomplete_completion')
    row = {'case_id':case['id'],'split':split,'mode':mode,'role':role or mode,'repeat':repeat,
           'expected':case['expected'],'context':context,**answer,**grade}
    append(model.output/'results.jsonl',row)
    return row


def lifecycle(Memory, model, a, suite):
    from local_memory_agent import LocalMemoryAgent
    path = str(a.output/'lifecycle.db')
    memory = Memory(path,sqlite_options=OPTIONS)
    rows, checks = [], []
    schema = suite['output_contracts']['port']
    def users():
        return (memory.user('iris',agent_id='planner',memory_config=CONFIG),
                memory.user('iris',agent_id='executor',memory_config=CONFIG))
    writer, reader = users()
    def check(name, expected, user=None, forbidden=()):
        agent = LocalMemoryAgent(user or reader,model.call,SYSTEM)
        case = {'id':name,'prompt':'Iris 服务当前部署端口是多少？只返回 port；记录不足则为 null。','expected':{'port':expected}}
        for repeat in range(a.repeats):
            for mode in ('none','memory'):
                target = case if mode=='memory' else {**case,'expected':{'port':None}}
                rows.append(evaluate(agent,model,target,'lifecycle',mode,repeat,schema,forbidden=forbidden))
        checks.append(name)
    try:
        check('empty',None)
        writer.remember(48731,key='Iris 部署端口',pool_id='team',expected_revision=0)
        check('shared_initial',48731)
        writer.remember(48739,key='Iris 部署端口',pool_id='team',expected_revision=1)
        check('shared_updated',48739,forbidden=('48731',))
        reader.remember(48743,key='Iris 部署端口')
        check('private_override',48743)
        check('private_agent_isolation',48739,writer,forbidden=('48743',))
        reader.forget('Iris 部署端口')
        check('private_deleted_shared_fallback',48739,forbidden=('48743',))
        writer.forget('Iris 部署端口',pool_id='team',expected_revision=2)
        check('shared_forgotten',None,forbidden=('48731','48739','48743'))
        memory.close()
        memory = Memory(path,sqlite_options=OPTIONS)
        writer, reader = users()
        check('forgotten_after_reopen',None,forbidden=('48731','48739','48743'))
        record = writer.remember(48751,key='Iris 部署端口',pool_id='team',expected_revision=0)
        assert record['revision']==4
        check('recreated_revision',48751)
        check('other_user',None,memory.user('other',memory_config=CONFIG),forbidden=('48751',))
        check('other_tenant',None,memory.user('iris',tenant_id='other',memory_config=CONFIG),forbidden=('48751',))
        return {'steps':checks,'n':len(rows),'passed':sum(r['score']==1 for r in rows),
                'by_mode':{mode:{'n':sum(r['mode']==mode for r in rows),
                                'passed':sum(r['mode']==mode and r['score']==1 for r in rows)} for mode in ['none','memory']},
                'storage':memory.storage_status()}
    finally:
        memory.close()


def learning(Memory, model, a, suite):
    from local_memory_agent import LocalMemoryAgent
    spec = suite['learning']; task = spec['task_type']
    with Memory(str(a.output/'learning.db'),sqlite_options=OPTIONS) as memory:
        writer = memory.user('support',agent_id='policy-owner',memory_config=CONFIG)
        learner = memory.user('support',agent_id='triage',memory_config=CONFIG)
        writer.remember(spec['memory']['value'],key=spec['memory']['key'],pool_id='team',expected_revision=0)
        agent = LocalMemoryAgent(learner,model.call,spec['system'])
        # Builder's only input is the labelled training split, never held-outs.
        content = build_strategy(spec['train'])
        dump(a.output/'proposal_input.json',{'builder':'deterministic-labelled-evidence-v1','train':spec['train']})
        feedback, checks = [], []
        for case in spec['train']:
            for repeat in range(a.repeats):
                before = evaluate(agent,model,case,'train','memory',repeat,case_schema(suite,case))
                after = evaluate(agent,model,case,'train_candidate','memory',repeat,case_schema(suite,case),candidate=content)
                item = learner.learning.feedback(id=f'{case["id"]}-{repeat}',task_type=task,
                    session_id='training',run_id=f'{case["id"]}-{repeat}',success=before['score']==1,
                    details={'question':case['prompt'],'answer':before['content'],'expected':case['expected']})
                feedback.append(item); checks.append(after)
        gate = training_gate(checks,feedback)
        dump(a.output/'training_gate.json',gate)
        version = 'langgraph-evidence-candidate-v1'
        dataset = suite['version']+'/langgraph-validation'
        evaluator = 'exact-json-fields-v2'
        job = learner.learning.start(id=version,proposal={'task_type':task,'content':content,
            'proposer_version':'deterministic-labelled-evidence-v1','source_pools':[{'pool_id':'team','key':spec['memory']['key']}]},
            dataset_version=dataset,evaluator_version=evaluator,
            case_ids=[f'{c["id"]}-{r}' for c in spec['validation'] for r in range(a.repeats)],
            policy={'min_cases':3,'min_gain':.05,'max_case_regression':0,'max_candidate_cost':100,
                    'max_candidate_latency_ms':a.timeout*1000})
        evidence = []
        if gate['passed']:
            for case in spec['validation']:
                for repeat in range(a.repeats):
                    pair = {}
                    for mode in (['baseline','candidate'] if repeat%2==0 else ['candidate','baseline']):
                        pair[mode] = evaluate(agent,model,case,'validation', 'memory',repeat,case_schema(suite,case),
                                              candidate=content if mode=='candidate' else None,role=mode)
                        append(a.output/'validation_pairs.jsonl',{'role':mode,'row':pair[mode]})
                    tokens = pair['candidate']['usage'].get('total_tokens')
                    assert isinstance(tokens,int) and tokens>=0
                    evidence.append({'case_id':f'{case["id"]}-{repeat}',
                        'baseline_score':pair['baseline']['score'],'candidate_score':pair['candidate']['score'],
                        'candidate_cost':tokens/1000,'candidate_latency_ms':pair['candidate']['latency_ms']})
            job = learner.learning.submit(version,{'dataset_version':dataset,'evaluator_version':evaluator,'cases':evidence})
        else:
            job = learner.learning.cancel(version,'candidate regressed on training evidence')
        dump(a.output/'learning.json',{'job':job,'evidence':evidence})
        pairs = []
        for case in spec['test']:
            for repeat in range(a.repeats):
                pair = {}
                for mode in (['memory','learned'] if repeat%2==0 else ['learned','memory']):
                    pair[mode] = evaluate(agent,model,case,'test',mode,repeat,case_schema(suite,case),task=task)
                    versions = [s['version'] for s in pair[mode]['context']['strategies']]
                    assert versions == ([version] if mode=='learned' and job['status']=='accepted' else [])
                pairs.append({'case_id':case['id'],'repeat':repeat,'baseline':pair['memory']['score'],'adopted':pair['learned']['score']})
        # Source update invalidates the accepted shared-derived strategy for all actors.
        active_before = learner.learning.active(task)
        writer.remember('Updated support policy source.',key=spec['memory']['key'],pool_id='team',expected_revision=1)
        assert learner.learning.active(task) is None
        assert not learner.session('after-update').context(task_type=task).strategies
        writer.forget(spec['memory']['key'],pool_id='team',expected_revision=2)
        assert not learner.session('after-forget').context(task_type=task).memories
        return {'status':job['status'],'training_gate':gate,'n':len(pairs),
                'baseline_passed':sum(p['baseline']==1 for p in pairs),
                'adopted_passed':sum(p['adopted']==1 for p in pairs),
                'improved':sum(p['adopted']>p['baseline'] for p in pairs),
                'regressed':sum(p['adopted']<p['baseline'] for p in pairs),
                'accepted_source_invalidation_exercised':active_before is not None,
                'source_update_and_forget_verified':True,'pairs':pairs,'storage':memory.storage_status()}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--native',type=Path,default=ROOT/'target/release/libmemweft_ffi.so')
    p.add_argument('--base-url',default='http://127.0.0.1:8002/v1')
    p.add_argument('--model',default='qwen3-8b')
    p.add_argument('--repeats',type=int,default=2)
    p.add_argument('--timeout',type=float,default=120)
    p.add_argument('--max-tokens',type=int,default=512)
    a=p.parse_args()
    if a.repeats<1:p.error('repeats must be positive')
    a.output.mkdir(parents=True,exist_ok=False)
    a.output_contract='schema';a.temperature=.2;a.seed=42;a.seed_step=1
    Memory=load_sdk(a.native)
    source=ROOT/'evals/scenarios/enterprise-v1-support.json'
    suite=json.loads(source.read_text());dump(a.output/'suite.json',suite)
    manifest={'model':a.model,'base_url':a.base_url,'repeats':a.repeats,'temperature':a.temperature,
              'seed':a.seed,'native_sha256':sha(a.native),'suite_sha256':sha(source),'sources':{},
              'limitations':'Real LangGraph and local model; synthetic business data. Replayed V6 split, not new generalization evidence.'}
    for path in [Path(__file__),ROOT/'examples/local_memory_agent.py',ROOT/'evals/evidence_learning.py',ROOT/'evals/run_local.py',ROOT/'evals/output_contract.py']:
        (a.output/path.name).write_bytes(path.read_bytes());manifest['sources'][str(path.relative_to(ROOT))]=sha(path)
    dump(a.output/'manifest.json',manifest);dump(a.output/'status.json',{'status':'running'})
    model=Model(a,a.output)
    try:
        life=lifecycle(Memory,model,a,suite);dump(a.output/'lifecycle.json',life)
        learned=learning(Memory,model,a,suite);dump(a.output/'learning-summary.json',learned)
        dump(a.output/'summary.json',{'lifecycle':life,'learning':learned,'model_calls':model.calls})
        dump(a.output/'status.json',{'status':'completed'})
    except Exception as error:
        dump(a.output/'status.json',{'status':'failed','error':str(error)});raise


if __name__=='__main__':main()
