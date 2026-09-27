# Long conversations and document reads

`context(conversation_window=N)` now loads at most N message documents from the
requested session in SQLite. It reads a separate bounded sample of the oldest
excluded message keys for the existing selection report. Both reads share one
read transaction, so concurrent writes cannot mix the window and its diagnostic
sample. `include_messages=False` skips these reads, and a zero window loads no
message bodies. The overall diagnostic cap remains 64 across facts and messages.

Messages retain their `(created_at, key)` ordering, including timestamp ties and
nanosecond precision. The token budget may subsequently remove more messages.
The existing byte-based token estimate and all public Python/Node signatures
are unchanged.

Message append/retry and learning job, active-strategy and saved-version lookups
use exact namespace/key queries. Namespace-prefix reads filter canonical JSON
component prefixes in SQL; a namespace `['a']` includes `['a', 'child']` and does
not include `['ab']`. Source invalidation only reads learning documents, rather
than decoding conversations and external documents in the same scope.

## Opening existing databases

The first open adds a document chronological index, a partial learning index,
and a private-fact key index with `CREATE INDEX IF NOT EXISTS`. Existing records,
document revisions and table layouts are preserved. Building these indexes
takes a write lock, startup time and additional disk/WAL space proportional to
existing data; allow a maintenance window for large databases. Later opens
reuse the indexes. Back up persistent data before upgrading and upgrade all
writers to obtain the private-source invalidation behavior described in
[memory pools](memory_pools.md).

The chronological index uses the canonical UTC timestamp encoding written by
MemWeft. Arbitrary manual rewrites of internal JSON/timestamps are unsupported.

## Remaining limits

- `messages()` and `memories()` intentionally return full lists. They are not
  paginated APIs.
- LangGraph Store search still filters and paginates the matching external
  document collection in memory. This change does not turn it into an indexed
  general-purpose document search service.
- Learning invalidation still inspects learning records for declared sources.
  A very large learning history may need a dedicated dependency index later.
- Memory use is bounded by the selected document count, not a byte limit on
  individual message bodies. Extremely large selected messages still cost memory.
- This change does not solve slow fact-ranking queries, long-reader WAL growth,
  asynchronous admission control or distributed storage.

The [document benchmark](../evals/benchmark_documents.py) measures the real Python
SDK on isolated databases and checks complete context hashes across builds.
See the [measured comparison](../evals/reports/2026-09-27-document-hardening.md).
