#!/usr/bin/env python3
"""Install built SDK artifacts outside the checkout and exercise their native APIs.

Run after building a wheel and/or npm tarball. No package registry is contacted.
"""

import argparse
from datetime import datetime, timezone
from email.parser import Parser
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import venv
import zipfile


PYTHON_SMOKE = r'''
from importlib.metadata import version
from pathlib import Path
import sqlite3
import sys

import memweft
from memweft import Memory
from memweft import _core

assert Path(memweft.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
assert Path(_core.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
assert version("memweft") == sys.argv[1]
database = Path("python-memory.db").resolve()
with Memory(str(database)) as memory:
    user = memory.user("package-smoke")
    user.remember("部署端口 17443", key="port")
    for index in range(80):
        user.remember("archived", key=f"a_archive_{index:03}")
    user.remember("approval needed", key="z_policy")
    user.session("chat").add_message("user", "哪个端口？", event_id="question")
    context = user.session("chat").context(query="部署端口", max_facts=1)
    assert context.memories[0]["fact_key"] == "port"
    assert "哪个端口？" in context.text
    assert user.session("chat").context(max_facts=1).explain()["warnings"][0]["code"] == "unranked_fact_limit"
    required = user.session("chat").context(query="部署端口", required_fact_keys=["z_policy"], max_facts=1)
    assert required.memories[0]["fact_key"] == "z_policy"
    assert required.explain()["requirements"] == {
        "requested": ["z_policy"], "included": ["z_policy"], "missing": [], "excluded": [], "complete": True,
    }
    assert user.session("chat").context(required_fact_keys=["z_policy"], max_tokens=0).explain()["requirements"] == {
        "requested": ["z_policy"], "included": [], "missing": [], "excluded": ["z_policy"], "complete": False,
    }
    assert user.session("chat").context(required_fact_keys=["z_policy"], max_facts=0).explain()["requirements"]["excluded"] == ["z_policy"]
    for other in (memory.user("other-user"), memory.user("package-smoke", tenant_id="other-tenant"),
                  memory.user("package-smoke", agent_id="other-agent")):
        isolated = other.session("chat").context(required_fact_keys=["z_policy"])
        assert isolated.memories == [] and isolated.messages == []
        assert isolated.explain()["requirements"]["missing"] == ["z_policy"]
    user.remember("部署端口 18443", key="port")
    ports = [fact for fact in user.memories() if fact["fact_key"] == "port"]
    assert len(ports) == 1 and ports[0]["value"] == "部署端口 18443"
with sqlite3.connect(database) as connection:
    assert connection.execute("SELECT version FROM memweft_recall_version").fetchone() == (3,)
with Memory(str(database)) as memory:
    user = memory.user("package-smoke")
    assert user.session("chat").context(query="部署端口", max_facts=1).memories[0]["value"] == "部署端口 18443"
    assert user.session("chat").context(required_fact_keys=["z_policy"], max_facts=1).explain()["requirements"]["complete"]
    assert user.forget("z_policy")
    assert user.session("chat").context(required_fact_keys=["z_policy"]).explain()["requirements"]["missing"] == ["z_policy"]
with Memory(str(database)) as memory:
    user = memory.user("package-smoke")
    assert not any(fact["fact_key"] == "z_policy" for fact in user.memories())
    assert user.session("chat").context(required_fact_keys=["z_policy"]).explain()["requirements"] == {
        "requested": ["z_policy"], "included": [], "missing": ["z_policy"], "excluded": [], "complete": False,
    }
    assert user.session("chat").context(query="部署端口", max_facts=1).memories[0]["value"] == "部署端口 18443"
print("installed Python wheel: required facts, limits, scopes, update, durable forget and reopen passed")
'''


NODE_SMOKE = r'''
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { Memory } from 'memweft';

assert.ok(fileURLToPath(import.meta.resolve('memweft'))
  .startsWith(resolve('node_modules', 'memweft') + sep));
assert.equal(JSON.parse(await readFile(resolve('node_modules','memweft','package.json'),'utf8')).version, process.argv[2]);
const path = resolve('node-memory.db');
const memory = await Memory.open({path});
try {
  const user = memory.user('package-smoke');
  await user.remember('部署端口 17443', {key: 'port'});
  for (let index=0; index<80; index++) await user.remember('archived',{key:`a_archive_${String(index).padStart(3,'0')}`});
  await user.remember('approval needed', {key:'z_policy'});
  await user.session('chat').addMessage('user', '哪个端口？', {eventId: 'question'});
  const context = await user.session('chat').context({query: '部署端口', maxFacts: 1});
  assert.equal(context.memories[0].fact_key, 'port');
  assert.match(context.text, /哪个端口/);
  assert.equal((await user.session('chat').context({maxFacts:1})).explain().warnings[0].code,'unranked_fact_limit');
  const required = await user.session('chat').context({query:'部署端口',requiredFactKeys:['z_policy'],maxFacts:1});
  assert.equal(required.memories[0].fact_key,'z_policy');
  assert.deepEqual(required.explain().requirements, {
    requested:['z_policy'],included:['z_policy'],missing:[],excluded:[],complete:true,
  });
  assert.deepEqual((await user.session('chat').context({requiredFactKeys:['z_policy'],maxTokens:0})).explain().requirements, {
    requested:['z_policy'],included:[],missing:[],excluded:['z_policy'],complete:false,
  });
  assert.deepEqual((await user.session('chat').context({requiredFactKeys:['z_policy'],maxFacts:0})).explain().requirements.excluded,['z_policy']);
  for (const other of [memory.user('other-user'), memory.user('package-smoke',{tenantId:'other-tenant'}),
                       memory.user('package-smoke',{agentId:'other-agent'})]) {
    const isolated = await other.session('chat').context({requiredFactKeys:['z_policy']});
    assert.deepEqual(isolated.memories,[]);
    assert.deepEqual(isolated.messages,[]);
    assert.deepEqual(isolated.explain().requirements.missing,['z_policy']);
  }
  await user.remember('部署端口 18443',{key:'port'});
  const ports = (await user.memories()).filter(fact=>fact.fact_key==='port');
  assert.equal(ports.length,1);
  assert.equal(ports[0].value,'部署端口 18443');
} finally { memory.close(); }
const reopened = await Memory.open({path});
try {
  const user = reopened.user('package-smoke');
  assert.equal((await user.session('chat').context({query:'部署端口',maxFacts:1})).memories[0].value,'部署端口 18443');
  assert.equal((await user.session('chat').context({requiredFactKeys:['z_policy'],maxFacts:1})).explain().requirements.complete,true);
  assert.equal(await user.forget('z_policy'),true);
  assert.deepEqual((await user.session('chat').context({requiredFactKeys:['z_policy']})).explain().requirements.missing,['z_policy']);
} finally { reopened.close(); }
const final = await Memory.open({path});
try {
  const user = final.user('package-smoke');
  assert.equal((await user.memories()).some(fact=>fact.fact_key==='z_policy'),false);
  assert.deepEqual((await user.session('chat').context({requiredFactKeys:['z_policy']})).explain().requirements, {
    requested:['z_policy'],included:[],missing:['z_policy'],excluded:[],complete:false,
  });
  assert.equal((await user.session('chat').context({query:'部署端口',maxFacts:1})).memories[0].value,'部署端口 18443');
} finally { final.close(); }
console.log('installed npm tarball: required facts, limits, scopes, update, durable forget and reopen passed');
'''


COMMON_CHECKS = [
    "archive_required_files", "isolated_native_import", "installed_version_matches_archive",
    "lexical_query_and_messages", "missing_query_warning", "required_key_outside_candidate_window",
    "required_report_complete_missing_excluded", "required_keys_respect_count_and_token_limits",
    "same_key_update", "user_tenant_agent_isolation", "updated_and_required_facts_survive_reopen",
    "forget_survives_reopen_and_preserves_other_facts",
]


def run(command: list[str], cwd: Path, environment: dict[str, str]) -> None:
    subprocess.run(command, cwd=cwd, env=environment, check=True)


def sole_artifact(directory: Path, pattern: str) -> Path:
    matches = sorted(directory.glob(pattern))
    if len(matches) != 1:
        raise ValueError(f"Expected one {pattern} in {directory}, found {len(matches)}")
    return matches[0].resolve(strict=True)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def wheel_metadata(wheel: Path) -> dict:
    with zipfile.ZipFile(wheel) as archive:
        files = archive.namelist()
        metadata_files = [name for name in files if name.endswith(".dist-info/METADATA")]
        require(len(metadata_files) == 1, "wheel must contain exactly one package metadata file")
        metadata = Parser().parsestr(archive.read(metadata_files[0]).decode("utf-8"))
    require(len([name for name in files if name.startswith("memweft/_core") and name.endswith((".so", ".pyd"))]) == 1,
            "wheel must contain exactly one native extension")
    require("memweft/__init__.py" in files, "wheel has no Python package")
    require(len([name for name in files if name.endswith(".dist-info/licenses/LICENSE")]) == 1,
            "wheel has no license text")
    require(not any("__pycache__" in name or name.endswith(".pyc") for name in files),
            "wheel includes generated bytecode")
    require(metadata.get("Name") == "memweft", "unexpected wheel package name")
    require(bool(metadata.get("Version", "").strip()), "wheel has no package version")
    return {"name": metadata["Name"], "version": metadata["Version"]}


def verify_wheel(wheel: Path, temporary: Path, environment: dict[str, str]) -> dict:
    metadata = wheel_metadata(wheel)
    location = temporary / "python"
    location.mkdir()
    venv.create(location / "venv", with_pip=True)
    executable = location / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    run([str(executable), "-m", "pip", "install", "--no-index", "--no-deps", str(wheel)], location, environment)
    run([str(executable), "-c", PYTHON_SMOKE, metadata["version"]], location, environment)
    return {"package": metadata, "verified_checks": COMMON_CHECKS + ["schema_v3", "no_generated_bytecode"]}


def npm_metadata(tarball: Path) -> dict:
    with tarfile.open(tarball, "r:gz") as archive:
        files = {member.name for member in archive.getmembers() if member.isfile()}
        package_file = archive.extractfile("package/package.json")
        require(package_file is not None, "npm package has no package metadata")
        metadata = json.load(package_file)
    for required in ("package/package.json", "package/LICENSE", "package/dist/index.js",
                     "package/dist/index.d.ts", "package/dist/langgraph.js",
                     "package/dist/langgraph.d.ts", "package/native/memweft.node"):
        require(required in files, f"npm package missing {required}")
    require(metadata.get("name") == "memweft", "unexpected npm package name")
    require(isinstance(metadata.get("version"), str) and bool(metadata["version"].strip()), "npm package has no version")
    return {"name": metadata["name"], "version": metadata["version"]}


def verify_npm(tarball: Path, temporary: Path, environment: dict[str, str]) -> dict:
    metadata = npm_metadata(tarball)
    location = temporary / "node"
    location.mkdir()
    (location / "package.json").write_text('{"private":true,"type":"module"}\n', encoding="utf-8")
    (location / "smoke.mjs").write_text(NODE_SMOKE, encoding="utf-8")
    npm = shutil.which("npm")
    node = shutil.which("node")
    if not npm or not node:
        raise RuntimeError("Node.js and npm are required for npm package verification")
    # The addon is already inside the tarball; ignore hooks and optional peers.
    run([npm, "install", "--offline", "--ignore-scripts", "--no-audit", "--no-fund",
         "--omit=optional", "--omit=peer", str(tarball)], location, environment)
    run([node, "smoke.mjs", metadata["version"]], location, environment)
    return {"package": metadata, "verified_checks": list(COMMON_CHECKS)}


def verification_source(root: Path) -> dict:
    """Observed verifier checkout, deliberately not inferred artifact provenance."""
    result = {"role": "verifier_checkout", "commit_sha": None, "dirty": None}
    try:
        result["commit_sha"] = subprocess.check_output(
            ["git", "rev-parse", "--verify", "HEAD"], cwd=root, text=True, stderr=subprocess.DEVNULL).strip()
        result["dirty"] = bool(subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=normal"], cwd=root, text=True, stderr=subprocess.DEVNULL).strip())
    except (OSError, subprocess.CalledProcessError):
        result.update(commit_sha=None, dirty=None, unavailable_reason="git checkout unavailable")
    return result


def github_context(environment: dict[str, str]) -> dict:
    return {key.lower().removeprefix("github_"): environment[key] for key in (
        "GITHUB_REPOSITORY", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_JOB", "GITHUB_SHA",
    ) if environment.get(key)}


def source_unchanged(before: dict, after: dict) -> bool | None:
    if before["commit_sha"] is None or after["commit_sha"] is None:
        return None
    if before["commit_sha"] != after["commit_sha"] or before["dirty"] != after["dirty"]:
        return False
    # A boolean dirty flag cannot prove that two modified trees have equal bytes.
    return True if before["dirty"] is False and after["dirty"] is False else None


def build_provenance(record_path: Path | None, artifact: Path, digest: str) -> dict:
    if record_path is None:
        return {"status": "not_recorded", "source": {"commit_sha": None, "dirty": None},
                "signed_attestation": False}
    raw = record_path.read_bytes()
    record = json.loads(raw)
    require(isinstance(record, dict), "build record must be an object")
    artifacts = record.get("artifacts")
    require(isinstance(artifacts, dict) and artifacts.get(artifact.name) == digest,
            f"artifact hash does not match build record: {artifact.name}")
    build = record.get("build")
    require(isinstance(build, dict), "build record has no build metadata")
    source = build.get("source")
    require(isinstance(source, dict), "build record has no source metadata")
    require(isinstance(source.get("commit_sha"), str) and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", source["commit_sha"]) is not None,
            "build record has an invalid source commit SHA")
    require(isinstance(source.get("dirty"), bool), "build record source dirty must be a boolean")
    require(isinstance(build.get("workflow_sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", build["workflow_sha256"]) is not None,
            "build record has no valid workflow hash")
    require(isinstance(build.get("github", {}), dict), "build record github context must be an object")
    return {"status": "matching_build_record", "source": source,
            "workflow_sha256": build["workflow_sha256"], "github": build.get("github", {}),
            "record_sha256": hashlib.sha256(raw).hexdigest(), "signed_attestation": False,
            "verified_checks": ["artifact_sha256_matches_build_record"],
            "limitation": "An auditable build record, not a cryptographic attestation of how the artifact was built."}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel-dir", type=Path, help="directory containing exactly one built wheel")
    parser.add_argument("--npm-dir", type=Path, help="directory containing exactly one built npm tarball")
    parser.add_argument("--manifest", type=Path, help="write target and artifact hashes for distribution")
    parser.add_argument("--build-record", type=Path, help="optional build-source record whose artifact hashes must match")
    args = parser.parse_args()
    if not args.wheel_dir and not args.npm_dir:
        parser.error("pass --wheel-dir and/or --npm-dir")
    wheel = sole_artifact(args.wheel_dir, "*.whl") if args.wheel_dir else None
    tarball = sole_artifact(args.npm_dir, "*.tgz") if args.npm_dir else None
    artifacts = [artifact for artifact in (wheel, tarball) if artifact is not None]
    hashes = {artifact.name: sha256(artifact) for artifact in artifacts}
    provenance = {artifact.name: build_provenance(args.build_record, artifact, hashes[artifact.name]) for artifact in artifacts}
    source_root = Path(__file__).resolve().parents[1]
    source = verification_source(source_root)
    started_at = datetime.now(timezone.utc).isoformat()
    results = {}
    environment = {key: value for key, value in os.environ.items()
                   if key not in ("PYTHONPATH", "PYTHONHOME", "PYTHONOPTIMIZE", "VIRTUAL_ENV", "NODE_PATH", "PIP_PREFIX")}
    environment["PIP_NO_INDEX"] = "1"
    environment["PIP_NO_CACHE_DIR"] = "1"
    environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    with tempfile.TemporaryDirectory(prefix="memweft-package-check-") as directory:
        temporary = Path(directory)
        if wheel:
            results[wheel.name] = verify_wheel(wheel, temporary, environment)
        if tarball:
            # Keep npm's cache within the disposable test directory too.
            environment["npm_config_cache"] = str(temporary / "npm-cache")
            results[tarball.name] = verify_npm(tarball, temporary, environment)
    for artifact in artifacts:
        require(sha256(artifact) == hashes[artifact.name], f"artifact changed during verification: {artifact.name}")
        results[artifact.name]["build_provenance"] = provenance[artifact.name]
        results[artifact.name]["verified_checks"].append("artifact_hash_unchanged_during_verification")
    source_after = verification_source(source_root)
    if args.manifest:
        host = {"os": platform.system(), "machine": platform.machine()}
        if platform.system() == "Linux":
            host["libc"] = platform.libc_ver()
        manifest = {"manifest_version": 2, "host": host, "artifacts": hashes,
                    "artifact_metadata": results, "verification_source": source,
                    "verification_source_after": source_after,
                    "verification": {"passed": True, "started_at": started_at,
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                        "source_unchanged": source_unchanged(source, source_after),
                        "github": github_context(os.environ),
                        "source_semantics": "verification_source identifies the verifier checkout only; see each artifact's build_provenance for recorded build association."}}
        if wheel:
            manifest["python"] = f"{sys.version_info.major}.{sys.version_info.minor}"
        if tarball:
            node = shutil.which("node")
            manifest["node"] = json.loads(subprocess.check_output(
                [node, "-p", "JSON.stringify({platform:process.platform,arch:process.arch,napi:process.versions.napi})"],
                cwd=tarball.parent, env=environment, text=True))
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"verified": True, "wheel": wheel is not None, "npm": tarball is not None}))


if __name__ == "__main__":
    main()
