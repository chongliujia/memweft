# Kimi 实际交接状态回放

2026-10-02（Asia/Shanghai）：完成 **12 次真实 Kimi API 调用**，模型为 `kimi-k2.6`，关闭思考，
每次最多 256 输出 token；没有重试、请求错误或截断回答。按预先冻结的规则，有记忆的
6 次状态判断全部正确；无记忆的 6 次均正确请求补充上下文，后者不计为完成任务。

## 观察结果

| 原工作 | 有记忆：接手前 | 有记忆：接手完成后 | 无记忆：两个时点 |
|---|---|---|---|
| 离线交付验收 | `ready_to_continue / verify_delivery` | `completed / record_completion` | 均为 `needs_context / request_context` |
| 数据库备份恢复 | `ready_to_continue / restore_backup` | `completed / record_completion` | 均为 `needs_context / request_context` |
| 多项目工作日志 | `ready_to_continue / finish_docs` | `completed / record_completion` | 均为 `needs_context / request_context` |

12 次回答均保留 `human_review=pending`。工作日志的接手前记录明确包含
`declared_record_coverage.complete=true`，但文档任务仍为 `todo`；模型仍选择继续完成文档，
没有把“记录齐全”误判为“任务完成”。更新为实际完成后的事实后，模型转为记录完成，未建议重做。

这些是[上一轮三项实际本地工作](2026-10-01-simulated-external-developers.md)的六个历史状态回放，
不是新完成的六项业务任务。原工作由助手模拟开发者完成；本轮也没有增加真实外部用户或人工验收，
没有执行模型建议的命令。

## 输入与判定

[冻结场景](../scenarios/handoff-live-v1.json)包含来源路径、25 项来源哈希引用、精选观察和本地期望结果。
模型只接收任务类型与事实投影；不发送原交接中的下一步指令段、评分答案、阶段标签、源码、
个人绝对路径或环境变量。投影保留实际检查结果和任务状态，不能把它等同于模型自行检索完整项目。

每项工作先在新数据库写入接手前事实，关闭并重开 `Memory` 读取；随后更新同一个事实键，
再次关闭重开读取完成后的事实。两个时点的 context 都逐项匹配冻结输入。这是同一进程内
重新打开存储的检查；此前跨进程模拟续接的结果仍以原报告为准。

两组使用同一系统规则和任务问题，只有记忆参考是否存在不同；每个任务无记忆组的两份问题
完全相同。发送前冻结全部输入，交替组别顺序。系统提示明确规定状态与动作映射，因此结果仅支持
**精选事实和固定业务规则下的状态判断**，不证明复杂开放推理、长期稳定性或自主执行能力。

判定器要求完整 JSON、恰好四个字符串字段、正确状态与动作、未冒称真人批准，以及不超过
80 字符的非空理由。重复字段、额外字段、错误类型和截断均拒绝。理由只做结构与长度校验，
没有独立语义评分或人工评价。每种状态只观测一次，六个状态来自三个相关工作流，不作显著性推断。

## 用量与费用

- 输入：4,818 tokens；输出：426 tokens；合计：5,244 tokens。12 次均返回完整用量。
- 请求时延中位数约 1.46 秒，范围 0.98–1.77 秒；不含请求间的 21 秒节流等待。
- 按国内[官方价格](https://platform.kimi.com/)每百万未缓存输入 ¥6.50、输出 ¥27.00 计算，
  本轮估算费用 **¥0.042819**。忽略缓存折扣，这是用量估算，不是账单。
- [官方参数说明](https://platform.kimi.com/docs/api/models-overview)支持 K2.6 关闭思考。
  本轮沿用已有客户端，实际使用 `thinking.type=disabled`、`max_completion_tokens=256` 和 JSON 输出。

先前模拟程序没有调用模型的历史记录保持不变；本轮调用和费用单独记账。

## 验证与复现

[入口](../run_handoff_live.py)和[判定测试](../test_handoff_live.py)已保存。
固定 wheel 独立环境的新增 5 项测试通过。尝试在该纯净环境运行全部评测时，4 个测试模块因
没有可选 `langgraph` 依赖导入失败；失败日志保留，未改动该环境。随后在项目原有完整开发环境中，
**216 项测试全部通过**。不能据此宣称纯净 wheel 环境具备所有可选应用依赖。

本地原始证据：`data/handoff-live-2026-10-01/`。目录名沿用准备阶段日期，实际 UTC 时间在
`metadata.json` 与 `verification.json` 中；北京时间调用完成于 10 月 2 日。
其中 `prepared.json`、`results.jsonl`、源文件副本、执行代码副本和用量记录均保留。
12 个不同响应 ID、全部请求与冻结输入逐字匹配、所有原始回答与判定、来源及执行代码哈希均已复核。
[机器报告](2026-10-02-kimi-handoff-live.json)记录汇总与证据 SHA256。

重跑前，需取得上一轮证据目录并使用包含已安装 MemWeft SDK 的 Python 环境；全量离线测试
另需项目开发依赖。每次必须选新输出目录，真实调用会产生费用：

```bash
python evals/run_handoff_live.py --evidence-root data/external-simulation-2026-10-01 --output data/handoff-live-new --prepare-only
# 付费运行使用另一个新目录；密钥通过隐藏提示输入，不写入文件。
python evals/run_handoff_live.py --evidence-root data/external-simulation-2026-10-01 --output data/handoff-live-new-paid --prompt-key
```
