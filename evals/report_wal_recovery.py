#!/usr/bin/env python3
"""Verify raw WAL recovery evidence before generating a paired report."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3

from benchmark_bounded import dump, sha
from benchmark_pressure import stats
from benchmark_wal_recovery import QUERIES, slots


def read(path):
    return json.loads(path.read_text())


def validate_client(client, config):
    kind, index = client["kind"], client["index"]
    assert kind in ("reader", "writer")
    total_rate = config["read_rate"] if kind == "reader" else config["write_rate"]
    count = config["readers"] if kind == "reader" else config["writers"]
    assert 0 <= index < count
    rate, stagger = total_rate / count, index / total_rate
    offered = slots(config["seconds"], rate, stagger)
    samples = client["samples"]
    assert client["offered_slots"] == offered
    assert client["completed"] == len(samples) > 0
    assert client["missed_slots"] == offered - len(samples) >= 0
    assert client["latency"] == stats([s["ms"] for s in samples])
    assert client["scheduled_latency"] == stats([s["ms"] + s["lag_ms"] for s in samples])
    previous = -1
    previous_at = -1
    for n, sample in enumerate(samples):
        assert previous < sample["slot"] < offered
        assert previous_at < sample["at_s"] < config["seconds"]
        scheduled = stagger + sample["slot"] / rate
        assert sample["at_s"] >= scheduled - 0.000001
        assert sample["ms"] >= 0 and sample["lag_ms"] >= 0
        assert abs(sample["lag_ms"] - (sample["at_s"] - scheduled) * 1000) < 0.001
        if kind == "writer":
            assert sample["revision"] == n + 2
        else:
            assert sample["query"] == QUERIES[(n + index) % len(QUERIES)][0]
        previous, previous_at = sample["slot"], sample["at_s"]
    if kind == "writer":
        assert client["last_revision"] == len(samples) + 1


def validate_recovery(monitor, clients, result):
    recovery_start, measured_end = result["recovery_start_s"], result["measured_end_s"]
    assert measured_end - recovery_start >= result["config"]["recovery_seconds"]
    assert monitor and monitor[-1]["phase"] == "recovery"
    assert abs(monitor[-1]["at_s"] - measured_end) < 0.1
    last_at, idle = -1, False
    for row in monitor:
        assert row["at_s"] > last_at
        assert isinstance(row["wal_bytes"], int) and row["wal_bytes"] >= 0
        assert row["phase"] in ("load", "recovery")
        if row["phase"] == "recovery":
            idle = True
            assert row["at_s"] >= recovery_start
        else:
            assert not idle and row["at_s"] < recovery_start
        last_at = row["at_s"]
    for client in clients:
        assert client["load_finished_s"] <= recovery_start
        assert client["connection_close_after_s"] >= measured_end


def summarize(path):
    manifest, result = read(path / "manifest.json"), read(path / "results.json")
    assert read(path / "status.json")["status"] == "completed"
    config = result["config"]
    assert config == manifest["config"]
    assert manifest["native_sha256"] == sha(Path(config["native"]))
    for filename, digest in manifest["sources"].items():
        assert sha(path / filename) == digest
    expected = read(path / "expected.json")
    for name, context in expected["contexts"].items():
        assert hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest() == expected["sha256"][name]
    assert expected["sha256"] == result["expected_sha256"]
    assert set(expected["sha256"]) == {q[0] for q in QUERIES}
    clients = []
    for compact in result["clients"]:
        client = read(path / f"{compact['kind']}-{compact['index']}.json")
        assert {k: v for k, v in client.items() if k not in ("samples", "maintenance")} == compact
        validate_client(client, config)
        assert client["maintenance"]
        status = client["final_status"]["checkpoint"]
        assert status["mode"] == "background" and status["progress"]["last_error"] is None
        assert status["interval_ms"] == config["interval_ms"]
        assert status["wal_reclaim_threshold_bytes"] == (config["threshold_bytes"] if config["mode"] == "reclaim" else None)
        clients.append(client)
    assert {(c["kind"], c["index"]) for c in clients} == {
        (kind, i) for kind, n in [("reader", config["readers"]), ("writer", config["writers"])] for i in range(n)}
    assert len(clients) == config["readers"] + config["writers"]
    monitor = read(path / "wal-samples.json")
    validate_recovery(monitor, clients, result)
    pinned = read(path / "pinned-reader.json")
    assert pinned == result["pinned_reader"] and pinned["snapshot_stable"]
    assert config["pin_at"] <= pinned["start_s"] < pinned["release_s"] < result["recovery_start_s"]
    assert pinned["release_s"] - pinned["start_s"] >= config["pin_seconds"]
    assert all(a < b for a, b in zip(pinned["old_revisions"], pinned["new_revisions"], strict=True))
    assert len(pinned["old_revisions"]) == config["writers"]
    assert result["acknowledged_writes_verified"]
    assert result["integrity"]["integrity_check"] == result["integrity"]["foreign_key_check"] == "ok"
    with sqlite3.connect(Path(config["database"]).resolve().as_uri() + "?mode=ro", uri=True) as conn:
        for writer in (c for c in clients if c["kind"] == "writer"):
            revision, raw = conn.execute("""SELECT revision,record FROM memweft_pool_facts
                WHERE tenant_id='default' AND user_id=? AND pool_id='pressure-live' AND fact_key=?""",
                (config["user"], f"pulse-{writer['index']}")).fetchone()
            assert revision == writer["last_revision"]
            assert json.loads(raw)["value"] == {"writer": writer["index"],
                "generation": writer["completed"], "mirror": writer["completed"]}
    def requests(kind):
        selected = [c for c in clients if c["kind"] == kind]
        rows = [s for c in selected for s in c["samples"]]
        return {"completed": len(rows), "offered": sum(c["offered_slots"] for c in selected),
            "missed": sum(c["missed_slots"] for c in selected), "rate": len(rows) / config["seconds"],
            "latency": stats([s["ms"] for s in rows]),
            "scheduled_latency": stats([s["ms"] + s["lag_ms"] for s in rows]),
            "by_query": {name: stats([s["ms"] for s in rows if s.get("query") == name]) for name, _ in QUERIES}
                if kind == "reader" else {},
            "windows": {name: stats([s["ms"] for s in rows if low <= s["at_s"] < high])
                for name, low, high in [("before_pin", 0, pinned["start_s"]),
                    ("pinned", pinned["start_s"], pinned["release_s"]),
                    ("after_release", pinned["release_s"], config["seconds"])]}}
    limit = config["threshold_bytes"]
    crossed = any(s["wal_bytes"] >= limit and pinned["start_s"] <= s["at_s"] < pinned["release_s"] for s in monitor)
    def first_below(start):
        return next((s["at_s"] for s in monitor if s["at_s"] >= start and s["wal_bytes"] < limit), None) if crossed else None
    counters = ["passive_runs", "reclaim_attempts", "reclaims", "busy_runs", "reader_deferred_runs", "backoff_deferred_runs"]
    progress = {f"{c['kind']}-{c['index']}": c["final_status"]["checkpoint"]["progress"] for c in clients}
    leaders = sorted({f"{c['kind']}-{c['index']}" for c in clients for s in c["maintenance"]
        if 2 <= s["at_s"] < config["seconds"] and s["status"]["checkpoint"]["progress"]["coordinator_role"] == "leader"})
    return {"path": str(path), "manifest": manifest, "fixture_sha256": result["fixture_sha256"],
        "sqlite_version": result["initial_status"]["sqlite_version"],
        "expected_sha256": expected["sha256"], "readers": requests("reader"), "writers": requests("writer"),
        "pin": pinned, "recovery_start_s": result["recovery_start_s"], "measured_end_s": result["measured_end_s"],
        "wal_peak_bytes": max(s["wal_bytes"] for s in monitor),
        "wal_at_load_end_bytes": next(s["wal_bytes"] for s in monitor if s["phase"] == "recovery"),
        "wal_at_recovery_end_bytes": monitor[-1]["wal_bytes"],
        "max_wal_sample_gap_s": max(b["at_s"] - a["at_s"] for a, b in zip(monitor, monitor[1:])),
        "first_below_threshold_after_release_s": first_below(pinned["release_s"]),
        "first_below_threshold_in_recovery_s": first_below(result["recovery_start_s"]),
        "threshold_crossed_while_pinned": crossed, "instance_progress": progress,
        "sampled_totals": {k: sum(p[k] for p in progress.values()) for k in counters},
        "observed_steady_leaders": leaders, "integrity": result["integrity"],
        "evidence_sha256": {p.name: sha(p) for p in sorted(path.glob("*.json"))}}


def render_report(a, b):
    config = a["manifest"]["config"]
    rows, query_rows, phase_rows = [], [], []
    for label, run in [("仅 PASSIVE", a), ("PASSIVE + 阈值回收", b)]:
        reads, writes = run["readers"], run["writers"]
        rows.append(f"| {label} | {reads['completed']:,}/{reads['offered']:,} | {reads['latency']['p95_ms']:.2f} | "
            f"{writes['completed']:,}/{writes['offered']:,} | {writes['latency']['p95_ms']:.2f} | "
            f"{writes['latency']['p99_ms']:.2f} | {writes['latency']['max_ms']:.2f} | "
            f"{run['wal_peak_bytes']/2**20:.2f} | {run['wal_at_recovery_end_bytes']/2**20:.2f} |")
        for phase in ["before_pin", "pinned", "after_release"]:
            r, w = reads["windows"][phase], writes["windows"][phase]
            phase_rows.append(f"| {label} | {phase} | {r['n']:,} | {r.get('p95_ms', 0):.2f} | {w['n']:,} | {w.get('p99_ms', 0):.2f} |")
    for name, _ in QUERIES:
        x, y = a["readers"]["by_query"][name], b["readers"]["by_query"][name]
        query_rows.append(f"| {name} | {x['n']:,} / {y['n']:,} | {x.get('p50_ms', 0):.2f} → {y.get('p50_ms', 0):.2f} | "
                          f"{x.get('p95_ms', 0):.2f} → {y.get('p95_ms', 0):.2f} |")
    recovery_lines = []
    for label, run in [("仅 PASSIVE", a), ("阈值回收", b)]:
        at = run["first_below_threshold_in_recovery_s"]
        recovered = ("空闲观察窗口内未回到阈值以下" if at is None else
                     f"空闲开始后 {at-run['recovery_start_s']:.2f} 秒首次采样到低于阈值")
        after_pin = run["first_below_threshold_after_release_s"]
        pin_recovered = ("长快照释放后未观察到低于阈值" if after_pin is None else
                         f"长快照释放后 {after_pin-run['pin']['release_s']:.2f} 秒首次低于阈值")
        counts = run["sampled_totals"]
        recovery_lines.append(f"- {label}：{pin_recovered}；前台全部结束时 {run['wal_at_load_end_bytes']/2**20:.2f} MiB，"
            f"观察结束时 {run['wal_at_recovery_end_bytes']/2**20:.2f} MiB；{recovered}。"
            f"回收尝试/成功采样合计 {counts['reclaim_attempts']}/{counts['reclaims']}，"
            f"进度不足延后 {counts['reader_deferred_runs']} 次，退避/冷却延后 {counts['backoff_deferred_runs']} 次。")
    recovery_at = b["first_below_threshold_in_recovery_s"]
    outcome = (f"启用阈值回收后，空闲约 {recovery_at-b['recovery_start_s']:.2f} 秒首次低于阈值，"
               f"观察结束为 {b['wal_at_recovery_end_bytes']/2**20:.2f} MiB。" if recovery_at is not None else
               "启用阈值回收后，本次空闲窗口内仍未观察到降至阈值以下。")
    return f'''# 多写者、慢查询与 WAL 空闲恢复

日期：2026-09-28。使用上一轮有序分块检索的同一原生构建，对比已有两种后台维护配置。
本轮补充压测和证据校验，没有调整 Rust 维护策略、事务、默认配置或索引。

{outcome}仅 PASSIVE 在空闲结束时仍占 {a['wal_at_recovery_end_bytes']/2**20:.2f} MiB。
两种模式都完成了绝大部分计划写入，但慢查询使计划查询完成率只有
{100*a['readers']['completed']/a['readers']['offered']:.1f}% / {100*b['readers']['completed']/b['readers']['offered']:.1f}%。
主动回收组的峰值仍达 {b['wal_peak_bytes']/2**20:.2f} MiB，最慢写入 {b['writers']['latency']['max_ms']:.2f} ms；
空间恢复不等于硬上限或延迟保证。

## 工作负载

- 本机 `{a['manifest']['platform']}`，Python {a['manifest']['python']}，SQLite {a['sqlite_version']}。
- 同一份 {a['integrity']['private_facts']:,} 条私有事实夹具的独立数据库副本；两个模式的初始副本和完整预期 Context 哈希一致。
- {config['readers']} 个读进程、{config['writers']} 个写进程，持续 {config['seconds']:g} 秒；总计划主查询 {config['read_rate']:g}/s、写入 {config['write_rate']:g}/s。
- 六种查询按实际执行次数循环，其中一种为双词慢查询。每个主查询后另查共享 live 记录，验证所有写者的 generation/mirror/修订号与单调可见性。
- 写者各更新一个独立共享 key，通过 CAS 连续推进修订号。覆盖数据库写锁竞争，未注入同 key 的业务 CAS 冲突。
- 第 {config['pin_at']:g} 秒额外只读进程固定快照 {config['pin_seconds']:g} 秒，验证期间不变、释放后看到新修订。
- 两组均为 {config['interval_ms']} ms 后台周期；第二组另启用 {config['threshold_bytes']/2**20:g} MiB 软回收阈值。
- 前台全部完成后继续观察 {config['recovery_seconds']:g} 秒，所有 SDK 实例保持打开，不再提交新请求。

顺序为仅 PASSIVE → 启用阈值回收，各一轮，非隔离专机。两组使用相同原生二进制和 runner。
延迟表仅统计已执行请求。错过的计划槽位直接跳过，未伪装成成功，也不积累无限队列。
额外 live 校验和维护状态采样不计入主查询延迟，但会消耗调度时间。

## 结果

单位 ms / MiB；WAL 数字为每 100 ms 采样的文件长度，可能漏过短暂峰值。

| 模式 | 查询完成/计划 | 查询 p95 | 写入完成/计划 | 写入 p95 | 写入 p99 | 最慢写入 | WAL 峰值 | 空闲末 WAL |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
{chr(10).join(rows)}

查询完成率会受到慢查询和有限客户端数的影响；不能仅凭延迟或完成数中的一项宣称更快。
写入 p99 之外保留最慢请求，完整调度延迟及每进程结果见 JSON。

| 查询 | 完成数 PASSIVE / 回收 | p50 PASSIVE→回收 | p95 PASSIVE→回收 |
|---|---:|---:|---:|
{chr(10).join(query_rows)}

| 模式 | 请求开始时所在阶段 | 查询次数 | 查询 p95 | 写入次数 | 写入 p99 |
|---|---|---:|---:|---:|---:|
{chr(10).join(phase_rows)}

## 空间恢复与边界

{chr(10).join(recovery_lines)}

后台 PASSIVE 会推进可复制的帧，但不承诺缩短已分配的 WAL 文件。
阈值回收仍受读快照、写锁及退避/冷却影响，{config['threshold_bytes']/2**20:g} MiB 不是大小硬上限。
连续慢查询可以让多个读事务重叠；显式长快照释放不代表数据库已经没有活跃读者。
本轮把停止所有前台请求后的空间变化单独记录，不将它混为持续负载下的回收保证。

原始状态保留 log_frames/checkpointed_frames、角色、繁忙和退避计数。
状态来自各实例最近一次维护；文件长度不是尚未 checkpoint 的帧数。
末次状态的维护计数不含 SDK 关闭时的最后一次维护。
两组稳态采样到的负责人分别为 `{a['observed_steady_leaders']}`、`{b['observed_steady_leaders']}`；
这是采样观察，文件锁互斥和崩溃接管仍由原有专项测试覆盖。

## 证据校验

所有主查询逐次比对完整 Context（包含诊断），live 查询检查跨进程修订；
两个长快照均稳定，释放后每个写者的修订均前进。
每次写入使用上一已确认修订作为 CAS 前提，结束后数据库中的修订和值与每个写者的全部成功次数相符。
完整性和外键检查通过。报表重算原始样本分位数、计划槽位、完成数和连续修订号，
并检查所有连接关闭时间都晚于空间观察结束。

完整性检查中的显式 checkpoint 在空间观察之后，未计为恢复收益。
原生二进制、runner 和帮助脚本的归档哈希也经报表核验。
这些测试不模拟断电；SQLite NORMAL 的持久化语义未改变。
单轮有限时长、合成事实、{config['writers']} 个共享写 key 不能证明数小时/数日、多租户热点或生产 SLO。
本轮没有执行模型评估，也没有把 macOS 结果与旧 Linux 压测直接计算改进比例。

## 复现

原始目录：`{a['path']}`、`{b['path']}`。大数据库、原生库和逐次样本保存在 Git 忽略的本地目录。
仓库中的同名 JSON 报告保留汇总、完整配置、实例末次状态和原始证据文件哈希。

- Runner：[benchmark_wal_recovery.py](../benchmark_wal_recovery.py)
- 校验器：[report_wal_recovery.py](../report_wal_recovery.py)
- 命令：[evals README](../README.md#multiple-writers-slow-queries-and-idle-wal-recovery)
- 原生库 SHA-256：`{a['manifest']['native_sha256']}`
- Runner SHA-256：`{a['manifest']['sources']['benchmark_wal_recovery.py']}`

后续重点是控制慢查询占用快照的时间与并发量，并在明确业务延迟/空间目标后做更长时间、多热点写入验证；不宜仅缩短回收间隔。
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--passive", type=Path, required=True)
    parser.add_argument("--reclaim", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="Output stem, without extension")
    args = parser.parse_args()
    a, b = summarize(args.passive), summarize(args.reclaim)
    assert a["fixture_sha256"] == b["fixture_sha256"]
    assert a["expected_sha256"] == b["expected_sha256"]
    assert a["manifest"]["native_sha256"] == b["manifest"]["native_sha256"]
    assert a["manifest"]["sources"] == b["manifest"]["sources"]
    ignored = {"mode", "output", "database", "native"}
    assert {k: v for k, v in a["manifest"]["config"].items() if k not in ignored} == {
        k: v for k, v in b["manifest"]["config"].items() if k not in ignored}
    assert a["manifest"]["config"]["mode"] == "passive"
    assert b["manifest"]["config"]["mode"] == "reclaim"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    dump(args.output.with_suffix(".json"), {"passive": a, "reclaim": b})
    args.output.with_suffix(".md").write_text(render_report(a, b))
    print(json.dumps({"verified": True, "output": str(args.output.with_suffix('.json'))}))


if __name__ == "__main__":
    main()
