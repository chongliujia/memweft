import copy
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from report_workflow_comparison import (
    MODES, ROOT, active_main_facts, build_report, expected_baseline, expected_messages,
    regrade, recompute_summary, render_markdown, sha256, write_reports,
)


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def dump_lines(path, rows):
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


class WorkflowReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.run = self.directory / "run"
        self.run.mkdir()
        shutil.copyfile(ROOT / "evals/scenarios/workflow-comparison-v1.json", self.run / "suite.json")
        self.suite = json.loads((self.run / "suite.json").read_text())
        self.metadata = {
            "suite_sha256": sha256(self.run / "suite.json"), "started_at": "2026-09-30T09:33:39+00:00",
            "model": "kimi-k2.6", "thinking": "disabled", "modes": list(MODES),
            "max_calls": 60, "max_completion_tokens": 192, "max_request_bytes": 16000,
            "reported_token_stop_threshold": 100000, "interval_seconds": 21,
            "memory_max_facts": 8, "memory_estimated_token_budget": 1200, "retries": 0,
            "native_sha256": "recorded-native-digest", "source_sha256": {},
        }
        for relative in ("evals/run_workflow_comparison.py", "examples/kimi_memory.py", "evals/output_contract.py"):
            source = ROOT / relative
            target = self.run / "sources" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            self.metadata["source_sha256"][relative] = sha256(source)
        dump(self.run / "metadata.json", self.metadata)
        self.inputs, self.rows = [], []
        for case in self.suite["cases"]:
            current = active_main_facts(case)
            for mode in MODES:
                if mode == "memweft":
                    selected = dict(current)
                    # A deliberately missing active fact plus a wrong answer:
                    # the report must expose both observations without causality.
                    if case is self.suite["cases"][0]:
                        selected.pop("campaign_receiver")
                    context = {"text": "\n".join(f"{key}: {value}" for key, value in selected.items()),
                               "memories": [{"fact_key": key, "value": value} for key, value in selected.items()],
                               "report": {"warnings": [], "omissions": []}}
                else:
                    context = {"text": expected_baseline(case, mode)}
                messages = expected_messages(case, context["text"])
                self.inputs.append({"case_id": case["id"], "mode": mode, "context": context,
                                    "messages": messages, "context_build_ms": 1.0,
                                    "request_bytes": len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))})
                answer = copy.deepcopy(case["expected"])
                if case is self.suite["cases"][0] and mode == "memweft":
                    answer["receiver"] = None
                row = {"case_id": case["id"], "category": case["category"], "mode": mode,
                       "expected": case["expected"], "content": json.dumps(answer, ensure_ascii=False),
                       "finish_reason": "stop", "model": "kimi-k2.6",
                       "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
                       "request": {"model": "kimi-k2.6", "messages": copy.deepcopy(messages), "stream": False,
                                   "thinking": {"type": "disabled"}, "max_completion_tokens": 192,
                                   "response_format": {"type": "json_object"}},
                       "context_build_ms": 1.0, "latency_ms": 2.0, "context_and_model_ms": 3.0}
                row.update(regrade(row, case))
                self.rows.append(row)
        self.save()

    def save(self):
        dump_lines(self.run / "inputs.jsonl", self.inputs)
        dump_lines(self.run / "results.jsonl", self.rows)
        dump(self.run / "summary.json", recompute_summary(self.rows))
        dump(self.run / "status.json", {"status": "completed", "completed_calls": 60})

    def test_complete_run_recomputes_and_preserves_failure_and_missing_facts(self):
        report = build_report(self.run)
        self.assertEqual(report["summary"]["by_mode"]["memweft"]["passed"], 19)
        self.assertEqual(len(report["cases"]), 20)
        self.assertEqual(len(report["failure_analysis"]), 1)
        failure = report["failure_analysis"][0]
        self.assertEqual(failure["actual"]["receiver"], None)
        self.assertEqual(failure["expected"]["receiver"], "media-intake-8")
        self.assertEqual(failure["memweft_omitted_main_fact_keys"], ["campaign_receiver"])
        self.assertEqual(failure["memweft_omission_reasons"], {"campaign_receiver": []})
        markdown = render_markdown(report)
        self.assertIn("campaign_receiver", markdown)
        self.assertIn("不是客户任务", markdown)
        self.assertIn("不调用 LLM", markdown)
        self.assertIn("不能据此确认因果", markdown)
        json_output, markdown_output = self.directory / "report.json", self.directory / "report.md"
        write_reports(report, json_output, markdown_output)
        self.assertEqual(json.loads(json_output.read_text()), report)
        self.assertEqual(markdown_output.read_text(), markdown)

    def test_incomplete_status_never_produces_final_report(self):
        dump(self.run / "status.json", {"status": "budget_stopped", "completed_calls": 59})
        with self.assertRaisesRegex(ValueError, "completed status"):
            build_report(self.run)
        (self.run / "status.json").unlink()
        with self.assertRaisesRegex(ValueError, "completed status"):
            build_report(self.run)

    def test_duplicate_or_missing_pairs_rejected_even_with_completed_status(self):
        for rows, message in ((self.rows[:-1], "all 60"), (self.rows[:-1] + [self.rows[0]], "duplicate")):
            with self.subTest(message=message):
                dump_lines(self.run / "results.jsonl", rows)
                with self.assertRaisesRegex(ValueError, message):
                    build_report(self.run)

    def test_frozen_input_or_actual_request_tampering_is_rejected(self):
        self.rows[0]["request"]["messages"][-1]["content"] = "A different paid question"
        dump_lines(self.run / "results.jsonl", self.rows)
        with self.assertRaisesRegex(ValueError, "request differs"):
            build_report(self.run)
        self.rows[0]["request"]["messages"] = copy.deepcopy(self.inputs[0]["messages"])
        self.inputs[0]["messages"].append({"role": "assistant", "content": json.dumps(self.suite["cases"][0]["expected"])})
        self.rows[0]["request"]["messages"] = copy.deepcopy(self.inputs[0]["messages"])
        self.save()
        with self.assertRaisesRegex(ValueError, "frozen messages do not match"):
            build_report(self.run)

    def test_grade_tampering_is_rejected_even_if_summary_was_updated(self):
        self.rows[2]["score"] = 1.0
        self.save()
        with self.assertRaisesRegex(ValueError, "stored grade disagrees"):
            build_report(self.run)

    def test_usage_and_timing_tampering_are_rejected(self):
        original = copy.deepcopy(self.rows)
        changes = (("usage", {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 1}, "inconsistent usage"),
                   ("context_and_model_ms", 9.0, "timing mismatch"))
        for key, value, message in changes:
            with self.subTest(key=key):
                self.rows = copy.deepcopy(original)
                self.rows[0][key] = value
                self.save()
                with self.assertRaisesRegex(ValueError, message):
                    build_report(self.run)

    def test_summary_tampering_is_rejected(self):
        summary = recompute_summary(self.rows)
        summary["by_mode"]["memweft"]["passed"] = 20
        dump(self.run / "summary.json", summary)
        with self.assertRaisesRegex(ValueError, "summary differs"):
            build_report(self.run)

    def test_suite_or_source_hash_tampering_is_rejected(self):
        raw = (self.run / "suite.json").read_bytes()
        self.suite["description"] += " altered"
        dump(self.run / "suite.json", self.suite)
        with self.assertRaisesRegex(ValueError, "Suite SHA mismatch"):
            build_report(self.run)
        (self.run / "suite.json").write_bytes(raw)
        (self.run / "sources/evals/output_contract.py").write_text("altered")
        with self.assertRaisesRegex(ValueError, "Source SHA mismatch"):
            build_report(self.run)

    def test_main_fact_diagnostics_ignore_deleted_and_foreign_values(self):
        forgotten = next(case for case in self.suite["cases"] if case["id"] == "wf-support-forgotten-recipient")
        self.assertNotIn("support_notice_recipient", active_main_facts(forgotten))
        scoped = next(case for case in self.suite["cases"] if case["id"] == "wf-troubleshoot-cache-generation")
        self.assertIn("43", active_main_facts(scoped)["price_cache_revision"])
        self.assertNotIn("46", active_main_facts(scoped)["price_cache_revision"])

    def test_stale_selected_memory_is_rejected(self):
        item = self.inputs[2]
        item["context"]["memories"][0]["value"] = "a stale value"
        dump_lines(self.run / "inputs.jsonl", self.inputs)
        with self.assertRaisesRegex(ValueError, "stale or out-of-scope"):
            build_report(self.run)

    def test_supplemental_source_provenance_cannot_replace_pre_call_hashes(self):
        dump(self.run / "source-provenance.json", {"captured_at": "2026-09-30T09:35:08+00:00", "note": "during run",
             "source_sha256": {"evals/output_contract.py": "different-digest"}})
        with self.assertRaisesRegex(ValueError, "disagrees with pre-call"):
            build_report(self.run)

    def test_existing_report_is_never_overwritten_and_other_output_is_not_created(self):
        report = build_report(self.run)
        json_output, markdown_output = self.directory / "report.json", self.directory / "report.md"
        json_output.write_text("keep me")
        with self.assertRaisesRegex(ValueError, "overwrite"):
            write_reports(report, json_output, markdown_output)
        self.assertEqual(json_output.read_text(), "keep me")
        self.assertFalse(markdown_output.exists())


if __name__ == "__main__":
    unittest.main()
