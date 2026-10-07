"""Application-owned completion receipts for bounded, serial tool sessions.

Register trusted handlers and classify their effects in application code. Handlers
receive the entire model action and must validate its fields, paths and values.
A successful public check certifies only that check; it does not prove business
correctness, authorize external actions, or make model-supplied rules trusted.

Receipts live only in this object and expire before every mutation or verification
attempt. The snapshot callback must cover all state relevant to completion,
including source versions if they live outside the project. This small example
assumes exclusive, serial use; snapshots do not prevent concurrent TOCTOU races.
It never runs tools automatically, repairs results, or extends a round budget.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
from types import MappingProxyType
from typing import Any, Callable, Literal


@dataclass(frozen=True)
class TrustedTool:
    handler: Callable[[dict[str, Any]], Any]
    effect: Literal["read", "mutate", "verify"]

    def __post_init__(self):
        if not callable(self.handler) or self.effect not in ("read", "mutate", "verify"):
            raise ValueError("A trusted tool requires a callable and a declared effect")


def file_snapshot(root: Path | str) -> str:
    """Hash relative paths and contents of every regular file in an isolated root.

    The root and descendants must not be symlinks or special files. Empty
    directories and file metadata are outside this content snapshot. Applications
    whose checks depend on permissions or external state need a richer callback.
    """
    root = Path(root)
    if not stat.S_ISDIR(root.lstat().st_mode):
        raise ValueError("Snapshot root must be an existing directory, not a symlink")
    rows = []

    def fail(error):
        raise error

    for directory, directories, files in os.walk(root, followlinks=False, onerror=fail):
        directories.sort()
        files.sort()
        for name in directories + files:
            path = Path(directory) / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise ValueError("Snapshot does not permit symlinks or special files")
            if stat.S_ISREG(mode):
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
                rows.append((path.relative_to(root).as_posix(), digest.hexdigest()))
    encoded = json.dumps(sorted(rows), ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _strict_json(content):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON field")
            result[key] = value
        return result

    def invalid(_value):
        raise ValueError("Non-JSON numeric constant")

    return json.loads(content, object_pairs_hook=unique, parse_constant=invalid)


class VerifiedSession:
    def __init__(self, tools: dict[str, TrustedTool], snapshot: Callable[[], str],
                 max_rounds: int = 4, max_actions: int = 3):
        if (type(max_rounds) is not int or max_rounds < 1
                or type(max_actions) is not int or max_actions < 1):
            raise ValueError("Round and action budgets must be positive integers")
        if not callable(snapshot):
            raise ValueError("Snapshot must be an application-owned callable")
        if (not isinstance(tools, dict)
                or any(not isinstance(name, str) or not name or not isinstance(tool, TrustedTool)
                       for name, tool in tools.items())):
            raise ValueError("Tools must map nonempty names to trusted tools")
        self._tools = MappingProxyType(dict(tools))
        self._snapshot = snapshot
        self._max_actions = max_actions
        self._remaining = max_rounds
        self._status = "active"
        self._receipt = None
        self._reason = "verification_required"

    @property
    def status(self):
        return self._status

    @property
    def rounds_remaining(self):
        return self._remaining

    def _invalidate(self, reason):
        self._receipt = None
        self._reason = reason

    def _capture(self):
        snapshot = self._snapshot()
        if not isinstance(snapshot, str) or not snapshot:
            raise ValueError("Snapshot must return a nonempty string")
        return snapshot

    def _refresh(self):
        if self._receipt is not None:
            try:
                current = self._capture()
            except Exception:
                self._invalidate("snapshot_failed")
            else:
                if current != self._receipt:
                    self._invalidate("project_changed")

    def _result(self, results, requested, accepted, protocol_error, reason=None):
        return {"results": results, "done_requested": requested, "done_accepted": accepted,
                "status": self._status, "rounds_remaining": self._remaining,
                "completion": {"verified": self._receipt is not None,
                               "reason": reason or self._reason},
                "protocol_error": protocol_error}

    def step(self, content: str, finish_reason: str = "stop"):
        """Consume one model turn; accept done only against a fresh trusted receipt.

        Terminal calls execute nothing and consume no further budget. Protocol
        errors, tool exceptions and failed checks prevent completion in that turn;
        remaining turns may retry. A check must return a dict with ``ok is True``.
        """
        if self._status != "active":
            self._refresh()
            return self._result([], False, False, False, "session_terminal")
        self._remaining -= 1
        results, requested, protocol_error, tool_error = [], False, False, False
        try:
            body = _strict_json(content)
            if (finish_reason != "stop" or not isinstance(body, dict)
                    or set(body) != {"actions", "done"} or type(body["done"]) is not bool
                    or not isinstance(body["actions"], list) or len(body["actions"]) > self._max_actions):
                raise ValueError("Expected bounded actions and boolean done in a complete JSON object")
            requested = body["done"]
        except (ValueError, TypeError, RecursionError) as error:
            protocol_error = True
            results.append({"error": str(error)})
        else:
            for action in body["actions"]:
                name = action.get("tool") if isinstance(action, dict) else None
                try:
                    if not isinstance(name, str) or name not in self._tools:
                        raise ValueError("Action must name a registered tool")
                    tool = self._tools[name]
                    if tool.effect in ("mutate", "verify"):
                        self._invalidate("verification_required")
                    value = tool.handler(action)
                    if tool.effect == "verify":
                        if isinstance(value, dict) and value.get("ok") is True:
                            try:
                                self._receipt = self._capture()
                            except Exception:
                                self._invalidate("snapshot_failed")
                                raise
                            self._reason = "verified"
                        else:
                            self._invalidate("verification_failed")
                            tool_error = True
                    results.append({"tool": name, "result": value})
                except Exception as error:
                    tool_error = True
                    results.append({"tool": name, "error": str(error)})
        self._refresh()
        accepted = requested and self._receipt is not None and not protocol_error and not tool_error
        if accepted:
            self._status = "completed"
        elif self._remaining == 0:
            self._status = "budget_exhausted"
        reason = "protocol_error" if protocol_error else "tool_error" if tool_error else None
        return self._result(results, requested, accepted, protocol_error, reason)
