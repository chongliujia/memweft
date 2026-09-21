import uuid
import warnings

from ..client import Memory


class WorkingStateAccessor:
    def __init__(self, memory: Memory, scope: dict):
        self._memory = memory
        self._scope = scope

    def get(self):
        return self._memory.get_working_state(self._scope)

    def put(self, state: dict):
        return self._memory.patch_working_state(self._scope, state)


class MemWeftCheckpointer(WorkingStateAccessor):
    def __init__(self, *args, **kwargs):
        warnings.warn("MemWeftCheckpointer is a working-state helper, not a LangGraph checkpointer. Use WorkingStateAccessor or a LangGraph checkpoint saver.", DeprecationWarning, stacklevel=2)
        super().__init__(*args, **kwargs)


class LangGraphMemory:
    """Node helper. LangGraph owns message history; MemWeft supplies long-term context."""
    def __init__(self, user):
        self.user = user

    def context(self, thread_id: str, **options):
        options["include_messages"] = False
        return self.user.session(thread_id).context(**options)

    def feedback(self, **feedback):
        return self.user.learning.feedback(**feedback)


class MemWeftNodeMiddleware:
    def __init__(self, memory: Memory, scope: dict):
        self._memory = memory
        self._scope = scope

    def before_node(self, purpose: str = "planner", task_type: str | None = None):
        request = {"scope": self._scope, "purpose": purpose}
        if task_type is not None:
            request["task_type"] = task_type
        return self._memory.build_memory_packet(request)

    def after_node(self, events: list[dict]):
        for event in events:
            if "event_id" not in event:
                event["event_id"] = str(uuid.uuid4())
            if "scope" not in event:
                event["scope"] = self._scope
            self._memory.append_event(event)
