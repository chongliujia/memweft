"""Offline regression tests for actionable release-proposal rejection details."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import kimi_release_agent as release
from release_pilot_journal import summarize_attempts, write_attempt


# Exact content from the 2026-10-01 old-package pilot completion. Keep the
# regression independent of the ignored local pilot journal and credentials.
LIVE_85_CONTENT = (
    '{"target":"developer_preview","delivery":"downloadable_artifacts",'
    '"next_action":"verify_release_artifacts","ready":false,"reason":'
    '"当前源码CI已通过且工作区干净，但发布产物尚未核验为当前源码构建'
    '（local_artifacts_verified_for_source=false），需先完成产物验证。"}'
)


def valid_proposal(**overrides):
    return {"target": "developer_preview", "delivery": "downloadable_artifacts",
            "next_action": "verify_release_artifacts", "ready": False,
            "reason": "需要核对当前源码的安装包。", **overrides}


def answer_for(proposal=None, *, content=None, finish_reason="stop"):
    return {"content": json.dumps(valid_proposal() if proposal is None else proposal,
                                   ensure_ascii=False) if content is None else content,
            "finish_reason": finish_reason, "model": "offline-fixture",
            "usage": {"prompt_tokens": 20, "completion_tokens": 7, "total_tokens": 27},
            "latency_ms": 3.5}


def diagnostic_cases():
    """One or two representative completions for each public diagnostic code."""
    return [
        ("reason_too_long", answer_for(content=LIVE_85_CONTENT), "reason"),
        ("reason_empty", answer_for(valid_proposal(reason="")), "reason"),
        ("invalid_reason_type", answer_for(valid_proposal(reason=80)), "reason"),
        ("invalid_next_action", answer_for(valid_proposal(next_action="publish")), "next_action"),
        ("invalid_ready", answer_for(valid_proposal(ready=1)), "ready"),
        ("invalid_decision", answer_for(valid_proposal(target="production")), "target"),
        ("invalid_decision", answer_for(valid_proposal(delivery=[])), "delivery"),
        ("wrong_fields", answer_for(valid_proposal(extra=True)), None),
        ("malformed_json", answer_for(content="{"), None),
        ("duplicate_field", answer_for(content='{"ready":false,' + answer_for()["content"][1:]), None),
        ("invalid_json_constant", answer_for(content=answer_for()["content"].replace('"ready": false', '"ready": NaN')), None),
        ("incomplete_completion", answer_for(finish_reason="length"), None),
        ("invalid_content", {**answer_for(), "content": {}}, None),
    ]


class ProposalParsingDiagnosticsTests(unittest.TestCase):
    def diagnostic(self, answer, code, *, field=None):
        with self.assertRaises(release.ProposalValidationError) as caught:
            release.parse_proposal(answer)
        self.assertIsInstance(caught.exception, ValueError)
        errors = caught.exception.errors
        self.assertIsInstance(errors, list)
        self.assertTrue(errors)
        self.assertTrue(all(isinstance(item, dict) for item in errors))
        matching = [item for item in errors if item.get("code") == code]
        self.assertEqual(len(matching), 1, errors)
        if field is not None:
            self.assertEqual(matching[0].get("field"), field)
        return matching[0]

    def test_error_remains_value_error_compatible(self):
        self.assertTrue(issubclass(release.ProposalValidationError, ValueError))
        with self.assertRaises(ValueError):
            release.parse_proposal(answer_for(content=LIVE_85_CONTENT))

    def test_reason_boundaries_count_characters_not_encoded_bytes(self):
        for label, source in (("chinese", "中" * 85), ("ascii", "A" * 85),
                              ("emoji", "🙂" * 85), ("mixed", "中A🙂" * 29)):
            for length in (80, 81, 85):
                with self.subTest(kind=label, length=length):
                    reason = source[:length]
                    self.assertEqual(len(reason), length)
                    answer = answer_for(valid_proposal(reason=reason))
                    if length == 80:
                        self.assertEqual(release.parse_proposal(answer)["reason"], reason)
                    else:
                        detail = self.diagnostic(answer, "reason_too_long", field="reason")
                        self.assertEqual(detail, {"code": "reason_too_long", "field": "reason",
                                                  "expected": 80, "actual": length})

    def test_real_85_character_completion_has_precise_length_diagnostic(self):
        self.assertEqual(len(json.loads(LIVE_85_CONTENT)["reason"]), 85)
        self.assertEqual(self.diagnostic(answer_for(content=LIVE_85_CONTENT), "reason_too_long"),
                         {"code": "reason_too_long", "field": "reason", "expected": 80, "actual": 85})

    def test_each_public_diagnostic_has_a_specific_code(self):
        for code, answer, field in diagnostic_cases():
            with self.subTest(code=code, field=field):
                # Only the decision and reason-length field metadata is part
                # of this contract; other diagnostics may add useful details.
                self.diagnostic(answer, code, field=field if code == "invalid_decision" else None)

    def test_reason_type_is_distinct_from_empty_reason(self):
        for value in (None, True, 0, [], {}):
            with self.subTest(value=value):
                self.diagnostic(answer_for(valid_proposal(reason=value)), "invalid_reason_type")
        self.diagnostic(answer_for(valid_proposal(reason="")), "reason_empty")

    def test_decisions_actions_and_ready_keep_strict_types(self):
        for field in ("target", "delivery"):
            for value in (False, 1, [], {}, "unsupported"):
                with self.subTest(field=field, value=value):
                    self.diagnostic(answer_for(valid_proposal(**{field: value})),
                                    "invalid_decision", field=field)
            self.assertIsNone(release.parse_proposal(answer_for(valid_proposal(**{field: None})))[field])
        for value in (None, False, 1, [], "publish"):
            with self.subTest(action=value):
                self.diagnostic(answer_for(valid_proposal(next_action=value)), "invalid_next_action")
        for value in (None, 0, 1, "false", []):
            with self.subTest(ready=value):
                self.diagnostic(answer_for(valid_proposal(ready=value)), "invalid_ready")

    def test_json_shape_duplicates_and_constants_are_separate(self):
        missing = valid_proposal()
        del missing["reason"]
        for content in ("[]", "null", json.dumps(missing), json.dumps(valid_proposal(extra=True))):
            with self.subTest(content=content):
                self.diagnostic(answer_for(content=content), "wrong_fields")
        for content in ("{", '{"target":}', answer_for()["content"] + " trailing"):
            with self.subTest(content=content):
                self.diagnostic(answer_for(content=content), "malformed_json")
        duplicate = '{"ready":false,' + answer_for()["content"][1:]
        self.diagnostic(answer_for(content=duplicate), "duplicate_field")
        for constant in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(constant=constant):
                content = answer_for()["content"].replace('"ready": false', '"ready": ' + constant)
                self.diagnostic(answer_for(content=content), "invalid_json_constant")

    def test_incomplete_completion_and_non_text_content_are_separate(self):
        for answer in (None, {}, answer_for(finish_reason="length"), answer_for(finish_reason=None)):
            with self.subTest(answer=answer):
                self.diagnostic(answer, "incomplete_completion")
        for value in (None, {}, [], 1):
            with self.subTest(content=value):
                self.diagnostic({**answer_for(), "content": value}, "invalid_content")
        self.diagnostic({"finish_reason": "stop"}, "invalid_content")


class ProposalDiagnosticIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.agent = release.ReleaseAgent(self.root / "memory.db", self.root)
        self.agent.remember("target", "developer_preview")
        self.agent.remember("delivery", "downloadable_artifacts")
        self.evidence = {"head": "a" * 40, "captured_at": "2026-10-01T00:00:00+00:00",
                         "ci_for_current_source": "passed", "worktree_dirty": False,
                         "local_artifacts_match_manifest": True,
                         "local_artifacts_verified_for_source": False,
                         "business_pilot": "not_verified"}
        self.prepared = self.agent.prepare("下一步？", evidence=self.evidence)

    def plan(self, answer):
        client = Mock()
        client.complete.return_value = answer
        result = self.agent.plan_prepared(client, self.prepared)
        client.complete.assert_called_once_with(self.prepared["messages"], max_tokens=256)
        return result

    def test_specific_parse_errors_survive_guard_without_rendering_or_losing_answer(self):
        original_prepared = json.dumps(self.prepared, sort_keys=True)
        for code, answer, field in diagnostic_cases():
            with self.subTest(code=code, field=field):
                result = self.plan(answer)
                self.assertEqual(result["validation_error"]["code"], "invalid_or_incomplete_model_plan")
                self.assertIn(code, [detail["code"] for detail in result["validation_error"]["errors"]])
                self.assertEqual(result["answer"], answer)
                self.assertIsNone(result["proposal"])
                self.assertEqual(result["model_call"], {"attempted": True, "completed": True})
                self.assertEqual(result["executed_actions"], [])
                with self.assertRaises(ValueError):
                    release.render_plan(result)
        self.assertEqual(json.dumps(self.prepared, sort_keys=True), original_prepared)

    def test_journal_round_trip_and_summary_preserve_specific_rejection_reasons(self):
        runs = self.root / "runs"
        expected_reasons = {}
        for index, (code, answer, _) in enumerate(diagnostic_cases()):
            result = self.plan(answer)
            path = write_attempt(runs / str(index), result)
            record = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(record["validation_error"], result["validation_error"])
            self.assertEqual(record["answer"], answer)
            self.assertEqual(record["status"], "rejected")
            for field in ("evidence", "context", "blockers", "expected_decisions", "expected_next_action"):
                self.assertEqual(record[field], result[field])
            expected_reasons[code] = expected_reasons.get(code, 0) + 1
        summary = summarize_attempts(runs)  # Also revalidates all serialized records.
        count = len(diagnostic_cases())
        for key in ("attempts", "model_calls", "completions", "rejected"):
            self.assertEqual(summary[key], count)
        self.assertEqual(summary["accepted"], 0)
        self.assertEqual(summary["errors"], 0)
        self.assertEqual(summary["rejection_reasons"], expected_reasons)
        self.assertEqual(summary["rejection_categories"], {"invalid_or_incomplete_model_plan": count})
        self.assertEqual(summary["assessments"]["unreviewed"], count)
        self.assertIsNone(summary["manual_correctness_rate"])
        self.assertIsNone(summary["priced_cost"])

    def test_excessive_json_depth_is_rejected_and_journaled_with_raw_answer(self):
        depth = sys.getrecursionlimit() + 100
        raw = "[" * depth + "0" + "]" * depth
        answer = answer_for(content=raw)
        with self.assertRaises(release.ProposalValidationError) as caught:
            release.parse_proposal(answer)
        # Decoder depth limits vary across Python versions. Either decoding
        # fails safely, or the decoded array is rejected as a non-object plan.
        allowed_errors = ([{"code": "json_too_deep"}], [{"code": "wrong_fields"}])
        self.assertIn(caught.exception.errors, allowed_errors)
        result = self.plan(answer)
        self.assertEqual(result["validation_error"]["code"], "invalid_or_incomplete_model_plan")
        self.assertIn(result["validation_error"]["errors"], allowed_errors)
        code = result["validation_error"]["errors"][0]["code"]
        self.assertEqual(result["answer"]["content"], raw)
        self.assertIsNone(result["proposal"])
        with self.assertRaises(ValueError):
            release.render_plan(result)
        runs = self.root / "deep-json-runs"
        path = write_attempt(runs / "rejected", result)
        record = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(record["answer"]["content"], raw)
        self.assertEqual(record["validation_error"], result["validation_error"])
        self.assertEqual(record["status"], "rejected")
        summary = summarize_attempts(runs)
        self.assertEqual(summary["rejected"], 1)
        self.assertEqual(summary["rejection_reasons"], {code: 1})
        self.assertEqual(summary["rejection_categories"], {"invalid_or_incomplete_model_plan": 1})

    def test_decoder_recursion_error_is_normalized_and_journaled(self):
        answer = answer_for()
        with patch.object(release.json, "loads", side_effect=RecursionError) as decoder:
            with self.assertRaises(release.ProposalValidationError) as caught:
                release.parse_proposal(answer)
            result = self.plan(answer)
            self.assertEqual(decoder.call_count, 2)
        # release and the journal share the stdlib json module. Restore the
        # decoder before reading or validating the persisted journal record.
        expected_error = {"code": "invalid_or_incomplete_model_plan",
                          "errors": [{"code": "json_too_deep"}]}
        self.assertEqual(caught.exception.errors, expected_error["errors"])
        self.assertEqual(result["validation_error"], expected_error)
        self.assertEqual(result["answer"], answer)
        self.assertIsNone(result["proposal"])
        with self.assertRaises(ValueError):
            release.render_plan(result)
        runs = self.root / "decoder-recursion-runs"
        path = write_attempt(runs / "rejected", result)
        record = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(record["answer"], answer)
        self.assertEqual(record["validation_error"], expected_error)
        self.assertEqual(record["status"], "rejected")
        summary = summarize_attempts(runs)
        self.assertEqual(summary["rejected"], 1)
        self.assertEqual(summary["rejection_reasons"], {"json_too_deep": 1})
        self.assertEqual(summary["rejection_categories"], {"invalid_or_incomplete_model_plan": 1})

    def test_cli_rejection_json_exposes_real_length_and_keeps_raw_completion(self):
        answer = answer_for(content=LIVE_85_CONTENT)
        output = self.root / "cli-rejected"
        argv = ["release", "--repo", str(self.root), "--db", str(self.agent.db),
                "plan", "--run-dir", str(output)]
        stdout = io.StringIO()
        with patch.object(sys, "argv", argv), contextlib.redirect_stdout(stdout), \
                patch.object(release, "collect_evidence", return_value=self.evidence), \
                patch.object(release, "read_api_key", return_value="offline-placeholder"), \
                patch.object(release, "KimiClient") as client:
            client.return_value.complete.return_value = answer
            self.assertEqual(release.main(), 2)
            client.return_value.complete.assert_called_once()
        response = json.loads(stdout.getvalue())
        expected_error = {"code": "invalid_or_incomplete_model_plan", "errors": [
            {"code": "reason_too_long", "field": "reason", "expected": 80, "actual": 85}]}
        self.assertEqual(response["status"], "rejected")
        self.assertEqual(response["validation_error"], expected_error)
        record = json.loads((output / "attempt.json").read_text(encoding="utf-8"))
        self.assertEqual(record["validation_error"], expected_error)
        self.assertEqual(record["answer"]["content"], LIVE_85_CONTENT)
        self.assertIsNone(record["proposal"])
        with self.assertRaises(ValueError):
            release.render_plan(record)


if __name__ == "__main__":
    unittest.main()
