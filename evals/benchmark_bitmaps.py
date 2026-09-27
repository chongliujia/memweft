#!/usr/bin/env python3
"""Compare v2/v3 SDK queries, upgrade size/time and source-write costs on copies."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import shutil
import sqlite3
import subprocess
import sys
import time

from benchmark_bounded import copy_db, dump, integrity, load_sdk, sha, stats


def worker(args):
    Memory = load_sdk(args.native)
    fixture = json.loads(args.fixture.read_text())
    report = {"native_sha256": sha(args.native), "source_bytes": args.database.stat().st_size,
              "source_sha256": sha(args.database), "fixture": fixture, "queries": [], "writes": {}}
    start = time.perf_counter()
    with Memory(str(args.database)):
        pass
    report["open_upgrade_close_ms"] = (time.perf_counter() - start) * 1000
    report["bytes_after_upgrade"] = args.database.stat().st_size
    with sqlite3.connect(args.database) as conn:
        report["index_version"] = conn.execute("SELECT version FROM memweft_recall_version").fetchone()[0]
        if report["index_version"] >= 3:
            report["bitmap_rows"] = conn.execute("SELECT count(*) FROM memweft_recall_blocks").fetchone()[0]
        report["posting_rows"] = conn.execute("SELECT count(*) FROM memweft_recall_terms").fetchone()[0]
    with Memory(str(args.database)) as memory:
        for profile in fixture["profiles"]:
            session = memory.user(profile).session("query")
            for query in ["red blue", "blue", "common", "unique", "missing", None]:
                samples, previous = [], None
                for i in range(args.samples + 1):
                    start = time.perf_counter()
                    context = session.context(query=query, max_facts=10, max_tokens=4096, include_messages=False)
                    elapsed = (time.perf_counter() - start) * 1000
                    data = vars(context)
                    if previous is not None:
                        assert data == previous
                    previous = data
                    if i:
                        samples.append(elapsed)
                if query == "red blue" and profile != "large":
                    assert context.memories[0]["fact_key"] == "zlate"
                report["queries"].append({"profile": profile, "query": query,
                    "context_sha256": hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest(),
                    "plan": context.report["recall"]["ranking_plan"], "samples_ms": samples, **stats(samples)})
        for pool in ["private", "team"]:
            user = memory.user("bitmap-writes", memory_config={"read_pools": [{"pool_id": pool, "access": "read_write"}],
                                                             "default_write_pool": pool})
            for operation in ["upsert", "insert"]:
                samples = []
                for i in range(args.writes):
                    key = "same" if operation == "upsert" else f"new-{i:04}"
                    start = time.perf_counter()
                    record = user.remember(i, key=key)
                    samples.append((time.perf_counter() - start) * 1000)
                    assert record["value"] == i
                report["writes"][f"{pool}_{operation}"] = {"samples_ms": samples, **stats(samples)}
    report["integrity"] = integrity(args.database)
    dump(args.output, report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path)
    parser.add_argument("--after", type=Path)
    parser.add_argument("--source", type=Path, action="append")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--writes", type=int, default=200)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--native", type=Path)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--fixture", type=Path)
    args = parser.parse_args()
    if args.samples < 1 or args.writes < 1:
        parser.error("sample and write counts must be positive")
    if args.worker:
        worker(args)
        return
    if not args.before or not args.after or not args.source:
        parser.error("before/after native builds and source fixtures are required")
    args.output.mkdir(parents=True, exist_ok=False)
    dump(args.output / "status.json", {"status": "running"})
    manifest = {"platform": platform.platform(), "python": platform.python_version(),
                "runner_sha256": sha(Path(__file__)), "helper_sha256": sha(Path(__file__).with_name("benchmark_bounded.py")),
                "sources": [str(p) for p in args.source], "samples": args.samples, "writes": args.writes}
    for name in ["benchmark_bitmaps.py", "benchmark_bounded.py"]:
        shutil.copyfile(Path(__file__).with_name(name), args.output / name)
    for label, native in [("before", args.before), ("after", args.after)]:
        shutil.copyfile(native, args.output / f"{label}.so")
        manifest[f"{label}_native_sha256"] = sha(native)
    dump(args.output / "manifest.json", manifest)
    result = {"manifest": manifest, "fixtures": []}
    try:
        for index, source in enumerate(args.source):
            fixture = source.with_suffix(".fixture.json")
            pair = {}
            for label in ["before", "after"]:
                database = args.output / f"{index}-{label}.db"
                copy_db(source, database)
                output = args.output / f"{index}-{label}.json"
                subprocess.run([sys.executable, str(Path(__file__).resolve()), "--worker", "--native",
                    str((args.output/f"{label}.so").resolve()), "--database", str(database.resolve()),
                    "--fixture", str(fixture.resolve()), "--output", str(output.resolve()),
                    "--samples", str(args.samples), "--writes", str(args.writes)], check=True, timeout=900)
                pair[label] = json.loads(output.read_text())
                print(json.dumps({"fixture": index, "build": label, "upgrade_ms": pair[label]["open_upgrade_close_ms"],
                    "two_terms": [(r["profile"], r["p50_ms"]) for r in pair[label]["queries"] if r["query"] == "red blue"]}), flush=True)
            assert pair["before"]["source_sha256"] == pair["after"]["source_sha256"]
            for a, b in zip(pair["before"]["queries"], pair["after"]["queries"], strict=True):
                for field in ["profile", "query", "context_sha256", "plan"]:
                    assert a[field] == b[field], (field, a, b)
            result["fixtures"].append(pair)
        dump(args.output / "results.json", result)
        dump(args.output / "status.json", {"status": "completed"})
    except BaseException as error:
        dump(args.output / "status.json", {"status": "failed", "error": repr(error)})
        raise


if __name__ == "__main__":
    main()
