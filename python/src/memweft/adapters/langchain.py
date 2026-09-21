import uuid

from ..client import Memory

try:
    from langchain_core.chat_history import BaseChatMessageHistory
    from langchain_core.messages import (
        AIMessage,
        BaseMessage,
        HumanMessage,
        SystemMessage,
        ToolMessage,
    )

    _LANGCHAIN_AVAILABLE = True
except Exception:  # pragma: no cover - optional dependency
    BaseChatMessageHistory = object
    BaseMessage = object
    AIMessage = HumanMessage = SystemMessage = ToolMessage = None
    _LANGCHAIN_AVAILABLE = False


class MemWeftChatMessageHistory(BaseChatMessageHistory):
    def __init__(self, memory: Memory, scope: dict, limit: int | None = None):
        if not _LANGCHAIN_AVAILABLE:
            raise ImportError("langchain-core is required for MemWeftChatMessageHistory")
        self._memory = memory
        self._scope = scope
        self._limit = limit

    @property
    def messages(self):
        events = self._memory.list_events(self._scope, limit=self._limit)
        messages = []
        for event in events:
            if event.get("kind") != "message":
                continue
            payload = event.get("payload") or {}
            role = payload.get("role", "user")
            content = payload.get("content") or payload.get("text") or ""
            content = content if isinstance(content, str) else str(content)
            messages.append(_message_from_role(role, content))
        return messages

    def add_message(self, message: BaseMessage):
        role = getattr(message, "type", "human")
        content = getattr(message, "content", "")
        if not isinstance(content, str):
            content = str(content)
        event = {
            "event_id": str(uuid.uuid4()),
            "scope": self._scope,
            "kind": "message",
            "payload": {"role": _role_from_message(role), "content": content},
        }
        self._memory.append_event(event)

    def add_user_message(self, message: str):
        self.add_message(HumanMessage(content=message))

    def add_ai_message(self, message: str):
        self.add_message(AIMessage(content=message))

    def clear(self):
        raise NotImplementedError("MemWeft store does not support clearing events yet.")


class MemWeftContextInjector:
    def __init__(self, memory: Memory, scope: dict):
        self._memory = memory
        self._scope = scope

    def build_packet(
        self,
        purpose: str = "planner",
        task_type: str | None = None,
        cues: dict | None = None,
        budget: dict | None = None,
        policy: dict | None = None,
        policy_id: str | None = None,
        persist: bool | None = None,
    ):
        request = {"scope": self._scope, "purpose": purpose}
        if task_type is not None:
            request["task_type"] = task_type
        if cues is not None:
            request["cues"] = cues
        if budget is not None:
            request["budget"] = budget
        if policy is not None:
            request["policy"] = policy
        if policy_id is not None:
            request["policy_id"] = policy_id
        if persist is not None:
            request["persist"] = persist
        return self._memory.build_memory_packet(request)


def _message_from_role(role: str, content: str):
    if role in ("assistant", "ai"):
        return AIMessage(content=content)
    if role == "system":
        return SystemMessage(content=content)
    if role == "tool":
        return ToolMessage(content=content)
    return HumanMessage(content=content)


def _role_from_message(message_type: str) -> str:
    if message_type in ("ai", "assistant"):
        return "assistant"
    if message_type == "system":
        return "system"
    if message_type == "tool":
        return "tool"
    return "user"
