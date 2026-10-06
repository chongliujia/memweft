#!/usr/bin/env python3
"""Prepare a separately disclosed offline cohort after an interrupted frozen run.

Completed tasks are never selected, regardless of their scores. Selected input
lines and backend preparation evidence are copied unchanged; projects restart
from create_fixture, with no previous model messages or output files. This script
does not construct a client, read credentials, or make network requests.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import shutil

import report_multistep_memory
from run_multistep_memory import (ROOT, accounted_usage, create_fixture, dump,
                                  now, read_rows, sha, verify_freeze)


def task_id(row):
    return row['case_id'], row['arm']


def identity(row):
    return {key: row[key] for key in ('case_id', 'arm')}


def attempt_id(row):
    return (*task_id(row), row['round'], row['attempt'])


def tree_hashes(root):
    result = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('Evidence must not contain symlinks: ' + str(path))
        if path.is_file():
            result[path.relative_to(root).as_posix()] = sha(path)
    return result


def ledger(original, selected):
    attempts = read_rows(original / 'attempts.jsonl')
    responses = read_rows(original / 'responses.jsonl')
    limits = read_rows(original / 'rate_limits.jsonl')
    attempts_by_id = {attempt_id(row): row for row in attempts}
    response_ids = {attempt_id(row) for row in responses}
    limit_ids = {attempt_id(row) for row in limits}
    if (len(attempts_by_id) != len(attempts) or len(response_ids) != len(responses)
            or len(limit_ids) != len(limits) or response_ids & limit_ids
            or not (response_ids | limit_ids) <= set(attempts_by_id)):
        raise ValueError('Original request ledger has duplicate or unmatched identities')
    unanswered = [row for row in attempts if attempt_id(row) not in response_ids | limit_ids]
    known = [row for row in responses if accounted_usage(row['response'])]
    partial = [row for row in responses if task_id(row) in selected]
    token_keys = ('prompt_tokens', 'completion_tokens', 'total_tokens')
    return {
        'original_attempts': len(attempts), 'original_recorded_responses': len(responses),
        'original_recorded_429': len(limits), 'original_unanswered_attempts': unanswered,
        'unknown_billing_attempts': len(unanswered),
        'original_known_usage_responses': len(known),
        'original_responses_with_unknown_usage': len(responses) - len(known),
        'original_known_usage': {key: sum(row['response']['usage'][key] for row in known) for key in token_keys},
        'billing_complete': not unanswered and len(known) == len(responses),
        'partial_responses': partial,
        'partial_valid_responses': sum(accounted_usage(row['response']) for row in partial),
        'partial_known_usage': {key: sum(row['response']['usage'][key] for row in partial
                                       if accounted_usage(row['response'])) for key in token_keys},
        'interpretation': ('Original partial responses remain original-run cost and evidence; they are not '
                           'supplement responses, are not resumed, and cannot be counted twice as task outcomes. '
                           'An attempt without a recorded response or explicit 429 has unknown billing, not zero cost.'),
    }


def prepare_supplement(original, output, *, expected_unfinished=None):
    original, output = Path(original).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError('Supplement destination must be new')
    if output.is_relative_to(original) or original.is_relative_to(output):
        raise ValueError('Original and supplement directories must be disjoint')
    original_manifest = verify_freeze(original)
    status = json.loads((original / 'status.json').read_text(encoding='utf-8'))
    if status.get('status') != 'stopped_error':
        raise ValueError('Only a stopped_error run may supply this supplement')
    before = tree_hashes(original)
    # A stable snapshot alone cannot establish that the preexisting ledger is
    # internally consistent. Replay it before deciding which tasks lack results.
    auditor = Path(report_multistep_memory.__file__).resolve()
    auditor_bytes = auditor.read_bytes()
    original_audit = report_multistep_memory.audit(original, allow_partial=True)
    if auditor.read_bytes() != auditor_bytes:
        raise ValueError('Original-run auditor changed during replay')
    rows = read_rows(original / 'inputs.jsonl')
    results = read_rows(original / 'results.jsonl')
    all_ids, done = {task_id(row) for row in rows}, {task_id(row) for row in results}
    if len(all_ids) != len(rows) or len(done) != len(results) or not done <= all_ids:
        raise ValueError('Original task identities are duplicate or inconsistent')
    selected = [row for row in rows if task_id(row) not in done]
    if not selected or (expected_unfinished is not None and len(selected) != expected_unfinished):
        raise ValueError('Unexpected unfinished task count')
    selected_ids = {task_id(row) for row in selected}
    accounting = ledger(original, selected_ids)
    suite = {case['id']: case for case in json.loads((original / 'suite.json').read_text(encoding='utf-8'))['cases']}
    output.mkdir(parents=True, exist_ok=False)
    dump(output / 'original-audit.json', original_audit)
    (output / 'original-audit-reporter.py').write_bytes(auditor_bytes)
    shutil.copytree(original / 'sources', output / 'sources')
    shutil.copy2(original / 'suite.json', output / 'suite.json')
    # Preserve exact original JSONL bytes, including ordering and serialization.
    lines = (original / 'inputs.jsonl').read_bytes().splitlines(keepends=True)
    if len(lines) != len(rows):
        raise ValueError('Input row count changed while preparing')
    (output / 'inputs.jsonl').write_bytes(b''.join(line for line, row in zip(lines, rows)
                                                if task_id(row) in selected_ids))
    backend_hashes = {}
    for row in selected:
        case_id, arm = task_id(row)
        if Path(case_id).name != case_id or Path(arm).name != arm or case_id not in suite:
            raise ValueError('Invalid frozen task identity')
        old_task, new_task = (root / 'tasks' / case_id / arm for root in (original, output))
        new_task.mkdir(parents=True)
        # Backends are used during original preparation only; the frozen runner
        # consumes row.active/current_sources. Keep their evidence byte-identical.
        for child in old_task.iterdir():
            if child.name == 'project':
                continue
            destination = new_task / child.name
            if child.is_dir():
                shutil.copytree(child, destination)
            else:
                shutil.copy2(child, destination)
        files = create_fixture(new_task / 'project', suite[case_id])
        if files != row['files']:
            raise ValueError('Fresh fixture permissions differ from frozen input')
        actual = tree_hashes(new_task / 'project')
        if actual != row['initial_file_sha256']:
            raise ValueError('Fresh fixture differs from original create state')
        copied = {name: digest for name, digest in tree_hashes(new_task).items()
                  if not name.startswith('project/')}
        expected = {name: digest for name, digest in tree_hashes(old_task).items()
                    if not name.startswith('project/')}
        if copied != expected:
            raise ValueError('Backend preparation evidence changed while copying')
        backend_hashes[case_id + '/' + arm] = copied
    script = Path(__file__).resolve()
    script_copy = output / 'preparation' / script.name
    script_copy.parent.mkdir()
    shutil.copy2(script, script_copy)
    try:
        original_name = original.relative_to(ROOT).as_posix()
    except ValueError:
        original_name = str(original)
    supplement = {
        'cohort_id': output.name, 'reason': 'Original frozen run stopped after a connection timeout; complete only tasks without final results.',
        'original_run': original_name,
        'original_audit_file': 'original-audit.json',
        'original_audit_sha256': sha(output / 'original-audit.json'),
        'original_auditor_file': 'original-audit-reporter.py',
        'original_auditor_sha256': sha(output / 'original-audit-reporter.py'),
        **{'original_' + name + '_sha256': sha(original / (name + extension))
           for name, extension in [('freeze', '.json'), ('inputs', '.jsonl'), ('results', '.jsonl'),
                                   ('status', '.json'), ('attempts', '.jsonl'), ('responses', '.jsonl'),
                                   ('turns', '.jsonl'), ('rate_limits', '.jsonl'), ('errors', '.jsonl')]},
        'original_completed_tasks': [identity(row) for row in results],
        'selected_tasks': [identity(row) for row in selected],
        'original_unanswered_attempts': accounting['original_unanswered_attempts'],
        'context_restarted': True, 'retained_original_partial_responses': True,
        'initial_messages_byte_equivalent': True, 'backend_preparation_copied_unchanged': True,
        'fixture_recreated_from_frozen_create_function': True,
        'selection': 'All and only tasks absent from original results, in original input order; success/failure scores are not selection criteria.',
        'interval_change': {'original_seconds': original_manifest['interval_seconds'], 'supplement_seconds': 21,
                            'reason': 'Prospective pacing for observed account limit of three requests per minute.'},
        'preparation_script': script_copy.relative_to(output).as_posix(), 'preparation_script_sha256': sha(script_copy),
        'accounting_file': 'supplement-accounting.json',
        'limitations': 'Separate restarted cohort after an interrupted run; do not describe it as one uninterrupted prospective execution.',
    }
    manifest = deepcopy(original_manifest)
    manifest.update(frozen_at=now(), inputs_sha256=sha(output / 'inputs.jsonl'),
                    max_successful_calls=4 * len(selected), max_requests=12 * len(selected),
                    interval_seconds=21, supplement=supplement)
    dump(output / 'supplement-accounting.json', accounting)
    dump(output / 'backend-evidence-sha256.json', backend_hashes)
    dump(output / 'original-tree-sha256.json', before)
    dump(output / 'freeze.json', manifest)
    dump(output / 'status.json', {'status': 'frozen_offline', 'tasks': len(selected), 'supplement': True})
    verify_freeze(output)
    verify_freeze(original)
    if tree_hashes(original) != before:
        raise ValueError('Original run changed during offline supplement preparation')
    dump(output / 'preparation-validation.json', {
        'validated_at': now(), 'original_unchanged': True, 'source_and_native_hashes_unchanged': True,
        'selected_tasks': len(selected), 'original_completed_tasks': len(done),
        'fresh_project_files_match_original_initial_hashes': True,
        'backend_evidence_matches_original': True, 'api_calls': 0,
        'original_unanswered_attempts': len(accounting['original_unanswered_attempts']),
        'original_partial_valid_responses': accounting['partial_valid_responses'],
    })
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--original', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--expected-unfinished', type=int)
    args = parser.parse_args()
    result = prepare_supplement(args.original, args.output, expected_unfinished=args.expected_unfinished)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
