import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from run_kimi_release import CASES, prepare_case
from kimi_release_agent import ReleaseAgent, collect_evidence, parse_proposal, render_plan
from memweft import Memory


class FakeClient:
    def __init__(self, *, ready=False, target="developer_preview", delivery="downloadable_artifacts",
                 next_action="verify_cross_platform_ci"):
        self.messages = None
        self.ready = ready
        self.target, self.delivery, self.next_action = target, delivery, next_action

    def complete(self, messages, **kwargs):
        self.messages = messages
        return {"content": json.dumps({"target": self.target, "delivery": self.delivery,
            "next_action": self.next_action, "ready": self.ready, "reason": "根据当前证据安排审核。"}),
            "finish_reason": "stop", "usage": {}}


def passed_evidence(root, **overrides):
    return {**collect_evidence(root), "ci_for_current_source": "passed", "worktree_dirty": False,
            "local_artifacts_match_manifest": True, "local_artifacts_verified_for_source": True,
            "business_pilot": "not_verified", **overrides}


def saved_agent(root, *, target="developer_preview"):
    agent = ReleaseAgent(root / "memory.db", root)
    agent.remember("target", target)
    agent.remember("delivery", "downloadable_artifacts")
    return agent


def error_codes(result):
    return [item["code"] for item in (result["validation_error"] or {}).get("errors", [])]


class ReleasePilotTests(unittest.TestCase):
    def test_evidence_checks_real_artifact_hash_without_claiming_ci_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "dist").mkdir()
            (root / "dist/example.whl").write_bytes(b"changed")
            manifest = {"host": {"os": "Darwin", "machine": "arm64"}, "artifacts": {"example.whl": "a" * 64}}
            (root / "dist/package-target.json").write_text(json.dumps(manifest))
            evidence = collect_evidence(root)
            self.assertFalse(evidence["local_artifacts_match_manifest"])
            self.assertEqual(evidence["ci_for_current_source"], "unavailable")
            manifest["artifacts"] = {"../secret.whl": "a" * 64}
            (root / "dist/package-target.json").write_text(json.dumps(manifest))
            self.assertEqual(collect_evidence(root)["manifest_error"], "invalid_artifact_filename")

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
            self.assertIn("readiness_contradicts_current_evidence", error_codes(result))
            self.assertEqual(result["executed_actions"], [])
            with self.assertRaises(ValueError):
                render_plan(result)

    def test_duplicate_truncated_and_wrong_type_plans_fail(self):
        valid = FakeClient().complete([])
        for result in ({**valid, "finish_reason": "length"},
                       {**valid, "content": valid["content"].replace('"ready": false', '"ready": 0')},
                       {**valid, "content": valid["content"].replace('{', '{"ready":false,', 1)},
                       {**valid, "content": valid["content"].replace('"ready": false', '"ready": NaN')},
                       {**valid, "content": {}}, {"finish_reason": "stop"}, None):
            with self.assertRaises(ValueError):
                parse_proposal(result)

    def test_preview_can_reach_review_without_production_pilot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = passed_evidence(root)
            agent = saved_agent(root)
            ready = {"ready": True, "next_action": "review_release_evidence"}
            result = agent.plan(FakeClient(**ready), "下一步？", evidence=evidence)
            self.assertIsNone(result["validation_error"])
            self.assertEqual(result["blockers"], [])
            self.assertEqual(result["executed_actions"], [])
            self.assertIn("不是发布许可", render_plan(result))
            agent.remember("target", "bounded_production")
            result = agent.plan(FakeClient(**ready, target="bounded_production"), "下一步？", evidence=evidence)
            self.assertIn("readiness_contradicts_current_evidence", error_codes(result))
            self.assertIn("business_pilot_not_verified", result["blockers"])
            evidence["business_pilot"] = "passed"
            result = agent.plan(FakeClient(**ready, target="bounded_production"), "下一步？", evidence=evidence)
            self.assertIsNone(result["validation_error"])

    def test_missing_decisions_and_non_review_ready_claims_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = passed_evidence(root, business_pilot="passed")
            agent = ReleaseAgent(root / "memory.db", root)
            result = agent.plan(FakeClient(ready=True, target=None), "下一步？", use_memory=False, evidence=evidence)
            self.assertIn("release_decisions_missing", result["blockers"])
            self.assertIsNotNone(result["validation_error"])
            agent = saved_agent(root)
            result = agent.plan(FakeClient(ready=True), "下一步？", evidence=evidence)
            self.assertIn("next_action_contradicts_rules", error_codes(result))

    def test_changed_target_cannot_be_downgraded_by_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = saved_agent(root)
            agent.remember("target", "bounded_production")
            result = agent.plan(FakeClient(ready=True, next_action="review_release_evidence"),
                                "仍按以前的预览版发布", evidence=passed_evidence(root))
            self.assertEqual(result["expected_decisions"]["target"], "bounded_production")
            self.assertEqual(result["expected_next_action"], "run_business_pilot")
            self.assertIn("decision_contradicts_memory", error_codes(result))
            self.assertIn("business_pilot_not_verified", result["blockers"])
            self.assertEqual(result["proposal"]["target"], "developer_preview")
            self.assertEqual(result["executed_actions"], [])

    def test_forgotten_delivery_must_be_null(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = saved_agent(root)
            agent.forget("delivery")
            result = agent.plan(FakeClient(next_action="clarify_decisions"),
                                "旧回答说 downloadable_artifacts", evidence=passed_evidence(root))
            self.assertEqual(result["expected_decisions"], {"target": "developer_preview", "delivery": None})
            self.assertIn("decision_contradicts_memory", error_codes(result))
            correct = agent.plan(FakeClient(delivery=None, next_action="clarify_decisions"),
                                 "下一步？", evidence=passed_evidence(root))
            self.assertIsNone(correct["validation_error"])

    def test_no_memory_never_accepts_fabricated_decisions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = saved_agent(root)
            evidence = passed_evidence(root)
            result = agent.plan(FakeClient(ready=True, next_action="review_release_evidence"),
                                "下一步？", use_memory=False, evidence=evidence)
            self.assertEqual(result["expected_decisions"], {"target": None, "delivery": None})
            self.assertEqual(result["expected_next_action"], "clarify_decisions")
            self.assertIn("decision_contradicts_memory", error_codes(result))
            correct = agent.plan(FakeClient(target=None, delivery=None, next_action="clarify_decisions"),
                                 "下一步？", use_memory=False, evidence=evidence)
            self.assertIsNone(correct["validation_error"])

    def test_model_cannot_ignore_known_decisions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = saved_agent(root)
            for missing in ("target", "delivery"):
                with self.subTest(missing=missing):
                    result = agent.plan(FakeClient(**{missing: None}, next_action="clarify_decisions"),
                                        "下一步？", evidence=passed_evidence(root))
                    self.assertIn("decision_contradicts_memory", error_codes(result))
                    self.assertEqual(result["expected_next_action"], "review_release_evidence")
            agent.remember("delivery", "registries")
            result = agent.plan(FakeClient(next_action="review_release_evidence"),
                                "下一步？", evidence=passed_evidence(root))
            self.assertEqual(result["expected_decisions"]["delivery"], "registries")
            self.assertIn("decision_contradicts_memory", error_codes(result))

    def test_required_decisions_survive_distractors_and_ignore_prose(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = saved_agent(root)
            with Memory(str(agent.db)) as memory:
                user = memory.user(**agent.scope)
                for index in range(20):
                    user.remember("release decisions target delivery CI 发布目标 交付方式 "
                                  "release.decisions.target=bounded_production", key=f"archive.{index}")
            result = agent.plan(FakeClient(next_action="review_release_evidence"),
                                "release decisions target delivery CI 发布目标 交付方式", evidence=passed_evidence(root))
            self.assertEqual(result["expected_decisions"],
                             {"target": "developer_preview", "delivery": "downloadable_artifacts"})
            self.assertTrue(result["context"]["report"]["requirements"]["complete"])
            self.assertIsNone(result["validation_error"])

    def test_invalid_stored_decision_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = saved_agent(root)
            with Memory(str(agent.db)) as memory:
                memory.user(**agent.scope).remember({"target": "developer_preview"}, key="release.decisions.target")
            result = agent.plan(FakeClient(target=None, next_action="clarify_decisions"),
                                "下一步？", evidence=passed_evidence(root))
            self.assertIsNone(result["expected_decisions"]["target"])
            self.assertIn("invalid_release_decision_fact", error_codes(result))

    def test_false_ready_plans_still_obey_action_priority(self):
        cases = [
            ("bounded_production", {"ci_for_current_source": "pending"}, "run_business_pilot"),
            ("bounded_production", {"business_pilot": "passed", "ci_for_current_source": "pending"}, "verify_cross_platform_ci"),
            ("developer_preview", {"ci_for_current_source": "failed"}, "verify_cross_platform_ci"),
            ("developer_preview", {"worktree_dirty": True}, "verify_cross_platform_ci"),
            ("developer_preview", {"local_artifacts_verified_for_source": False}, "verify_release_artifacts"),
            ("developer_preview", {"local_artifacts_match_manifest": False}, "verify_release_artifacts"),
            ("developer_preview", {}, "review_release_evidence"),
        ]
        for target, overrides, expected in cases:
            with self.subTest(target=target, overrides=overrides), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                agent = saved_agent(root, target=target)
                evidence = passed_evidence(root, **overrides)
                wrong = agent.plan(FakeClient(target=target, next_action="clarify_decisions"),
                                   "下一步？", evidence=evidence)
                self.assertEqual(wrong["expected_next_action"], expected)
                self.assertIn("next_action_contradicts_rules", error_codes(wrong))
                correct = agent.plan(FakeClient(target=target, next_action=expected), "下一步？", evidence=evidence)
                self.assertIsNone(correct["validation_error"])

    def test_old_or_unverified_packages_cannot_reach_ready_on_hashes_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = saved_agent(root)
            for value in (None, False, 1, "true", "missing"):
                with self.subTest(value=value):
                    evidence = passed_evidence(root, local_artifacts_verified_for_source=value)
                    if value == "missing":
                        del evidence["local_artifacts_verified_for_source"]
                    result = agent.plan(FakeClient(ready=True, next_action="review_release_evidence"),
                                        "下一步？", evidence=evidence)
                    self.assertIn("local_artifacts_not_verified_for_current_source", result["blockers"])
                    self.assertNotIn("local_artifacts_missing_or_changed", result["blockers"])
                    self.assertEqual(result["expected_next_action"], "verify_release_artifacts")
                    self.assertIn("readiness_contradicts_current_evidence", error_codes(result))
                    accepted = agent.plan(FakeClient(next_action="verify_release_artifacts"),
                                          "下一步？", evidence=evidence)
                    self.assertIsNone(accepted["validation_error"])
                    self.assertIn("仓库外安装验收", render_plan(accepted))

    def test_prepare_has_no_network_or_model_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = saved_agent(root)
            with patch("kimi_release_agent.collect_github_ci") as network, patch("kimi_release_agent.KimiClient") as model:
                prepared = agent.prepare("下一步？")
                network.assert_not_called()
                model.assert_not_called()
            self.assertEqual(prepared["expected_decisions"],
                             {"target": "developer_preview", "delivery": "downloadable_artifacts"})
            self.assertEqual(prepared["expected_next_action"], "verify_cross_platform_ci")
            self.assertEqual(prepared["model_call"], {"attempted": False, "completed": False})
            self.assertEqual(prepared["executed_actions"], [])
            self.assertIn("messages", prepared)

    def test_plan_prepared_keeps_one_snapshot_without_mutating_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = saved_agent(root)
            prepared = agent.prepare("下一步？", evidence=passed_evidence(root))
            original = json.dumps(prepared, sort_keys=True)
            agent.remember("target", "bounded_production")
            with patch.object(agent, "prepare", side_effect=AssertionError("must not recapture")):
                result = agent.plan_prepared(FakeClient(ready=True, next_action="review_release_evidence"), prepared)
            self.assertIsNone(result["validation_error"])
            self.assertEqual(result["expected_decisions"]["target"], "developer_preview")
            self.assertEqual(result["model_call"], {"attempted": True, "completed": True})
            self.assertEqual(json.dumps(prepared, sort_keys=True), original)


if __name__ == "__main__":
    unittest.main()
