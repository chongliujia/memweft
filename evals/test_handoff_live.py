"""Offline checks for live handoff scoring and persisted before/after preparation."""
import json
from pathlib import Path
import tempfile
import unittest

from run_handoff_live import dump, grade, prepare, sha, summarize


EXPECTED = {"state": "completed", "next": "record_completion", "human_review": "pending"}


class HandoffLiveTests(unittest.TestCase):
    def answer(self, **changes):
        return {"content": json.dumps({**EXPECTED, "reason": "全部约定工作已有验证记录", **changes}),
                "finish_reason": "stop"}

    def test_progress_correctness_is_separate_from_valid_json(self):
        self.assertTrue(grade(self.answer(), EXPECTED)["accepted"])
        wrong = grade(self.answer(state="ready_to_continue", next="verify_delivery"), EXPECTED)
        self.assertTrue(wrong["protocol_valid"])
        self.assertFalse(wrong["accepted"])

    def test_missing_context_is_safe_refusal_not_completion(self):
        expected = {**EXPECTED, "state": "needs_context", "next": "request_context"}
        row = {"case_id": "missing", "mode": "no_memory", "answer": self.answer(**expected),
               "grade": grade(self.answer(**expected), expected)}
        result = summarize([row])
        self.assertEqual(result["by_mode"]["no_memory"]["correct_under_frozen_rules"], 1)
        self.assertIn("not task completion", result["by_mode"]["no_memory"]["meaning"])

    def test_malformed_or_overconfident_proposals_are_rejected(self):
        samples = [self.answer(human_review="approved"), self.answer(reason="x" * 81),
                   self.answer(extra="unrequested"), self.answer(state=True), self.answer(reason="  "),
                   {**self.answer(), "finish_reason": "length"},
                   {"content": "[]", "finish_reason": "stop"},
                   {"content": '{"state":"completed","state":"completed"}', "finish_reason": "stop"}]
        for answer in samples:
            with self.subTest(answer=answer):
                self.assertFalse(grade(answer, EXPECTED)["accepted"])

    def test_unknown_usage_is_not_zero(self):
        rows = [{"case_id": "a", "mode": "memory", "answer": {"usage": None},
                 "grade": {"accepted": True}},
                {"case_id": "b", "mode": "memory", "answer": {"usage": {"prompt_tokens": 12}},
                 "grade": {"accepted": True}}]
        usage = summarize(rows)["usage"]["prompt_tokens"]
        self.assertIsNone(usage["total"])
        self.assertEqual(usage["known_subtotal"], 12)
        self.assertEqual(usage["unknown_calls"], 1)

    def test_frozen_preparation_preserves_updates_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.json"
            dump(source, {"fixture": "constructed test evidence"})
            cases = [{"id": task + "-" + stage, "task": task, "stage": stage,
                      "source_files": [{"path": source.name, "sha256": sha(source)}],
                      "facts": {"marker": task + "-" + stage},
                      "expected_memory": EXPECTED, "expected_no_memory": EXPECTED}
                     for task in ("release", "recovery", "worklog") for stage in ("before", "after")]
            suite = root / "suite.json"
            dump(suite, {"cases": cases})
            output = root / "out"
            rows = prepare(root, output, suite)
            self.assertEqual(len(rows), 12)
            for case in cases:
                context = json.loads((output / "contexts" / (case["id"] + ".json")).read_text())
                self.assertEqual(context["memories"][0]["value"], case["facts"])
            self.assertTrue(all(len(row["messages"]) == 2 for row in rows if row["mode"] == "no_memory"))
            self.assertNotIn("expected_memory", json.dumps([row["messages"] for row in rows]))
            with self.assertRaises(FileExistsError):
                prepare(root, output, suite)


if __name__ == "__main__":
    unittest.main()
