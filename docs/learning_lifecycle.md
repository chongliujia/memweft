# Learning dependencies: semantics, implementation and research scope

Status: implementation and bounded experimental evidence, not a mechanically
verified system or a claim of research novelty. This note specifies the existing
SQLite lifecycle and the source-index optimization. It does not change adoption
policy, the model, or how applications declare dependencies.

## State and assumptions

A shared source is identified by `(tenant, user, pool, key)` and a monotonically
increasing revision. Deletion retains the revision tombstone. A private source is
identified by `(tenant, user, agent, key)`; private facts retain their older API
without per-key revisions. A scope-level learning generation guards private
source changes and strategy publication.

A strategy has a version, task/target slot, declared private/shared dependencies,
and inherited dependencies from its baseline. A pending job captures its baseline
pointer revision, generation, and shared-source revisions. Historical versions
and the active pointer are distinct records. The dependency relation is the
recursive occurrence of `source_keys`, `source_pools`, and `pool_revisions` in a
learning document, preserving the previous scanner's membership rules.

Assumptions: identities and pool bindings are chosen by a trusted application;
dependencies are correctly declared; all writers use supported SDK connections
and transactions; the SQLite database and its indexes have not been externally
tampered with. Application-supplied evaluator results are trusted observations.
These assumptions do not assert that a strategy's natural-language content is
correct or actually follows its cited sources.

## Operations and properties

| Operation | Committed behavior | Relevant check |
|---|---|---|
| Start a job | Record source/baseline versions and the generation | Missing private active keys or shared records are rejected; shared revisions are checked when saving the job |
| Adopt a candidate | Publish only when evaluation policy and captured guards pass | Job, baseline and generation CAS plus shared revisions; writes commit together |
| Update/forget a source | Invalidate matching pending jobs and saved versions; replace matching active values with revisioned nulls | Invalidation and source mutation share one write transaction |
| Recreate a shared key | Allocate a revision newer than the deletion tombstone | A positive pre-deletion revision cannot update the recreated record |
| Roll back | Restore a saved version only if current pointer, generation and source guards still match | Reading generation precedes reading the saved version; publication uses checked mutation |

Private updates also advance the scope generation. An unrelated in-flight job
in that private scope may therefore conflict; this is conservative invalidation,
not per-key concurrency control. Unrelated accepted strategies remain available.
Forgetting an absent private key still advances the generation. Reading source
status for job creation retains the existing `active` check, not an added
validity-at-current-time requirement.

The intended safety property is scoped to publication and subsequent reads:
after a source change commits, a newly started read of an active strategy cannot
return a declared derivative invalidated by that change, unless a later valid
operation has published a replacement. An already materialized context or an
earlier SQLite read snapshot is not revoked. No cancellation of in-flight model
requests, model unlearning, or atomic snapshot of an entire multi-call application
workflow follows from this property.

## Proof sketches and their limits

**Index fidelity.** Each learning-document insert creates exactly the distinct
declared dependency edges. Replacement removes the old edges and inserts the new
ones; deletion cascades to its edges. These actions are SQLite triggers in the
document transaction. Backfill uses the same extractor and commits with index
schema/version creation. Starting from a valid backfill, induction over committed
document mutations preserves equality with the recursive scan relation. A failed
transaction restores both documents and edges. This is an informal argument;
extractor agreement, rollback and migration failures have executable tests.

**No stale republication through the supported lifecycle.** A source mutation
and invalidation serialize with strategy publication at SQLite write transactions.
If publication commits first, the following source mutation invalidates it. If
the source mutation commits first, a previously captured shared revision, private
generation, job revision or active-pointer revision rejects stale publication.
Inherited dependencies participate in both discovery and validation. This argument
requires the ordering of reads and guards in `start`, `submit` and `rollback`;
it does not establish correctness of arbitrary direct document writes.

**Delete/recreate protection.** Shared revision counters increase across deletion
and recreation; equality against a previous positive revision must fail. Revision
overflow fails rather than wrapping. Private facts have different, scope-level
semantics and should not be described as having this shared-key CAS contract.

The new index is a conventional materialized reverse relation, not a novel
consistency protocol. SQLite transactions, CAS and reverse indexes are engineering
building blocks. A paper still needs related-work comparison and task evidence
to establish whether their specific use here is a useful research contribution.

## Cost model

Let F be facts in a pool, D learning documents, E declared dependency edges,
A documents affected by one source change, and B the total bytes of affected
documents. The old source lookup enumerated F records, and invalidation decoded
all D learning documents in the applicable scope.

Shared source lookup now uses its primary key; private source existence uses the
scoped fact-key index (duplicate private keys may require multiple index entries).
Each declared source still requires a lookup. Invalidation seeks the reverse
index and fetches A matching documents, rather than decoding unrelated documents.
Its cost still includes B, every affected document write and removal of that
document's dependency edges. Large fanout is therefore inherently expensive;
this is not constant-time invalidation or a bound on all memory/work.

The tradeoff is persistent edge storage, extraction on learning-document writes,
and a one-time backfill. Measurements must report source lookup, low/high-fanout
invalidation, document-write overhead, database size and migration separately.

## Migration and operation

The learning source index has its own version `1`, independent of recall schema
v3 and SDK package versions. First open backfills existing learning documents in
an IMMEDIATE transaction. Failure rolls back the new index objects and edges;
later opens reuse the version. Unknown future source-index versions are rejected.

Back up persistent databases and upgrade writers together before admitting work.
The index triggers require `memweft_learning_sources_v1`, registered on every new
SDK connection. Older SDK/raw connections without it cannot insert or update
documents successfully, including non-learning documents: SQLite resolves the
function before evaluating the trigger's namespace condition. Failed statements
do not partially erase edges. Use SDK operations for application document writes.
Do not downgrade only the executable on an upgraded database. Restore the matched
pre-upgrade backup/build if rollback is required. Backfill needs write-lock time
and additional disk/WAL space; final file sizes do not measure peak migration space.

## Experimental protocol and next research step

The Rust benchmark `benchmark_learning_sources` measures real learning start and
Store operations on independent file databases at 1k/10k/100k facts per pool and
the same counts of unrelated learning documents. It includes 8- and 1,000-document
fanout, one warmup and 20 samples. Seeding, dependent resets and outcome checks are
outside operation timings; no model or API is called. Synthetic document-write
cases isolate storage costs and are not complete strategy adoption transactions.

Correctness evidence includes scan/index differential cases, inherited sources,
scope isolation, low-level writes, transaction rollback, migration failure,
delete/recreate, and concurrent source update/adoption/rollback on file connections.
Passing finite tests does not exhaust all interleavings or prove model quality.

For a research study, freeze tasks and evaluation before running four conditions:
no persistent memory, plain keyed memory, memory with versions but no derivative
invalidation, and the complete lifecycle. Add a representative external baseline
configured according to its supported semantics. Measure stale-strategy use,
task success, false invalidation/conflict, latency and total ingestion/query cost.
Use independent held-out tasks, multiple model backbones and repeated runs with
uncertainty estimates. Source-index before/after comparisons isolate storage cost;
they are not evidence that the existing consistency policy improves task accuracy.

Real developer handoffs, long-running contention and high dependency fanout remain
separate validation work. No new user-study, model-quality or novelty claim is
established by this optimization.

## Follow-up pilot and pool shutdown

The [frozen lifecycle task pilot](lifecycle_task_protocol.md) adds a strong lazy
version-checking baseline that validates inherited dependencies at read, adoption
and rollback. Its results are reported separately from the source-index storage
benchmark. The baseline adapter supports the scripted event order, not arbitrary
concurrent writers. See [the task report](../evals/reports/2026-10-04-lifecycle-tasks.md)
and [external capability review](lifecycle_baseline_review.md).

The expanded CI workload exposed retired r2d2 housekeeping workers waiting for
delayed tasks after a store closed. SQLite pool schedulers now discard queued
housekeeping when the last pool owner is released. Already-running tasks finish;
application writes retain their pool ownership. This does not make `close` an
in-flight request cancellation or promise a thread-join deadline. A 1,000-store
reopen regression and a delayed-job release check cover the change. The pilot's
frozen native build predates this resource fix; its recorded source/native hashes
and model inputs remain unchanged.
