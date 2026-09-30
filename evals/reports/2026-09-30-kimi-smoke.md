# Kimi K2.6 记忆接入测试

日期：2026-09-30（Asia/Shanghai）。通过真实 Python SDK、Rust 核心和 SQLite v3
召回索引调用 `https://api.moonshot.cn/v1/chat/completions`，响应模型为 `kimi-k2.6`。

完整对照完成 **26 次调用**：有记忆 **12/13**，无记忆 **2/13**。
全部 26 次输出均满足 JSON 字段和类型协议，且 `finish_reason=stop`。
使用既有 `common-v3` 的 13 个记忆场景，每种模式一次，不运行学习阶段。

## 成本与调用配置

- 非思考模式，JSON-object 输出，每次最多 128 输出 tokens；不手动设置采样参数。
- 完整对照：输入 5,639、输出 283，合计 **5,922 tokens**。
- 按当日[官方价格](https://platform.kimi.com/docs/pricing/chat)（每百万输入 ¥6.50、输出 ¥27）估算，完整对照 **¥0.044295**，未计缓存优惠。
- 首次尝试完成 3 次调用，第 4 次遇到 HTTP 429；账户返回上限为 3 RPM。该尝试立即停止，没有自动重试。之后新建完整对照，调用启动间隔为 21 秒。
- 包括首次尝试的 3 次成功调用，总计 29 次有用量记录的调用、6,500 tokens，估算 **¥0.048912**；另有 1 次限流失败。该失败没有返回 token 用量。
- 以上为按用量估算，未经账单核对，不是货币预算上限。模型 HTTP 耗时中位数 1082 ms，最大 1812 ms；不含本地存储和限流等待。

旧的 `moonshot-v1` 系列已在[官方模型列表](https://platform.kimi.com/docs/models)标注下线；选择当前较低价的 K2.6，并按[官方参数说明](https://platform.kimi.com/docs/guide/kimi-k2-6-quickstart)关闭思考。

## 逐场景结果

| 场景 | 无记忆 | 有记忆 |
|---|---|---|
| preference-language | 未通过 | 通过 |
| preference-format | 未通过 | 通过 |
| session-next-step | 未通过 | 通过 |
| session-retry | 未通过 | 通过 |
| update-region | 未通过 | 通过 |
| update-branch | 未通过 | 通过 |
| forget-project-code | 通过 | 通过 |
| forget-preserves-unrelated | 未通过 | 通过 |
| isolation-user | 通过 | 通过 |
| isolation-tenant-agent | 未通过 | 通过 |
| recall-pressure | 未通过 | 通过 |
| recall-pressure-control | 未通过 | 通过 |
| recall-pressure-key-order | 未通过 | 未通过 |

无记忆组刻意不提供历史信息；遗忘和用户隔离两项的正确答案是 null，所以这两项可以通过。
这些分数反映记录是否可用，不能解释为模型推理能力的提高，也不能与旧 Qwen 评测直接排名。

## 唯一的有记忆失败项

`recall-pressure-key-order` 是原有测试集中的负对照，显式设置 `query=null`。
40 条干扰记忆排在目标 key 前面，而 `max_facts=30`，目标端口 17443 被排除；
上下文遗漏报告明确给出 `max_facts`，模型返回 `{"port":null}`。

同样的事实在传入问题作为 `query` 的 `recall-pressure` 中进入前 30 条，Kimi 正确返回
17443；放宽候选上限的控制项也通过。应保留这个失败对照，并在应用接入时传入查询词，
不应通过改答案、重试生成或扩大所有上下文来掩盖它。

更新、遗忘与隔离场景在发送请求前检查禁止出现的旧值或外部作用域值；所有检查通过。
每个场景都关闭并重新打开数据库后召回。会话重试场景还核验了重复事件只写入一次。
模型回答没有写回记忆；遗忘只针对本地事实，不声称删除过去已发给提供方的数据。

## 复现与范围

```bash
python evals/run_kimi.py --check-memory
python evals/run_kimi.py --prompt-key --output data/evals/kimi-rerun
python -m unittest discover -s evals -p 'test_*.py' -q
```

先按主 README 安装 Python SDK。密钥通过隐藏终端提示或 `MOONSHOT_API_KEY` 环境变量读取，
未写入代码、数据库或结果文件；没有密钥命令行参数。已有输出目录会被拒绝。
离线回归 **69 项通过**，包括 HTTP 错误密钥脱敏、无自动重试、调用次数与间隔、
真实 SDK 关闭后重开，以及截断答案不得判为通过。

- [接入示例](../../examples/kimi_memory.py)
- [评测入口](../run_kimi.py)
- [机器可读报告](2026-09-30-kimi-smoke.json)：含逐项答案、token 用量、上下文诊断和代码/证据哈希。
- 本地完整证据：`data/evals/kimi-2026-09-30-paced/`；首次限流记录：`data/evals/kimi-2026-09-30-live/`，均被 Git 忽略。

本轮是小规模合成场景，事实由应用明确写入。没有自动提取、策略学习、真实客户任务、
长期运行或统计置信度验证。结论是低成本 Kimi 接入和基础记忆生命周期已经跑通。
