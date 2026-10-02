#!/usr/bin/env python3
"""Bounded Kimi smoke evaluation: 13 existing memory cases, two modes, 26 calls."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples"))
from kimi_memory import BASE_URLS, MODEL, KimiClient, KimiError, memory_messages, read_api_key
from memweft import Memory
import memweft._core as native
from output_contract import contract_request
from run_local import append, case_schema, dump, score_answer

SUITE = ROOT / "evals/scenarios/common-v3.json"
MODES = ("no_memory", "memory")


def prepare_case(path, case):
    scope = {"user_id": case["id"], "tenant_id": "kimi-smoke", "agent_id": "assistant"}
    with Memory(str(path)) as memory:
        user = memory.user(**scope)
        for index in range(case.get("distractors", 0)):
            user.remember(f"历史项目 archive-{index:03d} 的标签是 inactive。", key=f"a_distractor_{index:03d}")
        for action in case["setup"]:
            action = dict(action)
            target = memory.user(**{**scope, **action.pop("scope", {})})
            op = action.pop("op")
            if op == "add_message":
                target.session("resume-session").add_message(**action)
            elif op == "remember":
                target.remember(**action)
            elif op == "forget":
                target.forget(**action)
            else:
                raise ValueError(f"Unsupported fixture operation: {op}")
    # Close and reopen the native store before retrieving every case.
    with Memory(str(path)) as memory:
        session = memory.user(**scope).session("resume-session")
        if "expected_message_count" in case and len(session.messages()) != case["expected_message_count"]:
            raise AssertionError(f"{case['id']}: message idempotency failed")
        context = asdict(session.context(**{"query": case["prompt"], "max_tokens": 2048,
                                           **case.get("context_options", {})}))
    for forbidden in case.get("forbidden_context", []):
        if forbidden in json.dumps(context, ensure_ascii=False):
            raise AssertionError(f"{case['id']}: forbidden value in context")
    if context["report"]["estimated_tokens"] > context["report"]["max_tokens"]:
        raise AssertionError("Context budget exceeded")
    return context


def grade_result(result, expected, schema):
    grade = score_answer(result["content"], expected, schema)
    if result["finish_reason"] != "stop":
        grade.update(score=0.0, reason="incomplete_completion", protocol_valid=False,
                     protocol_error="incomplete_completion", content_correct=None)
    return grade


def summarize(rows):
    counts = {mode: {"passed": sum(row["score"] == 1 for row in rows if row["mode"] == mode),
                     "total": sum(row["mode"] == mode for row in rows)} for mode in MODES}
    usage = {key: sum(row["usage"].get(key, 0) for row in rows)
             for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
    # Published CN K2.6 rates checked 2026-09-30. Ignore cache discounts;
    # this is a usage-based estimate, not a billing statement or enforced RMB cap.
    estimate = (usage["prompt_tokens"] * 6.5 + usage["completion_tokens"] * 27) / 1_000_000
    return {"calls": len(rows), "by_mode": counts, "usage": usage,
            "estimated_cny_without_cache_discount": round(estimate, 6),
            "pricing_source": "https://platform.kimi.com/docs/pricing/chat",
            "pricing_checked": "2026-09-30",
            "failures": [{"case_id": row["case_id"], "mode": row["mode"], "reason": row["reason"]}
                         for row in rows if row["score"] != 1]}


def run(args, client=None):
    suite = json.loads(SUITE.read_text(encoding="utf-8"))
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    dump(output / "metadata.json", {"model": MODEL, "base_url": args.base_url,
        "suite": suite["version"], "suite_sha256": hashlib.sha256(SUITE.read_bytes()).hexdigest(),
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "client_sha256": hashlib.sha256((ROOT / "examples/kimi_memory.py").read_bytes()).hexdigest(),
        "source_sha256": {relative: hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
                          for relative in ("examples/kimi_memory.py", "examples/handoff_app/kimi_client.py")},
        "native_sha256": hashlib.sha256(Path(native.__file__).read_bytes()).hexdigest(),
        "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "max_calls": len(suite["cases"]) * 2, "max_output_tokens_per_call": 128,
        "thinking": "disabled", "retries": 0, "request_interval_seconds": args.interval,
        "check_memory": args.check_memory,
        "notes": "Synthetic explicit facts; no learning, extraction or production-data access."})
    dump(output / "suite.json", {"cases": suite["cases"], "output_contracts": suite["output_contracts"]})
    rows = []
    try:
        contexts = {case["id"]: prepare_case(output / "memory.db", case) for case in suite["cases"]}
        dump(output / "contexts.json", contexts)
        if args.check_memory:
            dump(output / "status.json", {"status": "memory_checked", "cases": len(contexts), "calls": 0})
            return {"memory_cases_checked": len(contexts), "calls": 0}
        if client is None:
            raise ValueError("A model client is required for live evaluation")
        last_request = None
        for index, case in enumerate(suite["cases"]):
            schema = case_schema(suite, case)
            for mode in (MODES if index % 2 == 0 else MODES[::-1]):
                context = contexts[case["id"]] if mode == "memory" else None
                messages, _ = contract_request(memory_messages(case["prompt"], context["text"] if context else ""),
                                               schema, "prompt")
                tag = f"{case['id']}/{mode}"
                if last_request is not None:
                    time.sleep(max(0, args.interval - (time.monotonic() - last_request)))
                last_request = time.monotonic()
                result = client.complete(messages, max_tokens=128)
                append(output / "calls.jsonl", {"tag": tag, **result})
                row = {"case_id": case["id"], "category": case["category"], "mode": mode,
                       "expected": case["expected"], **result,
                       **grade_result(result, case["expected"], schema)}
                rows.append(row)
                append(output / "results.jsonl", row)
                print(f"[{len(rows):02d}/26] {tag}: {row['reason']} ({result['latency_ms']:.0f} ms)", flush=True)
        summary = summarize(rows)
        if args.base_url != BASE_URLS[0]:
            summary["estimated_cny_without_cache_discount"] = None
        dump(output / "summary.json", summary)
        dump(output / "status.json", {"status": "completed", "calls": len(rows)})
        return summary
    except Exception as error:
        dump(output / "status.json", {"status": "failed", "completed_calls": len(rows),
                                      "error": str(error)})
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt-key", action="store_true")
    parser.add_argument("--base-url", choices=BASE_URLS, default=BASE_URLS[0])
    parser.add_argument("--output", type=Path, default=ROOT / "data/evals" / ("kimi-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S")))
    parser.add_argument("--check-memory", action="store_true", help="No network calls or key needed")
    parser.add_argument("--interval", type=float, default=21,
                        help="Minimum seconds between request starts (default 21 for 3 RPM accounts)")
    args = parser.parse_args()
    if not math.isfinite(args.interval) or args.interval < 0:
        parser.error("--interval must be finite and nonnegative")
    if args.output.exists():
        parser.error("Output directory must be new; previous results are never overwritten")
    client = None if args.check_memory else KimiClient(read_api_key(args.prompt_key), base_url=args.base_url)
    print(json.dumps(run(args, client), ensure_ascii=False, indent=2))
    print(f"Artifacts: {args.output}")


if __name__ == "__main__":
    try:
        main()
    except KimiError as error:
        raise SystemExit(str(error))
