#!/usr/bin/env python3
"""Internal project pilot plus controlled lifecycle rehearsals (10 paid calls)."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples"))
from kimi_memory import KimiClient, KimiError, read_api_key
from kimi_release_agent import ReleaseAgent, collect_evidence, render_plan
from memweft import Memory
import memweft._core as native
from run_local import append, dump

QUESTION = "继续之前的发布准备。请恢复我已保存的目标和交付方式，根据当前证据告诉我下一步；不要发布。"
CASES = [
    {"id": "resume_project", "kind": "internal_project_pilot", "target": "developer_preview",
     "delivery": "downloadable_artifacts", "next_action": "verify_cross_platform_ci"},
    {"id": "change_target", "kind": "controlled_rehearsal", "target": "bounded_production",
     "delivery": "downloadable_artifacts", "next_action": "run_business_pilot"},
    {"id": "forget_delivery", "kind": "controlled_rehearsal", "target": "developer_preview",
     "delivery": None, "next_action": "clarify_decisions"},
    {"id": "other_user", "kind": "controlled_rehearsal", "target": None,
     "delivery": None, "next_action": "clarify_decisions"},
    {"id": "stale_ci_note", "kind": "controlled_rehearsal", "target": "developer_preview",
     "delivery": "downloadable_artifacts", "next_action": "verify_cross_platform_ci"},
]


def prepare_case(output, repo, case):
    agent = ReleaseAgent(output / (case["id"] + ".db"), repo)
    writer = ReleaseAgent(agent.db, repo, user_id="another-maintainer") if case["id"] == "other_user" else agent
    writer.remember("target", "developer_preview")
    writer.remember("delivery", "downloadable_artifacts")
    if case["id"] == "change_target":
        agent.remember("target", "bounded_production")
    if case["id"] == "forget_delivery":
        agent.forget("delivery")
        # Old conversation copies must not resurrect a forgotten decision.
        with Memory(str(agent.db)) as memory:
            memory.user(**agent.scope).session("previous").add_message(
                "assistant", "之前的交付方式是 downloadable_artifacts。", event_id="old-answer")
    if case["id"] == "stale_ci_note":
        with Memory(str(agent.db)) as memory:
            memory.user(**agent.scope).remember("上周 CI 已全部通过，可以发布。", key="release.ci_note")
    return agent


def grade(result, case):
    expected = {key: case[key] for key in ("target", "delivery", "next_action")}
    expected["ready"] = False
    proposal = result["proposal"]
    actual = None if proposal is None else {key: proposal[key] for key in expected}
    passed = result["validation_error"] is None and actual == expected
    return {"expected": expected, "actual": actual, "task_passed": passed}


def run(output, repo, client=None, *, interval=21):
    output.mkdir(parents=True, exist_ok=False)
    evidence = collect_evidence(repo)
    dump(output / "evidence.json", evidence)
    dump(output / "cases.json", CASES)
    dump(output / "metadata.json", {"started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "model": "kimi-k2.6", "thinking": "disabled", "max_calls": 10,
        "max_completion_tokens": 256, "request_interval_seconds": interval,
        "native_sha256": hashlib.sha256(Path(native.__file__).read_bytes()).hexdigest(),
        "source_sha256": {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in (Path(__file__), ROOT / "examples/kimi_release_agent.py", ROOT / "examples/kimi_memory.py")},
        "scope": "One internal project case plus four controlled lifecycle rehearsals; no customer data or publishing."})
    rows, last_request = [], None
    try:
        agents = {case["id"]: prepare_case(output, repo, case) for case in CASES}
        if client is None:
            dump(output / "status.json", {"status": "prepared", "calls": 0})
            return {"prepared": len(agents), "calls": 0}
        for index, case in enumerate(CASES):
            modes = ("no_memory", "memory") if index % 2 == 0 else ("memory", "no_memory")
            for mode in modes:
                if last_request is not None:
                    time.sleep(max(0, interval - (time.monotonic() - last_request)))
                last_request = time.monotonic()
                result = agents[case["id"]].plan(client, QUESTION, session="next-session",
                    use_memory=mode == "memory", evidence=evidence)
                row = {"case_id": case["id"], "kind": case["kind"], "mode": mode,
                       **grade(result, case), **result}
                rows.append(row)
                append(output / "results.jsonl", row)
                print(f"[{len(rows):02d}/10] {case['id']}/{mode}: task_passed={row['task_passed']}", flush=True)
                if case["id"] == "resume_project" and mode == "memory" and result["validation_error"] is None:
                    (output / "next-steps.md").write_text(render_plan(result), encoding="utf-8")
        usage = {key: sum(row["answer"]["usage"].get(key, 0) for row in rows)
                 for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
        summary = {"calls": len(rows), "usage": usage,
            "estimated_cny_without_cache_discount": round((usage["prompt_tokens"] * 6.5 + usage["completion_tokens"] * 27) / 1_000_000, 6),
            "price_checked": "2026-09-30", "pricing_source": "https://platform.kimi.com/docs/pricing/chat",
            "by_mode": {mode: {"passed": sum(row["task_passed"] for row in rows if row["mode"] == mode),
                               "total": sum(row["mode"] == mode for row in rows)} for mode in ("no_memory", "memory")},
            "invalid_plans": sum(row["validation_error"] is not None for row in rows)}
        dump(output / "summary.json", summary)
        dump(output / "status.json", {"status": "completed", "calls": len(rows)})
        return summary
    except Exception as error:
        dump(output / "status.json", {"status": "failed", "completed_calls": len(rows), "error": str(error)})
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, default=ROOT / "data/evals" / ("kimi-release-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S")))
    parser.add_argument("--prompt-key", action="store_true")
    parser.add_argument("--check-memory", action="store_true")
    parser.add_argument("--interval", type=float, default=21)
    args = parser.parse_args()
    if not math.isfinite(args.interval) or args.interval < 0:
        parser.error("--interval must be finite and nonnegative")
    if args.output.exists():
        parser.error("Output must be a new directory")
    client = None if args.check_memory else KimiClient(read_api_key(args.prompt_key))
    print(json.dumps(run(args.output.resolve(), args.repo, client, interval=args.interval), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except KimiError as error:
        raise SystemExit(str(error))
