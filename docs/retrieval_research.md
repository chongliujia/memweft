# MemWeft 查询优化研究：借鉴 Elasticsearch / Lucene

研究日期：2026-09-22。结论：**先在 SQLite 中建立持久倒排索引，保留现有词项评分和池优先级，限制候选与解释输出；BM25 和分块跳过作为后续独立实验。** 本文对应隔离研究原型，不表示 SDK 查询实现已经替换。

## 当前问题

`Session::context` 先通过 `resolve_pools` 读取所有可见事实，再由 `recall::Ranking` 对每条事实分词、匹配、全量排序，最后截断 `max_facts`。未入选的每条事实还会生成一条 omission。`max_facts=10` 限制的是结果，不是读取和计算的工作量。

已有 release 压测中，100/1,000/10,000/100,000 条事实的完整 SDK 查询 p95 分别为 0.70/6.84/59.73/689.53 ms。原始证据见 [企业评测](../evals/reports/2026-09-22-enterprise-v1.md)。本轮研究另行测量，不把旧基线与不同范围的原型延迟拼成端到端加速比。

## Elasticsearch 的几个关键机制

| 机制 | 解决什么问题 | MemWeft 的应用方式 |
|---|---|---|
| 倒排索引 | 从词项直接找到包含它的文档，避免每次重新扫描、分词全文 | 写入时建立 `scope + term → fact IDs + weight` |
| BM25 | 使用词频、稀有度、长度等信号计算相关性 | 单独评估排序质量；不是修复全量读取的前提 |
| Top-K 收集 | 只保留最有竞争力的 K 个结果 | 限制数据库返回及后续 JSON 反序列化量 |
| WAND / MAXSCORE / Block-Max | 利用分数上界跳过不可能胜出的候选或块 | 高频词仍慢时再评估；普通 SQL `LIMIT` 不等于实现这些算法 |
| Filter context | 精确判断文档是否符合条件，不参与相关性评分 | 租户、用户、Agent/池、有效期作为资格条件 |
| 有限命中统计 | 避免为了完整统计而访问所有命中 | 热查询返回有限解释样本；完整诊断另设显式入口 |

Elastic 的全文检索文档将分析器、倒排索引和评分明确分开；查询文本与索引文本需要一致的分析流程。BM25 是默认评分方法，但其作用是排序。[官方检索流程](https://www.elastic.co/docs/solutions/search/full-text/how-full-text-works)

WAND/MAXSCORE 用当前第 K 名分数作为竞争门槛。例如第 10 名已经得到 5 分，而一块记录的安全得分上界只有 3 分，就可以跳过该块。正确的上界可以保留准确的 Top-K；有相同分数和按 key 排序的要求时，还必须处理平分边界，不能直接跳过所有“等于门槛”的记录。[Elastic 算法说明](https://www.elastic.co/search-labs/blog/more-skipping-with-bm-maxscore)

跳过算法也有维护上界、迭代器和堆的开销。高频词、多子句等场景可能跳过得不够多，收益取决于查询与数据分布，不能套用他人测得的倍数。[Elastic 高频词研究](https://www.elastic.co/blog/speedups-top-k-queries-high-frequency-terms)

资格过滤与评分应在逻辑上分离；Elasticsearch 的 filter context 不计算相关性，并可缓存适合复用的过滤结果。精确命中计数还会限制跳过优化。因此 MemWeft 不应为了普通上下文请求，强制列出所有遗漏记录。[过滤语义](https://www.elastic.co/docs/reference/query-languages/query-dsl/query-filter-context)、[命中计数与优化](https://www.elastic.co/docs/solutions/search/the-search-api)

## 为什么第一步保留现有评分

当前评分为：查询中的每个不同词项，命中 key 加 2 分，命中 value 加 1 分；重复堆词不继续加分。平分时按 key、fact ID 排序。中文使用相邻双字词项。

这种评分本身也适合倒排索引：写入时保存词项及 1/2/3 的权重，查询只对命中的索引项求和。性能修改和相关性修改可以分别验收。

BM25 会引入词频饱和、词项稀有度和长度归一化。短记忆、端口、ID、项目代号不一定从这些信号获得同等收益；需要有标注的相关性测试。BM25 的统计范围也需明确，不能假设加一个 tenant 过滤条件就会把统计自动变成租户私有。Elastic 文档中的文档频率统计基于分片字段，跨分片统计另有查询模式。[BM25 变量与统计范围](https://www.elastic.co/blog/practical-bm25-part-2-the-bm25-algorithm-and-its-variables)

SQLite FTS5 提供 BM25、列权重和 rank 排序，可用作原型或候选后端；其 BM25 分数方向是越小越好。默认 tokenizer 也不能直接替换本项目的中文双字分析器。[FTS5 官方文档](https://www.sqlite.org/fts5.html#the_bm25_function)

## 已运行的隔离原型

脚本：[retrieval_probe.py](../evals/retrieval_probe.py)。只读访问原十万条数据，另外建立测试数据库，比较：

1. **原 SDK**：真实 release Python/Rust 扩展调用 `context(max_facts=10,max_tokens=1024)`，含报告和文本构造。
2. **加权倒排候选**：SQL 求和并取 Top-10，保持原权重、平分规则和零分补齐。
3. **FTS5 BM25 候选**：相同的去重词项编码，key/value 列权重 2/1，允许改变评分。这不是 Elasticsearch BM25 的等价配置，也不是自然文本词频实验。

原型只适用于静态、单私有 scope、唯一 fact key 的样本；没有实现多池合并、写入维护、迁移或生产授权。对稀有词、高频词、混合词、无命中四种查询，逐一核对加权倒排结果与全量参考及真实 SDK 的前 10 条 ID。原型没有实现或测量 WAND/MAXSCORE。

发现并修正了 FTS5 查询计划问题：普通 JOIN 被优化成先扫描 scope 下的 docs，再对每条文档查询全文表。改为让全文匹配在外层执行、按主键取文档后，避免了这个全量循环。正式实现必须审查执行计划，不能仅凭 SQL 写了 MATCH 和 LIMIT 判断性能。

第一次错误计划实验被终止，产物保留在 `data/evals/retrieval-research-run1/`；第二次探测曾与旧进程短暂重叠，不作为主要基准。第三次在前两个进程退出后运行，数据见 [研究测量结果](../evals/reports/2026-09-22-retrieval-research.json)。每个原型查询预热后测 30 次，SDK 基线预热后测 5 次；样本数较小，p95 是观测分位数，不是生产 SLO。


本轮干净运行的实测结果如下（p95，单位 ms）：

| 查询 | 命中事实数 | 原 SDK 完整 context（5 次） | 加权倒排候选（30 次） | FTS5 BM25 候选（30 次） |
|---|---:|---:|---:|---:|
| `deployment port` | 1 | 669.60 | 0.073 | 0.128 |
| `archive` | 99,999 | 646.73 | 27.414 | 61.002 |
| `archive deployment port` | 100,000 | 699.38 | 55.984 | 66.527 |
| `notpresentxyz` | 0 | 683.50 | 0.081 | 0.067 |

**两类计时范围不同，不能据此声称 SDK 已经加速到亚毫秒。** 四个查询的倒排 Top-10 均与旧 SDK 一致；本语料下 BM25 的 Top-10 也相同，不构成普遍排序兼容性或质量提升证明。

原型一次批量构建耗时 7.49 秒，保存 799,998 条倒排记录。两种索引连同复制的文档合计 82.71 MiB，其中 postings 表约 38.5 MiB；这不是生产索引增量大小，也没有覆盖逐次事务写入成本。原 SDK 的 report 经 Python JSON 再序列化约 10.49 MiB，说明诊断输出也需限量。

## 建议实施的查询路径

```mermaid
flowchart LR
    Q[任务查询] --> A[与写入一致的分词]
    A --> I[按作用域访问倒排索引]
    I --> P[有效期与池优先级资格检查]
    P --> K[评分并保留 Top-K]
    K --> F[仅加载选中事实]
    F --> C[构造预算内上下文]
    C --> E[有限解释样本与截断标记]
```

多池合并不是最后的美化步骤。假设共享池的 `port` 含有强相关词项，但私有池存在同名的新值，`private_first` 必须让私有值胜出，即便它对当前 query 得分更低。否则“先各池取若干高分记录、最后去重”会恢复旧值或丢失正确候选。

落实时必须满足：

- 权限绑定、作用域和有效期在候选资格阶段生效；不能先做全库 Top-K 再过滤。
- 池优先级在排名和截断前解析；`conflict_policy=error` 仍须发现原先会报错的冲突，不能只检查最终候选。
- 事实及索引写入、更新、删除在同一 SQLite 事务完成；覆盖所有高低层写入入口、版本冲突回滚、忘记及重建，避免旧索引重新暴露已删除内容。
- 旧数据库一次性回填并记录索引版本；测量迁移时间、额外空间和写延迟。
- `max_facts=0`、空查询、无命中、同分顺序、中文、有效期变化保持兼容。
- 对 omissions 和 shadowed 设上限并标注不完整；`inspected_facts` 明确表示加载的候选数，不能冒充索引访问量或所有命中数。全面诊断采用独立模式。
- 先以原扫描器作为 oracle 做差分测试，再测稀有/高频词和不同池冲突密度的 10 万、100 万条规模与并发读写；不能通过提前丢候选换取漂亮延迟。

## 后端选择

优先选同一 SQLite 事务内的加权倒排表，可直接承接现有评分和即时更新要求。FTS5 值得作为相关性与容量对照，但要处理 tokenizer、过滤执行计划和排序差异。

如果后续规模确实需要成熟的搜索索引执行器，可评估 Rust 的 Tantivy；它由 Lucene 启发，写入后的搜索可见性涉及 commit 与 reader reload。Elasticsearch 也有 refresh 后才对搜索可见的语义。因此引入独立搜索索引必须设计一致性协议，不能让 `forget()` 成功后旧事实仍从索引返回。[Tantivy 官方说明](https://github.com/quickwit-oss/tantivy)、[Elasticsearch 搜索可见性](https://www.elastic.co/docs/manage-data/data-store/near-real-time-search)

本轮结论是已有足够证据支持实施“倒排候选 + 有限诊断”这一阶段。BM25、Block-Max 和搜索服务后端分别解决其他层面的问题，应在各自的测量和正确性标准下推进。

后续实现已完成这一阶段：见 [索引检索与迁移说明](indexed_retrieval.md) 和 [完整 SDK 对照实测](../evals/reports/2026-09-22-indexed-recall.md)。上面的亚毫秒数字仍然仅代表早期 SQL 原型，不能替代完整 SDK 测量。
