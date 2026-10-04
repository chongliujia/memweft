"""Validate paired Rust lifecycle benchmarks and measure a copied-fixture upgrade.

The input directory contains baseline/indexed binaries, their build manifests,
runner.rs, and BUILD-COUNT-FANOUT/{memory.db,result.json}. Never opens baseline
fixtures with the new SDK. The upgrade probe runs only against a new SQLite backup.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import platform
import shutil
import sqlite3
import statistics
import subprocess

CASES = [(1000, 8), (10000, 8), (100000, 8), (10000, 1000)]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fingerprint(path, *, exact=False):
    digest = hashlib.sha256()
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        for table, order in [
            ("facts", "tenant_id,user_id,agent_id,fact_id"),
            ("memweft_pool_facts", "tenant_id,user_id,pool_id,fact_key"),
            ("memweft_documents", "tenant_id,user_id,agent_id,namespace,key"),
        ]:
            digest.update(table.encode())
            cur = conn.execute(f"SELECT * FROM {table} ORDER BY {order}")
            names = [col[0] for col in cur.description]
            for row in cur:
                value = dict(zip(names, row))
                if not exact and table == "memweft_documents":
                    doc = json.loads(value["document"])
                    doc.pop("created_at")
                    doc.pop("updated_at")
                    value["document"] = doc
                digest.update(json.dumps(value, sort_keys=True, ensure_ascii=False).encode())
                digest.update(b"\n")
    return digest.hexdigest()


def summarize(root):
    builds = {}
    runner = sha(root / "runner.rs")
    results = {}
    for build in ("baseline", "indexed"):
        manifest = json.loads((root / f"{build}-build.json").read_text())
        assert manifest["binary_sha256"] == sha(root / build)
        assert manifest["runner_sha256"] == runner
        builds[build] = manifest
        for count, fanout in CASES:
            directory = root / f"{build}-{count}-{fanout}"
            result = json.loads((directory / "result.json").read_text())
            assert result["facts_per_pool"] == result["unrelated_learning_documents"] == count
            assert result["affected_documents"] == fanout
            assert result["repeats"] == 20
            assert result["verification"] == {
                "background_preserved": count, "dependent_values_null": fanout,
                "pending_jobs_removed": True,
            }
            for metric in result["timings"].values():
                samples = metric["samples_ms"]
                assert len(samples) == 20 and all(math.isfinite(x) and x >= 0 for x in samples)
                assert math.isclose(metric["p50_ms"], statistics.median(samples))
                assert math.isclose(metric["p95_ms"], sorted(samples)[18])
            result["database_bytes"] = (directory / "memory.db").stat().st_size
            result["semantic_sha256"] = fingerprint(directory / "memory.db")
            result["result_sha256"] = sha(directory / "result.json")
            results[f"{build}-{count}-{fanout}"] = result
    for count, fanout in CASES:
        assert results[f"baseline-{count}-{fanout}"]["semantic_sha256"] == results[f"indexed-{count}-{fanout}"]["semantic_sha256"]
    return {"platform": platform.platform(), "runner_sha256": runner,
            "builds": builds, "results": results,
            "scope": "Warm single-client Rust Learning/Store calls; one sequential run per build; no model calls. Timestamp-normalized authoritative rows match across all four fixture pairs."}


def migration(root, probe):
    directory = root / "migration"
    directory.mkdir()  # Preserve a completed observation; never silently rerun it.
    original = root / "baseline-100000-8" / "memory.db"
    target = directory / "memory.db"
    with sqlite3.connect(original.resolve().as_uri() + "?mode=ro", uri=True) as source:
        with sqlite3.connect(target) as dest:
            source.backup(dest)
    before = fingerprint(target, exact=True)
    before_bytes = target.stat().st_size
    shutil.copy2(probe, directory / "probe")
    observed = []
    for _ in range(2):
        run = subprocess.run([str(probe.resolve()), str(target.resolve())], check=True,
                             capture_output=True, text=True)
        observed.append(json.loads(run.stdout))
    assert fingerprint(target, exact=True) == before
    with sqlite3.connect(target) as conn:
        edges = conn.execute("SELECT count(*) FROM memweft_learning_sources").fetchone()[0]
        assert conn.execute("SELECT version FROM memweft_learning_source_version").fetchall() == [(1,)]
    result = {"first_open_ms": observed[0]["open_ms"], "reopen_ms": observed[1]["open_ms"],
              "before_bytes": before_bytes, "after_bytes": target.stat().st_size,
              "edges": edges, "authoritative_rows_sha256": before,
              "probe_sha256": sha(probe), "exact_rows_preserved": True,
              "limits": "First open includes SDK initialization and index backfill, excludes close; file sizes after process exit, not peak migration/WAL space."}
    (directory / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--upgrade-probe", type=Path)
    args = parser.parse_args()
    report = summarize(args.input)
    if args.upgrade_probe:
        report["migration"] = migration(args.input, args.upgrade_probe)
    else:
        report["migration"] = json.loads((args.input / "migration" / "result.json").read_text())
        assert sha(args.input / "migration" / "probe") == report["migration"]["probe_sha256"]
        assert fingerprint(args.input / "migration" / "memory.db", exact=True) == report["migration"]["authoritative_rows_sha256"]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"verified_pairs": len(CASES), "migration": report["migration"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
