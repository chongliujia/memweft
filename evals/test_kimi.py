"""Offline checks for the paid API boundary and real SDK fixture setup."""
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error

from run_kimi import SUITE, grade_result, prepare_case, run, summarize
from kimi_memory import BASE_URLS, KimiClient, KimiError, NoRedirect, memory_messages


class KimiTests(unittest.TestCase):
    def test_short_nonthinking_request_and_auth_separation(self):
        client = KimiClient("test-secret")
        client.opener = Mock()
        client.opener.open.return_value = io.BytesIO(json.dumps({
            "choices": [{"message": {"content": '{"answer":null}'}, "finish_reason": "stop"}],
            "usage": {"total_tokens": 30}, "model": "kimi-k2.6"}).encode())
        result = client.complete(memory_messages("Return JSON answer."))
        request = client.opener.open.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(payload["max_completion_tokens"], 128)
        self.assertFalse({"temperature", "seed", "chat_template_kwargs"} & payload.keys())
        self.assertEqual(request.get_header("Authorization"), "Bearer test-secret")
        self.assertNotIn("test-secret", json.dumps(result))
        with self.assertRaises(ValueError):
            client.complete([], max_tokens=1000)
        self.assertEqual(client.opener.open.call_count, 1)

    def test_provider_failure_redacts_key_and_does_not_retry(self):
        client = KimiClient("test-secret")
        client.opener = Mock()
        client.opener.open.side_effect = urllib.error.HTTPError(
            BASE_URLS[0], 401, "Unauthorized", {}, io.BytesIO(b'test-secret sk-other-secret'))
        with self.assertRaises(KimiError) as caught:
            client.complete(memory_messages("Return JSON answer."))
        self.assertNotIn("test-secret", str(caught.exception))
        self.assertNotIn("sk-other-secret", str(caught.exception))
        self.assertIn("401", str(caught.exception))
        self.assertEqual(client.opener.open.call_count, 1)
        with self.assertRaises(ValueError):
            KimiClient("test-secret", base_url="https://example.com/v1")
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.com"))

    def test_all_fixture_invariants_survive_native_store_reopen(self):
        cases = json.loads(SUITE.read_text())["cases"]
        with tempfile.TemporaryDirectory() as directory:
            contexts = {case["id"]: prepare_case(Path(directory) / "memory.db", case) for case in cases}
        self.assertEqual(len(contexts), 13)
        self.assertIn("eu-central-1", contexts["update-region"]["text"])
        self.assertIn("cedar-17", contexts["session-next-step"]["text"])
        self.assertIn("helix", contexts["forget-preserves-unrelated"]["text"])

    def test_truncated_correct_text_is_failure_and_usage_is_counted(self):
        schema = {"type": "object", "properties": {"value": {"type": ["string", "null"]}},
                  "required": ["value"], "additionalProperties": False}
        result = {"content": '{"value":null}', "finish_reason": "length"}
        grade = grade_result(result, {"value": None}, schema)
        self.assertEqual(grade["score"], 0)
        rows = [{"case_id": "case", "mode": "memory", "usage": {
            "prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100}, **grade}]
        summary = summarize(rows)
        self.assertEqual(summary["by_mode"]["memory"], {"passed": 0, "total": 1})
        self.assertAlmostEqual(summary["estimated_cny_without_cache_discount"], 0.0092)

    def test_runner_limits_calls_and_paces_starts_without_real_sleep(self):
        client = Mock()
        client.complete.return_value = {"content": "{}", "finish_reason": "stop",
                                       "usage": {}, "latency_ms": 5000}
        ticks = [0] + [tick for n in range(1, 26) for tick in (21 * n - 16, 21 * n)]
        with tempfile.TemporaryDirectory() as directory:
            args = SimpleNamespace(output=Path(directory) / "run", base_url=BASE_URLS[0],
                                   check_memory=False, interval=21)
            with patch("run_kimi.time.monotonic", side_effect=ticks), \
                 patch("run_kimi.time.sleep") as sleep, patch("builtins.print"):
                summary = run(args, client)
            self.assertEqual(client.complete.call_count, 26)
            metadata = json.loads((args.output / "metadata.json").read_text())
            self.assertIn("examples/handoff_app/kimi_client.py", metadata["source_sha256"])
            self.assertEqual(summary["calls"], 26)
            self.assertEqual(sleep.call_count, 25)
            self.assertTrue(all(call.args == (16,) for call in sleep.call_args_list))
            with self.assertRaises(FileExistsError):
                run(args, client)
            self.assertEqual(client.complete.call_count, 26)


if __name__ == "__main__":
    unittest.main()
