"""Cross-process configured continuation checks using a copied standalone app.

These are synthetic regression fixtures, not user evaluations or model calls.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


APP_SOURCE = Path(__file__).resolve().parents[1] / "examples" / "handoff_app"


class HandoffContinuationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.app = self.root / "copied-app"
        shutil.copytree(APP_SOURCE, self.app, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        self.workspace = self.root / "consumer-project"
        self.workspace.mkdir()
        self.config = self.workspace / "handoff.json"
        self.cwd = self.root / "unrelated-cwd"
        self.cwd.mkdir()
        self.env = dict(os.environ)
        for key in ("PYTHONPATH", "PYTHONHOME", "MOONSHOT_API_KEY"):
            self.env.pop(key, None)
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"

    def argv(self, *args):
        return [sys.executable, str(self.app / "handoff.py"), "--config", str(self.config), *args]

    def invoke(self, *args, expected=0):
        process = subprocess.run(self.argv(*args), cwd=self.cwd, env=self.env,
                                 text=True, encoding="utf-8", capture_output=True, timeout=20)
        if expected is not None:
            self.assertEqual(process.returncode, expected, process.stdout + process.stderr)
        return process

    def command(self, *args, expected=0):
        process = self.invoke(*args, expected=expected)
        try:
            return json.loads(process.stdout)
        except ValueError:
            self.fail("Expected JSON response: " + process.stdout + process.stderr)

    def reject(self, *args):
        process = self.invoke(*args, expected=None)
        self.assertNotEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertTrue(process.stderr.strip(), process.stdout)
        return process

    def init(self, *required):
        args = ["--project", "demo", "init"]
        for key in required or ("goal",):
            args += ["--require", key]
        return self.command(*args)

    def remember(self, key="goal", value="完成明确登记的交付并保留验收记录"):
        return self.command("remember", key, value, "--source", "synthetic regression fixture")

    def setup_task(self, script=None):
        self.init()
        self.remember()
        (self.workspace / "output.txt").write_text("ready", encoding="utf-8")
        script = script or (
            "from pathlib import Path\n"
            "assert Path('output.txt').read_text() in ('ready', 'updated')\n"
            "with Path('executions.log').open('a') as log: log.write('ran\\n')\n"
            "print('checked delivery')\n"
        )
        (self.workspace / "verify.py").write_text(script, encoding="utf-8")
        return self.command("task", "add", "delivery", "核验实际交付", "--verify", "verify.py",
                            "--artifact", "output.txt")

    def task(self):
        tasks = self.command("task", "list")["tasks"]
        self.assertEqual(len(tasks), 1)
        return tasks[0]

    def complete(self, revision=1, expected=0):
        return self.command("task", "complete", "delivery", "--revision", str(revision), expected=expected)

    def resume(self, *options, expected=0):
        return self.command("resume", "继续已登记的工作", "--max-facts", "50", "--max-tokens", "10000",
                            *options, expected=expected)

    def test_init_refuses_overwrite_and_configured_scope_overrides(self):
        self.reject("--project", "demo", "--db", ".", "init")
        self.assertFalse(self.config.exists())
        self.command("--project", "demo", "--db", "data/custom.db", "init", "--require", "goal")
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8"))["database"], "data/custom.db")
        before = self.config.read_bytes()
        self.reject("--project", "replacement", "init")
        self.assertEqual(self.config.read_bytes(), before)
        for option, value in (("--project", "other"), ("--user", "other"), ("--db", "other.db")):
            with self.subTest(option=option):
                self.reject(option, value, "remember", "goal", "wrong target", "--source", "fixture")
        self.assertEqual(self.command("list")["facts"], [])
        self.assertFalse((self.cwd / "other.db").exists())

    def test_project_can_move_and_write_back_from_an_unrelated_cwd(self):
        self.setup_task()
        before = self.workspace / "data/handoff.db"
        self.assertTrue(before.is_file())
        moved = self.root / "relocated-project"
        shutil.move(str(self.workspace), moved)
        self.workspace = moved
        self.config = moved / "handoff.json"
        written = self.remember(value="搬迁后更新")
        self.assertEqual(Path(written["writeback"]["database"]), moved / "data/handoff.db")
        self.assertFalse(before.exists())
        recovered = self.resume("--task", "delivery")
        self.assertEqual(recovered["facts"][0]["content"], "搬迁后更新")
        self.assertEqual(recovered["tasks"][0]["id"], "delivery")
        self.assertTrue(Path(recovered["output"]).is_relative_to(moved))
        self.assertEqual(self.complete()["status"], "completed")
        self.assertEqual(self.task()["status"], "done")
        self.assertFalse((self.cwd / "data").exists())

    def test_doctor_reports_an_unloadable_sdk_without_creating_project_database(self):
        self.init()
        # Simulate a broken native-wheel import at the copied consumer boundary.
        (self.app / "memweft.py").write_text("raise ImportError('synthetic incompatible native wheel')\n",
                                               encoding="utf-8")
        report = self.command("doctor", expected=2)
        self.assertEqual(report["status"], "needs_setup")
        sdk = next(check for check in report["checks"] if check["name"] == "sdk")
        self.assertFalse(sdk["ok"])
        self.assertEqual(sdk["detail"]["error_type"], "ImportError")
        self.assertEqual(report["model_calls"], 0)
        self.assertFalse((self.workspace / "data/handoff.db").exists())

    def test_required_facts_remain_mandatory_and_budget_exclusions_stay_explicit(self):
        self.setup_task()
        self.command("forget", "goal")
        missing = self.resume("--task", "delivery", "--model", "kimi", expected=2)
        self.assertIn("goal", missing["requirements"]["missing"])
        self.assertEqual(missing["planning"]["status"], "needs_context")
        self.assertEqual(missing["model_calls"], 0)
        self.remember()
        self.remember("extra", "额外的显式依赖")
        limited = self.resume("--task", "delivery", "--require", "extra", "--max-facts", "1", expected=2)
        self.assertEqual(limited["requirements"]["missing"], [])
        self.assertIn("goal", limited["requirements"]["requested"])
        self.assertIn("extra", limited["requirements"]["excluded"])
        self.assertIn("task:delivery", limited["requirements"]["excluded"])
        tiny = self.resume("--task", "delivery", "--max-tokens", "1", expected=2)
        self.assertEqual(tiny["requirements"]["missing"], [])
        self.assertFalse(tiny["requirements"]["complete"])
        self.assertTrue(tiny["requirements"]["excluded"])
        self.assertEqual(self.task()["status"], "pending")

    def test_duplicate_task_and_stale_revision_do_not_replace_or_execute_work(self):
        created = self.setup_task()["task"]
        self.reject("task", "add", "delivery", "替代标题", "--verify", "verify.py", "--artifact", "output.txt")
        after = self.task()
        self.assertEqual((after["title"], after["revision"]), (created["title"], 1))
        self.reject("task", "complete", "delivery", "--revision", "2")
        self.assertFalse((self.workspace / "executions.log").exists())
        self.assertEqual(self.task()["status"], "pending")

    def test_failed_verifier_preserves_diagnostics_and_pending_revision(self):
        self.setup_task("import sys\nprint('failure evidence')\nprint('check failed', file=sys.stderr)\nraise SystemExit(7)\n")
        result = self.complete(expected=2)
        self.assertEqual(result["status"], "verification_failed")
        evidence_path = Path(result["evidence"])
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        self.assertEqual(evidence["returncode"], 7)
        self.assertEqual(evidence["status"], "failed")
        self.assertIn("failure evidence", (evidence_path.parent / "stdout.log").read_text())
        self.assertIn("check failed", (evidence_path.parent / "stderr.log").read_text())
        task = self.task()
        self.assertEqual((task["status"], task["revision"]), ("pending", 1))
        self.assertNotIn("completion", task)

    def test_changed_frozen_verifier_is_refused_before_execution(self):
        self.setup_task()
        with (self.workspace / "verify.py").open("a", encoding="utf-8") as stream:
            stream.write("# verifier changed after task registration\n")
        self.reject("task", "complete", "delivery", "--revision", "1")
        self.assertFalse((self.workspace / "executions.log").exists())
        self.assertEqual(self.task()["verification"]["status"], "verifier_changed")
        self.assertEqual(self.task()["status"], "pending")

    def test_verifier_mutating_delivery_cannot_commit_success(self):
        self.setup_task("from pathlib import Path\nPath('output.txt').write_text('changed while checking')\n")
        result = self.complete(expected=2)
        self.assertEqual(result["verification"]["returncode"], 0)
        self.assertEqual(result["verification"]["status"], "stale")
        self.assertEqual((self.task()["status"], self.task()["revision"]), ("pending", 1))

    def test_configuration_change_during_verification_blocks_completion(self):
        self.setup_task("import json\nfrom pathlib import Path\np=Path('handoff.json')\nv=json.loads(p.read_text())\nv['required_facts'].append('new_requirement')\np.write_text(json.dumps(v))\n")
        result = self.complete(expected=2)
        self.assertEqual(result["verification"]["status"], "stale")
        self.assertIn("configuration changed", result["verification"]["error"].lower())
        self.assertEqual((self.task()["status"], self.task()["revision"]), ("pending", 1))

    def test_success_survives_restart_and_duplicate_complete_does_not_rerun(self):
        self.setup_task()
        prior = self.resume("--task", "delivery")
        prior_path = Path(prior["output"]) / "snapshot.json"
        frozen_prior = prior_path.read_bytes()
        completed = self.complete()
        self.assertEqual(completed["status"], "completed")
        task = self.task()
        self.assertEqual((task["status"], task["revision"]), ("done", 2))
        self.assertNotIn("\\", task["completion"]["evidence"])
        evidence = self.workspace / task["completion"]["evidence"]
        self.assertEqual(hashlib.sha256(evidence.read_bytes()).hexdigest(), task["completion"]["evidence_sha256"])
        self.assertEqual(self.complete(revision=2)["status"], "already_completed")
        recovered = self.resume("--task", "delivery", "--model", "kimi")
        self.assertEqual(recovered["planning"]["status"], "completed")
        self.assertEqual(recovered["model_calls"], 0)
        self.assertEqual(recovered["human_review_status"], "pending")
        self.assertEqual((self.workspace / "executions.log").read_text(), "ran\n")
        self.assertEqual(prior_path.read_bytes(), frozen_prior)
        self.reject("task", "complete", "delivery", "--revision", "1")
        self.assertEqual((self.workspace / "executions.log").read_text(), "ran\n")

    def test_changed_artifact_requires_explicit_reopen_and_fresh_verification(self):
        self.setup_task()
        completed = self.complete()
        old_evidence = Path(completed["evidence"])
        old_bytes = old_evidence.read_bytes()
        (self.workspace / "output.txt").write_text("updated", encoding="utf-8")
        result = self.resume("--task", "delivery", expected=2)
        self.assertEqual(result["planning"]["status"], "needs_context")
        self.assertEqual(result["tasks"][0]["verification"]["status"], "artifacts_changed")
        self.reject("task", "complete", "delivery", "--revision", "2")
        self.assertEqual((self.workspace / "executions.log").read_text(), "ran\n")
        reopened = self.command("task", "reopen", "delivery", "--revision", "2", "--reason", "交付文件按要求更新")
        self.assertEqual((reopened["task"]["status"], reopened["task"]["revision"]), ("pending", 3))
        self.assertEqual(self.complete(revision=3)["task"]["revision"], 4)
        self.assertEqual((self.workspace / "executions.log").read_text(), "ran\nran\n")
        self.assertEqual(old_evidence.read_bytes(), old_bytes)

    def test_changed_verification_log_invalidates_completed_evidence(self):
        self.setup_task()
        completed = self.complete()
        log = Path(completed["evidence"]).parent / "stdout.log"
        log.write_text("altered historical evidence\n", encoding="utf-8")
        result = self.resume("--task", "delivery", "--model", "kimi", expected=2)
        self.assertEqual(result["planning"]["status"], "needs_context")
        self.assertEqual(result["model_calls"], 0)
        self.assertNotEqual(result["tasks"][0]["verification"]["status"], "passed")

    def test_state_changed_during_planning_revokes_proposal_in_every_export(self):
        self.setup_task()
        # This copied offline planner does not import the HTTP client or call a model.
        # It simulates another cooperating CLI changing state while planning runs unlocked.
        (self.app / "planner.py").write_text(
            "import subprocess, sys\nfrom pathlib import Path\n"
            "def plan(snapshot, prompt_key=False):\n"
            "    subprocess.run([sys.executable, str(Path(__file__).with_name('handoff.py')), "
            "'--config', " + repr(str(self.config)) + ", 'remember', 'goal', 'new observation', "
            "'--source', 'synthetic concurrent writer'], check=True, capture_output=True)\n"
            "    return {'status': 'proposed', 'reason': 'offline fixture', 'model_calls': 0, "
            "'human_review': 'pending', 'proposal': {'task_id': 'delivery', "
            "'next_step': 'continue', 'reason': 'pending'}}\n", encoding="utf-8")
        result = self.resume("--task", "delivery", "--model", "kimi", expected=2)
        self.assertEqual(result["planning"]["status"], "stale")
        self.assertIsNone(result["planning"]["proposal"])
        output = Path(result["output"])
        raw = json.loads((output / "model-response.json").read_text(encoding="utf-8"))
        self.assertEqual(raw["state_check"], "pending")
        self.assertEqual(raw["unvalidated_planning"]["status"], "proposed")
        persisted = json.loads((output / "planning.json").read_text(encoding="utf-8"))
        attempt = json.loads((output / "attempt.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted, result["planning"])
        self.assertEqual(attempt["planning"], persisted)
        self.assertEqual(result["model_calls"], 0)
        self.assertEqual(self.task()["status"], "pending")

    def test_active_verification_excludes_a_second_cooperating_cli(self):
        self.setup_task("from pathlib import Path\nimport time\nPath('started').write_text('yes')\nwhile not Path('release').exists(): time.sleep(0.02)\nprint('one verifier')\n")
        first = subprocess.Popen(self.argv("task", "complete", "delivery", "--revision", "1"),
                                 cwd=self.cwd, env=self.env, text=True, encoding="utf-8",
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 8
            while not (self.workspace / "started").exists() and first.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue((self.workspace / "started").exists(), "First verifier did not reach the barrier")
            second = self.reject("task", "complete", "delivery", "--revision", "1")
            self.assertIn("locked", second.stderr.lower())
            (self.workspace / "release").write_text("continue", encoding="utf-8")
            stdout, stderr = first.communicate(timeout=10)
            self.assertEqual(first.returncode, 0, stdout + stderr)
            self.assertEqual(json.loads(stdout)["status"], "completed")
        finally:
            if first.poll() is None:
                (self.workspace / "release").write_text("cleanup", encoding="utf-8")
                try:
                    first.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    first.kill()
                    first.communicate(timeout=3)
        self.assertEqual((self.task()["status"], self.task()["revision"]), ("done", 2))


if __name__ == "__main__":
    unittest.main()
