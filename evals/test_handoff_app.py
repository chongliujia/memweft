"""Exercise the standalone handoff CLI across separate consumer processes.

Each test copies the app outside this repository, clears PYTHONPATH, and uses
the installed SDK. These synthetic integration checks are not real pilot tasks.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


APP_SOURCE = Path(__file__).resolve().parents[1] / "examples" / "handoff_app"


class HandoffAppTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.app = self.root / "consumer-app"
        shutil.copytree(APP_SOURCE, self.app)
        self.cwd = self.root / "consumer-workspace"
        self.cwd.mkdir()
        self.db = self.cwd / "handoff.db"
        self.child_env = dict(os.environ)
        self.child_env.pop("PYTHONPATH", None)

    def invoke(self, *args, project="project-a", user="maintainer", expected=0):
        result = subprocess.run(
            [sys.executable, str(self.app / "handoff.py"), "--db", str(self.db),
             "--project", project, "--user", user, *args],
            cwd=self.cwd, env=self.child_env, text=True, encoding="utf-8",
            capture_output=True, timeout=30,
        )
        if expected is not None:
            self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def command(self, *args, **kwargs):
        result = self.invoke(*args, **kwargs)
        try:
            return json.loads(result.stdout)
        except ValueError:
            self.fail("Expected JSON stdout, got: " + result.stdout + result.stderr)

    def remember(self, key, content, source="maintainer confirmation", **kwargs):
        result = self.command("remember", key, content, "--source", source, **kwargs)
        self.assertEqual(result, {"saved": key})

    def resume(self, name, *options, expected=0, **kwargs):
        output = self.cwd / name
        result = self.command("resume", "继续项目交接", *options, "--out", str(output),
                              expected=expected, **kwargs)
        snapshot = json.loads((output / "snapshot.json").read_text(encoding="utf-8"))
        self.assertTrue((output / "handoff.md").is_file())
        self.assertTrue((output / "handoff.md").read_text(encoding="utf-8").strip())
        self.assertEqual(result["status"], snapshot["status"])
        self.assertEqual(snapshot["model_calls"], 0)
        self.assertEqual(snapshot["human_review_status"], "pending")
        self.assertEqual(snapshot["question"], "继续项目交接")
        return snapshot

    def test_copied_app_uses_installed_sdk_without_repository_pythonpath(self):
        self.assertNotIn("PYTHONPATH", self.child_env)
        self.assertFalse(self.app.is_relative_to(APP_SOURCE.parent.parent))
        self.remember("constraint", "交付时保留离线使用能力", source="项目负责人确认")
        snapshot = self.resume("first-handoff", "--require", "constraint")
        self.assertEqual(snapshot["status"], "ready_for_review")
        self.assertEqual(snapshot["project"], "project-a")
        self.assertEqual(snapshot["user"], "maintainer")
        self.assertEqual(snapshot["requirements"], {
            "requested": ["constraint"], "included": ["constraint"],
            "missing": [], "excluded": [], "complete": True,
        })
        self.assertEqual(len(snapshot["facts"]), 1)
        fact = snapshot["facts"][0]
        self.assertEqual(fact["key"], "constraint")
        self.assertEqual(fact["content"], "交付时保留离线使用能力")
        self.assertEqual(fact["source"], "项目负责人确认")
        self.assertTrue(fact["recorded_at"])

    def test_updates_survive_process_restart_without_duplicate_facts(self):
        self.remember("deadline", "周四", source="最初确认")
        before = self.resume("before-update", "--require", "deadline")
        self.assertEqual(before["facts"][0]["content"], "周四")
        self.remember("deadline", "周五", source="调整后的确认")
        facts = self.command("list")["facts"]
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["content"], "周五")
        self.assertEqual(facts[0]["source"], "调整后的确认")
        after = self.resume("after-update", "--require", "deadline")
        self.assertEqual(after["facts"][0]["content"], "周五")
        self.assertEqual(after["facts"][0]["source"], "调整后的确认")
        # An earlier handoff is an immutable snapshot, not a live view.
        saved = json.loads((self.cwd / "before-update" / "snapshot.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["facts"][0]["content"], "周四")

    def test_forgetting_survives_restart_and_does_not_restore_old_value(self):
        self.remember("deadline", "周五")
        self.remember("owner", "项目负责人")
        self.assertEqual(self.command("forget", "deadline"), {"forgotten": True, "key": "deadline"})
        self.assertEqual([fact["key"] for fact in self.command("list")["facts"]], ["owner"])
        snapshot = self.resume("after-forget", "--require", "deadline", expected=2)
        self.assertEqual(snapshot["status"], "incomplete")
        self.assertFalse(snapshot["requirements"]["complete"])
        self.assertEqual(snapshot["requirements"]["missing"], ["deadline"])
        self.assertNotIn("deadline", [fact["key"] for fact in snapshot["facts"]])
        self.assertEqual(self.command("forget", "deadline"), {"forgotten": False, "key": "deadline"})

    def test_projects_and_users_have_separate_memories_in_the_same_database(self):
        scopes = [("project-a", "alice", "Alice 的项目 A"),
                  ("project-b", "alice", "Alice 的项目 B"),
                  ("project-a", "bob", "Bob 的项目 A")]
        for project, user, content in scopes:
            self.remember("constraint", content, project=project, user=user)
        for index, (project, user, content) in enumerate(scopes):
            with self.subTest(project=project, user=user):
                facts = self.command("list", project=project, user=user)["facts"]
                self.assertEqual([fact["content"] for fact in facts], [content])
                snapshot = self.resume("scope-" + str(index), "--require", "constraint",
                                       project=project, user=user)
                self.assertEqual(snapshot["project"], project)
                self.assertEqual(snapshot["user"], user)
                self.assertEqual([fact["content"] for fact in snapshot["facts"]], [content])
        unseen = self.resume("unseen-scope", "--require", "constraint", project="project-b",
                             user="bob", expected=2)
        self.assertEqual(unseen["facts"], [])
        self.assertEqual(unseen["requirements"]["missing"], ["constraint"])

    def test_missing_required_fact_is_reported_in_persisted_incomplete_snapshot(self):
        self.remember("owner", "项目负责人")
        snapshot = self.resume("missing-input", "--require", "owner", "--require", "deadline", expected=2)
        self.assertEqual(snapshot["status"], "incomplete")
        self.assertEqual(snapshot["requirements"], {
            "requested": ["owner", "deadline"], "included": ["owner"],
            "missing": ["deadline"], "excluded": [], "complete": False,
        })

    def test_fact_budget_exclusion_is_not_misreported_as_a_missing_fact(self):
        self.remember("owner", "项目负责人")
        self.remember("deadline", "周五")
        snapshot = self.resume("fact-budget", "--require", "owner", "--require", "deadline",
                               "--max-facts", "1", expected=2)
        self.assertEqual(snapshot["status"], "incomplete")
        requirements = snapshot["requirements"]
        self.assertFalse(requirements["complete"])
        self.assertEqual(requirements["missing"], [])
        self.assertEqual(requirements["included"], ["owner"])
        self.assertEqual(requirements["excluded"], ["deadline"])
        self.assertEqual([fact["key"] for fact in snapshot["facts"]], ["owner"])

    def test_token_budget_exclusion_keeps_snapshot_explicitly_incomplete(self):
        self.remember("constraint", "完整的项目交付约束。" * 100)
        snapshot = self.resume("token-budget", "--require", "constraint", "--max-tokens", "1", expected=2)
        self.assertEqual(snapshot["status"], "incomplete")
        self.assertEqual(snapshot["requirements"], {
            "requested": ["constraint"], "included": [], "missing": [],
            "excluded": ["constraint"], "complete": False,
        })
        self.assertEqual(snapshot["facts"], [])

    def test_existing_output_is_not_overwritten_and_database_bytes_do_not_change(self):
        self.remember("owner", "项目负责人")
        self.resume("existing-output", "--require", "owner")
        output = self.cwd / "existing-output"
        before = {path.name: path.read_bytes() for path in output.iterdir() if path.is_file()}
        db_before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        result = self.invoke("resume", "不同的问题", "--require", "owner", "--out", str(output), expected=None)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual({path.name: path.read_bytes() for path in output.iterdir() if path.is_file()}, before)
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), db_before)

    def test_non_utf8_stdout_preserves_json_and_success_or_incomplete_exit_codes(self):
        self.child_env["PYTHONIOENCODING"] = "ascii"
        self.remember("constraint", "交付时保留离线使用能力", source="项目负责人确认")
        listed = self.command("list")
        self.assertEqual(listed["facts"][0]["content"], "交付时保留离线使用能力")
        snapshot = self.resume("ascii-complete", "--require", "constraint")
        self.assertEqual(snapshot["status"], "ready_for_review")
        markdown = (self.cwd / "ascii-complete" / "handoff.md").read_text(encoding="utf-8")
        self.assertIn("交付时保留离线使用能力", markdown)
        incomplete = self.resume("ascii-incomplete", "--require", "unknown", expected=2)
        self.assertEqual(incomplete["requirements"]["missing"], ["unknown"])

    def test_remember_requires_an_explicit_source(self):
        result = self.invoke("remember", "owner", "未提供来源", expected=None)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.command("list")["facts"], [])


if __name__ == "__main__":
    unittest.main()
