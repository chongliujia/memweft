# Local model scenario evaluation

Run synthetic, inspectable agent tasks against the real Rust memory and learning
core and a local chat-completions endpoint. The runner uses Python's standard
library and the MemWeft CLI; it needs no Python native extension or model SDK.
It makes real model calls, writes an isolated SQLite database per run, and never
executes model-generated tools or changes production memory.

```bash
cargo build --locked -p memweft --bin memweft
python -m unittest discover -s evals -p 'test_*.py' -v
python evals/run_local.py --check-memory
python evals/run_local.py \
  --base-url http://127.0.0.1:8002/v1 \
  --model qwen3-8b \
  --repeats 2
# Query-ranked recall plus learning counterexamples (145 calls at two repeats):
python evals/run_local.py --suite evals/scenarios/common-v2.json --repeats 2
# Same v2 tasks with explicit output contracts; compare two independent runs:
python evals/run_local.py --suite evals/scenarios/common-v3.json --output-contract none
python evals/run_local.py --suite evals/scenarios/common-v3.json --output-contract schema
```

Run on the machine hosting the endpoint. HTTP requests bypass proxies. The API
key defaults to `EMPTY`; override with `MEMWEFT_EVAL_API_KEY` if needed. The
Qwen/vLLM request disables thinking with `chat_template_kwargs.enable_thinking`,
sets temperature to 0 and seed to 42. Other servers must accept these fields or
the client must be adapted. No model-provider fallback or automatic retry is used.

Artifacts go into a new directory under `data/evals/` (ignored by Git). Supply
`--output PATH` to select a **new** directory; existing directories are rejected.
`--binary PATH`, `--suite PATH`, `--timeout`, `--max-tokens`, and `--seed` are
available. Failures preserve partial JSONL logs and a failed `status.json`;
rerun into a new directory after correcting the cause.

## Output contracts

`common-v3.json` preserves the v2 prompts, expected answers, memories and dataset
splits. It adds explicit, separately authored `output_contracts` and references
them by name. Schema definitions do not contain answers, defaults or examples.
All memory fields permit `null` whether the expected answer is known or absent;
the same schema is used for all memory modes. Ticket routing permits the queues
and priorities already declared in the original task instruction.

`--output-contract` selects:

- `none` (default): the original prompts and no `response_format`. Declared
  schemas, if present, are still used to measure protocol compliance afterward.
- `prompt`: append the explicit schema/field instructions to the system message.
- `schema`: use the same instructions plus
  `response_format: {type: "json_schema", json_schema: {name, strict: true, schema}}`.

The local server must support `json_schema` for constrained decoding. Unsupported
requests fail rather than silently falling back. `prompt` is an explicit option
for servers without that capability, but it does not guarantee conformity.
This is an application/model-call contract; the Rust memory API continues to
return memory context without making model calls or rewriting model answers.

The validator in `output_contract.py` deliberately supports only closed, flat
objects with all properties required, scalar `string`/`integer`/`boolean`/`null`
types (or unions), and optional enums. Other features fail at startup. It does
not claim full JSON Schema support. Answer parsing rejects duplicate keys and
nonstandard NaN/Infinity constants; the evaluator version is `exact-json-fields-v2`.

All responses retain the original end-to-end strict score. The additional
`protocol_valid` metric checks fields, types, enums and complete generation;
`content_correct` compares values only when protocol validation succeeds.
Invalid protocols have `content_correct: null`, not an inferred success or a
guessed field mapping. Report denominators expose how many answers were actually
eligible for content scoring. Missing memory can therefore produce a valid
`{"port": null}` response that still fails the task's content score.

Contract mode covers training, proposal generation, validation and final testing.
Each run generates and evaluates its own candidate. Changes in learning results
between runs can involve both output formatting and a different proposal;
memory-only scenarios provide the cleaner comparison of output contracts.
The `schema`/`none` comparison measures the combined instructions and constrained
decoding. It does not isolate the decoder's contribution from the prompt change.

## What is measured

The fixture has 12 memory cases plus three final held-out workflow cases.
Every final case is evaluated in three modes:

- `no_memory`: the current task without persistent context.
- `memory`: actual Rust-built context; no learned strategy.
- `memory_learning`: actual context with the accepted strategy for the matching
  task. If the candidate is rejected, the baseline remains active. Non-workflow
  cases have no learned strategy, so this mode is a stability control there.

The original `common.json` (`common-v1`) is retained for reproducibility.
`common-v2.json` supplies each memory-case prompt as the Rust context `query`,
and adds a 13th memory case that explicitly sets `query: null` to retain key
ordering as an in-run control. It keeps all original task prompts and expected
answers, including the output-field failure found in the first run.

V2 keeps the same three training cases and adds three ordinary/non-urgent
counterexamples to **each** of validation and final test: routine billing,
security and account assistance. These examples never reach the proposer.
An overgeneralized rule can now fail the per-case regression gate even when
its average score improves. The active strategy is checked after submission;
rejected candidates must leave the baseline context unchanged. A rejection is
a valid measured result; the runner does not regenerate candidates to pass.

Memory cases cover preferences, reopening the same conversation, retry
idempotency, updating facts, forgetting while preserving unrelated facts,
user/tenant/agent isolation, and recall under 40 distractor facts. The pressure
case uses the default `max_facts=30`; its control uses `max_facts=64`. Both have
the same context token budget. Facts are explicitly supplied by the fixture;
this does not test automatic extraction or autonomous memory consolidation.

Workflow learning simulates customer-support ticket routing. Three training
cases produce real answers and feedback with known business labels. The model
proposer sees **only training feedback**, not validation/test cases. Rust pins
the validation cases (three in v1, six in v2/v3, with repeated observations), and adopts only
if mean gain is at least 0.05 with no per-observation regression, within the
configured latency and token-cost limits. Final held-out cases are called only
after the adoption decision. No second proposal is tuned against failed
validation or test examples. After successful adoption, rollback is checked
against the original context.

Training, validation and test use different wording of the same three business
rules. This measures transfer of learned routing rules, not broad reasoning
improvement or cross-domain generalization. It does not change model weights.

Scoring is strict JSON object/field/type/value matching, not model self-rating.
Missing fields, extra fields, wrong values, invalid JSON and truncated
completions fail. Missing information should be reported as JSON `null` where
the task requests it. Memory-dependent tasks intentionally deprive the
no-memory baseline of historical information; the difference measures access
to useful context, not superiority over another memory system.

## Evidence and interpretation

- `report.md`, `summary.json`: final-test results by scenario and mode.
- `calls.jsonl`: all model payloads, raw responses, actual usage and wall time.
- `results.jsonl`: training, validation and final-test scores and context.
- `learning.json`: candidate, pinned evidence, acceptance decision and reason.
- `adoption_check.json`: confirmation that acceptance changed the active strategy,
  or rejection preserved the baseline.
- `rollback.json`: successful post-evaluation rollback check, when applicable.
- `core.jsonl`, `contexts.json`, `memory.db`: actual Rust requests and stored state.
- `metadata.json`, `suite.json`, `status.json`: configuration, fixture/binary
  hashes, fixture snapshot and completion status. API key headers are not logged.

With two repeats, a complete run makes 109 model calls: 6 training answers,
1 proposal, 12 paired validation answers, and 90 final-test answers. The
reported final-test metrics exclude training and validation. Learning cost is
measured in **thousands of server-reported total tokens**, with a limit of 100
for the candidate validation calls; it is not currency or whole-run cost.
Actual whole-run token usage can be summed from `calls.jsonl`.

V2/v3 make 145 calls per run at two repeats: 6 training, 1 proposal, 24 validation and 114
final-test answers (13 memory cases plus 6 workflow cases, three modes each).
Two fresh v3 runs (`none` and `schema`) make 290 calls altogether.

Latency is HTTP/inference wall time and excludes CLI startup and storage.
Separate `cli_wall_ms` measurements include process and database startup, so
they are not in-process storage benchmarks. Call order rotates across modes;
warm caches may still affect latency. Repeated temperature-zero runs are
stability observations, not independent samples. Small-set pass rates and
sample p95 values should not be interpreted as statistical confidence or SLOs.

Expected scenario failures are findings and do not make the runner exit with an
error. Infrastructure, fixture or memory-invariant failures do. This keeps the
suite useful for characterizing current limitations without inventing passing
results. The network evaluation is opt-in and is not part of normal CI.

## Learning boundaries (common-v4)

V4 preserves the 13 memory control cases and replaces the learning validation/test
sets with new prompts authored before running the model. It covers six families:
confirmed payment with missing delivery, ordinary billing, ongoing account
compromise, preventive security advice, account self-service, and resolved
historical compromise. The final test also includes an incident that recurs after
an apparent resolution. There are 12 training, 12 validation and 18 test cases.
Training development uses findings from v2/v3; those older held-out sets are now
retired for development, and are not presented as fresh evidence.

The same pinned suite supports two profiles:

- `legacy`: the original three positive examples and original proposal prompt.
- `boundaries` (v4 default): all 12 training examples, with instructions to state
  scope, conditions, exceptions and fallback behavior, separate queue selection
  from severity, and account for negation and whether an incident is ongoing.
  The proposal output remains `{"content": "..."}`. Its completion limit is 1536
  tokens versus the legacy 768; answer-call settings are identical.

```bash
python evals/run_local.py --suite evals/scenarios/common-v4.json --output-contract schema \
  --learning-profile legacy --output data/evals/qwen3-8b-common-v4-legacy-run1
python evals/run_local.py --suite evals/scenarios/common-v4.json --output-contract schema \
  --learning-profile boundaries --output data/evals/qwen3-8b-common-v4-boundaries-run1
python evals/compare_learning.py \
  data/evals/qwen3-8b-common-v4-legacy-run1 data/evals/qwen3-8b-common-v4-boundaries-run1 \
  --output data/evals/v4-comparison.json
```

At two repeats these runs make 241 and 259 calls respectively (500 total).
`proposal_input.json` captures exactly which training feedback reached the
proposer. Neither validation nor test prompts/labels reach it. Adoption retains
the existing minimum gain and zero per-observation regression gate. There is
one proposal per run; no retry tuned to this run's validation or final test.

`learning_effect.json` compares the pre-learning memory baseline to the actually
adopted strategy on held-out workflow cases, including individual regressions.
`task_scope_check.json` verifies that a strategy is not injected into unrelated
task types. Accepted candidates also exercise rollback after final evaluation.
The comparison checks matching fixture, binary, runner, contract, sampling and
policy settings. It measures the combined training/prompt change, not their
individual contributions. This is same-domain rule transfer, not evidence of
broad or cross-domain generalization, automatic fact extraction, or model-weight
training. The reusable core continues to accept application-provided proposers
and evaluators; this bounded proposer is part of the opt-in local evaluation.

## Training-only refinement (common-v5)

The initial v4 experiment is retained: expanded examples plus a bounded prompt
still produced a candidate with a per-case regression, so it was rejected.
V5 keeps the 12 training examples unchanged and uses another fresh set of 12
validation and 18 test prompts. V4 is now development evidence, not an independent
test of subsequent changes.

`--learning-profile boundaries_refined` performs at most three proposal rounds.
Each proposed rule is applied to the **training** cases and scored with the same
strict field/value evaluator. The next proposal receives the original feedback,
previous candidate and its training errors. It stops early if all training
observations pass; otherwise it selects the highest training pass count, with
the earliest candidate winning ties. That single candidate is then submitted to
the unchanged Rust validation gate exactly once. A validation failure never
triggers a further proposal. Final tests are run after that decision.

```bash
python evals/run_local.py --suite evals/scenarios/common-v5.json --output-contract schema \
  --learning-profile boundaries_refined --output data/evals/qwen3-8b-common-v5-refined-run1
```

`proposal_round_N.json` saves each proposal input; `training_rounds.json` saves
all training checks, selection and the fixed round limit. At two repeats the
run makes 283, 308 or 333 calls for one, two or three rounds respectively.
Training perfection is not an adoption criterion or a claim of generalization:
validation can reject a perfectly fitted candidate, and the untouched final
set can reveal remaining failures after adoption. Compare learning against
its **same-run pre-learning baseline**. Do not compare v4/v5 headline pass rates
as a controlled improvement, because their held-out questions differ.

### Rules grouped by observed training actions

`--learning-profile grouped_rules` addresses ambiguous global summaries by
partitioning the same training cases by their expected action. For each observed
`(queue, priority)` pair, the proposer receives positive examples and all other
training cases as counterexamples. It returns a closed JSON object with `when`
and `unless` strings. The runner attaches the action from the training label and
compiles these clauses into one task strategy; it does not author business
conditions or infer labels from held-out answers.

Up to two rounds generate and check all rules on training data. Round two, if
needed, receives the previous rules and training results. The highest training
score is selected before a single independent validation. For these five action
groups and two repeats, a full run takes 287 calls for one round or 316 for two.

```bash
python evals/run_local.py --suite evals/scenarios/common-v5.json --output-contract schema \
  --learning-profile grouped_rules --output data/evals/qwen3-8b-common-v5-grouped-run1
```

This approach was designed after reviewing only v5's **training** failure to
separate ordinary account assistance from security advice. The v5 validation/test
scores and answers had not been inspected when its implementation and tests were
fixed. Its primary evidence is the within-run pre-learning versus adopted-strategy
comparison. The earlier refined run used a different runner revision, so the
strict cross-run comparison tool intentionally does not treat those two runs as
identical-code experiments. No extra proposal is generated from validation or
final-test failures.

The [measured boundary-learning report](reports/2026-09-22-qwen3-8b-learning-boundaries.md)
records all four runs, including rejected candidates. The grouped candidate
passed validation but regressed on one held-out question (two repeated calls),
so it did not pass the final generalization check. `compare_learning.py` can
summarize one run or strictly compare multiple matching runs. Its
`generalization_check` requires an accepted candidate, sufficient positive
held-out gain and zero held-out regressions; it is reporting, not another
candidate-selection step or a claim of production readiness.

## Evidence-grounded decisions (common-v6)

V6 has 18 training cases, 18 new validation cases and 24 new final-test cases.
Two v5 final failures are explicitly retired into training with `origin` fields;
four additional training boundaries cover failed payments, urgency without a
service failure, hypothetical future attacks, and recurrence after resolution.
Every v6 held-out prompt is distinct from earlier fixtures and was authored before
v6 model calls. V5 is now development evidence, not a fresh test of this change.

`--learning-profile evidence_table` groups the original labelled training cases
by their observed output. It preserves their wording and labels in a compact
strategy instead of asking the model to invent a summary condition. Contradictory
labels for an identical training prompt fail before any candidate is evaluated.
This is deterministic exemplar/few-shot learning, not novel rule discovery or
model-weight training. It requires reliable application-provided training labels.
The same Qwen model still makes every task decision; the scoring code never
rewrites its response to match an expected answer.

The candidate is checked against every training observation and its own baseline.
Any training regression cancels the actual Rust learning job before validation;
there is no substitute or fabricated validation score. Otherwise the existing
independent Rust adoption gate runs unchanged. `training_gate.json` records the
pairwise decision and `proposer_call_id: null` explicitly records that no model
proposal call occurred. Strategy records remain scoped by task and normal
rollback behavior still applies.

Use the previous generated-rule approach as a same-suite control:

```bash
python evals/run_local.py --suite evals/scenarios/common-v6.json --output-contract schema \
  --learning-profile grouped_rules --learning-only --output data/evals/qwen3-8b-common-v6-grouped-run1
python evals/run_local.py --suite evals/scenarios/common-v6.json --output-contract schema \
  --learning-profile evidence_table --learning-only --output data/evals/qwen3-8b-common-v6-evidence-run1
python evals/compare_learning.py \
  data/evals/qwen3-8b-common-v6-grouped-run1 data/evals/qwen3-8b-common-v6-evidence-run1 \
  --output data/evals/v6-comparison.json
```

`--learning-only` omits the unrelated memory control scenarios; it does not reuse
their old results or add them to current denominators. At two repeats the evidence
profile uses 288 model calls if the training gate passes, or 216 if it cancels
before validation. The generated-rule profile uses 293 or 334 calls depending
on its training rounds. Validation and final-test calls are still disjoint.
Run without this flag to include the 13 unchanged memory control cases.

The runner now snapshots its source, output-contract module and evidence builder
alongside the fixture. The comparison checks the evidence-builder hash and
`learning_only` flag as well as the existing model/core/runner settings. The
comparison measures the combined strategy representation and training guard;
it does not isolate the two changes. This opt-in experiment does not enable
automatic learning or auto-extraction in the SDK.

The [V6 measured report](reports/2026-09-22-qwen3-8b-v6.md) records a held-out
improvement from 34/48 to 48/48 with no regression for the evidence table. The
same-suite generated-rule control scored 44/48 with two regressed observations.
This is a bounded same-domain result; the evidence strategy used about 45% more
per-call tokens than the generated strategy. Both experimental adoptions were
rolled back after evaluation.

## Enterprise scenario and storage expansion

The [enterprise-v1 measured report](reports/2026-09-22-enterprise-v1.md) and
[JSON scorecard](reports/2026-09-22-enterprise-v1.json) record 2,385 local model
calls plus file-backed storage stress. These are reproducible synthetic probes,
not production certification. In particular, the run exposed successful prompt
injection through historical messages and remaining approval-boundary mistakes.

`run_local.py` now accepts `--temperature` (default 0) and `--seed-step`
(default 0). A repeat uses `seed + repeat * seed_step` in every paired mode,
including training and validation. The actual payload and sampling metadata are
logged. Seeds are requests to the serving implementation, not proof of independent
samples. Earlier default runs keep their original sampling behavior.

Build release binaries for the scale measurements and install the matching
Python extension into the source package (Python development dependencies must
already be available):

```bash
cargo build --release --locked --offline -p memweft --bin memweft -p memweft-ffi --lib
python - <<'PY'
import shutil, sysconfig
shutil.copy2('target/release/libmemweft_ffi.so',
             'python/src/memweft/_core' + sysconfig.get_config_var('EXT_SUFFIX'))
PY
python -m unittest discover -s evals -p 'test_*.py' -v
python evals/run_enterprise.py --output data/evals/enterprise-v1-run1
PYTHONPATH=python/src python evals/memory_adversarial.py \
  --output data/evals/enterprise-memory-run1
# Run storage measurements after the model batch has finished:
PYTHONPATH=python/src python evals/storage_stress.py \
  --output data/evals/enterprise-storage-run1
python evals/summarize_enterprise.py \
  --tasks data/evals/enterprise-v1-run1 \
  --memory data/evals/enterprise-memory-run1 \
  --storage data/evals/enterprise-storage-run1 \
  --output data/evals/enterprise-scorecard
```

Use new output paths for reruns; existing results are never overwritten. The
extension-copy command above targets this Linux environment. Native memory and
storage probes require `PYTHONPATH=python/src`; the task runner remains stdlib +
CLI. `summarize_enterprise.py` targets the default v1 configuration (three seeds,
four storage sizes with 30 samples). It audits snapshots, call IDs and paired
seeds before producing the scorecard. Other configurations retain raw summaries.

`enterprise_cases.py` is the auditable authoring source for the frozen checked-in
JSON fixtures. `run_enterprise.py` snapshots those JSON files before starting up
to three isolated subprocesses. All business labels are synthetic application
policy, never used to rewrite model responses:

- Support replays V6 to test sampling robustness; it is not fresh held-out data.
- Incident triage uses six component/severity families. Production outages still
  in progress are P1; sandbox, recovered incidents and advice are P2.
- Access requests use six request/action families. Ordinary report access needs
  manager approval, production admin access needs manager **and** security
  approval, and payroll access needs HR approval. Missing approvals route to
  review regardless of urgency. `fulfill` is only a simulated queue decision.
- The poisoned-access control flips three training labels. Its clean validation
  and test sets are identical to the access run, so this does not create extra
  independent test coverage. The contamination is documented separately and is
  not provided to the candidate builder as a hint.

Each new domain has 18 training, 18 validation and 36 final cases. There are six
semantic families with correlated phrasings, not 72 independent kinds of work.
English, temporal changes, negation, missing preconditions, urgency and reference
ambiguity appear in the final sets. Each domain learns independently; no strategy
transfer between domains is claimed. Accepted strategies are rolled back after
measurement. Independent evaluation labels are essential: training self-checks
cannot establish whether their own supplied labels are true.

The 36 native memory cases include 12 attack templates in both facts and history,
shared/private precedence and fallback, user/tenant/agent isolation, update/delete,
zero-budget behavior, and 200-message histories with a 10-message window. They
make 216 model calls at the default three seeds. That history test measures
windowing, not the model's maximum input length. No real secrets or tool execution
are involved. Only final JSON values and the explicitly checked context invariants
are evaluated; this is not a general prompt-injection defense guarantee.

Storage probes use isolated SQLite files through the real native SDK: eight
independent clients race on the same revision over 100 rounds, concurrent event
retries must deduplicate, 100 tenant/user pairs must remain isolated, and a child
writer is killed after 50 acknowledged commits. This tests process-crash recovery,
not power loss. The scale sweep writes 100/1,000/10,000/100,000 facts and measures
30 context reads at each size, then reopens the database to verify persisted
counts. RSS is a cumulative process high-water mark. SQLite runs WAL with
`synchronous=NORMAL`; report the build hash and machine measurements rather than
assuming a universal latency SLO. Pool bindings are application configuration,
not authentication of an untrusted database client.

## Retrieval algorithm research

The [Elasticsearch/Lucene research note](../docs/retrieval_research.md) separates
inverted candidate lookup, BM25 ranking and WAND/MAXSCORE skipping. An isolated
prototype compares SQL weighted postings and FTS5 over the existing 100,000-fact
fixture, preserving the original file and current SDK implementation:

```bash
PYTHONPATH=python/src python evals/retrieval_probe.py \
  --source data/evals/enterprise-storage-run1/scale.db \
  --output data/evals/retrieval-probe-new-run
```

The fixture must contain the single private scope produced by the storage test.
Results include source/build hashes, per-query measurements, storage size and
exact-order comparisons against both a reference scan and the native SDK.
This static prototype does not implement pool merging, online index maintenance,
WAND/MAXSCORE or an Elasticsearch backend. Native context timings include rendering
and diagnostics; candidate-only timings do not. Do not report their ratio as a
measured SDK speedup. The [recorded experiment](reports/2026-09-22-retrieval-research.json)
also retains the corrected FTS5 query plan and limitations.

## Complete SDK benchmark for indexed retrieval

The indexed implementation is described in [the migration and query guide](../docs/indexed_retrieval.md).
Unlike the research prototype, `benchmark_indexed.py` times the **entire** synchronous
Python SDK `context` call, including Rust ranking/rendering, diagnostics and JSON
transport. It uses separate processes for the baseline and new native libraries,
with identical fixture copies, one warm-up and 30 timed calls per query.

Preserve a release native library and the 100,000-fact **legacy** SQLite fixture
before upgrading. The source fixture must not already contain the new recall
index. The recorded local baseline is archived under
`data/evals/retrieval-before-build/`; binaries and databases are ignored by Git.
From the new checkout, after building the release native extensions:

```bash
PYTHONPATH=python/src python evals/benchmark_indexed.py \
  --before data/evals/retrieval-before-build/libmemweft_ffi.so \
  --after target/release/libmemweft_ffi.so \
  --source data/evals/enterprise-storage-run1/scale.db \
  --output data/evals/indexed-recall-new-comparison
# Run separately afterward so write load does not affect the read benchmark:
PYTHONPATH=python/src python evals/storage_stress.py \
  --output data/evals/indexed-storage-new-run
```

Profiles cover 100,000 private facts, 100,000 shared facts and a mixed profile
with 100,000 private + 100,000 same-key shared facts. Private queries cover rare,
frequent, mixed, absent and empty query terms; shared/mixed profiles cover rare
and absent terms. The mixed profile stresses shadow resolution before selection.
Every before/after pair must return identical text, facts, messages and strategies;
diagnostics intentionally change. First-open migration is timed separately.

The first implementation run exposed a slow zero-score fill for fully shadowed
pools. Its results remain in `indexed-recall-comparison-run1/`. The final
`indexed-recall-comparison-run2/` repeats the measurements after bounding later
pool scans by the best already-collected key range. The [measured report](reports/2026-09-22-indexed-recall.md)
includes both context latency and storage/write costs. These are warm-cache,
single-reader synthetic measurements; broad-term scoring, strict conflict mode
and full message-history loading still have separate scaling limits.

## Bounded scoring, writes, and million-record concurrency

`benchmark_bounded.py` compares the first indexed release with the subsequent
exact bounded-prefix plan and prepared-statement reuse. The baseline here is
already indexed, unlike the full-scan baseline in `benchmark_indexed.py`.

Prerequisites are the archived version 1 native library at
`data/evals/indexed-v1-before-build/libmemweft_ffi.so`, the three version 1 databases
under `data/evals/indexed-recall-comparison-run2/*-after/memory.db`, and the legacy
fixture `data/evals/enterprise-storage-run1/scale.db`. The runner preserves these
sources and creates disposable copies in a new output directory. Build the current
release first, then run the following **sequentially** to avoid overlapping loads:

```bash
PYTHONPATH=python/src python evals/benchmark_bounded.py \
  --output data/evals/bounded-recall-new-100k
PYTHONPATH=python/src python evals/benchmark_bounded.py --million \
  --output data/evals/bounded-recall-new-million
PYTHONPATH=python/src python evals/storage_stress.py \
  --output data/evals/bounded-storage-new-run
PYTHONPATH=python/src python evals/soak_bounded.py \
  --input data/evals/bounded-recall-new-million \
  --output data/evals/bounded-concurrency-new-run --seconds 60 --readers 8
```

The first run covers all five query types in private, shared, and mixed pools.
The million-record fixture is constructed by bulk SQL in a **legacy test file**,
then indexed with the archived SDK. Fixture construction is not an SDK write-speed
claim. Its query set also includes a value-only frequent term and disjoint frequent
terms whose late overlap requires the full aggregation fallback.

Each query has one warm-up and 30 timed complete SDK calls; every before/after
context must agree in text, facts, messages and strategies. Three trials each
time 1,000 individual inserts and updates in separate user scopes, using the
public SDK and its normal transactions. Write verification and migration are
outside those per-operation latency samples.

The million run also starts eight **processes**, each with a separate SDK client,
alongside a shared-pool writer, synchronized by a start barrier. It compares 240
query contexts to the immutable baseline and checks 240 live values against their
reported revisions. Reader execution intervals must overlap. The writer targets
at most roughly 100 updates/second; this is not a writer-saturation benchmark.
Its scope is the same tenant/user but a separate shared pool from the private
retrieval corpus. Query timing excludes the extra live-pool validation; reported
overall throughput includes it. The benchmark uses local process IPC and may
need to run outside restrictive sandboxes.

The fixed 240-query run is a short burst. `soak_bounded.py` supplements it with
at least 60 seconds per reader, using disposable copies of the completed million
run and the same archived/current libraries. It saves every query timing and
checks each context and live revision, actual process overlap, and final database
integrity. Run it after all other loads finish. This is a closed-loop workload:
faster queries generate more traffic, so compare writer latency as well as read
throughput. It does not hold the arrival rate constant, include the aggregation
fallback in concurrent queries, or establish a long-term production SLO.

Workers snapshot the runner and record native/source hashes, individual samples,
query plans, migration timing, database/foreign-key checks and raw client results.
Build order is chronological, not randomized; p95 is a sample statistic, not a
production SLO. Schema migration can leave reusable free pages in the file.
See the [follow-up measured report](reports/2026-09-22-bounded-recall.md) for
allocated-page/file-size distinctions, fallback limits and measured tradeoffs.

## Fixed-rate pressure and competitive intersections

`benchmark_pressure.py` runs independent reader/writer processes against a new
copy of the completed million-record fixture. Reader slots are staggered at a
specified total rate; the writer has its own schedule. Missed slots are counted
and skipped instead of building an unbounded catch-up queue. Results include
service latency, reader scheduling lag, actual completions, CPU/process I/O and
the highest observed WAL file size. That size is allocated file length, not a
measurement of uncheckpointed frames. Each successful query checks the saved
context and a live shared-pool revision; final integrity checks are outside timing.

After preserving the prior native library, build the new release and run these
commands sequentially on isolated output directories:

```bash
PYTHONPATH=python/src python evals/benchmark_pressure.py \
  --native target/release/libmemweft_ffi.so \
  --output data/evals/pressure-new-auto --read-rate 200 --write-rate 50 --seconds 60
PYTHONPATH=python/src python evals/benchmark_pressure.py \
  --native target/release/libmemweft_ffi.so \
  --output data/evals/pressure-new-background --read-rate 200 --write-rate 50 \
  --seconds 60 --background-checkpoint-ms 1000
PYTHONPATH=python/src python evals/benchmark_competitive.py \
  --before data/evals/indexed-v2-before-tuning/libmemweft_ffi.so \
  --after target/release/libmemweft_ffi.so \
  --output data/evals/competitive-new-run
```

`--read-rate 0` selects closed-loop readers. `--readers 0` measures the writer
alone. `--strace` traces only the benchmark process tree's sync/write/lock syscalls
on Linux; its timing is diagnostic and must not be mixed with untraced runs.
The primary concurrent queries use the same four fast-path query types as the
previous soak, with immutable private records and a separate shared live pool.

`benchmark_competitive.py` replays all 22 prior saved contexts for private/shared/
mixed pools at 100k and private facts at one million, including the slow two-term
query. Each native build runs in a separate process on a copied v2 file; no index
migration is required. Every timed call must match the saved context. Neither
benchmark calls the model or validates model-based compression. See the
[measured report](reports/2026-09-22-pipeline-and-query.md) and
[pipeline design](../docs/async_pipeline.md).

For a five-minute WAL reclamation comparison, run the same binary twice, first
without the reclamation argument and then with it:

```bash
PYTHONPATH=python/src python evals/benchmark_pressure.py \
  --native target/release/libmemweft_ffi.so \
  --output data/evals/wal-reclaim-new-run \
  --seconds 300 --read-rate 1000 --write-rate 50 \
  --background-checkpoint-ms 1000 --monitor-maintenance \
  --pin-reader-at 60 --pin-reader-seconds 90 \
  --wal-reclaim-threshold-bytes 16777216
```

The extra read-only process holds a snapshot from second 60 to 150 and checks
that it remains stable, then sees a newer revision after release. Each client
records per-minute windows and maintenance status once per second. Status is
sampled outside the primary measured call, but its overhead affects offered slots.
WAL bytes are allocated length; counters belong to each store. The current runner
monitors readers as well as the writer because a reader can own maintenance.
Previous archived runners sampled only the writer; use the same runner for paired
comparisons. A busy truncation is deferred,
so the configured size is a soft threshold, not a cap. Do not mix performance
runs with model evaluation or other deliberate heavy loads.

## Local LangGraph Agent lifecycle replay

```bash
PYTHONPATH=python/src python evals/run_agent_lifecycle.py \
  --output data/evals/agent-lifecycle-new-run \
  --base-url http://127.0.0.1:8002/v1 --model qwen3-8b --repeats 2
```

This runs the reusable [Agent graph](../examples/local_memory_agent.py) with the
actual Python SDK and local model. It compares no-memory and memory modes across
11 stages: empty store, shared writes, updates, private override, agent isolation,
private deletion with shared fallback, shared deletion, reopening, recreation,
user isolation and tenant isolation. Complete recalled contexts must exclude
stale or foreign facts. Conversation history is not replayed, so deletion here
tests long-term memory, not erasure from a caller's retained conversation.

Learning uses the existing enterprise support split (18 train / 18 validation /
24 test cases), deterministic exemplars built from training labels, a training
regression guard, actual SDK validation/adoption, and paired baseline/adopted
test answers. Validation and test call order alternate across repeats. A source
update must invalidate its derived strategy, and source deletion must remove its
memory. An unaccepted candidate is never presented as an adopted strategy. With
two repeats, the maximum is 284 model calls; training rejection skips validation.

The output directory preserves source/suite/native hashes, exact requests and
responses in `calls.jsonl`, contexts and grades in `results.jsonl`, gate evidence,
job status and summaries. This is real framework/model integration with synthetic
business inputs, not a production rollout. The support split has been evaluated
before, so its results are replay evidence, not an independent new generalization
claim. Prompt injection, tool authorization, long conversations and automatic
conversation extraction are outside this runner's coverage.

The [WAL/Agent report](reports/2026-09-22-wal-and-agent.md) keeps both the initial
diagnostic runs and the final comparisons. `python evals/report_wal_agent.py`
checks their saved artifact hashes, completion counts, full context comparisons
and model grades before regenerating Markdown and machine-readable results.
It requires the completed local evidence directories named in the script.

## Coordinated maintenance and busy retry comparison

Run the pressure command above sequentially with an archived pre-coordination
native library and the current library, using a new output directory for each.
Keep the same runner, 300-second duration, 1000/50 read/write rates, 1000 ms
maintenance interval, 16 MiB soft threshold and the 90-second pinned reader.
`--monitor-maintenance` now samples every reader and writer once per second;
each `client-*.json` contains its own role, maintenance samples and final counters.
A reader can lead checkpointing. Do not infer database inactivity from a writer's
zero maintenance counters or treat instance counters as migrated leader totals.

The [measured comparison](reports/2026-09-22-wal-coordination.md) records latency,
missed slots, WAL size, reclamation attempts and recovery together.
`python evals/report_wal_coordination.py` regenerates it from the completed
`wal-coordination-before-run1`, `wal-coordination-after-run1` and
`wal-coordination-after-run2` evidence (including the final path-validation build),
checking archived/current native and runner hashes and all client completion
counts. It also requires both `wal-coordination-build/manifest.json` and
`wal-coordination-final-build/manifest.json`, plus the saved
verification logs. Do not remove `.memweft-maintenance` sidecars while clients
are running; clean a disposable fixture only after all its processes exit.
