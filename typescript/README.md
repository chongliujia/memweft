# MemWeft for TypeScript

A Node.js SDK backed by the same Rust memory and evaluated-learning core as the Python SDK.

## Build from this repository

Requires Node.js 20+, Rust 1.89+, and the platform's C toolchain.

```bash
npm ci
npm run build:native
npm run build
npm test
node examples/langgraph.mjs
```

## Use

File stores optionally support
`Memory.open({path: "./memory.db", sqliteOptions: {backgroundCheckpointMs: 1000}})`.
Intervals must be 100–60,000 ms; in-memory databases reject the option. The default
remains SQLite automatic checkpointing. Writes still commit before resolving;
only maintenance notifications are queued. See [pipeline and checkpoint
semantics](../docs/async_pipeline.md) for WAL growth, failure fallback and shutdown.

Add `walReclaimThresholdBytes: 16 * 1024 * 1024` to those options to attempt
WAL truncation above a soft threshold (64 KiB–1 TiB). Readers can delay reclamation
and let the file exceed it. `await memory.storageStatus()` reports the actual
SQLite version and per-instance maintenance progress, busy attempts, sampled
bytes and errors.
Enabled instances elect one maintainer using a persistent
`<database>.memweft-maintenance` file lock, with busy retries backing off up to
30 seconds and a 30-second cooldown after success. Use consistent options across
instances and inspect `checkpoint.progress.coordinator_role` on readers too.
Do not delete the sidecar while any instance is open.

```typescript
import { Memory } from "memweft";

const memory = await Memory.open({ path: "./memory.db" });
const user = memory.user("alice", { tenantId: "my-app", agentId: "assistant" });
await user.remember("Prefers short answers", { key: "reply_style" });
const chat = user.session("chat-001");
await chat.addMessage("user", "Explain ownership", { eventId: "question-1" });
console.log((await chat.context({ query: "Explain ownership", maxTokens: 1000 })).text);
```

`user.memories()`, `user.forget(key)`, `chat.messages()` and `chat.clear()` support inspection and deletion. `user.learning` provides `feedback`, `start`, `submit`, `get`, `jobs`, `cancel`, `active`, and `rollback`. Learning request/result fields use the shared Rust/Python snake_case contract; ordinary SDK options use camelCase.

Call `memory.close()` when finished, normally in `finally`. Releasing the last
owner of a SQLite store waits for its checkpoint worker and pooled connections
to close. Other open stores or still-live shared owners retain their resources.

`await chat.context({ query: "current deployment port", maxFacts: 30 })` ranks
facts by lexical relevance before applying context limits. Inspect
`context.explain().recall` for matched terms and scores. `LangGraphMemory.context`
accepts the same option. Omitting `query` preserves key order. This does not
perform vector search. SQLite uses indexed candidate loading; see the repository
guide for ranking rules and fallback paths.

When the application knows a task's required facts, pass
`requiredFactKeys: ["tls.certificate", "clock.observation", "host.clock"]` to
`context`. Exact key retrieval honors the existing scope, pool and budget rules;
required facts take priority in declaration order. Inspect
`context.explain().requirements` for included, missing and excluded keys. A
complete result only covers the declared keys. See [required facts and workflow
validation](../docs/required_facts.md) for Python/Node examples and boundaries.

## Agent memory pools

Pass `memoryConfig: { readPools: [{ poolId: "project", access: "read_write" }], defaultWritePool: "project" }` in `memory.user` options to share facts across Agent IDs. `remember` and `forget` accept `poolId` and `expectedRevision`; `memories({ poolId })` inspects one pool. See the [pool guide](../docs/memory_pools.md) for mixed/private pools, conflict policies and learning dependencies.

## LangGraph.js

The optional `memweft/langgraph` entry point exports `MemWeftStore` (a real `BaseStore`) and `LangGraphMemory` (context/feedback helpers for nodes).

```typescript
import { MemWeftStore } from "memweft/langgraph";
const store = new MemWeftStore(memory, "alice");
const graph = builder.compile({ store, checkpointer: yourCheckpointer });
```

The store supports namespace/key CRUD, JSON comparison filters, namespace listing and batched operations. Reads in a batch observe the pre-write state; repeated writes to a key use the last value. Semantic/vector search is not supported. Use your application's LangGraph checkpointer for graph-state recovery.

## Packaging

`npm run build:native` creates `native/memweft.node` for the current host. After
`npm run build`, `npm pack` creates a package with the addon and TypeScript
declarations. Download the `memweft-native-...` CI artifact built for your
operating system, CPU and Linux libc. Its `package-target.json` identifies the
build target and tarball hash. Install the tarball in your application:

```bash
npm install /path/to/memweft-0.2.0-alpha.1.tgz
```

This package is specific to the runner that built it. CI is configured to verify that the
tarball installs and loads the native addon from a separate project, then tests
required-fact diagnostics, scope isolation, updates, durable forgetting and
persistence. The [release guide](../docs/preview_release.md) explains the
source commit and hashes recorded with each preview artifact.
No universal npm release or automatic platform
selection exists yet. The tarballs are not published to npm.

The native entry point is for Node.js, not browser/Edge runtimes. An HTTP client/server transport has not been implemented.

See the repository README for budget semantics, SQLite-first support and the evaluated-learning contract.

SQLite context queries now use a transactional inverted index with the existing
lexical scoring. Diagnostic arrays are capped at 64; inspect the truncation flags
in `context.explain()` rather than treating diagnostic lists as exhaustive.
Existing databases require an index backfill and coordinated writer upgrade;
see [indexed retrieval and migration](../docs/indexed_retrieval.md).
