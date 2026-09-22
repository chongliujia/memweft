# MemWeft

**Persistent memory and evaluated strategy improvement for AI agents.**

MemWeft provides a Rust core with Python and TypeScript SDKs. Give an agent persistent facts, conversation history and relevant context; let multiple agents share selected memory pools; evaluate candidate strategies before adopting them.

The high-level memory, document and learning APIs currently use **SQLite**. Optional PostgreSQL and MySQL backends are available through the older low-level API. Basic memory operations run locally without a model service or API key.

[Quickstart](#quickstart) · [Memory pools](#private-and-shared-memory-pools) · [Agent integration](#langgraph-and-local-agent-testing) · [Measured results](#measured-results) · [Development](#development-and-verification) · [Guides](#guides-and-project-layout)

## What works today

| Capability | Available behavior |
| --- | --- |
| Persistent facts | Remember, update, inspect and forget facts within tenant/user/agent scopes |
| Configurable memory pools | Private, shared or mixed facts; per-pool read/write access, conflict policies and revision checks |
| Conversations and context | Persistent sessions, idempotent message IDs, estimated context budgets and selection explanations |
| Indexed retrieval | Exact lexical ranking, bounded candidate loading and safe fallback when early stopping cannot be proven |
| Evaluated learning | Feedback, pinned evaluation cases, acceptance gates, strategy versions, source invalidation and rollback |
| Agent integration | Python LangGraph and LangGraph.js context helpers and `BaseStore` adapters |
| SQLite maintenance | Optional background checkpointing, one active maintainer per database, crash takeover, reclamation backoff and diagnostics |

**Enterprise deployment is the development target, not a completed certification.** Current evidence includes real local-model Agent runs and million-fact storage tests. Distributed sharding, hot-data preloading, RDMA and a complete query/loading/compression pipeline remain design work. Automatic conversation extraction is not a bundled SDK capability; applications currently supply facts explicitly.

## Build and install

Build from this repository. Native extensions require **Rust 1.89+** and a C toolchain. Registry publishing and automatic platform package selection remain future work.

### Python

Use Python 3.10–3.12 with the current PyO3 binding. From the repository root:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install maturin
cd python
maturin develop
cd ..
python examples/quickstart.py
```

On Windows, activate with `.venv\Scripts\activate`. Use one active Python environment when building and running the extension. See the [Python SDK guide](python/README.md).

### TypeScript / Node.js

Use Node.js 20+; local validation uses Node 20. From the repository root:

```bash
cd typescript
npm ci
npm run build:native
npm run build
npm test
cd ..
```

The native addon targets Node.js. Browser/Edge access and an HTTP service remain future work. See the [TypeScript SDK guide](typescript/README.md).

### Rust / CLI

```bash
cargo run --locked -p memweft -- remember alice reply_style "Prefers concise answers"
cargo run --locked -p memweft -- context alice chat-001
cargo run --locked -p memweft -- memories alice
```

Put `--db PATH` before the subcommand to choose a database. `memweft request FILE.json` executes the shared JSON request interface, including learning operations. The CLI does not call a model automatically.

## Quickstart

### Python

```python
from memweft import Memory

with Memory("./memory.db") as memory:
    alice = memory.user("alice", tenant_id="my-app", agent_id="assistant")
    alice.remember("Prefers concise answers", key="reply_style")

    chat = alice.session("chat-001")
    chat.add_message("user", "Explain Rust ownership", event_id="question-1")
    context = chat.context(query="reply style", max_tokens=1000)
    print(context.text)
    print(context.explain())
```

Run it twice: the fact is updated by key, and the stable event ID prevents a duplicate message. Use `alice.memories()` to inspect facts, `alice.forget("reply_style")` to delete a fact, and `chat.clear()` to clear session messages.

### TypeScript

```typescript
import { Memory } from "memweft";

const memory = await Memory.open({ path: "./memory.db" });
try {
  const alice = memory.user("alice", { tenantId: "my-app", agentId: "assistant" });
  await alice.remember("Prefers concise answers", { key: "reply_style" });

  const chat = alice.session("chat-001");
  await chat.addMessage("user", "Explain Rust ownership", { eventId: "question-1" });
  const context = await chat.context({ query: "reply style", maxTokens: 1000 });
  console.log(context.text);
} finally {
  await memory.close();
}
```

Python also supports `async with AsyncMemory(...)`; its I/O methods are awaitable. TypeScript I/O methods return promises. Rust applications use the workspace `memweft` crate; see the [Rust example](crates/memweft/examples/learning.rs).

The `query` option prioritizes facts before count and budget limits are applied. Ranking uses distinct word/number matches and adjacent Chinese ideograph pairs, with key matches weighted twice. This is lexical retrieval; BM25 and vector search are not implemented. Without a query, facts use stable key order. See [retrieval behavior and migration](docs/indexed_retrieval.md).

## Private and shared memory pools

Distinct `agent_id` values have independent facts by default. Add named pools to share facts between agents within the same tenant/user scope:

```python
from memweft import Memory

config = {
    "read_pools": [
        {"pool_id": "private", "access": "read_write"},
        {"pool_id": "project", "access": "read_write"},
    ],
    "default_write_pool": "private",
    "conflict_policy": "private_first",
}

with Memory("./team.db") as memory:
    planner = memory.user("alice", agent_id="planner", memory_config=config)
    executor = memory.user("alice", agent_id="executor", memory_config=config)

    planner.remember("Plan before executing", key="work_style")  # private
    planner.remember(8002, key="service_port", pool_id="project")  # shared
    print(executor.session("deploy").context(query="service_port").text)
```

The executor can recall the shared port; the planner's private work style remains isolated. Sessions and learning records remain agent-specific even when facts are shared.

Configure read-only bindings, explicit write targets, `private_first` / `read_order` / `error` conflict handling, and `expected_revision` checks for concurrent shared updates. TypeScript uses `memoryConfig`. Scope IDs must come from your application's trusted identity boundary. See the [pool guide](docs/memory_pools.md) or run `python examples/shared_pools.py`.

## LangGraph and local Agent testing

Two framework adapters are available in Python and JavaScript:

- `LangGraphMemory` supplies long-term context and accepts task feedback. It excludes MemWeft's message window so framework-managed history is not duplicated.
- `MemWeftStore` implements `BaseStore` for scoped JSON documents: CRUD, filtering, pagination, namespace listing and batches. Documents are separate from context-visible facts; search does not support vectors or TTL.

Use an existing framework checkpointer for graph execution state. The deprecated `MemWeftCheckpointer` wraps working state and is not a LangGraph checkpoint saver.

Runnable framework examples, without model credentials:

```bash
python -m pip install -r python/requirements-test.txt
python examples/langgraph_memory.py
node typescript/examples/langgraph.mjs
```

The [local Agent example](examples/local_memory_agent.py) runs a real LangGraph recall → answer graph using the Python SDK and a chat-completions model. To replay memory lifecycle and learning tests against the local Qwen server, run on the machine hosting the endpoint:

```bash
PYTHONPATH=python/src python evals/run_agent_lifecycle.py \
  --output data/evals/agent-lifecycle-new-run \
  --base-url http://127.0.0.1:8002/v1 --model qwen3-8b --repeats 2
```

The API key defaults to `EMPTY`. The runner requires the server's supported Qwen request options and strict JSON Schema output; see [evaluation setup](evals/README.md). Each run needs a new output directory and preserves requests, responses, recalled context, scores and adoption evidence. Raw artifacts stay under Git-ignored `data/evals/`; report summaries are versioned in `evals/reports/`.

## Evaluated learning

Learning changes the strategy included in an agent's prompt, not model weights. The workflow is explicit:

1. Record feedback and propose a task or reflection strategy with declared source keys.
2. Start a job with pinned dataset/evaluator versions and evaluation case IDs.
3. Measure baseline and candidate results, then submit scores, cost and latency.
4. Adopt only if the policy passes; otherwise retain the baseline. Inspect, cancel or roll back through the SDK.

The default policy requires at least three cases, mean score gain of at least 0.05, no per-case regression, total candidate cost at most 1.0 in the evaluator's cost unit, and latency at most 30,000 ms per case. Evaluators are trusted application components: MemWeft validates submissions and gates, but cannot establish whether externally supplied scores are truthful.

Accepted strategies enter context for the matching `task_type` / `taskType`. Revision checks prevent competing candidates from overwriting a changed baseline. Source changes or deletion invalidate dependent strategies. Rust exposes `Proposer`, `Evaluator` and `Learning::improve` for one bounded round; automatic model-backed proposal generation and multi-round scheduling are not bundled.

See the [Python learning API](python/README.md), [TypeScript learning API](typescript/README.md) and [learning design](docs/rust_learning_and_integrations.md). `cargo run --locked -p memweft --example learning` demonstrates the state machine with synthetic scores; model-backed evidence is listed below.

## Measured results

Local results from **2026-09-22**; these are workload-specific observations, not production SLOs.

| Evaluation | Observed result | Evidence and limits |
| --- | --- | --- |
| LangGraph + local `qwen3-8b` | 44/44 lifecycle checks; learning test score 34/48 → 48/48, no regressions; 284 model calls | [Agent report](evals/reports/2026-09-22-wal-and-agent.md). Synthetic business inputs and a previously evaluated split; 24 learning test tasks repeated twice, not a new blind test |
| Broader enterprise scenarios | 2,385 model calls across three tasks, including poisoned-label and injection controls | [Enterprise report](evals/reports/2026-09-22-enterprise-v1.md). Historical-message injection and tool-approval boundary failures remain unresolved |
| Exact indexed retrieval | 1,273 differential ranking checks; million-fact queries retain expected context | [Retrieval follow-up](evals/reports/2026-09-22-wal-and-agent.md). Common fast paths around 1–3 ms p95; a difficult two-term query still around 338 ms p95 |
| Coordinated WAL maintenance | Write p99 33.58 → 4.32 ms; 14,109 → 14,942 of 15,000 planned writes completed | [Latest maintenance report](evals/reports/2026-09-22-wal-coordination.md). Million facts, 8 readers + 1 writer, 5 minutes per run; both new runs had zero successful WAL truncations |

The Agent replay covers updates, shared/private precedence, tenant/user/agent isolation, deletion, reopening, strategy adoption and source invalidation. Its no-memory lifecycle controls correctly return unknown values; their passing checks do not mean they answered business facts. It does not cover real tool execution, production conversations or automatic fact extraction.

The maintenance comparison used sequential local runs, not an isolated performance lab. Lower write tail latency came with delayed space reclamation: the final WAL peak was 117.33 MiB against a 16 MiB soft threshold. Million-fact tests do not establish ten-million or hundred-million scale support.

## Operational behavior and limits

- **Context budget:** estimated as `ceil(UTF-8 bytes / 4)`, not by a model tokenizer. The budget applies to returned context text, not the entire structured response or complete model prompt. Explanations report selections and omissions.
- **Retrieval:** indexed candidate bodies are bounded by `max_facts + 64`; omission and shadowed-key samples are capped at 64 with completeness flags. Frequent terms can still require substantial index work. Strict pool conflict mode (`error`) retains full-scan resolution. Read the [index migration notes](docs/indexed_retrieval.md) before upgrading an existing database.
- **Forgetting:** deletes matching facts, scoped context snapshots and dependent learning records/strategies that declare those sources. Unrelated messages, application documents, feedback and content already copied into external prompts require separate deletion. High-level sessions do not automatically import older low-level event history.
- **Retries and cancellation:** use stable message/job IDs. Cancelling an awaiting SDK call does not guarantee cancellation of an already-running database write.
- **Background maintenance:** opt in with SQLite options; automatic checkpointing remains the default. Coordinated maintenance uses a persistent local sidecar lock, requires consistent configuration across participating instances, and reports per-instance status. Reclamation thresholds are soft; active readers can prevent truncation. See [configuration, takeover and diagnostics](docs/async_pipeline.md).
- **Durability and platform scope:** SQLite uses WAL with `synchronous=NORMAL`; process-crash tests do not prove power-loss durability. The bundled SQLite 3.51.3 includes the official WAL-reset fix; [source provenance and licenses](vendor/libsqlite3-sys/MEMWEFT-PATCH.md) are preserved. Local validation covers Linux; macOS and Windows require successful CI runs.

## Development and verification

After building both native extensions, run from the repository root with the Python environment active:

```bash
cargo test --locked
python -m pip install -r python/requirements-test.txt
PYTHONPATH=python/src python -m unittest discover -s python/tests -v
PYTHONPATH=python/src python -m unittest discover -s evals -p 'test_*.py' -v
cd typescript
npm run build
npm test
cd ..
```

The latest recorded verification passed **105 tests**: Rust 45, Python 15, Node 7 and evaluation tests 38; two optional database-DSN tests skipped. This count is separate from model-call and load-test results. Rust, Python and TypeScript replay [the same contract fixture](tests/contract.json). Coverage includes persistence, isolation, retries, learning gates, indexed ranking, shared revisions, crash recovery and maintenance takeover.

The [CI workflow](.github/workflows/verify.yml) defines Linux, macOS and Windows builds and uploads Python wheels, Node packages and CLI artifacts. See [evaluation commands](evals/README.md) for model runs, storage benchmarks and report reproduction.

## Guides and project layout

| Path | Purpose |
| --- | --- |
| [Memory pools](docs/memory_pools.md) | Independent/shared configuration, precedence and revision semantics |
| [Indexed retrieval](docs/indexed_retrieval.md) | Ranking, bounded loading, fallbacks and migration |
| [Async maintenance](docs/async_pipeline.md) | Current checkpoint implementation and future pipeline design |
| [Preloading and scale](docs/preloading_and_scale.md) | Future memory budgets, sharding and conditional RDMA evaluation |
| [Retrieval research](docs/retrieval_research.md) | Elasticsearch/Lucene algorithm research and applicability |
| [Evaluation guide](evals/README.md) | Reproducible scenarios, benchmarks and report history |
| [`crates/memweft`](crates/memweft/) | User/session API, context builder, shared request interface and CLI |
| [`crates/memweft-learning`](crates/memweft-learning/) | Evaluation gates, strategy versions and rollback |
| [`crates/memweft-store`](crates/memweft-store/) | Storage, indexed recall, pool transactions and maintenance |
| [`python`](python/README.md) / [`typescript`](typescript/README.md) | SDKs and framework adapters |
| [`examples`](examples/) | Memory, shared pools, LangGraph and model-provider examples |

## Migrating from Engram

- Rebuild native extensions; replace `engram` imports with `memweft`, `Engram*` classes with `MemWeft*`, and Rust crate prefixes with `memweft-*`.
- Rename `ENGRAM_` environment variables to `MEMWEFT_` and the benchmark configuration to `bench/memweft_bench.env`.
- The default SQLite path is `data/memweft.db`; the default server database name is `memweft`. Explicitly supply the previous path or database name to reuse existing data. No databases are renamed automatically.

Apache License 2.0. Some historical notes under [docs](docs/) and [images](images/) describe the earlier low-level implementation. Vendored dependencies retain their own licenses.
