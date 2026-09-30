"""Small application-owned rules for structured workflow proposals.

Facts are already-decoded JSON objects supplied by the application. The guard
reads only fixed keys and never takes rules, executable expressions, expected
answers or tool instructions from memory. Schema version checks do not establish
the freshness or authenticity of a fact; the caller owns those properties.

Acceptance means that a proposal matches these example rules. It executes no
actions, does not repair an answer and does not imply external authorization.
"""
from __future__ import annotations

import re


def _object(properties):
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


def _fact(properties):
    return _object({"version": {"type": "integer", "enum": [1]}, **properties})


def _integer(minimum=0, maximum=10**12):
    return {"type": "integer", "minimum": minimum, "maximum": maximum}


_BOOL = {"type": "boolean"}
_ID = {"type": "string", "pattern": r"^[A-Za-z][A-Za-z0-9_-]{0,63}$"}
_CURRENCY = {"type": "string", "enum": ["CNY", "USD", "EUR"]}
_TIME = {"type": "string", "pattern": r"^(?:(?:[01][0-9]|2[0-3]):[0-5][0-9]|24:00)$"}

WORKFLOWS = {
    "tls": {
        "required_fact_keys": ["tls.certificate", "clock.observation", "host.clock"],
        "fact_schemas": {
            "tls.certificate": _fact({"expires_minute": _integer(), "owner": _ID}),
            "clock.observation": _fact({"now_minute": _integer()}),
            "host.clock": _fact({"offset_seconds": _integer(-86400, 86400),
                                  "threshold_seconds": _integer(0, 86400)}),
        },
        "output_schema": _object({"action": {"type": "string", "enum": ["synchronize_clock", "renew_certificate", "no_action"]},
                                   "expired_minutes": _integer(), "owner": _ID}),
        "rule_text": "expired_minutes=max(0, now_minute-expires_minute)，owner 来自 tls.certificate。"
                     "若 abs(offset_seconds)>threshold_seconds，action=synchronize_clock；否则若 "
                     "now_minute>=expires_minute，action=renew_certificate；否则 action=no_action。"
                     "时钟偏移判断优先。分钟与秒不可混用。",
    },
    "refund": {
        "required_fact_keys": ["refund.policy", "order.state", "price.unit"],
        "fact_schemas": {
            "refund.policy": _fact({"window_days": _integer(0, 365000)}),
            "order.state": _fact({"age_days": _integer(0, 365000), "ordered": _integer(1, 10**6),
                                   "shipped": _integer(0, 10**6)}),
            "price.unit": _fact({"amount": _integer(0, 10**9), "currency": _CURRENCY}),
        },
        "output_schema": _object({"refund_amount": _integer(0, 10**15), "currency": _CURRENCY,
                                   "needs_manual_review": _BOOL}),
        "rule_text": "金额均为整数最小货币单位，不做汇率换算。若 age_days<=window_days，"
                     "refund_amount=(ordered-shipped)*amount，needs_manual_review=false；"
                     "否则 refund_amount=0，needs_manual_review=true。currency 来自 price.unit。"
                     "这是示例应用政策；已发货部分不自动退款，shipped 不得超过 ordered。",
    },
    "feature_scope": {
        "required_fact_keys": ["product.requirements", "implementation.readiness"],
        "fact_schemas": {
            "product.requirements": _fact({"offline_required": _BOOL, "pdf_required": _BOOL}),
            "implementation.readiness": _fact({"offline_ready": _BOOL, "pdf_ready": _BOOL}),
        },
        "output_schema": _object({"include_offline": _BOOL, "include_pdf_export": _BOOL,
                                   "unfinished_offline_blocks_release": _BOOL}),
        "rule_text": "include_offline=offline_required AND offline_ready；"
                     "include_pdf_export=pdf_required AND pdf_ready；"
                     "unfinished_offline_blocks_release=offline_required AND NOT offline_ready。"
                     "只纳入明确要求且已经就绪的功能。",
    },
    "maintenance": {
        "required_fact_keys": ["maintenance.job", "calendar.windows"],
        "fact_schemas": {
            "maintenance.job": _fact({"duration_minutes": _integer(1, 1440)}),
            "calendar.windows": _fact({"windows": {"type": "array", "minItems": 2, "maxItems": 2,
                "items": _object({"id": _ID, "start_minute": _integer(0, 1439), "end_minute": _integer(1, 1440)})}}),
        },
        "output_schema": _object({"window": _ID, "start_time": _TIME, "end_time": _TIME,
                                   "unused_minutes": _integer(0, 1439)}),
        "rule_text": "两个窗口是同一天的绝对分钟，id 唯一，start_minute<end_minute。"
                     "先保留长度至少为 duration_minutes 的窗口；选择窗口长度减 duration_minutes "
                     "最小的窗口，平手选 start_minute 更早的，再平手按 id 字典序。"
                     "从选中窗口 start_minute 开始，执行 duration_minutes；start_time/end_time "
                     "用 HH:MM 表示（午夜结束可为24:00）；window 为选中 id，unused_minutes 为"
                     "整个窗口长度减 duration_minutes。无合格窗口时不能给出可执行提案。",
    },
}


def _check(value, schema, path, errors):
    """Validate only the deliberately small JSON Schema subset defined above."""
    kind = schema["type"]
    expected_type = {"object": dict, "array": list, "integer": int, "boolean": bool, "string": str}[kind]
    if type(value) is not expected_type:
        errors.append(path + ".invalid_type")
        return
    if "enum" in schema and value not in schema["enum"]:
        errors.append(path + (".unsupported_fact_version" if path.endswith(".version") else ".invalid_value"))
    if kind == "object":
        properties = schema["properties"]
        if set(value) - set(properties):
            errors.append(path + ".unknown_fields")
        for key, child in properties.items():
            if key not in value:
                errors.append(path + "." + key + ".missing")
            else:
                _check(value[key], child, path + "." + key, errors)
    elif kind == "array":
        if not schema["minItems"] <= len(value) <= schema["maxItems"]:
            errors.append(path + ".invalid_length")
            return
        for index, item in enumerate(value):
            _check(item, schema["items"], path + f"[{index}]", errors)
    elif kind == "integer":
        if ("minimum" in schema and value < schema["minimum"]) or ("maximum" in schema and value > schema["maximum"]):
            errors.append(path + ".out_of_range")
    elif kind == "string" and "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
        errors.append(path + ".invalid_value")


def _tls(facts):
    cert, clock, host = (facts[key] for key in WORKFLOWS["tls"]["required_fact_keys"])
    expired = max(0, clock["now_minute"] - cert["expires_minute"])
    if abs(host["offset_seconds"]) > host["threshold_seconds"]:
        action = "synchronize_clock"
    elif clock["now_minute"] >= cert["expires_minute"]:
        action = "renew_certificate"
    else:
        action = "no_action"
    return {"action": action, "expired_minutes": expired, "owner": cert["owner"]}


def _refund(facts):
    policy, order, unit = (facts[key] for key in WORKFLOWS["refund"]["required_fact_keys"])
    manual = order["age_days"] > policy["window_days"]
    return {"refund_amount": 0 if manual else (order["ordered"] - order["shipped"]) * unit["amount"],
            "currency": unit["currency"], "needs_manual_review": manual}


def _feature(facts):
    required, ready = (facts[key] for key in WORKFLOWS["feature_scope"]["required_fact_keys"])
    return {"include_offline": required["offline_required"] and ready["offline_ready"],
            "include_pdf_export": required["pdf_required"] and ready["pdf_ready"],
            "unfinished_offline_blocks_release": required["offline_required"] and not ready["offline_ready"]}


def _maintenance(facts):
    duration = facts["maintenance.job"]["duration_minutes"]
    windows = facts["calendar.windows"]["windows"]
    candidates = [item for item in windows if item["end_minute"] - item["start_minute"] >= duration]
    if not candidates:
        return None
    chosen = min(candidates, key=lambda item: (item["end_minute"] - item["start_minute"] - duration,
                                              item["start_minute"], item["id"]))
    def time(minute):
        return f"{minute // 60:02d}:{minute % 60:02d}"
    return {"window": chosen["id"], "start_time": time(chosen["start_minute"]),
            "end_time": time(chosen["start_minute"] + duration),
            "unused_minutes": chosen["end_minute"] - chosen["start_minute"] - duration}


_RULES = {"tls": _tls, "refund": _refund, "feature_scope": _feature, "maintenance": _maintenance}


def validate_facts(workflow, facts_by_key):
    """Preflight fixed fact keys before spending a model call.

    Unknown fields within a required fact are rejected. Unrelated memory keys
    in facts_by_key are ignored, so they cannot supply rules or overwrite inputs.
    All required facts must have schema version 1; this is not a freshness check.
    """
    if type(workflow) is not str or workflow not in WORKFLOWS:
        return {"accepted": False, "errors": ["unknown_workflow"]}
    config, errors = WORKFLOWS[workflow], []
    if type(facts_by_key) is not dict:
        return {"accepted": False, "errors": ["facts.invalid_type"]}
    for key in config["required_fact_keys"]:
        if key not in facts_by_key:
            errors.append("facts." + key + ".missing")
        else:
            _check(facts_by_key[key], config["fact_schemas"][key], "facts." + key, errors)
    if errors:
        return {"accepted": False, "errors": errors}
    if workflow == "refund" and facts_by_key["order.state"]["shipped"] > facts_by_key["order.state"]["ordered"]:
        errors.append("facts.order.state.shipped_exceeds_ordered")
    if workflow == "maintenance":
        windows = facts_by_key["calendar.windows"]["windows"]
        if len({item["id"] for item in windows}) != len(windows):
            errors.append("facts.calendar.windows.duplicate_id")
        if any(item["start_minute"] >= item["end_minute"] for item in windows):
            errors.append("facts.calendar.windows.invalid_interval")
    if errors:
        return {"accepted": False, "errors": errors}
    if workflow == "maintenance" and _maintenance(facts_by_key) is None:
        return {"accepted": False, "errors": ["facts.calendar.windows.no_feasible_window"]}
    return {"accepted": True, "errors": []}


def validate_output(workflow, proposal):
    """Check the output contract alone, without reading facts or applying rules."""
    if type(workflow) is not str or workflow not in WORKFLOWS:
        return {"accepted": False, "errors": ["unknown_workflow"]}
    errors = []
    _check(proposal, WORKFLOWS[workflow]["output_schema"], "proposal", errors)
    return {"accepted": not errors, "errors": errors}


def validate_proposal(workflow, proposal, facts_by_key):
    """Return {accepted, errors}; never replace, fill or execute a proposal."""
    fact_check = validate_facts(workflow, facts_by_key)
    if type(workflow) is not str or workflow not in WORKFLOWS:
        return fact_check
    errors = fact_check["errors"] + validate_output(workflow, proposal)["errors"]
    if errors:
        return {"accepted": False, "errors": errors}
    required = _RULES[workflow](facts_by_key)
    for key, value in required.items():
        if proposal[key] != value:
            errors.append("proposal." + key + ".rule_mismatch")
    return {"accepted": not errors, "errors": errors}
