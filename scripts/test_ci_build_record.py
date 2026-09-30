"""Local, offline checks for the CI build-start/build-finish boundary."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ci_build_record import begin, finish


class BuildRecordTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.environment = patch.dict(os.environ, {key: value for key, value in os.environ.items()
            if not key.startswith(("GITHUB_", "RUNNER_"))}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(self.directory.cleanup)
        self.git("init", "--quiet")
        (self.root / ".github/workflows").mkdir(parents=True)
        (self.root / ".github/workflows/verify.yml").write_bytes(b"name: verify\non: push\n")
        (self.root / ".gitignore").write_text("dist/\n", encoding="utf-8")
        self.git("add", ".")
        self.commit()
        self.dist = self.root / "dist"
        self.start = self.dist / "build-source.json"
        self.output = self.dist / "build-record.json"

    def git(self, *arguments):
        return subprocess.check_output(["git", "-C", str(self.root), *arguments], stderr=subprocess.PIPE)

    def commit(self):
        self.git("-c", "user.name=CI test", "-c", "user.email=ci-test@example.invalid",
                 "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "fixture")

    def test_clean_build_records_exact_source_workflow_and_artifact_bytes(self):
        head = self.git("rev-parse", "HEAD").decode().strip()
        with patch.dict(os.environ, {"GITHUB_SHA": head, "GITHUB_RUN_ID": "42", "GITHUB_RUN_ATTEMPT": "2",
                                    "GITHUB_JOB": "python-wheel", "GITHUB_TOKEN": "must-not-record"}):
            begin(self.root, self.start)
            (self.dist / "example.whl").write_bytes(b"fresh package")
            finish(self.root, self.start, self.output, wheel_dir=self.dist)
        record = json.loads(self.output.read_text())
        self.assertEqual(record["build"]["source"], {"commit_sha": head, "dirty": False})
        self.assertEqual(record["build"]["github"]["run_attempt"], "2")
        self.assertEqual(record["artifacts"]["example.whl"], hashlib.sha256(b"fresh package").hexdigest())
        self.assertEqual(record["build"]["workflow_sha256"], hashlib.sha256(b"name: verify\non: push\n").hexdigest())
        self.assertNotIn("must-not-record", self.output.read_text())
        self.assertFalse(record["signed_attestation"])

    def test_dirty_or_wrong_commit_start_is_rejected(self):
        with patch.dict(os.environ, {"GITHUB_SHA": "a" * 40}):
            with self.assertRaisesRegex(ValueError, "GITHUB_SHA"):
                begin(self.root, self.start)
        (self.root / "uncommitted.py").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "clean checkout"):
            begin(self.root, self.start)

    def test_preexisting_package_cannot_be_attributed_to_new_build(self):
        self.dist.mkdir()
        (self.dist / "old.tgz").write_bytes(b"old")
        with self.assertRaisesRegex(ValueError, "existing"):
            begin(self.root, self.start)

    def test_source_changes_between_start_and_finish_are_rejected(self):
        begin(self.root, self.start)
        (self.dist / "example.whl").write_bytes(b"fresh")
        (self.root / ".github/workflows/verify.yml").write_text("name: changed\n", encoding="utf-8")
        self.git("add", ".github/workflows/verify.yml")
        self.commit()
        with self.assertRaisesRegex(ValueError, "changed during"):
            finish(self.root, self.start, self.output, wheel_dir=self.dist)

    def test_changed_run_missing_or_ambiguous_packages_are_rejected(self):
        begin(self.root, self.start)
        with patch.dict(os.environ, {"GITHUB_RUN_ATTEMPT": "2"}):
            with self.assertRaisesRegex(ValueError, "changed during"):
                finish(self.root, self.start, self.output, wheel_dir=self.dist)
        with self.assertRaisesRegex(ValueError, "exactly one"):
            finish(self.root, self.start, self.output, wheel_dir=self.dist)
        (self.dist / "first.whl").write_bytes(b"first")
        (self.dist / "second.whl").write_bytes(b"second")
        with self.assertRaisesRegex(ValueError, "exactly one"):
            finish(self.root, self.start, self.output, wheel_dir=self.dist)


if __name__ == "__main__":
    unittest.main()
