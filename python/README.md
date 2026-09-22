# MemWeft Python SDK

This package provides a thin Python wrapper around the MemWeft Rust core.

## Quick start

```python
from memweft import Memory

with Memory("data/memweft.db") as memory:
    alice = memory.user("alice")
    alice.remember("Prefers concise answers", key="reply_style")
    chat = alice.session("chat-001")
    chat.add_message("user", "Explain Rust ownership", event_id="question-1")
    print(chat.context(max_tokens=1000).text)
```

The high-level API supports SQLite and an optional evaluated-learning workflow.
File stores can opt into periodic background WAL maintenance with
`Memory(path, sqlite_options={"background_checkpoint_ms": 1000})`; `AsyncMemory`
accepts the same option. The valid interval is 100–60,000 ms. Defaults remain
unchanged, and in-memory databases reject this option. Writes still commit before
returning; this is not a queue of deferred writes. See [pipeline and checkpoint
semantics](../docs/async_pipeline.md) for shutdown, failure fallback, WAL growth
and durability limits.

Add `"wal_reclaim_threshold_bytes": 16 * 1024 * 1024` to those options to
attempt WAL truncation above a soft size threshold (64 KiB–1 TiB). Active readers
can delay reclamation and let the file exceed it. `memory.storage_status()`
(or `await async_memory.storage_status()`) reports the actual SQLite version and
per-instance checkpoint progress, busy attempts, sampled bytes and errors.
Enabled instances coordinate maintenance through a persistent
`<database>.memweft-maintenance` file lock. Busy reclamation backs off up to
30 seconds; successful reclamation has a 30-second cooldown. Use consistent
options for all instances; inspect `checkpoint.progress.coordinator_role` across
all of them, since a reader can lead maintenance. Do not delete the sidecar while
any instance is open. Native builds require Rust 1.89+.

See the [repository guide](../README.md) for `AsyncMemory`, LangGraph `BaseStore`,
learning jobs, budget semantics and the existing low-level interface.

`chat.context(query="当前部署端口", max_facts=30)` ranks facts by lexical relevance
before applying context limits. Inspect `context.explain()["recall"]` for matched
terms and scores. `AsyncSession.context` and `LangGraphMemory.context` also accept
`query`; omitting it preserves key order. This does not perform vector search or
provide semantic similarity. Indexed candidate selection and exact fallback paths
are described in the [retrieval guide](../docs/indexed_retrieval.md).

## Agent memory pools

`memory.user("alice", agent_id="planner", memory_config=config)` binds private, shared or mixed fact pools. `remember` and `forget` accept `pool_id` and `expected_revision`; `memories(pool_id=...)` inspects one pool. The same options work with `AsyncMemory`. See the [configuration and learning dependency guide](../docs/memory_pools.md) and [two-Agent example](../examples/shared_pools.py).

## Install (with database backends)

SQLite-only (default):

```bash
maturin develop
```

Enable MySQL/Postgres (rebuild required):

```bash
maturin develop --features mysql,postgres
```

## Backends

SQLite (default):

```python
mem = Memory()
```

MySQL / Postgres (compile with features first):

```bash
maturin develop --features mysql,postgres
```

```python
mem = Memory(
    backend="mysql",
    dsn="mysql://user:pass@localhost:3306/memweft",
)

mem = Memory(
    backend="postgres",
    dsn="postgres://user:pass@localhost:5432/memweft",
)

# Use a separate database name
mem = Memory(
    backend="mysql",
    dsn="mysql://user:pass@localhost:3306",
    database="memweft",
)
```

Notes:
- If the DSN omits a database name, it defaults to `memweft`.
- If the database does not exist, it will be created on first connect.

## Tests

```bash
maturin develop --features mysql,postgres
MEMWEFT_TEST_MYSQL_DSN="mysql://user:pass@localhost:3306/memweft" \
MEMWEFT_TEST_POSTGRES_DSN="postgres://user:pass@localhost:5432/memweft" \
python -m unittest python/tests/test_backends.py
```

## Benchmarks (Python)

```bash
maturin develop --features mysql,postgres
MEMWEFT_BENCH_MYSQL_DSN="mysql://user:pass@localhost:3306/memweft" \
MEMWEFT_BENCH_POSTGRES_DSN="postgres://user:pass@localhost:5432/memweft" \
python python/scripts/bench_backends.py
```

Outputs:
- `target/python_bench.json`
- `target/python_bench.html`
- `target/python_bench_prev.json` (auto-saved previous run)

Config file (optional):

- Copy `bench/memweft_bench.env.example` to `bench/memweft_bench.env`.
- Set `MEMWEFT_BENCH_CONFIG=/path/to/memweft_bench.env` to use a custom path.
- The Python benchmark scripts read this file for defaults if present.

## Load test (Python)

```bash
maturin develop --features mysql,postgres
MEMWEFT_LOAD_MYSQL_DSN="mysql://user:pass@localhost:3306/memweft" \
MEMWEFT_LOAD_POSTGRES_DSN="postgres://user:pass@localhost:5432/memweft" \
python python/scripts/load_test.py --duration 60 --concurrency 8
```

Outputs:
- `target/python_load.json`
- `target/python_load_prev.json` (auto-saved previous run)

## Soak test (Python)

```bash
maturin develop --features mysql,postgres
MEMWEFT_SOAK_MYSQL_DSN="mysql://user:pass@localhost:3306/memweft" \
MEMWEFT_SOAK_POSTGRES_DSN="postgres://user:pass@localhost:5432/memweft" \
python python/scripts/soak_test.py --duration 600 --interval 60
```

Outputs:
- `target/python_soak.json`
- `target/python_soak_prev.json` (auto-saved previous run)

SQLite context queries now use a transactional inverted index with the existing
lexical scoring. Diagnostic arrays are capped at 64; check `omissions_truncated`,
`recall.candidates_truncated` and `pools.shadowed_truncated` before interpreting
counts. Existing databases require an index backfill and coordinated writer
upgrade; see [indexed retrieval and migration](../docs/indexed_retrieval.md).
