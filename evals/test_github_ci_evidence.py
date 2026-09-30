"""Offline GitHub HTTP fixtures: never contact GitHub or a model."""
import base64
import io
import http.client
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
from github_ci_evidence import WORKFLOW_PATH, _NoRedirect, collect_github_ci, github_repository
from kimi_release_agent import collect_evidence

HEAD = "a" * 40
SOURCE = b"name: verify\non: push\n"
REPO = "example/memweft"


def run_record(**overrides):
    return {"id": 80, "run_number": 20, "run_attempt": 1, "workflow_id": 12,
            "head_sha": HEAD, "path": WORKFLOW_PATH, "repository": {"full_name": REPO},
            "head_repository": {"full_name": REPO}, "event": "push",
            "status": "completed", "conclusion": "success", **overrides}


def responses(*, runs=None, detail=None, content=None):
    runs = [run_record()] if runs is None else runs
    result = [{"id": 12, "path": WORKFLOW_PATH}, {"total_count": len(runs), "workflow_runs": runs}]
    if runs:
        result.extend([detail or runs[-1], content or {
            "type": "file", "path": WORKFLOW_PATH, "encoding": "base64",
            "content": base64.b64encode(SOURCE).decode()}])
    return result


class HttpFixture:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.requests = []

    def open(self, req, timeout):
        self.requests.append(req)
        if not self.payloads:
            raise AssertionError("Unexpected HTTP request")
        result = self.payloads.pop(0)
        if isinstance(result, Exception):
            raise result
        return io.BytesIO(json.dumps(result).encode())


def collect(payloads, **kwargs):
    fixture = HttpFixture(payloads)
    result = collect_github_ci(REPO, HEAD, SOURCE, worktree_dirty=False, token="", opener=fixture, **kwargs)
    return result, fixture


class GitHubEvidenceTests(unittest.TestCase):
    def test_success_is_bound_to_full_commit_repository_and_workflow_bytes(self):
        result, fixture = collect(responses())
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["workflow_id"], 12)
        self.assertEqual(result["run"]["attempt"], 1)
        self.assertFalse(result["matrix_jobs_verified"])
        self.assertFalse(result["release_artifacts_verified"])
        self.assertEqual(len(fixture.requests), 4)
        self.assertTrue(all(req.get_method() == "GET" for req in fixture.requests))
        self.assertIn("head_sha=" + HEAD, fixture.requests[1].full_url)
        self.assertTrue(fixture.requests[-1].full_url.endswith("?ref=" + HEAD))

    def test_old_commit_and_empty_history_never_pass(self):
        for runs in ([], [run_record(head_sha="b" * 40)]):
            with self.subTest(runs=runs):
                result, fixture = collect(responses(runs=runs))
                self.assertEqual(result["status"], "not_run")
                self.assertEqual(len(fixture.requests), 2)

    def test_failure_cancelled_skipped_and_pending_are_distinct_from_success(self):
        for conclusion in ("failure", "cancelled", "timed_out", "skipped", "neutral", "action_required", None):
            with self.subTest(conclusion=conclusion):
                result, _ = collect(responses(detail=run_record(conclusion=conclusion)))
                self.assertEqual(result["status"], "failed")
        result, _ = collect(responses(detail=run_record(status="in_progress", conclusion=None)))
        self.assertEqual(result["status"], "pending")

    def test_newest_run_failure_overrides_old_success_regardless_of_list_order(self):
        latest = run_record(id=81, run_number=21, conclusion="failure")
        result, fixture = collect(responses(runs=[latest, run_record()], detail=latest))
        self.assertEqual(result["status"], "failed")
        self.assertTrue(fixture.requests[2].full_url.endswith("/runs/81"))

    def test_detail_refresh_uses_new_attempt_and_rejects_older_attempt(self):
        result, _ = collect(responses(detail=run_record(run_attempt=2, status="queued", conclusion=None)))
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["run"]["attempt"], 2)
        result, _ = collect(responses(runs=[run_record(run_attempt=2)], detail=run_record(run_attempt=1)))
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], "run_identity_or_attempt_mismatch")

    def test_wrong_workflow_source_repo_or_trigger_never_passes(self):
        for override in ({"workflow_id": 13}, {"path": ".github/workflows/other.yml"},
                         {"head_sha": "b" * 40}, {"repository": {"full_name": "other/repo"}},
                         {"head_repository": {"full_name": "fork/repo"}}, {"event": "pull_request"}):
            with self.subTest(override=override):
                result, _ = collect(responses(detail=run_record(**override)))
                self.assertEqual(result["status"], "unavailable")
        wrong_workflow = responses()
        wrong_workflow[0]["path"] = ".github/workflows/other.yml"
        self.assertEqual(collect(wrong_workflow)[0]["reason"], "workflow_identity_mismatch")

    def test_workflow_hash_mismatch_rejects_success(self):
        result, _ = collect(responses(content={"type": "file", "path": WORKFLOW_PATH, "encoding": "base64",
                                               "content": base64.b64encode(b"different workflow").decode()}))
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], "workflow_content_mismatch")

    def test_incomplete_history_and_malformed_payloads_fail_closed(self):
        for malformed in ([1], {"total_count": 101, "workflow_runs": [run_record()]},
                          {"total_count": 1, "workflow_runs": [None]}):
            with self.subTest(malformed=malformed):
                result, _ = collect([{"id": 12, "path": WORKFLOW_PATH}, malformed])
                self.assertEqual(result["status"], "unavailable")

    def test_errors_are_diagnostic_without_returning_credentials_or_error_bodies(self):
        secret = "secret-never-log"
        for failure, expected in ((HTTPError("https://api.github.com/" + secret, 403, secret, {}, None), "forbidden_or_rate_limited"),
                                  (HTTPError("https://api.github.com/", 404, secret, {}, None), "not_found_or_inaccessible"),
                                  (URLError(secret), "network_unavailable"),
                                  (http.client.IncompleteRead(b"partial"), "network_unavailable")):
            with self.subTest(expected=expected):
                fixture = HttpFixture([failure])
                result = collect_github_ci(REPO, HEAD, SOURCE, worktree_dirty=False, token=secret, opener=fixture)
                self.assertEqual(result["status"], "unavailable")
                self.assertEqual(result["reason"], expected)
                self.assertEqual(result["request_stage"], "workflow")
                self.assertNotIn(secret, json.dumps(result))
                self.assertEqual(fixture.requests[0].get_header("Authorization"), "Bearer " + secret)

    def test_no_redirects_and_only_credential_free_remote_names(self):
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere.test"))
        for remote in ("git@github.com:example/memweft.git", "https://github.com/example/memweft.git",
                       "ssh://git@github.com/example/memweft"):
            self.assertEqual(github_repository(remote), REPO)
        for remote in ("https://secret@github.com/example/memweft", "https://other.test/example/memweft", "bad"):
            self.assertIsNone(github_repository(remote))

    def test_dirty_worktree_cannot_reuse_committed_source_success(self):
        fixture = HttpFixture(responses())
        result = collect_github_ci(REPO, HEAD, SOURCE, worktree_dirty=True, token="", opener=fixture)
        self.assertEqual(result["status"], "passed")
        self.assertFalse(result["applies_to_worktree"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".github/workflows").mkdir(parents=True)
            (root / WORKFLOW_PATH).write_bytes(SOURCE)
            def git(_repo, *args):
                return HEAD if args[0] == "rev-parse" else " M README.md"
            with patch("kimi_release_agent._git", side_effect=git), patch("kimi_release_agent.collect_github_ci", return_value=result):
                evidence = collect_evidence(root, fetch_ci=True, github_repo=REPO)
            self.assertEqual(evidence["ci_for_current_source"], "unavailable")
            self.assertTrue(evidence["worktree_dirty"])

    def test_default_collection_has_no_network_and_commit_changes_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("kimi_release_agent.collect_github_ci") as network:
                result = collect_evidence(directory)
                network.assert_not_called()
                self.assertEqual(result["github_ci"]["reason"], "network_not_requested")
            root = Path(directory)
            (root / ".github/workflows").mkdir(parents=True)
            (root / WORKFLOW_PATH).write_bytes(SOURCE)
            with patch("kimi_release_agent._git", side_effect=[HEAD, "", "b" * 40]), patch(
                    "kimi_release_agent.collect_github_ci", return_value={"status": "passed", "applies_to_worktree": True}):
                result = collect_evidence(root, fetch_ci=True, github_repo=REPO)
            self.assertEqual(result["ci_for_current_source"], "unavailable")
            self.assertTrue(result["github_ci"]["local_source_changed_during_capture"])


if __name__ == "__main__":
    unittest.main()
