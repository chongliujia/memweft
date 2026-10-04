#!/usr/bin/env python3
"""Audit and publish a complete frozen pilot; never fill missing model outcomes."""
import argparse
import json
from pathlib import Path
import tempfile

from run_lifecycle_tasks import (ARMS, MODEL, ROOT, dump, execute_and_grade, sha,
                                 summarize, valid, verify_freeze)

GRADE_FIELDS = ('protocol_error', 'proposal', 'stale_proposal', 'wrong_proposal', 'execution_blocked', 'task_success')


def read_rows(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def audit_traces(suite, traces):
    cases = {c['id']: c for c in suite['cases']}
    wanted = {(c, arm) for c in cases for arm in ARMS}
    keys = [(t['input']['case_id'], t['input']['arm']) for t in traces]
    if set(keys) != wanted or len(keys) != len(wanted):
        raise ValueError('Incomplete or duplicated case/arm coverage')
    by_key = dict(zip(keys, traces))
    with tempfile.TemporaryDirectory() as temp:
        for key, trace in by_key.items():
            row, result = trace['input'], trace['result']
            case = cases[key[0]]
            if (result['case_id'], result['arm']) != key:
                raise ValueError('Mismatched result identity')
            for field in ('split', 'event', 'domain'):
                if row[field] != case[field] or result[field] != case[field]:
                    raise ValueError('Mismatched case grouping')
            response = result['response']
            request = response['request']
            if (request['messages'] != row['messages'] or request['model'] != MODEL or response['model'] != MODEL
                    or request['max_completion_tokens'] != 256 or request['thinking'] != {'type': 'disabled'}):
                raise ValueError('Actual request differs from frozen input/model/budget')
            usage = response['usage']
            if any(type(usage.get(k)) is not int or usage[k] < 0 for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')):
                raise ValueError('Missing usage')
            if usage['total_tokens'] != usage['prompt_tokens'] + usage['completion_tokens']:
                raise ValueError('Inconsistent usage')
            source = row['current_sources'].get('policy')
            expected = {'action': 'defer', **{k: None for k in case['old']}} if source is None else {'action': 'write', **source['value']}
            if expected != case['expected']:
                raise ValueError('Fixture state disagrees with independent answer')
            if row['stale_returned'] != (row['active'] is not None and not valid(row['active'], row['current_sources'])):
                raise ValueError('Incorrect stale exposure label')
            grade = execute_and_grade(case, row, response, Path(temp) / key[0] / key[1])
            if any(grade[field] != result[field] for field in GRADE_FIELDS):
                raise ValueError('Saved model grade does not reproduce')
        for case in cases:
            rows = [by_key[case, arm]['input'] for arm in ARMS]
            if not all(r['current_sources'] == rows[0]['current_sources'] for r in rows):
                raise ValueError('Arms received different current sources')
            if rows[1]['messages'] != rows[2]['messages']:
                raise ValueError('Unexpected lazy/full context difference in v1')
    return {'audited_traces': len(traces), 'artifact_verifier_replayed': True,
            'equal_current_sources': True, 'versions_full_inputs_identical': True}


def publish(output, stem):
    freeze = verify_freeze(output)
    status = json.loads((output / 'status.json').read_text(encoding='utf-8'))
    if status['status'] != 'completed' or (output / 'errors.jsonl').exists():
        raise ValueError('Only complete, error-free runs may use this report template')
    inputs, results, attempts = [read_rows(output / name) for name in ('inputs.jsonl', 'results.jsonl', 'attempts.jsonl')]
    if len(attempts) != 90 or len({(a['case_id'], a['arm']) for a in attempts}) != 90:
        raise ValueError('Attempt accounting is incomplete')
    indexed = {(r['case_id'], r['arm']): r for r in results}
    if len(indexed) != len(results):
        raise ValueError('Duplicate result')
    traces = [{'input': row, 'result': indexed[row['case_id'], row['arm']]} for row in inputs]
    suite = json.loads((output / 'suite.json').read_text(encoding='utf-8'))
    audit = audit_traces(suite, traces)
    summary = summarize(output)
    prompt = sum(g['usage']['prompt_tokens'] for g in summary['by_arm'].values())
    completion = sum(g['usage']['completion_tokens'] for g in summary['by_arm'].values())
    price = {'checked': '2026-10-04', 'source': 'https://platform.kimi.com/docs/pricing/chat',
             'cny_per_million_uncached_input': 6.5, 'cny_per_million_output': 27,
             'estimated_cny_without_cache_discount': round((prompt * 6.5 + completion * 27) / 1e6, 6),
             'not_billing_statement': True}
    trace_path = stem.with_suffix('.traces.jsonl')
    trace_path.write_text(''.join(json.dumps(t, ensure_ascii=False, separators=(',', ':')) + '\n' for t in traces), encoding='utf-8')
    evidence = {'freeze': freeze, 'summary': summary, 'audit': audit, 'pricing': price,
                'attempts': attempts, 'trace_sha256': sha(trace_path),
                'reporter_sha256': sha(Path(__file__)),
                'limits': ['constructed configuration tasks', '5 templates, not 30 independent domains',
                           'one model, one sample per case/arm', 'no external participants or system baseline',
                           'complete current facts; no retrieval/extraction challenge',
                           'scripted interleavings, not concurrent race or throughput test']}
    dump(stem.with_suffix('.json'), evidence)
    lines = ['# Lifecycle task pilot — 2026-10-04', '',
        'Prospectively frozen v1, 30 constructed cases × 3 arms, one Kimi K2.6 call per case/arm. '
        'All arms receive the complete current facts. No user study or external-system comparison.', '',
        '| Arm | Stale strategy returned | Cases retaining stale versions | Task success | Stale proposals | Guard blocks |',
        '| --- | ---: | ---: | ---: | ---: | ---: |']
    for arm, g in summary['by_arm'].items():
        lines.append(f"| {arm} | {g['stale_returned']}/30 | {g['cases_with_resident_stale_versions']}/30 | {g['task_success']}/{g['calls']} | {g['stale_proposal']} | {g['execution_blocked']} |")
    lines += ['', '## Interpretation', '',
        'Compare task success separately from stale exposure. A common executor blocking a bad proposal '
        'is counted as task failure. The versions and full arms produced identical model inputs in every case; '
        'their response differences, if any, cannot establish a lifecycle quality advantage. '
        'All 5 unrelated-update controls retain valid strategies in all arms. '
        'The plain and versions arms are explicitly implemented research adapters, not external products.', '',
        'The strong versions baseline suppresses stale content using direct and inherited version checks at '
        'read/adoption/rollback. It leaves stale records resident. Full uses the actual SDK lifecycle to '
        'remove affected versions and reject stale publication in the source transaction. '
        'This pilot does not establish arbitrary concurrent safety of the sequential baseline adapter.', '',
        '## Model usage and timing', '',
        f"90 attempts, {summary['completed_calls']} completed calls; no automatic retries. "
        f"{prompt:,} input and {completion:,} output tokens. Estimated ¥{price['estimated_cny_without_cache_discount']:.4f} "
        'without cache discounts, using [official Kimi pricing](https://platform.kimi.com/docs/pricing/chat) '
        'checked on 2026-10-04; this is not an invoice.', '',
        '| Arm | Median active read (ms) | Median HTTP completion (ms) |', '| --- | ---: | ---: |']
    for arm, g in summary['by_arm'].items():
        lines.append(f"| {arm} | {g['read_median_ms']:.4f} | {g['model_median_ms']:.1f} |")
    lines += ['', 'Read measurements use tiny file-backed fixtures after reopening; they exclude source ingestion, '
        'model calls and pacing. Baselines perform Python orchestration while full runs native learning operations, '
        'so these are not isolated algorithm speedups. Per-operation times, per-domain/split outcomes, '
        'paired discordances and all raw proposals are in the accompanying evidence.', '',
        '## Reproduce and inspect', '',
        'The suite, protocol, runner, grader dependencies, client and native hashes were recorded before calls. '
        'The report generator replays all 90 proposals through the guard and artifact verifier, verifies '
        'equal current sources and checks actual requests against frozen inputs. '
        'No failed response was removed. API keys and authorization headers are absent from these artifacts.', '',
        '```sh', 'PYTHONPATH=python/src .venv/bin/python evals/run_lifecycle_tasks.py prepare --output data/new-pilot',
        'PYTHONPATH=python/src .venv/bin/python evals/run_lifecycle_tasks.py run --output data/new-pilot --prompt-key',
        'PYTHONPATH=python/src .venv/bin/python evals/report_lifecycle_tasks.py --input data/new-pilot --stem evals/reports/new-pilot', '```', '',
        'A new run creates new databases and preserves prior results. Use a native SDK built from the recorded '
        'base revision; hashes identify the observed binary, not a public package release. Source changes '
        'after preparation are rejected. Unaccounted attempts prevent resume. The independently pinned '
        'external handoff pilot has not been upgraded.', '',
        '## Research limits and next decision', '',
        'The 12 development and 18 held-out cases share five templates; none was tuned using model outcomes. '
        'This split is not evidence of broad generalization. Complete current policy plus a strict prompt '
        'may make this task too easy to expose stale-strategy errors. No population confidence interval '
        'or statistical significance is claimed. The next protocol should test retrieval omissions, '
        'longer dependency chains and genuine contention, with its own untouched cases. '
        'Retain this result even if all three arms tie.', '',
        'External comparison preparation is in [lifecycle_baseline_review.md](../../docs/lifecycle_baseline_review.md). '
        'Capabilities still need a version-pinned executable adapter; no external win is claimed.', '']
    stem.with_suffix('.md').write_text('\n'.join(lines), encoding='utf-8')
    return {'report': str(stem.with_suffix('.md')), **audit, 'estimated_cny': price['estimated_cny_without_cache_discount']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--stem', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(publish(args.input, args.stem), indent=2))


if __name__ == '__main__':
    main()
