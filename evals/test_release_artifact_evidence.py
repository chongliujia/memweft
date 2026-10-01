"""Offline artifact-evidence regressions; fixture archives are never installed."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
from release_artifact_evidence import REQUIRED_CHECKS, collect_artifact_evidence


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ArtifactEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name).resolve()
        self.git("init", "--quiet")
        self.git("config", "core.autocrlf", "false")
        for relative, content in {
            ".github/workflows/verify.yml": "name: verify\non: push\n",
            "python/pyproject.toml": '[project]\nname = "memweft"\nversion = "0.2.0a1"\n',
            "typescript/package.json": '{"name":"memweft","version":"0.2.0-alpha.1"}\n',
            ".gitignore": "dist/\n",
        }.items():
            path = self.repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content.encode())
        self.git("add", ".")
        self.git("-c", "user.name=Artifact test", "-c", "user.email=artifact-test@example.invalid",
                 "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "fixture")
        self.head = self.git("rev-parse", "HEAD").decode().strip()
        self.directory = self.repo / "dist" / "preview"
        self.directory.mkdir(parents=True)
        self.wheel = self.directory / "memweft-0.2.0a1-cp311-cp311-test.whl"
        self.npm = self.directory / "memweft-0.2.0-alpha.1.tgz"
        self.write_wheel()
        self.write_npm()
        self.record = {"schema_version": 1, "signed_attestation": False,
            "build": {"source": {"commit_sha": self.head, "dirty": False},
                "workflow_sha256": digest(self.repo / ".github/workflows/verify.yml"), "github": {},
                "started_at": "2026-10-01T01:00:00+00:00", "finished_at": "2026-10-01T01:01:00+00:00"},
            "artifacts": {path.name: digest(path) for path in (self.wheel, self.npm)}}
        self.manifest = {"manifest_version": 2, "host": {"os": "Darwin", "machine": "arm64"},
            "artifacts": dict(self.record["artifacts"]), "artifact_metadata": {},
            "verification": {"passed": True, "source_unchanged": True},
            "verification_source": {"role": "verifier_checkout", "commit_sha": self.head, "dirty": False},
            "verification_source_after": {"role": "verifier_checkout", "commit_sha": self.head, "dirty": False}}
        for path, version in ((self.wheel, "0.2.0a1"), (self.npm, "0.2.0-alpha.1")):
            self.manifest["artifact_metadata"][path.name] = {
                "package": {"name": "memweft", "version": version},
                "verified_checks": sorted(REQUIRED_CHECKS | ({"schema_v3", "no_generated_bytecode"} if path == self.wheel else set())),
                "build_provenance": {"status": "matching_build_record", "source": dict(self.record["build"]["source"]),
                    "workflow_sha256": self.record["build"]["workflow_sha256"], "signed_attestation": False,
                    "verified_checks": ["artifact_sha256_matches_build_record"]}}
        self.save_record_and_manifest()

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], stderr=subprocess.PIPE)

    def write_wheel(self, version="0.2.0a1", *, unsafe=False):
        with zipfile.ZipFile(self.wheel, "w") as archive:
            archive.writestr(f"memweft-{version}.dist-info/METADATA", f"Name: memweft\nVersion: {version}\n")
            archive.writestr("memweft/__init__.py", "")
            if unsafe:
                archive.writestr("../outside", "must not be extracted")

    def write_npm(self, version="0.2.0-alpha.1", *, link=False, duplicate=False):
        payload = (f'{{"name":"memweft","version":"{version}"' +
                   (',"version":"different"' if duplicate else '') + '}').encode()
        with tarfile.open(self.npm, "w:gz") as archive:
            entry = tarfile.TarInfo("package/package.json")
            entry.size = len(payload)
            archive.addfile(entry, io.BytesIO(payload))
            if link:
                entry = tarfile.TarInfo("package/symlink")
                entry.type, entry.linkname = tarfile.SYMTYPE, "../outside"
                archive.addfile(entry)

    def save_manifest(self):
        (self.directory / "package-target.json").write_text(json.dumps(self.manifest), encoding="utf-8")

    def save_record_and_manifest(self):
        (self.directory / "build-record.json").write_text(json.dumps(self.record), encoding="utf-8")
        for entry in self.manifest["artifact_metadata"].values():
            entry["build_provenance"]["record_sha256"] = digest(self.directory / "build-record.json")
        self.save_manifest()

    def rebind_artifact_bytes(self, path):
        self.manifest["artifacts"][path.name] = self.record["artifacts"][path.name] = digest(path)
        self.save_record_and_manifest()

    def collect(self, **kwargs):
        return collect_artifact_evidence(self.repo, kwargs.pop("artifact_dir", self.directory), kwargs.pop("head", self.head))

    def rejected(self, reason, **kwargs):
        result = self.collect(**kwargs)
        self.assertFalse(result["local_artifacts_verified_for_source"])
        self.assertEqual(result["artifact_verification"]["status"], "unavailable")
        self.assertEqual(result["artifact_verification"]["reason"], reason)
        return result

    def test_current_packages_and_records_are_bound_to_source_without_install_or_network(self):
        result = self.collect(artifact_dir="dist/preview")
        self.assertTrue(result["local_artifacts_match_manifest"])
        self.assertTrue(result["local_artifacts_verified_for_source"])
        self.assertEqual(result["artifact_verification"]["source_commit"], self.head)
        self.assertFalse(result["artifact_verification"]["signed_attestation"])
        self.assertEqual({item["package"]["version"] for item in result["artifacts"]}, {"0.2.0a1", "0.2.0-alpha.1"})
        self.assertEqual(result["source_sha256"]["dist/preview/build-record.json"], digest(self.directory / "build-record.json"))
        self.directory.rename(self.repo / "packages-temporary")
        (self.repo / "dist").rmdir()
        (self.repo / "packages-temporary").rename(self.repo / "dist")
        self.assertTrue(collect_artifact_evidence(self.repo, head=self.head)["local_artifacts_verified_for_source"])

    def test_old_hash_matching_manifest_cannot_assert_current_source_acceptance(self):
        self.write_wheel("0.1.0")
        self.write_npm("0.1.0")
        self.manifest = {"host": self.manifest["host"], "artifacts": {path.name: digest(path) for path in (self.wheel, self.npm)}}
        self.save_manifest()
        self.assertTrue(self.rejected("current_acceptance_manifest_required")["local_artifacts_match_manifest"])

    def test_each_sdk_is_required_and_content_changes_are_detected(self):
        saved = self.manifest["artifacts"].pop(self.npm.name)
        self.save_manifest()
        self.rejected("both_sdk_packages_required")
        self.manifest["artifacts"][self.npm.name] = saved
        self.save_manifest()
        self.npm.write_bytes(b"changed")
        self.rejected("artifact_hash_mismatch")

    def test_head_and_both_verifier_observations_must_match_clean_source(self):
        self.rejected("current_head_mismatch", head="a" * 40)
        for field in ("verification_source", "verification_source_after"):
            for key, value in (("dirty", True), ("dirty", 0), ("commit_sha", "a" * 40)):
                with self.subTest(field=field, key=key, value=value):
                    original = self.manifest[field][key]
                    self.manifest[field][key] = value
                    self.save_manifest()
                    self.rejected("verification_source_mismatch_or_dirty")
                    self.manifest[field][key] = original
        self.save_manifest()

    def test_failed_or_changed_installation_and_absent_required_api_checks_are_rejected(self):
        for key in ("passed", "source_unchanged"):
            self.manifest["verification"][key] = False
            self.save_manifest()
            self.rejected("installation_not_verified")
            self.manifest["verification"][key] = True
        self.manifest["artifact_metadata"][self.npm.name]["verified_checks"].remove("required_key_outside_candidate_window")
        self.save_manifest()
        self.rejected("required_install_checks_missing")

    def test_build_commit_dirty_state_workflow_and_record_hash_are_checked(self):
        for key, value in (("dirty", True), ("commit_sha", "a" * 40)):
            original = self.record["build"]["source"][key]
            self.record["build"]["source"][key] = value
            self.save_record_and_manifest()
            self.rejected("build_source_mismatch_or_dirty")
            self.record["build"]["source"][key] = original
        original = self.record["build"]["workflow_sha256"]
        self.record["build"]["workflow_sha256"] = "a" * 64
        self.save_record_and_manifest()
        self.rejected("build_workflow_mismatch")
        self.record["build"]["workflow_sha256"] = original
        self.save_record_and_manifest()
        with (self.directory / "build-record.json").open("a") as stream:
            stream.write(" ")
        self.rejected("build_record_hash_mismatch")

    def test_archive_version_cannot_be_faked_by_manifest_package_metadata(self):
        self.write_wheel("0.1.0")
        self.rebind_artifact_bytes(self.wheel)
        self.rejected("package_version_or_name_mismatch")
        self.write_wheel()
        self.rebind_artifact_bytes(self.wheel)
        self.write_npm("0.1.0")
        self.rebind_artifact_bytes(self.npm)
        self.rejected("package_version_or_name_mismatch")

    def test_changed_source_declarations_or_workflow_cannot_reuse_the_record(self):
        for relative in ("python/pyproject.toml", "typescript/package.json", ".github/workflows/verify.yml"):
            path = self.repo / relative
            original = path.read_bytes()
            path.write_bytes(original + b" ")
            self.rejected("current_source_file_differs_from_commit")
            path.write_bytes(original)
        # Normal Windows checkout newline conversion preserves committed identity.
        workflow = self.repo / ".github/workflows/verify.yml"
        workflow.write_bytes(workflow.read_bytes().replace(b"\n", b"\r\n"))
        self.assertTrue(self.collect()["local_artifacts_verified_for_source"])

    def test_duplicate_json_keys_and_wrong_scalar_types_fail_closed(self):
        path = self.directory / "package-target.json"
        path.write_text('{"manifest_version":2,"manifest_version":2}')
        self.rejected("duplicate_json_key")
        self.manifest["manifest_version"] = True
        self.save_manifest()
        self.rejected("current_acceptance_manifest_required")
        self.manifest["manifest_version"] = 2
        self.save_manifest()
        self.write_npm(duplicate=True)
        self.rebind_artifact_bytes(self.npm)
        self.rejected("duplicate_json_key")

    def test_path_traversal_is_rejected_before_reading_other_directories(self):
        self.rejected("unsafe_artifact_path", artifact_dir="dist/../outside")
        self.rejected("artifact_directory_outside_dist", artifact_dir=self.repo.parent)
        self.manifest["artifacts"] = {"../outside.whl": "a" * 64}
        self.save_manifest()
        self.rejected("invalid_artifact_filename")

    def test_directory_manifest_and_artifact_symlinks_are_rejected(self):
        link = self.repo / "dist" / "link"
        try:
            link.symlink_to(self.directory, target_is_directory=True)
        except OSError:
            self.skipTest("platform cannot create symlinks")
        self.rejected("symlink_not_allowed", artifact_dir=link)
        for name in ("package-target.json", "build-record.json", self.npm.name):
            path = self.directory / name
            original = path.read_bytes()
            target = self.repo / ("external-" + name)
            target.write_bytes(original)
            path.unlink()
            path.symlink_to(target)
            self.rejected("symlink_not_allowed")
            path.unlink()
            path.write_bytes(original)

    def test_unsafe_archive_paths_links_and_corrupt_archives_are_rejected(self):
        self.write_wheel(unsafe=True)
        self.rebind_artifact_bytes(self.wheel)
        self.rejected("unsafe_archive_path")
        self.write_wheel()
        self.rebind_artifact_bytes(self.wheel)
        self.write_npm(link=True)
        self.rebind_artifact_bytes(self.npm)
        self.rejected("archive_link_not_allowed")
        self.npm.write_bytes(b"not a tarball")
        self.rebind_artifact_bytes(self.npm)
        self.rejected("artifact_evidence_missing_or_invalid")

    def test_change_during_capture_cannot_receive_success(self):
        import release_artifact_evidence as evidence
        inspect = evidence.archive_package

        def mutate(path):
            package = inspect(path)
            if path == self.npm:
                self.wheel.write_bytes(b"changed after inspection")
            return package

        with patch.object(evidence, "archive_package", side_effect=mutate):
            self.rejected("evidence_changed_during_capture")


if __name__ == "__main__":
    unittest.main()
