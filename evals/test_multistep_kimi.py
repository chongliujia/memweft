"""No-network contract tests for the bounded multistep evaluation client."""
import io
import json
import unittest
from unittest.mock import Mock
import urllib.error
import urllib.request

from multistep_kimi import BoundedKimiClient, KimiClient, KimiError, MODEL, RateLimited
from handoff_app.kimi_client import NoRedirect


class BoundedKimiClientTests(unittest.TestCase):
    def setUp(self):
        self.key = "test-key-do-not-log"
        self.client = BoundedKimiClient(self.key)
        self.client.opener = Mock()
        self.messages = [{"role": "user", "content": "Return a JSON action."}]

    def response(self, *, content='{"action":"read"}', usage=None):
        raw = {
            "id": "completion-test", "model": MODEL,
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": usage,
        }
        self.client.opener.open.return_value = io.BytesIO(json.dumps(raw).encode())
        return raw

    def test_inherits_official_endpoint_and_transport_restrictions(self):
        self.assertTrue(issubclass(BoundedKimiClient, KimiClient))
        with self.assertRaises(ValueError):
            BoundedKimiClient(self.key, base_url="https://untrusted.example/v1")
        client = BoundedKimiClient(self.key)
        self.assertTrue(any(isinstance(h, NoRedirect) for h in client.opener.handlers))
        # An empty ProxyHandler contributes no handlers; the defaults must not
        # reintroduce an environment-derived proxy handler.
        self.assertFalse(any(isinstance(h, urllib.request.ProxyHandler)
                             for h in client.opener.handlers))

    def test_budget_payload_auth_and_usage_preserved(self):
        usage = {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12,
                 "prompt_tokens_details": {"cached_tokens": 2}}
        self.response(usage=usage)
        result = self.client.complete(self.messages)
        self.client.opener.open.assert_called_once()
        request = self.client.opener.open.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.get_header("Authorization"), "Bearer " + self.key)
        self.assertEqual(request.full_url, "https://api.moonshot.cn/v1/chat/completions")
        self.assertEqual(self.client.opener.open.call_args.kwargs, {"timeout": 60})
        self.assertEqual(payload, {
            "model": MODEL, "messages": self.messages, "stream": False,
            "thinking": {"type": "disabled"}, "max_completion_tokens": 1024,
            "response_format": {"type": "json_object"},
        })
        self.assertEqual(result["request"], payload)
        self.assertEqual(result["usage"], usage)
        self.assertEqual(result["response_id"], "completion-test")
        self.assertEqual(result["model"], MODEL)
        self.assertEqual(result["finish_reason"], "stop")
        self.assertGreaterEqual(result["latency_ms"], 0)
        self.assertNotIn(self.key, json.dumps(result))

    def test_budget_boundaries_and_invalid_types(self):
        for budget in (1, 1024):
            with self.subTest(budget=budget):
                self.response()
                result = self.client.complete(self.messages, max_tokens=budget)
                self.assertEqual(result["request"]["max_completion_tokens"], budget)
        self.client.opener.open.reset_mock()
        for budget in (0, 1025, -1, True, False, 1.5, "10", None):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                self.client.complete(self.messages, max_tokens=budget)
        self.client.opener.open.assert_not_called()

    def test_rate_limit_is_typed_redacted_and_never_retried(self):
        detail = f"rate limit {self.key} sk-another-secret\n\x1b retry later"
        self.client.opener.open.side_effect = urllib.error.HTTPError(
            self.client.base_url, 429, "Too Many Requests", {}, io.BytesIO(detail.encode()))
        with self.assertRaises(RateLimited) as raised:
            self.client.complete(self.messages)
        self.assertIsInstance(raised.exception, KimiError)
        self.assertEqual(raised.exception.status_code, 429)
        self.assertIn("HTTP 429", str(raised.exception))
        self.assertIn("rate limit", str(raised.exception))
        self.assertNotIn(self.key, str(raised.exception))
        self.assertNotIn("sk-another-secret", str(raised.exception))
        self.assertNotIn("\x1b", str(raised.exception))
        self.assertNotIn("\n", str(raised.exception))
        self.client.opener.open.assert_called_once()

    def test_other_http_error_has_safe_bounded_diagnostic(self):
        detail = f"invalid {self.key} sk-alternate-secret " + "x" * 9000
        self.client.opener.open.side_effect = urllib.error.HTTPError(
            self.client.base_url, 401, "Unauthorized", {}, io.BytesIO(detail.encode()))
        with self.assertRaises(KimiError) as raised:
            self.client.complete(self.messages)
        self.assertNotIsInstance(raised.exception, RateLimited)
        self.assertIn("HTTP 401: invalid", str(raised.exception))
        self.assertNotIn(self.key, str(raised.exception))
        self.assertNotIn("sk-alternate-secret", str(raised.exception))
        self.assertLessEqual(len(str(raised.exception)), 515)
        self.client.opener.open.assert_called_once()

    def test_connection_error_does_not_echo_transport_details(self):
        self.client.opener.open.side_effect = urllib.error.URLError(self.key)
        with self.assertRaisesRegex(KimiError, "no automatic retry") as raised:
            self.client.complete(self.messages)
        self.assertNotIn(self.key, str(raised.exception))
        self.client.opener.open.assert_called_once()

    def test_empty_or_missing_completion_fails_safely(self):
        for content in (None, "", " \n", [], {}):
            with self.subTest(content=content):
                self.response(content=content)
                with self.assertRaisesRegex(KimiError, "no text completion"):
                    self.client.complete(self.messages)
        for raw in ({}, {"choices": []}, [], None):
            with self.subTest(raw=raw):
                self.client.opener.open.return_value = io.BytesIO(json.dumps(raw).encode())
                with self.assertRaisesRegex(KimiError, "no text completion"):
                    self.client.complete(self.messages)

    def test_malformed_json_fails_without_echoing_body(self):
        self.client.opener.open.return_value = io.BytesIO(self.key.encode())
        with self.assertRaisesRegex(KimiError, "invalid JSON") as raised:
            self.client.complete(self.messages)
        self.assertNotIn(self.key, str(raised.exception))
        self.client.opener.open.assert_called_once()

    def test_missing_or_invalid_usage_stays_visible_to_caller(self):
        for usage in (None, {}, {"total_tokens": -1}):
            with self.subTest(usage=usage):
                self.response(usage=usage)
                self.assertEqual(self.client.complete(self.messages)["usage"], usage)


if __name__ == "__main__":
    unittest.main()
