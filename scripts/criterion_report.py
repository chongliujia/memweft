#!/usr/bin/env python3
import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class Point:
    backend: str
    label: str
    events: Optional[int]
    facts: Optional[int]
    episodes: Optional[int]
    procedures: Optional[int]
    insights: Optional[int]
    mean_us: float
    median_us: float
    std_dev_us: float
    base_mean_us: Optional[float]
    change_pct: Optional[float]


LABEL_RE = re.compile(
    r"events(?P<events>\d+)_facts(?P<facts>\d+)_episodes(?P<episodes>\d+)_procedures(?P<procedures>\d+)_insights(?P<insights>\d+)"
)


def parse_label(label: str):
    match = LABEL_RE.search(label)
    if not match:
        return (None, None, None, None, None)
    return (
        int(match.group("events")),
        int(match.group("facts")),
        int(match.group("episodes")),
        int(match.group("procedures")),
        int(match.group("insights")),
    )


def ns_to_us(value: float) -> float:
    return value / 1000.0


def load_groups(input_dir: Path) -> dict[str, list[Point]]:
    groups: dict[str, list[Point]] = {}
    for estimate_path in input_dir.glob("**/new/estimates.json"):
        try:
            rel = estimate_path.relative_to(input_dir)
        except ValueError:
            continue
        parts = rel.parts
        if len(parts) < 4:
            continue
        if parts[0] in ("memory", "sqlite", "mysql", "postgres"):
            group_name = input_dir.name
            backend = parts[0]
            label = parts[1]
        else:
            group_name = parts[0]
            backend = parts[1]
            label = parts[2]
        with estimate_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        events, facts, episodes, procedures, insights = parse_label(label)
        base_mean_us = None
        change_pct = None
        base_path = estimate_path.parent.parent / "base" / "estimates.json"
        if base_path.exists():
            with base_path.open("r", encoding="utf-8") as handle:
                base_data = json.load(handle)
            base_mean_us = ns_to_us(base_data["mean"]["point_estimate"])
            if base_mean_us != 0:
                point_mean_us = ns_to_us(data["mean"]["point_estimate"])
                change_pct = (point_mean_us - base_mean_us) / base_mean_us * 100.0
            else:
                change_pct = None

        point = Point(
            backend=backend,
            label=label,
            events=events,
            facts=facts,
            episodes=episodes,
            procedures=procedures,
            insights=insights,
            mean_us=ns_to_us(data["mean"]["point_estimate"]),
            median_us=ns_to_us(data["median"]["point_estimate"]),
            std_dev_us=ns_to_us(data["std_dev"]["point_estimate"]),
            base_mean_us=base_mean_us,
            change_pct=change_pct,
        )
        groups.setdefault(group_name, []).append(point)
    return groups


def choose_x_axis(points: list[Point]):
    def metric_values(getter):
        return [getter(point) for point in points if getter(point) is not None]

    metrics = [
        ("events count", "events"),
        ("facts count", "facts"),
        ("episodes count", "episodes"),
        ("procedures count", "procedures"),
        ("insights count", "insights"),
    ]

    for label, key in metrics:
        values = metric_values(lambda p, key=key: getattr(p, key))
        if values and len(set(values)) > 1:
            x_values = sorted(set(values))
            return label, x_values, min(x_values), max(x_values), False, key

    x_values = list(range(len(points)))
    return "dataset index", x_values, 0, max(x_values) if x_values else 1, True, "index"


def format_count(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M".rstrip("0").rstrip(".")
    if value >= 1_000:
        return f"{value / 1_000:.1f}k".rstrip("0").rstrip(".")
    return str(value)


def build_svg(points: list[Point], title: str) -> str:
    width = 960
    height = 420
    margin = 60
    plot_width = width - margin * 2
    plot_height = height - margin * 2

    backends = sorted({point.backend for point in points})
    colors = {
        "memory": "#1f77b4",
        "sqlite": "#ff7f0e",
        "mysql": "#2ca02c",
        "postgres": "#d62728",
    }

    x_label, x_values, x_min, x_max, use_index, x_key = choose_x_axis(points)

    max_y = max(point.mean_us for point in points) if points else 1.0
    max_y *= 1.1

    def x_scale(value: int, index: int) -> float:
        if use_index:
            x_pos = index
            x_range = max(len(points) - 1, 1)
            return margin + (x_pos / x_range) * plot_width
        return margin + ((value - x_min) / max(x_max - x_min, 1)) * plot_width

    def y_scale(value: float) -> float:
        return margin + plot_height - (value / max_y) * plot_height

    svg = [
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img">',
        '<rect x="0" y="0" width="100%" height="100%" fill="#ffffff"/>',
        f'<line x1="{margin}" y1="{margin}" x2="{margin}" y2="{margin + plot_height}" stroke="#333"/>',
        f'<line x1="{margin}" y1="{margin + plot_height}" x2="{margin + plot_width}" y2="{margin + plot_height}" stroke="#333"/>',
        f'<text x="{width / 2}" y="30" text-anchor="middle" font-size="16" font-family="Arial">{title} latency (microseconds)</text>',
        f'<text x="{width / 2}" y="{height - 10}" text-anchor="middle" font-size="12" font-family="Arial">{x_label}</text>',
        f'<text x="20" y="{height / 2}" text-anchor="middle" font-size="12" font-family="Arial" transform="rotate(-90 20 {height / 2})">mean latency (us)</text>',
    ]

    # Y axis ticks
    ticks = 5
    for i in range(ticks + 1):
        value = max_y * (i / ticks)
        y = y_scale(value)
        svg.append(f'<line x1="{margin - 4}" y1="{y}" x2="{margin}" y2="{y}" stroke="#333"/>')
        svg.append(
            f'<text x="{margin - 8}" y="{y + 4}" text-anchor="end" font-size="10" font-family="Arial">{value:.0f}</text>'
        )

    # X axis ticks
    max_labels = 8
    if not use_index:
        step = max(1, len(x_values) // max_labels)
        for idx, value in enumerate(x_values):
            if idx % step != 0 and idx != len(x_values) - 1:
                continue
            x = x_scale(value, 0)
            svg.append(
                f'<line x1="{x}" y1="{margin + plot_height}" x2="{x}" y2="{margin + plot_height + 4}" stroke="#333"/>'
            )
            label = format_count(value)
            rotation = " rotate(-35 {})".format(x) if len(x_values) > 6 else ""
            svg.append(
                f'<text x="{x}" y="{margin + plot_height + 18}" text-anchor="middle" font-size="10" font-family="Arial" transform="translate(0,0){rotation}">{label}</text>'
            )
    else:
        step = max(1, len(points) // max_labels)
        for i, point in enumerate(points):
            if i % step != 0 and i != len(points) - 1:
                continue
            x = x_scale(i, i)
            svg.append(
                f'<line x1="{x}" y1="{margin + plot_height}" x2="{x}" y2="{margin + plot_height + 4}" stroke="#333"/>'
            )
            rotation = " rotate(-35 {})".format(x) if len(points) > 6 else ""
            svg.append(
                f'<text x="{x}" y="{margin + plot_height + 18}" text-anchor="middle" font-size="10" font-family="Arial" transform="translate(0,0){rotation}">{point.label}</text>'
            )

    # Series lines and points
    def point_x(point: Point, index: int) -> int:
        if use_index or x_key == "index":
            return index
        value = getattr(point, x_key)
        if value is None:
            return index
        return value

    for backend in backends:
        series = [point for point in points if point.backend == backend]
        series.sort(key=lambda p: point_x(p, 0))
        poly_points = []
        for index, point in enumerate(series):
            x_value = point_x(point, index)
            x = x_scale(x_value, index)
            y = y_scale(point.mean_us)
            poly_points.append(f"{x},{y}")
        color = colors.get(backend, "#333333")
        svg.append(
            f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{" ".join(poly_points)}"/>'
        )
        for index, point in enumerate(series):
            x_value = point_x(point, index)
            x = x_scale(x_value, index)
            y = y_scale(point.mean_us)
            svg.append(f'<circle cx="{x}" cy="{y}" r="4" fill="{color}">')
            tooltip = f"{backend} {point.label} mean={point.mean_us:.2f}us median={point.median_us:.2f}us std={point.std_dev_us:.2f}us"
            svg.append(f"<title>{tooltip}</title></circle>")

    # Legend
    legend_x = margin + 10
    legend_y = margin - 30
    for idx, backend in enumerate(backends):
        color = colors.get(backend, "#333333")
        x = legend_x + idx * 120
        svg.append(f'<rect x="{x}" y="{legend_y}" width="12" height="12" fill="{color}"/>')
        svg.append(
            f'<text x="{x + 18}" y="{legend_y + 10}" font-size="12" font-family="Arial">{backend}</text>'
        )

    svg.append("</svg>")
    return "\n".join(svg)


def format_optional(value: Optional[float], fmt: str = "{:.2f}") -> str:
    if value is None:
        return "-"
    return fmt.format(value)


def format_change(value: Optional[float]) -> str:
    if value is None:
        return "-"
    return f"{value:+.2f}%"


def build_table(points: list[Point]) -> str:
    rows = []
    for point in sorted(points, key=lambda p: (p.backend, p.events or 0)):
        rows.append(
            "<tr>"
            f"<td>{point.backend}</td>"
            f"<td>{point.label}</td>"
            f"<td>{point.events or '-'}</td>"
            f"<td>{point.facts or '-'}</td>"
            f"<td>{point.episodes or '-'}</td>"
            f"<td>{point.procedures or '-'}</td>"
            f"<td>{point.insights or '-'}</td>"
            f"<td>{point.mean_us:.2f}</td>"
            f"<td>{point.median_us:.2f}</td>"
            f"<td>{point.std_dev_us:.2f}</td>"
            f"<td>{format_optional(point.base_mean_us)}</td>"
            f"<td>{format_change(point.change_pct)}</td>"
            "</tr>"
        )
    return "\n".join(rows)


def render_html(
    groups: dict[str, list[Point]],
    title: str,
    python_bench: Optional[dict],
    python_load: Optional[dict],
    python_soak: Optional[dict],
) -> str:
    sections = []
    for name in sorted(groups.keys()):
        points = groups[name]
        svg = build_svg(points, name)
        table_rows = build_table(points)
        sections.append(
            f"""
            <section>
              <h2>{name}</h2>
              <div class="chart">{svg}</div>
              <table>
                <thead>
                  <tr>
                    <th>backend</th>
                    <th>dataset</th>
                    <th>events</th>
                    <th>facts</th>
                    <th>episodes</th>
                    <th>procedures</th>
                    <th>insights</th>
                  <th>mean (us)</th>
                  <th>median (us)</th>
                  <th>std dev (us)</th>
                  <th>base mean (us)</th>
                  <th>change</th>
                </tr>
                </thead>
                <tbody>
                  {table_rows}
                </tbody>
              </table>
            </section>
            """
        )

    if python_bench:
        python_section = render_python_section(python_bench)
        sections.append(python_section)
    if python_load:
        load_section = render_python_load_section(python_load)
        sections.append(load_section)
    if python_soak:
        soak_section = render_python_soak_section(python_soak)
        sections.append(soak_section)
    sections_html = "\n".join(sections)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{title}</title>
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
  <h1>{title}</h1>
  <p>Source: {title}. Values are microseconds (mean, median, std dev).</p>
  {sections_html}
</body>
</html>
"""


def build_bar_svg(rows: list[dict], field: str, title: str) -> str:
    width = 900
    height = 140 + len(rows) * 28
    margin_left = 160
    margin_right = 40
    bar_height = 18
    max_value = max(row.get(field, 0.0) for row in rows) if rows else 1.0
    max_value = max(max_value, 1e-9)
    plot_width = width - margin_left - margin_right

    svg = [
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img">',
        '<rect x="0" y="0" width="100%" height="100%" fill="#ffffff"/>',
        f'<text x="{width / 2}" y="28" text-anchor="middle" font-size="16" font-family="Arial">{title}</text>',
    ]

    for idx, row in enumerate(rows):
        value = row.get(field, 0.0)
        y = 60 + idx * 28
        bar_width = (value / max_value) * plot_width
        svg.append(
            f'<text x="{margin_left - 10}" y="{y + 13}" text-anchor="end" font-size="12" font-family="Arial">{row.get("backend")}</text>'
        )
        svg.append(
            f'<rect x="{margin_left}" y="{y}" width="{bar_width}" height="{bar_height}" fill="#4c78a8"/>'
        )
        svg.append(
            f'<text x="{margin_left + bar_width + 8}" y="{y + 13}" font-size="12" font-family="Arial">{value:.3f} ms</text>'
        )

    svg.append("</svg>")
    return "\n".join(svg)


def render_python_section(python_bench: dict) -> str:
    results = python_bench["results"]
    chart_append = build_bar_svg(results, "append_event_ms_avg", "append_event average latency")
    chart_list = build_bar_svg(results, "list_events_ms_avg", "list_events average latency")
    chart_build = build_bar_svg(results, "build_memory_packet_ms_avg", "build_memory_packet average latency")

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
            f"<td>{result.get('append_event_ms_avg_change', '-') }</td>"
            f"<td>{result.get('list_events_ms_avg_change', '-') }</td>"
            f"<td>{result.get('build_memory_packet_ms_avg_change', '-') }</td>"
            "</tr>"
        )

    return f"""
    <section>
      <h2>python_bench</h2>
      <p>events={python_bench['events']} iterations={python_bench['iterations']}</p>
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
            <th>append Δ</th>
            <th>list Δ</th>
            <th>build Δ</th>
          </tr>
        </thead>
        <tbody>
          {''.join(rows)}
        </tbody>
      </table>
    </section>
    """


def render_python_load_section(python_load: dict) -> str:
    rows = []
    for result in python_load["results"]:
        rows.append(
            "<tr>"
            f"<td>{result['backend']}</td>"
            f"<td>{result['ops_per_sec']:.2f}</td>"
            f"<td>{result.get('ops_per_sec_change', '-')}</td>"
            f"<td>{result['append']['avg_ms']:.3f}</td>"
            f"<td>{result['append']['p95_ms']:.3f}</td>"
            f"<td>{result['list']['avg_ms']:.3f}</td>"
            f"<td>{result['list']['p95_ms']:.3f}</td>"
            f"<td>{result['build']['avg_ms']:.3f}</td>"
            f"<td>{result['build']['p95_ms']:.3f}</td>"
            "</tr>"
        )
    return f"""
    <section>
      <h2>python_load</h2>
      <p>duration={python_load['duration_s']}s concurrency={python_load['concurrency']} seed_events={python_load['seed_events']}</p>
      <table>
        <thead>
          <tr>
            <th>backend</th>
            <th>ops/sec</th>
            <th>ops Δ</th>
            <th>append avg (ms)</th>
            <th>append p95 (ms)</th>
            <th>list avg (ms)</th>
            <th>list p95 (ms)</th>
            <th>build avg (ms)</th>
            <th>build p95 (ms)</th>
          </tr>
        </thead>
        <tbody>
          {''.join(rows)}
        </tbody>
      </table>
    </section>
    """


def build_soak_svg(series: list[dict], width: int = 900, height: int = 260) -> str:
    margin = 50
    plot_width = width - margin * 2
    plot_height = height - margin * 2
    max_x = max((point["t_s"] for point in series), default=1)
    max_y = max((point["ops_per_sec"] for point in series), default=1.0) * 1.1

    def x_scale(x):
        return margin + (x / max_x) * plot_width

    def y_scale(y):
        return margin + plot_height - (y / max_y) * plot_height

    svg = [
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img">',
        '<rect x="0" y="0" width="100%" height="100%" fill="#ffffff"/>',
        f'<line x1="{margin}" y1="{margin}" x2="{margin}" y2="{margin + plot_height}" stroke="#333"/>',
        f'<line x1="{margin}" y1="{margin + plot_height}" x2="{margin + plot_width}" y2="{margin + plot_height}" stroke="#333"/>',
        f'<text x="{width / 2}" y="24" text-anchor="middle" font-size="14" font-family="Arial">ops/sec over time</text>',
    ]

    poly = []
    for point in series:
        poly.append(f"{x_scale(point['t_s'])},{y_scale(point['ops_per_sec'])}")
    svg.append(
        f'<polyline fill="none" stroke="#2ca02c" stroke-width="2" points="{" ".join(poly)}"/>'
    )
    svg.append("</svg>")
    return "\n".join(svg)


def render_python_soak_section(python_soak: dict) -> str:
    sections = []
    for result in python_soak["results"]:
        series = result["series"]
        chart = build_soak_svg(series) if series else ""
        latest = series[-1] if series else {}
        avg_ops = format_optional(result.get("ops_per_sec_avg"))
        avg_change = result.get("ops_per_sec_avg_change", "-")
        sections.append(
            f"""
            <section>
              <h3>{result['backend']}</h3>
              {chart}
              <p>last interval ops/sec={latest.get('ops_per_sec', 0):.2f}</p>
              <p>avg ops/sec={avg_ops} (Δ {avg_change})</p>
            </section>
            """
        )
    return f"""
    <section>
      <h2>python_soak</h2>
      <p>duration={python_soak['duration_s']}s interval={python_soak['interval_s']}s seed_events={python_soak['seed_events']}</p>
      {''.join(sections)}
    </section>
    """


def load_python_bench(path: Path, prev_path: Optional[Path]) -> Optional[dict]:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    prev = None
    if prev_path and prev_path.exists():
        with prev_path.open("r", encoding="utf-8") as handle:
            prev = json.load(handle)
    if prev:
        prev_map = {row["backend"]: row for row in prev.get("results", [])}
        for row in data["results"]:
            prev_row = prev_map.get(row["backend"])
            if not prev_row:
                continue
            row["append_event_ms_avg_change"] = format_change(
                pct_change(row["append_event_ms_avg"], prev_row["append_event_ms_avg"])
            )
            row["list_events_ms_avg_change"] = format_change(
                pct_change(row["list_events_ms_avg"], prev_row["list_events_ms_avg"])
            )
            row["build_memory_packet_ms_avg_change"] = format_change(
                pct_change(
                    row["build_memory_packet_ms_avg"],
                    prev_row["build_memory_packet_ms_avg"],
                )
            )
    return data


def load_python_load(path: Path, prev_path: Optional[Path]) -> Optional[dict]:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    prev = None
    if prev_path and prev_path.exists():
        with prev_path.open("r", encoding="utf-8") as handle:
            prev = json.load(handle)
    if prev:
        prev_map = {row["backend"]: row for row in prev.get("results", [])}
        for row in data["results"]:
            prev_row = prev_map.get(row["backend"])
            if not prev_row:
                continue
            row["ops_per_sec_change"] = format_change(
                pct_change(row["ops_per_sec"], prev_row.get("ops_per_sec", 0.0))
            )
    return data


def average_ops_per_sec(series: list[dict]) -> Optional[float]:
    if not series:
        return None
    return sum(point.get("ops_per_sec", 0.0) for point in series) / len(series)


def load_python_soak(path: Path, prev_path: Optional[Path]) -> Optional[dict]:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    prev = None
    if prev_path and prev_path.exists():
        with prev_path.open("r", encoding="utf-8") as handle:
            prev = json.load(handle)
    prev_map = None
    if prev:
        prev_map = {row["backend"]: row for row in prev.get("results", [])}
    for row in data.get("results", []):
        avg_ops = average_ops_per_sec(row.get("series", []))
        row["ops_per_sec_avg"] = avg_ops
        if prev_map and avg_ops is not None:
            prev_row = prev_map.get(row["backend"])
            if prev_row:
                prev_avg = average_ops_per_sec(prev_row.get("series", []))
                if prev_avg is not None:
                    row["ops_per_sec_avg_change"] = format_change(
                        pct_change(avg_ops, prev_avg)
                    )
    return data


def pct_change(current: float, base: float) -> Optional[float]:
    if base == 0:
        return None
    return (current - base) / base * 100.0


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate HTML chart from Criterion output.")
    parser.add_argument(
        "--input",
        default="target/criterion",
        help="Path to Criterion benchmark root directory.",
    )
    parser.add_argument(
        "--output",
        default="target/criterion/summary.html",
        help="Output HTML file path.",
    )
    parser.add_argument(
        "--python",
        default="target/python_bench.json",
        help="Optional Python benchmark JSON path.",
    )
    parser.add_argument(
        "--python-prev",
        default="target/python_bench_prev.json",
        help="Optional previous Python benchmark JSON path.",
    )
    parser.add_argument(
        "--python-load",
        default="target/python_load.json",
        help="Optional Python load JSON path.",
    )
    parser.add_argument(
        "--python-load-prev",
        default="target/python_load_prev.json",
        help="Optional previous Python load JSON path.",
    )
    parser.add_argument(
        "--python-soak",
        default="target/python_soak.json",
        help="Optional Python soak JSON path.",
    )
    parser.add_argument(
        "--python-soak-prev",
        default="target/python_soak_prev.json",
        help="Optional previous Python soak JSON path.",
    )
    parser.add_argument("--title", default="MemWeft build_memory_packet benchmark")
    args = parser.parse_args()

    input_dir = Path(args.input)
    if not input_dir.exists():
        raise SystemExit(f"Input directory not found: {input_dir}")

    groups = load_groups(input_dir)
    if not groups:
        raise SystemExit(f"No estimates.json found under {input_dir}")

    python_path = Path(args.python)
    python_prev_path = Path(args.python_prev) if args.python_prev else None
    python_bench = load_python_bench(python_path, python_prev_path)

    python_load_path = Path(args.python_load)
    python_load_prev_path = Path(args.python_load_prev) if args.python_load_prev else None
    python_load = load_python_load(python_load_path, python_load_prev_path)

    python_soak_path = Path(args.python_soak)
    python_soak_prev_path = Path(args.python_soak_prev) if args.python_soak_prev else None
    python_soak = load_python_soak(python_soak_path, python_soak_prev_path)

    html = render_html(groups, args.title, python_bench, python_load, python_soak)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    print(f"Wrote {output_path}")


if __name__ == "__main__":
    main()
