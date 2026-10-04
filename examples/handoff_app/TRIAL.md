# 两天真实任务试用

请用自己的一个小型开发任务体验“保存进度，次日继续”。预计分两次处理，已有明确交付文件与
可执行验收检查，首次先限 **macOS arm64 / CPython 3.11** 的已核验 wheel。参与者应未参与 MemWeft
开发；目前准备的是试用流程，不代表已经有人完成。日常使用默认离线，无需模型密钥。

## 第一天：接入并保存真实进度

开始前复制 [反馈表](FEEDBACK.md)，记下时间与平时使用的交接方式。使用维护者提供的试用包，
核对包旁的 SHA256 和 `sdk/build-record.json`。固定 wheel 的源码提交为 `1ee6b3b`，
本轮应用及资料的文件哈希列在包内 `MANIFEST.sha256`。它们是不同的版本身份。

在自己的小项目根目录执行，把 `trial_bundle` 换成解压后的交付包目录；不需要 MemWeft 源码。
如已有 `.memweft-trial/` 或 `memweft-trial.json`，先换一个试用目录，避免覆盖已有记录。

```sh
trial_bundle="/path/to/extracted-trial-bundle"
mkdir .memweft-trial
cp -R "$trial_bundle/app" .memweft-trial/app
cp .memweft-trial/app/FEEDBACK.md .memweft-trial/feedback.md
python3.11 -m venv .memweft-trial/.venv
.memweft-trial/.venv/bin/python -m pip install --no-index --no-deps "$trial_bundle"/sdk/*.whl
.memweft-trial/.venv/bin/python .memweft-trial/app/handoff.py doctor > .memweft-trial/doctor.json
.memweft-trial/.venv/bin/python .memweft-trial/app/handoff.py \
  --config memweft-trial.json --project pilot --db .memweft-trial/handoff.db \
  init --runs-dir .memweft-trial/runs \
  --require goal --require constraints --require progress --require next_step
```

下面的快捷函数只在当前终端有效。初始化后运行配置检查，再保存自己的目标和约束。
引号中的内容是占位说明，执行前替换成实际内容；来源写真实出处。

```sh
handoff() {
  .memweft-trial/.venv/bin/python .memweft-trial/app/handoff.py --config memweft-trial.json "$@"
}
handoff doctor
handoff remember goal '替换为这次任务要交付什么' --source '任务来源'
handoff remember constraints '替换为必须保留的约束和限制' --source '需求或项目文档'
```

准备真实的 Python 验收脚本：它可以调用现有测试命令，失败须返回非零退出码，不能修改交付文件。
不要使用无检查直接返回 0 的脚本。下列标题和文件名也要换成自己的；多个交付文件可重复 `--artifact`。
应用会冻结验收脚本，后续完成时实际运行它。

```sh
handoff task add pilot '替换为任务标题' --verify verify_task.py --artifact solution.py
```

照常开发，到自然停点保存进度，无需为了演示人为制造缺陷：

```sh
handoff remember progress '替换为已完成内容、实际检查结果和证据路径' --source '本次开发记录'
handoff remember next_step '替换为剩余工作、阻碍及下一步' --source '本次开发记录'
handoff resume '交给下一次会话继续' --task pilot --max-facts 12 --max-tokens 4000 --out .memweft-trial/runs/day1
```

阅读生成的 `handoff.md`，记录遗漏或错误；有问题就更新对应记录并导出到新的目录。
退出终端/开发会话。不要把 `.memweft-trial/`、`memweft-trial.json` 自动提交到自己的项目；
按项目习惯在本地 Git 排除项中排除它们。完整业务数据库无需提交给维护者。

## 第二天：从新会话恢复并完成

在同一项目根目录打开新终端，重新定义上面的 `handoff` 函数。开始记录恢复时间，运行：

```sh
handoff resume '继续昨天的真实任务' --task pilot --max-facts 12 --max-tokens 4000 --out .memweft-trial/runs/day2
```

仅根据项目文件与新交接确认目标、关键约束和下一步；若由新开发代理接手，不粘贴旧聊天作为额外提示。
能正确说清这三项时停止计时。记录需要重新输入的约束、找回的资料及向维护者求助的次数。
需要求助就如实求助，记录缺口；不要为了通过试用补填“没有问题”。

完成实际工作后先查看当前任务版本，再用输出中的 `revision` 完成。首次未变更的任务通常是 1：

```sh
handoff task list
handoff task complete pilot --revision 1
```

失败时保存报错和验收日志、修复后重试；成功后再运行 `task list` 确认 `done` 和 `passed`。
把 `progress` 更新为实际结果、`next_step` 更新为剩余事项，再导出至 `runs/final` 等新目录。
完成状态表示机器验收通过，使用者是否满意单独填入反馈表。可选模型使用见 [应用指南](README.md#可选-kimi-建议)。

## 本轮验收与反馈

至少一位参与者独立走完“安装 → 保存 → 次日恢复 → 实际任务验收”，已保存的关键约束无需重输，
且任务没有被错误标记完成。维护者协助和助手参与均需记录；维护者代写实现的任务不算独立完成。
未达标同样保留结果，不把失败者从统计里去掉。

提交填写后的 [反馈表](FEEDBACK.md) 和愿意分享的必要报错即可，不需要上传密钥或完整项目。
与平时交接方式比较恢复耗时、求助、重复输入和遗漏；没有基线就填“未测”。1–2 人的观察用于找问题，
不能直接推算效率提升比例或证明记忆功能的因果收益。
