"""Read-only, commit-bound GitHub Actions evidence (standard library only).

This verifies a workflow's overall conclusion, not individual matrix jobs or
downloaded release artifacts. Callers must opt into the network explicitly.
"""
from __future__ import annotations

import base64
import binascii
import datetime as dt
import hashlib
import http.client
import json
import os
import re
from urllib import error, parse, request

WORKFLOW_PATH = ".github/workflows/verify.yml"
API_ROOT = "https://api.github.com"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


def github_repository(remote):
    """Accept only credential-free GitHub remotes; never return arbitrary URLs."""
    match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
                         r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/?", remote or "")
    return match[1].removesuffix(".git") if match else None


class EvidenceError(Exception):
    def __init__(self, code, http_status=None):
        super().__init__(code)
        self.code, self.http_status = code, http_status


class _NoRedirect(request.HTTPRedirectHandler):
    # In particular, never forward an Authorization header to a redirect host.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _get_json(path, *, token, opener):
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10",
               "User-Agent": "memweft-ci-evidence"}
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        with opener.open(request.Request(API_ROOT + path, headers=headers, method="GET"), timeout=15) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise EvidenceError("response_too_large")
        result = json.loads(body)
        if not isinstance(result, dict):
            raise EvidenceError("invalid_api_response")
        return result
    except error.HTTPError as exc:
        code = {401: "authentication_failed", 403: "forbidden_or_rate_limited",
                404: "not_found_or_inaccessible", 429: "rate_limited"}.get(exc.code, "http_error")
        # Do not expose server bodies, URLs, headers, tokens or exception text.
        raise EvidenceError(code, exc.code) from None
    except (error.URLError, TimeoutError, OSError, http.client.HTTPException):
        raise EvidenceError("network_unavailable") from None
    except (ValueError, UnicodeError):
        raise EvidenceError("invalid_api_response") from None


def _positive_integer(value):
    return type(value) is int and value > 0


def collect_github_ci(repository, head, workflow_bytes, *, worktree_dirty, token=None, opener=None):
    """Fetch one snapshot; a newer commit/run/attempt requires collecting again.

    Only push and explicit workflow_dispatch runs are accepted: PR runs can test
    a synthetic merge commit even when their reported head SHA is the PR head.
    The caller supplies the exact local workflow bytes for comparison with the
    same file in GitHub at `head`. GITHUB_TOKEN is optional and never returned.
    """
    evidence = {"provider": "github_actions", "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "repository": repository, "head": head, "workflow_path": WORKFLOW_PATH,
                "workflow_sha256": hashlib.sha256(workflow_bytes).hexdigest() if workflow_bytes is not None else None,
                "status": "unavailable", "reason": None, "workflow_id": None, "run": None,
                "applies_to_worktree": worktree_dirty is False,
                "matrix_jobs_verified": False, "release_artifacts_verified": False}
    if not isinstance(repository, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        return {**evidence, "repository": None, "reason": "github_repository_unknown"}
    if not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}", head):
        return {**evidence, "reason": "full_commit_sha_required"}
    if workflow_bytes is None:
        return {**evidence, "reason": "local_workflow_missing"}
    token = os.environ.get("GITHUB_TOKEN") if token is None else token
    if token and (not token.isascii() or any(ch.isspace() for ch in token)):
        return {**evidence, "reason": "invalid_token_format"}
    opener = opener or request.build_opener(_NoRedirect())
    prefix = "/repos/" + repository
    stage = "workflow"
    try:
        workflow = _get_json(prefix + "/actions/workflows/verify.yml", token=token, opener=opener)
        workflow_id = workflow.get("id")
        if not _positive_integer(workflow_id) or workflow.get("path") != WORKFLOW_PATH:
            raise EvidenceError("workflow_identity_mismatch")
        evidence["workflow_id"] = workflow_id
        stage = "runs"
        payload = _get_json(prefix + f"/actions/workflows/{workflow_id}/runs?" +
                            parse.urlencode({"head_sha": head, "per_page": 100}), token=token, opener=opener)
        runs = payload.get("workflow_runs")
        total = payload.get("total_count")
        if not isinstance(runs, list) or type(total) is not int or total < 0 or total < len(runs):
            raise EvidenceError("invalid_run_list")
        if total > len(runs):
            # Fail closed rather than overlook a newer attempt in an incomplete result set.
            raise EvidenceError("run_history_incomplete")
        candidates = []
        for run in runs:
            if not isinstance(run, dict):
                raise EvidenceError("invalid_run_list")
            # Never trust the head_sha URL filter without validating each response.
            if run.get("head_sha") != head:
                continue
            if not _positive_integer(run.get("id")) or not _positive_integer(run.get("run_number")):
                raise EvidenceError("invalid_run_identity")
            candidates.append(run)
        if not candidates:
            return {**evidence, "status": "not_run", "reason": "no_run_for_commit"}
        latest = max(candidates, key=lambda item: (item["run_number"], item["id"]))
        stage = "run_detail"
        run = _get_json(prefix + f"/actions/runs/{latest['id']}", token=token, opener=opener)
        if (not _positive_integer(run.get("id")) or not _positive_integer(run.get("run_number"))
                or run.get("id") != latest["id"] or run.get("run_number") != latest["run_number"]
                or not _positive_integer(run.get("run_attempt"))
                or not _positive_integer(latest.get("run_attempt"))
                or run["run_attempt"] < latest["run_attempt"]):
            raise EvidenceError("run_identity_or_attempt_mismatch")
        if (run.get("head_sha") != head or not _positive_integer(run.get("workflow_id"))
                or run.get("workflow_id") != workflow_id
                or not isinstance(run.get("path"), str) or run["path"].split("@", 1)[0] != WORKFLOW_PATH
                or not isinstance(run.get("repository"), dict)
                or str(run["repository"].get("full_name", "")).lower() != repository.lower()
                or not isinstance(run.get("head_repository"), dict)
                or str(run["head_repository"].get("full_name", "")).lower() != repository.lower()):
            raise EvidenceError("run_source_or_workflow_mismatch")
        if run.get("event") not in ("push", "workflow_dispatch"):
            raise EvidenceError("unsupported_run_event")
        stage = "workflow_source"
        content = _get_json(prefix + "/contents/" + WORKFLOW_PATH + "?ref=" + head, token=token, opener=opener)
        if (content.get("type") != "file" or content.get("path") != WORKFLOW_PATH
                or content.get("encoding") != "base64" or not isinstance(content.get("content"), str)):
            raise EvidenceError("invalid_workflow_content")
        try:
            source = base64.b64decode("".join(content["content"].split()), validate=True)
        except (binascii.Error, ValueError):
            raise EvidenceError("invalid_workflow_content") from None
        if source != workflow_bytes:
            raise EvidenceError("workflow_content_mismatch")
        status, conclusion = run.get("status"), run.get("conclusion")
        known_statuses = ("completed", "queued", "in_progress", "requested", "waiting", "pending")
        known_conclusions = (None, "success", "failure", "neutral", "cancelled", "skipped", "timed_out", "action_required", "stale", "startup_failure")
        if status not in known_statuses or conclusion not in known_conclusions:
            raise EvidenceError("unknown_run_status")
        evidence["run"] = {"id": run["id"], "number": run["run_number"], "attempt": run["run_attempt"],
                           "event": run["event"], "status": status, "conclusion": conclusion,
                           "url": f"https://github.com/{repository}/actions/runs/{run['id']}"}
        if status != "completed":
            return {**evidence, "status": "pending", "reason": "workflow_not_completed"}
        if conclusion == "success":
            return {**evidence, "status": "passed", "reason": "workflow_succeeded_for_commit"}
        return {**evidence, "status": "failed", "reason": "workflow_did_not_succeed"}
    except EvidenceError as exc:
        return {**evidence, "reason": exc.code, "request_stage": stage, "http_status": exc.http_status}
