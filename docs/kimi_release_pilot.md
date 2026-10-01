# Kimi 发布准备内部试点

这个例子把 MemWeft 自己作为试点项目。应用读取本地发布证据，MemWeft 保存明确的
发布决策，Kimi 恢复决策并提出下一步。没有远端发布、推送、外部消息或模型生成的命令执行。
这是一项内部工作流验证，还不是客户业务收益或生产可靠性验证。

[2026-09-30 首次实测报告](../evals/reports/2026-09-30-kimi-release-pilot.md)记录了全部
10 次调用、逐项判断、成本估算，以及[当时的下一步计划](../evals/reports/2026-09-30-release-next-steps.md)。
这两份文件保留首次实现的历史结果；下文的 GitHub 采集是之后增加的功能。

## 使用

先按主 README 安装 Python SDK。以下命令在仓库根目录运行：

```bash
python examples/kimi_release_agent.py remember target developer_preview
python examples/kimi_release_agent.py remember delivery downloadable_artifacts
python examples/kimi_release_agent.py --artifact-dir dist/preview-0.2.0-alpha.1-a474cd2 plan --prepare-only
python examples/kimi_release_agent.py --artifact-dir dist/preview-0.2.0-alpha.1-a474cd2 plan --prompt-key --output data/release-next.md
```

前两步完全离线，将选择写入 `data/release-agent.db`。`--prepare-only` 读取当前文件和
决策，给出代码计算的下一步，零模型调用。最后一步调用一次
Kimi K2.6，关闭思考，最多输出 256 tokens。密钥从隐藏终端提示读取，也可以设置
`MOONSHOT_API_KEY` 环境变量并省略 `--prompt-key`。不自动加载 `.env`，不保存密钥。
账户每分钟 3 次请求时，连续手动调用也需要自行间隔；评测入口默认间隔 21 秒。

`--repo`、`--db`、`--user`、`--artifact-dir` 是放在子命令之前的可选参数。身份由调用者绑定；
这个 CLI 不提供生产鉴权。项目的绝对路径用于派生独立 tenant，移动目录会改变项目作用域。
项目路径、用户和固定的 release-planner agent 共同隔离记忆。

跨会话继续、修改和遗忘：

```bash
python examples/kimi_release_agent.py plan --session next-day --prompt-key
python examples/kimi_release_agent.py remember target bounded_production
python examples/kimi_release_agent.py forget delivery
python examples/kimi_release_agent.py plan --prompt-key
```

可用决策：

| key | 取值 |
|---|---|
| target | developer_preview / bounded_production |
| delivery | downloadable_artifacts / registries |

模型不自动写记忆。更新按 key 替换，遗忘之后需要重新明确选择；历史聊天和模型答案
不会被自动重新注入。它不从仓库内容猜测用户已忘记的发布目标。CLI 的 `--no-memory`
会按缺失决策返回补充请求；付费的有／无记忆对照使用下方评测入口。
输出路径必须是新文件，避免覆盖已有计划。

## 证据和边界

实时读取的文件限于 README、两个 SDK 的包描述、CI 工作流、升级文档、安装包清单及
清单内的 `.whl` / `.tgz` 文件；读取 Git HEAD 和工作区是否有改动。
模型收到的是文件哈希、安装包哈希匹配结果及结构化状态，不是完整仓库或环境变量。
通过 `--artifact-dir` 显式选择 `dist` 内的产物目录；默认仍为 `dist`，不会自动猜测
最新目录。包名称和目录拒绝路径穿越、符号链接。上面的目录属于提交 `a474cd2`，
更换提交后应选择为新提交重新构建和验收的目录。

`local_artifacts_match_manifest` 只表示包字节哈希匹配。
`local_artifacts_verified_for_source` 还要求两种 SDK 包版本与当前声明一致、v2 安装验收
清单通过、配套构建记录与清单的哈希一致、源码提交和工作流身份匹配，以及当前工作区
干净。旧 `0.1.0` 包即使哈希匹配，也不能满足当前预览的来源验收。
这些本地记录未签名，不能替代可信构建环境和人工发布审核。

默认只采集本地证据，CI 状态为 `unavailable / network_not_requested`。显式添加
`--github-ci` 才会只读访问 GitHub Actions；也可单独采集，不调用模型、不修改记忆：

```bash
python examples/kimi_release_agent.py evidence --github-ci
python examples/kimi_release_agent.py plan --github-ci --prompt-key
# origin 不是 GitHub 地址时可显式指定仓库：
python examples/kimi_release_agent.py evidence --github-ci --github-repo OWNER/REPO
```

公开仓库无需凭据。私有仓库可使用有读取 Actions 和仓库内容权限的 `GITHUB_TOKEN`，
令牌不会写入证据或传给模型。采集使用只读 GET，不触发工作流或推送。适配器校验
完整提交 SHA、仓库、工作流 ID/路径、该提交的工作流内容及最新运行的 attempt；
只接受 `push` 或 `workflow_dispatch` 事件，避免将 PR 合并测试当作当前源码测试。

| `github_ci.status` | 含义 |
|---|---|
| `unavailable` | 未请求网络、网络/权限错误、响应不完整或来源无法核验；具体见 `reason` |
| `not_run` | 查询成功，但该提交没有对应运行 |
| `pending` | 对应工作流尚未结束 |
| `failed` | 对应工作流已结束，结论不是 success |
| `passed` | 对应提交和工作流的总体结论为 success |

工作区有改动或 Git 状态无法读取时，`ci_for_current_source` 始终为 `unavailable`。
一个提交的 CI 结果不能覆盖未提交的文件。证据只是采集时的快照，换提交、修改文件或
重新运行 CI 后需要重新采集。2026-09-30 的实际查询中，提交 `7aa5ed09f343a999b5e7cc017c19836ba593a361`
没有对应运行，工作区也有改动，因此没有宣称 CI 通过。
见[本次采集报告和结构化证据](../evals/reports/2026-09-30-github-ci-evidence.md)。

`ready=true` 仅表示可以进入人工发布证据审核，不是发布许可。应用会复核 CI、工作区、
安装包清单和发布目标；证据不足时拒绝渲染模型声称 ready=true 的结果。适配器尚未
在线逐项核验矩阵作业和远端产物，这些仍需要审核。磁盘中昨天保存的 CI 快照不会被
自动当作今天的实时状态。CI 通过但本地包来源或安装验收不足时，下一步为
`verify_release_artifacts`。

`review_release_evidence` 表示**现在开始人工审核**，不表示审核已经完成。应用的前置条件
全部满足时，应进入这一步，`ready=true`；矩阵细项或远端产物尚未独立核验，是该步需要
核对的内容，不会将已经通过的当前源码 CI 改判为未通过。工作区不干净、CI 未通过、
安装包未核验属于当前源码，或生产目标缺少真实业务试点时，仍按原有顺序处理阻碍。

2026-10-01 的真实内部任务暴露过一个错误：安装验收完成后，模型以“矩阵作业与产物未
独立核验”为由退回 `verify_cross_platform_ci`。应用以 `next_action_contradicts_rules`
拒绝该提案，原始回答被保留。该回答已加入离线回归，用于确保错误仍会被拦截；
提示词改动是否改善新回答，需要另外调用模型验证，不能由离线重放推断。

发布目标、交付方式从同一上下文快照的两个必需事实键取得，模型必须逐项一致；遗忘、
缺失或无记忆模式对应 `null`。即使 `ready=false`，下一步也必须满足上述工作流顺序。
提示词建议 `reason` 使用 20–40 字符的中文短句，避免内部字段名；硬性上限为 80 字符。长度按解析后的
Unicode 字符数（Python `len`）计算，汉字、英文字母、标点和空格均计入上限。
超限时继续拒绝提案，保留原始回答，不截断、不自动修复，也不自动重试。

## 小范围日常试用与记录

先限定为一位维护者、一个项目的发布准备。每次 `plan` 默认写入新的
`data/release-pilot/<时间与随机标识>/attempt.json`；可用 `--run-dir` 指定新目录。
准备快照、正常回答、应用拒绝、模型请求错误分别记录。缺少发布决策时，CLI 直接返回
`clarify_decisions`，不消耗模型调用。记录包含证据身份、上下文、原始回答、拒绝原因、
token 用量和时延；请求头、密钥和环境变量不会写入记录。现有目录不会被覆盖。

解析或输出限制错误保留顶层分类 `validation_error.code=invalid_or_incomplete_model_plan`，
具体原因写入 `errors`，并在 CLI JSON、试用记录及汇总中显示。例如 `reason` 长度为
85 字符时，细节为 `{"code":"reason_too_long","field":"reason","expected":80,"actual":85}`。
这样既保留原有分类，也能区分具体拒绝原因。

```bash
python examples/kimi_release_agent.py summary --runs-dir data/release-pilot
python examples/kimi_release_agent.py assess 'data/release-pilot/<attempt-id>' correct_completion
```

人工复核结果可选 `correct_completion`、`incorrect_acceptance`、`correct_rejection`、
`incorrect_rejection`，每次尝试只记录一次；应由使用者检查原始提案和实际任务后填写。
汇总将规则接受、规则拒绝、请求错误、未复核和四类人工结果分开统计。规则接受不等于
任务正确，合理拒绝也不计为完成业务任务。未知用量保持未知，费用尚未接入计价，调用
发生后不会将费用记为零。人工复核记录不会自动使 `business_pilot` 变成 `passed`。

建议先观察恢复决策、修改目标、遗忘交付方式、选择旧包和补齐信息后再试这五类状态，
记录实际使用中发生的案例。自动化回归属于受控验证，不能冒充外部用户试用结果。
离线重放历史回答只能验证校验与诊断是否正确，不能证明调整提示词后模型的新回答已经改善。

发布范围未知时，模型应请求补齐决策；开发者预览优先验证跨平台 CI，不要求生产业务试点。
小范围生产目标优先安排业务试点；真实业务试点尚未接入证据服务，保持 `not_verified`。
这些是此应用明确声明的工作流规则，并非模型自己学习出来的策略。
交付方式会保留到计划中，但不会据此创建包注册表凭据或执行发布。

## 小规模对照

```bash
python evals/run_kimi_release.py --check-memory
python evals/run_kimi_release.py --prompt-key --output data/evals/kimi-release-rerun
```

默认共 10 次调用：5 个场景 × 有／无记忆，每种一次。第一个场景恢复当前项目已选的
开发者预览目标；另外四个是独立数据库里的目标变更、遗忘、其他用户隔离、过期 CI
记忆演练。改变目标不会修改日常使用的 `data/release-agent.db`。

两个模式收到完全相同的当前文件证据和工作流规则，只改变持久记忆。答案和分数只在
本地评测，期望答案不交给模型。完整响应、上下文、证据、token 用量、源码哈希和计划
保存在新的 `data/evals/` 目录；接口错误立即停止，保留已完成结果，不自动重试。

评测使用同一份带时间的证据快照，避免短暂文件变化混入有／无记忆对照。它度量的是
已保存决策能否跨会话影响下一步工作，不是全能项目规划能力，也不是独立用户的长期
成功率。无记忆组请求补齐缺失信息是合理行为，任务评分失败不等于模型回答违规。
