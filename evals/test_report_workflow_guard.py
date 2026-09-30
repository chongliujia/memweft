"""Offline report integrity and metric-denominator checks; no native SDK needed."""
import copy
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from report_workflow_guard import (
    MODES, ROOT, WORKFLOWS, build_report, check_answer, digest, facts_from_context,
    messages_for, raw_grade, render_context, render_markdown, source_facts,
    summarize, validate_facts, write_reports,
)


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def dump_lines(path, rows):
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


class WorkflowGuardReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.run = self.directory / "run"
        self.run.mkdir()
        shutil.copyfile(ROOT / "evals/scenarios/workflow-guard-v1.json", self.run / "suite.json")
        self.suite = json.loads((self.run / "suite.json").read_text())
        self.metadata = {"suite_sha256": digest(self.run / "suite.json"), "source_sha256": {},
            "native_sha256": "recorded-native-digest", "started_at": "2026-09-30T12:00:00+00:00",
            "model": "kimi-k2.6", "thinking": "disabled", "modes": list(MODES), "repeats": 2,
            "max_calls": 32, "reported_token_stop_threshold": 60000, "max_completion_tokens": 192,
            "message_bytes_cap": 16000, "interval_seconds": 21, "max_facts": 8, "max_tokens": 1200,
            "automatic_retries": 0,
            "pricing": {"checked": "2026-09-30", "input_cny_per_million": 6.5,
                        "output_cny_per_million": 27, "cache_discount": False,
                        "source": "https://platform.kimi.com/docs/pricing/chat"}}
        for relative in ("evals/run_workflow_guard.py", "examples/workflow_guard.py", "examples/guarded_workflow_agent.py",
                         "examples/kimi_memory.py", "evals/run_local.py"):
            path = self.run / "sources" / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, path)
            self.metadata["source_sha256"][relative] = digest(path)
        dump(self.run / "metadata.json", self.metadata)
        self.inputs, self.rows = [], []
        for index, case in enumerate(self.suite["cases"]):
            current = source_facts(case)
            for mode in MODES:
                selected = source_facts(case, False)
                if index == 0 and mode == "lexical":
                    selected.pop("clock.observation")
                selected["archive_00"] = current["archive_00"]
                memories = [{"fact_key": key, "value": value} for key, value in selected.items()]
                text = render_context(memories)
                required = WORKFLOWS[case["workflow"]]["required_fact_keys"] if mode == "required_facts" else []
                missing = [key for key in required if key not in current]
                excluded = [key for key in required if key not in selected and key not in missing]
                context = {"text": text, "memories": memories, "messages": [], "strategies": [],
                    "report": {"estimated_tokens": (len(text.encode("utf-8")) + 3) // 4,
                        "requirements": {"requested": required, "included": [key for key in required if key in selected],
                                         "missing": missing, "excluded": excluded, "complete": not missing and not excluded}}}
                messages = messages_for(case, context)
                self.inputs.append({"case_id": case["id"], "workflow": case["workflow"], "mode": mode,
                    "context": context, "messages": messages, "context_build_ms": 1.25,
                    "input_validation": validate_facts(case["workflow"], selected),
                    "message_bytes": len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))})
        inputs = {(item["case_id"], item["mode"]): item for item in self.inputs}
        for repeat in range(2):
            for index, case in enumerate(self.suite["cases"]):
                for mode in (MODES if (repeat + index) % 2 == 0 else MODES[::-1]):
                    item = inputs[(case["id"], mode)]
                    if case["expectation"] == "needs_data":
                        proposal = {"window": "invented-window-99", "start_time": "08:00", "end_time": "08:25", "unused_minutes": 0}
                    else:
                        proposal = copy.deepcopy(case["expected"])
                    if index == 0 and mode == "lexical" and repeat == 1:
                        proposal["expired_minutes"] = 1
                    if index == 3 and mode == "required_facts" and repeat == 0:
                        proposal["refund_amount"] = 1
                    answer = {"content": json.dumps(proposal), "finish_reason": "stop", "model": "kimi-k2.6",
                        "latency_ms": 2.75, "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
                        "request": {"model": "kimi-k2.6", "messages": copy.deepcopy(item["messages"]), "stream": False,
                            "thinking": {"type": "disabled"}, "max_completion_tokens": 192,
                            "response_format": {"type": "json_object"}}}
                    self.rows.append({"case_id": case["id"], "workflow": case["workflow"], "mode": mode, "repeat": repeat,
                        "expectation": case["expectation"], "expected": case["expected"], "answer": answer,
                        "grade": raw_grade(answer, case), "input_validation": item["input_validation"],
                        "decision": check_answer(case["workflow"], item["context"], answer),
                        "context_build_ms": 1.25, "context_and_model_ms": 4.0})
        self.save()

    def save(self):
        dump_lines(self.run / "inputs.jsonl", self.inputs)
        dump_lines(self.run / "results.jsonl", self.rows)
        dump(self.run / "summary.json", summarize(self.rows))
        dump(self.run / "status.json", {"status": "completed", "completed_calls": 32})

    def test_denominators_separate_answerability_guesses_and_blocked_wrong_answers(self):
        report = build_report(self.run)
        lexical, required = (report["summary"]["by_mode"][mode] for mode in MODES)
        self.assertEqual(lexical["answerable_observations"], 14)
        self.assertEqual(lexical["raw_correct"], 13)
        self.assertEqual(lexical["validated_correct"], 12)
        self.assertEqual(lexical["correct_but_rejected"], 1)
        self.assertEqual(lexical["false_rejection_with_complete_inputs"], 0)
        self.assertEqual(lexical["incorrect_blocked"], 1)
        self.assertEqual(required["raw_correct"], 13)
        self.assertEqual(required["validated_correct"], 13)
        self.assertEqual(required["incorrect_blocked"], 1)
        for group in (lexical, required):
            self.assertEqual(group["observations"], 16)
            self.assertEqual(group["missing_data_observations"], 2)
            self.assertEqual(group["missing_data_rejected"], 2)
            self.assertEqual(group["incorrect_accepted"], 0)
        self.assertEqual(len(report["observations"]), 32)
        self.assertEqual(report["cases"][0]["modes"]["lexical"]["available_but_omitted_required_keys"], ["clock.observation"])
        markdown = render_markdown(report)
        self.assertIn("13/14", markdown)
        self.assertIn("猜中答案但输入不完整", markdown)
        self.assertIn("不能与上一轮20道自由文本题直接比较", markdown)
        self.assertEqual(markdown.count("### guard-"), 32)

    def test_missing_or_incomplete_status_cannot_generate_final_report(self):
        for state in ({"status": "prepared", "calls": 0}, {"status": "budget_stopped", "completed_calls": 31}):
            dump(self.run / "status.json", state)
            with self.assertRaisesRegex(ValueError, "completed status"):
                build_report(self.run)
        (self.run / "status.json").unlink()
        with self.assertRaisesRegex(ValueError, "completed status"):
            build_report(self.run)

    def test_duplicate_missing_or_out_of_range_repeats_are_rejected(self):
        for rows, error in ((self.rows[:-1], "incomplete coverage"),
                            (self.rows[:-1] + [self.rows[0]], "duplicate identity")):
            dump_lines(self.run / "results.jsonl", rows)
            with self.assertRaisesRegex(ValueError, error):
                build_report(self.run)
        altered = copy.deepcopy(self.rows)
        altered[0]["repeat"] = 2
        dump_lines(self.run / "results.jsonl", altered)
        with self.assertRaisesRegex(ValueError, "unexpected identity"):
            build_report(self.run)

    def test_input_coverage_and_actual_request_are_frozen(self):
        dump_lines(self.run / "inputs.jsonl", self.inputs[:-1])
        with self.assertRaisesRegex(ValueError, "incomplete coverage"):
            build_report(self.run)
        self.save()
        self.rows[0]["answer"]["request"]["messages"][-1]["content"] = "Different question"
        self.save()
        with self.assertRaisesRegex(ValueError, "Actual request differs"):
            build_report(self.run)

    def test_facts_and_model_text_cannot_disagree(self):
        self.inputs[0]["context"]["text"] += "\nHidden replacement facts"
        self.save()
        with self.assertRaisesRegex(ValueError, "Context text differs"):
            build_report(self.run)

    def test_foreign_or_stale_source_values_rejected(self):
        item = self.inputs[1]
        fact = next(fact for fact in item["context"]["memories"] if fact["fact_key"] == "clock.observation")
        fact["value"]["now_minute"] = 900  # This value exists only in the foreign source event.
        self.save()
        with self.assertRaisesRegex(ValueError, "foreign or fabricated"):
            build_report(self.run)

    def test_forgotten_windows_are_not_a_missing_input_workaround(self):
        item = next(item for item in self.inputs if item["case_id"] == "guard-maintenance-withdrawn-windows-en")
        original = next(event["value"] for event in self.suite["cases"][-1]["events"] if event.get("key") == "calendar.windows" and event["op"] == "remember")
        item["context"]["memories"].append({"fact_key": "calendar.windows", "value": original})
        self.save()
        with self.assertRaisesRegex(ValueError, "foreign or fabricated"):
            build_report(self.run)

    def test_raw_grade_guard_and_input_decisions_are_recomputed(self):
        originals = copy.deepcopy(self.rows)
        for key, replacement, error in (("grade", {"score": 1}, "raw grade differs"),
                                        ("decision", {"status": "validated"}, "guard decision differs"),
                                        ("input_validation", {"accepted": True, "errors": []}, "input validation differs")):
            self.rows = copy.deepcopy(originals)
            self.rows[0][key] = replacement
            dump_lines(self.run / "results.jsonl", self.rows)
            with self.assertRaisesRegex(ValueError, error):
                build_report(self.run)

    def test_usage_latency_and_summary_are_recomputed(self):
        self.rows[0]["answer"]["usage"]["total_tokens"] = 1
        self.save()
        with self.assertRaisesRegex(ValueError, "Usage total mismatch"):
            build_report(self.run)
        self.rows[0]["answer"]["usage"]["total_tokens"] = 120
        self.rows[0]["context_and_model_ms"] = 1.0
        self.save()
        with self.assertRaisesRegex(ValueError, "latency sum mismatch"):
            build_report(self.run)
        self.rows[0]["context_and_model_ms"] = 4.0
        self.save()
        summary = summarize(self.rows)
        summary["by_mode"]["lexical"]["validated_correct"] += 1
        dump(self.run / "summary.json", summary)
        with self.assertRaisesRegex(ValueError, "summary disagrees"):
            build_report(self.run)

    def test_fixture_and_source_hashes_cannot_change(self):
        saved = (self.run / "suite.json").read_bytes()
        (self.run / "suite.json").write_bytes(saved + b" ")
        with self.assertRaisesRegex(ValueError, "fixture SHA mismatch"):
            build_report(self.run)
        (self.run / "suite.json").write_bytes(saved)
        (self.run / "sources/examples/workflow_guard.py").write_text("altered rules")
        with self.assertRaisesRegex(ValueError, "Source snapshot SHA mismatch"):
            build_report(self.run)

    def test_invalid_and_incomplete_proposals_do_not_become_validated(self):
        case = self.suite["cases"][0]
        context = self.inputs[1]["context"]
        for content, finish in ((json.dumps(case["expected"]), "length"), ('{"owner":"a","owner":"b"}', "stop"),
                                ("NaN", "stop"), ("[]", "stop")):
            answer = {"content": content, "finish_reason": finish}
            self.assertEqual(raw_grade(answer, case)["score"], 0)
            decision = check_answer(case["workflow"], context, answer)
            self.assertFalse(decision["validation"]["accepted"])
            self.assertEqual(decision["executed_actions"], [])

    def test_report_writes_once_and_never_overwrites(self):
        report = build_report(self.run)
        json_path, md_path = self.directory / "report.json", self.directory / "report.md"
        write_reports(report, json_path, md_path)
        self.assertEqual(json.loads(json_path.read_text()), report)
        self.assertEqual(md_path.read_text(), render_markdown(report))
        saved = json_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "overwrite"):
            write_reports(report, json_path, self.directory / "another.md")
        self.assertEqual(json_path.read_bytes(), saved)
        self.assertFalse((self.directory / "another.md").exists())


if __name__ == "__main__":
    unittest.main()
