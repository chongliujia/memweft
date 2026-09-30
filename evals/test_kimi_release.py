import json
from pathlib import Path
import tempfile
import unittest

from run_kimi_release import CASES, prepare_case
from kimi_release_agent import ReleaseAgent, collect_evidence, parse_proposal, render_plan


class FakeClient:
    def __init__(self, *, ready=False, target="developer_preview", next_action="verify_cross_platform_ci"):
        self.messages = None
        self.ready = ready
        self.target, self.next_action = target, next_action

    def complete(self, messages, **kwargs):
        self.messages = messages
        return {"content": json.dumps({"target": self.target, "delivery": "downloadable_artifacts",
            "next_action": self.next_action, "ready": self.ready, "reason": "根据当前证据安排审核。"}),
            "finish_reason": "stop", "usage": {}}


class ReleasePilotTests(unittest.TestCase):
    def test_evidence_checks_real_artifact_hash_without_claiming_ci_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "dist").mkdir()
            (root / "dist/example.whl").write_bytes(b"changed")
            (root / "dist/package-target.json").write_text(json.dumps({"artifacts": {"example.whl": "old"}}))
            evidence = collect_evidence(root)
            self.assertFalse(evidence["local_artifacts_match_manifest"])
            self.assertEqual(evidence["ci_for_current_source"], "unavailable")
            (root / "dist/package-target.json").write_text(json.dumps({"artifacts": {"../secret.whl": "old"}}))
            self.assertEqual(collect_evidence(root)["manifest_error"], "manifest_missing_or_invalid")

    def test_updates_forgetting_and_scope_isolation_survive_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeClient()
            for case in CASES:
                agent = prepare_case(root, root, case)
                result = agent.plan(client, "下一步？", session="previous")
                text = result["context"]["text"]
                if case["id"] == "change_target":
                    self.assertIn("bounded_production", text)
                    self.assertNotIn("developer_preview", text)
                elif case["id"] == "forget_delivery":
                    self.assertNotIn("downloadable_artifacts", text)
                    self.assertFalse(result["context"]["messages"])
                elif case["id"] == "other_user":
                    self.assertNotIn("developer_preview", text)
                    self.assertNotIn("downloadable_artifacts", text)
                elif case["id"] == "stale_ci_note":
                    self.assertIn("上周 CI", text)
                agent.plan(client, "下一步？", use_memory=False)
                self.assertEqual([item["role"] for item in client.messages], ["system", "system", "user"])

    def test_project_binding_and_no_model_answer_writeback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = ReleaseAgent(root / "memory.db", root / "project-a")
            agent.remember("target", "developer_preview")
            other = ReleaseAgent(agent.db, root / "project-b")
            self.assertNotIn("developer_preview", other.plan(FakeClient(), "下一步？")["context"]["text"])
            agent.plan(FakeClient(), "下一步？")
            agent.forget("target")
            self.assertNotIn("developer_preview", agent.plan(FakeClient(), "下一步？")["context"]["text"])

    def test_stale_ready_claim_is_rejected_without_executing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = ReleaseAgent(root / "memory.db", root).plan(FakeClient(ready=True), "下一步？")
            self.assertEqual(result["validation_error"], "readiness_contradicts_current_evidence")
            self.assertEqual(result["executed_actions"], [])
            with self.assertRaises(ValueError):
                render_plan(result)

    def test_duplicate_truncated_and_wrong_type_plans_fail(self):
        valid = FakeClient().complete([])
        for result in ({**valid, "finish_reason": "length"},
                       {**valid, "content": valid["content"].replace('"ready": false', '"ready": 0')},
                       {**valid, "content": valid["content"].replace('{', '{"ready":false,', 1)}):
            with self.assertRaises(ValueError):
                parse_proposal(result)

    def test_preview_can_reach_review_without_production_pilot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = {**collect_evidence(root), "ci_for_current_source": "passed", "worktree_dirty": False,
                        "local_artifacts_match_manifest": True, "business_pilot": "not_verified"}
            agent = ReleaseAgent(root / "memory.db", root)
            ready = {"ready": True, "next_action": "review_release_evidence"}
            result = agent.plan(FakeClient(**ready), "下一步？", use_memory=False, evidence=evidence)
            self.assertIsNone(result["validation_error"])
            self.assertEqual(result["blockers"], [])
            self.assertEqual(result["executed_actions"], [])
            self.assertIn("不是发布许可", render_plan(result))
            result = agent.plan(FakeClient(**ready, target="bounded_production"), "下一步？", use_memory=False, evidence=evidence)
            self.assertEqual(result["validation_error"], "readiness_contradicts_current_evidence")
            self.assertIn("business_pilot_not_verified", result["blockers"])
            evidence["business_pilot"] = "passed"
            result = agent.plan(FakeClient(**ready, target="bounded_production"), "下一步？", use_memory=False, evidence=evidence)
            self.assertIsNone(result["validation_error"])

    def test_missing_decisions_and_non_review_ready_claims_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = {**collect_evidence(root), "ci_for_current_source": "passed", "worktree_dirty": False,
                        "local_artifacts_match_manifest": True, "business_pilot": "passed"}
            agent = ReleaseAgent(root / "memory.db", root)
            result = agent.plan(FakeClient(ready=True, target=None), "下一步？", use_memory=False, evidence=evidence)
            self.assertIn("release_decisions_missing", result["blockers"])
            self.assertIsNotNone(result["validation_error"])
            result = agent.plan(FakeClient(ready=True), "下一步？", use_memory=False, evidence=evidence)
            self.assertEqual(result["validation_error"], "readiness_requires_evidence_review")


if __name__ == "__main__":
    unittest.main()
