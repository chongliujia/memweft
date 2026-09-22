import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Memory } from '../dist/index.js';
import { MemWeftStore, LangGraphMemory } from '../dist/langgraph.js';
import { Annotation, StateGraph, START, END, MemorySaver } from '@langchain/langgraph';

test('optional background checkpoint preserves shared CAS and forgetting', async () => {
  await assert.rejects(Memory.open({inMemory:true,sqliteOptions:{backgroundCheckpointMs:100}}));
  const dir = await mkdtemp(join(tmpdir(), 'memweft-checkpoint-'));
  const options = {path:join(dir,'memory.db'),sqliteOptions:{backgroundCheckpointMs:100,walReclaimThresholdBytes:65536}};
  const writer = await Memory.open(options);
  const reader = await Memory.open(options);
  const memoryConfig = {readPools:[{poolId:'team',access:'read_write'}],defaultWritePool:'team'};
  try {
    const status = await writer.storageStatus();
    assert.equal(status.sqlite_version,'3.51.3');
    assert.equal(status.checkpoint.wal_reclaim_threshold_bytes,65536);
    const a = writer.user('u',{memoryConfig});
    const b = reader.user('u',{agentId:'reader',memoryConfig});
    await a.remember('old',{key:'key',expectedRevision:0});
    await a.remember('new',{key:'key',expectedRevision:1});
    assert.equal((await b.session('s').context()).memories[0].value,'new');
    await assert.rejects(a.remember('stale',{key:'key',expectedRevision:1}));
    await a.forget('key',{expectedRevision:2});
    assert.deepEqual((await b.session('s').context()).memories,[]);
    assert.equal((await a.remember('recreated',{key:'key',expectedRevision:0})).revision,4);
  } finally { reader.close(); writer.close(); await rm(dir,{recursive:true,force:true}); }
});

test('task query reaches shared Rust ranking', async () => {
  const memory = await Memory.open({inMemory:true});
  try {
    const user = memory.user('recall');
    await user.remember('archived', {key:'a_archive'});
    await user.remember('部署端口 17443', {key:'z_port'});
    const context = await user.session('s').context({query:'部署端口',maxFacts:1});
    assert.equal(context.memories[0].fact_key,'z_port');
    assert.equal(context.explain().recall.method,'lexical_overlap_v1');
  } finally { memory.close(); }
});

test('native persistence, scopes, retries and deletion', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'memweft-'));
  const path = join(dir, 'memory.db');
  const memory = await Memory.open({ path });
  let reopenedMemory;
  try {
    const alice = memory.user('alice');
    await alice.remember('long', { key:'style' });
    await alice.remember('brief', { key:'style' });
    const chat = alice.session('s');
    await Promise.all(Array.from({length:4}, () => chat.addMessage('user','hello',{eventId:'event'})));
    assert.equal((await chat.messages()).length,1);
    await assert.rejects(chat.addMessage('user','changed',{eventId:'event'}));
    assert.deepEqual(await memory.user('bob').memories(),[]);
    assert.deepEqual(await memory.user('alice',{tenantId:'other'}).memories(),[]);
    reopenedMemory = await Memory.open({path});
    const reopened = reopenedMemory.user('alice');
    assert.equal((await reopened.memories())[0].value,'brief');
    assert.match((await reopened.session('s').context()).text,/hello/);
    assert.equal((await chat.context({maxTokens:0})).text,'');
    assert.equal(await reopened.forget('style'),true);
    assert.deepEqual(await alice.memories(),[]);
    assert.equal(await chat.clear(),1);
  } finally { reopenedMemory?.close(); memory.close(); await rm(dir,{recursive:true,force:true}); }
  await assert.rejects(memory.user("alice").memories(),/closed/);
});

test('native learning acceptance, repeated feedback, rejection and rollback', async () => {
  const memory = await Memory.open({inMemory:true});
  const user = memory.user('alice');
  const learning = user.learning;
  const feedback={id:'f',task_type:'answer',session_id:'s',run_id:'r',success:false};
  assert.deepEqual(await learning.feedback(feedback),await learning.feedback(feedback));
  const start = id => learning.start({id,proposal:{task_type:'answer',content:'Be concise',proposer_version:'p1'},dataset_version:'heldout',evaluator_version:'e1',case_ids:['a','b','c']});
  const evaluation = score => ({dataset_version:'heldout',evaluator_version:'e1',cases:['a','b','c'].map(case_id=>({case_id,baseline_score:0.5,candidate_score:score,candidate_cost:0.01,candidate_latency_ms:10}))});
  await start('v1');
  assert.equal((await learning.submit('v1',evaluation(0.8))).status,'accepted');
  assert.equal((await learning.submit('v1',evaluation(0.8))).status,'accepted');
  await start('bad');
  assert.equal((await learning.submit('bad',evaluation(0.4))).status,'rejected');
  assert.equal((await learning.active('answer')).version,'v1');
  assert.match((await user.session('s').context({taskType:'answer'})).text,/Be concise/);
  assert.equal(await learning.rollback('answer',{expectedVersion:'v1'}),null);
});

test('real LangGraph.js BaseStore and graph nodes', async () => {
  const memory=await Memory.open({inMemory:true});
  await memory.user('alice').remember('brief',{key:'style'});
  const helper=new LangGraphMemory(memory.user('alice'));
  const store=new MemWeftStore(memory,'alice');
  await store.put(['preferences'],'style',{value:'brief',score:2});
  assert.equal((await store.get(['preferences'],'style')).value.value,'brief');
  assert.equal((await store.search(['preferences'],{filter:{score:{$gte:2}}})).length,1);
  assert.deepEqual(await store.listNamespaces(),[['preferences']]);
  assert.equal(await new MemWeftStore(memory,'bob').get(['preferences'],'style'),null);
  await assert.rejects(store.search(['preferences'],{query:'style'}),/semantic/);
  const result=await store.batch([{namespace:['batch'],key:'k',value:{n:1}},{namespace:['batch'],key:'k'},{namespace:['batch'],key:'k',value:{n:2}}]);
  assert.equal(result[1],null);
  assert.equal((await store.get(['batch'],'k')).value.n,2);
  const State=Annotation.Root({answer:Annotation()});
  const builder=new StateGraph(State)
    .addNode('read',async (state,config)=>{
      const context=await helper.context(config.configurable.thread_id);
      assert.match(context.text,/brief/);
      return {answer:(await config.store.get(['preferences'],'style')).value.value};
    }).addEdge(START,'read').addEdge('read',END);
  const graph=builder.compile({store,checkpointer:new MemorySaver()});
  for(const thread_id of ['s1','s2']) assert.equal((await graph.invoke({answer:''},{configurable:{thread_id}})).answer,'brief');
  await store.delete(['preferences'],'style');
  assert.equal(await store.get(['preferences'],'style'),null);
});

test('shared Rust/Python/TypeScript contract', async () => {
  const { readFile } = await import('node:fs/promises');
  const fixture=JSON.parse(await readFile(new URL('../../tests/contract.json',import.meta.url),'utf8'));
  const memory=await Memory.open({inMemory:true});
  for(const entry of fixture) {
    const result=await memory.request(entry.request);
    for(const [pointer,expected] of Object.entries(entry.checks)) {
      const actual=pointer.split('/').slice(1).reduce((value,key)=>value[key],result);
      assert.deepEqual(actual,expected,JSON.stringify(entry.request));
    }
  }
});

test('mixed pools, read-only bindings, provenance and revision conflicts', async () => {
  const memory = await Memory.open({inMemory:true});
  try {
    const a = memory.user('alice', {agentId:'planner',memoryConfig:{
      readPools:[{poolId:'team',access:'read_write'},{poolId:'private',access:'read_write'}],defaultWritePool:'private'
    }});
    const b = memory.user('alice', {agentId:'executor',memoryConfig:{readPools:[{poolId:'team'}]}});
    const record = await a.remember(8002,{key:'port',poolId:'team',expectedRevision:0});
    assert.equal(record.revision,1);
    assert.equal((await b.memories())[0].writer_agent_id,'planner');
    await a.remember(9000,{key:'port'});
    const context = await a.session('s').context({query:'port'});
    assert.equal(context.memories[0].value,9000);
    assert.equal(context.explain().pools.selected[0].pool_id,'private');
    assert.equal((await a.memories({poolId:'team'}))[0].value,8002);
    await assert.rejects(b.remember(1,{key:'port',poolId:'team'}),/writing/);
    await assert.rejects(b.forget('port',{poolId:'team'}),/writing/);
    await assert.rejects(b.memories({poolId:'private'}),/reading/);
    await assert.rejects(a.remember(1,{key:'port',poolId:'team',expectedRevision:7}),/changed/);
    assert.equal(await a.forget('port'),true);
    assert.equal((await a.memories())[0].value,8002);
    assert.equal(await a.forget('port',{poolId:'team',expectedRevision:1}),true);
    assert.deepEqual(await b.memories(),[]);
  } finally { memory.close(); }
});
