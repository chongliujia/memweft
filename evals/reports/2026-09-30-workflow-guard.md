# 显式必需事实与应用规则校验：重复对照

模型 `kimi-k2.6`，非思考模式；运行开始于 `2026-09-30T10:11:05.922644+00:00`。
8题 × 2组 × 2次，共32次模型观测。原始回答与校验结果分别计数，没有执行业务动作。

## 质量与拒绝

| 指标 | lexical | required_facts |
|---|---:|---:|
| 原始正确 / 可答观测 | 6/14 | 13/14 |
| 校验后正确 / 可答观测 | 6/14 | 13/14 |
| 错误接受 | 0 | 0 |
| 错误拦截 | 8 | 1 |
| 正确但被拒绝 | 0 | 0 |
| 输入完整仍误拒 | 0 | 0 |
| 缺数据拒绝 / 缺数据观测 | 2/2 | 2/2 |
| 输出协议有效 / 全部观测 | 13/16 | 14/16 |

缺数据拒绝单独统计，不计为任务答对；错误拦截也不增加校验后正确数。

## 用量与时间

| 组别 | 输入 tokens | 输出 tokens | 总 tokens | 估算费用（元） | 上下文+HTTP中位数（ms） |
|---|---:|---:|---:|---:|---:|
| lexical | 8188 | 390 | 8578 | 0.063752 | 1240.041 |
| required_facts | 8174 | 391 | 8565 | 0.063688 | 1314.337 |

估算合计 **¥0.127440**；按[官方价格](https://platform.kimi.com/docs/pricing/chat)计算，未计缓存优惠，未经账单核对。

## 每题两次原始结果与校验

| 场景 | lexical 第1次 | lexical 第2次 | required 第1次 | required 第2次 |
|---|---|---|---|---|
| guard-tls-clock-offset-cn | 原始错误/拒绝 | 原始错误/拒绝 | 原始正确/接受 | 原始正确/接受 |
| guard-tls-expired-certificate-en | 原始正确/接受 | 原始正确/接受 | 原始正确/接受 | 原始正确/接受 |
| guard-refund-updated-dispatch-cn | 原始错误/拒绝 | 原始错误/拒绝 | 原始正确/接受 | 原始正确/接受 |
| guard-refund-window-expired-en | 原始正确/接受 | 原始正确/接受 | 原始正确/接受 | 原始正确/接受 |
| guard-feature-offline-unready-cn | 原始错误/拒绝 | 原始错误/拒绝 | 原始正确/接受 | 原始正确/接受 |
| guard-feature-ready-unrequested-en | 原始正确/接受 | 原始正确/接受 | 原始正确/接受 | 原始正确/接受 |
| guard-maintenance-least-unused-cn | 原始错误/拒绝 | 原始错误/拒绝 | 原始正确/接受 | 原始错误/拒绝 |
| guard-maintenance-withdrawn-windows-en | 缺数题/拒绝 | 缺数题/拒绝 | 缺数题/拒绝 | 缺数题/拒绝 |

## 全部原始回答与校验记录

下列记录保留全部32个原始回答，包括通过、失败、猜中但被拒绝以及缺数据提案；诊断不替代因果证据。

### guard-tls-clock-offset-cn / lexical / 第1次

原始错误/拒绝；原始判定 `wrong_values:action`。

原始内容（JSON字符串）：

    "{\"action\":\"no_action\",\"expired_minutes\":0,\"owner\":\"clockwatch-desk-44\"}"

固定期望（null 表示需要补数据，没有可评分业务答案）：

    {
      "action": "synchronize_clock",
      "expired_minutes": 0,
      "owner": "clockwatch-desk-44"
    }

应用校验：

    {
      "accepted": false,
      "errors": [
        "facts.clock.observation.missing",
        "facts.host.clock.missing"
      ]
    }

上下文缺少的必需事实键：

    [
      "clock.observation",
      "host.clock"
    ]

其中源中存在但未进入上下文的键：

    [
      "clock.observation",
      "host.clock"
    ]

### guard-tls-clock-offset-cn / lexical / 第2次

原始错误/拒绝；原始判定 `wrong_values:action`。

原始内容（JSON字符串）：

    "{\"action\":\"renew_certificate\",\"expired_minutes\":0,\"owner\":\"clockwatch-desk-44\"}"

固定期望（null 表示需要补数据，没有可评分业务答案）：

    {
      "action": "synchronize_clock",
      "expired_minutes": 0,
      "owner": "clockwatch-desk-44"
    }

应用校验：

    {
      "accepted": false,
      "errors": [
        "facts.clock.observation.missing",
        "facts.host.clock.missing"
      ]
    }

上下文缺少的必需事实键：

    [
      "clock.observation",
      "host.clock"
    ]

其中源中存在但未进入上下文的键：

    [
      "clock.observation",
      "host.clock"
    ]

### guard-tls-clock-offset-cn / required_facts / 第1次

原始正确/接受；原始判定 `pass`。

原始内容（JSON字符串）：

    "{\"action\":\"synchronize_clock\",\"expired_minutes\":0,\"owner\":\"clockwatch-desk-44\"}"

固定期望（null 表示需要补数据，没有可评分业务答案）：

    {
      "action": "synchronize_clock",
      "expired_minutes": 0,
      "owner": "clockwatch-desk-44"
    }

应用校验：

    {
      "accepted": true,
      "errors": []
    }

上下文缺少的必需事实键：

    []

其中源中存在但未进入上下文的键：

    []

### guard-tls-clock-offset-cn / required_facts / 第2次

原始正确/接受；原始判定 `pass`。

原始内容（JSON字符串）：

    "{\"action\":\"synchronize_clock\",\"expired_minutes\":0,\"owner\":\"clockwatch-desk-44\"}"

固定期望（null 表示需要补数据，没有可评分业务答案）：

    {
      "action": "synchronize_clock",
      "expired_minutes": 0,
      "owner": "clockwatch-desk-44"
    }

应用校验：

    {
      "accepted": true,
      "errors": []
    }

上下文缺少的必需事实键：

    []

其中源中存在但未进入上下文的键：

    []

### guard-tls-expired-certificate-en / lexical / 第1次

原始正确/接受；原始判定 `pass`。

原始内容（JSON字符串）：

    "{\"action\":\"renew_certificate\",\"expired_minutes\":48,\"owner\":\"edge-certistry-73\"}"

固定期望（null 表示需要补数据，没有可评分业务答案）：

    {
      "action": "renew_certificate",
      "expired_minutes": 48,
      "owner": "edge-certistry-73"
    }

应用校验：

    {
      "accepted": true,
      "errors": []
    }

上下文缺少的必需事实键：

    []

其中源中存在但未进入上下文的键：

    []

### guard-tls-expired-certificate-en / lexical / 第2次

原始正确/接受；原始判定 `pass`。

原始内容（JSON字符串）：

    "{\"action\":\"renew_certificate\",\"expired_minutes\":48,\"owner\":\"edge-certistry-73\"}"

固定期望（null 表示需要补数据，没有可评分业务答案）：

    {
      "action": "renew_certificate",
      "expired_minutes": 48,
      "owner": "edge-certistry-73"
    }

应用校验：

    {
      "accepted": true,
      "errors": []
    }

上下文缺少的必需事实键：

    []

其中源中存在但未进入上下文的键：

    []

### guard-tls-expired-certificate-en / required_facts / 第1次

原始正确/接受；原始判定 `pass`。

原始内容（JSON字符串）：

    "{\"action\":\"renew_certificate\",\"expired_minutes\":48,\"owner\":\"edge-certistry-73\"}"

固定期望（null 表示需要补数据，没有可评分业务答案）：

    {
      "action": "renew_certificate",
      "expired_minutes": 48,
      "owner": "edge-certistry-73"
    }

应用校验：

    {
      "accepted": true,
      "errors": []
    }

上下文缺少的必需事实键：

    []

其中源中存在但未进入上下文的键：

    []

### guard-tls-expired-certificate-en / required_facts / 第2次

原始正确/接受；原始判定 `pass`。

原始内容（JSON字符串）：

    "{\"action\":\"renew_certificate\",\"expired_minutes\":48,\"owner\":\"edge-certistry-73\"}"

固定期望（null 表示需要补数据，没有可评分业务答案）：

    {
      "action": "renew_certificate",
      "expired_minutes": 48,
      "owner": "edge-certistry-73"
    }

应用校验：

    {
      "accepted": true,
      "errors": []
    }

上下文缺少的必需事实键：

    []

其中源中存在但未进入上下文的键：

    []

### guard-refund-updated-dispatch-cn / lexical / 第1次

原始错误/拒绝；原始判定 `wrong_values:refund_amount,currency,needs_manual_review`。

原始内容（JSON字符串）：

    "{\"refund_amount\":0,\"currency\":\"CNY\",\"needs_manual_review\":true}"

固定期望（null 表示需要补数据，没有可评分业务答案）：

    {
      "refund_amount": 2190,
      "currency": "USD",
      "needs_manual_review": false
    }

应用校验：

    {
      "accepted": false,
      "errors": [
        "facts.refund.policy.missing",
        "facts.order.state.missing",
        "facts.price.unit.missing"
      ]
    }

上下文缺少的必需事实键：

    [
      "refund.policy",
      "order.state",
      "price.unit"
    ]

其中源中存在但未进入上下文的键：

    [
      "refund.policy",
      "order.state",
      "price.unit"
    ]

### guard-refund-updated-dispatch-cn / lexical / 第2次

原始错误/拒绝；原始判定 `wrong_values:refund_amount,currency,needs_manual_review`。

原始内容（JSON字符串）：

    "{\"refund_amount\":0,\"currency\":\"CNY\",\"needs_manual_review\":true}"

固定期望（null 表示需要补数据，没有可评分业务答案）：

    {
      "refund_amount": 2190,
      "currency": "USD",
      "needs_manual_review": false
    }

应用校验：

    {
      "accepted": false,
      "errors": [
        "facts.refund.policy.missing",
        "facts.order.state.missing",
        "facts.price.unit.missing"
      ]
    }

上下文缺少的必需事实键：

    [
      "refund.policy",
      "order.state",
      "price.unit"
    ]

其中源中存在但未进入上下文的键：

    [
      "refund.policy",
      "order.state",
      "price.unit"
    ]

### guard-refund-updated-dispatch-cn / required_facts / 第1次

原始正确/接受；原始判定 `pass`。

原始内容（JSON字符串）：

    "{\"refund_amount\":2190,\"currency\":\"USD\",\"needs_manual_review\":false}"

固定期望（null 表示需要补数据，没有可评分业务答案）：

    {
      "refund_amount": 2190,
      "currency": "USD",
      "needs_manual_review": false
    }

应用校验：

    {
      "accepted": true,
      "errors": []
    }

上下文缺少的必需事实键：

    []

其中源中存在但未进入上下文的键：

    []

### guard-refund-updated-dispatch-cn / required_facts / 第2次

原始正确/接受；原始判定 `pass`。

原始内容（JSON字符串）：

    "{\"refund_amount\":2190,\"currency\":\"USD\",\"needs_manual_review\":false}"

固定期望（null 表示需要补数据，没有可评分业务答案）：

    {
      "refund_amount": 2190,
      "currency": "USD",
      "needs_manual_review": false
    }

应用校验：

    {
      "accepted": true,
      "errors": []
    }

上下文缺少的必需事实键：

    []

其中源中存在但未进入上下文的键：

    []

### guard-refund-window-expired-en / lexical / 第1次

原始正确/接受；原始判定 `pass`。

原始内容（JSON字符串）：

    "{\"refund_amount\":0,\"currency\":\"EUR\",\"needs_manual_review\":true}"

固定期望（null 表示需要补数据，没有可评分业务答案）：

    {
      "refund_amount": 0,
      "currency": "EUR",
      "needs_manual_review": true
    }

应用校验：

    {
      "accepted": true,
      "errors": []
    }

上下文缺少的必需事实键：

    []

其中源中存在但未进入上下文的键：

    []

### guard-refund-window-expired-en / lexical / 第2次

原始正确/接受；原始判定 `pass`。

原始内容（JSON字符串）：

    "{\"refund_amount\":0,\"currency\":\"EUR\",\"needs_manual_review\":true}"

固定期望（null 表示需要补数据，没有可评分业务答案）：

    {
      "refund_amount": 0,
      "currency": "EUR",
      "needs_manual_review": true
    }

应用校验：

    {
      "accepted": true,
      "errors": []
    }

上下文缺少的必需事实键：

    []

其中源中存在但未进入上下文的键：

    []

### guard-refund-window-expired-en / required_facts / 第1次

原始正确/接受；原始判定 `pass`。

原始内容（JSON字符串）：

    "{\"refund_amount\":0,\"currency\":\"EUR\",\"needs_manual_review\":true}"

固定期望（null 表示需要补数据，没有可评分业务答案）：

    {
      "refund_amount": 0,
      "currency": "EUR",
      "needs_manual_review": true
    }

应用校验：

    {
      "accepted": true,
      "errors": []
    }

上下文缺少的必需事实键：

    []

其中源中存在但未进入上下文的键：

    []

### guard-refund-window-expired-en / required_facts / 第2次

原始正确/接受；原始判定 `pass`。

原始内容（JSON字符串）：

    "{\"refund_amount\":0,\"currency\":\"EUR\",\"needs_manual_review\":true}"

固定期望（null 表示需要补数据，没有可评分业务答案）：

    {
      "refund_amount": 0,
      "currency": "EUR",
      "needs_manual_review": true
    }

应用校验：

    {
      "accepted": true,
      "errors": []
    }

上下文缺少的必需事实键：

    []

其中源中存在但未进入上下文的键：

    []

### guard-feature-offline-unready-cn / lexical / 第1次

原始错误/拒绝；原始判定 `wrong_values:include_pdf_export,unfinished_offline_blocks_release`。

原始内容（JSON字符串）：

    "{\"include_offline\": false, \"include_pdf_export\": false, \"unfinished_offline_blocks_release\": false}"

固定期望（null 表示需要补数据，没有可评分业务答案）：

    {
      "include_offline": false,
      "include_pdf_export": true,
      "unfinished_offline_blocks_release": true
    }

应用校验：

    {
      "accepted": false,
      "errors": [
        "facts.product.requirements.missing",
        "facts.implementation.readiness.missing"
      ]
    }

上下文缺少的必需事实键：

    [
      "product.requirements",
      "implementation.readiness"
    ]

其中源中存在但未进入上下文的键：

    [
      "product.requirements",
      "implementation.readiness"
    ]

### guard-feature-offline-unready-cn / lexical / 第2次

原始错误/拒绝；原始判定 `wrong_values:include_pdf_export,unfinished_offline_blocks_release`。

原始内容（JSON字符串）：

    "{\"include_offline\": false, \"include_pdf_export\": false, \"unfinished_offline_blocks_release\": false}"

固定期望（null 表示需要补数据，没有可评分业务答案）：

    {
      "include_offline": false,
      "include_pdf_export": true,
      "unfinished_offline_blocks_release": true
    }

应用校验：

    {
      "accepted": false,
      "errors": [
        "facts.product.requirements.missing",
        "facts.implementation.readiness.missing"
      ]
    }

上下文缺少的必需事实键：

    [
      "product.requirements",
      "implementation.readiness"
    ]

其中源中存在但未进入上下文的键：

    [
      "product.requirements",
      "implementation.readiness"
    ]

### guard-feature-offline-unready-cn / required_facts / 第1次

原始正确/接受；原始判定 `pass`。

原始内容（JSON字符串）：

    "{\"include_offline\":false,\"include_pdf_export\":true,\"unfinished_offline_blocks_release\":true}"

固定期望（null 表示需要补数据，没有可评分业务答案）：

    {
      "include_offline": false,
      "include_pdf_export": true,
      "unfinished_offline_blocks_release": true
    }

应用校验：

    {
      "accepted": true,
      "errors": []
    }

上下文缺少的必需事实键：

    []

其中源中存在但未进入上下文的键：

    []

### guard-feature-offline-unready-cn / required_facts / 第2次

原始正确/接受；原始判定 `pass`。

原始内容（JSON字符串）：

    "{\"include_offline\": false, \"include_pdf_export\": true, \"unfinished_offline_blocks_release\": true}"

固定期望（null 表示需要补数据，没有可评分业务答案）：

    {
      "include_offline": false,
      "include_pdf_export": true,
      "unfinished_offline_blocks_release": true
    }

应用校验：

    {
      "accepted": true,
      "errors": []
    }

上下文缺少的必需事实键：

    []

其中源中存在但未进入上下文的键：

    []

### guard-feature-ready-unrequested-en / lexical / 第1次

原始正确/接受；原始判定 `pass`。

原始内容（JSON字符串）：

    "{\"include_offline\":true,\"include_pdf_export\":false,\"unfinished_offline_blocks_release\":false}"

固定期望（null 表示需要补数据，没有可评分业务答案）：

    {
      "include_offline": true,
      "include_pdf_export": false,
      "unfinished_offline_blocks_release": false
    }

应用校验：

    {
      "accepted": true,
      "errors": []
    }

上下文缺少的必需事实键：

    []

其中源中存在但未进入上下文的键：

    []

### guard-feature-ready-unrequested-en / lexical / 第2次

原始正确/接受；原始判定 `pass`。

原始内容（JSON字符串）：

    "{\"include_offline\": true, \"include_pdf_export\": false, \"unfinished_offline_blocks_release\": false}"

固定期望（null 表示需要补数据，没有可评分业务答案）：

    {
      "include_offline": true,
      "include_pdf_export": false,
      "unfinished_offline_blocks_release": false
    }

应用校验：

    {
      "accepted": true,
      "errors": []
    }

上下文缺少的必需事实键：

    []

其中源中存在但未进入上下文的键：

    []

### guard-feature-ready-unrequested-en / required_facts / 第1次

原始正确/接受；原始判定 `pass`。

原始内容（JSON字符串）：

    "{\"include_offline\":true,\"include_pdf_export\":false,\"unfinished_offline_blocks_release\":false}"

固定期望（null 表示需要补数据，没有可评分业务答案）：

    {
      "include_offline": true,
      "include_pdf_export": false,
      "unfinished_offline_blocks_release": false
    }

应用校验：

    {
      "accepted": true,
      "errors": []
    }

上下文缺少的必需事实键：

    []

其中源中存在但未进入上下文的键：

    []

### guard-feature-ready-unrequested-en / required_facts / 第2次

原始正确/接受；原始判定 `pass`。

原始内容（JSON字符串）：

    "{\"include_offline\":true,\"include_pdf_export\":false,\"unfinished_offline_blocks_release\":false}"

固定期望（null 表示需要补数据，没有可评分业务答案）：

    {
      "include_offline": true,
      "include_pdf_export": false,
      "unfinished_offline_blocks_release": false
    }

应用校验：

    {
      "accepted": true,
      "errors": []
    }

上下文缺少的必需事实键：

    []

其中源中存在但未进入上下文的键：

    []

### guard-maintenance-least-unused-cn / lexical / 第1次

原始错误/拒绝；原始判定 `wrong_fields`。

原始内容（JSON字符串）：

    "{\"error\": \"无合格窗口\"}"

固定期望（null 表示需要补数据，没有可评分业务答案）：

    {
      "window": "window-birch-72",
      "start_time": "13:00",
      "end_time": "13:55",
      "unused_minutes": 5
    }

应用校验：

    {
      "accepted": false,
      "errors": [
        "facts.maintenance.job.missing",
        "facts.calendar.windows.missing",
        "proposal.unknown_fields",
        "proposal.window.missing",
        "proposal.start_time.missing",
        "proposal.end_time.missing",
        "proposal.unused_minutes.missing"
      ]
    }

上下文缺少的必需事实键：

    [
      "maintenance.job",
      "calendar.windows"
    ]

其中源中存在但未进入上下文的键：

    [
      "maintenance.job",
      "calendar.windows"
    ]

### guard-maintenance-least-unused-cn / lexical / 第2次

原始错误/拒绝；原始判定 `wrong_values:window,start_time,end_time,unused_minutes`。

原始内容（JSON字符串）：

    "{\"window\": \"archive_00\", \"start_time\": \"00:00\", \"end_time\": \"00:00\", \"unused_minutes\": 0}"

固定期望（null 表示需要补数据，没有可评分业务答案）：

    {
      "window": "window-birch-72",
      "start_time": "13:00",
      "end_time": "13:55",
      "unused_minutes": 5
    }

应用校验：

    {
      "accepted": false,
      "errors": [
        "facts.maintenance.job.missing",
        "facts.calendar.windows.missing"
      ]
    }

上下文缺少的必需事实键：

    [
      "maintenance.job",
      "calendar.windows"
    ]

其中源中存在但未进入上下文的键：

    [
      "maintenance.job",
      "calendar.windows"
    ]

### guard-maintenance-least-unused-cn / required_facts / 第1次

原始正确/接受；原始判定 `pass`。

原始内容（JSON字符串）：

    "{\"window\":\"window-birch-72\",\"start_time\":\"13:00\",\"end_time\":\"13:55\",\"unused_minutes\":5}"

固定期望（null 表示需要补数据，没有可评分业务答案）：

    {
      "window": "window-birch-72",
      "start_time": "13:00",
      "end_time": "13:55",
      "unused_minutes": 5
    }

应用校验：

    {
      "accepted": true,
      "errors": []
    }

上下文缺少的必需事实键：

    []

其中源中存在但未进入上下文的键：

    []

### guard-maintenance-least-unused-cn / required_facts / 第2次

原始错误/拒绝；原始判定 `wrong_values:window,start_time,end_time,unused_minutes`。

原始内容（JSON字符串）：

    "{\"window\":\"window-pine-61\",\"start_time\":\"09:00\",\"end_time\":\"09:55\",\"unused_minutes\":20}"

固定期望（null 表示需要补数据，没有可评分业务答案）：

    {
      "window": "window-birch-72",
      "start_time": "13:00",
      "end_time": "13:55",
      "unused_minutes": 5
    }

应用校验：

    {
      "accepted": false,
      "errors": [
        "proposal.window.rule_mismatch",
        "proposal.start_time.rule_mismatch",
        "proposal.end_time.rule_mismatch",
        "proposal.unused_minutes.rule_mismatch"
      ]
    }

上下文缺少的必需事实键：

    []

其中源中存在但未进入上下文的键：

    []

### guard-maintenance-withdrawn-windows-en / lexical / 第1次

缺数题/拒绝；原始判定 `unanswerable_expected`。

原始内容（JSON字符串）：

    "{\"window\":\"maintenance.job\",\"start_time\":\"00:00\",\"end_time\":\"00:25\",\"unused_minutes\":0}"

固定期望（null 表示需要补数据，没有可评分业务答案）：

    null

应用校验：

    {
      "accepted": false,
      "errors": [
        "facts.calendar.windows.missing",
        "proposal.window.invalid_value"
      ]
    }

上下文缺少的必需事实键：

    [
      "calendar.windows"
    ]

其中源中存在但未进入上下文的键：

    []

### guard-maintenance-withdrawn-windows-en / lexical / 第2次

缺数题/拒绝；原始判定 `unanswerable_expected`。

原始内容（JSON字符串）：

    "{\"window\":\"maintenance.job\",\"start_time\":\"00:00\",\"end_time\":\"00:25\",\"unused_minutes\":0}"

固定期望（null 表示需要补数据，没有可评分业务答案）：

    null

应用校验：

    {
      "accepted": false,
      "errors": [
        "facts.calendar.windows.missing",
        "proposal.window.invalid_value"
      ]
    }

上下文缺少的必需事实键：

    [
      "calendar.windows"
    ]

其中源中存在但未进入上下文的键：

    []

### guard-maintenance-withdrawn-windows-en / required_facts / 第1次

缺数题/拒绝；原始判定 `unanswerable_expected`。

原始内容（JSON字符串）：

    "{\"window\":\"maintenance.job\",\"start_time\":\"00:00\",\"end_time\":\"00:25\",\"unused_minutes\":0}"

固定期望（null 表示需要补数据，没有可评分业务答案）：

    null

应用校验：

    {
      "accepted": false,
      "errors": [
        "facts.calendar.windows.missing",
        "proposal.window.invalid_value"
      ]
    }

上下文缺少的必需事实键：

    [
      "calendar.windows"
    ]

其中源中存在但未进入上下文的键：

    []

### guard-maintenance-withdrawn-windows-en / required_facts / 第2次

缺数题/拒绝；原始判定 `unanswerable_expected`。

原始内容（JSON字符串）：

    "{\"error\": \"No available calendar windows found in current records\"}"

固定期望（null 表示需要补数据，没有可评分业务答案）：

    null

应用校验：

    {
      "accepted": false,
      "errors": [
        "facts.calendar.windows.missing",
        "proposal.unknown_fields",
        "proposal.window.missing",
        "proposal.start_time.missing",
        "proposal.end_time.missing",
        "proposal.unused_minutes.missing"
      ]
    }

上下文缺少的必需事实键：

    [
      "calendar.windows"
    ]

其中源中存在但未进入上下文的键：

    []

## 范围与指标口径

- 8道新合成结构化题，4类各中英文1题；不是客户业务任务，不能与上一轮20道自由文本题直接比较质量。
- 每题每模式重复2次，复用同一份冻结输入；重复观测有关联，不能将32次调用视为32个独立任务。
- 两组只改变required_fact_keys：必需事实键由应用事先声明，不是自动发现关系、自动提取事实或跨语言语义检索改进。
- 每模式可答题7道×2次=14个观测；缺数据题1道×2次=2个观测单列，不能把拦截算作任务答对。
- 原始正确与校验后正确均以14个可答观测为分母；后一项还要求提案通过应用规则。猜中答案但输入不完整仍会被拒绝。
- false_rejection_with_complete_inputs只统计原始答案正确、当前输入完整却被拒绝的可答观测；correct_but_rejected还包含输入不完整的猜对。
- 同一校验器只读各组本次召回的事实，不从答案标签或其他组补值；它不修复模型答案，也不执行任何动作。
- 本基准为观察缺数行为仍调用模型一次再校验；正常应用示例应在输入缺失时提前停止，避免调用与执行。
- 当前应用规则固定：维护选择可容纳任务且空余最少的窗口，再按开始时间和标识排序；功能纳入条件是required AND ready。
- 同源事件、作用域与遗忘语义一致，每题添加16条档案干扰；两组最多8条事实、1200估算tokens，返回文本使用UTF-8字节数/4估算。
- 每次最多192输出tokens、16000字节messages JSON；已报告token阈值只停止后续调用，不是货币硬上限。
- 时延是预先测量的上下文构建加HTTP耗时，不包含写入、数据准备、限流等待或最终校验；不是完整端到端延迟。
- 费用按2026-09-30每百万输入¥6.50、输出¥27估算，未计缓存优惠，未经账单核对。
- 错误拦截、遗漏与原始答错是分别记录的观察，不能据此推断生产可靠性、统计显著性或所有场景的因果效果。

## 证据核验

题库 SHA-256：`6079e3610f152696c8b837ffa9e6eaf653d08c6f1471cb56e8c459c6dd2b3b73`。
已核对16份输入、32个唯一题目/模式/重复组合，以及 27 个调用前源码快照哈希。
重新检查了当前作用域的更新与遗忘、模型实际请求、原始成绩、输入完整性、应用判定、用量和原始汇总。
原生模块哈希由运行器记录；离线报告不重跑原生模块，也不独立证明构建来源。
