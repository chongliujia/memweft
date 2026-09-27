"""Offline before/after native SDK checks for dense two-term intersections.

Prepare a disposable fixture once, then run each native build in a separate
process against that same fixture. Setup is outside request timings.
"""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import sqlite3
import time

from benchmark_bounded import dump, load_sdk, sha, stats


def prepare(Memory, path, count, profiles):
    if path.exists():
        raise ValueError("fixture already exists; use a new database path")
    path.parent.mkdir(parents=True, exist_ok=True)
    with Memory(str(path)):
        pass
    with sqlite3.connect(path) as conn:
        # Fixture loading only. The native SDK then rebuilds its own exact index
        # from the authoritative facts; no Python tokenizer substitutes for it.
        for table in ["facts", "memweft_pool_facts"]:
            for operation in ["insert", "update", "delete"]:
                conn.execute(f"DROP TRIGGER memweft_recall_{table}_{operation}")
        conn.executescript("DROP TABLE memweft_recall_terms; DROP TABLE IF EXISTS memweft_recall_blocks; DROP TABLE memweft_recall_items; DROP TABLE memweft_recall_version;")
        def rows():
            for profile in profiles:
                for i in range(count):
                    if profile == "balanced":
                        value = "blue" if i % 2 else "red"
                    elif profile == "clustered":
                        value = "blue" if i < count // 2 else "red"
                    elif profile == "skewed":
                        value = "red" if i >= count - 128 else "blue"
                    else:
                        value = "blue" if i < 200 else "blue red"
                    key = f"a{i:09}"
                    yield (profile, f"memweft:key:{key}", key, json.dumps(value + " common"))
                yield (profile, "memweft:key:zlate", "zlate", json.dumps("blue red common unique"))
        conn.executemany("""INSERT INTO facts
            (tenant_id,user_id,agent_id,fact_id,fact_key,value_json,status,
             valid_from,valid_to,confidence,sources,scope_level,notes)
            VALUES ('default',?,'default',?,?,?,'active',NULL,NULL,1,'[]','user','')""", rows())
    with Memory(str(path)):
        pass
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchone() is None
    return {"facts_per_profile": count + 1, "profiles": profiles,
            "database_sha256": sha(path), "database_bytes": path.stat().st_size}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--facts", type=int, default=100000)
    parser.add_argument("--profiles", nargs="+", choices=["balanced", "clustered", "skewed", "large"],
                        default=["balanced", "clustered", "skewed", "large"])
    parser.add_argument("--samples", type=int, default=20)
    args = parser.parse_args()
    if args.facts < 1000 or args.samples < 1:
        parser.error("facts must be >=1000; samples must be positive")
    if args.output.exists():
        parser.error("output already exists; use a new output path")
    Memory = load_sdk(args.native)
    fixture_file = args.database.with_suffix(".fixture.json")
    if args.prepare:
        dump(fixture_file, prepare(Memory, args.database, args.facts, args.profiles))
    fixture = json.loads(fixture_file.read_text())
    assert fixture["database_sha256"] == sha(args.database)
    report = {"label": args.label, "platform": platform.platform(), "fixture": fixture,
        "python": platform.python_version(), "native_sha256": sha(args.native),
        "runner_sha256": sha(Path(__file__)), "samples": args.samples, "results": []}
    with Memory(str(args.database)) as memory:
        for profile in fixture["profiles"]:
            session = memory.user(profile).session("query")
            for query in ["red blue", "blue", "common", "unique", "missing", None]:
                measurements = []
                previous = None
                for i in range(args.samples + 1):
                    start = time.perf_counter()
                    context = session.context(query=query, max_facts=10, max_tokens=4096, include_messages=False)
                    elapsed = (time.perf_counter() - start) * 1000
                    data = vars(context)
                    if previous is not None:
                        assert data == previous
                    previous = data
                    if i:
                        measurements.append(elapsed)
                if query == "red blue" and profile != "large":
                    assert context.memories[0]["fact_key"] == "zlate"
                assert context.report["recall"]["inspected_facts"] <= 74
                row = {"profile": profile, "query": query, **stats(measurements),
                    "samples_ms": measurements, "plan": context.report["recall"]["ranking_plan"],
                    "context_sha256": hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()}
                report["results"].append(row)
                print(json.dumps({k: row[k] for k in ["profile", "query", "p50_ms", "p95_ms", "plan"]}), flush=True)
    assert fixture["database_sha256"] == sha(args.database), "query run modified fixture"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    dump(args.output, report)


if __name__ == "__main__":
    main()
