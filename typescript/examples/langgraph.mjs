import { Annotation, StateGraph, START, END, MemorySaver } from '@langchain/langgraph';
import { Memory } from '../dist/index.js';
import { LangGraphMemory, MemWeftStore } from '../dist/langgraph.js';

const memory=await Memory.open({path:'data/langgraph-js.db'});
const user=memory.user('alice');
await user.remember('Prefer concise answers',{key:'reply_style'});
const helper=new LangGraphMemory(user);
const store=new MemWeftStore(memory,'alice');
const State=Annotation.Root({context:Annotation()});
const graph=new StateGraph(State)
  .addNode('recall',async (state,config)=>({context:(await helper.context(config.configurable.thread_id)).text}))
  .addEdge(START,'recall').addEdge('recall',END)
  .compile({store,checkpointer:new MemorySaver()});
console.log(await graph.invoke({context:''},{configurable:{thread_id:'chat-001'}}));
