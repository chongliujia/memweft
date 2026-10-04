# Lifecycle task pilot v1

This is a prospective, bounded mechanism pilot, not a user study or an external
system leaderboard. Freeze this protocol, the 30-case suite, runner, scorer,
client, wrapper and native module hashes before any paid call. Preserve every
attempt. Do not tune prompts or cases after inspecting model outcomes.

## Question and conditions

Does source lifecycle handling reduce stale strategy exposure, incorrect model
proposals, or failed configuration tasks when every arm also receives the same
complete, current source facts? A null task-quality result is useful evidence.

| Arm | Source storage | Strategy handling |
| --- | --- | --- |
| plain | Actual SDK shared-pool facts, including monotonic revisions | Persist strategies and inherited dependency snapshots, but do not enforce source validity |
| versions | Same | Check all direct AND inherited revisions at read, adoption and rollback; stale records remain stored but are suppressed |
| full | Same | Actual SDK learning start/submit/active/rollback, transactional source invalidation and inherited guards |

The first two arms are explicit research adapters over SDK generic documents,
not old product releases or representations of third-party systems. They use the
same scripted publication evidence and source events. The versions arm deliberately
includes a strong lazy validity check; do not manufacture a full-arm advantage by
omitting inherited dependencies. The adapter checks are sequential and are NOT an
atomic implementation suitable for arbitrary concurrent writers. This experiment
uses a fixed start/update/submit ordering with a second SDK connection, not a race
stress test. Only shared sources are covered; private scope-generation tradeoffs
need a separate experiment.

## Tasks and split

`evals/scenarios/lifecycle-tasks-v1.json` contains five configuration templates:
Python onboarding, deployment, backup export, developer handoff and CI runtime.
Each has six events: source update, forget, delete/recreate, update between start
and submit, stale rollback after fresh adoption, and unrelated-source update.
Every schedule first creates a strategy and then a descendant with an additional
source, exercising inherited dependencies. Expected output is fixed in the suite.

The first two domains (12 cases) are development; the other three (18 cases) are
held out from model-driven tuning. All are authored together and share templates,
so this is not an independent dataset generalization claim. Offline harness checks
may inspect all cases. No model output from either split may change this v1 run.
A future revised protocol requires a new version and an untouched test set.

The model emits a flat JSON configuration proposal. The common executor compares
it to current application requirements, writes a file only when valid, then reads
it back and checks against the frozen answer. Withdrawal requires a defer decision
with all configuration fields null and no file. A blocked wrong proposal is a
failed task, not a success. No generated shell commands are executed. These are
small constructed artifact tasks, not full repository modifications or deployments.

## Equal inputs, budget, and measurements

Every arm sees the same task, JSON contract, complete current source facts and
system rules. Only the available strategy differs. No oracle or evaluation label
is supplied to context construction. Canonical strategy text is rendered without
arm labels. Current facts take precedence over strategies. This deliberately tests
whether lifecycle handling adds value when fresh facts are available; it does not
simulate forgotten facts copied into arbitrary messages or omitted retrieval.

Use existing `kimi-k2.6`, thinking disabled, provider sampling defaults, one call
per case/arm: 90 scheduled calls, maximum 256 output tokens each, no automatic
retries. Request cap: 16,000 UTF-8 bytes, stop before a subsequent call if cumulative
reported tokens reach 150,000. Rotate arm order by case; shuffle case order with
fixed seed 20261004. Calls are sequential, paced at least 21 seconds between starts (compatible with a 3 RPM account).
A rate limit or transport error is recorded and stops the run; resume is explicit,
never silently retried, and is refused when an earlier attempt has unknown usage. Missing usage also stops rather than estimating it as zero.
Token thresholds are not a hard currency ceiling. Current pricing must be verified
before monetary estimates; token counts remain authoritative usage evidence.

Report separately: stale strategy returned, stale strategy still resident, blocked
adoption/rollback, valid control retention, protocol-invalid responses, stale-valued
model proposals, other wrong proposals, execution blocks, artifact/defer success,
API errors, prompt/output tokens, and latency. Ingestion/event handling/read latency
exclude model time and pacing; single tiny fixtures are not throughput benchmarks.
Common guard blocks must never conceal proposal errors. Do not treat 30 templated
cases as independent human tasks or repeated calls as independent samples. Give
case-level and domain/split breakdowns, paired discordances and descriptive rates;
no significance or population confidence claim from this pilot.

## Interpretation and next comparison

Equal success with less stale exposure supports the narrower lifecycle guarantee,
not a claim of higher task accuracy. Equal versions/full results may favor a simpler
lazy design where its operational limits are acceptable. Resident stale records
are not model exposure. Additional write overhead and false conflicts count against
full lifecycle. Frozen evaluator scores used to seed strategies are fixtures,
not evidence that a model learned them.

Only after this pilot, examine an external system's documented update/deletion,
provenance and derived-policy behavior. Pin its version and disclose adaptation,
extraction/model calls and total cost. Do not label unsupported operations as
system failures or retrofit the same adapter and call it a native capability.
