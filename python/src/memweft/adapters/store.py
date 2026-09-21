"""LangGraph BaseStore backed by Rust; no vector index or TTL support yet."""
from __future__ import annotations

from datetime import datetime
from langgraph.store.base import BaseStore, GetOp, PutOp, SearchOp, ListNamespacesOp, Item, SearchItem


def _encode(op):
    if isinstance(op, GetOp):
        return {"kind": "get", "namespace": op.namespace, "key": op.key}
    if isinstance(op, PutOp):
        if op.ttl is not None or (op.index is not None and op.index is not False):
            raise ValueError("MemWeft Store does not support TTL or vector indexing")
        return {"kind": "put", "namespace": op.namespace, "key": op.key, "value": op.value}
    if isinstance(op, SearchOp):
        if op.query is not None:
            raise ValueError("MemWeft Store does not support semantic search")
        return {"kind": "search", "prefix": op.namespace_prefix, "filter": op.filter, "limit": op.limit, "offset": op.offset}
    if isinstance(op, ListNamespacesOp):
        return {"kind": "list", "conditions": [{"match_type": c.match_type, "path": c.path} for c in op.match_conditions or ()],
                "max_depth": op.max_depth, "limit": op.limit, "offset": op.offset}
    raise TypeError(f"Unsupported store operation: {type(op).__name__}")


def _item(data, search=False):
    if data is None:
        return None
    cls = SearchItem if search else Item
    return cls(value=data["value"], key=data["key"], namespace=tuple(data["namespace"]),
               created_at=datetime.fromisoformat(data["created_at"]), updated_at=datetime.fromisoformat(data["updated_at"]))


def _decode(ops, results):
    output = []
    for op, result in zip(ops, results, strict=True):
        if isinstance(op, GetOp):
            output.append(_item(result))
        elif isinstance(op, SearchOp):
            output.append([_item(d, search=True) for d in result])
        elif isinstance(op, ListNamespacesOp):
            output.append([tuple(ns) for ns in result])
        else:
            output.append(None)
    return output


class MemWeftStore(BaseStore):
    """Bind one user's namespace to a graph; construct a separate store per user.

    Accepts a synchronous Memory instance. abatch uses its native async bridge,
    so the same store supports graph.invoke and graph.ainvoke.
    """
    def __init__(self, memory, user_id: str, *, tenant_id="default", agent_id="default"):
        self._memory = memory
        self._scope = {"user_id": user_id, "tenant_id": tenant_id, "agent_id": agent_id}

    def batch(self, ops):
        ops = list(ops)
        request = {"op": "store_batch", "scope": self._scope, "operations": [_encode(op) for op in ops]}
        return _decode(ops, self._memory._request(request))

    async def abatch(self, ops):
        import json
        ops = list(ops)
        request = {"op": "store_batch", "scope": self._scope, "operations": [_encode(op) for op in ops]}
        data = await self._memory._store.async_request(json.dumps(request, allow_nan=False))
        return _decode(ops, json.loads(data))
