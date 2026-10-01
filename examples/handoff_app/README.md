# 独立 Python 项目交接应用

一个只依赖 MemWeft **公开 Python SDK** 的离线下游应用。保存明确的项目事实及其来源，
关闭进程后恢复任务，并将缺失或因预算排除的必需记录明确显示出来。没有模型调用、API key、
仓库模块导入或后台任务；它直接展示已保存记录，不自动推断事实或替使用者判断任务成功。

## 在仓库外运行

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
