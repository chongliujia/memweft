import copy
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from contextlib import redirect_stdout

from output_contract import contract_request, schema_error, validate_schema
from run_local import Model, case_schema, evaluate, score_answer, validate_suite


PORT = {"type": "object", "properties": {"port": {"type": ["integer", "null"]}},
        "required": ["port"], "additionalProperties": False}


class ContractTests(unittest.TestCase):
    def test_nullable_contract_does_not_reveal_whether_information_exists(self):
        validate_schema(PORT)
        self.assertIsNone(schema_error({"port": None}, PORT))
        self.assertIsNone(schema_error({"port": 17443}, PORT))
        for answer in ({"port": True}, {"port": "17443"}, {"port": 17443.0}, {},
                       {"deployment_port": 17443}, {"port": 17443, "other": 1}):
            self.assertIsNotNone(schema_error(answer, PORT))

    def test_content_and_protocol_are_independent(self):
        wrong_value = score_answer('{"port":999}', {"port": 17443}, PORT)
        self.assertTrue(wrong_value["protocol_valid"])
        self.assertFalse(wrong_value["content_correct"])
        self.assertEqual(wrong_value["score"], 0)
        wrong_field = score_answer('{"z_deployment_port":17443}', {"port": 17443}, PORT)
        self.assertFalse(wrong_field["protocol_valid"])
        self.assertIsNone(wrong_field["content_correct"])
        self.assertEqual(wrong_field["score"], 0)
        unknown = score_answer('{"port":null}', {"port": 17443}, PORT)
        self.assertTrue(unknown["protocol_valid"])
        self.assertFalse(unknown["content_correct"])

    def test_invalid_json_cannot_hide_an_incorrect_answer(self):
        for text in ('{"port":999,"port":17443}', '{"port":NaN}', '{"port":Infinity}'):
            self.assertEqual(score_answer(text, {"port": 17443}, PORT)["reason"], "invalid_json")

    def test_unknown_schema_features_fail_before_model_calls(self):
        for feature in ("const", "default", "examples", "pattern"):
            schema = copy.deepcopy(PORT)
            schema["properties"]["port"][feature] = 17443
            with self.assertRaises(ValueError):
                validate_schema(schema)
        schema = copy.deepcopy(PORT)
        schema["properties"]["port"] = {"type": "object"}
        with self.assertRaises(ValueError):
            validate_schema(schema)

    def test_format_modes_do_not_mutate_prompts_or_receive_answers(self):
        original = [{"role": "system", "content": "Follow the task."},
                    {"role": "user", "content": "Return port or null."}]
        saved = copy.deepcopy(original)
        free, response = contract_request(original, PORT, "none")
        self.assertEqual(free, saved)
        self.assertIsNone(response)
        prompted, response = contract_request(original, PORT, "prompt")
        self.assertIsNone(response)
        constrained, response = contract_request(original, PORT, "schema")
        self.assertEqual(prompted, constrained)
        self.assertEqual(response["json_schema"]["schema"], PORT)
        self.assertTrue(response["json_schema"]["strict"])
        self.assertNotIn("17443", json.dumps([constrained, response]))
        self.assertEqual(original, saved)

    def test_new_suite_preserves_tasks_answers_and_training_split(self):
        root = Path(__file__).parent / "scenarios"
        old = json.loads((root / "common-v2.json").read_text())
        new = json.loads((root / "common-v3.json").read_text())
        validate_suite(new)
        for group in ["cases", "train", "validation", "test"]:
            before = old[group] if group == "cases" else old["learning"][group]
            after = new[group] if group == "cases" else new["learning"][group]
            self.assertEqual(before, [{k: v for k, v in case.items() if k != "output_contract"} for case in after])
            for case in after:
                self.assertIsNotNone(case_schema(new, case))
        for case in new["cases"]:
            for leaf in case_schema(new, case)["properties"].values():
                self.assertIn("null", leaf["type"])
                self.assertNotIn("enum", leaf)

    def test_model_transport_sends_schema_and_logs_raw_response(self):
        class Opener:
            def open(self, request, timeout):
                self.payload = json.loads(request.data)
                return io.BytesIO(json.dumps({"choices": [{"message": {"content": '{"port":17443}'},
                    "finish_reason": "stop"}], "usage": {"total_tokens": 20}}).encode())

        with tempfile.TemporaryDirectory() as folder:
            args = SimpleNamespace(base_url="http://127.0.0.1:8002/v1", model="test",
                                   max_tokens=128, seed=42, timeout=1, output_contract="schema")
            model = Model(args, Path(folder))
            model.opener = Opener()
            with redirect_stdout(io.StringIO()):
                result = model.call([{"role": "user", "content": "Return port."}], tag="transport", schema=PORT)
            self.assertEqual(model.opener.payload["response_format"]["json_schema"]["schema"], PORT)
            self.assertEqual(result["content"], '{"port":17443}')
            logged = json.loads((Path(folder) / "calls.jsonl").read_text())
            self.assertEqual(logged["response"]["usage"]["total_tokens"], 20)

    def test_truncated_but_parseable_completion_does_not_pass(self):
        with tempfile.TemporaryDirectory() as folder:
            model = SimpleNamespace(output=Path(folder), args=SimpleNamespace(output_contract="schema"),
                call=lambda *args, **kwargs: {"content": '{"port":17443}', "finish_reason": "length"})
            row = evaluate(model, {"id": "port", "prompt": "Return port.", "expected": {"port": 17443}},
                           "memory", "test", 0, "Follow task", schema=PORT)
            self.assertEqual(row["score"], 0)
            self.assertFalse(row["protocol_valid"])
            self.assertIsNone(row["content_correct"])


if __name__ == "__main__":
    unittest.main()
