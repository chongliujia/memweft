# SQLite indexed retrieval

`Session.context()` now uses a persistent, scoped inverted index for `private_first`
and `read_order` configurations. Rust, Python (sync/async), Node and the CLI use the
same core implementation. No option is required to enable it; public call arguments
and the context's fact/message/strategy shapes remain unchanged.

## Selection and consistency

The index stores the existing tokenizer's distinct Unicode words/numbers and
adjacent Chinese ideograph pairs. A query term contributes 2 points for a key
match and 1 for a JSON-value match. Repetitions do not inflate the score; ties
retain fact-key/fact-ID order. A key-ordered index fills remaining slots when
there are fewer positive matches than requested, including for empty queries.
This is `lexical_overlap_v1`, not BM25 or vector retrieval.

Index schema version 2 orders each scoped term's postings by descending weight.
A B-tree seek now exposes its maximum weight without aggregating the entire list.
This reorders the existing primary key; it does not add a second large postings
index. Queries first try an exact bounded-prefix plan:

1. Consume short posting lists completely (at most 64 entries each). For longer
   lists, retain their maximum weights as conservative score bounds.
2. Score the union of those short-list items and a qualified key-ordered prefix.
   The prefix uses the same pool precedence and validity rules as final results.
3. Stop only when the last required candidate beats the maximum possible unseen
   score, or ties it and precedes every unexamined key. Bounds are computed per
   pool because a winning fact belongs to one pool. Ineligible postings can make
   a bound looser, never smaller than a possible winner's score.
4. When two query terms leave a loose bound, try a competitive intersection:
   if the qualified prefix proves tie order at the current Kth score, enumerate
   every single-term list and weight-pair intersection that can score strictly
   higher. Rescore the resulting union with the same qualification rules.
5. If either proof fails, run the original full postings aggregation.

The plan includes one extra candidate to preserve the exact `has_more` flag.
Large candidate windows, many query/pool combinations, or a large union of short
lists go directly to the aggregation fallback. The intersection path supports
two terms, at most four pools, and the existing 1/2/3 weights. It aborts to the
general plan if its candidate union exceeds 2,048; the cap never silently drops
winners. Disjoint frequent terms with a late overlapping match test this path.
Three or more terms and large intersections can still require aggregation.
This is not WAND or Block-Max; worst-case query work
still grows with matching postings. Prefix qualification can itself visit many
entries when records are inactive or shadowed.

Each index entry is scoped by tenant, user, Agent and pool. Shared pools have an
Agent-independent index key; private entries retain the Agent ID. The trusted
application still supplies bindings; an index does not add authentication.

Candidates must pass pool precedence and private-fact status/validity checks
before SQL applies its Top-K limit. In particular, a relevant shared value cannot
resurrect a key shadowed by a private value with a lower query score. Legacy private
facts with duplicate keys still resolve to the first fact ID. Live shared records
retain the existing pool API's status/validity behavior.

SQLite triggers update the index in the source write transaction, covering both
high-level `remember`/`forget` and low-level `Store` writes. Shared CAS failures and
transaction rollbacks cannot leave index-only changes. Selection, shadow sampling
and fact-body retrieval use one SQLite read transaction, so concurrent updates
cannot invalidate the chosen record between these steps.
Connections cache prepared query and fact-write statements, including their
trigger programs. Statement reuse does not cache query results or weaken CAS,
read snapshots, or commit durability.

The `error` conflict policy deliberately retains global full-scan resolution:
conflicts outside Top-K must still be reported. Other Store backends may return
`None` from `recall_candidates` to preserve their legacy behavior. `memories()` and
explicit pool listing also remain exhaustive; this optimization targets context
retrieval, not pagination of all stored facts.

## Bounded diagnostics

A normal indexed query loads at most `max_facts + 64` winning fact bodies: the
requested candidates plus an omission sample. Shadow diagnostics may fetch up to
64 additional losing records. Omission and shadow arrays each contain at most
64 entries. These bounds do not promise a bound on matching postings or SQLite
index work: a frequent term may still match the entire visible corpus.

Shadow lookups are batched across the selected IDs in the same read transaction.
Their order remains selected-record order, then pool order and fact ID. This avoids
repeating a diagnostic CTE for every record, an overhead exposed by the SQLite
3.51.3 upgrade; it does not change ranking or expand the diagnostic sample.

Relevant report fields:

| Field | Meaning |
|---|---|
| `recall.method` | Existing scoring name: `lexical_overlap_v1` or `key_order` |
| `recall.retrieval` | `sqlite_inverted_v1` or `full_scan` |
| `recall.ranking_plan` | `bounded_prefix`, `bounded_intersection`, `postings_aggregate`, `key_order`, or `full_scan`; the retrieval identifier remains stable across index schema versions |
| `recall.candidate_limit` | Requested indexed candidate window, including the 64-entry diagnostic sample; fallback may load more |
| `recall.inspected_facts` | Loaded winning facts used by the final ranker |
| `recall.matched_facts` | Positive-score facts among those loaded candidates |
| `recall.counts_apply_to` | `loaded_candidates`; these counts are not global totals |
| `recall.candidates_truncated` | Additional winning candidates existed beyond the index window |
| `recall.index_entries_visited` | `null`: physical index work is not instrumented |
| `diagnostic_limit` | 64 |
| `omissions_truncated` | Some omission details were not enumerated |
| `pools.shadowed_scope` | `loaded_candidate_keys` for indexed retrieval, `all_visible_keys` for fallback |
| `pools.shadowed_truncated` | Shadow coverage is incomplete, including when candidate keys were omitted |

Small datasets retain their previous detailed omissions. Consumers of the old
exhaustive report must inspect these flags instead of treating list lengths as
global omitted/shadowed totals. Scope, ranking and budget behavior still apply to
the returned facts and text. Message retrieval remains on its existing path;
only its diagnostic output is capped here. The context token budget is still a
UTF-8-byte estimate, not a model-tokenizer guarantee.

## Existing databases and deployment

On first open, the new SDK creates index schema version 2 and backfills existing
private facts and live shared records in one immediate transaction. Existing
fact/document tables and shared revision tombstones remain authoritative. Failure
rolls back the entire index migration, including its schema. Later opens reuse
that version. The one-time backfill takes additional time and disk space; it is
reported separately from steady-state query latency in the benchmark.

Existing version 1 indexes migrate atomically by copying their postings into the
new primary-key order and replacing the maintenance triggers; facts and item IDs
are preserved. Migration failure rolls back the version, tables, and triggers.
The copy needs temporary additional disk space and a write lock. Freed pages stay
in the SQLite file for reuse: the file need not shrink when the old table is
dropped. No automatic `VACUUM` is performed. Version 1 SDKs reject a version 2
database, so upgrade all processes before reopening a migrated persistent file.
Allow space for the migration's WAL as well as both postings layouts; final file
size is not a measurement of peak migration disk usage.

Run the first migration during a maintenance/startup window before admitting
concurrent writers. SQLite's existing busy timeout still applies to other
connections waiting for the migration's write lock. The index is maintained by
versioned SQLite function `memweft_recall_terms_v1`, registered on each SDK
connection. **Old binaries and raw SQLite connections without this function cannot
insert/update facts after migration**: such writes fail instead of silently making
the index stale. Upgrade all writers together; do not roll back the executable
alone against a migrated database. Read-only SQL inspection remains available.
Use a normal database backup before upgrading persistent application data.

## Verification and performance evidence

- 900 differential contexts compare indexed results with exhaustive pool merging
  and scoring across pool orders, Agents, empty/no-match/Chinese/JSON-value queries
  and limits 0/1/10/30/200.
- Targeted cases cover shadowed high scores, global strict conflicts, duplicate
  private keys, validity/status changes, CAS, deletion/recreation and reopen.
- Migration tests verify legacy backfill, tombstones, idempotence and rollback on
  invalid source JSON. Concurrent shared writes/read contexts check snapshot safety.
- Another 276 differential checks cover dense terms, 64/65-posting boundaries,
  late high scores, fallback, larger limits, and mixed-pool shadowing. Version 1
  migration tests verify preserved weights, rollback, trigger maintenance, and
  subsequent cached writes/deletes.
- Another 97 differential checks cover competitive intersections, weight tiers,
  pool precedence and candidate-cap fallback (1,273 differential checks total).
- A dedicated batched-diagnostic test checks relevance-ordered keys, pool order,
  differing values and the 64-entry truncation boundary. SQLite upgrade and
  final full-context comparisons are in the [WAL/Agent follow-up](../evals/reports/2026-09-22-wal-and-agent.md).
- The paired benchmark uses old/new release native libraries in separate processes
  with identical fixture copies, comparing complete Python SDK contexts (including
  Rust rendering and JSON transport), not candidate-only SQL timings.

Benchmark runner: [`benchmark_indexed.py`](../evals/benchmark_indexed.py).
Measured results: [indexed retrieval report](../evals/reports/2026-09-22-indexed-recall.md).
Full storage stress still exercises concurrent updates, isolation, process-crash
recovery and 100/1,000/10,000/100,000-fact files.

The subsequent [bounded retrieval benchmark](../evals/benchmark_bounded.py) compares
version 1 and version 2 query/write costs on identical copied files, expands to
one million facts, and uses independent processes for concurrent readers and a
shared-pool writer. See the [measured follow-up report](../evals/reports/2026-09-22-bounded-recall.md).

The [intersection and background-maintenance report](../evals/reports/2026-09-22-pipeline-and-query.md)
adds fixed-rate read/write comparisons and a second exact query path, without
changing index schema version 2. [Background checkpointing](async_pipeline.md)
is a separate opt-in storage option; normal write acknowledgements still follow
the source/index transaction commit.

The remaining performance boundaries are broad-term scoring, `error` mode's
full-scan conflict validation, full history retrieval, and large explicit context
budgets. This change does not address the historical-message prompt injection or
business-label errors identified in the enterprise model evaluation.
