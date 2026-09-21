# 基于Rust的上下文工程/记忆管理系统框架开发

---

# 0. 执行摘要

本方案构建一个“类人记忆”的**上下文工程/记忆管理系统**，以 **Rust in-process 核心库（PyO3/maturin）+ Python SDK + LangChain/LangGraph 同步适配** 的形态交付。系统默认 **SQLite 零配置**，可无缝升级 **Postgres / MySQL**。输出统一的 **MemoryPacket**（短期/长期/灵感分区），并内建 explain/replay、治理与评估机制。

**关键差异化能力**：

- 不是“全量检索记忆”，而是复刻人类记忆的 **线索驱动激活（cue-based recall）**：任何回忆都先将范围缩小到小候选集，再做装配与预算裁剪。
- 引入 **记忆压缩/降级/遗忘**：让总记忆可以无限增长，但每次参与回忆的候选集始终保持小，从而长期保持 P99 延迟可预测。

---

# 1. 目标、约束与范围

## 1.1 目标

- 支持 agent 运行态：**Working Memory（WM）**强结构化、低延迟。
- 支持会话连续性：**STM**窗口 + 滚动摘要 + 关键引用。
- 支持长期记忆：**Facts（语义）/ Episodes（经历）/ Procedures（程序性）**可治理、可纠错、可审计。
- 支持灵感：**Insight（假设/策略草图）**默认 run 内，验证后晋升。
- 每次生成结构化 **MemoryPacket**：可解释、可回放、可控预算。
- 同步覆盖 **LangChain + LangGraph**。
- **pip 安装即用**：默认 SQLite，升级 Postgres / MySQL。

## 1.2 约束（v1）

- 不做 embedding、rerank；不依赖语义向量检索。
- 回忆依赖：scope、task_type、时间窗、tags/entities、关键词、工具类型、冲突/失败信号。
- “Postgres / MySQL 是能力上限，不是起步门槛”：默认零配置。

---

# 2. 总体架构（含图）

## 2.1 总体架构图（pip 本地嵌入式 + 可升级 Postgres / MySQL）

```
┌──────────────────────────────────────────────────────────────────────────┐
│                         User Application (Python)                         │
│  ┌───────────────────────────┐            ┌───────────────────────────┐  │
│  │        LangChain          │            │         LangGraph          │  │
│  │  Chains/Agents/Tools      │            │ Graph/Nodes/State          │  │
│  └─────────────┬─────────────┘            └─────────────┬─────────────┘  │
│                │                                         │                │
│   ┌────────────▼─────────────┐             ┌────────────▼─────────────┐  │
│   │ LangChain Adapters        │             │ LangGraph Adapters        │  │
│   │ - ChatHistory             │             │ - Checkpointer            │  │
│   │ - ContextInjector         │             │ - Middleware              │  │
│   └────────────┬─────────────┘             └────────────┬─────────────┘  │
│                │                                         │                │
│   ┌────────────▼─────────────────────────────────────────▼────────────┐   │
│   │                        Python SDK (yourmem)                        │   │
│   │ append_event/append_tool_result/patch_state/build_memory_packet    │   │
│   │ explain/replay/consolidate/forget/policies/templates               │   │
│   └────────────┬─────────────────────────────────────────┬────────────┘   │
└────────────────┼─────────────────────────────────────────┼────────────────┘
                 │ PyO3 in-process (low latency)           │
                 ▼                                          ▼
        ┌──────────────────────────────────────────────────────────┐
        │                 Rust Core (yourmem-core)                  │
        │  Context Composer (热路径) | Consolidation Engine          │
        │  Explain/Replay            | Governance & Audit            │
        │  Stores: WM/STM/Facts/Episodes/Procedures/Insights         │
        │          Candidates/EventLog                               │
        └───────────────────────┬──────────────────────────────────┘
                                │ Storage Adapter (pluggable)
          ┌─────────────────────┴─────────────────────┐
          ▼                                           ▼
┌──────────────────────────┐              ┌──────────────────────────┐
│ Default: SQLite (WAL)     │              │ Upgrade: PostgreSQL      │
│ Zero-config local DB file │              │ DSN provided by user     │
└──────────────────────────┘              └──────────────────────────┘

```

---

# 3. 类人记忆分层与实体定义

- **WM（工作记忆）**：run 级结构化 state（goal/plan/slots/tool evidence/constraints）。
- **STM（短期记忆）**：session 级窗口 + 滚动摘要 + 关键句引用（key_quotes）。
- **LTM（长期记忆）**：
    - **Semantic Facts**：事实/偏好/规则（版本、冲突、有效期、证据链）
    - **Episodic Episodes**：经历摘要（时间线、tags/entities、证据引用）
    - **Procedural Procedures**：程序性策略（task_type → playbook）
- **Insight**：灵感/假设/策略草图（默认 run 内 TTL；验证后晋升，不当作事实）。

---

# 4. MemoryPacket v1（对外输出契约）

## 4.1 统一结构

- `meta`：schema_version、scope、purpose、generated_at、budget
- `short_term`：working_state、rolling_summary、key_quotes、open_loops
- `long_term`：facts、procedures、episodes（结构化、带 status/validity/citations）
- `insight`：hypotheses/strategy/patterns（validation_state、confidence、tests）
- `citations`：统一证据清单
- `budget_report`：各分区占用与降级路径
- `explain`：选择/丢弃/冲突提示

## 4.2 三模板注入规则（强制）

- **planner**：允许 insight；用于决策与下一步行动
- **tool**：强调 constraints + tool evidence 引用
- **responder**：默认不注入未验证 insight；以 facts + key_quotes 为主

---

# 5. 关键创新：记忆增长下的性能保障

> 目标：总记忆可增长，但每次参与回忆的候选集必须保持小。
> 
> 
> 人类靠“线索激活 + 压缩 + 抑制 + 分层”做到这一点。
> 

## 5.1 性能信条（铁律）

1. **永不全量回忆**：Recall 只在小候选集上进行。
2. **先缩范围再装配**：Scope/Time/Task/Tags/Entities → Candidate Set → Assemble。
3. **老记忆不参与默认回忆**：必须被压缩、聚合、降级；仅强线索才激活。
4. **Insight 不进入默认 responder**：避免污染正确性与性能。
5. **性能上限由候选集大小决定，不由总记忆量决定**：设定 SLO 护栏。

## 5.2 四层“记忆激活管线”（Cue-based Recall Pipeline）

```
Layer 0: WM as Primary Cue
  - goal/plan/slots/task_type/tools/conflicts
        │
Layer 1: Hard Scope Filters (DB indexed)
  - tenant/user/agent/session/run
  - purpose, task_type
  - time windows
  - fact status=active, insight not expired
        │
Layer 2: Cue Activation (small set)
  - tags/entities match
  - keyword match (light rules)
  - tool-type match
  - conflict/failure triggers
        │
Layer 3: Assemble & Budget Trim (O(n) with small n)
  - dedup/merge
  - degrade: raw → quote → summary → ref
  - stable ordering (deterministic)
  - output MemoryPacket + explain

```

**设计目标护栏**：

- `candidate_set_size`（每类记忆）应稳定在 **10–100** 的量级（可配置上限），超限即触发更强压缩/更窄时间窗。

## 5.3 记忆压缩/降级/遗忘策略

### A) Episodic（经历）时间线压缩

- 近 7 天：保留多条 episode（细粒度）
- 30 天：合并为“阶段摘要”
- 90 天：保留“里程碑级摘要”
- 更久：仅保留“主题总结 + 引用索引”

**效果**：episode 数量随时间增长趋于次线性（接近 O(log T)）。

### B) Facts（语义事实）版本替代而非累加

- 每个 `fact_key` 仅允许 1 条 `active`（默认参与回忆）
- 历史事实 `deprecated/disputed` 默认不参与，除非 purpose=debug 或显式请求
- 带 `valid_from/to` 控制可用性

**效果**：facts 回忆规模与 key 的数量相关，而非事件数量。

### C) Procedures（程序性策略）按 task_type Top-K

- 每个 task_type 保留 Top-K（priority/usage）
- 低优先级策略在默认 recall 中被抑制

### D) Insights（灵感）强 TTL + 必须验证晋升

- 默认 TTL=run 或短时间
- 未验证必须过期清理
- 验证成功后晋升为 Fact/Procedure/Episode

## 5.4 性能监控与 SLO（防止“悄悄变慢”）

必须内建指标与阈值：

- `build_memory_packet_latency_p50/p95/p99`
- `candidate_set_size`（按 memory type）
- `facts_active_count`（异常增长告警）
- `episode_compression_ratio`（压缩是否工作）
- `insight_expiry_rate`（未验证是否能清理）
- `packet_tokens_used` 与 `degradation_rate`

---

# 6. Rust Core 模块拆分（热路径优先）

1. EventLog（append-only）
2. WM Store（run 状态，版本控制）
3. STM（滚动摘要、窗口、关键引用）
4. LTM（Facts/Episodes/Procedures/Insights）
5. Candidate/Staging（评分、TTL、冲突标记）
6. Consolidation（晋升、合并、冲突状态机）
7. Context Composer（选择→合并→裁剪→输出→explain）
8. Governance（soft/hard delete、tombstone）
9. Explain/Replay（输入引用、输出包、裁剪决策）

---

# 7. 核心流程（时序）

## 7.1 LangGraph 节点级（推荐路径）

- node 前：`build_memory_packet(purpose=planner/tool/responder)`
- node 执行：LLM + tools
- node 后：`append_event`（消息/工具结果）+ `patch_state`
- run_end 或 milestone：`consolidate`

## 7.2 LangChain 链式（兼容路径）

- 调用前：build_memory_packet + 注入
- 调用后：append_event +（可选）patch_state

---

# 8. LangChain + LangGraph 同步适配

## 8.1 LangChain

- ChatMessageHistory Adapter（STM window）
- ContextInjector（将 MemoryPacket 按模板注入 prompt/messages）
- Tool Hook（可选：工具输出统一 append_event）

## 8.2 LangGraph

- Checkpointer（graph state ↔ WM）
- Middleware（node 前后 hook）
- Scope 映射：run_id=graph run；session_id=thread；agent_id=graph id；user_id 外部上下文

---

# 9. 存储：默认 SQLite + 可升级 Postgres / MySQL（行为一致）

## 9.1 零配置（SQLite）

- pip 安装后开箱即用
- WAL 模式
- 适合个人/原型

## 9.2 升级（Postgres / MySQL）

- 提供 DSN 或环境变量自动切换
- 多租户/审计/团队协作
- 与 SQLite 保持一致的排序与裁剪规则（deterministic）
- 数据库不存在时自动创建；未指定库名默认 `memweft`

## 9.3 存储抽象契约（关键）

Rust Core 仅依赖“查询原语”，保证双后端一致：

- events append/query（按 scope+ts）
- wm get/patch
- stm get/update
- facts query active + validity + keys
- episodes query time/tags/entities
- procedures query task_type
- insights query not expired
- context_builds write/read（explain/replay）
- ttl cleanup

---

# 10. 逻辑数据模型（两后端统一）

必需实体：

- events
- wm_state
- stm_summary
- candidates
- facts (+ fact_evidence, version chain)
- episodes
- procedures
- insights
- context_builds（MemoryPacket+explain+budget）

---

# 11. pip 分发与运行体验

- `pip install yourmem`
- 默认：本地 SQLite（无需配置）
- 设置 `DATABASE_URL`：自动切 Postgres / MySQL
- wheel 覆盖主流平台（Linux/macOS/Windows）
- 提供 migrations 初始化/升级流程（对用户透明或一键执行）

---

# 12. 可解释、回放与评估（护城河）

- 每次 MemoryPacket 记录 explain + budget_report
- Replay：同一输入序列可回放
- 支持 A/B 对比策略（policy id）
- 支持导出审计与删除证明

---

# 13. 非功能与可预测延迟策略

- 热路径 deterministic（排序、裁剪稳定）
- 后台 consolidation 不阻塞 compose（队列/节流）
- 大对象只引用不注入（摘要 + evidence refs）
- 设定 candidate_set_size 硬护栏（超限触发更强压缩/更窄窗口）

---

# 14. 路线图（建议）

Phase 1：WM/STM + MemoryPacket + LangChain/LangGraph 适配 + SQLite 默认 + explain

Phase 2：Facts/ Episodes/ Procedures + Consolidation（规则版）+ Postgres / MySQL 升级一致性

Phase 3：Insight（触发/验证/晋升）+ 更强评估体系 + 多租户 ACL

---

# 15. 架构补充图：性能关键路径（候选集控制）

```
Total Memory (can grow)  ─────────────────────────────────────────────┐
                                                                      │
                       (DB indexed hard filters)                      ▼
                ┌───────────────────────────────┐          ┌─────────────────┐
                │ Scope/Task/Time/Status Filters │─────────►│ Candidate Set    │
                │ (fast, indexed, deterministic) │          │ (must be small) │
                └───────────────────────────────┘          └───────┬─────────┘
                                                                    │
                                                       (cue activation & assembly)
                                                                    ▼
                                                          ┌─────────────────┐
                                                          │ MemoryPacket     │
                                                          │ + budget + explain│
                                                          └─────────────────┘

```

**核心承诺**：无论总记忆量多大，系统保证 candidate set 小且可控，从而长期保持延迟优势。

---

## 项目目录结构

```jsx
memweft/
├─ README.md
├─ LICENSE
├─ .gitignore
├─ .editorconfig
├─ rust-toolchain.toml
├─ Cargo.toml                  # Rust workspace
├─ crates/
│  ├─ memweft-core/             # 核心引擎：Composer/Consolidation/Policies（无DB细节）
│  ├─ memweft-store/            # Storage trait + 通用查询原语
│  ├─ memweft-store-sqlite/     # SQLite 实现（默认）
│  ├─ memweft-store-postgres/   # Postgres 实现（升级）
│  ├─ memweft-store-mysql/      # MySQL 实现（升级）
│  ├─ memweft-types/            # MemoryPacket/事件/实体类型与 schema（Rust side）
│  ├─ memweft-metrics/          # 指标与 tracing（可选，建议单独）
│  └─ memweft-ffi/              # PyO3 暴露的 API（最薄层）
├─ python/
│  ├─ pyproject.toml           # maturin 配置 + python 包元数据
│  ├─ src/
│  │  └─ memweft/               # Python SDK（对外 API）
│  │     ├─ __init__.py
│  │     ├─ client.py          # Memory() / from_env() 等入口
│  │     ├─ policy.py          # 注入模板与策略（planner/tool/responder）
│  │     ├─ adapters/
│  │     │  ├─ langchain/
│  │     │  └─ langgraph/
│  │     └─ utils/
│  └─ tests/                   # Python 侧集成测试（后续）
├─ docs/
│  ├─ architecture.md
│  ├─ memorypacket-v1.md
│  ├─ performance.md
│  ├─ storage.md
│  ├─ testing.md
│  └─ adr/
│     └─ 0001-architecture.md
├─ schemas/
│  └─ memorypacket.v1.schema.json
├─ examples/
│  ├─ langchain_basic.py
│  ├─ langgraph_basic.py
│  └─ sqlite_to_pg_migration.md
└─ .github/
   └─ workflows/
      ├─ ci.yml                # Rust+Python 单测/格式化/静态检查
      └─ release.yml           # wheels 发布（后续）
```

[MemoryPacket v1 字段级规范表](https://www.notion.so/MemoryPacket-v1-2d6a3806cc2080f0a6dcfdd1a6514232?pvs=21)

[压缩/降级/遗忘策略表 + SLO 护栏（窗口、Top-K、TTL、超限处理）](https://www.notion.so/SLO-Top-K-TTL-2d6a3806cc2080c7a253dfeb647236fb?pvs=21)
