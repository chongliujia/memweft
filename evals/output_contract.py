"""Explicit flat JSON output contracts, independent of evaluator answer keys.

This intentionally supports a small schema subset rather than pretending to
implement all of JSON Schema. Unsupported schema features fail before a run.
"""
import json


TYPES = {"string": str, "integer": int, "boolean": bool, "null": type(None)}
PROPOSAL_SCHEMA = {
    "type": "object", "properties": {"content": {"type": "string"}},
    "required": ["content"], "additionalProperties": False,
}


def strict_json_loads(content):
    def unique_pairs(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError(f"duplicate JSON key: {key}")
            obj[key] = value
        return obj

    def invalid_constant(value):
        raise ValueError(f"non-JSON constant: {value}")

    return json.loads(content, object_pairs_hook=unique_pairs, parse_constant=invalid_constant)


def validate_schema(schema):
    if (not isinstance(schema, dict)
            or set(schema) != {"type", "properties", "required", "additionalProperties"}
            or schema["type"] != "object" or schema["additionalProperties"] is not False):
        raise ValueError("contract must be a closed object with properties and all fields required")
    props, required = schema["properties"], schema["required"]
    if (not isinstance(props, dict) or not props
            or not all(isinstance(key, str) and key for key in props)
            or not isinstance(required, list) or not all(isinstance(key, str) for key in required)
            or len(required) != len(set(required)) or set(required) != set(props)):
        raise ValueError("contract must require each declared field exactly once")
    for leaf in props.values():
        if not isinstance(leaf, dict) or "type" not in leaf or set(leaf) - {"type", "enum"}:
            raise ValueError("only explicit scalar types and optional enums are supported")
        kinds = leaf["type"] if isinstance(leaf["type"], list) else [leaf["type"]]
        if (not kinds or not all(isinstance(kind, str) and kind in TYPES for kind in kinds)
                or len(set(kinds)) != len(kinds)):
            raise ValueError("unsupported or duplicate scalar type")
        if "enum" in leaf:
            if (not isinstance(leaf["enum"], list) or not leaf["enum"]
                    or any(not _type_matches(value, leaf) for value in leaf["enum"])):
                raise ValueError("enum values must have a declared type")


def _type_matches(value, leaf):
    kinds = leaf["type"] if isinstance(leaf["type"], list) else [leaf["type"]]
    # bool is a subclass of int in Python, but is not an integer JSON output.
    return any(type(value) is TYPES[kind] for kind in kinds)


def schema_error(actual, schema):
    """Validate an answer against an already validated flat contract."""
    if not isinstance(actual, dict) or set(actual) != set(schema["properties"]):
        return "wrong_fields"
    for key, leaf in schema["properties"].items():
        if not _type_matches(actual[key], leaf):
            return f"wrong_type:{key}"
        if "enum" in leaf and not any(type(actual[key]) is type(v) and actual[key] == v for v in leaf["enum"]):
            return f"invalid_enum:{key}"
    return None


def contract_request(messages, schema, mode):
    """Return messages/response_format; this function never receives expected answers."""
    if mode not in {"none", "prompt", "schema"}:
        raise ValueError("unknown output contract mode")
    if mode == "none":
        return messages, None
    validate_schema(schema)
    instruction = (
        "\n输出必须符合以下 JSON Schema，字段名使用契约中的名称。"
        "没有可用信息时，仅在相应字段允许 null 的情况下填写 null。"
        "存储记录的 key 用于识别资料，输出字段由本契约规定。\n"
        + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    )
    result = [dict(message) for message in messages]
    if result and result[0]["role"] == "system":
        result[0]["content"] += instruction
    else:
        result.insert(0, {"role": "system", "content": instruction})
    response_format = None
    if mode == "schema":
        response_format = {"type": "json_schema", "json_schema": {
            "name": "memweft_answer", "strict": True, "schema": schema,
        }}
    return result, response_format
