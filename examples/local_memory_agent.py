"""Reusable LangGraph Agent: scoped recall -> local model -> returned answer.

The injected model_call receives messages and generation options, never answer
keys. The evaluation runner demonstrates it against the local Qwen endpoint.
Conversation history is owned by the caller; this graph does not persist copies
of facts in chat messages or execute model-generated tools.
"""
from typing import Any, TypedDict
from langgraph.graph import StateGraph, START, END
from memweft.adapters.langgraph import LangGraphMemory


class AgentState(TypedDict, total=False):
    question: str
    session_id: str
    task_type: str | None
    mode: str
    candidate: str | None
    schema: dict
    tag: str
    repeat: int
    context: dict
    answer: dict


class LocalMemoryAgent:
    def __init__(self, user, model_call, system: str):
        self.user, self.model_call, self.system = user, model_call, system
        self.memory = LangGraphMemory(user)
        builder = StateGraph(AgentState)
        builder.add_node('recall', self._recall)
        builder.add_node('answer', self._answer)
        builder.add_edge(START, 'recall')
        builder.add_edge('recall', 'answer')
        builder.add_edge('answer', END)
        self.graph = builder.compile()

    def _recall(self, state: AgentState) -> dict[str, Any]:
        if state['mode'] == 'none':
            return {'context': {'text':'','memories':[],'strategies':[],'report':{}}}
        context = self.memory.context(state['session_id'], query=state['question'],
            max_tokens=12000, max_facts=30,
            task_type=state.get('task_type') if state['mode']=='learned' else None)
        return {'context': {'text':context.text,'memories':context.memories,
                            'strategies':context.strategies,'report':context.report}}

    def _answer(self, state: AgentState) -> dict[str, Any]:
        messages = [{'role':'system','content':self.system}]
        if state['context']['text']:
            messages.append({'role':'user','content':'以下是持久记忆组件提供的参考记录：\n'+state['context']['text']})
        if state.get('candidate'):
            messages.append({'role':'user','content':'本次试运行的业务策略：\n'+state['candidate']})
        messages.append({'role':'user','content':state['question']})
        return {'answer':self.model_call(messages,tag=state['tag'],schema=state['schema'],repeat=state['repeat'])}

    def ask(self, question, *, schema, session_id='session', task_type=None,
            mode='memory', candidate=None, tag='agent', repeat=0):
        if mode not in ('none','memory','learned'):
            raise ValueError('mode must be none, memory or learned')
        return self.graph.invoke({'question':question,'schema':schema,'session_id':session_id,
            'task_type':task_type,'mode':mode,'candidate':candidate,'tag':tag,'repeat':repeat})
