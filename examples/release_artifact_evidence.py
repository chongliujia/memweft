"""Read local SDK acceptance records without installing packages or using a network.

These unsigned records associate bytes with a recorded clean build and install
check. They are not signed attestations or proof that build tools were trusted.
Only a directory inside the selected repository's dist tree is accepted.
"""
from __future__ import annotations

from email.parser import BytesParser
import hashlib
import json
from pathlib import Path
import re
import stat
import subprocess
import tarfile
import zipfile
import zlib


WORKFLOW = ".github/workflows/verify.yml"
SOURCE_FILES = (WORKFLOW, "python/pyproject.toml", "typescript/package.json")
MAX_METADATA_BYTES = 1024 * 1024
REQUIRED_CHECKS = frozenset({
    "archive_required_files", "isolated_native_import", "installed_version_matches_archive",
    "lexical_query_and_messages", "missing_query_warning", "required_key_outside_candidate_window",
    "required_report_complete_missing_excluded", "required_keys_respect_count_and_token_limits",
    "same_key_update", "user_tenant_agent_isolation", "updated_and_required_facts_survive_reopen",
    "forget_survives_reopen_and_preserves_other_facts", "artifact_hash_unchanged_during_verification",
})


class ArtifactEvidenceError(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise ArtifactEvidenceError(reason)


def strict_json(payload):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate_json_key")
            result[key] = value
        return result

    def constant(_value):
        raise ArtifactEvidenceError("invalid_json_constant")

    value = json.loads(payload, object_pairs_hook=pairs, parse_constant=constant)
    require(type(value) is dict, "metadata_must_be_object")
    return value


def safe_path(root, path, *, directory=False):
    """Validate components before following any link inside the repository."""
    require(".." not in path.parts, "unsafe_artifact_path")
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise ArtifactEvidenceError("artifact_directory_outside_dist") from None
    current = root
    for part in relative.parts:
        current = current / part
        require(not current.is_symlink(), "symlink_not_allowed")
        attributes = getattr(current.lstat(), "st_file_attributes", 0)
        require(not attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0), "symlink_not_allowed")
    require(path.is_dir() if directory else path.is_file(), "required_file_or_directory_missing")
    return path


def read_metadata(root, path):
    safe_path(root, path)
    require(path.stat().st_size <= MAX_METADATA_BYTES, "metadata_too_large")
    with path.open("rb") as stream:
        payload = stream.read(MAX_METADATA_BYTES + 1)
    require(len(payload) <= MAX_METADATA_BYTES, "metadata_too_large")
    return payload


def file_hash(root, path):
    safe_path(root, path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def valid_digest(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def artifact_name(name):
    return type(name) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,180}", name) is not None


def archive_path(name):
    require(type(name) is str and bool(name) and "\\" not in name and ":" not in name
            and not name.startswith("/")
            and all(part not in ("", ".", "..") for part in name.rstrip("/").split("/")),
            "unsafe_archive_path")


def archive_package(path):
    """Inspect package metadata in place; never extract or execute archive contents."""
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            require(len(entries) <= 10000, "archive_too_many_entries")
            names = [entry.filename for entry in entries]
            require(len(set(names)) == len(names), "duplicate_archive_entry")
            for entry in entries:
                archive_path(entry.filename)
                require(not stat.S_ISLNK(entry.external_attr >> 16), "archive_link_not_allowed")
            metadata = [entry for entry in entries if entry.filename.endswith(".dist-info/METADATA")]
            require(len(metadata) == 1 and metadata[0].file_size <= MAX_METADATA_BYTES, "invalid_wheel_metadata")
            parsed = BytesParser().parsebytes(archive.read(metadata[0]))
            require(len(parsed.get_all("Name", [])) == 1 and len(parsed.get_all("Version", [])) == 1,
                    "invalid_wheel_metadata")
            return {"name": parsed["Name"], "version": parsed["Version"]}
    with tarfile.open(path, "r:gz") as archive:
        package = None
        names = set()
        for index, entry in enumerate(archive):
            require(index < 10000, "archive_too_many_entries")
            archive_path(entry.name)
            require(entry.name not in names, "duplicate_archive_entry")
            names.add(entry.name)
            require(entry.isfile() or entry.isdir(), "archive_link_not_allowed")
            if entry.name == "package/package.json":
                require(entry.isfile() and entry.size <= MAX_METADATA_BYTES, "invalid_npm_metadata")
                with archive.extractfile(entry) as stream:
                    package = strict_json(stream.read(MAX_METADATA_BYTES + 1))
        require(type(package) is dict, "invalid_npm_metadata")
        return {"name": package.get("name"), "version": package.get("version")}


def python_package(payload):
    """Read this project's literal [project] name/version on Python 3.10 too.

    This deliberately supports only single-line unescaped string declarations;
    dynamic versions and multiline TOML strings fail closed instead of guessing.
    """
    text = payload.decode("utf-8")
    require('"""' not in text and "'''" not in text, "unsupported_python_metadata")
    active, seen, values = False, False, {}
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            active = re.fullmatch(r"\[project\]\s*(?:#.*)?", stripped) is not None
            if active:
                require(not seen, "duplicate_python_project")
                seen = True
            continue
        if active and re.match(r"(?:name|version)\s*=", stripped):
            match = re.fullmatch(r'''(name|version)\s*=\s*(["'])([A-Za-z0-9.+-]+)\2\s*(?:#.*)?''', stripped)
            require(match is not None, "unsupported_python_metadata")
            require(match[1] not in values, "duplicate_python_metadata")
            values[match[1]] = match[3]
    require(set(values) == {"name", "version"}, "unsupported_python_metadata")
    return values


def git_bytes(repo, *arguments):
    return subprocess.run(["git", "-C", str(repo), *arguments], check=True, capture_output=True,
                          timeout=10).stdout


def clean_source(value, head, reason):
    require(type(value) is dict and value.get("commit_sha") == head and value.get("dirty") is False, reason)


def check_list(value, required):
    require(type(value) is list and all(type(item) is str for item in value)
            and len(set(value)) == len(value) and required <= set(value), "required_install_checks_missing")


def collect_artifact_evidence(repo, artifact_dir=None, head=None):
    """Return fail-closed evidence for both SDKs in dist or a selected subdirectory."""
    result = {"artifacts": [], "recorded_build_host": None, "manifest_error": None,
              "local_artifacts_match_manifest": False, "local_artifacts_verified_for_source": False,
              "artifact_verification": {"status": "unavailable", "reason": None, "source_commit": None,
                  "signed_attestation": False, "scope": "local_python_and_node_sdk_records",
                  "limitation": "Unsigned local build and installation records, not a signed attestation or independent proof of the build."},
              "source_sha256": {}}
    try:
        repo = Path(repo).resolve()
        directory = Path(artifact_dir) if artifact_dir is not None else repo / "dist"
        require(".." not in directory.parts, "unsafe_artifact_path")
        if not directory.is_absolute():
            directory = repo / directory
        require(directory.is_relative_to(repo / "dist"), "artifact_directory_outside_dist")
        safe_path(repo, directory, directory=True)
        result["artifact_verification"]["directory"] = directory.relative_to(repo).as_posix()
        observed = {}

        def metadata(name):
            path = directory / name
            payload = read_metadata(repo, path)
            digest = hashlib.sha256(payload).hexdigest()
            observed[path] = digest
            result["source_sha256"][path.relative_to(repo).as_posix()] = digest
            return strict_json(payload)

        manifest = metadata("package-target.json")
        host = manifest.get("host")
        require(type(host) is dict and all(type(host.get(key)) is str and
                re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", host[key]) for key in ("os", "machine")), "invalid_build_host")
        result["recorded_build_host"] = {key: host[key] for key in ("os", "machine")}
        artifacts = manifest.get("artifacts")
        require(type(artifacts) is dict and bool(artifacts), "invalid_artifact_manifest")
        for name, expected in artifacts.items():
            require(artifact_name(name) and name.endswith((".whl", ".tgz")), "invalid_artifact_filename")
            require(valid_digest(expected), "invalid_artifact_digest")
            actual = file_hash(repo, directory / name)
            observed[directory / name] = actual
            result["artifacts"].append({"name": name, "sha256": actual, "matches_manifest": actual == expected})
        result["local_artifacts_match_manifest"] = all(item["matches_manifest"] for item in result["artifacts"])
        require(result["local_artifacts_match_manifest"], "artifact_hash_mismatch")
        require(len(artifacts) == 2 and sum(name.endswith(".whl") for name in artifacts) == 1
                and sum(name.endswith(".tgz") for name in artifacts) == 1, "both_sdk_packages_required")
        require(type(manifest.get("manifest_version")) is int and manifest["manifest_version"] == 2,
                "current_acceptance_manifest_required")
        require(type(head) is str and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head), "full_commit_sha_required")
        require(git_bytes(repo, "rev-parse", "HEAD").decode("ascii").strip() == head, "current_head_mismatch")
        verification = manifest.get("verification")
        require(type(verification) is dict and verification.get("passed") is True
                and verification.get("source_unchanged") is True, "installation_not_verified")
        for key in ("verification_source", "verification_source_after"):
            clean_source(manifest.get(key), head, "verification_source_mismatch_or_dirty")
            require(manifest[key].get("role") == "verifier_checkout", "invalid_verification_source_role")
        record = metadata("build-record.json")
        require(type(record.get("schema_version")) is int and record["schema_version"] == 1
                and record.get("signed_attestation") is False, "invalid_build_record")
        build = record.get("build")
        require(type(build) is dict, "invalid_build_record")
        clean_source(build.get("source"), head, "build_source_mismatch_or_dirty")
        record_artifacts = record.get("artifacts")
        require(type(record_artifacts) is dict and all(artifact_name(name) and valid_digest(value)
                for name, value in record_artifacts.items()), "invalid_build_record_artifacts")
        require(all(record_artifacts.get(name) == value for name, value in artifacts.items()), "build_record_artifact_mismatch")
        committed, current = {}, {}
        for relative in SOURCE_FILES:
            payload = read_metadata(repo, repo / relative)
            digest = hashlib.sha256(payload).hexdigest()
            observed[repo / relative] = digest
            result["source_sha256"][relative] = digest
            committed[relative] = git_bytes(repo, "show", head + ":" + relative)
            require(payload.replace(b"\r\n", b"\n") == committed[relative].replace(b"\r\n", b"\n"),
                    "current_source_file_differs_from_commit")
            current[relative] = payload
        workflow_hash = hashlib.sha256(committed[WORKFLOW]).hexdigest()
        require(build.get("workflow_sha256") == workflow_hash, "build_workflow_mismatch")
        packages = {".whl": python_package(current["python/pyproject.toml"]),
                    ".tgz": strict_json(current["typescript/package.json"])}
        require(all(package.get("name") == "memweft" and type(package.get("version")) is str
                    and re.fullmatch(r"[0-9][A-Za-z0-9.+-]{0,63}", package["version"])
                    for package in packages.values()), "invalid_declared_package_version")
        entries = manifest.get("artifact_metadata")
        require(type(entries) is dict and set(entries) == set(artifacts), "artifact_metadata_incomplete")
        record_hash = observed[directory / "build-record.json"]
        for item in result["artifacts"]:
            name = item["name"]
            actual_package = archive_package(directory / name)
            expected_package = {key: packages[Path(name).suffix][key] for key in ("name", "version")}
            item["package"] = actual_package
            entry = entries[name]
            require(type(entry) is dict and type(entry.get("package")) is dict
                    and actual_package == entry["package"] == expected_package, "package_version_or_name_mismatch")
            required = REQUIRED_CHECKS | ({"schema_v3", "no_generated_bytecode"} if name.endswith(".whl") else set())
            check_list(entry.get("verified_checks"), required)
            provenance = entry.get("build_provenance")
            require(type(provenance) is dict and provenance.get("status") == "matching_build_record"
                    and provenance.get("signed_attestation") is False, "build_provenance_missing")
            clean_source(provenance.get("source"), head, "artifact_build_source_mismatch_or_dirty")
            require(provenance.get("record_sha256") == record_hash, "build_record_hash_mismatch")
            require(provenance.get("workflow_sha256") == workflow_hash, "artifact_workflow_mismatch")
            check_list(provenance.get("verified_checks"), {"artifact_sha256_matches_build_record"})
        require(all(file_hash(repo, path) == digest for path, digest in observed.items()), "evidence_changed_during_capture")
        require(git_bytes(repo, "rev-parse", "HEAD").decode("ascii").strip() == head, "current_head_changed_during_capture")
        result["local_artifacts_verified_for_source"] = True
        result["artifact_verification"].update(status="passed", reason="local_sdk_records_match_current_source",
            source_commit=head, workflow_sha256=workflow_hash, build_record_sha256=record_hash)
    except ArtifactEvidenceError as error:
        result["manifest_error"] = str(error)
        result["artifact_verification"]["reason"] = str(error)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, EOFError, RuntimeError,
            subprocess.SubprocessError, zipfile.BadZipFile, zipfile.LargeZipFile, tarfile.TarError, zlib.error):
        result["manifest_error"] = "artifact_evidence_missing_or_invalid"
        result["artifact_verification"]["reason"] = "artifact_evidence_missing_or_invalid"
    return result
