#!/usr/bin/env python3
import argparse
import json
import os
import random
import tempfile
import threading
import time
import uuid
from pathlib import Path
import sys

from memweft import Memory
from bench_config import (
    env_float,
    env_int,
    env_str,
    find_config_arg,
    load_bench_env,
)


def unique_suffix():
    return uuid.uuid4().hex


def sample_scope():
    suffix = unique_suffix()
    return {
        "tenant_id": f"tenant-{suffix}",
        "user_id": f"user-{suffix}",
        "agent_id": f"agent-{suffix}",
        "session_id": f"session-{suffix}",
        "run_id": f"run-{suffix}",
    }


def sample_event(scope, event_id):
    return {
        "event_id": event_id,
        "scope": scope,
        "ts_ms": int(time.time() * 1000),
        "kind": "message",
        "payload": {"role": "user", "content": "hello"},
        "tags": ["intro"],
        "entities": [],
    }


def percentile(samples, pct):
    if not samples:
        return None
    values = sorted(samples)
    k = int(round((pct / 100.0) * (len(values) - 1)))
    return values[min(max(k, 0), len(values) - 1)]


def record_sample(samples, seen, value, max_samples, rng):
    if seen <= max_samples:
        samples.append(value)
        return
    idx = rng.randint(0, seen - 1)
    if idx < max_samples:
        samples[idx] = value


class OpStats:
    def __init__(self, name, max_samples, rng):
        self.name = name
        self.count = 0
        self.total_ms = 0.0
        self.samples = []
        self.max_samples = max_samples
        self.rng = rng

    def record(self, elapsed_ms):
        self.count += 1
        self.total_ms += elapsed_ms
        record_sample(self.samples, self.count, elapsed_ms, self.max_samples, self.rng)

    def summarize(self):
        avg = self.total_ms / self.count if self.count else 0.0
        return {
            "count": self.count,
            "avg_ms": avg,
            "p50_ms": percentile(self.samples, 50) or 0.0,
            "p95_ms": percentile(self.samples, 95) or 0.0,
            "p99_ms": percentile(self.samples, 99) or 0.0,
        }


def seed_events(mem, scope, events):
    for idx in range(events):
        mem.append_event(sample_event(scope, f"seed-{idx}"))


def run_load(
    backend_name,
    mem,
    duration_s,
    concurrency,
    seed_events_count,
    mix,
    max_samples,
    list_limit,
):
    scope = sample_scope()
    seed_events(mem, scope, seed_events_count)

    op_names = ["append", "list", "build"]
    rng = random.Random(42)
    op_stats = {name: OpStats(name, max_samples, rng) for name in op_names}
    counter_lock = threading.Lock()
    counters = {"append": 0}
    stop_at = time.perf_counter() + duration_s
    request = {"scope": scope, "purpose": "planner", "task_type": "generic"}

    mix_total = sum(mix.values())
    if mix_total <= 0:
        raise ValueError("mix ratios must be > 0")
    mix_norm = {k: v / mix_total for k, v in mix.items()}

    def pick_op():
        r = rng.random()
        cumulative = 0.0
        for name in op_names:
            cumulative += mix_norm.get(name, 0.0)
            if r <= cumulative:
                return name
        return "build"

    def worker():
        while time.perf_counter() < stop_at:
            op = pick_op()
            start = time.perf_counter()
            if op == "append":
                with counter_lock:
                    counters["append"] += 1
                    eid = counters["append"]
                mem.append_event(sample_event(scope, f"{backend_name}-e{eid}"))
            elif op == "list":
                mem.list_events(scope, limit=list_limit)
            else:
                mem.build_memory_packet(request)
            elapsed = (time.perf_counter() - start) * 1000.0
            op_stats[op].record(elapsed)

    threads = []
    for _ in range(concurrency):
        t = threading.Thread(target=worker, daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()

    total_ops = sum(stat.count for stat in op_stats.values())
    ops_per_sec = total_ops / duration_s if duration_s else 0.0

    return {
        "backend": backend_name,
        "ops_per_sec": ops_per_sec,
        "append": op_stats["append"].summarize(),
        "list": op_stats["list"].summarize(),
        "build": op_stats["build"].summarize(),
    }


def resolve_backends(args):
    backends = []
    if args.backend:
        backends.append(args.backend)
        return backends
    backends.extend(["sqlite-memory", "sqlite-file"])
    if env_str("MEMWEFT_LOAD_MYSQL_DSN"):
        backends.append("mysql")
    if env_str("MEMWEFT_LOAD_POSTGRES_DSN"):
        backends.append("postgres")
    return backends


def build_memory_for_backend(name, args):
    if name == "sqlite-memory":
        return Memory(in_memory=True)
    if name == "sqlite-file":
        if args.sqlite_path:
            return Memory(path=args.sqlite_path)
        tmpdir = tempfile.mkdtemp()
        return Memory(path=os.path.join(tmpdir, "memweft.db"))
    if name == "mysql":
        dsn = env_str("MEMWEFT_LOAD_MYSQL_DSN")
        database = env_str("MEMWEFT_LOAD_MYSQL_DB")
        return Memory(backend="mysql", dsn=dsn, database=database)
    if name == "postgres":
        dsn = env_str("MEMWEFT_LOAD_POSTGRES_DSN")
        database = env_str("MEMWEFT_LOAD_POSTGRES_DB")
        return Memory(backend="postgres", dsn=dsn, database=database)
    raise ValueError(f"unknown backend: {name}")


def main():
    config_arg = find_config_arg(sys.argv[1:])
    load_bench_env(config_arg)
    repo_root = Path(__file__).resolve().parents[2]
    default_output = repo_root / "target" / "python_load.json"

    parser = argparse.ArgumentParser(description="Concurrent load test for MemWeft backends.")
    parser.add_argument(
        "--config",
        default=config_arg,
        help="Optional bench config file path.",
    )
    parser.add_argument(
        "--backend",
        default=env_str("MEMWEFT_LOAD_BACKEND"),
        help="Optional single backend to test.",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=env_int("MEMWEFT_LOAD_DURATION", 60),
        help="Test duration (seconds).",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=env_int("MEMWEFT_LOAD_CONCURRENCY", 8),
        help="Worker threads.",
    )
    parser.add_argument(
        "--seed-events",
        type=int,
        default=env_int("MEMWEFT_LOAD_SEED_EVENTS", 2000),
        help="Seed events per backend.",
    )
    parser.add_argument(
        "--list-limit",
        type=int,
        default=env_int("MEMWEFT_LOAD_LIST_LIMIT", 50),
        help="list_events limit.",
    )
    parser.add_argument(
        "--append-ratio",
        type=float,
        default=env_float("MEMWEFT_LOAD_APPEND_RATIO", 0.3),
        help="Append ratio.",
    )
    parser.add_argument(
        "--list-ratio",
        type=float,
        default=env_float("MEMWEFT_LOAD_LIST_RATIO", 0.4),
        help="List ratio.",
    )
    parser.add_argument(
        "--build-ratio",
        type=float,
        default=env_float("MEMWEFT_LOAD_BUILD_RATIO", 0.3),
        help="Build ratio.",
    )
    parser.add_argument(
        "--max-samples",
        type=int,
        default=env_int("MEMWEFT_LOAD_MAX_SAMPLES", 10000),
        help="Max latency samples.",
    )
    parser.add_argument(
        "--sqlite-path",
        default=env_str("MEMWEFT_LOAD_SQLITE_PATH"),
        help="Optional SQLite file path.",
    )
    parser.add_argument("--output", default=str(default_output), help="Output JSON path.")
    args = parser.parse_args()

    mix = {"append": args.append_ratio, "list": args.list_ratio, "build": args.build_ratio}
    results = []
    for backend in resolve_backends(args):
        mem = build_memory_for_backend(backend, args)
        results.append(
            run_load(
                backend,
                mem,
                args.duration,
                args.concurrency,
                args.seed_events,
                mix,
                args.max_samples,
                args.list_limit,
            )
        )

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    prev_path = output_path.with_name("python_load_prev.json")
    if output_path.exists():
        prev_path.write_text(output_path.read_text(encoding="utf-8"), encoding="utf-8")

    payload = {
        "duration_s": args.duration,
        "concurrency": args.concurrency,
        "seed_events": args.seed_events,
        "mix": mix,
        "results": results,
    }
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote {output_path}")
    if prev_path.exists():
        print(f"Wrote {prev_path}")


if __name__ == "__main__":
    main()
