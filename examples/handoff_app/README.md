# 独立 Python 项目交接应用

一个依赖 MemWeft **公开 Python SDK** 的独立下游应用，默认离线使用。保存明确的项目事实及其来源，
关闭进程后恢复任务，并显示缺失或因预算排除的必需记录。配置项目后可显式启用 Kimi 续接建议，
并通过已登记的验收脚本写回任务完成状态。模型建议和机器验收分别记录，人工复核保持待进行。

## 在仓库外运行

仓库中的 `examples/handoff_app/` 与交付包中的 `app/` 是同一应用目录；请复制整个目录。
下文的 `handoff.py` 命令从应用目录运行。若从交付包根目录执行，脚本路径应为 `app/handoff.py`，
并留意 `init` 默认在当前工作目录创建 `handoff.json`。

选择支持你的 Python/操作系统/CPU 的固定提交 wheel。已核验的参考来源是
[提交 1ee6b3b 的 CI](https://github.com/chongliujia/memweft/actions/runs/36822104764)，
其中 `macos-15-arm64-py3.11-attempt1` 专用于 CPython 3.11 / macOS arm64。
核对下载 artifact 的 `build-record.json`、`package-target.json`、源码 SHA 和 wheel 哈希；
相同版本号不能替代提交身份。其他平台需要选择对应 artifact。

将本目录复制到 MemWeft 仓库**之外**，在那里创建环境并安装 wheel：

```bash
cd /path/to/your-handoff-app
python3.11 -m venv .venv
.venv/bin/python -m pip install --no-index --no-deps /path/to/matching-memweft.whl
.venv/bin/python handoff.py --help
```

不设置 `PYTHONPATH`，不需要 Rust 或从源码安装 MemWeft。应用及数据库位置由调用者选择。
Windows 使用 `.venv\Scripts\python.exe`；示例 shell 命令使用 Unix 路径格式。

### 在自己的工具中读取 SDK 记录

`user.memories()` 返回事实记录：显式指定的 key 在 `fact_key`，保存的数据在 `value`。
应用自己的来源说明可以随业务值一起保存；它不是 SDK 自动核验的来源。以下示例使用独立数据库，
可在已安装 wheel 的环境运行，不需要查询包元数据或阅读 SDK 实现：

```python
from memweft import Memory

with Memory("decisions-demo.db") as memory:
    user = memory.user("maintainer", tenant_id="decision-log:demo", agent_id="decision-log")
    user.remember({"value": "Use SQLite", "source": "Demo decision"}, key="storage")
    for fact in user.memories():
        if fact.get("fact_key") == "storage":
            value = fact["value"]
            if not isinstance(value, dict) or not all(
                isinstance(value.get(key), str) for key in ("value", "source")
            ):
                raise ValueError("Unexpected decision shape")
            print(value["value"], value["source"])
```

再次调用同一 scope、同一 key 的 `remember` 会替换整个 `value`；更新时要同时传入需要保留的
业务值和来源。`user.forget("storage")` 返回是否删除了记录，关闭并重新打开数据库后仍然生效。
scope 用于本地数据分区；调用者仍需负责身份绑定和授权。

## 保存、恢复、修改和遗忘

以下是演示输入，实际试用时换成使用者确认的真实需求和来源：

```bash
.venv/bin/python handoff.py --project demo remember goal '完成本周项目交接' --source '使用者明确要求'
.venv/bin/python handoff.py --project demo remember merge_policy '合并到 main 后提交远端' --source '团队确认的约定'
.venv/bin/python handoff.py --project demo resume '继续项目交接' --require goal --require merge_policy --out runs/first
```

每条命令都是独立进程；`resume` 不依赖上一进程的会话状态。它生成 `snapshot.json` 和
可阅读的 `handoff.md`。现有输出目录拒绝覆盖。显式必需记录缺失或被预算排除时仍保存诊断，
退出码为 **2**；完整时为 **0**，表示可以交给使用者复核，不表示任务已经通过人工验收。

同一个 key 的 `remember` 替换当前值；`forget` 从以后生成的交接里移除记录：

```bash
.venv/bin/python handoff.py --project demo remember goal '补齐验收后完成项目交接' --source '使用者更新的需求'
.venv/bin/python handoff.py --project demo forget merge_policy
.venv/bin/python handoff.py --project demo resume '继续项目交接' --require goal --require merge_policy --out runs/second
```

上面的第二次恢复应明确报告 `merge_policy` 缺失。遗忘不会删除已导出的文件；如需删除历史副本，
由持有副本的应用另行处理。`list` 列出当前 scope 中的事实。

`--project` 与 `--user` 共同隔离记录，调用者负责可信身份绑定；本例不是鉴权服务。
`--require` 声明任务依赖，`--max-facts` / `--max-tokens` 控制上下文预算。预算 token 是 SDK 的估算，
不是模型 tokenizer 计数。完整性只针对声明的必需 key，不证明记录真实性或覆盖全部业务需求。
来源字段是记录者的说明，未经独立验证。旧 schema 数据库升级前遵循 SDK 的备份恢复指引。

## 真实试用记录

将实际需求与演示、故障演练分开计数。一个交接任务中的保存、修改、重启、恢复属于同一个任务。
记录固定 SDK wheel 的提交和哈希、应用源码哈希、接入起止时间、遇到的阻碍、各次输出及实际结果。
只有使用者阅读具体交接并给出评价后，才能写入人工验收；自动测试和助手复核都不代替人类。
本应用不自动创建人工评估，也不把完整性检查等同于生产业务试点通过。

## 配置项目并继续真实任务

把整个应用目录复制到仓库外；新增模块和 `handoff.py` 一起交付。
原有 `--db --project` 离线命令继续可用。配置模式默认读取当前目录的 `handoff.json`，
也可在子命令前用 `--config /path/to/handoff.json` 选择项目。

首次接入按“环境检查 → 初始化 → 按配置检查”的顺序进行：创建配置前先运行不带 `--config` 的
`doctor`；显式指定的配置文件必须已经存在，否则会提示先运行 `init`。`doctor` 在 SDK 缺失时也能运行，检查 Python、原生 SDK 的临时数据库
读写、数据库父目录及可选 Kimi 凭据；不会访问模型或打开项目数据库。它不认证安装包发布者。

```bash
.venv/bin/python handoff.py doctor
.venv/bin/python handoff.py --project demo init --require goal
.venv/bin/python handoff.py --config handoff.json doctor
.venv/bin/python handoff.py remember goal '完成项目文档链接检查工具' --source '维护者确定的任务'
```

配置绑定 `project`、`user`、`database`、`workspace`、`runs_dir` 和 `required_facts`。
路径相对于配置文件所在目录，不受执行命令的工作目录影响；默认数据库为 `data/handoff.db`，
工作目录为 `.`，记录目录为 `runs/`。可移动整个项目目录。配置不含凭据，也不允许命令临时
覆盖数据库或身份。`init` 拒绝覆盖已有配置；需要调整时显式编辑配置。

将你信任的验收逻辑保存为 `verify.py`，再登记任务。验收脚本应在失败时返回非零退出码，
并且不能修改待验收交付文件。例如，脚本可以用 `subprocess` 调用工具并检查真实输入和异常输入。
登记时冻结验收脚本哈希，交付文件可以在之后创建：

```bash
.venv/bin/python handoff.py task add links '实现文档链接检查工具' --verify verify.py --artifact solution.py
.venv/bin/python handoff.py resume '接着完成工具' --task links
```

`resume` 从 SDK 重新读取配置必需记录和当前任务，默认生成新的 `runs/resume-*/`。
`--require` 只能追加必需记录；`--max-facts` 和 `--max-tokens` 同时覆盖事实与任务记录。
任务数量较多时用 `--task ID` 聚焦，或增加上下文预算。缺失与预算排除分别显示，退出码为 2。
任务 ID 已存在时 `task add` 拒绝创建第二份；`task list` 显示当前版本和验收证据是否仍匹配。

### 可选 Kimi 建议

```bash
.venv/bin/python handoff.py doctor --model kimi
# 使用 MOONSHOT_API_KEY 环境变量，或以下隐藏输入；不保存密钥。
.venv/bin/python handoff.py resume '下一步应该做什么？' --task links --model kimi --prompt-key
```

复用 Kimi K2.6 客户端，关闭思考，每次最多 256 输出 tokens，失败不自动重试。
只发送选中的事实（包括来源和记录时间）、任务 ID/标题/状态/版本/验收状态、由应用生成的带版本完成命令、项目标识和问题；
不发送数据库路径、完整配置、文件内容或验收日志。事实中主动记录的内容会发送给模型。
必需信息不完整、没有任务、任务证据已变或选中任务全部完成时在本地返回，不产生模型请求。
`doctor --model kimi` 只检查环境变量是否存在；使用隐藏输入的用户可以在 `resume` 时提供凭据。

模型只能为一个当前待办任务返回 `{task_id, next_step, reason}` 建议。结构错误、过长、截断、
引用已完成任务或运行期间状态改变均不能成为有效提案。自然语言建议仍需使用者判断，结构校验
不证明建议正确。建议不执行命令，也不修改任务完成状态。

每次保存 `snapshot.json`、`model-response.json`、`planning.json`、`attempt.json` 和 `handoff.md`：输入快照、原始响应、
校验结果、用量及模型时延都可复核。`model-response.json` 是尚未复核当前状态的原始候选；只有最终 `planning.json`/`attempt.json` 记录有效或撤销的结果。请求失败后的未知用量不会记为零。只有明确提供单价后才能
估算费用；这些记录本身不是账单。全部完成时的零请求是应用状态检查，不是新增的模型测试。

### 验证、写回和再次接手

按建议完成实际工作后，在新进程运行：

```bash
.venv/bin/python handoff.py task complete links --revision 1
.venv/bin/python handoff.py resume '还有什么需要做？' --task links --model kimi
```

`complete` 仅运行登记时绑定的本地 Python 验收脚本，使用当前解释器与项目工作目录。
这是你主动登记并信任的代码，运行权限等同于当前进程；它应自行管理启动的子进程。
超时默认 60 秒，可用 `--timeout` 调整。脚本返回 0、交付文件哈希稳定、配置和项目记录未变，
应用才先保存验收证据，再写回 `done` 和新版本。验收失败保留日志，任务继续待办，退出码为 2。
输出明确标注同一数据库与 scope 的写回位置；该状态不是人工批准。

重复完成已完成任务不会重跑脚本；过期 `--revision` 被拒绝。重新恢复时会检查冻结验收脚本、
交付文件和验收回执的哈希。交付文件改变后须先检查原因，再明确重新打开任务：

```bash
.venv/bin/python handoff.py task reopen links --revision 2 --reason '需求变更，重新验收'
.venv/bin/python handoff.py task complete links --revision 3
```

本应用面向一位维护者的小项目，每个 scope 最多 50 项任务。配置模式的 CLI 使用数据库旁的
互斥文件协调写操作；这不提供 SDK 私有事实的原子 CAS，也不保护绕过本应用的直接写入。
崩溃可能留下 `.handoff-lock`；先确认相关进程已结束，再移除该锁文件。验收结果由本地文件
支持，未签名；验收脚本的覆盖度决定完成判断的可信范围。没有长期并发服务可靠性承诺。
