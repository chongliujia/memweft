# 🧠 MemWeft

**Structured memory and evaluated strategy improvement for AI agents.**

MemWeft is a Rust library with Python and TypeScript bindings. Store user preferences, resume conversations, build model context, and evaluate candidate strategies before adopting them. Python LangGraph and LangGraph.js can use the same core through node helpers and `BaseStore` adapters.

The new user/session and learning APIs currently target **SQLite**. The existing low-level API also supports optional Postgres and MySQL backends.

| What you need | MemWeft API |
| --- | --- |
| Persistent user preferences | `user.remember()`, `user.memories()`, `user.forget()` |
| Conversation history | `user.session()`, message IDs for safe retries |
| Context for a model call | `session.context()`, estimated budget and selection report |
| Python LangGraph or LangGraph.js | Context helpers and native `BaseStore` adapters |
| Evaluated strategy changes | Feedback, candidate evaluation, versioning and rollback |

The storage, context construction and learning decisions run in Rust. Python and TypeScript provide native bindings and framework adapters. The basic examples need no model service or API key.

[Build and install](#installation-and-development) · [Quickstart](#start-with-a-user-and-a-conversation) · [LangGraph](#langgraph-in-python-and-javascript) · [Learning / RSI](#evaluated-learning-and-rsi-foundations) · [Migration](#migrating-from-engram)

## Installation and development

Build from this repository using the commands below, starting at the repository root. A native build requires Rust and a C toolchain. Registry publishing and automatic platform package selection remain future work.

Python (3.10–3.12 for the current PyO3 binding):

```bash
python -m venv .venv
source .venv/bin/activate
pip install maturin
cd python
maturin develop
cd ..
python examples/quickstart.py
```

On Windows, activate with `.venv\Scripts\activate`. If using Conda, use one active environment when invoking maturin.

TypeScript (Node.js 20+; local builds tested with Node 20):

```bash
cd typescript
npm ci
npm run build:native
npm run build
npm test
```

The native addon targets Node.js. Browser/Edge access and the optional HTTP service remain future work. Precompiled packages must match the operating system, CPU and Python version; see [the TypeScript package guide](typescript/README.md).

Rust CLI:

```bash
cargo run -p memweft -- remember alice reply_style "Prefers concise answers"
cargo run -p memweft -- context alice chat-001
cargo run -p memweft -- memories alice
```

Use `--db PATH` before the command to select a database. `memweft request FILE.json` executes the shared JSON request interface, including learning operations. No model calls happen automatically.

## Start with a user and a conversation

Python:

```python
from memweft import Memory

memory = Memory("./memory.db")
alice = memory.user("alice")
alice.remember("Prefers concise answers", key="reply_style")

chat = alice.session("chat-001")
chat.add_message("user", "Explain Rust ownership", event_id="question-1")
context = chat.context(max_tokens=1000)
print(context.text)
```

Run it twice: the preference is updated by key, and the same event ID does not duplicate the message. The database survives process restarts. `alice.memories()`, `alice.forget(key)` and `chat.clear()` provide explicit inspection and deletion.

TypeScript (Node.js):

```typescript
import { Memory } from "memweft";

const memory = await Memory.open({ path: "./memory.db" });
const alice = memory.user("alice");
await alice.remember("Prefers concise answers", { key: "reply_style" });

const chat = alice.session("chat-001");
await chat.addMessage("user", "Explain Rust ownership", { eventId: "question-1" });
const context = await chat.context({ maxTokens: 1000 });
console.log(context.text);
```

Rust:

```rust
use memweft::{Memory, ContextOptions};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let memory = Memory::open("./memory.db")?;
    let alice = memory.user("alice")?;
    alice.remember("reply_style", serde_json::json!("Prefers concise answers"))?;
    let context = alice.session("chat-001")?.context(ContextOptions::default())?;
    println!("{}", context.text);
    Ok(())
}
```

Use the workspace crate at `crates/memweft` and `serde_json` as dependencies for the Rust example.

Python also provides `AsyncMemory`:

```python
import asyncio
from memweft import AsyncMemory

async def main():
    async with AsyncMemory("./memory.db") as memory:
        alice = memory.user("alice", tenant_id="my-app", agent_id="assistant")
        await alice.remember("Prefers concise answers", key="reply_style")
        context = await alice.session("chat-001").context(max_tokens=1000)
        print(context.text)
        print(context.explain())

asyncio.run(main())
```

TypeScript I/O operations return promises. Tenant and agent IDs can be bound when selecting a user; they are part of every storage query. Python supports `with Memory(...)` and `async with AsyncMemory(...)`; call `memory.close()` when managing the lifecycle explicitly in Python or TypeScript.

## LangGraph in Python and JavaScript

There are two integration surfaces:

- `LangGraphMemory` supplies long-term context and accepts task feedback from nodes. It excludes MemWeft's message window so LangGraph-managed history is not duplicated.
- `MemWeftStore` implements the framework's `BaseStore` protocol for JSON documents, including get/put/delete, filtering, pagination, namespace listing and batches.

Python:

```python
from memweft.adapters.store import MemWeftStore

store = MemWeftStore(memory, "alice")
graph = builder.compile(store=store, checkpointer=your_checkpointer)
```

TypeScript:

```typescript
import { MemWeftStore } from "memweft/langgraph";

const store = new MemWeftStore(memory, "alice");
const graph = builder.compile({ store, checkpointer: yourCheckpointer });
```

A store instance is bound to one user; build/request the correct instance at the application's identity boundary. Generic framework documents are separate from user facts: use `remember()` for context-visible facts and Store operations for arbitrary application data. Store search currently supports JSON filters, **not vector search or TTL**; unsupported options fail explicitly.

Use an existing LangGraph checkpointer for graph execution state. The old `MemWeftCheckpointer` name is deprecated: it only wraps working state and is not a LangGraph checkpoint saver.

Runnable examples, without model credentials:

```bash
pip install 'langgraph>=1.2,<2'
python examples/langgraph_memory.py
node typescript/examples/langgraph.mjs
```

## Evaluated learning and RSI foundations

The Rust learning engine persists feedback, candidate jobs, pinned evaluation cases, accepted strategy versions and rollback history. A candidate is adopted only after the supplied evaluation meets the policy. Competing candidates use transactional revision checks rather than overwriting the active version.

The following continues the Python quickstart. It uses synthetic scores to demonstrate the API; replace them with measurements from your evaluator before using this workflow to adopt real strategies.

```python
learning = alice.learning
learning.feedback(
    id="feedback-1", task_type="answer", session_id="chat-001",
    run_id="run-1", success=False, details="The answer was too long",
)
job = learning.start(
    id="candidate-1",
    proposal={
        "task_type": "answer",
        "content": "Give a direct, one-sentence answer to simple questions.",
        "proposer_version": "my-proposer-v1",
        "source_keys": ["reply_style"],
    },
    dataset_version="heldout-v1",
    evaluator_version="my-evaluator-v1",
    case_ids=["case-a", "case-b", "case-c"],
)
result = learning.submit(job["id"], {
    "dataset_version": "heldout-v1",
    "evaluator_version": "my-evaluator-v1",
    "cases": [
        {
            "case_id": case_id,
            "baseline_score": 0.6,
            "candidate_score": 0.8,
            "candidate_cost": 0.01,
            "candidate_latency_ms": 100,
        }
        for case_id in ["case-a", "case-b", "case-c"]
    ],
})
print(result["status"])
print(chat.context(task_type="answer").text)
```

Each evaluation supplies `dataset_version`, `evaluator_version` and `cases`. Each case contains `case_id`, `baseline_score`, `candidate_score` (0–1), `candidate_cost` (in your consistent cost unit), and `candidate_latency_ms`.

The default policy requires at least three cases, positive mean score gain of at least 0.05, no per-case regression, total candidate cost at most 1.0, and candidate latency at most 30,000 ms per case. Applications should set a policy appropriate to their task. Evaluators are trusted application components: the library validates result structure and policy compliance but cannot verify the truth of externally reported scores.

Use `learning.get(id)`, `learning.jobs()`, `learning.cancel(id)` and `learning.rollback(...)` to inspect and control jobs. A baseline conflict leaves the job available for inspection/cancellation; create a new job against the current baseline before reevaluation. Accepted task strategies enter context when `task_type` / `taskType` matches.

Rust applications can implement `Proposer` and `Evaluator` and call `Learning::improve` for one bounded round. Reflection strategies are versioned separately (`target="reflection"`) and passed to the next proposer round. This is an explicit extension point for recursive improvement; automatic model-backed proposal generation and multi-round scheduling are not bundled.

```bash
cargo run -p memweft --example learning
```

This demo uses synthetic evaluation scores to demonstrate the state machine. It is not evidence that a model has improved. See [the learning and integration design](docs/rust_learning_and_integrations.md) for remaining work.

## Behavior and limits

- `context.text` has an explicit estimated budget: `ceil(UTF-8 bytes / 4)`. It is not a model tokenizer guarantee. `context.explain()` in Python/TypeScript reports selection, omissions and estimated usage. The budget applies to the returned text, not the entire structured response or your complete model prompt.
- Context includes active user facts, recent session messages and the accepted task strategy. It does not implement relevance-based vector retrieval.
- `forget(key)` deletes matching facts, saved context snapshots for that user/agent, and learning records/strategies that declare the key in their sources. It also prevents previously started evaluations from being adopted. Unrelated raw messages, application documents and feedback remain until explicitly deleted by their owner.
- High-level session messages use a separate document namespace. Existing low-level `append_event` history is not automatically imported into that namespace.
- Cancelling an awaiting Python/JavaScript call does not guarantee that an already-running database write was cancelled; use stable event/job IDs for retries.
- New document and learning operations support SQLite first. Postgres/MySQL remain available through the existing low-level API and do not yet implement the new document protocol.

## Verification

After building both native extensions, run these commands from the repository root with the Python environment active:

```bash
cargo test --locked
python -m pip install -r python/requirements-test.txt
python -m unittest discover -s python/tests -v
cd typescript
npm run build
npm test
```

Rust, Python and TypeScript replay [the same contract fixture](tests/contract.json). Tests cover actual SQLite persistence, identity isolation, idempotent writes, context budgets, learning acceptance/rejection/rollback, and real graphs in both LangGraph implementations. Optional Postgres/MySQL tests skip when their DSNs are not configured.

The [CI workflow](.github/workflows/verify.yml) defines Linux, macOS and Windows builds and uploads Python wheels, Node packages and CLI artifacts. Local validation has covered Linux; the other platforms still require successful CI runs.

## Project layout

| Path | Purpose |
| --- | --- |
| [`crates/memweft`](crates/memweft/) | User/session API, context builder, shared request interface and CLI |
| [`crates/memweft-learning`](crates/memweft-learning/) | Candidate evaluation, strategy versions and rollback |
| [`crates/memweft-store`](crates/memweft-store/) | Storage backends and SQLite document transactions |
| [`python`](python/README.md) | Python SDK and LangGraph adapters, backed by PyO3 |
| [`typescript`](typescript/README.md) | Node.js SDK and LangGraph.js adapters, backed by N-API |
| [`examples`](examples/) | Basic memory, LangGraph and model-provider examples |


## Migrating from Engram

- Rebuild native extensions; replace `engram` imports with `memweft`, `Engram*` classes with `MemWeft*`, and Rust crate prefixes with `memweft-*`.
- Rename `ENGRAM_` environment variables to `MEMWEFT_` and the benchmark configuration to `bench/memweft_bench.env`.
- The default SQLite path is `data/memweft.db`; the default server database name is `memweft`. Explicitly supply your former path or database name to reuse existing data. No existing databases are renamed automatically.

Apache License 2.0. Historical architecture notes and screenshots remain under [docs](docs/) and [images](images/); some describe the earlier low-level implementation.
