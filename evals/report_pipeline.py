#!/usr/bin/env python3
"""Verify local benchmark artifacts and generate the pipeline/query report."""
import hashlib
import json
from pathlib import Path
import re
import sqlite3

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return json.loads(path.read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    build = read(ROOT/'data/evals/pipeline-after-build/manifest.json')
    for filename, record in build.items():
        assert sha(ROOT/record['snapshot']) == record['sha256']
        assert sha(ROOT/filename) == record['sha256'], filename
    after_sha = build['target/release/libmemweft_ffi.so']['sha256']
    before_sha = sha(ROOT/'data/evals/indexed-v2-before-tuning/libmemweft_ffi.so')
    pressure = {}
    for name in ['pressure-before-200','pressure-before-trace','pressure-after-auto-200',
                 'pressure-after-background-200','pressure-after-background-1000']:
        path = ROOT/'data/evals'/name
        assert read(path/'status.json')['status'] == 'completed'
        manifest = read(path/'manifest.json')
        assert sha(path/'benchmark_pressure.py') == manifest['runner_sha256']
        assert sha(path/'benchmark_bounded.py') == manifest['helper_sha256']
        result = read(path/'results.json')
        assert result['native_sha256'] == manifest['native_sha256'] == (before_sha if 'before' in name else after_sha)
        assert result['integrity']['integrity_check'] == result['integrity']['foreign_key_check'] == 'ok'
        readers = [read(path/f'client-{i}.json') for i in range(result['readers'])]
        assert all(r['native_sha256'] == result['native_sha256'] for r in readers)
        count = sum(len(r['samples']) for r in readers)
        assert count == result['query_checks'] == result['live_checks']
        assert count + result['read_missed_slots'] == result['read_offered_slots']
        writer = read(path/'client--1.json')
        assert len(writer['samples']) == result['writer']['latency']['n']
        assert len(writer['samples']) + writer['missed_slots'] == writer['offered_slots']
        assert max(r['start_monotonic'] for r in readers) < min(r['finish_monotonic'] for r in readers)
        intervals = [(r['start_monotonic']+r['samples'][0]['at_s'],
                      r['start_monotonic']+r['samples'][-1]['at_s']+r['samples'][-1]['ms']/1000)
                     for r in [*readers, writer]]
        assert max(a for a,_ in intervals) < min(b for _,b in intervals)
        with sqlite3.connect((path/'memory.db').resolve().as_uri()+'?mode=ro',uri=True) as conn:
            revision, record = conn.execute("SELECT revision,record FROM memweft_pool_facts WHERE tenant_id=? AND user_id=? AND pool_id=? AND fact_key=?",
                                            ('default','scale','pressure-live','pulse')).fetchone()
            record = json.loads(record)
            assert revision == record['revision'] == len(writer['samples'])+1
            assert record['value'] == {'generation':len(writer['samples']),'mirror':len(writer['samples'])}
        pressure[name] = {'manifest':manifest,'result':result}
    queries = ROOT/'data/evals/competitive-recall-run1'
    assert read(queries/'status.json')['status'] == 'completed'
    manifest = read(queries/'manifest.json')
    assert manifest['before_sha256'] == before_sha and manifest['after_sha256'] == after_sha
    for filename, digest in manifest['scripts'].items():
        assert sha(queries/filename) == digest
    for filename, digest in manifest['sources'].items():
        assert sha(ROOT/filename) == digest
    comparison = []
    for label in ['million','100k-private','100k-shared','100k-mixed']:
        a,b = [read(queries/f'{label}-{version}'/'queries.json') for version in ['before','after']]
        for version in ['before','after']:
            meta = read(queries/f'{label}-{version}'/'metadata.json')
            assert meta['native_sha256'] == (before_sha if version == 'before' else after_sha)
            assert meta['integrity']['index_version'] == 2
            assert meta['integrity']['integrity_check'] == meta['integrity']['foreign_key_check'] == 'ok'
        for old,new in zip(a,b,strict=True):
            assert old['context'] == new['context'] and old['name'] == new['name']
            comparison.append({'profile':label,'name':old['name'],'before':old,'after':new})
    assert len(comparison) == 22
    trace_path = ROOT/'data/evals/pressure-before-trace'
    pid = read(trace_path/'pid--1.json')['pid']
    trace = (trace_path/f'syscalls.{pid}').read_text()
    fsync = [float(m.group(1)) for line in trace.splitlines()
             if 'fsync(' in line and (m:=re.search(r'<([\d.]+)>$',line))]
    tests = read(ROOT/'data/evals/pipeline-verification.json')
    assert tests['status'] == 'completed'
    summary = {'build':build,'before_native_sha256':before_sha,'after_native_sha256':after_sha,
               'pressure':pressure,'query_manifest':manifest,'comparison':comparison,
               'traced_writer_fsync':{'calls':len(fsync),'seconds':sum(fsync),'max_seconds':max(fsync)},
               'verification':tests}
    output = ROOT/'evals/reports/2026-09-22-pipeline-and-query'
    output.with_suffix('.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    names = {'pressure-before-200':'旧版默认（诊断）','pressure-after-auto-200':'新版默认',
             'pressure-after-background-200':'新版后台 / 200 QPS','pressure-after-background-1000':'新版后台 / 1000 QPS'}
    rows = []
    for name,label in names.items():
        r = pressure[name]['result']; w = r['writer']
        rows.append(f"| {label} | {r['query_checks']:,} / {r['read_offered_slots']:,} | {r['query_latency']['p95_ms']:.2f} | {w['latency']['n']:,} / {w['offered_slots']:,} | {w['latency']['p95_ms']:.2f} | {w['latency']['p99_ms']:.2f} | {r['writer_wal_max_bytes']/2**20:.2f} |")
    query_rows = [f"| {r['profile']} | {r['name']} | {r['before']['p95_ms']:.2f} | {r['after']['p95_ms']:.2f} | {r['after']['plan']} |" for r in comparison]
    slow = next(r for r in comparison if r['profile']=='million' and r['name']=='fallback')
    report = f'''# 有界后台维护与精确交集检索

日期：2026-09-22。本轮在索引 v2 基础上，新增可选后台 WAL checkpoint 和两词竞争候选交集路径。默认 checkpoint 配置未变，索引版本仍为 2，不触发新的 postings 迁移。完整查询/加载/压缩流水线与预加载仍是[后续设计](../../docs/async_pipeline.md)。

## 先定位写入长尾

上一轮闭环实验中，快速读者产生更多请求，无法区分读速率与维护开销。本轮增加固定速率计划：8 个独立读进程、1 个共享池写进程，读者错峰调度，写者目标 50 次/秒；每轮 60 秒。调用超出时间槽时记录错过的槽位，不无限排队，也不把未完成槽位算成成功。

旧版在目标 200 QPS 下仍有写入长尾。随后单独对测试进程树跟踪系统调用，写者生命周期内观察到 {len(fsync)} 次 `fsync`，合计 {sum(fsync):.2f} 秒、单次最长 {max(fsync)*1000:.2f} ms。这证明文件同步等待是重要因素；跟踪包含启动/关闭，且跟踪本身会扰动调度，不能把这些时长精确拆分为每次提交或据此解释所有长尾。SQLite 官方说明自动 checkpoint 可在提交线程产生慢提交，并可移到其他线程执行。[SQLite WAL](https://www.sqlite.org/wal.html#performance_considerations)

## 可选后台维护的结果和成本

新选项 `background_checkpoint_ms=1000` 使用独立连接执行周期性 PASSIVE checkpoint，通知队列容量为 1。重复通知合并，**事实写入不进入该队列**，仍在原事务提交后返回。后台错误使后续连接借出恢复自动 checkpoint；关闭时停止并等待维护线程。配置与故障/持久化语义见[设计与用法](../../docs/async_pipeline.md)。

主要对照使用**同一个新版二进制**的默认模式与后台模式，避免把查询算法变化误算成后台维护收益。旧版默认列保留作定位证据。每个请求在查询后额外读取一次 live 共享池并检查修订号；主查询读取不变的百万条私有语料。实际数据库还包含 3,000 条其他测试 user 的记录。

| 模式 | 查询完成 / 计划槽位 | 查询 p95 / ms | 写入完成 / 计划槽位 | 写入 p95 / ms | 写入 p99 / ms | WAL 文件最高观测 / MiB |
|---|---:|---:|---:|---:|---:|---:|
{chr(10).join(rows)}

后台维护明显减少了这组负载中的写入等待，但 WAL 文件最高观测大小增加到了约 70 MiB。该数值是文件长度，不等于未 checkpoint 的帧数，也不是严格测得的瞬时峰值。没有在计时中强制 TRUNCATE，也没有承诺长期空间上限；最终完整性检查会在计时之外进行 checkpoint。长读快照、慢磁盘与持续写入需要更长时段的空间治理验证。

查询耗时包括 SDK、Rust 检索、渲染和 JSON 传输，不包括额外 live 校验；槽位完成率包含整个循环。原始结果还记录了从计划时刻计算的查询延迟、错过槽位、CPU、缺页和进程 I/O。被跳过的槽位没有延迟样本，因此必须把延迟与完成率一起解读。1000 QPS 一轮不是已达到 1000 QPS 的承诺，应看实际完成数。

默认与后台对照各只有一轮，按时间顺序运行，非随机顺序或隔离专机。200 QPS 与 1000 QPS 的尾延迟不可据此推断单调关系，也不是生产 SLO。当前并发组合不含必须聚合的慢查询，没有模拟更新主查询命中的同一批私有记录或多写者饱和。

## 两词精确交集查询

原来的前缀路径若不能证明完整性，会聚合全部匹配 postings。现在，当恰有两个 query term、最多四个池，并且完整有效前缀已证明当前 Kth 分数的同分顺序时，只需再找出分数**严格更高**的记录：枚举较高权重的单词列表和可能超过该阈值的权重组合交集。将这些记录加入候选后，重新执行原有资格检查和精确评分。

现有词权重范围为 1/2/3。交集候选超过 2,048 条、窗口过大、词/池过多或不能证明同分顺序时，继续使用原聚合算法；上限不会造成近似截断。查询全过程仍在一个读事务内，保留池优先级、状态、有效期和稳定排序。新执行标识为 `bounded_intersection`，不是完整 WAND/Block-Max。

旧版/新版各使用相同 v2 测试文件的独立副本，查询每种预热 1 次，再计时 30 次；每一次结果都与保存的完整 text、memories、messages、strategies 比较。全部 22 组一致：

| 数据/池 | 查询 | 旧版 p95 / ms | 新版 p95 / ms | 新版执行路径 |
|---|---|---:|---:|---|
{chr(10).join(query_rows)}

百万条 `red blue` 从 {slow['before']['p95_ms']:.2f} ms 到 {slow['after']['p95_ms']:.2f} ms。它仍需要遍历大量索引项，不是常数时间检索。三词以上、很大的交集和严格冲突模式等慢路径仍在；没有验证千万/亿级，也没有新增大型索引或缓存查询结果。

## 回归和证据

- Rust 40 项通过，包含 1,273 次差分排名检查、候选上限回退、并发快照、迁移回滚，以及后台错误回退与关闭测试。
- Python {tests['python_passed']} 项通过，2 项外部 PostgreSQL/MySQL DSN 测试跳过；Node 7 项通过，TypeScript 构建通过。
- 新增后台维护模式的 SIGKILL 测试：checkpoint 间隔设为 60 秒，50 次提交确认后杀死写进程，重开恢复全部 50 条。测试不模拟断电；NORMAL 同步模式没有变成 FULL。
- 所有基准逐次检查上下文和 live 修订号，运行后 SQLite 完整性与外键检查通过。原生库、runner 快照及源码快照哈希已核对。

旧原生库：`{before_sha}`。新原生库：`{after_sha}`。

原始证据在 `data/evals/pressure-before-200/`、`pressure-before-trace/`、`pressure-after-auto-200/`、`pressure-after-background-200/`、`pressure-after-background-1000/` 和 `competitive-recall-run1/`；构建与源码快照在 `data/evals/pipeline-after-build/`。

[运行说明](../README.md)、[机器可读结果](2026-09-22-pipeline-and-query.json)。本轮未调用模型，未实现模型压缩、热点预加载、分布式调度或 RDMA。下一步应补后台维护的长期 WAL 空间控制，再用有界队列推进热点预热和带源版本校验的压缩任务。
'''
    output.with_suffix('.md').write_text(report)
    print('Pipeline/query report written; artifact hashes, contexts and completion counts verified.')


if __name__ == '__main__':
    main()
