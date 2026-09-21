#!/usr/bin/env python3
import argparse
import json
import os
import tempfile
import time
import uuid
from pathlib import Path
import shutil
import sys

from memweft import Memory
from bench_config import (
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


def run_backend(name, mem, events, iterations, list_limit=None):
    scope = sample_scope()
    append_start = time.perf_counter()
    for idx in range(events):
        mem.append_event(sample_event(scope, f"e-{idx}"))
    append_total = (time.perf_counter() - append_start) * 1000.0

    list_start = time.perf_counter()
    for _ in range(iterations):
        mem.list_events(scope, limit=list_limit)
    list_total = (time.perf_counter() - list_start) * 1000.0

    request = {"scope": scope, "purpose": "planner", "task_type": "generic"}
    build_start = time.perf_counter()
    for _ in range(iterations):
        mem.build_memory_packet(request)
    build_total = (time.perf_counter() - build_start) * 1000.0

    return {
        "backend": name,
        "events": events,
        "iterations": iterations,
        "append_event_ms_total": append_total,
        "append_event_ms_avg": append_total / max(events, 1),
        "list_events_ms_total": list_total,
        "list_events_ms_avg": list_total / max(iterations, 1),
        "build_memory_packet_ms_total": build_total,
        "build_memory_packet_ms_avg": build_total / max(iterations, 1),
    }


def build_bar_chart(results, field, title):
    width = 900
    height = 140 + len(results) * 28
    margin_left = 160
    margin_right = 40
    bar_height = 18
    max_value = max(result[field] for result in results) if results else 1.0
    max_value = max(max_value, 1e-9)
    plot_width = width - margin_left - margin_right

    svg = [
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img">',
        '<rect x="0" y="0" width="100%" height="100%" fill="#ffffff"/>',
        f'<text x="{width / 2}" y="28" text-anchor="middle" font-size="16" font-family="Arial">{title}</text>',
    ]

    for idx, result in enumerate(results):
        value = result[field]
        y = 60 + idx * 28
        bar_width = (value / max_value) * plot_width
        svg.append(
            f'<text x="{margin_left - 10}" y="{y + 13}" text-anchor="end" font-size="12" font-family="Arial">{result["backend"]}</text>'
        )
        svg.append(
            f'<rect x="{margin_left}" y="{y}" width="{bar_width}" height="{bar_height}" fill="#4c78a8"/>'
        )
        svg.append(
            f'<text x="{margin_left + bar_width + 8}" y="{y + 13}" font-size="12" font-family="Arial">{value:.3f} ms</text>'
        )

    svg.append("</svg>")
    return "\n".join(svg)


def write_html(results, output_path):
    chart_append = build_bar_chart(
        results, "append_event_ms_avg", "append_event average latency"
    )
    chart_list = build_bar_chart(
        results, "list_events_ms_avg", "list_events average latency"
    )
    chart_build = build_bar_chart(
        results, "build_memory_packet_ms_avg", "build_memory_packet average latency"
    )
    rows = []
    for result in results:
        rows.append(
            "<tr>"
            f"<td>{result['backend']}</td>"
            f"<td>{result['events']}</td>"
            f"<td>{result['iterations']}</td>"
            f"<td>{result['append_event_ms_avg']:.3f}</td>"
            f"<td>{result['list_events_ms_avg']:.3f}</td>"
            f"<td>{result['build_memory_packet_ms_avg']:.3f}</td>"
            "</tr>"
        )
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>MemWeft Python Benchmarks</title>
  <style>
    body {{
      font-family: Arial, sans-serif;
      margin: 24px;
      color: #222;
    }}
    .chart {{
      margin-bottom: 24px;
    }}
    table {{
      border-collapse: collapse;
      width: 100%;
      font-size: 13px;
    }}
    th, td {{
      border: 1px solid #ddd;
      padding: 8px;
      text-align: left;
    }}
    th {{
      background-color: #f5f5f5;
    }}
  </style>
</head>
<body>
  <h1>MemWeft Python Benchmarks</h1>
  <div class="chart">{chart_append}</div>
  <div class="chart">{chart_list}</div>
  <div class="chart">{chart_build}</div>
  <table>
    <thead>
      <tr>
        <th>backend</th>
        <th>events</th>
        <th>iterations</th>
        <th>append_event avg (ms)</th>
        <th>list_events avg (ms)</th>
        <th>build_memory_packet avg (ms)</th>
      </tr>
    </thead>
    <tbody>
      {"".join(rows)}
    </tbody>
  </table>
</body>
</html>
"""
    output_path.write_text(html, encoding="utf-8")


def main():
    config_arg = find_config_arg(sys.argv[1:])
    load_bench_env(config_arg)
    repo_root = Path(__file__).resolve().parents[2]
    default_output = repo_root / "target" / "python_bench.json"
    default_html = repo_root / "target" / "python_bench.html"
    parser = argparse.ArgumentParser(description="Benchmark MemWeft Python backends.")
    parser.add_argument(
        "--config",
        default=config_arg,
        help="Optional bench config file path.",
    )
    parser.add_argument(
        "--events",
        type=int,
        default=env_int("MEMWEFT_BENCH_EVENTS", 2000),
        help="Events to insert per backend.",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=env_int("MEMWEFT_BENCH_ITERATIONS", 30),
        help="Iterations for list/build.",
    )
    parser.add_argument(
        "--list-limit",
        type=int,
        default=env_int("MEMWEFT_BENCH_LIST_LIMIT", 0) or None,
        help="Optional limit for list_events.",
    )
    parser.add_argument("--output", default=str(default_output), help="Output JSON path.")
    parser.add_argument("--html", default=str(default_html), help="Output HTML report path.")
    args = parser.parse_args()

    results = []
    mem = Memory(in_memory=True)
    results.append(
        run_backend("sqlite-memory", mem, args.events, args.iterations, args.list_limit)
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "memweft.db")
        mem = Memory(path=path)
        results.append(
            run_backend("sqlite-file", mem, args.events, args.iterations, args.list_limit)
        )

    mysql_dsn = env_str("MEMWEFT_BENCH_MYSQL_DSN")
    if mysql_dsn:
        database = env_str("MEMWEFT_BENCH_MYSQL_DB")
        mem = Memory(backend="mysql", dsn=mysql_dsn, database=database)
        results.append(
            run_backend("mysql", mem, args.events, args.iterations, args.list_limit)
        )

    postgres_dsn = env_str("MEMWEFT_BENCH_POSTGRES_DSN")
    if postgres_dsn:
        database = env_str("MEMWEFT_BENCH_POSTGRES_DB")
        mem = Memory(backend="postgres", dsn=postgres_dsn, database=database)
        results.append(
            run_backend("postgres", mem, args.events, args.iterations, args.list_limit)
        )

    output_path = Path(os.path.abspath(args.output))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    prev_path = output_path.with_name("python_bench_prev.json")
    if output_path.exists():
        shutil.copyfile(output_path, prev_path)

    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(
            {
                "events": args.events,
                "iterations": args.iterations,
                "list_limit": args.list_limit,
                "results": results,
            },
            handle,
            indent=2,
        )

    html_path = Path(os.path.abspath(args.html))
    html_path.parent.mkdir(parents=True, exist_ok=True)
    write_html(results, html_path)

    print(f"Wrote {output_path}")
    print(f"Wrote {html_path}")
    if prev_path.exists():
        print(f"Wrote {prev_path}")


if __name__ == "__main__":
    main()
