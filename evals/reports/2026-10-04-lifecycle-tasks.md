# Lifecycle task pilot — 2026-10-04

Prospectively frozen v1, 30 constructed cases × 3 arms, one Kimi K2.6 call per case/arm. All arms receive the complete current facts. No user study or external-system comparison.

| Arm | Stale strategy returned | Cases retaining stale versions | Task success | Stale proposals | Guard blocks |
| --- | ---: | ---: | ---: | ---: | ---: |
| plain | 25/30 | 25/30 | 30/30 | 0 | 0 |
| versions | 0/30 | 25/30 | 30/30 | 0 | 0 |
| full | 0/30 | 0/30 | 30/30 | 0 | 0 |

## Interpretation

**No task-success improvement was observed: all three arms passed 30/30.** The mechanism reduced stale strategy exposure, but the available current facts were sufficient for this model to produce correct configurations even with stale advice present.

Compare task success separately from stale exposure. A common executor blocking a bad proposal is counted as task failure. The versions and full arms produced identical model inputs in every case; their response differences, if any, cannot establish a lifecycle quality advantage. All 5 unrelated-update controls retain valid strategies in all arms. The plain and versions arms are explicitly implemented research adapters, not external products.

The strong versions baseline suppresses stale content using direct and inherited version checks at read/adoption/rollback. It leaves stale records resident. Full uses the actual SDK lifecycle to remove affected versions and reject stale publication in the source transaction. This pilot does not establish arbitrary concurrent safety of the sequential baseline adapter.

## Model usage and timing

90 attempts, 90 completed calls; no automatic retries. 23,876 input and 1,716 output tokens. Estimated ¥0.2015 without cache discounts, using [official Kimi pricing](https://platform.kimi.com/docs/pricing/chat) checked on 2026-10-04; this is not an invoice.

| Arm | Median active read (ms) | Median HTTP completion (ms) |
| --- | ---: | ---: |
| plain | 0.0938 | 907.3 |
| versions | 0.1417 | 929.5 |
| full | 0.0507 | 874.9 |

Read measurements use tiny file-backed fixtures after reopening; they exclude source ingestion, model calls and pacing. Baselines perform Python orchestration while full runs native learning operations, so these are not isolated algorithm speedups. Per-operation times, per-domain/split outcomes, paired discordances and all raw proposals are in the accompanying evidence.

## Reproduce and inspect

The suite, protocol, runner, grader dependencies, client and native hashes were recorded before calls. The report generator replays all 90 proposals through the guard and artifact verifier, verifies equal current sources and checks actual requests against frozen inputs. No failed response was removed. API keys and authorization headers are absent from these artifacts.

```sh
PYTHONPATH=python/src .venv/bin/python evals/run_lifecycle_tasks.py prepare --output data/new-pilot
PYTHONPATH=python/src .venv/bin/python evals/run_lifecycle_tasks.py run --output data/new-pilot --prompt-key
PYTHONPATH=python/src .venv/bin/python evals/report_lifecycle_tasks.py --input data/new-pilot --stem evals/reports/new-pilot
```

A new run creates new databases and preserves prior results. Use a native SDK built from the recorded base revision; hashes identify the observed binary, not a public package release. Source changes after preparation are rejected. Unaccounted attempts prevent resume. The independently pinned external handoff pilot has not been upgraded.

## Research limits and next decision

The 12 development and 18 held-out cases share five templates; none was tuned using model outcomes. This split is not evidence of broad generalization. Complete current policy plus a strict prompt may make this task too easy to expose stale-strategy errors. No population confidence interval or statistical significance is claimed. The next protocol should test retrieval omissions, longer dependency chains and genuine contention, with its own untouched cases. Retain this result even if all three arms tie.

External comparison preparation is in [lifecycle_baseline_review.md](../../docs/lifecycle_baseline_review.md). Capabilities still need a version-pinned executable adapter; no external win is claimed.

## Engineering findings and provenance

The new CI workload exposed delayed SQLite pool worker shutdown. The initial
[CI run](https://github.com/chongliujia/memweft/actions/runs/37202385914) passed 9/12
jobs and failed the three macOS Python jobs when thread creation exhausted
resources. Commit `0df97ff` changes the scheduler drop policy to discard queued
housekeeping. The [fixed run](https://github.com/chongliujia/memweft/actions/runs/37202950265)
passed 12/12. This is a real SDK fix discovered by the expanded validation.

A macOS probe creating/closing 120 stores observed 361 threads after close with
the original module and 1 with the fixed module; both counts were unchanged after
one second. This is one descriptive sample, not a shutdown-time guarantee.
Run `evals/benchmark_pool_workers.py` in a fresh process with each native build to
repeat the measurement. An additional Rust test creates/closes 1,000 stores.

The live trial used the original native SHA recorded in `freeze`, from base
`47f71ef`; its runner and scorer match commit `26c606a`. The thread fix did not
change its already-prepared inputs. After publishing the audited outcomes, the
runner was hardened to reject negative, inconsistent or mismatched token accounting
on explicit resume and to refresh its running status. There were no resumes or
invalid usage responses in this trial, so this post-run change does not change its
outcomes. Exact source snapshots remain in the local run directory; the frozen
runner is available in Git. To audit the published traces without another API call,
load the suite and call `report_lifecycle_tasks.audit_traces(suite, traces)`.
