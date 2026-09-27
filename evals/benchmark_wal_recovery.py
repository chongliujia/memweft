#!/usr/bin/env python3
"""Multiwriter/slow-query pressure followed by recovery with all SDK clients open.

Use a disposable source from benchmark_intersections.py (profile: balanced).
The runner copies it, never modifies the source, and retains unsuccessful runs.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor
import hashlib
import json
import math
import multiprocessing
from pathlib import Path
import platform
import shutil
import sqlite3
import time

from benchmark_bounded import copy_db, dump, integrity, load_sdk, sha
from benchmark_pressure import LIVE, stats, usage

QUERIES = [("two_terms", "red blue"), ("single", "blue"), ("common", "common"),
           ("rare", "unique"), ("absent", "missing"), ("empty", None)]


def digest(context):
    return hashlib.sha256(json.dumps(vars(context), sort_keys=True).encode()).hexdigest()


def slots(seconds, rate, stagger):
    return max(0, math.ceil((seconds - stagger) * rate))


def next_slot(slot, elapsed, rate):
    # Missed arrivals are skipped, never accumulated into a catch-up queue.
    return max(slot + 1, math.ceil(elapsed * rate))


def wal_bytes(database):
    try:
        return Path(str(database) + "-wal").stat().st_size
    except FileNotFoundError:
        return 0


def checkpoint_options(config):
    options = {"background_checkpoint_ms": config["interval_ms"]}
    if config["mode"] == "reclaim":
        options["wal_reclaim_threshold_bytes"] = config["threshold_bytes"]
    return {"sqlite_options": options}


def sample_status(memory, start):
    return {"at_s": time.perf_counter() - start, "status": memory.storage_status()}


def client(config, kind, index, expected, barrier, start_at, drained, release):
    output = Path(config["output"])
    name = f"{kind}-{index}"
    samples, maintenance = [], []
    try:
        Memory = load_sdk(Path(config["native"]))
        with Memory(config["database"], **checkpoint_options(config)) as memory:
            session = memory.user(config["user"]).session("wal-pressure")
            user = memory.user(config["user"], agent_id=name, memory_config=LIVE)
            live = user.session("live")
            if kind == "reader":
                for query_name, query in QUERIES:
                    assert digest(session.context(query=query, max_facts=10,
                        max_tokens=4096, include_messages=False)) == expected[query_name]
            barrier.wait(timeout=120)
            while start_at.value == 0:
                time.sleep(0.001)
            start = start_at.value
            total_rate = config["read_rate"] if kind == "reader" else config["write_rate"]
            count = config["readers"] if kind == "reader" else config["writers"]
            rate = total_rate / count
            stagger = index / total_rate
            offered = slots(config["seconds"], rate, stagger)
            end = start + config["seconds"]
            slot, generation, revision = 0, 0, 1
            last_seen = {i: 0 for i in range(config["writers"])}
            before_usage = usage()
            next_status = start
            while slot < offered:
                deadline = start + stagger + slot / rate
                now = time.perf_counter()
                if now >= end:
                    break
                if now < deadline:
                    time.sleep(deadline - now)
                before = time.perf_counter()
                if before >= end:
                    break
                row = {"slot": slot, "at_s": before - start,
                       "lag_ms": (before - deadline) * 1000}
                if kind == "writer":
                    generation += 1
                    record = user.remember({"writer": index, "generation": generation,
                        "mirror": generation}, key=f"pulse-{index}", expected_revision=revision)
                    revision = record["revision"]
                    assert revision == generation + 1
                    row["ms"] = (time.perf_counter() - before) * 1000
                    row["revision"] = revision
                else:
                    query_name, query = QUERIES[(len(samples) + index) % len(QUERIES)]
                    context = session.context(query=query, max_facts=10, max_tokens=4096,
                                              include_messages=False)
                    row["ms"] = (time.perf_counter() - before) * 1000
                    assert digest(context) == expected[query_name]
                    row["query"] = query_name
                    snapshot = live.context(max_facts=config["writers"], max_tokens=16384,
                                            include_messages=False)
                    selected = {r["key"]: r for r in snapshot.report["pools"]["selected"]}
                    assert len(snapshot.memories) == config["writers"]
                    for fact in snapshot.memories:
                        value = fact["value"]
                        writer = value["writer"]
                        assert value["generation"] == value["mirror"]
                        assert value["generation"] >= last_seen[writer]
                        assert selected[fact["fact_key"]]["revision"] == value["generation"] + 1
                        last_seen[writer] = value["generation"]
                samples.append(row)
                if before >= next_status:
                    maintenance.append(sample_status(memory, start))
                    next_status = before + 1
                slot = next_slot(slot, time.perf_counter() - start - stagger, rate)
            after_usage = usage()
            result = {"kind": kind, "index": index, "offered_slots": offered,
                "completed": len(samples), "missed_slots": offered - len(samples),
                "load_finished_s": time.perf_counter() - start,
                "resources": {k: after_usage[k] - v for k, v in before_usage.items()},
                "latency": stats([s["ms"] for s in samples]),
                "scheduled_latency": stats([s["ms"] + s["lag_ms"] for s in samples]),
                "last_revision": revision if kind == "writer" else None}
            # All callers remain open: recovery cannot be credited to final-close
            # cleanup. Only the controller releases this gate after observation.
            drained.wait(timeout=120)
            until = time.perf_counter() + config["recovery_seconds"] + 120
            while not release.wait(timeout=0.25):
                if time.perf_counter() >= until:
                    raise TimeoutError("controller did not release idle clients")
                if time.perf_counter() >= next_status:
                    maintenance.append(sample_status(memory, start))
                    next_status = time.perf_counter() + 1
            result["final_status"] = memory.storage_status()
            result["connection_close_after_s"] = time.perf_counter() - start
            assert result["final_status"]["checkpoint"]["mode"] == "background"
            assert result["final_status"]["checkpoint"]["progress"]["last_error"] is None
        dump(output / f"{name}.json", {**result, "samples": samples, "maintenance": maintenance})
        return result
    except BaseException as error:
        dump(output / f"{name}-failed.json", {"error": repr(error), "samples": samples,
                                             "maintenance": maintenance})
        barrier.abort()
        drained.abort()
        raise


def pin_reader(config, barrier, start_at):
    barrier.wait(timeout=120)
    while start_at.value == 0:
        time.sleep(0.001)
    start = start_at.value
    time.sleep(max(0, start + config["pin_at"] - time.perf_counter()))
    conn = sqlite3.connect(Path(config["database"]).resolve().as_uri() + "?mode=ro", uri=True)
    query = """SELECT fact_key,revision,record FROM memweft_pool_facts
        WHERE tenant_id='default' AND user_id=? AND pool_id='pressure-live' ORDER BY fact_key"""
    try:
        conn.execute("BEGIN")
        old = conn.execute(query, (config["user"],)).fetchall()
        began = time.perf_counter() - start
        time.sleep(config["pin_seconds"])
        assert conn.execute(query, (config["user"],)).fetchall() == old
        conn.execute("COMMIT")
        released = time.perf_counter() - start
        new = conn.execute(query, (config["user"],)).fetchall()
        assert len(old) == len(new) == config["writers"]
        assert all(a[0] == b[0] and a[1] < b[1] for a, b in zip(old, new, strict=True))
        result = {"start_s": began, "release_s": released, "snapshot_stable": True,
                  "old_revisions": [r[1] for r in old], "new_revisions": [r[1] for r in new]}
        dump(Path(config["output"]) / "pinned-reader.json", result)
        return result
    finally:
        conn.close()


def run(config):
    output = Path(config["output"])
    database = Path(config["database"])
    copy_db(Path(config["source"]), database)
    fixture_hash = sha(database)
    Memory = load_sdk(Path(config["native"]))
    with Memory(str(database)) as memory:
        user = memory.user(config["user"], memory_config=LIVE)
        for i in range(config["writers"]):
            user.remember({"writer": i, "generation": 0, "mirror": 0},
                          key=f"pulse-{i}", expected_revision=0)
        session = memory.user(config["user"]).session("wal-pressure")
        contexts = {name: vars(session.context(query=query, max_facts=10, max_tokens=4096,
            include_messages=False)) for name, query in QUERIES}
        assert contexts["two_terms"]["memories"][0]["fact_key"] == "zlate"
        status = memory.storage_status()
    expected = {name: hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
                for name, value in contexts.items()}
    dump(output / "expected.json", {"contexts": contexts, "sha256": expected})
    ctx = multiprocessing.get_context("spawn")
    monitor = []
    with ctx.Manager() as manager:
        count = config["readers"] + config["writers"]
        barrier = manager.Barrier(count + 2)
        drained = manager.Barrier(count + 1)
        start_at, release = manager.Value("d", 0.0), manager.Event()
        with ProcessPoolExecutor(count + 1, mp_context=ctx) as executor:
            tasks = [executor.submit(client, config, kind, index, expected, barrier,
                                     start_at, drained, release)
                     for kind, n in [("reader", config["readers"]), ("writer", config["writers"])]
                     for index in range(n)]
            held = executor.submit(pin_reader, config, barrier, start_at)
            def observe(phase):
                monitor.append({"at_s": time.perf_counter() - start_at.value,
                                "phase": phase, "wal_bytes": wal_bytes(database)})
            try:
                barrier.wait(timeout=120)
                # Publish the epoch after every client has completed warmup.
                start_at.value = time.perf_counter() + 0.2
                until = start_at.value + config["seconds"]
                next_progress = start_at.value + 30
                while time.perf_counter() < until:
                    for task in [*tasks, held]:
                        if task.done() and task.exception() is not None:
                            task.result()
                    observe("load")
                    if time.perf_counter() >= next_progress:
                        print(json.dumps(monitor[-1]), flush=True)
                        next_progress += 30
                    time.sleep(0.1)
                drained.wait(timeout=120)
                recovery_start = time.perf_counter() - start_at.value
                until = time.perf_counter() + config["recovery_seconds"]
                while time.perf_counter() < until:
                    observe("recovery")
                    time.sleep(0.1)
                observe("recovery")
                measured_end = time.perf_counter() - start_at.value
                release.set()
                clients = [task.result(timeout=120) for task in tasks]
                pinned = held.result(timeout=30)
            finally:
                release.set()
                barrier.abort()
                drained.abort()
                dump(output / "wal-samples.json", monitor)
    assert all(c["connection_close_after_s"] >= measured_end for c in clients)
    # This explicit integrity checkpoint happens only after measured recovery.
    checked = integrity(database)
    with sqlite3.connect(database) as conn:
        for writer in (c for c in clients if c["kind"] == "writer"):
            revision, raw = conn.execute("""SELECT revision,record FROM memweft_pool_facts
                WHERE tenant_id='default' AND user_id=? AND pool_id='pressure-live' AND fact_key=?""",
                (config["user"], f"pulse-{writer['index']}")).fetchone()
            assert revision == writer["last_revision"] == writer["completed"] + 1
            assert json.loads(raw)["value"] == {"writer": writer["index"],
                "generation": writer["completed"], "mirror": writer["completed"]}
    result = {"config": config, "fixture_sha256": fixture_hash, "initial_status": status,
              "clients": clients, "pinned_reader": pinned, "recovery_start_s": recovery_start,
              "measured_end_s": measured_end, "integrity": checked,
              "expected_sha256": expected, "acknowledged_writes_verified": True}
    dump(output / "results.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=["passive", "reclaim"], required=True)
    parser.add_argument("--user", default="balanced")
    parser.add_argument("--seconds", type=float, default=300)
    parser.add_argument("--recovery-seconds", type=float, default=45)
    parser.add_argument("--readers", type=int, default=4)
    parser.add_argument("--writers", type=int, default=4)
    parser.add_argument("--read-rate", type=float, default=100)
    parser.add_argument("--write-rate", type=float, default=200)
    parser.add_argument("--pin-at", type=float, default=60)
    parser.add_argument("--pin-seconds", type=float, default=90)
    parser.add_argument("--interval-ms", type=int, default=1000)
    parser.add_argument("--threshold-bytes", type=int, default=16 * 1024 * 1024)
    args = parser.parse_args()
    if (args.readers < 1 or not 1 <= args.writers <= 32 or args.seconds <= 0
            or args.recovery_seconds <= 0 or args.read_rate <= 0 or args.write_rate <= 0
            or args.pin_at < 0 or args.pin_seconds <= 0
            or args.pin_at + args.pin_seconds >= args.seconds
            or not 100 <= args.interval_ms <= 60000
            or not 65536 <= args.threshold_bytes <= 1099511627776):
        parser.error("invalid duration, rates, clients, pin timing or maintenance options")
    args.output.mkdir(parents=True, exist_ok=False)
    config = {k: str(v.resolve()) if isinstance(v, Path) else v for k, v in vars(args).items()}
    config["database"] = str(args.output.resolve() / "memory.db")
    # Freeze the binary too: a subsequent SDK rebuild must not change a run's
    # worker implementation or make its evidence impossible to verify.
    native_snapshot = args.output.resolve() / "native.so"
    shutil.copyfile(args.native, native_snapshot)
    config["native"] = str(native_snapshot)
    manifest = {"config": config, "native_sha256": sha(args.native),
                "platform": platform.platform(), "python": platform.python_version(), "sources": {}}
    for name in ["benchmark_wal_recovery.py", "benchmark_pressure.py", "benchmark_bounded.py"]:
        source = Path(__file__).with_name(name)
        (args.output / name).write_bytes(source.read_bytes())
        manifest["sources"][name] = sha(source)
    dump(args.output / "manifest.json", manifest)
    dump(args.output / "status.json", {"status": "running"})
    try:
        result = run(config)
        dump(args.output / "status.json", {"status": "completed"})
        print(json.dumps({"completed": True, "recovery_start_s": result["recovery_start_s"],
                          "output": config["output"]}), flush=True)
    except BaseException as error:
        dump(args.output / "status.json", {"status": "failed", "error": repr(error)})
        raise


if __name__ == "__main__":
    main()
