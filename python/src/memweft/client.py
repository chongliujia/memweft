import json

from ._core import MemWeftStore


class Memory:
    def close(self):
        self._store = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def user(self, user_id: str, *, tenant_id: str = "default", agent_id: str = "default"):
        from .api import UserMemory
        return UserMemory(self, user_id, tenant_id=tenant_id, agent_id=agent_id)

    def _request(self, request):
        if self._store is None:
            raise RuntimeError("Memory is closed")
        return json.loads(self._store.request(json.dumps(request, allow_nan=False)))

    def __init__(
        self,
        path="data/memweft.db",
        in_memory=False,
        backend="sqlite",
        dsn=None,
        database=None,
    ):
        self._store = MemWeftStore(
            path=path,
            backend=backend,
            dsn=dsn,
            database=database,
            in_memory=in_memory,
        )

    def append_event(self, event):
        self._store.append_event(json.dumps(event))

    def list_events(self, scope, time_range=None, limit=None):
        payload = json.dumps(time_range) if time_range is not None else None
        return json.loads(self._store.list_events(json.dumps(scope), payload, limit))

    def get_working_state(self, scope):
        data = self._store.get_working_state(json.dumps(scope))
        return json.loads(data) if data is not None else None

    def patch_working_state(self, scope, patch):
        return json.loads(
            self._store.patch_working_state(json.dumps(scope), json.dumps(patch))
        )

    def get_stm(self, scope):
        data = self._store.get_stm(json.dumps(scope))
        return json.loads(data) if data is not None else None

    def update_stm(self, scope, stm_state):
        self._store.update_stm(json.dumps(scope), json.dumps(stm_state))

    def list_facts(self, scope, fact_filter=None):
        payload = json.dumps(fact_filter) if fact_filter is not None else None
        return json.loads(self._store.list_facts(json.dumps(scope), payload))

    def upsert_fact(self, scope, fact):
        self._store.upsert_fact(json.dumps(scope), json.dumps(fact))

    def list_episodes(self, scope, episode_filter=None):
        payload = json.dumps(episode_filter) if episode_filter is not None else None
        return json.loads(self._store.list_episodes(json.dumps(scope), payload))

    def append_episode(self, scope, episode):
        self._store.append_episode(json.dumps(scope), json.dumps(episode))

    def list_procedures(self, scope, task_type, limit=None):
        return json.loads(
            self._store.list_procedures(json.dumps(scope), task_type, limit)
        )

    def upsert_procedure(self, scope, procedure):
        self._store.upsert_procedure(json.dumps(scope), json.dumps(procedure))

    def list_insights(self, scope, insight_filter=None):
        payload = json.dumps(insight_filter) if insight_filter is not None else None
        return json.loads(self._store.list_insights(json.dumps(scope), payload))

    def append_insight(self, scope, insight):
        self._store.append_insight(json.dumps(scope), json.dumps(insight))

    def write_context_build(self, scope, packet):
        self._store.write_context_build(json.dumps(scope), json.dumps(packet))

    def list_context_builds(self, scope, limit=None):
        return json.loads(self._store.list_context_builds(json.dumps(scope), limit))

    def build_memory_packet(self, request):
        return json.loads(self._store.build_memory_packet(json.dumps(request)))


class AsyncMemory:
    def close(self):
        self._store = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.close()

    def user(self, user_id: str, *, tenant_id: str = "default", agent_id: str = "default"):
        from .api import AsyncUserMemory
        return AsyncUserMemory(self, user_id, tenant_id=tenant_id, agent_id=agent_id)

    async def _request(self, request):
        if self._store is None:
            raise RuntimeError("Memory is closed")
        return json.loads(await self._store.async_request(json.dumps(request, allow_nan=False)))

    def __init__(
        self,
        path="data/memweft.db",
        in_memory=False,
        backend="sqlite",
        dsn=None,
        database=None,
    ):
        self._store = MemWeftStore(
            path=path,
            backend=backend,
            dsn=dsn,
            database=database,
            in_memory=in_memory,
        )

    async def append_event(self, event):
        await self._store.async_append_event(json.dumps(event))

    async def list_events(self, scope, time_range=None, limit=None):
        payload = json.dumps(time_range) if time_range is not None else None
        data = await self._store.async_list_events(json.dumps(scope), payload, limit)
        return json.loads(data)

    async def get_working_state(self, scope):
        data = await self._store.async_get_working_state(json.dumps(scope))
        return json.loads(data) if data is not None else None

    async def patch_working_state(self, scope, patch):
        data = await self._store.async_patch_working_state(
            json.dumps(scope), json.dumps(patch)
        )
        return json.loads(data)

    async def get_stm(self, scope):
        data = await self._store.async_get_stm(json.dumps(scope))
        return json.loads(data) if data is not None else None

    async def update_stm(self, scope, stm_state):
        await self._store.async_update_stm(json.dumps(scope), json.dumps(stm_state))

    async def list_facts(self, scope, fact_filter=None):
        payload = json.dumps(fact_filter) if fact_filter is not None else None
        data = await self._store.async_list_facts(json.dumps(scope), payload)
        return json.loads(data)

    async def upsert_fact(self, scope, fact):
        await self._store.async_upsert_fact(json.dumps(scope), json.dumps(fact))

    async def list_episodes(self, scope, episode_filter=None):
        payload = json.dumps(episode_filter) if episode_filter is not None else None
        data = await self._store.async_list_episodes(json.dumps(scope), payload)
        return json.loads(data)

    async def append_episode(self, scope, episode):
        await self._store.async_append_episode(json.dumps(scope), json.dumps(episode))

    async def list_procedures(self, scope, task_type, limit=None):
        data = await self._store.async_list_procedures(
            json.dumps(scope), task_type, limit
        )
        return json.loads(data)

    async def upsert_procedure(self, scope, procedure):
        await self._store.async_upsert_procedure(json.dumps(scope), json.dumps(procedure))

    async def list_insights(self, scope, insight_filter=None):
        payload = json.dumps(insight_filter) if insight_filter is not None else None
        data = await self._store.async_list_insights(json.dumps(scope), payload)
        return json.loads(data)

    async def append_insight(self, scope, insight):
        await self._store.async_append_insight(json.dumps(scope), json.dumps(insight))

    async def write_context_build(self, scope, packet):
        await self._store.async_write_context_build(json.dumps(scope), json.dumps(packet))

    async def list_context_builds(self, scope, limit=None):
        data = await self._store.async_list_context_builds(json.dumps(scope), limit)
        return json.loads(data)

    async def build_memory_packet(self, request):
        data = await self._store.async_build_memory_packet(json.dumps(request))
        return json.loads(data)
