# 工作流三组对照：完整历史、确定性摘要与 MemWeft

模型 `kimi-k2.6`，非思考模式；运行开始于 `2026-09-30T09:33:39.837214+00:00`。
完整20题 × 3组，共60次调用；逐条成绩已重新计算，冻结输入、请求、用量和原始汇总已核对。

## 分组结果

| 组别 | 通过 | 协议有效 | 输入 tokens | 输出 tokens | 总 tokens | 估算费用（元） | 上下文+HTTP 中位数（ms） |
|---|---:|---:|---:|---:|---:|---:|---:|
| full_history | 18/20 | 20/20 | 21551 | 497 | 22048 | 0.153501 | 1659.495 |
| current_state_summary | 19/20 | 20/20 | 15761 | 497 | 16258 | 0.115865 | 1533.886 |
| memweft | 16/20 | 19/20 | 8937 | 497 | 9434 | 0.071510 | 1461.612 |

合计估算费用 **¥0.340876**；按[官方价格](https://platform.kimi.com/docs/pricing/chat)计算，未计缓存优惠，未经账单核对。

## 逐题结果

| 场景 | 类别 | 完整历史 | 确定性摘要 | MemWeft | MemWeft 遗漏的当前主事实键 |
|---|---|---|---|---|---|
| wf-handoff-render-bundle | project_handoff | 通过 | 通过 | 通过 | 无 |
| wf-handoff-consumer-contract | project_handoff | 通过 | 通过 | 通过 | 无 |
| wf-handoff-shared-lab-slot | project_handoff | 通过 | 通过 | 通过 | 无 |
| wf-handoff-field-kit-shortfall | project_handoff | 通过 | 通过 | 通过 | 无 |
| wf-troubleshoot-report-query | incident_triage | 通过 | 通过 | 通过 | 无 |
| wf-troubleshoot-tls-window | incident_triage | 通过 | 通过 | 未通过 | tls_observation_time |
| wf-troubleshoot-replayed-charge | incident_triage | 通过 | 通过 | 通过 | 无 |
| wf-troubleshoot-cache-generation | incident_triage | 通过 | 通过 | 通过 | 无 |
| wf-support-partial-shipment-refund | support_resolution | 通过 | 通过 | 未通过 | order_item_price |
| wf-support-forgotten-recipient | support_resolution | 通过 | 通过 | 通过 | 无 |
| wf-support-warranty-routing | support_resolution | 通过 | 通过 | 通过 | 无 |
| wf-support-future-discount | support_resolution | 通过 | 通过 | 通过 | 无 |
| wf-requirement-offline-scope-change | requirement_change | 通过 | 通过 | 未通过 | 无 |
| wf-requirement-client-migration | requirement_change | 通过 | 通过 | 通过 | 无 |
| wf-requirement-audit-retention | requirement_change | 未通过 | 未通过 | 通过 | 无 |
| wf-requirement-locale-readiness | requirement_change | 通过 | 通过 | 通过 | 无 |
| wf-plan-uninterrupted-maintenance | planning_constraints | 未通过 | 通过 | 未通过 | 无 |
| wf-plan-vendor-latency-budget | planning_constraints | 通过 | 通过 | 通过 | 无 |
| wf-plan-parallel-critical-path | planning_constraints | 通过 | 通过 | 通过 | 无 |
| wf-plan-arrival-with-transfer | planning_constraints | 通过 | 通过 | 通过 | 无 |

## 失败记录

### wf-troubleshoot-tls-window / memweft

判定：`wrong_values:action,expired_minutes`；结束原因：`stop`。

原始回答（以 JSON 字符串保留）：

    "{\"action\":\"sync_clock\",\"expired_minutes\":0,\"owner\":\"cert-desk-27\"}"

期望答案：

    {
      "action": "renew_certificate",
      "expired_minutes": 25,
      "owner": "cert-desk-27"
    }

该题 MemWeft 遗漏的当前主事实键：

    [
      "tls_observation_time"
    ]

遗漏诊断：

    {
      "tls_observation_time": [
        "max_facts"
      ]
    }

遗漏不等于已确认的失败原因；基线失败项中此处仅描述配对的 MemWeft 上下文。

### wf-support-partial-shipment-refund / memweft

判定：`wrong_values:refund_amount,currency`；结束原因：`stop`。

原始回答（以 JSON 字符串保留）：

    "{\"refund_amount\":null,\"currency\":null,\"needs_manual_review\":false}"

期望答案：

    {
      "refund_amount": 72,
      "currency": "EUR",
      "needs_manual_review": false
    }

该题 MemWeft 遗漏的当前主事实键：

    [
      "order_item_price"
    ]

遗漏诊断：

    {
      "order_item_price": [
        "max_facts"
      ]
    }

遗漏不等于已确认的失败原因；基线失败项中此处仅描述配对的 MemWeft 上下文。

### wf-requirement-offline-scope-change / memweft

判定：`wrong_values:unfinished_offline_blocks_release`；结束原因：`stop`。

原始回答（以 JSON 字符串保留）：

    "{\"include_offline\":false,\"include_pdf_export\":true,\"unfinished_offline_blocks_release\":true}"

期望答案：

    {
      "include_offline": false,
      "include_pdf_export": true,
      "unfinished_offline_blocks_release": false
    }

该题 MemWeft 遗漏的当前主事实键：

    []

遗漏诊断：

    {}

遗漏不等于已确认的失败原因；基线失败项中此处仅描述配对的 MemWeft 上下文。

### wf-requirement-audit-retention / full_history

判定：`wrong_values:retention_days`；结束原因：`stop`。

原始回答（以 JSON 字符串保留）：

    "{\"can_accept\":false,\"retention_days\":7,\"additional_days\":23,\"owner\":\"compliance-config-11\"}"

期望答案：

    {
      "can_accept": false,
      "retention_days": 30,
      "additional_days": 23,
      "owner": "compliance-config-11"
    }

该题 MemWeft 遗漏的当前主事实键：

    []

遗漏诊断：

    {}

遗漏不等于已确认的失败原因；基线失败项中此处仅描述配对的 MemWeft 上下文。

### wf-requirement-audit-retention / current_state_summary

判定：`wrong_values:retention_days`；结束原因：`stop`。

原始回答（以 JSON 字符串保留）：

    "{\"can_accept\":false,\"retention_days\":7,\"additional_days\":23,\"owner\":\"compliance-config-11\"}"

期望答案：

    {
      "can_accept": false,
      "retention_days": 30,
      "additional_days": 23,
      "owner": "compliance-config-11"
    }

该题 MemWeft 遗漏的当前主事实键：

    []

遗漏诊断：

    {}

遗漏不等于已确认的失败原因；基线失败项中此处仅描述配对的 MemWeft 上下文。

### wf-plan-uninterrupted-maintenance / full_history

判定：`wrong_values:end_time`；结束原因：`stop`。

原始回答（以 JSON 字符串保留）：

    "{\"window\":\"window-b28\",\"start_time\":\"13:00\",\"end_time\":\"14:00\",\"unused_minutes\":15}"

期望答案：

    {
      "window": "window-b28",
      "start_time": "13:00",
      "end_time": "13:45",
      "unused_minutes": 15
    }

该题 MemWeft 遗漏的当前主事实键：

    []

遗漏诊断：

    {}

遗漏不等于已确认的失败原因；基线失败项中此处仅描述配对的 MemWeft 上下文。

### wf-plan-uninterrupted-maintenance / memweft

判定：`wrong_values:window,start_time,end_time,unused_minutes`；结束原因：`stop`。

原始回答（以 JSON 字符串保留）：

    "{\"window\":\"window-a17\",\"start_time\":\"09:00\",\"end_time\":\"09:30\",\"unused_minutes\":0}"

期望答案：

    {
      "window": "window-b28",
      "start_time": "13:00",
      "end_time": "13:45",
      "unused_minutes": 15
    }

该题 MemWeft 遗漏的当前主事实键：

    []

遗漏诊断：

    {}

遗漏不等于已确认的失败原因；基线失败项中此处仅描述配对的 MemWeft 上下文。


## 范围与解释

- 新合成工作流基准，不是客户任务或长期业务收益证据；20题、每题每组仅观察一次。
- 三组采用相同源事件、作用域隔离和遗忘语义；完整历史保留更新前记录，但移除被遗忘键的历史值。
- current_state_summary 是不看问题的确定性最新状态摘要，不调用 LLM，不代表 LLM 摘要器的效果或成本。
- MemWeft 使用当前问题检索，最多8条事实、1200估算tokens；基线完整保留可见历史或最新状态，因此这是上下文策略组合的对照。
- 每题增加16条相同无关档案事实；事实由应用显式写入，没有自动抽取或策略学习。
- 时间为预先测量的上下文构建时间加本次模型HTTP时间；不包含历史写入、数据准备、限流等待，不是完整端到端时延。
- 每次最多192输出tokens；16000字节限制只计算messages JSON，不是完整HTTP请求体。累计已报告tokens阈值只阻止后续调用。
- 费用按2026-09-30公布的每百万输入¥6.50、输出¥27估算，未计缓存优惠，未经账单核对，不是货币硬上限。
- 失败原因与遗漏事实是观察结果，不能据此确认因果；单次观测不能证明统计显著性、模型普遍能力或生产SLO。

## 证据校验

题库 SHA-256：`1c97c7e387f4772d81dbc1282b3bd1401b246b4014635f92dae029d630a14d29`。
已验证 9 个记录的源文件哈希；输入、请求、60个唯一配对、成绩与汇总一致。
原生二进制哈希由运行器在开始时记录；报告器不重跑原生代码，也不独立证明二进制来源。

补充源码快照采集于 `2026-09-30T09:35:08.980931+00:00`，属于运行期间补记；仅原始 metadata 中列出的文件具备调用前哈希记录。补记说明保留于 JSON 报告。
