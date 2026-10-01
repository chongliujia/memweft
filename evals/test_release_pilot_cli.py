"""Exercise the pilot CLI with native memory and a local, deterministic client."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import kimi_release_agent as release
from kimi_memory import KimiError


class PilotCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.db = self.root / "memory.db"
        self.agent = release.ReleaseAgent(self.db, self.root)

    def invoke(self, *args):
        argv = ["release", "--repo", str(self.root), "--db", str(self.db), *map(str, args)]
        with patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
            return release.main()

    def remember_decisions(self):
        self.agent.remember("target", "developer_preview")
        self.agent.remember("delivery", "downloadable_artifacts")

    def test_prepare_and_missing_decisions_need_no_model_credentials(self):
        with patch.object(release, "read_api_key") as key, patch.object(release, "KimiClient") as client:
            for name, flags in (("prepare", ["--prepare-only"]), ("missing", [])):
                output = self.root / name
                self.assertEqual(self.invoke("plan", "--run-dir", output, *flags), 0)
                record = json.loads((output / "attempt.json").read_text())
                self.assertIn("clarify_decisions", json.dumps(record))
            key.assert_not_called()
            client.assert_not_called()

    def test_rejected_model_proposal_is_saved_before_exit(self):
        self.remember_decisions()
        answer = {"content": json.dumps({"target": "bounded_production", "delivery": "downloadable_artifacts",
            "next_action": "review_release_evidence", "ready": False, "reason": "错误目标"}),
            "finish_reason": "stop", "usage": {"prompt_tokens": 12, "completion_tokens": 7}, "latency_ms": 4.0}
        with patch.object(release, "read_api_key", return_value="test-placeholder"), patch.object(release, "KimiClient") as client:
            client.return_value.complete.return_value = answer
            output = self.root / "rejected"
            self.assertEqual(self.invoke("plan", "--run-dir", output), 2)
            record = json.loads((output / "attempt.json").read_text())
            self.assertIn("decision_contradicts_memory", json.dumps(record))
            self.assertIn("bounded_production", json.dumps(record))
            client.return_value.complete.assert_called_once()

    def test_transport_error_is_recorded_without_exception_details(self):
        self.remember_decisions()
        with patch.object(release, "read_api_key", return_value="test-placeholder"), patch.object(release, "KimiClient") as client:
            client.return_value.complete.side_effect = KimiError("private diagnostic must not enter journal")
            output = self.root / "failed"
            with self.assertRaises(KimiError):
                self.invoke("plan", "--run-dir", output)
            text = (output / "attempt.json").read_text()
            self.assertIn("model_request_failed", text)
            self.assertNotIn("private diagnostic", text)

    def test_existing_attempt_is_rejected_before_calling_model(self):
        self.remember_decisions()
        output = self.root / "existing"
        output.mkdir()
        with patch.object(release, "KimiClient") as client, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.invoke("plan", "--run-dir", output)
            client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
