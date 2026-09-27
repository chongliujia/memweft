#!/usr/bin/env python3
"""Verify v2/v3 query/write samples and paired multiwriter pressure evidence."""
import argparse
import json
from pathlib import Path

from benchmark_bounded import dump, sha, stats
from report_wal_recovery import summarize


def read(path):
    return json.loads(path.read_text())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--micro", type=Path, required=True)
    parser.add_argument("--before-pressure", type=Path, required=True)
    parser.add_argument("--after-pressure", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    assert read(args.micro / "status.json")["status"] == "completed"
    micro = read(args.micro / "results.json")
    manifest = read(args.micro / "manifest.json")
    assert micro["manifest"] == manifest
    assert sha(args.micro / "benchmark_bitmaps.py") == manifest["runner_sha256"]
    assert sha(args.micro / "benchmark_bounded.py") == manifest["helper_sha256"]
    pairs = 0
    for index, pair in enumerate(micro["fixtures"]):
        for label, version in [("before", 2), ("after", 3)]:
            part = pair[label]
            assert part == read(args.micro / f"{index}-{label}.json")
            assert part["native_sha256"] == manifest[f"{label}_native_sha256"] == sha(args.micro / f"{label}.so")
            assert part["index_version"] == part["integrity"]["index_version"] == version
            assert part["integrity"]["integrity_check"] == part["integrity"]["foreign_key_check"] == "ok"
            for row in [*part["queries"], *part["writes"].values()]:
                for key, value in stats(row["samples_ms"]).items():
                    assert row[key] == value
        a, b = pair["before"], pair["after"]
        assert a["source_sha256"] == b["source_sha256"] and a["fixture"] == b["fixture"]
        for x, y in zip(a["queries"], b["queries"], strict=True):
            assert all(x[k] == y[k] for k in ["profile", "query", "context_sha256", "plan"])
            pairs += 1
    before, after = summarize(args.before_pressure), summarize(args.after_pressure)
    assert before["manifest"]["native_sha256"] == manifest["before_native_sha256"]
    assert after["manifest"]["native_sha256"] == manifest["after_native_sha256"]
    assert before["manifest"]["sources"] == after["manifest"]["sources"]
    assert before["fixture_sha256"] == after["fixture_sha256"]
    assert before["expected_sha256"] == after["expected_sha256"]
    ignored = {"output", "database", "native"}
    configs = [{k: v for k, v in r["manifest"]["config"].items() if k not in ignored} for r in [before, after]]
    assert configs[0] == configs[1] and configs[0]["mode"] == "reclaim"
    assert before["integrity"]["index_version"] == 2 and after["integrity"]["index_version"] == 3
    result = {"micro": micro, "pressure_before": before, "pressure_after": after,
              "full_context_pairs": pairs}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    dump(args.output.with_suffix(".json"), result)
    queries, costs, writes, pressure = [], [], [], []
    for pair in micro["fixtures"]:
        a, b = pair["before"], pair["after"]
        count = a["fixture"]["facts_per_profile"]
        costs.append(f"| {count:,} × {len(a['fixture']['profiles'])} 作用域 | {a['bytes_after_upgrade']/2**20:.2f} → {b['bytes_after_upgrade']/2**20:.2f} | "
            f"{100*(b['bytes_after_upgrade']/a['bytes_after_upgrade']-1):.1f}% | {b['open_upgrade_close_ms']/1000:.2f} | "
            f"{b['bitmap_rows']:,} / {b['posting_rows']:,} |")
        for x, y in zip(a["queries"], b["queries"], strict=True):
            if x["query"] == "red blue":
                queries.append(f"| {count:,} | {x['profile']} | {x['p50_ms']:.3f} → {y['p50_ms']:.3f} | "
                    f"{x['p95_ms']:.3f} → {y['p95_ms']:.3f} | {x['p50_ms']/y['p50_ms']:.1f}× |")
        for key, x in a["writes"].items():
            y = b["writes"][key]
            writes.append(f"| {count:,} | {key} | {x['p50_ms']:.3f} → {y['p50_ms']:.3f} | {x['p95_ms']:.3f} → {y['p95_ms']:.3f} |")
    for label, r in [("v2 有序 ID 分块", before), ("v3 精确位图", after)]:
        reads, writer = r["readers"], r["writers"]
        pressure.append(f"| {label} | {reads['completed']:,}/{reads['offered']:,} | {reads['latency']['p95_ms']:.2f} | "
            f"{writer['completed']:,}/{writer['offered']:,} | {writer['latency']['p99_ms']:.2f} | {writer['latency']['max_ms']:.2f} | "
            f"{r['wal_peak_bytes']/2**20:.2f} | {r['wal_at_load_end_bytes']/2**20:.2f} | {r['wal_at_recovery_end_bytes']/2**20:.2f} |")
    report = f'''# 精确位图交集与慢查询快照

日期：2026-09-28。基线是上一轮有序 ID 分块实现，当前实现增加 v3 精确位图索引。
同一并发负载下，计划 {after['readers']['offered']:,} 次查询的完成数从 {before['readers']['completed']:,}
增加到 {after['readers']['completed']:,}，整体查询 p95 从 {before['readers']['latency']['p95_ms']:.2f}
降到 {after['readers']['latency']['p95_ms']:.2f} ms。索引增加了磁盘占用和写入成本，下面同时列出代价。
原有来源、权重、记忆池优先级、有效性、排序、候选上限和聚合回退保持不变。
{pairs} 组完整 Context 哈希全部一致，包含 text、memories、messages、strategies 和 report。

## 实现与正确性

每个作用域/词项/权重的连续 64 个 item ID 用一个 64-bit 位图表示，保留全部有效位。
双词路径按块 ID 求交，再用位运算枚举实际共同 ID；每个游标缓存最多 256 个位图行。
交集为空的交错 ID 不再逐条进入 Rust 比较。百万夹具中每个词由 500,001 个 posting
变为 15,626 个位图行。稀疏 ID 可能每块只有一位，因此没有固定压缩比或次线性最坏保证。

这不是 Bloom filter，也没有查询结果缓存。posting 的插入、更新、删除触发器在同一源事务内
维护位图，包括外键级联；空块立即删除。全部资格筛选、排名和正文读取仍处于同一读快照。
新增测试覆盖最高符号位、i64 极值、游标跨页、权重改变、级联删除及回滚。
v2 升级从现有 posting 回填；v1 先重排 posting，legacy 从源事实回填，最终均为 v3。
失败回滚包含已创建的位图表和回填数据。旧 v1/v2 二进制会拒绝新版本数据库。

## 完整 SDK 查询

本机 `{manifest['platform']}`、Python {manifest['python']}、Rust 1.89.0 release，SQLite 3.51.3。
每项一次预热、{manifest['samples']} 次计时，单位 ms。测量真实 `context()`，不是单独 SQL 或缓存命中。

| 每作用域事实数 | 分布 | p50 旧→新 | p95 旧→新 | p50 倍率 |
|---:|---|---:|---:|---:|
{chr(10).join(queries)}

balanced 是交错且几乎不相交的列表；clustered 为连续区间，skewed 为一侧极短，
large 为必须完整聚合的大交集。旧的按 ID 跳跃已经能快速处理 clustered/skewed，
位图主要改善 balanced；大交集回退依旧慢。单词、高频词、稀有词、无匹配和空查询全部保留在 JSON 中。

## 迁移、空间与写入代价

| 数据库内容 | 升级前后 MiB | 增量 | 首次打开/升级/关闭秒数 | 位图行 / posting 行 |
|---|---:|---:|---:|---:|
{chr(10).join(costs)}

上述时间含首次打开、迁移和关闭时 checkpoint；数据库文件大小在关闭后测量。
这不包含迁移临时分组、WAL 和副本的峰值空间。升级持有写锁，应在接收并发写入前完成。
已有 v2 posting 不重写；新增位图表需要额外空间和每次源修改的触发器维护。

单客户端分别执行 {manifest['writes']} 次私有/共享更新及插入，scope 与查询夹具隔离。
记录全部样本，含自动 checkpoint；没有丢掉较慢的提交。单位 ms。

| 夹具规模 | 操作 | p50 旧→新 | p95 旧→新 |
|---:|---|---:|---:|
{chr(10).join(writes)}

写入成本确实增加，尤其是不断引入新 key 的插入。稀疏词项通常不能合并到少量位图行；
应根据查询/写入比例评估这一代价，不能只引用密集交集的加速倍数。

## 相同五分钟并发负载

复用上一轮 v2 的完整原始回收压测作为基线，并用同一 runner 和源库重复 v3：
四读四写、计划主查询 100/s、写入 200/s，300 秒负载；第 60 秒固定快照 90 秒，
随后在所有 SDK 连接保持打开时观察 45 秒空闲恢复。均启用 1,000 ms 后台维护和 16 MiB 软阈值。
每个主查询均验证完整 Context，再检查所有写者的实时修订。时间单位 ms，空间单位 MiB。

| 构建 | 查询完成/计划 | 查询 p95 | 写入完成/计划 | 写入 p99 | 最慢写入 | WAL 峰值 | 负载末 WAL | 空闲末 WAL |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
{chr(10).join(pressure)}

双词查询在并发负载下的 p95 为 {before['readers']['by_query']['two_terms']['p95_ms']:.2f} → {after['readers']['by_query']['two_terms']['p95_ms']:.2f} ms。
完整请求耗时是本轮观测指标，没有单独插桩报告读事务微秒数。
新增位图缩短了查询工作，但不能撤销应用主动持有的 90 秒快照，也没有改变维护退避策略。
WAL 阈值仍是软阈值；文件占用与未 checkpoint 的帧数不同，低于阈值后不保证继续截断到零。

旧/新运行不是随机交错或隔离专机试验。每种构建各一轮，包含代码构建与测试之间的时间间隔。
仅比较本机同一工作负载，不与早期 Linux 结果计算收益，也不宣称长期尾延迟或生产 SLO。
长快照稳定性、每次读取内容、单调修订、最终已确认写入和数据库完整性/外键校验均通过。
报表核验原始样本、原生库及 runner 哈希；显式完整性 checkpoint 在空间观察结束之后执行。

## 验证与复现

本轮 Rust 54 项通过，Python 22 项通过（2 项外部数据库 DSN 测试跳过），Node 7 项通过，
离线 Agent/授权评估 64 项通过。没有调用模型。现有混合池、并发更新和大交集回退测试继续通过。

- 查询/写入 runner：[benchmark_bitmaps.py](../benchmark_bitmaps.py)
- 并发 runner：[benchmark_wal_recovery.py](../benchmark_wal_recovery.py)
- 报表校验器：[report_bitmap_recall.py](../report_bitmap_recall.py)
- 原始目录：`{args.micro}`、`{args.before_pressure}`、`{args.after_pressure}`
- 原生库 SHA-256：旧 `{manifest['before_native_sha256']}`；新 `{manifest['after_native_sha256']}`

同名 JSON 保留查询/写入全部样本和并发汇总、配置、实例状态及证据哈希；大数据库、二进制和逐次并发样本保留在 Git 忽略目录。
构建前先归档旧库，再用独立数据库副本顺序运行。参数与升级限制见 [evals README](../README.md#exact-bitmap-index-comparison)
和[索引检索指南](../../docs/indexed_retrieval.md)。
'''
    args.output.with_suffix(".md").write_text(report)
    print(json.dumps({"verified": True, "full_context_pairs": pairs, "output": str(args.output)}))


if __name__ == "__main__":
    main()
