# 修改后验证，再接受完成

[VerifiedSession 示例](../examples/verified_completion.py)让应用执行器检查完成条件：
可信验证工具必须检查过当前产物，验证后的文件快照必须仍然一致，本轮也不能有工具或协议错误。
模型输出的 `done=true` 是完成请求；应用只在本轮返回 `done_accepted is True` 时触发一次完成处理。
此时会话进入 `status="completed"`。状态表示会话曾经结束，不是可反复使用的交付授权。
这是可选的应用执行器示例，不改变 MemWeft SDK 的默认行为。

应用用 `TrustedTool(handler, effect=...)` 注册工具，`handler` 接收包含 `tool` 的完整操作对象。
工具名、回调及其类别均由应用代码定义，不能由模型、记忆或待处理文档提供。

| `effect` | 执行器如何处理验证回执 |
|---|---|
| `read` | 保留既有回执；应用应确保回调只读。 |
| `mutate` | 调用前撤销回执，包括失败或只完成部分修改的调用。 |
| `verify` | 调用前撤销回执；只有回调返回字典且 `ok is True` 才能为当前快照建立新回执。 |

修改后需要再次成功验证。验证成功后又修改文件，也需要再次验证。
验证回调的 `ok` 必须来自应用实际运行的检查，模型自行写入的成功说明不能建立回执。
`file_snapshot(root)`记录隔离目录内的文件路径和内容字节哈希，并拒绝符号链接。
它能发现快照覆盖范围内的文件变化，但哈希本身不说明文件内容或业务决策正确。
空目录、权限和时间戳不在这个内容快照内；检查若依赖这些信息或目录外的来源版本，
应用应提供覆盖相应状态的 `snapshot` 回调。

下面的完整示例可在仓库根目录执行，只使用临时目录和模拟模型回复，不调用 API：

```python
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path("examples").resolve()))
from verified_completion import TrustedTool, VerifiedSession, file_snapshot

with TemporaryDirectory() as directory:
    root = Path(directory)

    def write_settings(action):
        if set(action) != {"tool", "enabled"} or type(action["enabled"]) is not bool:
            raise ValueError("Expected only tool and boolean enabled")
        (root / "settings.json").write_text(
            json.dumps({"enabled": action["enabled"]}), encoding="utf-8"
        )
        return {"written": "settings.json"}

    def check_settings(action):
        if set(action) != {"tool"}:
            raise ValueError("The check takes no arguments")
        value = json.loads((root / "settings.json").read_text(encoding="utf-8"))
        # A small application-owned rule for this demonstration.
        ok = isinstance(value, dict) and set(value) == {"enabled"} and value["enabled"] is True
        return {"ok": ok}

    session = VerifiedSession(
        tools={
            "write_settings": TrustedTool(write_settings, effect="mutate"),
            "check_settings": TrustedTool(check_settings, effect="verify"),
        },
        snapshot=lambda: file_snapshot(root),
        max_rounds=4,
        max_actions=3,
    )
    first = session.step(
        '{"actions":[{"tool":"write_settings","enabled":true}],"done":true}'
    )
    assert first["status"] == "active" and not first["done_accepted"]

    second = session.step('{"actions":[{"tool":"check_settings"}],"done":true}')
    assert second["status"] == "completed" and second["done_accepted"]
```

回调负责参数校验、路径白名单、权限和真实工具执行。示例的写入目标固定为 `settings.json`；
换成接收模型提供的路径时，应用必须另行限制可读写范围。
`effect` 是执行器对可信回调的约定，不能把任意模型代码包装成工具就获得这些保证。

接入模型时，每个任务创建一个新的 `VerifiedSession`。
每轮将原始回复传给 `step(content, finish_reason=...)`，不要从文本中提取或修补一段 JSON 后再传入。
回复契约是 `{"actions":[...],"done":false}`，操作上限和轮数由应用构造会话时确定。

| 返回字段 | 接入方式 |
|---|---|
| `results` | 每个已处理操作的工具结果或错误，反馈给模型。 |
| `completion.verified` / `completion.reason` | 验证状态及说明，反馈给模型；不能单独作为结束依据。 |
| `done_requested` / `done_accepted` | 区分模型提出完成与本轮接受完成；只用后者触发一次完成处理。 |
| `protocol_error` | 表示本轮回复不满足协议；保留并反馈错误。 |
| `rounds_remaining` | 剩余轮数，反馈给模型并遵守该预算。 |
| `status` | `active` 继续；`completed` / `budget_exhausted` 是终态，分别表示曾接受完成 / 预算耗尽。 |

把已有模型客户端接入以下循环；这里的 `call_model` 代表应用自身的调用函数，
`messages` 和新的 `session` 由应用初始化：

```python
while True:
    reply = call_model(messages)
    outcome = session.step(reply["content"], finish_reason=reply["finish_reason"])
    messages.extend([
        {"role": "assistant", "content": reply["content"]},
        {"role": "user", "content": json.dumps({
            "tool_results": outcome["results"],
            "completion": outcome["completion"],
            "protocol_error": outcome["protocol_error"],
            "done_accepted": outcome["done_accepted"],
            "rounds_remaining": outcome["rounds_remaining"],
        }, ensure_ascii=False)},
    ])
    if outcome["done_accepted"] is True:
        # Handle completion once. This does not authorize external side effects.
        break
    if outcome["status"] != "active":
        raise RuntimeError("Task did not meet completion conditions within its budget")
```

拒绝完成后，必须把反馈交给模型，并让它自行选择下一步工具操作。
执行器不会自动调用验证、修复文件、替模型改答案或增加预算。
最后一轮仍未获接受时，返回 `budget_exhausted`；不要通过自动新建会话继续同一次尝试。
本轮发生工具错误、验证失败或协议错误时，即使稍后又成功验证，也不接受本轮完成请求。
若还有预算，模型可以在下一轮根据反馈继续；应用仍需检查该轮返回的状态。
轮数和操作数限制不等于 API 请求或费用上限；模型客户端的 token、重试和费用预算仍需单独设置。

终态后再次调用 `step` 不执行工具、不消耗预算，也不产生新的完成事件：`done_accepted` 为 false。
即使状态仍是 `completed`，随后外部文件变化也会使返回的 `completion.verified` 变为 false。
应用不能仅凭历史终态重复交付，也不能将进程内回执当作文件以后永远不变的保证。

这份回执只保存在当前进程的会话对象中，不提供持久化或跨进程恢复。
示例面向串行操作的隔离目录；快照检查与实际交付之间没有并发事务、锁或原子发布保证。
外部 API、数据库、目录外文件等副作用也不会自动进入文件快照。
验证应覆盖应用真正要交付的产物与条件，相关权限、业务正确性和并发控制仍由应用实现。

[多步任务适配器](../evals/verified_multistep.py)将既有公共文件工具接到这个完成条件上。
[旧回复离线回放](../evals/replay_verified_completion.py)只能观察已记录动作和完成请求在新条件下的接受或拒绝情况。
旧回复没有读到新的反馈，回放不能推断模型会如何补做验证，也不能计为新的模型成功率。
既有 [v2 多步协议](multistep_memory_protocol.md)、提示、主评分和 60 个最终任务结果保持原口径。
本轮[离线回放记录](../evals/reports/2026-10-07-verified-completion.md)包含逐例完成状态、剩余预算和来源哈希。
