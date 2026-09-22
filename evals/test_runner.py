import copy
import json
from pathlib import Path
import unittest

from run_local import score_answer, summarize, validate_suite


class ScoringTests(unittest.TestCase):
    def test_missing_null_is_not_correct_abstention(self):
        self.assertEqual(score_answer('{}', {"project": None})["score"], 0)
        self.assertEqual(score_answer('{"project":null}', {"project": None})["score"], 1)

    def test_wrong_json_types_do_not_pass(self):
        for response in ('{"port":true}', '{"port":"1"}', '{"port":1.0}'):
            self.assertEqual(score_answer(response, {"port": 1})["score"], 0)
        self.assertEqual(score_answer('{"port":1}', {"port": 1})["score"], 1)

    def test_malformed_or_extra_output_fails(self):
        for response in ('```json\n{"queue":"billing"}\n```', 'null', '[]',
                         '{"queue":"billing","extra":"x"}', '{"queue":"billing"} trailing'):
            self.assertEqual(score_answer(response, {"queue": "billing"})["score"], 0)

    def test_wrong_values_fail(self):
        grade = score_answer('{"queue":"billing","priority":"P2"}',
                             {"queue": "billing", "priority": "P1"})
        self.assertEqual(grade["score"], 0)
        self.assertEqual(grade["reason"], "wrong_values:priority")


class DatasetTests(unittest.TestCase):
    def setUp(self):
        self.suite = json.loads((Path(__file__).parent / "scenarios/common.json").read_text())

    def test_real_fixture_has_disjoint_ids_and_prompts(self):
        validate_suite(self.suite)
        newer = json.loads((Path(__file__).parent / "scenarios/common-v2.json").read_text())
        validate_suite(newer)
        self.assertEqual(newer["learning"]["train"], self.suite["learning"]["train"])
        for split in ("validation", "test"):
            negatives = [c for c in newer["learning"][split] if c.get("category") == "workflow_counterexamples"]
            self.assertEqual(len(negatives), 3)
            self.assertTrue(all(c["expected"]["priority"] == "P2" for c in negatives))

    def test_reused_test_prompt_is_rejected(self):
        suite = copy.deepcopy(self.suite)
        suite["learning"]["test"][0]["prompt"] = suite["learning"]["train"][0]["prompt"]
        with self.assertRaises(ValueError):
            validate_suite(suite)

    def test_duplicate_id_is_rejected(self):
        suite = copy.deepcopy(self.suite)
        suite["learning"]["test"][0]["id"] = suite["learning"]["train"][0]["id"]
        with self.assertRaises(ValueError):
            validate_suite(suite)

    def test_summary_keeps_modes_and_categories_separate(self):
        rows = [dict(category="recall", mode=mode, score=score, latency_ms=10, usage={"total_tokens":20})
                for mode, score in [("memory", 0.0), ("memory", 1.0), ("no_memory", 0.0)]]
        groups = {(g["category"], g["mode"]): g for g in summarize(rows)}
        self.assertEqual(groups[("recall", "memory")]["pass_rate"], 0.5)
        self.assertEqual(groups[("recall", "no_memory")]["pass_rate"], 0)


if __name__ == "__main__":
    unittest.main()
