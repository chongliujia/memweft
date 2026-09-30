import copy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from run_workflow_comparison import (
    MODES, SUITE, baseline_context, build_context, prepare_memory, run, validate_suite,
)


class WorkflowComparisonTests(unittest.TestCase):
    def test_frozen_suite_has_new_ids_and_contract_valid_answers(self):
        suite = json.loads(SUITE.read_text())
        validate_suite(suite)
        previous = json.loads((SUITE.parent / "common-v3.json").read_text())
        self.assertFalse({case["id"] for case in suite["cases"]} & {case["id"] for case in previous["cases"]})
        self.assertEqual(len({case["question"] for case in suite["cases"]}), 20)

    def test_scope_and_forgetting_have_same_semantics_in_all_modes(self):
        case = {"id": "lifecycle", "question": "active setting",
            "events": [{"op": "remember", "key": "old", "value": "forgotten-value-778"},
                       {"op": "remember", "key": "foreign", "value": "outside-value-559", "scope": {"user_id": "other"}},
                       {"op": "forget", "key": "old"},
                       {"op": "remember", "key": "active", "value": "current-setting-902"}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.db"
            prepare_memory(path, case)
            for mode in MODES:
                text = build_context(path, case, mode)["text"]
                self.assertNotIn("forgotten-value-778", text)
                self.assertNotIn("outside-value-559", text)
                self.assertIn("current-setting-902", text)

    def test_summary_is_query_and_answer_independent_and_history_preserves_updates(self):
        case = {"id": "update", "events": [
            {"op": "remember", "key": "channel", "value": "old-channel"},
            {"op": "remember", "key": "channel", "value": "new-channel"}],
            "question": "old question", "expected": {"answer": "label"}}
        changed = copy.deepcopy(case)
        changed.update(question="unrelated question", expected={"answer": "never-send-this-label"})
        summary = baseline_context(case, "current_state_summary")
        self.assertEqual(summary, baseline_context(changed, "current_state_summary"))
        self.assertNotIn("old-channel", summary)
        self.assertIn("old-channel", baseline_context(case, "full_history"))

    def test_budget_stops_before_extra_calls_and_partial_pairs_are_counted(self):
        client = Mock()
        client.complete.return_value = {"content": "{}", "finish_reason": "stop", "latency_ms": 1,
                                       "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}
        with tempfile.TemporaryDirectory() as directory:
            args = SimpleNamespace(suite=SUITE, output=Path(directory) / "run", max_calls=2,
                token_limit=10000, max_request_bytes=16000, interval=0, check_memory=False)
            with patch("builtins.print"):
                result = run(args, client)
            self.assertEqual(client.complete.call_count, 2)
            self.assertEqual(result["completed_calls"], 2)
            self.assertEqual(result["by_mode"]["memweft"]["total"], 0)
            self.assertEqual(json.loads((args.output / "status.json").read_text())["status"], "budget_stopped")
            self.assertEqual(len((args.output / "inputs.jsonl").read_text().splitlines()), 60)

    def test_sources_and_exact_fixture_bytes_are_archived_before_first_call(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "compact-suite.json"
            payload = json.dumps(json.loads(SUITE.read_text()), ensure_ascii=False).encode("utf-8")
            fixture.write_bytes(payload)
            args = SimpleNamespace(suite=fixture, output=Path(directory) / "run", max_calls=1,
                token_limit=10000, max_request_bytes=16000, interval=0, check_memory=False)

            def answer(*_args, **_kwargs):
                self.assertEqual((args.output / "suite.json").read_bytes(), payload)
                metadata = json.loads((args.output / "metadata.json").read_text())
                self.assertEqual(metadata["suite_sha256"], hashlib.sha256(payload).hexdigest())
                for required in ("evals/run_kimi.py", "evals/run_local.py", "python/src/memweft/__init__.py"):
                    self.assertIn(required, metadata["source_sha256"])
                for relative, digest in metadata["source_sha256"].items():
                    self.assertEqual(hashlib.sha256((args.output / "sources" / relative).read_bytes()).hexdigest(), digest)
                self.assertEqual(len((args.output / "inputs.jsonl").read_text().splitlines()), 60)
                return {"content": "{}", "finish_reason": "stop", "latency_ms": 1,
                        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}

            client = Mock()
            client.complete.side_effect = answer
            with patch("builtins.print"):
                run(args, client)
            self.assertEqual(client.complete.call_count, 1)


if __name__ == "__main__":
    unittest.main()
