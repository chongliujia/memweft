"""Small append-only journal for the internal release-planning pilot.

Only explicit result fields are retained. Model acceptance is not correctness;
correctness requires a separate, immutable human assessment. No prices are used.
"""
from __future__ import annotations

from collections import Counter
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile


OUTCOMES = ("correct_completion", "incorrect_acceptance", "correct_rejection", "incorrect_rejection")
RESULT_FIELDS = ("evidence", "context", "proposal", "validation_error", "blockers", "expected",
                 "expected_decisions", "expected_next_action")
ANSWER_FIELDS = ("content", "finish_reason", "response_id", "model", "usage", "latency_ms")
SENSITIVE_KEYS = {"authorization", "headers", "request_headers", "request", "env", "environment",
                  "credential", "credentials", "api_key", "apikey", "x_api_key", "_api_key",
                  "moonshot_api_key", "github_token", "access_token", "refresh_token", "password",
                  "secret", "secrets"}
MAX_RECORD_BYTES = 8 * 1024 * 1024
RECORD_FIELDS = {"schema_version", "recorded_at", "status", "model_call", "answer", "usage",
                 "latency_ms", "error_code", *RESULT_FIELDS}


def _code(value):
    if not isinstance(value, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,95}", value) is None:
        raise ValueError("invalid_journal_error_code")
    return value


def _finite(value):
    if type(value) not in (int, float) or value < 0:
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:
        return None


def _json_data(value, depth=0):
    if depth > 64:
        raise ValueError("journal_data_too_deep")
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        return value if math.isfinite(value) else None
    if isinstance(value, list):
        return [_json_data(item, depth + 1) for item in value]
    if isinstance(value, dict):
        if any(type(key) is not str for key in value):
            raise ValueError("journal_keys_must_be_strings")
        return {key: _json_data(item, depth + 1) for key, item in value.items()
                if key.lower().replace("-", "_") not in SENSITIVE_KEYS}
    # In particular, never stringify exceptions, HTTP requests or clients.
    raise ValueError("journal_requires_json_data")


def _validation_error(value):
    if value is None:
        return None
    if isinstance(value, str):
        return _code(value)
    if not isinstance(value, dict) or set(value) != {"code", "errors"} or not isinstance(value["errors"], list):
        raise ValueError("invalid_journal_validation_error")
    _code(value["code"])
    for error in value["errors"]:
        if (not isinstance(error, dict) or "code" not in error
                or set(error) - {"code", "field", "expected", "actual", "blockers"}):
            raise ValueError("invalid_journal_validation_detail")
        _code(error["code"])
        if "field" in error and not isinstance(error["field"], str):
            raise ValueError("invalid_journal_validation_field")
        if "blockers" in error and (not isinstance(error["blockers"], list)
                or any(not isinstance(blocker, str) for blocker in error["blockers"])):
            raise ValueError("invalid_journal_validation_blockers")
    return _json_data(value)


def _record(result, error_code):
    if result is None:
        if error_code is None:
            raise ValueError("journal_requires_result_or_error_code")
        result = {}
    if not isinstance(result, dict):
        raise ValueError("journal_result_must_be_object")
    if error_code is not None:
        _code(error_code)
    answer = result.get("answer")
    if answer is not None and not isinstance(answer, dict):
        raise ValueError("journal_answer_must_be_object")
    answer = None if answer is None else _json_data({key: answer[key] for key in ANSWER_FIELDS if key in answer})
    call = result.get("model_call", {"attempted": True if answer is not None else None,
                                      "completed": answer is not None})
    if (not isinstance(call, dict) or set(call) != {"attempted", "completed"}
            or (call["attempted"] is not None and type(call["attempted"]) is not bool)
            or type(call["completed"]) is not bool
            or (call["completed"] and (call["attempted"] is not True or answer is None))
            or (answer is not None and not call["completed"])):
        raise ValueError("invalid_journal_model_call")
    error = _validation_error(result.get("validation_error"))
    proposal = result.get("proposal")
    if proposal is not None and not isinstance(proposal, dict):
        raise ValueError("journal_proposal_must_be_object")
    if proposal is not None and not call["completed"]:
        raise ValueError("journal_proposal_requires_completion")
    raw_usage = answer.get("usage") if answer is not None else result.get("usage")
    raw_usage = raw_usage if isinstance(raw_usage, dict) else {}
    usage = {key: raw_usage.get(key) if type(raw_usage.get(key)) is int and raw_usage[key] >= 0 else None
             for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
    latency = _finite(answer.get("latency_ms") if answer is not None else result.get("latency_ms"))
    if error_code is not None:
        status = "error"
    elif call["attempted"] is False:
        status = "prepared"
    elif not call["completed"]:
        status = "incomplete"
    else:
        status = "rejected" if error is not None or proposal is None else "accepted"
    record = {"schema_version": 1, "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(),
              "status": status, "model_call": dict(call), "answer": answer, "usage": usage,
              "latency_ms": latency, "error_code": error_code}
    for key in RESULT_FIELDS:
        record[key] = _json_data(result.get(key, [] if key == "blockers" else None))
    record["validation_error"] = error
    if not isinstance(record["blockers"], list) or any(not isinstance(item, str) for item in record["blockers"]):
        raise ValueError("journal_blockers_must_be_strings")
    return record


def _reject_symlinks(path):
    path = Path(path).absolute()
    if ".." in path.parts or any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("journal_symlink_or_parent_traversal")
    return path


def _write_new(path, payload):
    """Publish a complete file with an exclusive link, never replacing a path."""
    path = _reject_symlinks(path)
    content = (json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if len(content) > MAX_RECORD_BYTES:
        raise ValueError("journal_record_too_large")
    descriptor, temporary = tempfile.mkstemp(prefix=".journal-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_attempt(output: Path, result: dict | None, *, error_code=None):
    """Reserve a new attempt directory and atomically publish attempt.json.

    For transport errors, pass a partial result with model_call={attempted: True,
    completed: False}. Unknown call status remains unknown, never silently zero.
    Explicit evidence/context and raw answer content may contain application data;
    callers should provide only information appropriate for this local journal.
    """
    record = _record(result, error_code)
    output = _reject_symlinks(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir()  # Existing files and directories must fail, even if empty.
    try:
        _write_new(output / "attempt.json", record)
    except Exception:
        try:
            output.rmdir()
        except OSError:
            pass
        raise
    return output / "attempt.json"


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_journal_json_field")
        result[key] = value
    return result


def _load(path):
    path = _reject_symlinks(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RECORD_BYTES:
            raise ValueError("invalid_journal_file")
        raw = stream.read(MAX_RECORD_BYTES + 1)
    if len(raw) > MAX_RECORD_BYTES:
        raise ValueError("journal_record_too_large")
    return json.loads(raw, object_pairs_hook=_unique_pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite_journal_json")))


def _read_attempt(directory):
    record = _load(directory / "attempt.json")
    if not isinstance(record, dict) or set(record) != RECORD_FIELDS or type(record["schema_version"]) is not int or record["schema_version"] != 1:
        raise ValueError("invalid_journal_schema")
    _timestamp(record["recorded_at"])
    if (not isinstance(record["usage"], dict) or set(record["usage"]) != {"prompt_tokens", "completion_tokens", "total_tokens"}
            or any(value is not None and (type(value) is not int or value < 0) for value in record["usage"].values())
            or (record["latency_ms"] is not None and _finite(record["latency_ms"]) is None)):
        raise ValueError("invalid_journal_measurements")
    expected = _record(record, record["error_code"])
    if any(record[key] != expected[key] for key in RECORD_FIELDS - {"recorded_at"}):
        raise ValueError("inconsistent_journal_record")
    return record


def _timestamp(value):
    try:
        if not isinstance(value, str):
            raise ValueError
        dt.datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("invalid_journal_timestamp") from None


def _assessment(outcome, status):
    if outcome not in OUTCOMES:
        raise ValueError("invalid_journal_assessment")
    allowed = {"accepted": OUTCOMES[:2], "rejected": OUTCOMES[2:], "error": (), "prepared": (), "incomplete": ()}
    if outcome not in allowed[status]:
        raise ValueError("assessment_does_not_match_plan_status")


def record_assessment(attempt_dir: Path, outcome):
    attempt_dir = _reject_symlinks(attempt_dir)
    record = _read_attempt(attempt_dir)
    _assessment(outcome, record["status"])
    _write_new(attempt_dir / "assessment.json", {"schema_version": 1, "outcome": outcome,
        "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat()})
    return attempt_dir / "assessment.json"


def summarize_attempts(directory: Path):
    directory = _reject_symlinks(directory)
    if not directory.is_dir():
        raise ValueError("journal_directory_required")
    records, assessments = [], Counter({outcome: 0 for outcome in (*OUTCOMES, "unreviewed")})
    reasons, top_reasons, error_codes = Counter(), Counter(), Counter()
    for child in sorted(directory.iterdir()):
        _reject_symlinks(child)
        if not child.is_dir():
            continue
        path, assessment_path = child / "attempt.json", child / "assessment.json"
        _reject_symlinks(path)
        _reject_symlinks(assessment_path)
        if not path.exists():
            if assessment_path.exists():
                raise ValueError("journal_assessment_without_attempt")
            continue
        record = _read_attempt(child)
        records.append(record)
        if assessment_path.exists():
            assessment = _load(assessment_path)
            if (not isinstance(assessment, dict) or set(assessment) != {"schema_version", "outcome", "recorded_at"}
                    or type(assessment["schema_version"]) is not int or assessment["schema_version"] != 1
                    or not isinstance(assessment["recorded_at"], str)):
                raise ValueError("invalid_journal_assessment_record")
            _timestamp(assessment["recorded_at"])
            _assessment(assessment["outcome"], record["status"])
            assessments[assessment["outcome"]] += 1
        else:
            assessments["unreviewed"] += 1
        error = record["validation_error"]
        if error is not None and record["status"] == "rejected":
            code = error if isinstance(error, str) else error["code"]
            top_reasons[code] += 1
            details = [item["code"] for item in error["errors"]] if isinstance(error, dict) else []
            reasons.update(set(details or [code]))  # Attempts with this reason, not repeated fields.
        if record["error_code"] is not None:
            error_codes[record["error_code"]] += 1
    calls = [record for record in records if record["model_call"]["attempted"] is not False]
    usage = {}
    for key in ("prompt_tokens", "completion_tokens"):
        values = [record["usage"][key] for record in calls]
        unknown = sum(value is None for value in values)
        known = sum(value for value in values if value is not None)
        usage[key] = None if unknown else known
        usage["known_" + key] = known
        usage["unknown_" + key + "_calls"] = unknown
    usage["unknown_usage_calls"] = sum(any(record["usage"][key] is None for key in ("prompt_tokens", "completion_tokens")) for record in calls)
    latencies = [record["latency_ms"] for record in calls if record["latency_ms"] is not None]
    def finite_sum(values):
        try:
            return math.fsum(values)
        except OverflowError:
            return None
    reviewed = len(records) - assessments["unreviewed"]
    correctly_reviewed = assessments["correct_completion"] + assessments["correct_rejection"]
    return {"schema_version": 1, "attempts": len(records),
        "model_calls": sum(record["model_call"]["attempted"] is True for record in records),
        "unknown_model_calls": sum(record["model_call"]["attempted"] is None for record in records),
        "completions": sum(record["model_call"]["completed"] for record in records),
        **{status: sum(record["status"] == status for record in records) for status in ("accepted", "rejected", "prepared", "incomplete")},
        "errors": sum(record["status"] == "error" for record in records),
        "rejection_reasons": dict(reasons), "rejection_categories": dict(top_reasons), "error_codes": dict(error_codes),
        "usage": usage, "latency_ms": {"measured_calls": len(latencies), "unknown_calls": len(calls) - len(latencies),
            "total": finite_sum(latencies) if len(latencies) == len(calls) else None,
            "known_total": finite_sum(latencies), "mean": finite_sum(value / len(latencies) for value in latencies) if latencies else None,
            "min": min(latencies) if latencies else None, "max": max(latencies) if latencies else None},
        "priced_cost": None if calls else 0, "cost_status": "unknown" if calls else "no_model_calls",
        "assessments": dict(assessments), "manual_reviewed": reviewed,
        "manual_correctness_rate": correctly_reviewed / reviewed if reviewed else None,
        "correctness_basis": "human_assessments_only; acceptance and rejection are not correctness judgments"}
