"""Completion-control tests use local fake handlers; no SDK or model is needed."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
from verified_completion import TrustedTool, VerifiedSession, file_snapshot


def message(*actions, done=False):
    return json.dumps({"actions": list(actions), "done": done})


READ = {"tool": "read"}
WRITE = {"tool": "write"}
VERIFY = {"tool": "run"}


class VerifiedCompletionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "output.json"
        self.calls = []
        self.check_result = {"ok": True}

        def read(action):
            self.calls.append(action)
            return self.path.read_text() if self.path.exists() else None

        def write(action):
            self.calls.append(action)
            self.path.write_text("changed", encoding="utf-8")
            return {"written": "output.json"}

        def verify(action):
            self.calls.append(action)
            return self.check_result

        self.tools = {"read": TrustedTool(read, "read"),
                      "write": TrustedTool(write, "mutate"),
                      "run": TrustedTool(verify, "verify")}
        self.session = self.new_session()

    def new_session(self, **kwargs):
        return VerifiedSession(self.tools, lambda: file_snapshot(self.root), **kwargs)

    def test_write_check_done_finishes_without_extra_calls(self):
        result = self.session.step(message(WRITE, VERIFY, done=True))
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["done_accepted"])
        self.assertEqual(result["completion"], {"verified": True, "reason": "verified"})
        self.assertEqual(self.calls, [WRITE, VERIFY])

    def test_unverified_done_is_feedback_without_automatic_check(self):
        result = self.session.step(message(WRITE, done=True))
        self.assertFalse(result["done_accepted"])
        self.assertTrue(result["done_requested"])
        self.assertEqual(result["completion"]["reason"], "verification_required")
        self.assertEqual(result["rounds_remaining"], 3)
        self.assertEqual(self.calls, [WRITE])
        result = self.session.step(message(VERIFY, done=True))
        self.assertTrue(result["done_accepted"])

    def test_check_without_done_never_completes(self):
        result = self.session.step(message(VERIFY))
        self.assertTrue(result["completion"]["verified"])
        self.assertEqual(result["status"], "active")
        self.assertFalse(result["done_accepted"])
        self.assertTrue(self.session.step(message(READ, done=True))["done_accepted"])

    def test_later_write_in_same_or_following_turn_invalidates_check(self):
        for same_turn in (False, True):
            with self.subTest(same_turn=same_turn):
                session = self.new_session()
                if same_turn:
                    result = session.step(message(VERIFY, WRITE, done=True))
                else:
                    session.step(message(VERIFY))
                    result = session.step(message(WRITE, done=True))
                self.assertFalse(result["completion"]["verified"])
                self.assertFalse(result["done_accepted"])

    def test_partial_write_exception_invalidates_before_call(self):
        def fail(_action):
            self.path.write_text("partial", encoding="utf-8")
            raise OSError("disk write failed")
        self.tools["write"] = TrustedTool(fail, "mutate")
        session = self.new_session()
        session.step(message(VERIFY))
        result = session.step(message(WRITE, done=True))
        self.assertFalse(result["done_accepted"])
        self.assertFalse(result["completion"]["verified"])
        self.assertIn("disk write failed", result["results"][0]["error"])

    def test_noop_or_rejected_mutation_also_requires_reverification(self):
        def reject(_action):
            raise ValueError("write path not permitted")
        for handler in (lambda _action: {"removed": "absent"}, reject):
            self.tools["remove"] = TrustedTool(handler, "mutate")
            session = self.new_session()
            session.step(message(VERIFY))
            result = session.step(message({"tool": "remove"}, done=True))
            self.assertFalse(result["completion"]["verified"])
            self.assertFalse(session.step(message(done=True))["done_accepted"])

    def test_verify_requires_actual_boolean_true_and_invalidates_old_receipt(self):
        for value in ({"ok": False}, {"ok": 1}, {"ok": "true"}, {}, True, None):
            with self.subTest(value=value):
                self.check_result = {"ok": True}
                session = self.new_session()
                session.step(message(VERIFY))
                self.check_result = value
                result = session.step(message(VERIFY, done=True))
                self.assertFalse(result["done_accepted"])
                self.assertFalse(result["completion"]["verified"])
                self.assertEqual(result["results"][0]["result"], value)

    def test_failed_check_after_success_cannot_reuse_success(self):
        self.tools["run"] = TrustedTool(Mock(side_effect=[{"ok": True}, {"ok": False}]), "verify")
        result = self.new_session().step(message(VERIFY, VERIFY, done=True))
        self.assertFalse(result["done_accepted"])
        self.assertFalse(result["completion"]["verified"])

    def test_tool_error_and_later_good_check_still_rejects_current_turn(self):
        self.tools["read"] = TrustedTool(Mock(side_effect=RuntimeError("read failed")), "read")
        session = self.new_session()
        result = session.step(message(READ, VERIFY, done=True))
        self.assertFalse(result["done_accepted"])
        self.assertTrue(result["completion"]["verified"])
        self.assertEqual(result["completion"]["reason"], "tool_error")
        self.assertTrue(session.step(message(done=True))["done_accepted"])

    def test_failed_check_then_good_check_requires_another_turn(self):
        self.tools["run"] = TrustedTool(Mock(side_effect=[{"ok": False}, {"ok": True}]), "verify")
        session = self.new_session()
        result = session.step(message(VERIFY, VERIFY, done=True))
        self.assertFalse(result["done_accepted"])
        self.assertTrue(result["completion"]["verified"])
        self.assertTrue(session.step(message(done=True))["done_accepted"])

    def test_verify_exception_and_snapshot_failure_leave_no_receipt(self):
        for handler in (Mock(side_effect=OSError("check failed")), lambda _action: {"ok": True}):
            self.tools["run"] = TrustedTool(handler, "verify")
            session = VerifiedSession(self.tools, Mock(side_effect=OSError("snapshot failed")))
            result = session.step(message(VERIFY, done=True))
            self.assertFalse(result["completion"]["verified"])
            self.assertFalse(result["done_accepted"])
            self.assertIn("error", result["results"][0])

    def test_external_change_add_delete_or_rename_invalidates_receipt(self):
        for change in (lambda: self.path.write_text("new", encoding="utf-8"),
                       lambda: self.path.unlink(),
                       lambda: self.path.rename(self.root / "renamed.json"),
                       lambda: (self.root / "added.json").write_text("added", encoding="utf-8")):
            with self.subTest(change=change):
                self.path.write_text("original", encoding="utf-8")
                session = self.new_session()
                session.step(message(VERIFY))
                change()
                result = session.step(message(done=True))
                self.assertFalse(result["done_accepted"])
                self.assertEqual(result["completion"]["reason"], "project_changed")

    def test_read_handler_that_changes_files_cannot_preserve_receipt(self):
        self.tools["read"] = TrustedTool(lambda _action: self.path.write_text("changed"), "read")
        session = self.new_session()
        result = session.step(message(VERIFY, READ, done=True))
        self.assertFalse(result["done_accepted"])
        self.assertEqual(result["completion"]["reason"], "project_changed")

    def test_snapshot_failure_at_done_invalidates_receipt(self):
        snapshot = Mock(side_effect=["verified", "verified", OSError("gone")])
        session = VerifiedSession(self.tools, snapshot)
        session.step(message(VERIFY))
        result = session.step(message(done=True))
        self.assertFalse(result["done_accepted"])
        self.assertEqual(result["completion"]["reason"], "snapshot_failed")

    def test_strict_protocol_rejects_forged_receipts_duplicates_nan_and_truncation(self):
        invalid = ["[]", "null", "NaN", '{"actions":[],"done":true,"done":false}',
                   '{"actions":[],"done":1}', '{"actions":{},"done":false}',
                   '{"actions":[],"done":true,"receipt":"fake"}',
                   '{"actions":[{"tool":"read","tool":"run"}],"done":true}',
                   message(READ, READ, READ, READ), "{"]
        for content in invalid:
            session = self.new_session()
            result = session.step(content)
            self.assertTrue(result["protocol_error"], content)
            self.assertFalse(result["done_accepted"])
            self.assertEqual(result["rounds_remaining"], 3)
        self.assertTrue(self.new_session().step(message(VERIFY, done=True), "length")["protocol_error"])
        self.assertEqual(self.calls, [])

    def test_protocol_error_does_not_complete_and_next_turn_may_retry(self):
        self.session.step(message(VERIFY))
        result = self.session.step('{"actions":[],"done":true,"fake":true}')
        self.assertFalse(result["done_accepted"])
        self.assertTrue(self.session.step(message(done=True))["done_accepted"])

    def test_unknown_tool_invalid_action_and_forged_effect_are_handler_data(self):
        for action in ([], {"tool": "missing"}, {"tool": []}):
            result = self.new_session().step(message(action, done=True))
            self.assertFalse(result["done_accepted"])
            self.assertIn("error", result["results"][0])
        result = self.new_session().step(message({"tool": "read", "effect": "verify", "ok": True}, done=True))
        self.assertFalse(result["done_accepted"])

    def test_last_round_without_accepted_done_exhausts_even_when_verified(self):
        for actions, done in (((), True), ((VERIFY,), False), ((WRITE,), True)):
            with self.subTest(actions=actions, done=done):
                result = self.new_session(max_rounds=1).step(message(*actions, done=done))
                self.assertEqual(result["status"], "budget_exhausted")
                self.assertFalse(result["done_accepted"])
                self.assertEqual(result["rounds_remaining"], 0)

    def test_last_round_can_complete_and_terminal_retries_execute_nothing(self):
        for complete in (False, True):
            session = self.new_session(max_rounds=1)
            first = session.step(message(*([VERIFY] if complete else []), done=True))
            calls = list(self.calls)
            again = session.step(message(WRITE, VERIFY, done=True))
            self.assertEqual(again["status"], first["status"])
            self.assertFalse(again["done_accepted"])
            self.assertEqual(again["rounds_remaining"], 0)
            self.assertEqual(again["results"], [])
            self.assertEqual(self.calls, calls)

    def test_registry_is_copied_and_configuration_is_checked(self):
        session = self.new_session()
        self.tools["read"] = TrustedTool(lambda _action: {"ok": True}, "verify")
        self.assertFalse(session.step(message(READ, done=True))["done_accepted"])
        for kwargs in ({"max_rounds": True}, {"max_rounds": 0}, {"max_actions": 0}):
            with self.assertRaises(ValueError):
                self.new_session(**kwargs)
        with self.assertRaises(ValueError):
            TrustedTool(lambda _action: None, "model_declared")
        with self.assertRaises(ValueError):
            VerifiedSession({"fake": lambda _: None}, lambda: "hash")

    def test_completed_status_is_historical_not_a_second_delivery_event(self):
        first = self.session.step(message(WRITE, VERIFY, done=True))
        self.assertTrue(first["done_accepted"])
        calls, remaining = list(self.calls), self.session.rounds_remaining
        self.path.write_text("changed outside the session", encoding="utf-8")
        again = self.session.step(message(WRITE, VERIFY, done=True))
        self.assertEqual(again["status"], "completed")
        self.assertFalse(again["done_accepted"])
        self.assertFalse(again["completion"]["verified"])
        self.assertEqual(again["results"], [])
        self.assertEqual(again["rounds_remaining"], remaining)
        self.assertEqual(self.calls, calls)


class FileSnapshotTests(unittest.TestCase):
    def test_path_and_contents_affect_snapshot_but_creation_order_does_not(self):
        with tempfile.TemporaryDirectory() as directory:
            left, right = Path(directory) / "left", Path(directory) / "right"
            left.mkdir()
            right.mkdir()
            for root, names in ((left, ("a", "b")), (right, ("b", "a"))):
                for name in names:
                    (root / name).write_bytes(b"binary\x00\xff")
            self.assertEqual(file_snapshot(left), file_snapshot(right))
            (right / "a").write_bytes(b"different")
            self.assertNotEqual(file_snapshot(left), file_snapshot(right))

    def test_missing_root_and_file_root_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(OSError):
                file_snapshot(root / "missing")
            (root / "file").touch()
            with self.assertRaises(ValueError):
                file_snapshot(root / "file")

    def test_symlink_files_directories_roots_and_broken_links_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "root"
            root.mkdir()
            target = base / "target"
            target.mkdir()
            (target / "file").write_text("outside", encoding="utf-8")
            link = root / "link"
            for destination in (target / "file", target, base / "missing"):
                try:
                    link.symlink_to(destination, target_is_directory=destination.is_dir())
                except OSError as error:
                    self.skipTest(f"Creating symlinks is unavailable: {error}")
                with self.assertRaises(ValueError):
                    file_snapshot(root)
                with self.assertRaises(ValueError):
                    file_snapshot(link)
                link.unlink()

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO support is unavailable")
    def test_special_file_is_rejected_without_opening_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            os.mkfifo(root / "fifo")
            with self.assertRaises(ValueError):
                file_snapshot(root)


if __name__ == "__main__":
    unittest.main()
