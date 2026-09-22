# 有界后台维护与精确交集检索

日期：2026-09-22。本轮在索引 v2 基础上，新增可选后台 WAL checkpoint 和两词竞争候选交集路径。默认 checkpoint 配置未变，索引版本仍为 2，不触发新的 postings 迁移。完整查询/加载/压缩流水线与预加载仍是[后续设计](../../docs/async_pipeline.md)。

## 先定位写入长尾

上一轮闭环实验中，快速读者产生更多请求，无法区分读速率与维护开销。本轮增加固定速率计划：8 个独立读进程、1 个共享池写进程，读者错峰调度，写者目标 50 次/秒；每轮 60 秒。调用超出时间槽时记录错过的槽位，不无限排队，也不把未完成槽位算成成功。

旧版在目标 200 QPS 下仍有写入长尾。随后单独对测试进程树跟踪系统调用，写者生命周期内观察到 140 次 `fsync`，合计 6.77 秒、单次最长 135.63 ms。这证明文件同步等待是重要因素；跟踪包含启动/关闭，且跟踪本身会扰动调度，不能把这些时长精确拆分为每次提交或据此解释所有长尾。SQLite 官方说明自动 checkpoint 可在提交线程产生慢提交，并可移到其他线程执行。[SQLite WAL](https://www.sqlite.org/wal.html#performance_considerations)

## 可选后台维护的结果和成本

新选项 `background_checkpoint_ms=1000` 使用独立连接执行周期性 PASSIVE checkpoint，通知队列容量为 1。重复通知合并，**事实写入不进入该队列**，仍在原事务提交后返回。后台错误使后续连接借出恢复自动 checkpoint；关闭时停止并等待维护线程。配置与故障/持久化语义见[设计与用法](../../docs/async_pipeline.md)。

主要对照使用**同一个新版二进制**的默认模式与后台模式，避免把查询算法变化误算成后台维护收益。旧版默认列保留作定位证据。每个请求在查询后额外读取一次 live 共享池并检查修订号；主查询读取不变的百万条私有语料。实际数据库还包含 3,000 条其他测试 user 的记录。

| 模式 | 查询完成 / 计划槽位 | 查询 p95 / ms | 写入完成 / 计划槽位 | 写入 p95 / ms | 写入 p99 / ms | WAL 文件最高观测 / MiB |
|---|---:|---:|---:|---:|---:|---:|
| 旧版默认（诊断） | 11,994 / 12,000 | 2.88 | 1,226 / 3,000 | 76.06 | 111.36 | 21.34 |
| 新版默认 | 11,939 / 12,000 | 14.57 | 2,029 / 3,000 | 47.81 | 92.97 | 9.24 |
| 新版后台 / 200 QPS | 11,963 / 12,000 | 12.33 | 2,988 / 3,000 | 1.93 | 4.96 | 70.44 |
| 新版后台 / 1000 QPS | 58,140 / 60,000 | 2.94 | 2,994 / 3,000 | 1.06 | 4.38 | 70.58 |

后台维护明显减少了这组负载中的写入等待，但 WAL 文件最高观测大小增加到了约 70 MiB。该数值是文件长度，不等于未 checkpoint 的帧数，也不是严格测得的瞬时峰值。没有在计时中强制 TRUNCATE，也没有承诺长期空间上限；最终完整性检查会在计时之外进行 checkpoint。长读快照、慢磁盘与持续写入需要更长时段的空间治理验证。

查询耗时包括 SDK、Rust 检索、渲染和 JSON 传输，不包括额外 live 校验；槽位完成率包含整个循环。原始结果还记录了从计划时刻计算的查询延迟、错过槽位、CPU、缺页和进程 I/O。被跳过的槽位没有延迟样本，因此必须把延迟与完成率一起解读。1000 QPS 一轮不是已达到 1000 QPS 的承诺，应看实际完成数。

默认与后台对照各只有一轮，按时间顺序运行，非随机顺序或隔离专机。200 QPS 与 1000 QPS 的尾延迟不可据此推断单调关系，也不是生产 SLO。当前并发组合不含必须聚合的慢查询，没有模拟更新主查询命中的同一批私有记录或多写者饱和。

## 两词精确交集查询

原来的前缀路径若不能证明完整性，会聚合全部匹配 postings。现在，当恰有两个 query term、最多四个池，并且完整有效前缀已证明当前 Kth 分数的同分顺序时，只需再找出分数**严格更高**的记录：枚举较高权重的单词列表和可能超过该阈值的权重组合交集。将这些记录加入候选后，重新执行原有资格检查和精确评分。

现有词权重范围为 1/2/3。交集候选超过 2,048 条、窗口过大、词/池过多或不能证明同分顺序时，继续使用原聚合算法；上限不会造成近似截断。查询全过程仍在一个读事务内，保留池优先级、状态、有效期和稳定排序。新执行标识为 `bounded_intersection`，不是完整 WAND/Block-Max。

旧版/新版各使用相同 v2 测试文件的独立副本，查询每种预热 1 次，再计时 30 次；每一次结果都与保存的完整 text、memories、messages、strategies 比较。全部 22 组一致：

| 数据/池 | 查询 | 旧版 p95 / ms | 新版 p95 / ms | 新版执行路径 |
|---|---|---:|---:|---|
| million | rare | 1.52 | 1.34 | bounded_prefix |
| million | frequent | 2.29 | 1.94 | bounded_prefix |
| million | mixed_terms | 1.99 | 2.26 | bounded_prefix |
| million | absent | 1.36 | 1.35 | bounded_prefix |
| million | empty | 1.30 | 1.25 | key_order |
| million | value_common | 2.18 | 2.06 | bounded_prefix |
| million | fallback | 2309.86 | 338.70 | bounded_intersection |
| 100k-private | rare | 1.67 | 1.52 | bounded_prefix |
| 100k-private | frequent | 2.33 | 1.83 | bounded_prefix |
| 100k-private | mixed_terms | 1.84 | 2.24 | bounded_prefix |
| 100k-private | absent | 1.22 | 1.31 | bounded_prefix |
| 100k-private | empty | 1.19 | 1.20 | key_order |
| 100k-shared | rare | 1.25 | 1.38 | bounded_prefix |
| 100k-shared | frequent | 1.77 | 1.84 | bounded_prefix |
| 100k-shared | mixed_terms | 1.79 | 1.82 | bounded_prefix |
| 100k-shared | absent | 1.24 | 1.32 | bounded_prefix |
| 100k-shared | empty | 1.26 | 1.16 | key_order |
| 100k-mixed | rare | 2.63 | 2.63 | bounded_prefix |
| 100k-mixed | frequent | 2.96 | 3.22 | bounded_prefix |
| 100k-mixed | mixed_terms | 3.00 | 3.05 | bounded_prefix |
| 100k-mixed | absent | 2.09 | 2.03 | bounded_prefix |
| 100k-mixed | empty | 2.04 | 2.12 | key_order |

百万条 `red blue` 从 2309.86 ms 到 338.70 ms。它仍需要遍历大量索引项，不是常数时间检索。三词以上、很大的交集和严格冲突模式等慢路径仍在；没有验证千万/亿级，也没有新增大型索引或缓存查询结果。

## 回归和证据

- Rust 40 项通过，包含 1,273 次差分排名检查、候选上限回退、并发快照、迁移回滚，以及后台错误回退与关闭测试。
- Python 14 项通过，2 项外部 PostgreSQL/MySQL DSN 测试跳过；Node 7 项通过，TypeScript 构建通过。
- 新增后台维护模式的 SIGKILL 测试：checkpoint 间隔设为 60 秒，50 次提交确认后杀死写进程，重开恢复全部 50 条。测试不模拟断电；NORMAL 同步模式没有变成 FULL。
- 所有基准逐次检查上下文和 live 修订号，运行后 SQLite 完整性与外键检查通过。原生库、runner 快照及源码快照哈希已核对。

旧原生库：`4c34c6ae3095f75ea61566c56b2e9cc390023567ec4eace655ff3437a0a4b979`。新原生库：`2f10401bb1b134db4cc2c9a819287311a275a2416e1530734940503c326fdd18`。

原始证据在 `data/evals/pressure-before-200/`、`pressure-before-trace/`、`pressure-after-auto-200/`、`pressure-after-background-200/`、`pressure-after-background-1000/` 和 `competitive-recall-run1/`；构建与源码快照在 `data/evals/pipeline-after-build/`。

[运行说明](../README.md)、[机器可读结果](2026-09-22-pipeline-and-query.json)。本轮未调用模型，未实现模型压缩、热点预加载、分布式调度或 RDMA。下一步应补后台维护的长期 WAL 空间控制，再用有界队列推进热点预热和带源版本校验的压缩任务。
