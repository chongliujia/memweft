import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from run_workflow_guard import (
    MODES, SUITE, build_input, grade_answer, prepare_memory, run, summarize,
)
from guarded_workflow_agent import check_answer, prepare_workflow, run_prepared
from memweft import Memory
from workflow_guard import WORKFLOWS


class GuardedWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.cases = json.loads(SUITE.read_text())["cases"]

    def test_normal_example_missing_inputs_never_calls_model(self):
        with Memory(in_memory=True) as memory:
            prepared = prepare_workflow(memory.user("missing").session("s"), "tls", "Check TLS")
        self.assertEqual(prepared["status"], "needs_data")
        self.assertFalse(prepared["context"]["report"]["requirements"]["complete"])
        # The caller cannot force a call just by changing a cached status flag.
        prepared["status"] = "prepared"
        client = Mock()
        result = run_prepared(prepared, client)
        client.complete.assert_not_called()
        self.assertEqual(result["status"], "needs_data")
        self.assertEqual(result["executed_actions"], [])

    def test_guard_preserves_wrong_proposal_and_never_repairs_or_executes(self):
        case = next(c for c in self.cases if c["workflow"] == "feature_scope")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "facts.db"
            prepare_memory(path, case)
            item = build_input(path, case, "required_facts")
        wrong = copy.deepcopy(case["expected"])
        wrong["include_offline"] = not wrong["include_offline"]
        client = Mock()
        client.complete.return_value = {"finish_reason": "stop", "content": json.dumps(wrong)}
        result = run_prepared(item, client)
        self.assertEqual(client.complete.call_count, 1)
        self.assertEqual(result["proposal"], wrong)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["executed_actions"], [])

    def test_duplicate_truncated_and_non_json_outputs_are_rejected(self):
        for answer in ({"finish_reason": "stop", "content": '{"a":1,"a":2}'},
                       {"finish_reason": "length", "content": "{}"},
                       {"finish_reason": "stop", "content": "NaN"}):
            result = check_answer("tls", {"memories": []}, answer)
            self.assertEqual(result["status"], "rejected")
            self.assertFalse(result["validation"]["accepted"])

    def test_missing_fixture_remains_missing_in_both_modes_and_labels_do_not_reach_messages(self):
        case = next(c for c in self.cases if c["expectation"] == "needs_data")
        changed = {**case, "expected": {"marker": "NEVER-SEND-ANSWER-LABEL"}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "facts.db"
            prepare_memory(path, case)
            for mode in MODES:
                item = build_input(path, case, mode)
                other = build_input(path, changed, mode)
                self.assertEqual(item["messages"], other["messages"])
                self.assertFalse(item["input_validation"]["accepted"])
                self.assertNotIn("NEVER-SEND-ANSWER-LABEL", json.dumps(item["messages"]))
                self.assertNotIn("calendar.windows", {f["fact_key"] for f in item["context"]["memories"]})

    def test_budget_stops_and_all_inputs_and_source_snapshots_precede_paid_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            args = SimpleNamespace(suite=SUITE, output=Path(directory) / "run", interval=0,
                                   max_calls=2, token_limit=60000, check_memory=False)
            def answer(*_args, **_kwargs):
                self.assertEqual(len((args.output / "inputs.jsonl").read_text().splitlines()), 16)
                metadata = json.loads((args.output / "metadata.json").read_text())
                self.assertIn("examples/workflow_guard.py", metadata["source_sha256"])
                self.assertIn("examples/handoff_app/kimi_client.py", metadata["source_sha256"])
                self.assertTrue((args.output / "sources/examples/workflow_guard.py").is_file())
                return {"content": "{}", "finish_reason": "stop", "latency_ms": 1,
                        "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}}
            client = Mock()
            client.complete.side_effect = answer
            with patch("builtins.print"):
                summary = run(args, client)
            self.assertEqual(client.complete.call_count, 2)
            self.assertEqual(summary["completed_calls"], 2)
            self.assertEqual(json.loads((args.output / "status.json").read_text())["status"], "budget_stopped")

    def test_rejecting_everything_does_not_count_as_task_completion(self):
        case = next(c for c in self.cases if c["expectation"] == "completed")
        answer = {"content": json.dumps(case["expected"]), "finish_reason": "stop",
                  "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}
        row = {"mode": "required_facts", "expectation": "completed", "answer": answer,
               "grade": grade_answer(answer, case), "decision": {"validation": {"accepted": False}},
               "input_validation": {"accepted": True}, "context_and_model_ms": 1}
        group = summarize([row])["by_mode"]["required_facts"]
        self.assertEqual(group["raw_correct"], 1)
        self.assertEqual(group["validated_correct"], 0)
        self.assertEqual(group["false_rejection_with_complete_inputs"], 1)


if __name__ == "__main__":
    unittest.main()
