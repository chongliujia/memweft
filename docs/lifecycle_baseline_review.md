# External lifecycle baseline review — 2026-10-04

This is a capability review, not an executed external benchmark or a novelty
claim. The v1 pilot contains only our explicit plain/versions adapters and the
actual MemWeft lifecycle. A product name must not be attached to those adapters.

| Candidate | Verified primary-source capability | What must be established before comparison |
| --- | --- | --- |
| Mem0 OSS | Update replaces a memory's text/metadata; deletion operates on memory IDs or scoped collections | Pin an OSS release; map source IDs and separately persisted strategies; inspect actual behavior of dependency deletion, not just direct memory removal |
| Graphiti | Temporal fact validity, source-episode provenance and retained superseded facts; graph backend plus model/embedding integration | Pin a commit and backend; use native temporal retrieval and episode operations; account for extraction, embedding and query calls; distinguish invalidation from physical deletion |
| MoM / P-Mem | Active current values plus retained provenance, typed changes and recovery along prior-value chains | Obtain a verifiable implementation/version or label any reproduction explicitly; match revoke/recovery semantics before scoring hard-forget cases |

Mem0 sources: [update](https://docs.mem0.ai/core-concepts/memory-operations/update)
and [delete](https://docs.mem0.ai/core-concepts/memory-operations/delete).
These pages establish ordinary CRUD support; they do not, by themselves, establish
MemWeft's exact pending-job/derived-strategy transactional contract. Absence from
these pages is not proof that a capability does not exist elsewhere.

Graphiti source: [official repository README](https://github.com/getzep/graphiti).
Temporal invalidation and retained history are native features. It would be an
unfair comparison to flatten all historical graph facts into an unfiltered prompt
and attribute the resulting stale answers to its intended current-state retrieval.
Its default inference/embedding stack also differs from this Kimi-only pilot.

MoM source: [paper v1](https://arxiv.org/html/2609.25054v1), especially sections
3.2–3.4 and the limitations. This is close prior work: explicit current state,
provenance, revision and recovery already appear there. It retains displaced
values and distinguishes semantic revocation from erasure; its limitations discuss
the additional work needed for privacy deletion. A MemWeft paper must not claim
that tracking source versions or retaining a current view is new. Semantic conflicts
between observations are also different from SQLite writer races. This review did
not verify a runnable P-Mem release.

## Proposed next experiment

Prioritize Graphiti as an external temporal/provenance system, with Mem0 as a
secondary practical memory baseline. First run a small compatibility fixture:
initial fact → superseding fact → current retrieval; delete a source episode;
verify the documented retention behavior. Only then freeze a new task protocol.

Use two separately reported tracks. Native semantic ingestion lets each system
extract and reconcile the same text stream, while charging all model and embedding
calls. Structured source events isolate lifecycle handling without extraction, but
any application-defined derivative adapter must be listed as added functionality.
Do not compare a native end-to-end system with an uncharged oracle-fed adapter.

For each system, record commit/package hashes, backend, model, prompt and output
budgets, ingestion costs, current-read behavior, retained history, error handling,
and whether publication/rollback atomicity is native or application-provided.
Unsupported operations should be marked outside the common contract, not counted
as incorrect answers. Hard erasure and reversible revision require separate tests.
