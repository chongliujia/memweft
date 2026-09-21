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
See the [repository guide](../README.md) for `AsyncMemory`, LangGraph `BaseStore`,
learning jobs, budget semantics and the existing low-level interface.

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
