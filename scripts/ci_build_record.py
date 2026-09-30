#!/usr/bin/env python3
"""Bind freshly built preview artifacts to a clean checkout and CI run.

Run begin before building, finish afterwards, then pass the resulting JSON to
verify_packages.py --build-record. This is an unsigned build record, not a
cryptographic attestation or proof that external build tools were trustworthy.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def capture(repo):
    def git(*arguments):
        return subprocess.check_output(["git", "-C", str(repo), *arguments], stderr=subprocess.PIPE)
    head = git("rev-parse", "--verify", "HEAD").decode("ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise ValueError("A full Git commit SHA is required")
    if git("status", "--porcelain", "--untracked-files=normal").strip():
        raise ValueError("Build records require a clean checkout, including untracked source files")
    expected = os.environ.get("GITHUB_SHA")
    if expected is not None and expected != head:
        raise ValueError("Checkout HEAD does not match GITHUB_SHA")
    # Hash the committed blob, so Windows checkout newline conversion cannot
    # change the workflow identity recorded for the same commit.
    workflow = git("show", head + ":.github/workflows/verify.yml")
    github = {name: os.environ[variable] for name, variable in {
        "repository": "GITHUB_REPOSITORY", "sha": "GITHUB_SHA", "run_id": "GITHUB_RUN_ID",
        "run_attempt": "GITHUB_RUN_ATTEMPT", "job": "GITHUB_JOB",
        "workflow_ref": "GITHUB_WORKFLOW_REF", "workflow_sha": "GITHUB_WORKFLOW_SHA",
        "event_name": "GITHUB_EVENT_NAME", "runner_os": "RUNNER_OS", "runner_arch": "RUNNER_ARCH",
    }.items() if variable in os.environ}
    return {"source": {"commit_sha": head, "dirty": False},
            "workflow_sha256": hashlib.sha256(workflow).hexdigest(), "github": github}


def write_new(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def begin(repo, output, expected_arch=None):
    snapshot = capture(repo)
    actual_arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower())
    if expected_arch is not None and actual_arch != expected_arch:
        raise ValueError("Build interpreter architecture does not match the CI matrix")
    if list(output.parent.glob("*.whl")) or list(output.parent.glob("*.tgz")):
        raise ValueError("Start with an artifact directory containing no existing wheels or npm tarballs")
    snapshot["started_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    write_new(output, {"schema_version": 1, "build": snapshot, "signed_attestation": False})


def finish(repo, source_path, output, *, wheel_dir=None, npm_dir=None, cli_dir=None):
    started = json.loads(source_path.read_text(encoding="utf-8"))
    if type(started) is not dict or started.get("schema_version") != 1 or type(started.get("build")) is not dict:
        raise ValueError("Invalid build-start record")
    build = started["build"]
    if (type(build.get("source")) is not dict or build["source"].get("dirty") is not False
            or type(build.get("started_at")) is not str):
        raise ValueError("Build-start record must describe a clean source snapshot")
    current = capture(repo)
    if any(build.get(key) != value for key, value in current.items()):
        raise ValueError("Source commit, workflow or CI run changed during the build")
    artifacts = []
    for directory, pattern in ((wheel_dir, "*.whl"), (npm_dir, "*.tgz")):
        if directory is not None:
            matches = list(directory.glob(pattern))
            if len(matches) != 1:
                raise ValueError(f"Expected exactly one {pattern} in the artifact directory")
            artifacts.extend(matches)
    if cli_dir is not None:
        artifacts.append(cli_dir / ("memweft.exe" if os.name == "nt" else "memweft"))
    if not artifacts:
        raise ValueError("At least one artifact directory is required")
    if any(path.is_symlink() or not path.is_file() for path in artifacts):
        raise ValueError("Build artifacts must be existing regular files")
    if len({path.name for path in artifacts}) != len(artifacts):
        raise ValueError("Build artifact filenames must be distinct")
    write_new(output, {"schema_version": 1, "build": {**build,
        "finished_at": dt.datetime.now(dt.timezone.utc).isoformat()},
        "artifacts": {path.name: sha256(path) for path in artifacts}, "signed_attestation": False})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("begin")
    start.add_argument("--output", type=Path, required=True)
    start.add_argument("--expected-arch", choices=("x64", "arm64"))
    end = commands.add_parser("finish")
    end.add_argument("--source", type=Path, required=True)
    end.add_argument("--output", type=Path, required=True)
    end.add_argument("--wheel-dir", type=Path)
    end.add_argument("--npm-dir", type=Path)
    end.add_argument("--cli-dir", type=Path)
    args = parser.parse_args()
    if args.command == "begin":
        begin(args.repo, args.output, args.expected_arch)
    else:
        finish(args.repo, args.source, args.output, wheel_dir=args.wheel_dir, npm_dir=args.npm_dir, cli_dir=args.cli_dir)
    print(json.dumps({"recorded": args.command, "output": str(args.output)}))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error))
