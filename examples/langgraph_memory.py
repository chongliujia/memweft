"""A real Python LangGraph with persistent memory; no model API needed."""
from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import InMemorySaver
from memweft import Memory
from memweft.adapters.langgraph import LangGraphMemory
from memweft.adapters.store import MemWeftStore

memory = Memory("data/langgraph.db")
user = memory.user("alice")
user.remember("Prefer concise answers", key="reply_style")
helper = LangGraphMemory(user)
store = MemWeftStore(memory, "alice")

class State(TypedDict):
    question: str
    context: str

def recall(state, config):
    context = helper.context(config["configurable"]["thread_id"])
    return {"context": context.text}

builder = StateGraph(State).add_node("recall", recall)
builder.add_edge(START, "recall")
builder.add_edge("recall", END)
# This checkpointer is intentionally temporary; MemWeft's long-term data is on disk.
graph = builder.compile(store=store, checkpointer=InMemorySaver())
if __name__ == "__main__":
    print(graph.invoke({"question": "How should I answer?", "context": ""},
                       {"configurable": {"thread_id": "chat-001"}}))
