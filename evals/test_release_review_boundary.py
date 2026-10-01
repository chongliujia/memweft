"""Offline checks for the boundary between CI validation and human review.

Replaying a recorded answer verifies the guard, not a new prompt's model quality.
The fixture is self-contained and never reads local pilot data or credentials.
"""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import kimi_release_agent as release
from release_pilot_journal import summarize_attempts, write_attempt


# Exact answer from the real internal task's 2026-10-01 after-acceptance stage.
AFTER_ACCEPTANCE_CONTENT = (
    '{"target":"developer_preview","delivery":"downloadable_artifacts",'
    '"next_action":"verify_cross_platform_ci","ready":false,"reason":'
    '"CI总结果通过但各矩阵作业与产物未独立核验，需人工审核。"}'
)


def answer_for(content):
    return {"content": content, "finish_reason": "stop", "model": "offline-fixture",
            "usage": {"prompt_tokens": 20, "completion_tokens": 7, "total_tokens": 27},
            "latency_ms": 2.0}


def proposal_for(action="review_release_evidence", *, ready=True, target="developer_preview"):
    return {"target": target, "delivery": "downloadable_artifacts",
            "next_action": action, "ready": ready, "reason": "请按当前证据完成下一步审核。"}


class ReleaseReviewBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.agent = release.ReleaseAgent(self.root / "memory.db", self.root)
        self.agent.remember("target", "developer_preview")
        self.agent.remember("delivery", "downloadable_artifacts")
        self.evidence = {"head": "a" * 40, "captured_at": "2026-10-01T00:00:00+00:00",
                         "worktree_dirty": False, "ci_for_current_source": "passed",
                         "business_pilot": "not_verified",
                         "local_artifacts_match_manifest": True,
                         "local_artifacts_verified_for_source": True}

    def prepare(self, **overrides):
        return self.agent.prepare("继续发布准备。", evidence={**self.evidence, **overrides})

    def plan(self, prepared, *, proposal=None, content=None):
        answer = answer_for(content if content is not None else json.dumps(
            proposal_for() if proposal is None else proposal, ensure_ascii=False))
        client = Mock()
        client.complete.return_value = answer
        result = self.agent.plan_prepared(client, prepared)
        client.complete.assert_called_once_with(prepared["messages"], max_tokens=256)
        self.assertEqual(result["executed_actions"], [])
        self.assertEqual(result["answer"], answer)
        return result

    def assert_review_cannot_skip(self, prepared, expected_action):
        self.assertEqual(prepared["expected_next_action"], expected_action)
        result = self.plan(prepared)
        details = result["validation_error"]["errors"]
        self.assertIn({"code": "next_action_contradicts_rules", "field": "next_action",
                       "expected": expected_action, "actual": "review_release_evidence"}, details)
        self.assertIn("readiness_contradicts_current_evidence", [item["code"] for item in details])
        with self.assertRaises(ValueError):
            release.render_plan(result)
        return result

    def test_review_is_next_when_matrix_details_are_absent_unknown_or_unverified(self):
        variants = [
            {},
            {"github_ci": {}},
            {"github_ci": {"matrix_jobs_verified": None, "release_artifacts_verified": None}},
            {"github_ci": {"matrix_jobs_verified": False, "release_artifacts_verified": False}},
            {"github_ci": {"matrix_jobs_verified": True, "release_artifacts_verified": True}},
        ]
        for overrides in variants:
            with self.subTest(overrides=overrides):
                prepared = self.prepare(**overrides)
                self.assertEqual(prepared["expected_next_action"], "review_release_evidence")
                self.assertEqual(prepared["blockers"], [])
                self.assertEqual(prepared["decision_validation_errors"], [])
                result = self.plan(prepared)
                self.assertIsNone(result["validation_error"])

    def test_recorded_wrong_action_is_rejected_without_rewriting_answer_or_snapshot(self):
        prepared = self.prepare()
        original = json.dumps(prepared, sort_keys=True)
        result = self.plan(prepared, content=AFTER_ACCEPTANCE_CONTENT)
        self.assertEqual(result["validation_error"], {
            "code": "model_plan_rejected", "errors": [{
                "code": "next_action_contradicts_rules", "field": "next_action",
                "expected": "review_release_evidence", "actual": "verify_cross_platform_ci"}]})
        self.assertEqual(result["answer"]["content"], AFTER_ACCEPTANCE_CONTENT)
        self.assertEqual(result["proposal"], json.loads(AFTER_ACCEPTANCE_CONTENT))
        self.assertEqual(result["blockers"], [])
        self.assertEqual(json.dumps(prepared, sort_keys=True), original)
        with self.assertRaises(ValueError):
            release.render_plan(result)

    def test_recorded_rejection_journal_preserves_reason_and_separate_human_review(self):
        result = self.plan(self.prepare(), content=AFTER_ACCEPTANCE_CONTENT)
        runs = self.root / "runs"
        path = write_attempt(runs / "after-acceptance", result)
        record = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "rejected")
        self.assertEqual(record["answer"]["content"], AFTER_ACCEPTANCE_CONTENT)
        self.assertEqual(record["validation_error"], result["validation_error"])
        summary = summarize_attempts(runs)
        self.assertEqual(summary["attempts"], 1)
        self.assertEqual(summary["model_calls"], 1)
        self.assertEqual(summary["rejected"], 1)
        self.assertEqual(summary["accepted"], 0)
        self.assertEqual(summary["rejection_reasons"], {"next_action_contradicts_rules": 1})
        self.assertEqual(summary["rejection_categories"], {"model_plan_rejected": 1})
        self.assertEqual(summary["assessments"]["unreviewed"], 1)
        self.assertEqual(summary["manual_reviewed"], 0)
        self.assertIsNone(summary["manual_correctness_rate"])
        self.assertFalse((path.parent / "assessment.json").exists())

    def test_correct_review_and_ready_are_accepted_and_renderable(self):
        result = self.plan(self.prepare())
        self.assertIsNone(result["validation_error"])
        self.assertTrue(result["proposal"]["ready"])
        self.assertEqual(result["proposal"]["next_action"], "review_release_evidence")
        self.assertTrue(release.render_plan(result))
        runs = self.root / "accepted-runs"
        write_attempt(runs / "review", result)
        summary = summarize_attempts(runs)
        self.assertEqual(summary["accepted"], 1)
        self.assertEqual(summary["manual_reviewed"], 0)

    def test_review_cannot_skip_pending_failed_or_unavailable_ci(self):
        for status in ("pending", "failed", "unavailable"):
            with self.subTest(status=status):
                prepared = self.prepare(ci_for_current_source=status)
                self.assert_review_cannot_skip(prepared, "verify_cross_platform_ci")
                valid = self.plan(prepared, proposal=proposal_for("verify_cross_platform_ci", ready=False))
                self.assertIsNone(valid["validation_error"])

    def test_review_cannot_skip_dirty_or_unknown_worktree(self):
        for dirty in (True, None):
            with self.subTest(dirty=dirty):
                prepared = self.prepare(worktree_dirty=dirty)
                self.assert_review_cannot_skip(prepared, "verify_cross_platform_ci")

    def test_review_cannot_skip_missing_changed_or_old_source_packages(self):
        for overrides in (
            {"local_artifacts_match_manifest": False},
            {"local_artifacts_match_manifest": None},
            {"local_artifacts_verified_for_source": False},
            {"local_artifacts_verified_for_source": None},
        ):
            with self.subTest(overrides=overrides):
                prepared = self.prepare(**overrides)
                self.assert_review_cannot_skip(prepared, "verify_release_artifacts")
                valid = self.plan(prepared, proposal=proposal_for("verify_release_artifacts", ready=False))
                self.assertIsNone(valid["validation_error"])

    def test_ci_precedes_package_verification_when_both_are_missing(self):
        prepared = self.prepare(ci_for_current_source="pending",
                                local_artifacts_verified_for_source=False)
        self.assertEqual(prepared["expected_next_action"], "verify_cross_platform_ci")
        result = self.plan(prepared, proposal=proposal_for("verify_release_artifacts", ready=False))
        self.assertEqual(result["validation_error"]["errors"], [{
            "code": "next_action_contradicts_rules", "field": "next_action",
            "expected": "verify_cross_platform_ci", "actual": "verify_release_artifacts"}])

    def test_production_requires_business_pilot_before_ci_and_artifacts(self):
        self.agent.remember("target", "bounded_production")
        for overrides in ({}, {"ci_for_current_source": "failed", "worktree_dirty": True,
                               "local_artifacts_verified_for_source": False}):
            with self.subTest(overrides=overrides):
                prepared = self.prepare(**overrides)
                self.assertEqual(prepared["expected_next_action"], "run_business_pilot")
                self.assertIn("business_pilot_not_verified", prepared["blockers"])
                correct = self.plan(prepared, proposal=proposal_for(
                    "run_business_pilot", ready=False, target="bounded_production"))
                self.assertIsNone(correct["validation_error"])
                wrong = self.plan(prepared, proposal=proposal_for(target="bounded_production"))
                self.assertIn("next_action_contradicts_rules",
                              [item["code"] for item in wrong["validation_error"]["errors"]])

    def test_missing_release_decision_still_precedes_review(self):
        self.agent.forget("delivery")
        prepared = self.prepare()
        self.assertEqual(prepared["expected_next_action"], "clarify_decisions")
        self.assertIn("release_decisions_missing", prepared["blockers"])
        result = self.plan(prepared)
        self.assertIn("decision_contradicts_memory",
                      [item["code"] for item in result["validation_error"]["errors"]])


if __name__ == "__main__":
    unittest.main()
