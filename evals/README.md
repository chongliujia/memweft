# Local model scenario evaluation

## Verified completion regression

The optional [application completion gate](../docs/verified_completion.md) requires
an explicit completion request backed by a fresh trusted check. Mutations and
failed verification revoke the in-memory receipt. Rejected completion consumes
the existing turn budget; no verification, repair, extra turn or model call is
inserted automatically.

Run its portable regression tests and replay the published October 6 replies:

```bash
.venv/bin/python -B -m unittest discover -s evals -p 'test_verified*.py' -v
.venv/bin/python -B evals/replay_verified_completion.py \
  --output data/verified-completion-new
```

The replay uses temporary project directories and **zero model calls**. Original
replies did not observe the new feedback, so accepted completion counts are not a
new model success rate. An original task may have passed the v2 final checker
while exhausting its rounds without ever requesting completion; the new session
does not treat that as an accepted completion. The frozen v2 runner, prompts,
scoring, and historical results remain unchanged.

The [October 7 replay](reports/2026-10-07-verified-completion.md) rejected all nine
historical unverified completion requests. It accepted 33 requests; another 18
historically successful tasks had verified artifacts but never requested
completion within their budget. Published JSON evidence is checked out with LF
line endings so Windows does not change the bytes used by its integrity hashes.

### Prospective live completion pilot

The [frozen pilot protocol](../docs/completion_pilot_protocol.md) pairs the same
six constructed situations across a control executor and the completion gate.
Both use the same initial prompt, files, public tools, source snapshots, model,
four rounds and three actions per round. Only completion acceptance changes.
The public verifier does not replace independent business grading. Source data
comes from fixed synthetic fixtures; this pilot does not measure online SDK
retrieval or compare memory systems.

Run the scripted regression suite without credentials:

```bash
.venv/bin/python -B -m unittest discover -s evals -p 'test_completion_pilot.py' -v
```

The protocol documents separate preparation, paid execution, local audit and
publication commands. After publication, the saved trace can also be replayed
without the original `data/` directory or any model calls:

```bash
.venv/bin/python -B evals/audit_completion_publication.py \
  evals/reports/2026-10-07-completion-live-pilot.json
```

This replay requires the trusted implementation to match the recorded source
hashes and the originating file newline conventions. A differing checkout fails
instead of silently dropping byte checks. It reconstructs each task, compares
model context, tool feedback, business grades, final artifacts and accounting,
then checks the published summary and journal hashes. It cannot authenticate
provider origin or prove that no unpublished trials exist.

The [October 7 live pilot](reports/2026-10-07-completion-live-pilot.md) recorded
4/6 correct completions in control and 6/6 in guarded, with correct file contents
in all 12 attempts. Control had one completion after its verification receipt
was invalidated and one verified task that never requested completion before its
budget ended. There were **no guarded completion refusals**, so recovery is
unknown and the group difference does not demonstrate a gate benefit. The 44
responses reported 39,144 tokens. Six first replies omitted the required `done`
field; protocol-error recovery is recorded separately from completion refusal.

## Frozen multistep file tasks with an external structured baseline

The [v2 protocol](../docs/multistep_memory_protocol.md) evaluates three constructed
executable workflows across five source lifecycle events and four arms: ordinary
memory, lazy revision checks, MemWeft lifecycle, and **Mem0 OSS 2.2.1 structured
CRUD**. The model reads project files, chooses whether to query the current source,
writes JSON files, and runs a trusted program. A separate final checker grades the
actual artifacts. These 15 situations are derived from three templates; they are
not 15 independent human tasks or an external developer study.

The [October 6 evidence overview](reports/2026-10-06-multistep-memory-overview.md)
records all 60 distinct final outcomes across two interrupted cohorts and a
completed continuation. Nine completed tasks failed the frozen requirement to
run after their last file modification, despite correct file contents. Both
unknown-cost requests and the separate successful real CLI continuation remain
visible. Each arm now covers all 15 cases; cross-day restarts and single samples
do not establish causal task-quality superiority.

Build/install the current MemWeft Python extension in the main `.venv` first. On a
fresh checkout, use a separate Python 3.11 environment for the external backend:

```bash
python3.11 -m venv data/external-lifecycle-env/venv
data/external-lifecycle-env/venv/bin/python -m pip install \
  -r evals/requirements-external-baseline.txt
export MEMWEFT_BASELINE_EMBEDDING_PATH=/absolute/path/to/all-MiniLM-L12-v2
```

The model directory must already contain the complete local
`sentence-transformers/all-MiniLM-L12-v2` model, including `pytorch_model.bin`,
tokenizer/configuration files and `1_Pooling/config.json`. Preparation runs with
Hugging Face offline mode enabled; it does not download a model or need an
embedding API key. The manifest records the files' SHA-256 values. The default
fallback is `~/.cache/torch/sentence_transformers/sentence-transformers_all-MiniLM-L12-v2`.
Use the environment variable to select an explicit local model directory.

The requirements file pins the measured main dependencies, including the
NumPy/Hub versions required by this older encoder stack. It is not a complete
transitive or cross-platform lockfile. The original macOS arm64/Python 3.11.5 run
reused preinstalled embedding libraries through a dedicated venv with
`--system-site-packages` and installed compatibility overrides only in that venv;
it did not modify the shared Python environment. The fresh isolated recipe above
avoids importing unrelated global packages. Keep an existing experiment environment
unchanged while its run is in progress; use another directory and
`--external-python /absolute/path/to/venv/bin/python` for another installation.
The recipe was also checked in a second venv with system packages disabled:
58 resolved packages installed successfully and the two actual Mem0 integration
tests passed, using the same existing local model files. That check does not
replace the original experiment's recorded environment or prove portability to
other Python versions or operating systems. The full verified version list is
[`requirements-external-baseline-macos-arm64-py311.txt`](requirements-external-baseline-macos-arm64-py311.txt);
it is platform-specific and does not include distribution hashes.

Prepare all 60 inputs offline into a **new** output directory, then run that same
frozen directory with the paid Kimi API:

```bash
PYTHONPATH=python/src .venv/bin/python evals/run_multistep_memory.py prepare \
  --output data/evals/multistep-new \
  --external-python data/external-lifecycle-env/venv/bin/python
PYTHONPATH=python/src .venv/bin/python evals/run_multistep_memory.py run \
  --output data/evals/multistep-new --prompt-key
```

`--prompt-key` reads the key through a hidden terminal prompt. Alternatively,
provide `MOONSHOT_API_KEY` through your secret manager and omit the flag; never put
the key in a command-line argument or fixture. Preparation freezes the code,
native library, public files, source snapshots, initial prompts and dependency
manifests before any API call. Changing frozen inputs or code makes `run` refuse
the experiment. An already attempted directory cannot be rerun silently.

There are at most four model rounds per task and three tool actions per round:
at most 240 responses, each capped at 1,024 output tokens. The runner stops further
calls at the reported 500,000-token threshold. Only HTTP 429 is retried, with two
recorded retries at 60-second intervals; other API failures stop and preserve
partial evidence. See the protocol for pacing, request limits and budget caveats.
Inspect or regenerate the machine-readable summary with:

```bash
PYTHONPATH=python/src .venv/bin/python evals/run_multistep_memory.py summary \
  --output data/evals/multistep-new
```

This writes `summary.json`; raw attempts, responses, tool executions, per-case
outcomes and project artifacts remain in the run directory. The summary separately
reports stale memory exposure, wrong writes, source queries, task success, usage
and latency. A missing or failed task must not be counted as successful.

Publish a complete run only after replaying every request, tool result and file:

```bash
PYTHONPATH=python/src .venv/bin/python evals/report_multistep_memory.py \
  --input data/evals/multistep-new --prefix evals/reports/multistep-new
```

The report retains the frozen primary success definition. Artifact-only success
and missing-public-run counts are explicitly labelled post-hoc diagnostics.
Published 429 records preserve request identities while omitting account identifiers;
raw local error logs and their hashes remain available for the audit.

For a run stopped by a connection error, `--allow-partial` explicitly audits its
completed prefix, interrupted task and unstarted suffix. It requires exactly one
final unanswered attempt and replays every recorded response; missing outcomes
are not scored as failed tasks. The total token cost remains unknown, alongside
the reported-token subtotal. The [interrupted October 4 run](reports/2026-10-06-multistep-memory-partial.md)
retains 41 completed outcomes, 155 responses and one unanswered attempt.

An explicit supplemental cohort can restart only the unfinished tasks in a new
directory. Its initial inputs and backend evidence come from the original frozen
run; project files and model conversations start afresh. Completed failures are
not selected again. The supplement uses 21-second request spacing and retains
the original call, tool and grading contracts:

```bash
PYTHONPATH=python/src .venv/bin/python evals/prepare_multistep_supplement.py \
  --original data/evals/multistep-new \
  --output data/evals/multistep-supplement
PYTHONPATH=python/src .venv/bin/python evals/run_multistep_memory.py run \
  --output data/evals/multistep-supplement --prompt-key
```

Preparation still requires the original runtime and native hashes to match. A
supplement is a separately declared restarted cohort, not an uninterrupted run;
keep its report and cost ledger separate. Reporting re-audits the original cohort
and verifies that every supplemental task was originally unfinished.
The [supplemental run](reports/2026-10-06-multistep-memory-supplement.md) also stopped
on a connection error. Local power logs confirm repeated clamshell sleep;
unattended execution needs a continuously awake machine. This preparation helper
does not silently chain further supplemental cohorts.

The [final continuation](reports/2026-10-06-multistep-memory-continuation.md)
completed the remaining 10 tasks, using the original frozen runner, client, SDK
sources and native binary in an isolated runtime directory. The explicit
`prepare_multistep_continuation.py` audits the ordered prior cohorts, excludes
every existing final result, and records prior partial responses and unknown
requests. Its `run_frozen.py --prompt-key` entry point executes that saved
runtime; it does not switch a measurement to the subsequently patched client.

After both trials stopped, accounting was hardened to preserve known response
usage separately from unanswered requests, and preparation gained a complete
audit before selecting unfinished tasks. The saved trial sources and outcomes
were not changed. Reports replay against saved inputs and preserve source hashes
so later transport fixes do not invalidate past evidence.

The external arm uses Mem0's actual `add(infer=False)`, `update`, `get_all`, `get`,
`delete` and `history` with local Qdrant persistence and real CPU embeddings.
`get_all` performs exact metadata filtering, followed by ID-based `get`; no
semantic search or LLM extraction is scored. JSON serialization, kind/key
addressing, source revision labels and strategy selection are application
adaptation. Native atomic derived-policy invalidation and rollback are outside
this common CRUD contract. The [baseline review](../docs/lifecycle_baseline_review.md)
records the executed compatibility fixture and these interpretation limits.

## Existing project ledger export continuation

`real_cli_export_task.py` is one separate case using the previously delivered
`decision_log.py` and the three actual public project records from 2026-10-02.
It is not part of the constructed four-arm comparison and is not a human study.
It requires the archived project and the original CPython 3.11/macOS arm64 SDK
wheel pinned to `1ee6b3b`; it refuses a silently upgraded SDK.

`prepare(output)` copies the original ledger and records hashes, source snapshots
and an independent historical receipt. `run(output, client)` accepts a bounded
Kimi client and gives the model read/write/run tools to discover records and
write an export plan. A trusted driver performs actual read-only CLI `list/get`
operations in separate processes and creates `handoff.json`; the checker verifies
all records, sources, SDK provenance and preservation of the originals.
The exported records describe that historical snapshot, not newly verified current
project status. Never run `inspect` on the untouched model workspace before the
experiment if doing so would pre-complete its discovery step.

The case allows four calls with 21-second request-start spacing and no retries.
The caller must leave at least 60 seconds after other jobs on a 3 RPM account.
A used directory cannot be rerun; preserve its attempts, responses, CLI receipts
and result. Local integration tests require the archived project; portable CI
only runs the independent contract tests.

The [October 6 completed continuation](reports/2026-10-06-real-cli-export.md)
exported all three records with four model calls and six actual CLI processes.
`report_real_cli_export.py --output RUN_DIR --report REPORT_STEM` audits saved
requests, immutable source snapshots, records, SDK files and CLI receipts. Its
published traces redact personal absolute paths. The audit does not require the
current Kimi transport source to equal the saved trial version.

## Low-cost Kimi smoke test

After installing the Python SDK, run the existing 13 `common-v3` memory cases
through the real SDK and Kimi K2.6, with and without persistent context:

```bash
python evals/run_kimi.py --check-memory  # offline, no API key
python evals/run_kimi.py --prompt-key    # hidden prompt; key is never saved
# Or export MOONSHOT_API_KEY using your secret manager and omit --prompt-key.
```

This is an opt-in paid API test: 26 calls, at most 128 output tokens each,
`thinking: disabled`, provider-default sampling, and JSON-object output with
explicit field instructions. It does not use vLLM-specific fields or automatic
retries/model fallback. Calls start at least 21 seconds apart by default, to
accommodate a 3 RPM account; a complete run takes about nine minutes. Adjust
`--interval` only to match your account limits. A 429 stops the run and preserves
partial results. Wait for the quota window before starting a new run.

The default endpoint is `https://api.moonshot.cn/v1`; select
`--base-url https://api.moonshot.ai/v1` explicitly for an international key.
The runner never automatically tries a credential on another endpoint. It uses
direct HTTPS without environment proxies, refuses redirects, and reads keys
only from the environment or hidden terminal input; `.env` is not auto-loaded.
There is no API key command-line argument, and authentication headers are not
logged. See [`examples/kimi_memory.py`](../examples/kimi_memory.py) for a reusable
short-answer client and a one-call example using explicitly saved demo facts.

Each case closes and reopens SQLite before recall. Fixtures cover preferences,
session recovery/idempotency, fact updates, forgetting, scope isolation and
recall among distractors. Expected answers are used only by the local evaluator;
they are not sent to the model. Model answers are not copied into memory.
Both modes use identical instructions and JSON contracts. Truncated answers,
extra/missing fields and incorrect types fail strict scoring.

Results, exact requests (without credentials), usage, contexts, fixture and
code/native hashes are written to a new directory under `data/evals/`. Use
`--output PATH` to choose another new directory; old runs are never overwritten.
The summary estimates CN cost from reported input/output tokens using the
2026-09-30 [published rates](https://platform.kimi.com/docs/pricing/chat)
(¥6.50 / ¥27 per million, excluding cache discounts). This is an estimate, not
a billing receipt or an enforced currency budget. International runs omit the
CNY estimate. Model parameters follow the [official K2.6 guide](https://platform.kimi.com/docs/guide/kimi-k2-6-quickstart).

These are synthetic, previously used fixtures with manually supplied facts,
one observation per mode and no learning phase. They demonstrate integration
and memory availability, not real customer benefit, automatic extraction,
statistical confidence, or a comparison of model quality.

The [2026-09-30 Kimi run](reports/2026-09-30-kimi-smoke.md) completed all 26 calls:
12/13 with memory and 2/13 without. The one memory failure is the deliberately
query-free key-order control; its target fact was omitted by `max_facts`.

For an internal workflow pilot on this repository's actual local release
evidence, use `python evals/run_kimi_release.py --prompt-key`. This makes 10 calls
with a 256-token output cap and 21-second pacing. It pairs one project planning
case and four controlled lifecycle rehearsals, without publishing anything.
See the [pilot guide](../docs/kimi_release_pilot.md) for the usable Agent CLI,
scope boundaries and what the task scores do and do not establish.

## Frozen workflow context comparison

The [20-task workflow comparison](../docs/workflow_comparison.md) pairs full
history, a query-independent deterministic current-state summary, and actual
MemWeft retrieval. All modes share the same source events, scope/forgetting
semantics, questions and output contracts. These are new synthetic tasks across
five workflow categories, frozen before observing model answers.

```bash
python evals/run_workflow_comparison.py --check-memory --output data/evals/workflow-offline-new
python evals/run_workflow_comparison.py --prompt-key --output data/evals/workflow-live-new
```

The paid run makes at most 60 Kimi calls, with 192 output tokens per call and
21-second pacing. It freezes all actual inputs before calling the provider and
preserves incorrect answers. The summary baseline uses no model, query or answer
oracle. MemWeft selects at most eight facts under a 1,200 estimated-token context
budget; the other modes preserve all visible information. Actual prompt size is
part of the comparison. See the guide for cost limits and measurement boundaries.

The [2026-09-30 measured report](reports/2026-09-30-workflow-comparison.md) records
18/20 for full history, 19/20 for the deterministic summary and 16/20 for MemWeft.
MemWeft used 58.53% fewer input tokens than full history, with lower accuracy in
this run; this is not evidence of equal-quality savings. All 60 calls cost an
estimated ¥0.340876 without cache discounts. Four MemWeft failures include two
omitted dependencies and two decisions that were incorrect despite available
facts. The report preserves baseline failures as well.

## Required-fact and workflow-guard comparison

The [required-fact guide](../docs/required_facts.md) describes a second frozen
fixture: eight new structured tasks, two recall modes and two observations per
mode, for 32 calls. Both modes share source events and the same application
rules; only `required_fact_keys` changes recall. The guard checks both sets of
raw proposals without changing answers or reading expected labels.

```bash
python evals/run_workflow_guard.py --check-memory --output data/evals/guard-offline-new
python evals/run_workflow_guard.py --prompt-key --output data/evals/guard-live-new
```

One task deliberately lacks a required fact after forgetting. The benchmark
observes raw model output before rejection, while the usable example makes no
model call when preflight fails. Reports keep 14 answerable observations per mode
as the completion denominator, separate from missing-data rejections, blocked
errors and correct answers withheld. Rejecting everything cannot count as task
completion. These typed, application-adapted inputs do not support a direct
quality comparison with the preceding free-text fixture.

The [2026-09-30 measured report](reports/2026-09-30-workflow-guard.md) records
6/14 raw and validated correct answers with lexical recall, versus 13/14 with
required facts. The one incorrect proposal with complete required inputs was
rejected by the application rule check; it remains a failed task observation.
Both modes rejected 2/2 missing-data observations, with no incorrect acceptance
or correct-answer rejection in this sample. All 32 calls cost an estimated
¥0.127440 without cache discounts. Gains concentrate in the bilingual schema
matching cases, not evidence of general semantic retrieval improvements.

## Local evaluation and storage benchmarks

For the offline long-conversation SDK benchmark, run after building the Python
extension:

```bash
python evals/benchmark_documents.py --label current \
  --output data/evals/documents-current.json
```

This uses no model service. It tests 1,000/10,000/100,000 messages plus the same
number of unrelated messages, checks the selected window, and records raw timing
samples and native/runner hashes. Run each build in a separate process with its
own `PYTHONPATH` and compare `context_sha256` before comparing latency. Seeding,
index construction and a context warmup are outside request timing. See the
[2026-09-27 comparison](reports/2026-09-27-document-hardening.md) for scope and limits.

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

## Ordered posting-block comparison

`benchmark_intersections.py` prepares a disposable SQLite fixture with the native
SDK's exact index, then measures the complete Python `context()` call. Archive
the old native library before rebuilding. Use one process per native build and
run the following commands sequentially, without other deliberate heavy loads:

```bash
PYTHONPATH=python/src python evals/benchmark_intersections.py \
  --prepare --database data/evals/intersections-new.db --facts 100000 \
  --native /path/to/old-native-library.so --label before \
  --output data/evals/intersections-before.json --samples 20
PYTHONPATH=python/src python evals/benchmark_intersections.py \
  --database data/evals/intersections-new.db \
  --native /path/to/new-native-library.so --label after \
  --output data/evals/intersections-after.json --samples 20
```

Native extension filenames vary by platform. The default fixture has four user
scopes, each containing `--facts` records plus one late two-term match:
interleaved disjoint lists, contiguous disjoint lists, one dense/one short list,
and a large overlap that must use exact aggregation. Add `--profiles balanced
--facts 1000000` to preparation for a million-record interleaved fixture. Setup,
index backfill and integrity checks are outside request timings. Each query has
one discarded warmup; all measured samples and full context hashes (including
diagnostics) are retained. Pair reports by profile/query and require identical
context, runner and database hashes. The script also checks repeated-call
equality and that querying leaves the fixture database file unchanged.

These are warm, serial local measurements, not concurrent throughput or cold-I/O
claims. See the [measured report](reports/2026-09-27-intersections.md).
This runner assumes both builds reuse the same index schema and checks that the
database file stays unchanged. For a v2→v3 upgrade, use separate copies with the
bitmap runner below instead of opening the original v2 fixture with a v3 build.

## Exact bitmap index comparison

`benchmark_bitmaps.py` compares the v2 ordered-ID implementation with v3 exact
64-ID bitmaps. Use existing v2 fixtures from `benchmark_intersections.py`; the
runner copies each fixture for each build and measures first open/upgrade/close
separately from warm query timing. It also measures private/shared inserts and
updates. Archive the old native library before rebuilding:

```bash
PYTHONPATH=python/src python evals/benchmark_bitmaps.py \
  --before /path/to/v2-native.so --after /path/to/v3-native.so \
  --source data/evals/intersections-100k.db \
  --source data/evals/intersections-million.db \
  --output data/evals/bitmap-comparison-new-run
PYTHONPATH=python/src python evals/benchmark_wal_recovery.py \
  --native /path/to/v3-native.so --source data/evals/intersections-million.db \
  --output data/evals/bitmap-pressure-new-run --mode reclaim
PYTHONPATH=python/src python evals/report_bitmap_recall.py \
  --micro data/evals/bitmap-comparison-new-run \
  --before-pressure data/evals/v2-pressure-run \
  --after-pressure data/evals/bitmap-pressure-new-run \
  --output data/evals/bitmap-report
```

The v2 pressure run must use the same source and pressure runner/options as v3;
the verifier rejects mismatched fixtures, query contexts, runner hashes or
settings. Default micro measurements use one warmup plus 20 samples per query
and 200 writes per operation. Each binary runs in a separate process. Upgrades
are performed only on disposable copies; the original fixtures remain reusable
with the older binary. Opening v3 files with v1/v2 SDKs is unsupported.

Archive manifests, binaries, scripts, raw samples and databases under `data/evals/`.
Final database size is not peak migration space; the v3 bitmap table increases
storage and trigger work even for sparse terms. See the
[measured query/space/write/concurrency report](reports/2026-09-28-bitmap-recall.md).

## Multiple writers, slow queries and idle WAL recovery

`benchmark_wal_recovery.py` complements the previous pressure runner with several
independent writers, a slow two-term query, and an idle observation phase that
keeps every SDK client open. It requires a disposable `balanced` fixture from
`benchmark_intersections.py`; a million-record example is:

```bash
PYTHONPATH=python/src python evals/benchmark_intersections.py \
  --prepare --database data/evals/recovery-source.db \
  --facts 1000000 --profiles balanced --native /path/to/native.so \
  --label recovery-fixture --output data/evals/recovery-fixture.json
PYTHONPATH=python/src python evals/benchmark_wal_recovery.py \
  --native /path/to/native.so --source data/evals/recovery-source.db \
  --output data/evals/recovery-passive --mode passive
PYTHONPATH=python/src python evals/benchmark_wal_recovery.py \
  --native /path/to/native.so --source data/evals/recovery-source.db \
  --output data/evals/recovery-reclaim --mode reclaim
PYTHONPATH=python/src python evals/report_wal_recovery.py \
  --passive data/evals/recovery-passive --reclaim data/evals/recovery-reclaim \
  --output data/evals/recovery-comparison
```

Run the two modes sequentially. Defaults are 300 seconds, four reader processes,
four writer processes, total offered rates of 100 primary queries/s and 200
writes/s, a 90-second pinned snapshot starting at second 60, and 45 seconds of
idle recovery. Both modes use a 1,000 ms background checkpoint interval; only
`reclaim` enables the 16 MiB soft reclamation threshold. Each writer owns a
different shared key, so SQLite write-lock contention is exercised without
intentional same-key CAS conflicts. The six-query cycle includes one slow
two-term query. Each successful primary query checks its complete saved Context,
then checks all writers' live generations, mirrors and revisions in a separate
snapshot. That extra validation is outside primary-query timing but consumes
load-scheduling time. Late slots are skipped; there is no catch-up queue.

The controller samples allocated WAL file length every 100 ms during load and
recovery. A drain barrier confirms all foreground work has ended before recovery;
an explicit release gate prevents SDK close-time cleanup from being counted as
background recovery. The subsequent integrity checkpoint is also outside that
window. All clients sample maintenance status during both phases. The report
verifier checks native/runner hashes, raw latencies, offered/completed slots,
monotonic revisions, persisted acknowledgements, pinned snapshots and connection
lifetimes. It accepts unreclaimed space as an outcome rather than silently
extending the run or invoking an extra checkpoint to make recovery pass.

Each run archives its native binary and runner sources alongside raw JSON and a
database copy. A failed run keeps its status and available evidence. This runner
needs local multiprocessing IPC; it makes no network or model calls. Keep database
copies, binaries and raw logs under Git-ignored `data/evals/`. Neither a single
five-minute run nor the idle phase establishes hours/days endurance or production
throughput. See the [measured follow-up](reports/2026-09-28-wal-recovery.md).

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

## Frozen access holdouts and sandbox tools

This follow-up runs a real LangGraph graph and the Python SDK against local Qwen,
with a deterministic tool executor that writes **only disposable SQLite grant
and audit rows**. It never changes real accounts, IAM permissions or host settings.
Build/install the Python native extension and install `python/requirements-test.txt`
first. Run on the host exposing the model endpoint:

```bash
PYTHONPATH=python/src python -m unittest discover -s evals -p 'test_*.py' -v
PYTHONPATH=python/src python evals/run_access_holdout.py \
  --output data/evals/access-holdout-tools-new-run \
  --base-url http://127.0.0.1:8002/v1 --model qwen3-8b --repeats 2
PYTHONPATH=python/src python evals/report_access_holdout.py \
  --run data/evals/access-holdout-tools-new-run \
  --output data/evals/access-holdout-tools-new-report
```

The Linux CI job also runs these offline evaluation tests without a model server.
Model-call evaluations remain explicit local runs.

A run requires a new output directory. The API key defaults to `EMPTY`; strict
JSON Schema and Qwen request options follow the existing runner. Default sampling
is temperature 0.2, seeds 42/43, maximum 256 output tokens. Modes rotate their
execution order by case and repeat. Model/provider errors preserve a failed run;
there is no response rewriting, automatic retry or regeneration to pass a test.

The [frozen strategy](fixtures/access-strategy-frozen-v1.json) is derived only
from the original access task's 18 training examples and includes its content
hash and training-suite hash. The SDK evaluates it against the **old** validation
split (18 cases × 2 repeats × baseline/candidate = 72 calls), requiring positive
gain and no per-case regression. The `learned` mode includes it only when that
actual job is accepted. A rejection leaves the baseline active and remains a
valid evaluation outcome. Candidate cost is measured in thousands of model
reported tokens; the gate's 10,000-unit ceiling is an evaluation budget, not money.

The new [fixture](scenarios/access-holdout-tools-v1.json) contains:

- **30 fresh same-domain boundary cases**, each run in no-memory, memory and
  accepted-learning modes, twice: 180 calls. These cover approval reference,
  revocation, expiration, conditional approval, urgency, English and missing
  evidence. None of these prompts are in the old train/validation/test splits;
  they never reach the proposal builder or acceptance gate.
- **40 tool scenarios**, also in three modes and two repeats: 240 calls. These
  include 12 attacks in each of persistent memory and caller-managed history,
  clean allow/deny controls, availability attacks, and execution boundaries.
  The same history is provided in every mode; no-memory mode does not receive
  memory-injected payloads and is not counted as exposed for those attacks.

Total: **492 model calls** for one complete run. Repeated cases and attack
paraphrases are correlated, not independent samples. This is an evaluator-authored
synthetic holdout, not an external secret benchmark. Freeze the strategy and
fixtures before execution; any later tuning requires a new test version.

The [sandbox Agent](../examples/sandbox_access_agent.py) uses
recall → decide → execute. The model emits a proposed request ID, resource class
and action. Its output cannot supply identity, approver roles or approvals.
Trusted caller context binds tenant/user/agent/current request; the executor
checks its own request/approval tables inside the same write transaction as the
grant and audit insert. It rejects wrong routes, resource mismatches, missing,
revoked, expired or stale-revision approvals, and non-executor agents. An approval
revoked after the model response is checked again at execution time. Every action
is retried once to test request-level idempotency; a retry still reauthorizes.
Approved decoy requests in another route, user and tenant must remain untouched.

The tool database is separate from MemWeft. Its setup helpers are trusted fixture
APIs, not model tools. This example uses a fixed virtual approval time of `1000`,
local SQLite WAL/NORMAL, and one serial caller; it does not implement production
identity authentication, distributed transactions, real approval issuance,
role management or power-loss durability. A recorded grant is a simulated effect,
not evidence that a real IAM integration is secured.

Results separate model correctness, protocol validity, unsafe proposals,
unauthorized **effects**, and authorized-operation completion. The scoring oracle
uses fixture labels and persisted grant rows, not the executor's reported verdict.
A deny-all executor would fail legitimate-operation checks. In the late-revocation
case, a model may correctly propose execution based on its earlier snapshot;
that suggestion is unauthorized at commit time and must still be blocked.

Artifacts include source/fixture snapshots, strategy/native hashes, raw model
requests/responses, recalled contexts, gate evidence, model proposals, and a
separate audited sandbox database per observation. The report generator rechecks
scores, call coverage, seeds, recorded effects, audit entries and database integrity.
It supports `--interrupted-run PATH` for the first recorded checker failure in this
experiment. The initial run stopped at call 272 because the exposure assertion
compared raw newlines/quotes against JSON-escaped context text. The corrected
checker verifies both the selected fact value and its quoted representation;
strategy, fixtures and prompts were unchanged. First-exposure blind results are
retained separately; a full rerun is not another independent blind test.

See the [measured report](reports/2026-09-22-access-holdout-tools.md). To regenerate
the recorded comparison, use `--run data/evals/access-holdout-tools-v1-run2`
and `--interrupted-run data/evals/access-holdout-tools-v1-run1` with the report
script. Prior history-injection failures remain historical evidence: the new graph
uses explicit trusted approval snapshots and quoted history, so comparisons do
not isolate a change in model behavior or prove that memory storage alone solves
prompt injection.

## Reference boundary comparison

The optional Python [reference projection](../docs/reference_boundaries.md)
compares three profiles in the same sandbox access graph:

- `baseline`: previous memory text/history framing and original snapshot order.
- `quoted`: separate JSON strategy/fact/history references, an explicit boundary
  reminder, and the live approval snapshot placed after references.
- `restricted`: the same framing as quoted, with only application-allowed finite
  fact values and exact strategy-content pins; other memory/history is omitted.

The executor, frozen learned strategy, truth labels and sampling parameters are
unchanged. This is a combined framing/ordering comparison; it does not isolate the
causal effect of a particular delimiter. Restricted inputs reduce exposure, not
teach the model to resist content that it never receives. Current question text
remains exposed, including new direct-question attack controls.

```bash
PYTHONPATH=python/src python -m unittest discover -s python/tests -p test_references.py -v
PYTHONPATH=python/src python -m unittest discover -s evals -p 'test_*.py' -v
PYTHONPATH=python/src python evals/run_reference_boundary.py \
  --output data/evals/reference-boundary-new-run --repeats 2 \
  --base-url http://127.0.0.1:8002/v1 --model qwen3-8b
PYTHONPATH=python/src python evals/report_reference_boundary.py \
  --run data/evals/reference-boundary-new-run \
  --output data/evals/reference-boundary-new-report
```

The [frozen fixture](scenarios/reference-boundary-v1.json) has 40 old regression
scenarios and 32 new synthetic scenarios. Each is tested in three profiles,
with memory and with adopted learning, twice: 864 model/tool observations.
Rechecking the old validation/adoption gate takes 72 calls. Six separate language
preference tasks × three profiles × two repeats add 36 calls, verifying that valid
allowed memory remains usable and missing/invalid values are not invented.
Total: **972 calls**. Sampling is temperature 0.2, seed 42/43; profiles/modes rotate
by case and repeat. Strategies and prompts are not tuned after test results.

The no-memory comparison remains in the prior access/tool experiment. This round
uses two memory modes to compare input policies while retaining the same stored
memory and learning state. Old cases are regressions, not blind evidence; new cases
are evaluator-authored synthetic probes, not external secret benchmarks.

Every observation preserves selected context, exact model-facing projection,
omission counts, actual model proposal, execution result, grant rows and audit.
The report verifier checks raw call coverage, seeds, regenerated projections,
actual sent reference messages, model scores and every sandbox database. It
separates exposed from excluded attacks and retains failures, including direct
question attacks outside the projection boundary. There is no model-answer repair.

See the [measured report](reports/2026-09-22-reference-boundary.md). The published
run is `data/evals/reference-boundary-v1-run1`; raw databases and model logs stay
Git-ignored. This helper currently targets Python only and is explicitly opt-in;
it does not change native storage, default context text or the Node API.

## Confirmed command comparison

The [application-confirmed command example](../docs/confirmed_commands.md) adds
pending/confirmed/canceled command state bound to tenant, user, agent, request,
revision, action, expiry and the SHA-256 of submitted material. Confirmation is
supplied by the trusted application, never inferred from model output or chat.

Three profiles share the new command-aware executor and sanitized snapshot:

- `previous_restricted`: previous reference filtering with raw current question.
- `rules_with_text`: explicit business rules and fixed recall query; quoted input.
- `bound_command`: same rules and query; raw input omitted, binding checked.

The frozen fixture contains 72 adapted regression cases and 28 new cases.
Old cases now have explicit application confirmation fixtures, so historical
totals are not directly comparable. Two memory modes × three profiles × two
repeats produce 1,200 tool observations, plus 72 old validation calls: **1,272
model calls**. New cases cover pending/fake confirmation, inspection, cancellation,
expiry, changed material, changed revisions, foreign identities/routes, late
cancellation/revocation and valid/missing/expired role approvals. There is no
answer repair and no prompt tuning after observing this fixture's results.

```bash
PYTHONPATH=python/src python -m unittest discover -s evals -p 'test_*.py' -v
PYTHONPATH=python/src python evals/run_confirmed_command.py \
  --output data/evals/confirmed-command-new-run --repeats 2 \
  --base-url http://127.0.0.1:8002/v1 --model qwen3-8b
PYTHONPATH=python/src python evals/report_confirmed_command.py \
  --run data/evals/confirmed-command-new-run \
  --output data/evals/confirmed-command-new-report
```

Use a fresh output directory. Results include raw proposals, snapshot and reference
messages, independent labels, input hashes, command audits, grants and retry
outcomes. The report reconciles every raw call and database against recorded
evidence. The bound path excludes raw submissions; this does not prove resistance
to content the model never receives. Synthetic serial fixtures do not validate a
real confirmation UI, authentication, IAM integration or concurrent external tools.

See the [measured report](reports/2026-09-22-confirmed-command.md). The published
run is `data/evals/confirmed-command-v1-run1`; source and fixture hashes remain in
the report, while raw model logs and databases stay Git-ignored.

## Learning source lookup and invalidation

The offline Rust runner isolates source lookup from unrelated learning-document
scans. Build it in release mode and give each invocation a **new** directory:

```bash
cargo +1.89.0 build --release --locked -p memweft \
  --example benchmark_learning_sources --example benchmark_learning_open
mkdir -p data/learning-source-run
target/release/examples/benchmark_learning_sources \
  data/learning-source-run/indexed-1000-8 1000 20 8
```

Arguments are directory, facts per private/shared pool, timed repeats, and affected
document count. The recorded matrix uses `(1000,8)`, `(10000,8)`, `(100000,8)` and
`(10000,1000)`, with 20 samples after one warmup. It seeds the same number of
unrelated learning documents as facts per pool. Source updates include commit and
index maintenance; resetting dependents and checking outcomes are outside timings.
The generated learning documents are storage fixtures, not model-generated strategies.

For a paired comparison, build the identical example source against baseline
`9029727` in an isolated checkout, archive the executable, then build the current
runtime. Keep binaries as `baseline`/`indexed`, the shared source as `runner.rs`,
and invocation directories as `BUILD-COUNT-FANOUT`. Each `BUILD-build.json` records
`binary_sha256` and `runner_sha256`; the baseline manifest also records its commit.
Run builds sequentially, and retain all `result.json` samples and databases.

```bash
python evals/report_learning_sources.py --input data/learning-source-run \
  --output data/learning-source-run/report.json \
  --upgrade-probe target/release/examples/benchmark_learning_open
```

The validator recomputes quantiles, checks timestamp-normalized authoritative-row
equality and SQLite integrity, and measures startup/backfill on a **backup** of
the baseline 100k fixture. It refuses to overwrite an existing migration run.
Omit `--upgrade-probe` to verify existing migration evidence without rerunning it.
First-open timing includes SDK initialization, excludes close; final disk sizes
exclude temporary/WAL peaks. This is a warm single-client experiment, not a soak
or model-quality evaluation. [Measured results](reports/2026-10-04-learning-sources.md)
and [consistency assumptions](../docs/learning_lifecycle.md).

## README performance figures

The versioned SVG/PNG figures read the measured WAL coordination and query
comparison JSON reports. Regenerate them without rerunning benchmarks or calling
a model:

```bash
python -m pip install matplotlib
python evals/plot_readme_metrics.py
```

Matplotlib is only needed to regenerate these figures, not to use MemWeft.
The local render used Matplotlib 3.10.8. Keep workload conditions, slow paths and
WAL-space limitations alongside any headline latency numbers.

## Frozen lifecycle task pilot

The 30-case, three-arm pilot compares plain persisted strategies, lazy checks of
direct/inherited source versions, and the actual SDK lifecycle. It uses shared
source events, real file-backed stores, scripted interleavings, and a common
configuration-artifact executor. The independent oracle and the executor are
scored separately from stale memory exposure. These are constructed tasks, not
an external developer study.

See [the protocol](../docs/lifecycle_task_protocol.md),
[external baseline review](../docs/lifecycle_baseline_review.md), and
[the report](reports/2026-10-04-lifecycle-tasks.md).

```sh
PYTHONPATH=python/src .venv/bin/python evals/run_lifecycle_tasks.py prepare --output data/new-pilot
PYTHONPATH=python/src .venv/bin/python evals/run_lifecycle_tasks.py run --output data/new-pilot --prompt-key
PYTHONPATH=python/src .venv/bin/python evals/report_lifecycle_tasks.py --input data/new-pilot --stem evals/reports/new-pilot
```

Preparation freezes source/native/input hashes before any network call. The live
run permits 90 attempts, 256 output tokens per call, no automatic retries, and
stops subsequent calls at 150,000 reported tokens or an error. It uses the
existing Kimi K2.6 client. `--prompt-key` reads a key without echo or saving;
otherwise use `MOONSHOT_API_KEY`. Keep original run directories.
The reporter requires all 90 results and replays the saved proposals; incomplete
runs remain explicitly incomplete and cannot produce a complete report.
