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

## Executed structured CRUD fixture — 2026-10-04

The subsequent v2 preparation now executes **Mem0 OSS 2.2.1** through
[`external_lifecycle_baseline.py`](../evals/external_lifecycle_baseline.py), with
Qdrant client 1.19.1 in local persistent mode and actual
`sentence-transformers/all-MiniLM-L12-v2` CPU embeddings. It uses
`add(infer=False)`, `update`, exact metadata-filtered `get_all`, ID-based `get`,
`delete` and `history`; no LLM extraction or semantic retrieval is evaluated.
The fixture closes and reopens storage before reading final state.

| Executed operation | Observed behavior |
| --- | --- |
| Add an explicit source and a separate strategy | Both structured records round-trip through native Mem0 storage |
| Update the source by its memory ID | Current source changes in place; separate strategy stays unchanged |
| Delete the source by its memory ID | Source disappears from current reads; separate strategy remains |
| Close and reopen | Current source remains absent and the strategy persists |
| Read source history after deletion | ADD, UPDATE and DELETE history still contains prior source text |
| Read using a different user scope | Exact scoped listing does not expose the other scope's records |

The compatibility fixture installed no service and created no hosted account.
An audit hook rejecting `socket.connect` and `socket.getaddrinfo` recorded zero
network attempts; the inference guard recorded zero LLM attempts. Two opt-in
integration tests passed, including six event schedules. Local raw evidence is
retained under `data/external-lifecycle-env/` (Gitignored):
`compatibility-final/result.json`, `dependency-receipt.json`, and
`test-revision3-final.log`. The [evaluation guide](../evals/README.md) gives the
installation and preparation commands; the [requirements file](../evals/requirements-external-baseline.txt)
pins the main dependencies without copying unrelated global packages.

This result establishes CRUD behavior for separately stored application objects.
The adapter supplies JSON serialization, kind/key addressing, revision labels,
sequential update-or-add and explicit local-Qdrant client cleanup. Dependency
snapshots are application data, not native Mem0 dependency declarations. Selecting
and writing an earlier strategy is an application rollback emulation. Retaining
that separate strategy is not a failure of a promised native dependency/rollback
contract, and retained history means direct deletion is not hard erasure. No
concurrent publication, semantic extraction, similarity ranking, graph-memory or
hosted-platform quality claim follows from this structured track.

The official Mem0 2.2.1 wheel's SHA-256 is
`fe91bb91ac8926231993a4aa58df00a60c6c74c709e6a338fe399500776eef4d`.
The installation receipt matches that value against
[PyPI's versioned metadata](https://pypi.org/pypi/mem0ai/2.2.1/json). This identifies
the downloaded official wheel; it is **not** an attestation of every file in the
running `site-packages` tree. The run additionally records package versions and
local embedding file hashes. Its dedicated environment reused preinstalled
embedding libraries through `--system-site-packages`, with compatible overrides
installed only inside the venv; this is disclosed rather than presented as a
fully hermetic environment.

Graphiti 0.30.2 was reviewed first using its
[official quickstart](https://help.getzep.com/graphiti/getting-started/quick-start),
[package metadata](https://pypi.org/pypi/graphiti-core/0.30.2/json), and
[repository configuration](https://github.com/getzep/graphiti/blob/main/pyproject.toml).
No supported graph backend was already running in the checked local containers;
Kuzu's optional integration is marked deprecated, and FalkorDBLite requires
Python 3.12 or later while this fixture uses 3.11. A native inference/embedding
pipeline and a new graph service would require separate provisioning and cost
accounting. We selected executable local Mem0 CRUD for this bounded track.
Graphiti was not executed and receives neither a score nor an inferred failure.
