import asyncio
import os
import tempfile
import unittest

from memweft import Memory, AsyncMemory
from memweft.adapters.langgraph import LangGraphMemory
from memweft.adapters.store import MemWeftStore
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.base import GetOp, PutOp
from typing import TypedDict

class HighLevelTests(unittest.TestCase):
    def test_shared_language_contract(self):
        import json
        from pathlib import Path
        memory = Memory(in_memory=True)
        fixture = json.loads((Path(__file__).resolve().parents[2] / "tests/contract.json").read_text(encoding="utf-8"))
        for case in fixture:
            result = memory._request(case["request"])
            for pointer, expected in case["checks"].items():
                actual = result
                for part in pointer.split("/")[1:]:
                    actual = actual[int(part)] if isinstance(actual, list) else actual[part]
                self.assertEqual(actual, expected, case["request"])

    def test_restart_and_scopes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "memory.db")
            m = Memory(path)
            user = m.user("alice")
            user.remember("verbose", key="style")
            user.remember("concise", key="style")
            chat = user.session("s")
            chat.add_message("user", "hello", event_id="event", run_id="r1")
            chat.add_message("user", "hello", event_id="event", run_id="r2")
            self.assertEqual(len(chat.messages()), 1)
            with self.assertRaises(ValueError):
                chat.add_message("user", "changed", event_id="event")
            reopened_memory = Memory(path)
            reopened = reopened_memory.user("alice")
            self.assertEqual(reopened.memories()[0]["value"], "concise")
            self.assertIn("hello", reopened.session("s").context().text)
            self.assertEqual(reopened.session("s").context(max_tokens=0).text, "")
            self.assertEqual(m.user("bob").memories(), [])
            self.assertEqual(m.user("alice", tenant_id="other").memories(), [])
            self.assertEqual(chat.clear(), 1)
            self.assertTrue(reopened.forget("style"))
            self.assertEqual(reopened.memories(), [])
            reopened_memory.close()
            m.close()
            with self.assertRaises(RuntimeError):
                user.memories()

    def test_learning(self):
        m = Memory(in_memory=True)
        learning = m.user("alice").learning
        feedback = dict(id="f1", task_type="answer", session_id="s", run_id="r", success=False, details="too long")
        self.assertEqual(learning.feedback(**feedback), learning.feedback(**feedback))
        cases = ["a", "b", "c"]
        job = learning.start(id="v1", proposal={"task_type":"answer", "content":"Be concise", "proposer_version":"manual-v1"},
                             dataset_version="heldout-v1", evaluator_version="test-v1", case_ids=cases)
        self.assertEqual(job["status"], "evaluating")
        evaluation = {"dataset_version":"heldout-v1", "evaluator_version":"test-v1", "cases":[
            {"case_id":c,"baseline_score":0.4,"candidate_score":0.8,"candidate_cost":0.01,"candidate_latency_ms":10}
            for c in cases]}
        self.assertEqual(learning.submit("v1", evaluation)["status"], "accepted")
        self.assertEqual(learning.submit("v1", evaluation)["status"], "accepted")
        self.assertIn("Be concise", m.user("alice").session("s").context(task_type="answer").text)
        self.assertIsNone(learning.rollback("answer", expected_version="v1"))

    def test_real_langgraph_store_and_node(self):
        m = Memory(in_memory=True)
        m.user("alice").remember("concise", key="style")
        helper = LangGraphMemory(m.user("alice"))
        store = MemWeftStore(m, "alice")
        store.put(("preferences",), "style", {"value":"concise", "score":2})
        self.assertEqual(store.get(("preferences",), "style").value["value"], "concise")
        self.assertEqual(len(store.search(("preferences",), filter={"score":{"$gte":2}})), 1)
        self.assertEqual(store.list_namespaces(), [("preferences",)])
        self.assertIsNone(MemWeftStore(m,"bob").get(("preferences",),"style"))
        with self.assertRaises(ValueError):
            store.search(("preferences",), query="find style")
        with self.assertRaises(NotImplementedError):
            store.put(("preferences",), "ttl", {"x":1}, ttl=10)
        # LangGraph reference semantics: reads see pre-batch state; last put wins.
        results = store.batch([PutOp(("batch",), "k", {"n":1}), GetOp(("batch",), "k"), PutOp(("batch",), "k", {"n":2})])
        self.assertIsNone(results[1])
        self.assertEqual(store.get(("batch",), "k").value, {"n":2})

        class State(TypedDict):
            answer: str
        def node(state, config, *, store):
            context = helper.context(config["configurable"]["thread_id"])
            self.assertIn("concise", context.text)
            item = store.get(("preferences",), "style")
            return {"answer": item.value["value"]}
        builder = StateGraph(State)
        builder.add_node("answer", node)
        builder.add_edge(START, "answer")
        builder.add_edge("answer", END)
        graph = builder.compile(store=store, checkpointer=InMemorySaver())
        for thread in ["session1", "session2"]:
            result = graph.invoke({"answer":""}, {"configurable":{"thread_id":thread}})
            self.assertEqual(result["answer"], "concise")
        store.delete(("preferences",), "style")
        self.assertIsNone(store.get(("preferences",), "style"))

class AsyncHighLevelTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_async_and_graph(self):
        m = AsyncMemory(in_memory=True)
        user = m.user("alice")
        await user.remember("brief", key="style")
        chat = user.session("s")
        await asyncio.gather(*(chat.add_message("user", "same", event_id="e") for _ in range(4)))
        self.assertEqual(len(await chat.messages()), 1)
        self.assertIn("brief", (await chat.context()).text)
        await user.learning.feedback(id="f", task_type="answer", session_id="s", run_id="r", success=True)
        self.assertEqual(await user.learning.jobs(), [])
        self.assertTrue(await user.forget("style"))
        self.assertEqual(await chat.clear(), 1)

        store = MemWeftStore(Memory(in_memory=True), "alice")
        await store.aput(("n",), "k", {"v":1})
        self.assertEqual((await store.aget(("n",), "k")).value, {"v":1})
        class State(TypedDict):
            answer: int
        async def node(state, *, store):
            return {"answer": (await store.aget(("n",), "k")).value["v"]}
        graph = StateGraph(State).add_node("read", node).add_edge(START, "read").add_edge("read", END).compile(store=store)
        self.assertEqual((await graph.ainvoke({"answer":0}))["answer"], 1)

if __name__ == "__main__": unittest.main()
