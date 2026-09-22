"""User/session API. Validation, defaults and learning decisions live in Rust."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Context:
    text: str
    memories: list[dict]
    messages: list[dict]
    strategies: list[dict]
    report: dict

    def explain(self) -> dict:
        return self.report


class UserMemory:
    def __init__(self, memory, user_id: str, *, tenant_id: str = "default", agent_id: str = "default", memory_config: dict | None = None):
        self._memory = memory
        self.scope = {"user_id": user_id, "tenant_id": tenant_id, "agent_id": agent_id}
        if memory_config is not None:
            self.scope["memory_config"] = deepcopy(memory_config)
        self.learning = Learning(self)

    def _request(self, op: str, **kwargs):
        return self._memory._request({"op": op, "scope": self.scope, **kwargs})

    def remember(self, value: Any, *, key: str, pool_id: str | None = None, expected_revision: int | None = None) -> dict:
        return self._request("remember", key=key, value=value, pool_id=pool_id, expected_revision=expected_revision)

    def memories(self, *, pool_id: str | None = None) -> list[dict]:
        return self._request("memories", pool_id=pool_id)

    def forget(self, key: str, *, pool_id: str | None = None, expected_revision: int | None = None) -> bool:
        return self._request("forget", key=key, pool_id=pool_id, expected_revision=expected_revision)

    def session(self, session_id: str) -> Session:
        return Session(self, session_id)


class Session:
    def __init__(self, user: UserMemory, session_id: str):
        self.user = user
        self.session_id = session_id

    def add_message(self, role: str, content: str, *, event_id: str | None = None, run_id: str | None = None) -> dict:
        return self.user._request("add_message", session_id=self.session_id, role=role,
                                  content=content, event_id=event_id, run_id=run_id)

    def messages(self) -> list[dict]:
        return self.user._request("messages", session_id=self.session_id)

    def clear(self) -> int:
        """Delete messages recorded through this session API (not user facts)."""
        return self.user._request("clear_session", session_id=self.session_id)

    def context(self, *, max_tokens: int = 2048, conversation_window: int = 10,
                max_facts: int = 30, include_messages: bool = True, task_type: str | None = None,
                query: str | None = None) -> Context:
        return Context(**self.user._request("context", session_id=self.session_id, options={
            "max_tokens": max_tokens, "conversation_window": conversation_window,
            "max_facts": max_facts, "include_messages": include_messages, "task_type": task_type, "query": query,
        }))


class Learning:
    def __init__(self, user: UserMemory):
        self.user = user

    def feedback(self, *, id: str, task_type: str, session_id: str, run_id: str,
                 success: bool, details: Any = None) -> dict:
        return self.user._request("feedback", feedback={"id": id, "task_type": task_type,
            "session_id": session_id, "run_id": run_id, "success": success, "details": details})

    def start(self, *, id: str, proposal: dict, dataset_version: str, evaluator_version: str,
              case_ids: list[str], policy: dict | None = None) -> dict:
        kwargs = dict(id=id, proposal=proposal, dataset_version=dataset_version,
                      evaluator_version=evaluator_version, case_ids=case_ids)
        if policy is not None:
            kwargs["policy"] = policy
        return self.user._request("learning_start", **kwargs)

    def submit(self, id: str, evaluation: dict) -> dict:
        return self.user._request("learning_submit", id=id, evaluation=evaluation)

    def get(self, id: str) -> dict:
        return self.user._request("learning_get", id=id)

    def jobs(self) -> list[dict]:
        return self.user._request("learning_jobs")

    def cancel(self, id: str, reason: str = "") -> dict:
        return self.user._request("learning_cancel", id=id, reason=reason)

    def active(self, task_type: str, *, target: str = "task") -> dict | None:
        return self.user._request("learning_active", task_type=task_type, target=target)

    def rollback(self, task_type: str, *, expected_version: str, version: str | None = None,
                 target: str = "task") -> dict | None:
        return self.user._request("learning_rollback", task_type=task_type, target=target,
                                  version=version, expected_version=expected_version)


class AsyncUserMemory(UserMemory):
    """Same wire API, scheduled through the Rust blocking-task bridge."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.learning = AsyncLearning(self)

    async def _request(self, op: str, **kwargs):
        return await self._memory._request({"op": op, "scope": self.scope, **kwargs})

    async def remember(self, value: Any, *, key: str, pool_id: str | None = None, expected_revision: int | None = None) -> dict:
        return await self._request("remember", key=key, value=value, pool_id=pool_id, expected_revision=expected_revision)

    async def memories(self, *, pool_id: str | None = None) -> list[dict]:
        return await self._request("memories", pool_id=pool_id)

    async def forget(self, key: str, *, pool_id: str | None = None, expected_revision: int | None = None) -> bool:
        return await self._request("forget", key=key, pool_id=pool_id, expected_revision=expected_revision)

    def session(self, session_id: str) -> AsyncSession:
        return AsyncSession(self, session_id)


class AsyncSession(Session):
    async def add_message(self, role, content, *, event_id=None, run_id=None):
        return await super().add_message(role, content, event_id=event_id, run_id=run_id)

    async def messages(self):
        return await super().messages()

    async def clear(self):
        return await super().clear()

    async def context(self, *, max_tokens=2048, conversation_window=10, max_facts=30,
                      include_messages=True, task_type=None, query=None):
        data = await self.user._request("context", session_id=self.session_id, options={
            "max_tokens": max_tokens, "conversation_window": conversation_window,
            "max_facts": max_facts, "include_messages": include_messages, "task_type": task_type, "query": query,
        })
        return Context(**data)


class AsyncLearning(Learning):
    async def feedback(self, **kwargs):
        return await super().feedback(**kwargs)

    async def start(self, **kwargs):
        return await super().start(**kwargs)

    async def submit(self, id, evaluation):
        return await super().submit(id, evaluation)

    async def get(self, id):
        return await super().get(id)

    async def jobs(self):
        return await super().jobs()

    async def cancel(self, id, reason=""):
        return await super().cancel(id, reason)

    async def active(self, task_type, *, target="task"):
        return await super().active(task_type, target=target)

    async def rollback(self, task_type, *, expected_version, version=None, target="task"):
        return await super().rollback(task_type, expected_version=expected_version, version=version, target=target)
