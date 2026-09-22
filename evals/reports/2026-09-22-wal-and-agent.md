# WAL 空间治理与本地 Agent 验证

日期：2026-09-22。增加可选 WAL 回收软阈值、实例维护状态、官方 SQLite 修复版，以及实际 LangGraph + Qwen 的记忆生命周期和学习采纳回放。批量池冲突诊断消除了升级过程中发现的逐记录诊断开销。默认仍使用 SQLite 自动 checkpoint；回收模式需要显式开启。没有实现完整查询/加载/压缩流水线、热点预加载、分片或 RDMA。

## 存储行为与 SQLite 修复

`background_checkpoint_ms=1000` 配合 `wal_reclaim_threshold_bytes=16777216`：后台先 PASSIVE，再在文件达到阈值时通过 SQLite 尝试 TRUNCATE。维护连接 busy timeout 为 0；繁忙则后续重试，读快照不被驱逐。零 busy timeout 不代表零 I/O、零持锁时间或不会影响提交延迟。阈值是软阈值，长读或持续读写可能使文件超过它。

`storage_status()` / `storageStatus()` 提供实际 SQLite 版本、维护模式、采样时刻、页数、文件字节、回收尝试/成功、繁忙、耗时和错误。计数属于实例，不是数据库全局统计；字节数是文件长度，不等于尚未 checkpoint 的帧数。配置、故障回退和 NORMAL 持久化边界见[维护说明](../../docs/async_pipeline.md)。

内置 SQLite 从 3.45.0 升级到官方 3.51.3，修复并发写入/checkpoint 的 WAL-reset race；此前短测完整性通过不能排除这个罕见问题。保留未修改的官方 amalgamation、下载与校验信息及兼容 Rust 绑定，见[源码说明](../../vendor/libsqlite3-sys/MEMWEFT-PATCH.md)。[SQLite 官方缺陷说明](https://www.sqlite.org/wal.html#walresetbug)、[3.51.3 发布说明](https://www.sqlite.org/releaselog/3_51_3.html)。

## 百万条混合读写，各 5 分钟

每轮独立复制同一 v2 数据库（目标用户 1,000,000 条私有事实，另有其他测试用户记录）。8 个读进程、1 个写进程，计划 1000 次查询/秒、50 次写入/秒，持续 300 秒。第 60 秒另一个只读进程持有快照，到第 150 秒释放，验证快照内旧值稳定、释放后读取到新修订。写者每秒采样维护状态，计入调度负担但不计入单次写入耗时。

| 版本 / 模式 | 查询完成 / 计划 | 查询 p95 ms | 写入完成 / 计划 | 写入 p95 ms | 写入 p99 ms | WAL 最高观测 MiB |
|---|---:|---:|---:|---:|---:|---:|
| 诊断版 / passive | 245,544 / 300,000 | 9.05 | 14,986 / 15,000 | 1.63 | 4.68 | 353.29 |
| 诊断版 / reclaim | 244,356 / 300,000 | 9.11 | 14,320 / 15,000 | 1.88 | 18.61 | 110.00 |
| 最终版 / passive | 287,768 / 300,000 | 2.99 | 14,985 / 15,000 | 1.60 | 4.31 | 353.27 |
| 最终版 / reclaim | 287,675 / 300,000 | 2.95 | 14,111 / 15,000 | 1.71 | 78.78 | 108.96 |

同一版本内比较 PASSIVE 与回收模式；诊断版与最终版的主要查询差异是池冲突解释批量化。第一对结果暴露了空间收益与写入尾延迟的交换，全部保留，没有只挑有利轮次。

最终回收轮：在第 214.02 秒首次采样到降回 16 MiB 以下（长读约第 150 秒释放）。 最后一次写入观测到 WAL 为 16.31 MiB。写者自身维护计数为 251 次回收尝试、1 次成功、253 次繁忙；其他实例也可能维护同一文件，不能把这些数当作全库合计。长期空间上限和低延迟回收仍未得到保证，因此不默认开启阈值。

**这一回收配置尚未通过低尾延迟服务的验收。** 最终轮写入 p99 从 4.31 ms 升至 78.78 ms，最慢一次为 515.57 ms，错过写入槽位从 15 增至 889。空间回收有效，但需要繁忙退避、回收冷却期和维护调度继续优化；单看接近的 p95 会漏掉这个代价。

每次主查询都核对已保存的 text、memories、messages、strategies，随后检查 live 共享记录 generation/mirror/修订号；四轮均完成数据库完整性、外键和最终已确认写入校验。完整性检查在计时外会回收 WAL，不能用检查后的文件大小宣称线上已回收。进程内维护和专门长快照的观测均已保存。

主查询服务耗时含 SDK/Rust/组装/JSON，不含额外 live 校验；完成率包含整个循环。错过时间槽会记录并跳过，因此 p95/p99 必须和完成率一起看。测试覆盖四种较快的检索路径，不含慢聚合、多写者饱和或主查询命中记录的持续改写。每个配置只有一轮、顺序执行，并非隔离专机；5 分钟也不是数小时/数日 SLO，更不是千万/亿级验证。

## 引擎升级后的查询回归与修复

首次升级对照发现快速查询由约 1–3 ms 增至约 4–7 ms。临时分段诊断定位到池冲突解释：每个已取回记录分别运行一次解释查询。现在一次批量读取，按选中记录序号、池顺序、事实 ID 保持解释顺序，仍最多返回 64 项并标记截断。查询仍在同一个读事务内，没有缓存过期结果或跳过隔离检查。诊断插桩已移除。

最终旧/新库对照每种查询预热 1 次、计时 30 次，22 组完整上下文均一致。这里的旧库已包含上一轮精确两词交集优化，不能再拿最早全量聚合的 2 秒基线计算本轮收益。

| 数据 / 池 | 查询 | SQLite 3.45 旧库 p95 ms | 最终版 p95 ms | 最终路径 |
|---|---|---:|---:|---|
| million | rare | 1.45 | 1.31 | bounded_prefix |
| million | frequent | 3.87 | 2.16 | bounded_prefix |
| million | mixed_terms | 3.64 | 2.00 | bounded_prefix |
| million | absent | 1.30 | 1.35 | bounded_prefix |
| million | empty | 1.59 | 1.02 | key_order |
| million | value_common | 2.57 | 1.92 | bounded_prefix |
| million | fallback | 345.66 | 338.44 | bounded_intersection |
| 100k-private | rare | 1.24 | 1.35 | bounded_prefix |
| 100k-private | frequent | 2.88 | 1.97 | bounded_prefix |
| 100k-private | mixed_terms | 2.25 | 1.89 | bounded_prefix |
| 100k-private | absent | 2.06 | 1.08 | bounded_prefix |
| 100k-private | empty | 1.82 | 0.98 | key_order |
| 100k-shared | rare | 1.25 | 1.46 | bounded_prefix |
| 100k-shared | frequent | 1.76 | 1.82 | bounded_prefix |
| 100k-shared | mixed_terms | 1.84 | 1.87 | bounded_prefix |
| 100k-shared | absent | 1.27 | 1.14 | bounded_prefix |
| 100k-shared | empty | 1.20 | 1.05 | key_order |
| 100k-mixed | rare | 2.15 | 2.05 | bounded_prefix |
| 100k-mixed | frequent | 3.34 | 2.99 | bounded_prefix |
| 100k-mixed | mixed_terms | 3.06 | 3.00 | bounded_prefix |
| 100k-mixed | absent | 2.02 | 2.05 | bounded_prefix |
| 100k-mixed | empty | 2.03 | 1.95 | key_order |

表内慢查询仍须遍历较多索引项；任何变慢项也原样保留。SQL 引擎升级不是所有查询都变快的保证。解释顺序、不同池值、截断上限另有专门测试；原先 1,273 次差分排名校验继续通过。

## 实际 LangGraph + 本机 Qwen

使用 [Agent 示例](../../examples/local_memory_agent.py) 的 recall → answer 图，通过实际 Python SDK 调用本机 `qwen3-8b`。温度 0.2、两次重复（种子 42/43），严格 JSON Schema。业务输入为模拟数据，没有真实工具执行或生产接入。旧版诊断轮和最终轮各运行 284 次调用；以下使用最终轮数据。

- 生命周期：44/44 通过。11 个阶段 × 无记忆/有记忆 × 2 次重复，涵盖更新、私有覆盖、跨 Agent 共享和隔离、删除回退、重启后遗忘、重建修订号、用户/租户隔离。无记忆对照预期为 null，验证正确弃答，不能把其通过率误读为已能回答业务事实。
- 学习训练门：候选 36/36，原策略 26/36；验证集候选 36/36，原策略 24/36。实际 SDK 任务状态为 `accepted`。
- 测试集：记忆基线 34/48，采纳策略后 48/48；改善 14、回退 0。这是 24 个任务各重复两次，不是 48 个独立任务。
- 采纳后修改共享源记忆，策略正确失效；随后遗忘源记录，检索不再带出它。完整请求、回答、召回上下文、评分和门控证据保存并重新核对。最终轮合计 150,739 模型 tokens。

候选由训练标签构造可审计案例表，经训练回退检查和验证集门控后采用，属于提示层策略学习，不是模型权重训练。支持数据沿用已评估的 V6 split，因此这是集成回放，不是新盲测或独立泛化证明。没有重放历史消息；遗忘测试不意味着外部会话里已经复制的内容被抹去。提示注入、工具授权、自动对话提取和真实长会话不在本轮覆盖中，先前企业测试暴露的问题不能据此宣称解决。

## 验证与证据

- Rust：42 项通过，含长快照繁忙后无需新写入也能重试回收、后台失败回退、SQLite 实际版本及批量诊断上限/顺序。
- Python：14 项通过，2 项外部 DSN 测试跳过；Node：7 项通过；TypeScript 构建通过。Python 的 50 次已确认提交后 SIGKILL/重开恢复通过；未模拟断电，NORMAL 并非 FULL。
- 离线评估测试：38 项通过，包含实际 LangGraph 的召回模式、采纳策略开关、会话历史排除及严格评分。

最终原生库 SHA-256：`8aaec66b15fbcded8d73a1816e8bd46553ebdc78a09fa22f4e6a331ef323577b`。构建/源文件快照在 `data/evals/wal-reclaim-final-build/`；长测在 `wal-soak-passive-final-run1/`、`wal-soak-reclaim-final-run1/`；查询在 `sqlite-upgrade-recall-final-run1/`；Agent 在 `agent-lifecycle-final-run1/`（均位于 `data/evals/`）。诊断阶段对应去掉 `final-` 的目录，保留独立版本哈希。

[机器可读结果](2026-09-22-wal-and-agent.json)、[复现命令](../README.md)。接下来优先做数据库级统一维护与退避，进一步压低回收尾延迟；接入真实业务长会话并加入新的盲测，再推进带内存预算和版本失效的预加载。
