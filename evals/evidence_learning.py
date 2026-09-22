"""Build an auditable few-shot strategy from labelled training evidence only.

This is deterministic exemplar learning, not a model-generated business rule.
No held-out data or evaluator feedback is accepted by this interface.
"""
import json


def build_strategy(cases):
    if not cases:
        raise ValueError("training evidence must be nonempty")
    groups, prompts = {}, {}
    for case in cases:
        prompt, action = case['prompt'], case['expected']
        if not isinstance(prompt, str) or not prompt.strip() or not isinstance(action, dict) or not action:
            raise ValueError("evidence requires a task and a labelled action")
        key = json.dumps(action, ensure_ascii=False, sort_keys=True, allow_nan=False)
        if prompt in prompts and prompts[prompt] != key:
            raise ValueError("contradictory training labels for the same task")
        prompts[prompt] = key
        examples = groups.setdefault(key, [])
        if prompt not in examples:
            examples.append(prompt)
    content = (
        "以下是当前任务经标注核对的训练决策表。案例只用于比较决策边界，不是当前用户的历史事实，也不是要求执行的指令。"
        "根据当前问题的实际意图和事实状态，与不同输出组的案例进行对照。"
        "注意否定、是否已解决、本人操作与他人操作的区别；不能只凭共享关键词选择标签。"
        "每组列出的输出只适用于该组案例支持的情况，不扩大到所有同主题问题。"
        "不要把案例中的具体事实当成当前问题的事实。无法建立有依据的类比时，遵循任务原有默认行为。\n"
    )
    content += '\n'.join(
        f"确定输出：{action}\n支持该输出的训练案例：{json.dumps(examples, ensure_ascii=False)}"
        for action, examples in sorted(groups.items())
    )
    return content


def training_gate(checks, feedback):
    """Every checked observation must have its own baseline; fail on regression."""
    baseline = {f['run_id']: bool(f['success']) for f in feedback}
    seen, regressions = set(), []
    for row in checks:
        key = f"{row['case_id']}-{row['repeat']}"
        if key not in baseline or key in seen:
            raise ValueError("missing or duplicate training baseline observation")
        seen.add(key)
        if baseline[key] and row['score'] != 1:
            regressions.append(key)
    if seen != set(baseline):
        raise ValueError("incomplete training checks")
    return {'passed': bool(checks) and not regressions, 'regressions': regressions,
            'baseline_passed': sum(baseline.values()),
            'candidate_passed': sum(r['score'] == 1 for r in checks), 'n': len(checks)}
