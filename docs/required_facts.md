# 必需事实补全与工作流校验

词法检索可能找到证书，却漏掉判断过期所需的当前时间。应用知道任务需要哪些事实时，
可以通过 `required_fact_keys` 声明这些依赖；MemWeft 精确补齐可见键并优先放入上下文。
这是一项显式、可选的应用配置，不会自动推断事实关系，也没有引入向量检索。

## Python 和 Node API

```python
context = session.context(
    query="支付回调证书是否需要处理？",
    required_fact_keys=["tls.certificate", "clock.observation", "host.clock"],
    max_facts=8,
    max_tokens=1200,
    include_messages=False,
)
requirements = context.explain()["requirements"]
if not requirements["complete"]:
    print("还需补充或重新分配上下文预算：", requirements)
```

```typescript
const context = await session.context({
  query: "Does the certificate need attention?",
  requiredFactKeys: ["tls.certificate", "clock.observation", "host.clock"],
  maxFacts: 8,
  maxTokens: 1200,
  includeMessages: false,
});
console.log(context.explain().requirements);
```

Python 异步接口接受相同参数。键必须由可信应用配置提供，不能让模型或记忆里的文本
扩大读取范围。最多 64 个合法且不重复的键；未声明时继续使用原有召回路径。

| `report.requirements` 字段 | 含义 |
|---|---|
| `requested` | 声明的必需键，按传入顺序 |
| `included` | 实际进入最终 `memories` 和 `text` 的必需键 |
| `missing` | 当前作用域、池、状态和有效期规则下不可见的必需键 |
| `excluded` | 可见，但被 `max_facts` 或预算排除的必需键 |
| `complete` | 声明的键是否全部进入最终上下文 |

必需事实按声明顺序优先，其他事实保留词法相关性排序。`max_facts` 与文本预算仍然生效；
不能容纳全部必需事实时，`complete=false`，不会暗中扩大上限。预算仍是 UTF-8 字节估算，
不是模型 tokenizer 的精确 token 上限。`complete=true` 也不证明值正确、来源可信或业务条件齐全。

更新、遗忘、有效期、用户/租户/Agent 隔离和共享池冲突策略保持生效。`missing` 不区分
“从未记录”“已遗忘”或“其他作用域存在”，不会借此泄露其他作用域的信息。

SQLite 的普通事实召回和精确键补齐在同一个读事务中完成，避免两次读取之间的更新混入
同一事实集合；消息和策略仍是另行读取，不在这项快照保证内。补齐使用索引键查找。
`error` 冲突策略仍需全局检查可见池，保留其扫描成本。
未实现新接口的自定义 Store 会显式回退至 `recall.retrieval=full_scan`，不新增跨池事务保证。
本功能没有数据库迁移；从源码使用时需要同时重建 SDK 和原生扩展，既有安装包不自动获得新接口。

## 工作流示例：补齐、提案、校验

[应用规则](../examples/workflow_guard.py)定义四类示例任务，事实值是已解析的 JSON 对象：

| 工作流 | 必需事实 | 检查内容 |
|---|---|---|
| `tls` | 证书、当前时间、时钟偏移 | 时钟阈值优先、到期判断、时间差 |
| `refund` | 退款政策、订单状态、单价 | 时间窗口、未发货数量、整数最小货币单位 |
| `feature_scope` | 功能要求、实现状态 | 只纳入明确要求且就绪的能力；检查未完成离线要求是否阻塞 |
| `maintenance` | 维护时长、候选时段 | 选择合格窗口中空余最少者，计算结束时间，按固定规则解平局 |

这些规则是可读的应用代码，不是存进记忆的指令。它们不是通用业务政策，接入其他业务时
应自行定义。每个所需对象的字段、类型、取值和 `version: 1` 都会检查；忽略无关事实键，
拒绝所需对象中的未知字段、bool 冒充整数、隐式 JSON 字符串和不支持的版本。
版本检查只表示 schema 兼容性，不证明时间鲜活性或真实性。

可运行示例先保存一组明确的输入：

```python
from memweft import Memory

with Memory("data/workflows.db") as memory:
    user = memory.user("maintainer", tenant_id="workflow-demo", agent_id="workflow-assistant")
    user.remember({"version": 1, "expires_minute": 600, "owner": "cert_team"}, key="tls.certificate")
    user.remember({"version": 1, "now_minute": 625}, key="clock.observation")
    user.remember({"version": 1, "offset_seconds": 2, "threshold_seconds": 60}, key="host.clock")
```

在仓库根目录执行：

```bash
# 只准备上下文和检查输入，不调用模型。
python examples/guarded_workflow_agent.py tls "检查证书并提出处理建议" --prepare-only
# 输入完整且合法才读取密钥、调用一次 Kimi，并检查原始提案。
python examples/guarded_workflow_agent.py tls "检查证书并提出处理建议" --prompt-key
```

[示例](../examples/guarded_workflow_agent.py)正常流程：

1. 精确补齐应用声明的事实，检查事实类型和跨字段约束。
2. 输入不完整或非法时返回 `needs_data`，不调用模型。
3. 输入可用时调用一次 Kimi，保留原始回答。
4. 严格解析 JSON、检查输出契约和业务规则；通过为 `validated`，否则为 `rejected`。

拒绝不会被算作“完成任务”。示例不代填答案、不自动重试、不执行外部动作。检查针对
召回时的事实快照；真正执行前仍由业务系统核对当前状态和权限。确定性的金额和时间计算
也可直接由程序完成，模型适合负责需求理解和解释；这里让模型提出数值是为了验证校验边界。

## 冻结的新题对照

[题库](../evals/scenarios/workflow-guard-v1.json)有 8 个新任务，四类各中英文 1 题，
包含更新、其他作用域干扰和一题遗忘后数据不足。每题分别使用普通词法召回与必需事实补全，
各重复两次，共 32 次模型观测。标准答案只供评分，校验器不读取它。

```bash
python evals/run_workflow_guard.py --check-memory --output data/evals/workflow-guard-offline-new
python evals/run_workflow_guard.py --prompt-key --output data/evals/workflow-guard-live-new
```

最多 32 次调用，每次最多 192 输出 token，默认 21 秒间隔，约 11 分钟。不自动重试。
`--max-calls` 和 `--token-limit` 可降低限额；默认累计已报告 60,000 token 后停止后续请求，
不是人民币硬上限。每份 messages JSON 最多 16,000 字节，不含附加 HTTP 请求体字段。
所有输入、题库字节、源码快照及哈希在调用前冻结；密钥不进入文件。

完整运行后可离线复核并生成报告，输出文件必须不存在：

```bash
python evals/report_workflow_guard.py --run data/evals/workflow-guard-live-new \
  --json data/evals/workflow-guard-report-new.json --markdown data/evals/workflow-guard-report-new.md
```

报告器只需要标准库和应用规则模块，不调用模型、不加载原生 SDK。它会校验 32 个唯一
题目/模式/重复编号、实际请求、上下文文本与事实对象一致性、更新/遗忘/作用域、规则判定、
成绩、用量及源码哈希。不完整运行不能生成正式报告。

为了观察缺数据时的行为，**评测入口**会给每组同样一次原始模型观测，再检查结果；
**正常示例**会提前停止。报告分开统计原始答对、经过校验可交付的正确答案、错误放行、
拦截错误、正确答案被拒绝，以及缺数据场景拒绝情况。可答题保持 7×2=14 的分母，
不能通过拒绝所有答案获得高完成率。

两次观测并不独立，不代表稳定成功概率。这轮使用结构化事实和显式必需键，
不能把正确率直接与上一轮 20 道自由文本题相比较；它也不证明系统能自动抽取事实、发现
关联或抵御所有提示注入。事实 schema、应用规则和数据质量仍由接入方负责。

## 2026-09-30 实测

[完整报告](../evals/reports/2026-09-30-workflow-guard.md)及其 JSON 记录保留全部 32 次原始回答，
并重新核验了实际请求、事实、规则、评分和用量：

| 指标 | 普通词法召回 | 必需事实补全 |
|---|---:|---:|
| 原始正确 / 可答观测 | 6/14 | 13/14 |
| 校验后可交付正确 / 可答观测 | 6/14 | 13/14 |
| 错误放行 | 0 | 0 |
| 错误被拦截 | 8 | 1 |
| 正确答案被拒绝 | 0 | 0 |
| 缺数据拒绝 / 缺数据观测 | 2/2 | 2/2 |
| 输入 token | 8,188 | 8,174 |

总估算费用 ¥0.127440，未计缓存折扣。两次维护任务使用完全相同的完整上下文，补全组
第一次选对，第二次却选了更早但空余更大的窗口。校验器按既定“空余最少”规则拒绝了
第二次提案，保留原始回答，没有替它生成正确值。这验证了信息完整与决策正确是两件事。

普通组的失误集中在中文问句与英文结构化字段缺少词法匹配的四题；显式键配置补齐了这些
字段。这支持本例的应用适配方式，不代表通用语义检索已改善。样本仍很小，重复也有关联；
下一步真实业务接入应独立定义事实依赖和规则，再验证任务完成率及拒绝后的处理流程。
