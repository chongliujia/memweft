#!/usr/bin/env python3
"""Frozen, repeated comparison of lexical recall and application-required facts.

The benchmark deliberately observes one model proposal even with missing facts;
the usable example stops such requests before a paid call. Nothing is executed.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples"))
from kimi_memory import KimiClient, KimiError, MODEL, read_api_key
from guarded_workflow_agent import check_answer, facts_from_context, parse_proposal, workflow_messages
from workflow_guard import WORKFLOWS, validate_facts, validate_output
from memweft import Memory
import memweft._core as native
from run_local import append, dump

SUITE = ROOT / "evals/scenarios/workflow-guard-v1.json"
MODES = ("lexical", "required_facts")
REPEATS = 2


def scope_for(case):
    return {"tenant_id": "workflow-guard-eval", "user_id": case["id"], "agent_id": "workflow-assistant"}


def source_events(case):
    noise = [{"op": "remember", "key": f"archive_{i:02d}",
              "value": f"Closed archive box-{820+i}; 旧档案材料，不表示当前业务状态。"} for i in range(16)]
    return noise[:8] + case["events"] + noise[8:]


def prepare_memory(path, case):
    with Memory(str(path)) as memory:
        for event in source_events(case):
            user = memory.user(**{**scope_for(case), **event.get("scope", {})})
            if event["op"] == "remember":
                user.remember(event["value"], key=event["key"])
            else:
                user.forget(event["key"])


def build_input(path, case, mode):
    start = time.perf_counter()
    with Memory(str(path)) as memory:
        context = asdict(memory.user(**scope_for(case)).session("answer").context(
            query=case["question"], max_facts=8, max_tokens=1200, include_messages=False,
            required_fact_keys=WORKFLOWS[case["workflow"]]["required_fact_keys"] if mode == "required_facts" else []))
    elapsed = (time.perf_counter() - start) * 1000
    messages = workflow_messages(case["workflow"], case["question"], context)
    return {"case_id": case["id"], "workflow": case["workflow"], "mode": mode, "context": context,
            "messages": messages, "context_build_ms": elapsed,
            "input_validation": validate_facts(case["workflow"], facts_from_context(context)),
            "message_bytes": len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))}


def grade_answer(answer, case):
    try:
        proposal = parse_proposal(answer)
    except (ValueError, TypeError, KeyError):
        proposal = None
    protocol = validate_output(case["workflow"], proposal)
    if answer.get("finish_reason") != "stop":
        protocol = {"accepted": False, "errors": ["incomplete_completion"]}
    score, reason = None, "unanswerable_expected"
    if case["expectation"] == "completed":
        expected = case["expected"]
        if not isinstance(proposal, dict) or set(proposal) != set(expected):
            score, reason = 0, "wrong_fields"
        else:
            wrong = [key for key, value in expected.items()
                     if type(proposal[key]) is not type(value) or proposal[key] != value]
            score, reason = int(not wrong and protocol["accepted"]), "wrong_values:" + ",".join(wrong) if wrong else "pass"
    return {"score": score, "reason": reason, "actual": proposal, "protocol": protocol}


def summarize(rows):
    groups = {}
    for mode in MODES:
        selected = [row for row in rows if row["mode"] == mode]
        answerable = [row for row in selected if row["expectation"] == "completed"]
        missing = [row for row in selected if row["expectation"] == "needs_data"]
        accepted = lambda row: row["decision"]["validation"]["accepted"]
        usage = {key: sum(row["answer"]["usage"][key] for row in selected)
                 for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
        groups[mode] = {
            "observations": len(selected), "answerable_observations": len(answerable),
            "raw_correct": sum(row["grade"]["score"] == 1 for row in answerable),
            "validated_correct": sum(row["grade"]["score"] == 1 and accepted(row) for row in answerable),
            "incorrect_accepted": sum(accepted(row) and row["grade"]["score"] != 1 for row in selected),
            "incorrect_blocked": sum(row["grade"]["score"] == 0 and not accepted(row) for row in answerable),
            "correct_but_rejected": sum(row["grade"]["score"] == 1 and not accepted(row) for row in answerable),
            "false_rejection_with_complete_inputs": sum(row["grade"]["score"] == 1 and not accepted(row)
                and row["input_validation"]["accepted"] for row in answerable),
            "missing_data_observations": len(missing),
            "missing_data_rejected": sum(not accepted(row) for row in missing),
            "protocol_valid": sum(row["grade"]["protocol"]["accepted"] for row in selected),
            "usage": usage,
            "context_and_model_median_ms": statistics.median(row["context_and_model_ms"] for row in selected) if selected else None,
            "estimated_cny_without_cache_discount": round((usage["prompt_tokens"]*6.5+usage["completion_tokens"]*27)/1_000_000, 6),
        }
    return {"completed_calls": len(rows), "by_mode": groups,
            "estimated_cny_without_cache_discount": round(sum(v["estimated_cny_without_cache_discount"] for v in groups.values()), 6)}


def validate_suite(suite):
    cases = suite["cases"]
    if len(cases) != 8 or len({c["id"] for c in cases}) != 8:
        raise ValueError("Expected eight unique frozen tasks")
    for case in cases:
        if case["workflow"] not in WORKFLOWS or case["expectation"] not in ("completed", "needs_data"):
            raise ValueError("Unknown workflow or expectation")
        if (case["expectation"] == "needs_data") != (case["expected"] is None):
            raise ValueError("Only needs_data tasks have null answer labels")
        if case["expected"] is not None and not validate_output(case["workflow"], case["expected"])["accepted"]:
            raise ValueError("Answer label violates output contract")
        for event in case["events"]:
            if event["op"] not in ("remember", "forget") or set(event.get("scope", {})) - {"tenant_id", "user_id", "agent_id"}:
                raise ValueError("Invalid source event")


def snapshot_sources(output):
    paths = [Path(__file__).resolve(), ROOT / "examples/kimi_memory.py",
             ROOT / "examples/guarded_workflow_agent.py", ROOT / "examples/workflow_guard.py", ROOT / "evals/run_local.py"]
    paths += sorted((ROOT / "python/src/memweft").glob("*.py"))
    paths += sorted((ROOT / "crates").glob("*/src/**/*.rs"))
    hashes = {}
    for path in paths:
        relative, payload = path.relative_to(ROOT), path.read_bytes()
        target = output / "sources" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        hashes[relative.as_posix()] = hashlib.sha256(payload).hexdigest()
    return hashes


def run(args, client=None):
    suite_bytes = args.suite.read_bytes()
    suite = json.loads(suite_bytes)
    validate_suite(suite)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / "suite.json").write_bytes(suite_bytes)
    dump(output / "metadata.json", {"suite_sha256": hashlib.sha256(suite_bytes).hexdigest(),
        "source_sha256": snapshot_sources(output), "native_sha256": hashlib.sha256(Path(native.__file__).read_bytes()).hexdigest(),
        "started_at": dt.datetime.now(dt.timezone.utc).isoformat(), "model": MODEL, "thinking": "disabled",
        "modes": MODES, "repeats": REPEATS, "max_calls": args.max_calls,
        "reported_token_stop_threshold": args.token_limit, "max_completion_tokens": 192,
        "message_bytes_cap": 16000, "interval_seconds": args.interval, "max_facts": 8, "max_tokens": 1200,
        "automatic_retries": 0, "sampling": "provider default; repeats have no client seed",
        "comparison": "Same structured source events, same application rules and messages framing; only required_fact_keys changes recall. Both proposals are checked using the same guard.",
        "missing_data_policy": "Benchmark observes raw proposals even for incomplete input; normal example preflights and makes zero model calls in that state.",
        "timing": "Prepared context construction plus model HTTP; excludes ingestion, setup, pacing and final validation.",
        "pricing": {"checked": "2026-09-30", "input_cny_per_million": 6.5, "output_cny_per_million": 27,
                    "source": "https://platform.kimi.com/docs/pricing/chat", "cache_discount": False}})
    rows, used_tokens, last_start = [], 0, None
    try:
        inputs = {}
        for case in suite["cases"]:
            path = output / (case["id"] + ".db")
            prepare_memory(path, case)
            for mode in MODES:
                item = build_input(path, case, mode)
                if item["message_bytes"] > 16000:
                    raise ValueError("Frozen messages exceed the byte cap")
                inputs[(case["id"], mode)] = item
                append(output / "inputs.jsonl", item)
        if args.check_memory:
            dump(output / "status.json", {"status": "prepared", "inputs": 16, "calls": 0})
            return {"prepared_inputs": 16, "calls": 0}
        if client is None:
            raise ValueError("Client required")
        for repeat in range(REPEATS):
            for index, case in enumerate(suite["cases"]):
                modes = MODES if (repeat + index) % 2 == 0 else MODES[::-1]
                for mode in modes:
                    if len(rows) >= args.max_calls or used_tokens >= args.token_limit:
                        dump(output / "status.json", {"status": "budget_stopped", "completed_calls": len(rows)})
                        summary = summarize(rows)
                        dump(output / "summary.json", summary)
                        return summary
                    if last_start is not None:
                        time.sleep(max(0, args.interval - (time.monotonic() - last_start)))
                    last_start = time.monotonic()
                    item = inputs[(case["id"], mode)]
                    answer = client.complete(item["messages"], max_tokens=192)
                    usage = answer.get("usage", {})
                    if any(type(usage.get(k)) is not int or usage[k] < 0 for k in ("prompt_tokens", "completion_tokens", "total_tokens")):
                        append(output / "unaccounted_response.jsonl", {"case_id": case["id"], "mode": mode, "repeat": repeat, "answer": answer})
                        raise ValueError("Missing/invalid provider usage; stopping")
                    used_tokens += usage["total_tokens"]
                    row = {"case_id": case["id"], "workflow": case["workflow"], "mode": mode, "repeat": repeat,
                        "expectation": case["expectation"], "expected": case["expected"], "answer": answer,
                        "grade": grade_answer(answer, case), "input_validation": item["input_validation"],
                        "decision": check_answer(case["workflow"], item["context"], answer),
                        "context_build_ms": item["context_build_ms"], "context_and_model_ms": item["context_build_ms"] + answer["latency_ms"]}
                    rows.append(row)
                    append(output / "results.jsonl", row)
                    print(f"[{len(rows):02d}/32] {case['id']}/{mode}/r{repeat+1}: {row['grade']['reason']}; guard={row['decision']['status']}", flush=True)
        summary = summarize(rows)
        dump(output / "summary.json", summary)
        dump(output / "status.json", {"status": "completed", "completed_calls": len(rows)})
        return summary
    except Exception as error:
        dump(output / "status.json", {"status": "failed", "completed_calls": len(rows), "error": str(error)})
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=SUITE)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--check-memory", action="store_true")
    parser.add_argument("--prompt-key", action="store_true")
    parser.add_argument("--interval", type=float, default=21)
    parser.add_argument("--max-calls", type=int, default=32)
    parser.add_argument("--token-limit", type=int, default=60000)
    args = parser.parse_args()
    if not math.isfinite(args.interval) or args.interval < 0 or not 1 <= args.max_calls <= 32 or args.token_limit <= 0:
        parser.error("Invalid pacing or limits")
    if args.output.exists():
        parser.error("Output directory must be new")
    client = None if args.check_memory else KimiClient(read_api_key(args.prompt_key))
    print(json.dumps(run(args, client), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except KimiError as error:
        raise SystemExit(str(error))
