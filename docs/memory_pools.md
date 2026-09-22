# Configurable memory pools

Fact memories can be private to one Agent, shared by several Agents, or read from a mixture of pools. This API currently supports SQLite. Agents must connect to the same database; separate `in_memory=True` instances do not share data.

`agent_id` identifies the actor. `pool_id` identifies a fact collection. Shared collections are scoped by `(tenant_id, user_id, pool_id)`: reusing a pool name across users or tenants does not share their data. The reserved name `private` selects the existing `(tenant_id, user_id, agent_id)` fact collection. Existing databases and calls without configuration retain their private behavior.

## Python

```python
from memweft import Memory

with Memory("./memory.db") as memory:
    config = {
        "read_pools": [
            {"pool_id": "private", "access": "read_write"},
            {"pool_id": "project", "access": "read_write"},
        ],
        "default_write_pool": "private",
        "conflict_policy": "private_first",
    }
    planner = memory.user("alice", agent_id="planner", memory_config=config)
    executor = memory.user("alice", agent_id="executor", memory_config=config)

    planner.remember("Prefers concise plans", key="style")  # planner only
    record = planner.remember(8002, key="port", pool_id="project")
    print(executor.memories())  # includes shared port, excludes planner's style
    print(executor.session("deploy").context(query="port").text)
    executor.remember(9000, key="port", pool_id="project",
                      expected_revision=record["revision"])
```

`AsyncMemory.user` accepts the same configuration and its read/write methods accept the same keywords. Bind a configured user to `LangGraphMemory` to use these facts in graph context. The LangGraph `BaseStore` adapter's generic documents and checkpoint state retain their existing scope; this configuration controls fact memory.

## Configuration

| Field | Meaning |
| --- | --- |
| `read_pools` | Ordered list of pool bindings. Each has `pool_id` and `access`: `read` (default) or `read_write`. Up to 32 distinct pools; an empty list reads no facts. |
| `default_write_pool` | Pool used by `remember` and `forget` when no pool is specified. Must be bound with `read_write`. Omitted or `null` requires explicit pool selection for writes. |
| `conflict_policy` | `private_first` (default), `read_order`, or `error`. Applies when merging facts with the same key. |

Typical choices:

- **Independent:** omit `memory_config`. This binds only `private`, with read/write access.
- **Shared:** bind only a named pool such as `project`, and use it as the default write pool. Give each Agent a different `agent_id`.
- **Mixed:** bind `private` and one or more named pools. Keep private as the default and explicitly publish shared facts with `pool_id`.
- **Read-only consumer:** bind named pools with `read` and omit `default_write_pool`.

Explicit reads and writes require a matching binding. A write never falls back to another pool. These bindings are trusted application configuration, not database authentication or a global access-control registry. An application must choose allowed bindings before giving an Agent a handle. A caller with raw storage access or the ability to choose arbitrary scope/configuration has broader access.

## Conflicts, provenance and deletion

`private_first` selects private facts first, then shared pools in configured order. `read_order` follows the configured order exactly. `error` rejects different values for the same key. Identical values deduplicate under all policies.

`memories()` merges by key. `memories(pool_id="project")` inspects that pool directly. Returned records add `pool_id`, `writer_agent_id`, and `revision` to the existing fact fields. The Rust `memories()` and `remember()` methods retain their `Fact` return type; use `memory_records()` and `remember_in()` for metadata.

Context resolves pool conflicts before query ranking and budget cuts. `context.explain()["pools"]` reports selected provenance and shadowed keys; the selected list includes only memories actually included in the context. Context's `memories` field retains the existing fact shape.

SQLite now resolves `private_first` and `read_order` eligibility in its candidate
query before Top-K selection. Shadow diagnostics cover retrieved candidate keys,
contain at most 64 entries, and expose `shadowed_scope` and `shadowed_truncated`.
The `error` policy still checks conflicts globally using the full-scan path,
including when `max_facts=0`; its latency can therefore grow with all visible facts.
See [indexed retrieval](indexed_retrieval.md) for migration and diagnostic semantics.

`forget(key)` deletes from the default write pool. `forget(key, pool_id="project")` deletes from that specific pool. Deleting a private override can reveal a shared value with the same key. Inspect the shared pool explicitly when needed. Deleting a shared fact removes it for all readers in that tenant/user, without deleting other users' facts or Agent conversations.

Shared facts support optimistic concurrency via `expected_revision`:

- Omit it for an unconditional write (last writer wins).
- Use `0` to create only if the key is absent.
- Use the returned positive revision to update or delete only that version.

A conflict fails without partial writes. Revision counters survive deletion and recreation, so an old revision cannot overwrite a recreated fact. Deletions retain only the key and revision tombstone, not the fact value. Legacy private facts return `revision: null` and reject revision checks.

## Learning dependencies

Sessions, feedback, jobs, and accepted strategies remain Agent-specific, even when facts are shared. Sharing a pool does not automatically share or adopt another Agent's strategies.

Declare shared sources in learning proposals:

```python
proposal = {
    "task_type": "deploy",
    "content": "Use the verified deployment port",
    "proposer_version": "planner-v1",
    "source_pools": [{"pool_id": "project", "key": "port"}],
}
```

`source_keys` continues to identify **private** fact keys. Shared sources must use `source_pools`, even if they are in the default write pool. Sources must be readable and present when the job starts. Jobs and strategies capture shared revisions; adoption and rollback check those revisions in the same transaction as the strategy change.

Updating or deleting a shared source invalidates dependent jobs, saved strategy versions and active strategies across Agents in the same tenant/user. This also covers dependencies inherited from a baseline strategy. Updating to the same value still advances the revision and invalidates derivatives. Unrelated strategies survive. An Agent handle that no longer reads a required pool does not include that strategy in `active()` or context; learning history remains owned by the Agent.

Dependencies must be declared by the application. MemWeft cannot discover copies placed in arbitrary messages, external documents or model-generated text; those are not automatically removed.

## TypeScript

SDK options use camelCase; learning proposal fields and returned records use the shared snake_case wire format.

```typescript
const config = {
  readPools: [
    { poolId: "private", access: "read_write" as const },
    { poolId: "project", access: "read_write" as const },
  ],
  defaultWritePool: "private",
};
const planner = memory.user("alice", { agentId: "planner", memoryConfig: config });
const executor = memory.user("alice", { agentId: "executor", memoryConfig: config });
const record = await planner.remember(8002, { key: "port", poolId: "project" });
await executor.remember(9000, {
  key: "port", poolId: "project", expectedRevision: record.revision!,
});
console.log(await executor.memories({ poolId: "project" }));
```

## Rust and wire API

Set `UserScope.memory_config` using the exported `MemoryConfig`, `PoolBinding`, `PoolAccess`, and `ConflictPolicy` types. `UserScope::new()` retains default configuration. Existing Rust struct literals need the new field (`memory_config: Default::default()`); `Proposal` literals need `source_pools: vec![]` if they do not use shared sources.

`UserMemory::remember_in(pool, key, value, expected_revision)`, `forget_in(pool, key, expected_revision)`, and `memory_records(pool)` accept optional explicit pool IDs. JSON requests put `memory_config` in `scope`; `remember`, `memories`, and `forget` accept `pool_id` at the request level, and writes accept `expected_revision`.

See [the runnable two-Agent example](../examples/shared_pools.py), [Rust behavior tests](../crates/memweft/tests/pools.rs), and [the shared Rust/Python/Node contract](../tests/contract.json).
