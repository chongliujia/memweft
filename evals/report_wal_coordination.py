#!/usr/bin/env python3
"""Verify paired long-reader pressure evidence for coordinated WAL maintenance."""
import json
from pathlib import Path

from benchmark_bounded import sha
from report_wal_agent import DATA, ROOT, pressure, read


def measure(name, native):
    verified = pressure(name, native)
    path = DATA / name
    clients = [read(path / f'client-{i}.json') for i in range(-1, verified['result']['readers'])]
    assert all(c['maintenance_samples'] for c in clients)
    progress = {str(c['index']): c['final_maintenance']['checkpoint']['progress'] for c in clients}
    for c in clients:
        assert c['final_maintenance']['checkpoint']['mode'] == 'background'
        assert c['final_maintenance']['checkpoint']['progress']['last_error'] is None
    counters = ['passive_runs', 'reclaim_attempts', 'reclaims', 'busy_runs',
                'reader_deferred_runs', 'backoff_deferred_runs', 'leadership_acquisitions']
    leader_samples = sorted({c['index'] for c in clients for sample in c['maintenance_samples']
        if 2 <= sample['at_s'] < 295 and
        sample['status']['checkpoint']['progress'].get('coordinator_role') == 'leader'})
    verified.update(instance_progress=progress,
                    sampled_totals={k: sum(p.get(k, 0) for p in progress.values()) for k in counters},
                    steady_sampled_leader_indices=leader_samples)
    return verified


def main():
    initial_build = read(DATA / 'wal-coordination-build/manifest.json')
    for record in initial_build.values():
        assert sha(ROOT / record['snapshot']) == record['sha256']
    build = read(DATA / 'wal-coordination-final-build/manifest.json')
    for filename, record in build.items():
        assert sha(ROOT / filename) == sha(ROOT / record['snapshot']) == record['sha256'], filename
    native = build['target/release/libmemweft_ffi.so']['sha256']
    previous = sha(DATA / 'wal-reclaim-final-build/target__release__libmemweft_ffi.so')
    before = measure('wal-coordination-before-run1', previous)
    first = measure('wal-coordination-after-run1', initial_build['target/release/libmemweft_ffi.so']['sha256'])
    after = measure('wal-coordination-after-run2', native)
    assert before['manifest']['runner_sha256'] == after['manifest']['runner_sha256']
    assert before['manifest']['helper_sha256'] == after['manifest']['helper_sha256']
    assert len(after['steady_sampled_leader_indices']) == 1
    assert len(first['steady_sampled_leader_indices']) == 1
    assert first['manifest']['runner_sha256'] == after['manifest']['runner_sha256']
    verification = read(DATA / 'wal-coordination-verification.json')
    for filename, record in verification['logs'].items():
        assert sha(ROOT / filename) == record['sha256']
    assert verification['status'] == 'completed'
    output = ROOT / 'evals/reports/2026-09-22-wal-coordination'
    result = {'build': build, 'initial_build': initial_build, 'before': before,
              'first_after': first, 'after': after, 'verification': verification}
    output.with_suffix('.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    rows = []
    for label, data in [('旧版独立维护', before), ('新版协调与退避 / 首轮', first), ('最终版 / 复测', after)]:
        r = data['result']; w = r['writer']; totals = data['sampled_totals']
        rows.append(f"| {label} | {r['query_rate']:.2f} | {r['query_latency']['p95_ms']:.2f} | {w['latency']['n']:,}/{w['offered_slots']:,} | {w['latency']['p95_ms']:.2f} | {w['latency']['p99_ms']:.2f} | {w['latency']['max_ms']:.2f} | {r['writer_wal_max_bytes']/2**20:.2f} | {totals['reclaim_attempts']} / {totals['reclaims']} |")
    recovery = after['first_below_16mib_after_release_s']
    recovery_text = ('计时窗口内未采样到重新降至 16 MiB 以下，不能宣称这一轮完成了空间恢复。'
                     if recovery is None else
                     f"第 {recovery:.2f} 秒首次采样到 WAL 重新降至 16 MiB 以下；长读在约第 150 秒释放。")
    old = before['result']; new = after['result']
    p99_reduction = 100 * (1 - new['writer']['latency']['p99_ms'] / old['writer']['latency']['p99_ms'])
    p99_change = f"{'下降' if p99_reduction >= 0 else '上升'} {abs(p99_reduction):.1f}%"
    report = f'''# WAL 维护协调与回收退避

日期：2026-09-22。针对[上一轮](2026-09-22-wal-and-agent.md)回收时写入 p99 达到 78.78 ms 的问题，本轮加入同库维护执行权协调、繁忙退避与成功冷却。配置接口不变，主动回收仍需显式开启。保留原有 SQLite 3.51.3、索引 v2、查询和写入事务语义。

## 实现

- 同一规范化数据库路径使用一个持久空侧文件 `<database>.memweft-maintenance`，后台实例以非阻塞文件锁选出负责人。待命实例不执行 checkpoint，但保留自己的线程、连接和接管检查。负责人退出/崩溃后锁由内核释放，待命实例在后续周期接管。符号链接别名共用同一锁域；活动期间不能删除/替换侧文件。[Rust 文件锁](https://doc.rust-lang.org/std/fs/struct.File.html#method.try_lock)
- 负责人在同一连接上检查 `PRAGMA data_version`，发现其他实例或进程的提交。因此，即使负责人没有收到本进程写入通知，也能推进维护。[SQLite data_version](https://www.sqlite.org/pragma.html#pragma_data_version)
- 每周期先 PASSIVE。进度尚未完成时不额外尝试 TRUNCATE；满足回收条件后，TRUNCATE 繁忙则等待 5、10、20、30 秒，之后最多每 30 秒重试。成功后冷却 30 秒。实际执行还需等待维护周期，且保持零 busy timeout；没有驱逐读者。
- 没有后续写入时，未完成/超阈值任务仍会重试。关闭时只有负责人尝试最后一次 PASSIVE，不绕过退避做 TRUNCATE。WAL 元数据读取错误也会报告并触发后续连接自动 checkpoint 回退，不再当作零字节掩盖错误。
- 状态新增负责人角色、接管次数、协调等待、进度不足/退避次数和剩余重试时间。仍是每实例采样；计数不会在接管时迁移。负责人也可能是应用读者，监控必须覆盖所有实例。
- 文件名无法解析为 UTF-8 时明确拒绝后台模式，不把真实文件误判为无文件连接而跳过维护锁。此输入校验在首轮长测后的审查中补上，随后对最终构建重新执行同条件长测；正常文件的调度策略不变。

这是本地文件数据库的协作维护，不是分布式选主或网络文件系统支持。所有后台实例需升级并采用一致配置；当前由负责人参数决定维护行为，不会集中校验/合并配置。默认自动 checkpoint 的实例、旧版程序和外部 SQLite 连接不参加此协调。原生构建最低 Rust 版本更新为 1.89。详细配置、侧文件与持久化语义见[维护说明](../../docs/async_pipeline.md)。

## 相同 runner 的五分钟对照

按旧→新首轮→最终复测顺序，各在独立百万条数据库副本上运行 300 秒：8 个读进程、1 个写进程，计划查询 1000 次/秒、写入 50 次/秒，后台周期 1000 ms，回收软阈值 16 MiB。第 60 秒额外只读进程持有快照，90 秒后释放。各构建使用同一新版 runner，对所有实例每秒采样维护状态（此前 runner 仅采样写者）；采样在主请求计时之外，但计入调度负担。

| 实现 | 实际查询/s | 查询 p95 ms | 写入完成/计划 | 写入 p95 ms | 写入 p99 ms | 最慢写入 ms | WAL 峰值观测 MiB | 回收尝试/成功采样合计 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
{chr(10).join(rows)}

本轮写入 p99 相比同次旧版对照{p99_change}，错过的写入槽位由 {old['writer']['missed_slots']} 变为 {new['writer']['missed_slots']}。查询错过槽位分别为 {old['read_missed_slots']} 和 {new['read_missed_slots']}（各计划 {new['read_offered_slots']:,} 个槽位）。延迟仅统计实际执行请求，不能将跳过槽位计为成功。

新策略首轮写入 p99 为 {first['result']['writer']['latency']['p99_ms']:.2f} ms，成功截断采样合计为 {first['sampled_totals']['reclaims']} 次，末次 WAL 为 {first['wal_at_last_write_bytes']/2**20:.2f} MiB。首轮与复测全部保留，不只选择回收成功或延迟更低的一轮；两轮也不足以给出统计置信区间。

{recovery_text} 最后一次写入观测到 WAL 为 {after['wal_at_last_write_bytes']/2**20:.2f} MiB。文件长度不是未 checkpoint 的帧数，采样峰值也不是绝对瞬时峰值。退避/冷却会允许文件更久地超过阈值，阈值仍不是硬上限。

新版稳态采样（第 2–295 秒）只有一个实例报告 leader，索引为 {after['steady_sampled_leader_indices'][0]}（-1 表示写者，0–7 表示读者）。实例末次采样累计回收尝试 {after['sampled_totals']['reclaim_attempts']} 次，成功 {after['sampled_totals']['reclaims']} 次，因 PASSIVE 进度不足延后 {after['sampled_totals']['reader_deferred_runs']} 次，因退避/冷却延后 {after['sampled_totals']['backoff_deferred_runs']} 次。这些是各实例末次采样合计，末次采样后的关闭维护不在其中；角色采样也不能替代互斥锁的正确性测试。

三个长快照均保持稳定，释放后读取到新修订；每次主查询检查保存的完整上下文，额外 live 查询检查 generation/mirror/修订号；结束后数据库完整性、外键及全部已确认写入检查通过。结束后的完整性检查会回收 WAL，因此没有把其后的文件大小算作计时内收益。

## 验证和范围

- Rust {verification['rust_passed']} 项通过，含原有差分检索、CAS/遗忘/学习，以及新增单负责人、其他连接提交检测、正常退出接管、退避上限和冷却测试。长读释放且不再写入时仍能回收。
- Python {verification['python_passed']} 项通过，{verification['python_skipped']} 项外部数据库 DSN 测试跳过。新增跨进程测试通过符号链接打开同库，验证待命实例不维护、负责人检测外部写入、SIGKILL 后接管、已提交事实仍可读且只有一个协调侧文件。原有提交后崩溃恢复也通过。
- Node {verification['node_passed']} 项和离线评估 {verification['eval_passed']} 项通过。当前实测环境为本机 Linux；其他平台仍需 CI。本轮未重跑模型学习评估，未改变模型或学习策略逻辑。

旧版一轮，新策略首轮和最终复测各一轮，均为顺序运行，非隔离专机或随机重复试验；5 分钟不是数小时/数日验收。读负载覆盖现有四种较快的检索路径，写者更新独立 live 共享记录，没有加入慢聚合、多写者饱和或持续改写主查询命中记录。即使 p99 下降，仍须保留最慢请求和空间代价；这里没有给出生产 SLO 或千万/亿级承诺。

默认模式不变，主动回收仍为可选。一次实际 checkpoint 的磁盘同步/持锁时间没有被消除，退避只是减少尝试频率。[SQLite checkpoint 模式](https://www.sqlite.org/c3ref/wal_checkpoint_v2.html) `synchronous=NORMAL` 仍不等同于 FULL；进程崩溃测试不模拟断电。后续可在长时、多写者和慢盘条件下设定业务 SLO，再考虑调整策略参数。

## 复现与证据

原始目录：`data/evals/wal-coordination-before-run1/`、`wal-coordination-after-run1/`、`wal-coordination-after-run2/`；源码和构建快照：`data/evals/wal-coordination-build/`、`wal-coordination-final-build/`。旧原生库 SHA-256：`{previous}`；最终新库：`{native}`。

[机器可读结果](2026-09-22-wal-coordination.json)保存配置、构建哈希、全部实例末次状态及对照汇总。[运行说明](../README.md)给出命令；`python evals/report_wal_coordination.py` 核对原始证据后生成本报告。测试日志摘要在 `data/evals/wal-coordination-verification.json`。
'''
    output.with_suffix('.md').write_text(report)
    print('Coordination report written; paired runner hashes, all clients and committed writes verified.')


if __name__ == '__main__':
    main()
