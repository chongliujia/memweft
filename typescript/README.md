# MemWeft for TypeScript

A Node.js SDK backed by the same Rust memory and evaluated-learning core as the Python SDK.

## Build from this repository

Requires Node.js 20+, Rust, and the platform's C toolchain.

```bash
npm ci
npm run build:native
npm run build
npm test
node examples/langgraph.mjs
```

## Use

```typescript
import { Memory } from "memweft";

const memory = await Memory.open({ path: "./memory.db" });
const user = memory.user("alice", { tenantId: "my-app", agentId: "assistant" });
await user.remember("Prefers short answers", { key: "reply_style" });
const chat = user.session("chat-001");
await chat.addMessage("user", "Explain ownership", { eventId: "question-1" });
console.log((await chat.context({ maxTokens: 1000 })).text);
```

`user.memories()`, `user.forget(key)`, `chat.messages()` and `chat.clear()` support inspection and deletion. `user.learning` provides `feedback`, `start`, `submit`, `get`, `jobs`, `cancel`, `active`, and `rollback`. Learning request/result fields use the shared Rust/Python snake_case contract; ordinary SDK options use camelCase.

## LangGraph.js

The optional `memweft/langgraph` entry point exports `MemWeftStore` (a real `BaseStore`) and `LangGraphMemory` (context/feedback helpers for nodes).

```typescript
import { MemWeftStore } from "memweft/langgraph";
const store = new MemWeftStore(memory, "alice");
const graph = builder.compile({ store, checkpointer: yourCheckpointer });
```

The store supports namespace/key CRUD, JSON comparison filters, namespace listing and batched operations. Reads in a batch observe the pre-write state; repeated writes to a key use the last value. Semantic/vector search is not supported. Use your application's LangGraph checkpointer for graph-state recovery.

## Packaging

`npm run build:native` creates `native/memweft.node` for the current host. After `npm run build`, `npm pack` creates a package that includes the addon and TypeScript declarations. This package is platform-specific, not a universal npm release. Choose the matching CI artifact for your operating system, CPU and libc. Publishing platform packages with automatic selection is future work.

The native entry point is for Node.js, not browser/Edge runtimes. An HTTP client/server transport has not been implemented.

See the repository README for budget semantics, SQLite-first support and the evaluated-learning contract.
