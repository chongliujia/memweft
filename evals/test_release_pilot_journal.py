"""Offline persistence and accounting tests for the internal pilot journal."""
import json
from pathlib import Path
import tempfile
import unittest
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
from release_pilot_journal import record_assessment, summarize_attempts, write_attempt


def result(*, rejected=False, usage=None, latency=12.5):
    return {"evidence": {"head": "abc", "ci_for_current_source": "unavailable"},
        "context": {"text": "saved decisions", "report": {"requirements": {"complete": True}}},
        "answer": {"content": '{"ready":false}', "finish_reason": "stop", "model": "fake",
                   "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25} if usage is None else usage,
                   "latency_ms": latency},
        "proposal": {"ready": False, "next_action": "verify_cross_platform_ci"},
        "validation_error": {"code": "model_plan_rejected", "errors": [
            {"code": "decision_mismatch", "field": "target", "expected": "developer_preview", "actual": "bounded_production"},
            {"code": "decision_mismatch", "field": "delivery", "expected": "downloadable_artifacts", "actual": "registries"},
            {"code": "next_action_mismatch"}]} if rejected else None,
        "blockers": ["current_source_ci_not_verified"], "expected": {"ready": False},
        "expected_decisions": {"target": "developer_preview"}, "expected_next_action": "verify_cross_platform_ci"}


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def read(self, name):
        return json.loads((self.root / name / "attempt.json").read_text())

    def symlink(self, path, target, *, directory=False):
        try:
            path.symlink_to(target, target_is_directory=directory)
        except (OSError, NotImplementedError):
            self.skipTest("Symlink creation is unavailable on this host")

    def test_rejection_keeps_evidence_raw_answer_proposal_expected_and_specific_reasons(self):
        original = result(rejected=True)
        write_attempt(self.root / "rejected", original)
        record = self.read("rejected")
        for field in ("evidence", "context", "proposal", "validation_error", "blockers", "expected", "expected_decisions", "expected_next_action"):
            self.assertEqual(record[field], original[field])
        self.assertEqual(record["answer"]["content"], original["answer"]["content"])
        self.assertEqual(record["model_call"], {"attempted": True, "completed": True})
        summary = summarize_attempts(self.root)
        self.assertEqual(summary["rejected"], 1)
        self.assertEqual(summary["completions"], 1)
        self.assertEqual(summary["rejection_reasons"], {"decision_mismatch": 1, "next_action_mismatch": 1})
        self.assertEqual(summary["rejection_categories"], {"model_plan_rejected": 1})
        self.assertIsNone(summary["manual_correctness_rate"])
        self.assertEqual(summary["assessments"]["unreviewed"], 1)

    def test_transport_error_keeps_partial_evidence_and_unknown_usage_is_not_zero(self):
        write_attempt(self.root / "completed", result())
        partial = {"evidence": {"head": "abc"}, "context": {"text": "before transport"},
                   "model_call": {"attempted": True, "completed": False}, "latency_ms": 100.0}
        write_attempt(self.root / "transport", partial, error_code="model_transport_error")
        record = self.read("transport")
        self.assertEqual(record["context"], partial["context"])
        self.assertIsNone(record["answer"])
        self.assertFalse(record["model_call"]["completed"])
        summary = summarize_attempts(self.root)
        self.assertEqual((summary["attempts"], summary["model_calls"], summary["completions"], summary["errors"]), (2, 2, 1, 1))
        self.assertEqual(summary["error_codes"], {"model_transport_error": 1})
        self.assertEqual(summary["usage"]["known_prompt_tokens"], 20)
        self.assertEqual(summary["usage"]["known_completion_tokens"], 5)
        self.assertEqual(summary["usage"]["unknown_usage_calls"], 1)
        self.assertIsNone(summary["usage"]["prompt_tokens"])
        self.assertIsNone(summary["usage"]["completion_tokens"])
        self.assertIsNone(summary["priced_cost"])
        self.assertEqual(summary["latency_ms"]["measured_calls"], 2)

    def test_no_call_is_free_but_unknown_call_is_not_assumed_free(self):
        no_call = {"model_call": {"attempted": False, "completed": False},
                   "validation_error": "required_decisions_missing"}
        write_attempt(self.root / "preflight", no_call)
        summary = summarize_attempts(self.root)
        self.assertEqual(summary["model_calls"], 0)
        self.assertEqual(summary["priced_cost"], 0)
        self.assertEqual(summary["usage"]["prompt_tokens"], 0)
        self.assertEqual(summary["prepared"], 1)
        self.assertEqual(summary["rejected"], 0)
        write_attempt(self.root / "unknown", None, error_code="attempt_failed")
        summary = summarize_attempts(self.root)
        self.assertEqual(summary["unknown_model_calls"], 1)
        self.assertIsNone(summary["priced_cost"])
        self.assertIsNone(summary["usage"]["prompt_tokens"])

    def test_readiness_rejection_keeps_blockers_details(self):
        original = result(rejected=True)
        original["validation_error"]["errors"] = [{"code":"readiness_contradicts_current_evidence",
            "blockers":["worktree_dirty_or_unknown", "current_source_ci_not_verified"]}]
        write_attempt(self.root / "readiness", original)
        self.assertEqual(self.read("readiness")["validation_error"], original["validation_error"])
        self.assertEqual(summarize_attempts(self.root)["rejection_reasons"], {"readiness_contradicts_current_evidence":1})
        original["validation_error"]["errors"][0]["blockers"] = "invalid"
        with self.assertRaises(ValueError):
            write_attempt(self.root / "invalid", original)

    def test_preparation_and_unknown_calls_are_not_model_rejections_or_assessable(self):
        write_attempt(self.root / "prepared", {"context":{"text":"inputs only"},
            "model_call":{"attempted":False,"completed":False}})
        write_attempt(self.root / "unknown", {"context":{"text":"call status unknown"}})
        summary = summarize_attempts(self.root)
        self.assertEqual((summary["prepared"], summary["incomplete"], summary["rejected"]), (1,1,0))
        self.assertEqual(summary["unknown_model_calls"], 1)
        self.assertEqual(summary["rejection_reasons"], {})
        for directory in ("prepared", "unknown"):
            with self.assertRaises(ValueError):
                record_assessment(self.root / directory, "correct_rejection")

    def test_missing_invalid_usage_and_nonfinite_latency_stay_unknown(self):
        write_attempt(self.root / "empty_usage", result(usage={}, latency=float("inf")))
        write_attempt(self.root / "bad_usage", result(usage={"prompt_tokens": True, "completion_tokens": -2}, latency=float("nan")))
        summary = summarize_attempts(self.root)
        self.assertEqual(summary["usage"]["unknown_usage_calls"], 2)
        self.assertEqual(summary["latency_ms"]["measured_calls"], 0)
        self.assertEqual(summary["latency_ms"]["unknown_calls"], 2)
        self.assertIsNone(summary["latency_ms"]["mean"])
        self.assertIsNone(summary["latency_ms"]["total"])
        self.assertIsNone(self.read("bad_usage")["latency_ms"])

    def test_acceptance_and_rejection_are_separate_from_human_correctness(self):
        rows = (("good", False, "correct_completion"), ("bad", False, "incorrect_acceptance"),
                ("blocked", True, "correct_rejection"), ("overblocked", True, "incorrect_rejection"))
        for name, rejected, outcome in rows:
            write_attempt(self.root / name, result(rejected=rejected))
            record_assessment(self.root / name, outcome)
        write_attempt(self.root / "unreviewed", result())
        summary = summarize_attempts(self.root)
        self.assertEqual((summary["accepted"], summary["rejected"]), (3, 2))
        self.assertEqual(summary["manual_reviewed"], 4)
        self.assertEqual(summary["manual_correctness_rate"], 0.5)
        self.assertEqual(summary["assessments"], {"correct_completion":1, "incorrect_acceptance":1,
            "correct_rejection":1, "incorrect_rejection":1, "unreviewed":1})

    def test_attempt_and_assessment_cannot_be_overwritten(self):
        write_attempt(self.root / "a", result())
        before = (self.root / "a/attempt.json").read_bytes()
        with self.assertRaises(FileExistsError):
            write_attempt(self.root / "a", result(rejected=True))
        self.assertEqual((self.root / "a/attempt.json").read_bytes(), before)
        (self.root / "already-empty").mkdir()
        with self.assertRaises(FileExistsError):
            write_attempt(self.root / "already-empty", result())
        record_assessment(self.root / "a", "correct_completion")
        with self.assertRaises(FileExistsError):
            record_assessment(self.root / "a", "incorrect_acceptance")
        with self.assertRaises(ValueError):
            record_assessment(self.root / "a", "correct_rejection")
        self.assertFalse(list((self.root / "a").glob(".journal-*")))

    def test_explicit_fields_do_not_capture_headers_env_requests_or_exception_text(self):
        original = result()
        original.update(headers={"Authorization":"TOP_SECRET"}, env={"MOONSHOT_API_KEY":"TOP_SECRET"})
        original["answer"].update(request={"headers":{"Authorization":"TOP_SECRET"}}, api_key="TOP_SECRET")
        original["evidence"]["headers"] = {"Authorization":"TOP_SECRET"}
        write_attempt(self.root / "safe", original)
        self.assertNotIn("TOP_SECRET", (self.root / "safe/attempt.json").read_text())
        class DangerousError(Exception):
            def __str__(self):
                raise AssertionError("Exception text must never be read")
        with self.assertRaises(ValueError):
            write_attempt(self.root / "unsafe", None, error_code=DangerousError())
        self.assertFalse((self.root / "unsafe").exists())

    def test_only_direct_child_attempts_are_counted(self):
        write_attempt(self.root / "direct", result())
        write_attempt(self.root / "group/nested", result())
        (self.root / "ignored.json").write_text("not a journal record")
        self.assertEqual(summarize_attempts(self.root)["attempts"], 1)

    def test_symlink_directory_and_record_are_rejected(self):
        write_attempt(self.root / "a", result())
        self.symlink(self.root / "alias", self.root / "a", directory=True)
        with self.assertRaises(ValueError):
            summarize_attempts(self.root)
        with self.assertRaises(ValueError):
            write_attempt(self.root / "alias/new", result())
        (self.root / "alias").unlink()
        (self.root / "b").mkdir()
        self.symlink(self.root / "b/attempt.json", self.root / "a/attempt.json")
        with self.assertRaises(ValueError):
            summarize_attempts(self.root)

    def test_malformed_duplicate_or_inconsistent_records_are_rejected(self):
        write_attempt(self.root / "a", result())
        path = self.root / "a/attempt.json"
        valid = json.loads(path.read_text())
        for contents in ("not-json", '{"schema_version":1,"schema_version":1}',
                         json.dumps({**valid, "status":"rejected"}), json.dumps({**valid, "usage": {"prompt_tokens":0}}),
                         json.dumps({**valid, "usage": {"prompt_tokens":True,"completion_tokens":5,"total_tokens":25}}),
                         json.dumps({**valid, "recorded_at":"invalid-private-string"})):
            path.write_text(contents)
            with self.assertRaises(ValueError):
                summarize_attempts(self.root)
        path.write_text(json.dumps(valid))
        (self.root / "a/assessment.json").write_text('{"outcome":"correct_completion"}')
        with self.assertRaises(ValueError):
            summarize_attempts(self.root)

    def test_empty_journal_has_no_quality_claim_and_zero_cost(self):
        summary = summarize_attempts(self.root)
        self.assertEqual(summary["attempts"], 0)
        self.assertEqual(summary["priced_cost"], 0)
        self.assertIsNone(summary["manual_correctness_rate"])


if __name__ == "__main__":
    unittest.main()
