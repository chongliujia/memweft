import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Memory } from '../dist/index.js';
import { MemWeftStore, LangGraphMemory } from '../dist/langgraph.js';
import { Annotation, StateGraph, START, END, MemorySaver } from '@langchain/langgraph';

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
