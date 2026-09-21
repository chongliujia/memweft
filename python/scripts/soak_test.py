#!/usr/bin/env python3
import argparse
import json
import os
import tempfile
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
        return 0.0
    values = sorted(samples)
    k = int(round((pct / 100.0) * (len(values) - 1)))
    return values[min(max(k, 0), len(values) - 1)]


def seed_events(mem, scope, events):
    for idx in range(events):
        mem.append_event(sample_event(scope, f"seed-{idx}"))


def resolve_backend(args):
    if args.backend:
        return args.backend
    if env_str("MEMWEFT_SOAK_MYSQL_DSN"):
        return "mysql"
    if env_str("MEMWEFT_SOAK_POSTGRES_DSN"):
        return "postgres"
    return "sqlite-file"


def build_memory_for_backend(name, args):
    if name == "sqlite-memory":
        return Memory(in_memory=True)
    if name == "sqlite-file":
        if args.sqlite_path:
            return Memory(path=args.sqlite_path)
        tmpdir = tempfile.mkdtemp()
        return Memory(path=os.path.join(tmpdir, "memweft.db"))
    if name == "mysql":
        dsn = env_str("MEMWEFT_SOAK_MYSQL_DSN")
        database = env_str("MEMWEFT_SOAK_MYSQL_DB")
        return Memory(backend="mysql", dsn=dsn, database=database)
    if name == "postgres":
        dsn = env_str("MEMWEFT_SOAK_POSTGRES_DSN")
        database = env_str("MEMWEFT_SOAK_POSTGRES_DB")
        return Memory(backend="postgres", dsn=dsn, database=database)
    raise ValueError(f"unknown backend: {name}")


def run_soak(mem, duration_s, interval_s, seed_events_count, mix, list_limit):
    scope = sample_scope()
    seed_events(mem, scope, seed_events_count)
    request = {"scope": scope, "purpose": "planner", "task_type": "generic"}

    start = time.perf_counter()
    next_tick = start + interval_s
    end = start + duration_s

    interval_data = []
    append_lat = []
    list_lat = []
    build_lat = []
    op_counts = {"append": 0, "list": 0, "build": 0}

    mix_total = sum(mix.values())
    mix_norm = {k: v / mix_total for k, v in mix.items()}

    def pick_op():
        r = time.perf_counter() % 1.0
        cumulative = 0.0
        for name in ("append", "list", "build"):
            cumulative += mix_norm.get(name, 0.0)
            if r <= cumulative:
                return name
        return "build"

    append_counter = 0

    while time.perf_counter() < end:
        op = pick_op()
        t0 = time.perf_counter()
        if op == "append":
            append_counter += 1
            mem.append_event(sample_event(scope, f"soak-{append_counter}"))
        elif op == "list":
            mem.list_events(scope, limit=list_limit)
        else:
            mem.build_memory_packet(request)
        elapsed = (time.perf_counter() - t0) * 1000.0
        if op == "append":
            append_lat.append(elapsed)
        elif op == "list":
            list_lat.append(elapsed)
        else:
            build_lat.append(elapsed)
        op_counts[op] += 1

        now = time.perf_counter()
        if now >= next_tick:
            interval_ops = sum(op_counts.values())
            interval_duration = interval_s
            interval_data.append(
                {
                    "t_s": int(now - start),
                    "ops_per_sec": interval_ops / interval_duration if interval_duration else 0.0,
                    "append_p95_ms": percentile(append_lat, 95),
                    "list_p95_ms": percentile(list_lat, 95),
                    "build_p95_ms": percentile(build_lat, 95),
                }
            )
            append_lat.clear()
            list_lat.clear()
            build_lat.clear()
            op_counts = {"append": 0, "list": 0, "build": 0}
            next_tick = now + interval_s

    return interval_data


def main():
    config_arg = find_config_arg(sys.argv[1:])
    load_bench_env(config_arg)
    repo_root = Path(__file__).resolve().parents[2]
    default_output = repo_root / "target" / "python_soak.json"

    parser = argparse.ArgumentParser(description="Soak test for MemWeft backends.")
    parser.add_argument(
        "--config",
        default=config_arg,
        help="Optional bench config file path.",
    )
    parser.add_argument(
        "--backend",
        default=env_str("MEMWEFT_SOAK_BACKEND"),
        help="Optional backend override.",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=env_int("MEMWEFT_SOAK_DURATION", 600),
        help="Duration in seconds.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=env_int("MEMWEFT_SOAK_INTERVAL", 60),
        help="Interval in seconds.",
    )
    parser.add_argument(
        "--seed-events",
        type=int,
        default=env_int("MEMWEFT_SOAK_SEED_EVENTS", 2000),
        help="Seed events count.",
    )
    parser.add_argument(
        "--list-limit",
        type=int,
        default=env_int("MEMWEFT_SOAK_LIST_LIMIT", 50),
        help="list_events limit.",
    )
    parser.add_argument(
        "--append-ratio",
        type=float,
        default=env_float("MEMWEFT_SOAK_APPEND_RATIO", 0.3),
        help="Append ratio.",
    )
    parser.add_argument(
        "--list-ratio",
        type=float,
        default=env_float("MEMWEFT_SOAK_LIST_RATIO", 0.4),
        help="List ratio.",
    )
    parser.add_argument(
        "--build-ratio",
        type=float,
        default=env_float("MEMWEFT_SOAK_BUILD_RATIO", 0.3),
        help="Build ratio.",
    )
    parser.add_argument(
        "--sqlite-path",
        default=env_str("MEMWEFT_SOAK_SQLITE_PATH"),
        help="Optional SQLite file path.",
    )
    parser.add_argument("--output", default=str(default_output), help="Output JSON path.")
    args = parser.parse_args()

    backend = resolve_backend(args)
    mem = build_memory_for_backend(backend, args)
    mix = {"append": args.append_ratio, "list": args.list_ratio, "build": args.build_ratio}
    interval_data = run_soak(
        mem,
        args.duration,
        args.interval,
        args.seed_events,
        mix,
        args.list_limit,
    )

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    prev_path = output_path.with_name("python_soak_prev.json")
    if output_path.exists():
        prev_path.write_text(output_path.read_text(encoding="utf-8"), encoding="utf-8")

    payload = {
        "duration_s": args.duration,
        "interval_s": args.interval,
        "seed_events": args.seed_events,
        "mix": mix,
        "results": [{"backend": backend, "series": interval_data}],
    }
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote {output_path}")
    if prev_path.exists():
        print(f"Wrote {prev_path}")


if __name__ == "__main__":
    main()
