"""Offline SDK benchmark for long conversations and unrelated scoped documents.

Run each build in a separate process via PYTHONPATH. Seeding and index creation
are outside request timings; this is a warm, single-client microbenchmark.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import platform
import sqlite3
import statistics
import tempfile
import time


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def measure(action, repeats):
    samples = []
    for _ in range(repeats):
        start = time.perf_counter()
        action()
        samples.append((time.perf_counter() - start) * 1000)
    ordered = sorted(samples)
    return {"p50_ms": statistics.median(samples),
            "p95_ms": ordered[math.ceil(.95 * len(ordered)) - 1],
            "samples_ms": samples}


def seed(path, count):
    # Use SQL only for message fixture loading; measured operations use the SDK.
    # SQLite resolves functions in conditional document triggers even when their
    # learning-namespace WHEN condition is false. Register a fail-closed stub for
    # this non-learning loader, never a replacement dependency extractor.
    def message_dependencies(encoded):
        document = json.loads(encoded)
        if document["namespace"][0] != "messages":
            raise ValueError("fixture loader may only insert message documents")
        return "[]"
    origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
    def rows():
        for i in range(count):
            ts = (origin + timedelta(seconds=i)).isoformat().replace("+00:00", "Z")
            for namespace in [["messages", "long"], ["messages", f"other-{i % 100}"]]:
                key = f"{i:09}"
                document = dict(namespace=namespace, key=key,
                    value={"role": "user", "content": f"message {i}", "run_id": None},
                    revision=1, created_at=ts, updated_at=ts)
                yield ("default", "u", "default", json.dumps(namespace, separators=(",", ":")),
                       key, 1, json.dumps(document))
    with sqlite3.connect(path) as conn:
        conn.create_function("memweft_learning_sources_v1", 1, message_dependencies,
                             deterministic=True)
        conn.executemany("INSERT INTO memweft_documents VALUES (?,?,?,?,?,?,?)", rows())


def run(count, repeats):
    from memweft import Memory
    with tempfile.TemporaryDirectory(prefix="memweft-documents-") as directory:
        path = str(Path(directory) / "memory.db")
        with Memory(path) as memory:
            user = memory.user("u")
            user.remember("brief", key="style")
            user.learning.start(id="v1", proposal={"task_type": "answer", "content": "Be brief",
                "proposer_version": "p1", "source_keys": ["style"]},
                dataset_version="v1", evaluator_version="e1", case_ids=["a", "b", "c"])
            user.learning.submit("v1", {"dataset_version": "v1", "evaluator_version": "e1",
                "cases": [{"case_id": c, "baseline_score": 0, "candidate_score": 1,
                    "candidate_cost": 0, "candidate_latency_ms": 1} for c in ["a", "b", "c"]]})
        seed(path, count)
        with Memory(path) as memory:
            user = memory.user("u")
            chat = user.session("long")
            def context():
                result = chat.context(conversation_window=10, task_type="answer")
                assert [d["key"] for d in result.messages] == [f"{i:09}" for i in range(count - 10, count)]
                assert result.strategies[0]["version"] == "v1"
                return result
            reference = context()
            results = {"messages": count, "unrelated_messages": count,
                "database_bytes": Path(path).stat().st_size,
                "context_sha256": digest(vars(reference)), "context": measure(context, repeats),
                "learning_active": measure(lambda: user.learning.active("answer"), repeats)}
            counter = iter(range(repeats))
            results["append_message"] = measure(lambda: chat.add_message("user", "new message",
                event_id=f"append-{next(counter)}"), repeats)
            # Repeating an existing message exercises the conflict/retry point lookup.
            results["retry_message"] = measure(lambda: chat.add_message("user", "new message",
                event_id="append-0"), repeats)
            assert len(chat.messages()) == count + repeats
            return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sizes", type=int, nargs="+", default=[1000, 10000, 100000])
    parser.add_argument("--repeats", type=int, default=20)
    args = parser.parse_args()
    if args.repeats < 1 or any(n < 10 for n in args.sizes):
        parser.error("repeats must be positive and sizes must be >=10")
    import memweft._core as core
    report = {"label": args.label, "platform": platform.platform(), "python": platform.python_version(),
        "native_sha256": hashlib.sha256(Path(core.__file__).read_bytes()).hexdigest(),
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "repeats": args.repeats, "results": []}
    for size in args.sizes:
        result = run(size, args.repeats)
        report["results"].append(result)
        print(json.dumps({"label": args.label, "messages": size,
            **{key: round(result[key]["p50_ms"], 3) for key in
               ["context", "learning_active", "append_message", "retry_message"]}}), flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
