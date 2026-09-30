<div align="center">

# MemWeft

**Persistent memory. Shared knowledge. Evaluated learning.**

A Rust memory engine for agents, with Python and TypeScript SDKs.

[![CI](https://github.com/chongliujia/memweft/actions/workflows/verify.yml/badge.svg)](https://github.com/chongliujia/memweft/actions/workflows/verify.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-2563eb.svg)](LICENSE)
[![Rust 1.89+](https://img.shields.io/badge/Rust-1.89%2B-000000?logo=rust)](Cargo.toml)
[![Python 3.10–3.12](https://img.shields.io/badge/Python-3.10–3.12-3776ab?logo=python&logoColor=white)](python/README.md)
[![Node 20+](https://img.shields.io/badge/Node-20%2B-339933?logo=nodedotjs&logoColor=white)](typescript/README.md)

[Quickstart](#quickstart) · [Architecture](#architecture) · [Performance](#performance) · [Agent evaluations](#agent-evaluations) · [Guides](#guides) · [Contributing](#contributing)

</div>

MemWeft gives agents persistent facts, resumable conversations and relevant model context. Keep one agent's memory private, share selected facts across agents, and adopt learned strategies only after evaluation.

**Local-first:** memory operations need no model service or API key. The high-level APIs use SQLite; optional PostgreSQL/MySQL support is currently limited to the older low-level API.

**Development status (2026-09-30):** core SDK features are implemented and undergoing pre-release hardening. Million-fact retrieval and multiwriter recovery have local benchmark evidence; registry distribution and long-running business validation remain open. Package versions are still `0.1.0`; the current SQLite recall index is **schema v3**. Before opening an existing database with this build, follow the [backup, upgrade and recovery guide](docs/upgrade_v3.md). Older v1/v2 SDKs cannot open a migrated v3 database.

## What you can build

| Need | Available today |
|---|---|
| An agent that remembers | Persistent facts, updates, forgetting and idempotent session messages |
| A team of agents | Private/shared/mixed pools, read/write permissions, precedence and revision checks |
| Relevant context at scale | Indexed lexical retrieval, bounded candidate loading and selection explanations |
| Measurable improvement | Feedback, evaluation gates, strategy versioning, source invalidation and rollback |
| Framework integration | Python LangGraph and LangGraph.js context helpers and `BaseStore` adapters |
| Controlled model inputs | Optional Python reference projection and audited sandbox execution examples |
| Observable local storage | Optional background checkpointing, coordinated maintenance, takeover and diagnostics |

Enterprise deployment is the target. Distributed sharding, hot-data preloading, RDMA, automatic conversation extraction and a full query/loading/compression pipeline remain future work.

## Quickstart

Build from source with **Rust 1.89+**, a C toolchain and Python 3.10–3.12:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install maturin
cd python
maturin develop
cd ..
```

On Windows, activate with `.venv\Scripts\activate`. Registry publishing and automatic platform package selection remain future work.

For an installable preview, the [CI workflow](.github/workflows/verify.yml)
is configured to build Python wheels for 3.10–3.12 on Linux, macOS and Windows runners,
plus one host-specific Node tarball per runner. Choose a wheel whose Python tag
and platform match your interpreter, or a Node tarball built for your operating
system, CPU and Linux libc. The artifact's `package-target.json` records the
build host and hashes. Install the downloaded file directly:

```bash
python -m pip install /path/to/downloaded-wheel.whl
npm install /path/to/memweft-0.1.0.tgz
```

These artifacts are verified in a fresh environment outside the checkout using
[`scripts/verify_packages.py`](scripts/verify_packages.py). They are not yet
published to package registries; check the matching CI run before distributing
an artifact. The [Python](python/README.md#install-a-built-wheel) and
[Node](typescript/README.md#packaging) guides describe each package's limits.

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

Run it twice: the preference updates by key, and the event ID prevents duplicate messages. `memories()` inspects facts, `forget(key)` removes them, and `chat.clear()` clears session messages. Python also provides `AsyncMemory`.

<details>
<summary><b>TypeScript / Node.js</b></summary>

From the repository root:

```bash
cd typescript
npm ci
npm run build:native
npm run build
npm test
cd ..
```

```typescript
import { Memory } from "memweft";

const memory = await Memory.open({ path: "./memory.db" });
try {
  const alice = memory.user("alice", { tenantId: "my-app", agentId: "assistant" });
  await alice.remember("Prefers concise answers", { key: "reply_style" });
  const chat = alice.session("chat-001");
  await chat.addMessage("user", "Explain Rust ownership", { eventId: "question-1" });
  console.log((await chat.context({ query: "reply style", maxTokens: 1000 })).text);
} finally {
  memory.close();
}
```

The native addon targets Node.js 20+. Browser/Edge access and an HTTP service remain future work. See the [TypeScript guide](typescript/README.md).

</details>

<details>
<summary><b>Rust / CLI</b></summary>

```bash
cargo run --locked -p memweft -- remember alice reply_style "Prefers concise answers"
cargo run --locked -p memweft -- context alice chat-001
cargo run --locked -p memweft -- memories alice
```

Put `--db PATH` before the subcommand to select a database. `memweft request FILE.json` executes the shared JSON request interface. Rust applications use the workspace `memweft` crate; see the [learning example](crates/memweft/examples/learning.rs).

</details>

## Architecture

```mermaid
flowchart TB
    A[Agent A] --> SDK[Python / TypeScript / Rust API]
    B[Agent B] --> SDK
    subgraph Core[MemWeft Rust core]
        Scope[Tenant / user / agent scope]
        Scope --> Private[Private facts]
        Scope --> Shared[Shared pools]
        Private --> DB[(SQLite WAL + inverted index)]
        Shared --> DB
        DB --> Context[Ranked context + selection report]
        Learning[Evaluation gates + strategy versions] --> Context
        Learning --> DB
        Maintenance[Optional coordinated maintenance] <--> DB
    end
    SDK --> Scope
    Context --> App[Application model call]
    App --> Feedback[Measured task feedback]
    Feedback --> Learning
```

The application supplies model calls, identity and tool authorization. MemWeft handles storage, scoped retrieval, context construction and learning decisions. Learning adapts prompt strategies; it does not train model weights.

## Private and shared memory

Different `agent_id` values have independent facts by default. Add named pools to share facts within a tenant/user scope:

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
    planner.remember("Plan before executing", key="work_style")
    planner.remember(8002, key="service_port", pool_id="project")
    print(executor.session("deploy").context(query="service_port").text)
```

The executor sees the shared port, while the planner's work style stays private. Conversations and learning records remain agent-specific. Configure read-only access, explicit write targets, conflict policies and `expected_revision` checks in the [pool guide](docs/memory_pools.md).

## Performance

The latest measurements are from **2026-09-27–28 on macOS arm64**, using Rust 1.89.0 release builds. Earlier Linux measurements are retained separately below. Results describe tested workloads, not production SLOs; gains are calculated only within each paired experiment.

### One million facts: exact bitmap retrieval

Schema v3 adds exact 64-ID bitmaps to the existing postings index. It preserves lexical scores, pool precedence, validity checks and complete context output. The strongest improvement is for interleaved, dense two-term lists with few common records.

| Metric | v2 ordered-ID blocks | v3 exact bitmaps |
|---|---:|---:|
| Serial two-term query p50 | 170.620 ms | **7.471 ms** |
| Concurrent queries completed / offered | 16,910 / 30,000 | **29,995 / 30,000** |
| Concurrent query p95, all query types | 189.06 ms | **11.61 ms** |
| Concurrent write p99 | 0.97 ms | 1.37 ms |
| Concurrent writes completed / offered | 59,975 / 60,000 | 59,973 / 60,000 |
| Observed WAL peak | 926.80 MiB | 669.16 MiB |
| WAL after idle recovery with clients open | 0.00 MiB | 0.00 MiB |

Serial queries use one warmup and 20 measured complete Python SDK `context()` calls. The separate concurrent comparison uses four readers and four writers for five minutes, targeting 100 primary queries/s and 200 writes/s. An extra reader pins a snapshot for 90 seconds, followed by 45 seconds of idle observation with all SDK clients still open. Both builds use the same 1,000 ms maintenance interval and 16 MiB soft reclamation threshold. Missed arrival slots are counted, not queued; there is one sequential run per build.

**Costs and limits:** the million-fact database grew from 823.30 to 889.90 MiB (**8.1%**); first open/upgrade/close took 3.80 seconds. Serial private-insert p50 rose from 0.113 to 0.345 ms. These are final file sizes, not peak migration space. Sparse IDs may see little benefit, and the 100k large-overlap fallback stayed around 181 ms. WAL reclamation remains opt-in and its threshold is not a space cap.

All **30 complete context comparisons** matched. The correctness suite includes **1,309 differential ranking checks**, plus bitmap, signed-ID, migration rollback and concurrent-write cases. [Query, write and space report](evals/reports/2026-09-28-bitmap-recall.md) · [JSON](evals/reports/2026-09-28-bitmap-recall.json) · [WAL recovery configuration comparison](evals/reports/2026-09-28-wal-recovery.md)

### Long conversations

With 100,000 messages in the target session and 100,000 in other sessions, reading a ten-message context fell from **762.607 to 0.374 ms p50**. Complete context hashes matched at 1k, 10k and 100k target messages. This was a separate, warm, single-client document-index experiment; its database grew from 68.43 to 84.79 MiB. It does not measure the additional v3 bitmap cost. [Document hardening report](evals/reports/2026-09-27-document-hardening.md) · [Window behavior and limits](docs/long_conversations.md)

Exact lexical ranking uses word/number matches and Chinese ideograph pairs, not BM25 or vector search. Three-or-more-term queries, large intersections and strict conflict validation can still require broad scans. Million-fact tests do not establish ten-million or hundred-million scale support. [Index behavior and migration](docs/indexed_retrieval.md)

<details>
<summary><b>Earlier Linux measurements — 2026-09-22, before the bitmap optimization</b></summary>

### Coordinated maintenance on selected fast queries

![Before/after SQLite maintenance: write p99 33.58 to 4.32 ms, query p95 3.00 to 2.92 ms, WAL peak 107.45 to 117.33 MiB.](docs/assets/maintenance-performance.svg)

| Metric | Before coordination | Coordinated maintenance |
|---|---:|---:|
| Actual queries/second | 953.45 | 964.71 |
| Query p95 | 3.00 ms | 2.92 ms |
| Write p99 | 33.58 ms | **4.32 ms** |
| Completed writes / offered | 14,109 / 15,000 | 14,942 / 15,000 |
| Slowest write | 523.71 ms | 218.33 ms |
| Observed WAL peak | 107.45 MiB | 117.33 MiB |

**Conditions:** independent million-fact database copies, 8 reader processes + 1 writer, 5 minutes/run, target 1,000 queries/s and 50 writes/s. An extra reader held a snapshot for 90 seconds. Latency covers completed requests; missed scheduling slots are reported separately.

Write p99 fell **87.1%**, but both new runs recorded **zero successful WAL truncations** during measurement. The 16 MiB threshold is soft, and proactive reclamation remains opt-in. Runs were sequential on a shared local machine; read paths were selected fast paths. [Full report and both new runs](evals/reports/2026-09-22-wal-coordination.md) · [JSON](evals/reports/2026-09-22-wal-coordination.json)

### Earlier query-shape baseline

![Million-fact retrieval p95 on a logarithmic axis: fast paths around 1–2 ms, difficult two-term query around 338 ms.](docs/assets/retrieval-performance.svg)

The SDK query includes retrieval, context construction and serialization. Exact lexical ranking uses word/number matches and Chinese ideograph pairs; it is not BM25 or vector search. Difficult queries still inspect many index entries. The comparison preserved complete context output and includes **1,273 differential ranking checks**. [Query report](evals/reports/2026-09-22-wal-and-agent.md) · [Index behavior and migration](docs/indexed_retrieval.md)

These historical figures are generated from their versioned JSON by [`evals/plot_readme_metrics.py`](evals/plot_readme_metrics.py). They do not show the subsequent macOS bitmap results.

</details>

## Agent evaluations

We test with **real LangGraph + the Python SDK + a locally deployed `qwen3-8b`**. Business inputs and tool effects are synthetic; failures and interrupted runs are retained.

| Evaluation | Measured result | What it establishes |
|---|---|---|
| Memory lifecycle | 44/44 checks; 284 model calls in the complete lifecycle/learning replay | Updates, pool isolation, deletion, reopening and strategy invalidation; [report](evals/reports/2026-09-22-wal-and-agent.md) |
| Fresh access boundaries | No memory **49/60** → memory **54/60** → learned strategy **60/60** | 30 new same-domain cases, each repeated twice; [report](evals/reports/2026-09-22-access-holdout-tools.md) |
| Audited sandbox tools | 27 injection-induced unsafe proposals blocked; no unauthorized grants in 240 executions | Deterministic execution checks work for these fixtures; model injection failures remain |
| Reference projection | 972 calls; preference checks **4/12 → 12/12** with restricted inputs | Reduced exposure, but legitimate completion regressed in the learned mode and current-question attacks remained; [report](evals/reports/2026-09-22-reference-boundary.md) |
| Confirmed commands | 1,272 calls; **0 unauthorized grants** across 1,200 audited sandbox decisions | Fresh learned-mode accuracy: rules + quoted input **42/56**, input omitted **28/56**. Command guards block unsafe effects; input omission is not an overall model-quality improvement; [report](evals/reports/2026-09-22-confirmed-command.md) |

A passing schema or a learned strategy is not authorization. Optional [reference projection](docs/reference_boundaries.md) filters model inputs; application-side checks must still validate actions against current state. Evaluator scores are trusted application inputs, so adoption gates cannot independently prove their truth.

<details>
<summary><b>Run a local Agent evaluation</b></summary>

After building the Python extension, run on the machine hosting the model:

```bash
python -m pip install -r python/requirements-test.txt
PYTHONPATH=python/src python evals/run_agent_lifecycle.py \
  --output data/evals/agent-lifecycle-new-run \
  --base-url http://127.0.0.1:8002/v1 --model qwen3-8b --repeats 2
```

The API key defaults to `EMPTY`. The runner requires supported Qwen request options and strict JSON Schema output. Use a new output directory for every run. Raw requests, responses, contexts and scores stay under Git-ignored `data/evals/`; summaries are versioned in `evals/reports/`.

[All evaluation commands](evals/README.md) · [Agent example](examples/local_memory_agent.py) · [Sandbox executor](examples/sandbox_access_agent.py)

</details>

## Framework integration

`LangGraphMemory` provides long-term context without duplicating framework-managed history. `MemWeftStore` implements `BaseStore` for scoped JSON documents. Store documents are separate from context-visible facts; store search supports filters, not vectors or TTL.

```bash
python -m pip install -r python/requirements-test.txt
python examples/langgraph_memory.py
node typescript/examples/langgraph.mjs
```

Use an existing LangGraph checkpointer for graph execution state. The deprecated `MemWeftCheckpointer` wraps working state and is not a checkpoint saver. [Python API](python/README.md) · [TypeScript API](typescript/README.md)

## Current boundaries

- **Context budgets** estimate `ceil(UTF-8 bytes / 4)` for returned text, not a model tokenizer limit or the entire prompt.
- **Forgetting** removes facts and dependent scoped records; messages or content already copied into external prompts require separate deletion.
- **Durability** uses SQLite WAL with `synchronous=NORMAL`. Process-crash tests do not prove power-loss durability. The bundled engine is SQLite 3.51.3; [source and fix provenance](vendor/libsqlite3-sys/MEMWEFT-PATCH.md) are retained.
- **Isolation needs trusted identity:** callers must bind tenant/user/agent scopes correctly. Memory pools are not a production IAM service.
- **Platform validation:** recorded local measurements cover Linux and macOS arm64 in separate experiments. The CI matrix defines Linux, macOS and Windows builds; the latest hardening was verified locally on macOS, not by a new three-platform CI run.
- **Database compatibility:** opening a legacy/v1/v2 database with this build upgrades its recall index to v3. Upgrade all processes together and retain a verified pre-upgrade backup; switching only the executable back is unsupported. [Upgrade and recovery](docs/upgrade_v3.md)

## Roadmap

| Stage | Focus |
|---|---|
| Implemented | Scoped pools, exact bitmap recall, bounded conversation windows, private-source invalidation, evaluation gates and coordinated local maintenance |
| Locally verified | Million-fact queries, five-minute multiwriter pressure, pinned snapshots, idle WAL recovery and fresh wheel/npm installations on macOS arm64; [retrieval evidence](evals/reports/2026-09-28-bitmap-recall.md) |
| Next: distributable preview | Confirm the new cross-platform package CI passes, select supported runner architectures, publish installable artifacts, and rehearse application-specific upgrade/recovery |
| Next: business validation | Real long conversations, memory/learning task quality, concurrent tools, hours-long storage soak tests, write-heavy and sparse-term workloads |
| Planned | Bounded hot-data preloading, staged background jobs, sharding; RDMA only after measuring a relevant bottleneck |

## Guides

| Guide | Contents |
|---|---|
| [Memory pools](docs/memory_pools.md) | Private/shared configuration, precedence and concurrent revisions |
| [Long conversations](docs/long_conversations.md) | Bounded message windows, document indexes and upgrade behavior |
| [Indexed retrieval](docs/indexed_retrieval.md) | Ranking, bounded loading, fallbacks and migration |
| [Upgrade and recovery](docs/upgrade_v3.md) | Schema v3 compatibility, verified backups, migration checks and restoring the old build |
| [Learning design](docs/rust_learning_and_integrations.md) | Evaluation gates, strategy lifecycle and integration |
| [Reference boundaries](docs/reference_boundaries.md) | Python input projection, content pins and information loss |
| [Confirmed commands](docs/confirmed_commands.md) | Application confirmation, input binding, cancellation and execution checks |
| [Async maintenance](docs/async_pipeline.md) | Checkpoint configuration, diagnostics and pipeline plans |
| [Preloading and scale](docs/preloading_and_scale.md) | Memory budgets, sharding and RDMA considerations |
| [Evaluation guide](evals/README.md) | Reproduction commands, scenarios and report history |

<details>
<summary><b>Repository map</b></summary>

```text
crates/
  memweft/           User/session API, context builder and CLI
  memweft-store/     Storage, pools, indexed recall and maintenance
  memweft-learning/  Evaluation gates and strategy versions
python/              Python SDK and LangGraph adapters
typescript/          Node SDK and LangGraph.js adapters
examples/            Runnable memory and Agent integrations
evals/               Fixtures, runners, benchmarks and reports
docs/                Design notes, operational guides and figures
```

</details>

## Contributing

Bug reports, reproducible workloads and focused pull requests are welcome. For retrieval or learning changes, include correctness checks alongside latency or score improvements; preserve failures and the dataset/build versions used.

After building both native extensions, run from the repository root:

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

Rust, Python and TypeScript share a [contract fixture](tests/contract.json). Optional database tests skip without their DSNs. The [CI workflow](.github/workflows/verify.yml) is configured for nine Python wheels and three Node packages across its runners; each job installs its package outside the checkout, and offline Agent evaluation tests run on Linux/Python 3.12. Model-call evaluations remain explicit local runs. These new matrix results still need a remote CI run.

[Report an issue](https://github.com/chongliujia/memweft/issues) · [Browse examples](examples/) · [Inspect evaluation reports](evals/reports/)

<details>
<summary><b>Migrating from Engram</b></summary>

Rebuild native extensions; replace `engram` imports with `memweft`, `Engram*` names with `MemWeft*`, and Rust crate prefixes with `memweft-*`. Rename `ENGRAM_` environment variables to `MEMWEFT_` and benchmark configuration to `bench/memweft_bench.env`.

The default SQLite path is `data/memweft.db`; the default server database name is `memweft`. Explicitly supply the previous path/name to reuse data. Nothing is renamed automatically. Some older files under `docs/` and `images/` describe the earlier low-level implementation.

</details>

## License

[Apache License 2.0](LICENSE). Vendored dependencies retain their own licenses.
