"""Standalone, offline project handoff application using only the public SDK."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import re

from memweft import Memory

PREFIX = "handoff."


def identifier(value):
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", value) is None:
        raise argparse.ArgumentTypeError("Use 1–80 letters, digits, dots, underscores or hyphens")
    return value


def nonnegative(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("Must be nonnegative")
    return number


def now():
    return datetime.now(timezone.utc).isoformat()


def facts_from(records):
    facts = []
    for record in records:
        key = record.get("fact_key", "")
        if not key.startswith(PREFIX):
            continue
        value = record.get("value")
        if (not isinstance(value, dict)
                or any(not isinstance(value.get(field), str)
                       for field in ("content", "source", "recorded_at"))):
            raise ValueError("Invalid handoff fact; inspect its source before continuing")
        facts.append({"key": key[len(PREFIX):], **{field: value[field]
                      for field in ("content", "source", "recorded_at")}})
    return facts


def requirements_from(report):
    required = report["requirements"]
    return {**{field: [key.removeprefix(PREFIX) for key in required[field]]
               for field in ("requested", "included", "missing", "excluded")},
            "complete": required["complete"]}


def quote_block(text):
    # A quoted JSON string stays on one line even if a stored value has line
    # breaks; dynamically sized fences also keep arbitrary backticks as data.
    encoded = json.dumps(text, ensure_ascii=False)
    longest = max((len(run) for run in re.findall(r"`+", encoded)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{encoded}\n{fence}"


def render(snapshot):
    requirement = snapshot["requirements"]
    status = "记录已恢复，等待使用者复核" if requirement["complete"] else "交接不完整，需要补充或扩大上下文预算"
    parts = [f"# 项目交接：{snapshot['project']}", "", f"状态：{status}。", "",
             "交接任务：", quote_block(snapshot["question"]), "",
             "这份交接由已保存的记录直接生成，未调用模型。来源说明由记录者提供，尚未独立核实。", "",
             "## 当前记录", ""]
    for fact in snapshot["facts"]:
        parts += [f"### {fact['key']}", "", quote_block(fact["content"]), "",
                  "来源：", quote_block(fact["source"]), ""]
    if not snapshot["facts"]:
        parts += ["没有召回记录。", ""]
    parts += ["## 完整性", "",
              "必需记录：" + (", ".join(requirement["requested"]) or "未声明"), "",
              "缺失记录：" + (", ".join(requirement["missing"]) or "无"), "",
              "因预算排除：" + (", ".join(requirement["excluded"]) or "无"), "",
              "完整性只覆盖显式声明的必需记录，不代表所有需求已覆盖或记录属实。", "",
              "## 使用者复核", "", "请确认这些记录是否准确，以及是否足够让下一次会话继续任务。",
              "当前尚未记录人工评估。", ""]
    return "\n".join(parts)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/handoff.db"))
    parser.add_argument("--project", type=identifier, required=True)
    parser.add_argument("--user", type=identifier, default="maintainer")
    commands = parser.add_subparsers(dest="command", required=True)
    remember = commands.add_parser("remember", help="Save or replace an explicit project fact")
    remember.add_argument("key", type=identifier)
    remember.add_argument("value")
    remember.add_argument("--source", required=True, help="Where the explicit fact came from")
    forget = commands.add_parser("forget", help="Remove a fact from future handoffs")
    forget.add_argument("key", type=identifier)
    commands.add_parser("list", help="List saved facts in this project/user scope")
    resume = commands.add_parser("resume", help="Restore a task in a new process and export a handoff")
    resume.add_argument("question")
    resume.add_argument("--require", type=identifier, action="append", default=[])
    resume.add_argument("--max-facts", type=nonnegative, default=12)
    resume.add_argument("--max-tokens", type=nonnegative, default=1200)
    resume.add_argument("--out", type=Path, required=True, help="New output directory; never overwritten")
    args = parser.parse_args(argv)
    # Reject existing outputs before opening the database, including broken links.
    if args.command == "resume" and (args.out.exists() or args.out.is_symlink()):
        parser.error("Output already exists; choose a new --out directory")
    if args.command == "remember" and (not args.value.strip() or not args.source.strip()):
        parser.error("Fact content and source must be nonempty")
    if args.command == "resume" and not args.question.strip():
        parser.error("A nonempty task question is required")
    args.db.parent.mkdir(parents=True, exist_ok=True)
    with Memory(str(args.db)) as memory:
        user = memory.user(args.user, tenant_id="handoff:" + args.project, agent_id="project-handoff")
        if args.command == "remember":
            user.remember({"content": args.value, "source": args.source, "recorded_at": now()},
                          key=PREFIX + args.key)
            result = {"saved": args.key}
        elif args.command == "forget":
            result = {"key": args.key, "forgotten": user.forget(PREFIX + args.key)}
        elif args.command == "list":
            result = {"facts": facts_from(user.memories())}
        else:
            context = user.session("handoff").context(
                query=args.question, required_fact_keys=[PREFIX + key for key in dict.fromkeys(args.require)],
                max_facts=args.max_facts, max_tokens=args.max_tokens, include_messages=False)
            requirements = requirements_from(context.explain())
            result = {"schema_version": 1, "captured_at": now(), "project": args.project, "user": args.user,
                      "question": args.question, "facts": facts_from(context.memories),
                      "requirements": requirements, "context_report": context.explain(),
                      "status": "ready_for_review" if requirements["complete"] else "incomplete",
                      "model_calls": 0, "human_review_status": "pending", "sdk_version": version("memweft"),
                      "app_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    if args.command == "resume":
        args.out.mkdir(parents=True)  # Existing directories remain a hard error, even after a race.
        with (args.out / "snapshot.json").open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        with (args.out / "handoff.md").open("x", encoding="utf-8") as stream:
            stream.write(render(result))
    # Keep the JSON transport valid even when the caller pipes through a
    # non-UTF-8 terminal; the exported files above remain readable UTF-8.
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 2 if result.get("status") == "incomplete" else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as error:
        raise SystemExit(str(error)) from None
