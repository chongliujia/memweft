# Kimi 发布准备内部试点

2026-09-30，模型 `kimi-k2.6`，非思考模式。基于 MemWeft 当前仓库的真实文件、安装包清单和
Git 状态，完成一个内部发布规划任务；另在独立数据库演练四种记忆生命周期变化。
这不是客户数据测试，也没有执行实际发布或远端 CI。

## 产出与结果

- [当前项目的下一步计划](2026-09-30-release-next-steps.md)：恢复开发者预览目标和可下载安装包的交付方式，下一步核验待发布版本的跨平台 CI。
- [可复用 Agent CLI](../../examples/kimi_release_agent.py)：明确保存/修改/遗忘决策，跨会话继续规划。
- 有记忆 **5/5** 完成任务，无记忆 **1/5**；10 次输出均通过结构检查和当前证据一致性检查。
- 输入 **8,431**、输出 **583**，共 **9,014 tokens**。
- 按 2026-09-30 [官方费率](https://platform.kimi.com/docs/pricing/chat)估算 **¥0.070542**，不计缓存优惠，未经账单核对。本轮没有重试或限流失败。
- 离线回归 **74 项通过**。

## 逐项观察

| 场景 | 无记忆 | 有记忆 |
|---|---|---|
| resume_project | 未完成 | 通过 |
| change_target | 未完成 | 通过 |
| forget_delivery | 未完成 | 通过 |
| other_user | 通过 | 通过 |
| stale_ci_note | 未完成 | 通过 |

1. `resume_project`：使用当前选择的 `developer_preview` / `downloadable_artifacts`，恢复后建议核验跨平台 CI。
2. `change_target`：在隔离副本将目标更新为 `bounded_production`，旧目标消失，下一步转为业务试点。它是演练，没有改变日常 Agent 的预览目标。
3. `forget_delivery`：删除交付方式后仍记得目标，只请求补齐交付方式。旧聊天中保留的答案没有被重新注入。
4. `other_user`：相同项目里另一个用户的选择不进入当前上下文，两种模式都请求明确决策。
5. `stale_ci_note`：旧记忆写着“上周 CI 已全部通过，可以发布”，Agent 仍依据当前未验证的状态返回 ready=false。

无记忆组的澄清行为是合理的。评分要求恢复完整的既有决策并给出下一步，因此其中四组
计为未完成；这不表示模型错误臆测或违反业务规则。两个模式使用相同的应用规则和当前
证据，仅持久记忆不同。reason 是自由文本，不与预写句子匹配；评分检查四个业务字段。

## 当前证据实际说明了什么

本地清单引用的 Python wheel 与 Node tarball 存在，文件哈希匹配；记录的平台是 macOS arm64。
工作区有未提交修改。应用尚未接入远端 CI 和真实业务试点状态，因此这两个状态是
`not_verified`，不能把存在工作流配置当作执行成功，也不能把安装包哈希匹配当作当前
源码的完整发布验收。所有场景都保留 ready=false，没有发布、推送、消息发送或模型命令执行。

每个模式读取同一份带时间的证据快照；原始日志保存每次请求、上下文、回答和用量。
历史决策通过真实 Python SDK 和 SQLite 保存，每次操作关闭数据库后再次打开。
日常 CLI 的当前决策已保存在被 Git 忽略的 `data/release-agent.db`，演练数据库相互独立。

## 复现

参见[使用说明](../../docs/kimi_release_pilot.md)。有 API 费用的评测需显式启动：

```bash
python evals/run_kimi_release.py --check-memory
python evals/run_kimi_release.py --prompt-key --output data/evals/kimi-release-rerun
```

共 10 次调用，每次最多 256 输出 tokens，间隔 21 秒，任何接口错误立即停止。
密钥不写入仓库、数据库或结果文件。机器可读结果见
[报告 JSON](2026-09-30-kimi-release-pilot.json)，包含源码/证据哈希和全部业务判断。
本地详细记录在 `data/evals/kimi-release-2026-09-30-live/`。

本轮说明：在明确声明的发布工作流规则下，记忆可以减少重复解释，并使已保存目标的变化
影响下一步任务。样本规模只有一个项目、五种状态、每模式一次；尚不能据此推断客户任务
成功率、自动学习能力、长期可靠性或生产就绪。下一阶段应接入真实 CI 证据和实际使用记录。
