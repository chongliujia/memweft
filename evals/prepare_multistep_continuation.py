#!/usr/bin/env python3
"""Offline continuation of audited cohorts using the original frozen runtime.

No credentials or API clients are used. Prior completed results, including failed
tasks, are retained; only tasks without any prior final result are restarted.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import memweft
import memweft._core as native
import multistep_fixture
import report_multistep_memory as report

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ('freeze.json', 'inputs.jsonl', 'results.jsonl', 'status.json', 'attempts.jsonl',
            'responses.jsonl', 'turns.jsonl', 'rate_limits.jsonl', 'errors.jsonl')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def rows(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []


def key(row):
    return row['case_id'], row['arm']


def identity(row):
    return {field: row[field] for field in ('case_id', 'arm')}


def inventory(root):
    found = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('Evidence contains symlink: ' + str(path))
        if path.is_file():
            found[path.relative_to(root).as_posix()] = sha(path)
    return found


def source_paths(frozen_hashes):
    """Canonical lookup only; preserve the original frozen manifest keys."""
    normalized, aliases = {}, set()
    for name, digest in frozen_hashes.items():
        relative = report._relative_path(name)
        if relative.casefold() in aliases:
            raise ValueError('Duplicate normalized snapshot path')
        aliases.add(relative.casefold())
        normalized[relative] = digest
    return normalized


def prepare_continuation(priors, output, *, expected_unfinished=10):
    priors, output = [Path(path).resolve() for path in priors], Path(output).resolve()
    if len(priors) < 2 or len(set(priors)) != len(priors):
        raise ValueError('At least two distinct ordered prior cohorts are required')
    if output.exists() or any(output.is_relative_to(path) or path.is_relative_to(output) for path in priors):
        raise ValueError('Output must be a new directory disjoint from all prior cohorts')
    prior_names = [path.relative_to(ROOT).as_posix() for path in priors]
    before = [inventory(path) for path in priors]
    auditor = Path(report.__file__).resolve()
    auditor_bytes = auditor.read_bytes()
    audits = [report.audit(path, allow_partial=True) for path in priors]
    if auditor.read_bytes() != auditor_bytes:
        raise ValueError('Auditor changed during preparation')
    original = priors[0]
    original_manifest = json.loads((original / 'freeze.json').read_text(encoding='utf-8'))
    sources = source_paths(original_manifest['source_sha256'])
    if 'supplement' in original_manifest or 'continuation' in original_manifest:
        raise ValueError('First prior must be the original cohort')
    for prior in priors[1:]:
        manifest = json.loads((prior / 'freeze.json').read_text(encoding='utf-8'))
        if any(manifest[name] != original_manifest[name] for name in ('suite_sha256', 'source_sha256', 'native_sha256')):
            raise ValueError('Prior cohort runtime or suite differs from original')
    if sha(Path(multistep_fixture.__file__)) != sources['evals/multistep_fixture.py']:
        raise ValueError('Fixture implementation differs from frozen original')
    if sha(Path(native.__file__)) != original_manifest['native_sha256']:
        raise ValueError('Installed native binary differs from original')
    package = Path(memweft.__file__).resolve().parent
    for name, digest in sources.items():
        if name.startswith('python/src/memweft/') and sha(package / Path(name).name) != digest:
            raise ValueError('Installed SDK source differs from original: ' + name)
    completed, completed_keys, accounting, unknown = [], set(), [], []
    for name, prior, audit in zip(prior_names, priors, audits):
        local_results = rows(prior / 'results.jsonl')
        local_done = {key(row) for row in local_results}
        for row in local_results:
            if key(row) in completed_keys:
                raise ValueError('Duplicate final result across prior cohorts')
            completed_keys.add(key(row))
            completed.append(identity(row))
        partial = [row for row in rows(prior / 'responses.jsonl') if key(row) not in local_done]
        unknown.extend({'cohort': name, **attempt} for attempt in audit['unanswered_attempts'])
        accounting.append({'cohort': name, 'completed_tasks': len(local_results),
            'recorded_responses': audit['model_calls'], 'requests': audit['requests'],
            'known_reported_usage': audit['usage'], 'usage_complete': audit['usage_complete'],
            'unanswered_attempts': audit['unanswered_attempts'], 'partial_responses': partial,
            'partial_response_count': len(partial)})
    original_rows = rows(original / 'inputs.jsonl')
    all_keys = {key(row) for row in original_rows}
    if not completed_keys <= all_keys:
        raise ValueError('Prior final result is outside original task matrix')
    selected = [row for row in original_rows if key(row) not in completed_keys]
    if not selected or len(selected) != expected_unfinished:
        raise ValueError('Unexpected unfinished task count')
    selected_keys = {key(row) for row in selected}
    suite = {case['id']: case for case in json.loads((original / 'suite.json').read_text(encoding='utf-8'))['cases']}
    output.mkdir(parents=True, exist_ok=False)
    shutil.copytree(original / 'sources', output / 'sources')
    shutil.copy2(original / 'suite.json', output / 'suite.json')
    lines = (original / 'inputs.jsonl').read_bytes().splitlines(keepends=True)
    if len(lines) != len(original_rows):
        raise ValueError('Input byte lines do not match parsed rows')
    (output / 'inputs.jsonl').write_bytes(b''.join(line for line, row in zip(lines, original_rows) if key(row) in selected_keys))
    backend_hashes = {}
    for row in selected:
        case_id, arm = key(row)
        old_task, new_task = (root / 'tasks' / case_id / arm for root in (original, output))
        new_task.mkdir(parents=True)
        for child in old_task.iterdir():
            if child.name != 'project':
                destination = new_task / child.name
                shutil.copytree(child, destination) if child.is_dir() else shutil.copy2(child, destination)
        files = multistep_fixture.create_fixture(new_task / 'project', suite[case_id])
        if files != row['files'] or inventory(new_task / 'project') != row['initial_file_sha256']:
            raise ValueError('Fresh project differs from original initial fixture')
        expected = {name: digest for name, digest in inventory(old_task).items() if not name.startswith('project/')}
        actual = {name: digest for name, digest in inventory(new_task).items() if not name.startswith('project/')}
        if expected != actual:
            raise ValueError('Backend evidence changed during copying')
        backend_hashes[case_id + '/' + arm] = actual
    # Runtime contains the old measured sources, independent of current patched
    # runner/client files. Copy installed SDK support files too, then overwrite
    # the prospectively frozen SDK files and verify the recorded native digest.
    runtime = output / 'isolated-runtime'
    shutil.copytree(original / 'sources', runtime)
    sdk_copy = runtime / 'python/src/memweft'
    shutil.copytree(package, sdk_copy, dirs_exist_ok=True, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for name in sources:
        shutil.copy2(original / 'sources' / name, runtime / name)
    runtime_native = sdk_copy / Path(native.__file__).name
    if sha(runtime_native) != original_manifest['native_sha256']:
        raise ValueError('Copied native binary differs from original')
    for name, digest in sources.items():
        if sha(runtime / name) != digest:
            raise ValueError('Isolated runtime source changed: ' + name)
    launcher = output / 'run_frozen.py'
    launcher.write_text('''#!/usr/bin/env python3
"""Explicit paid-run entry point; offline preparation never invokes this file."""
import os
from pathlib import Path
import sys
root = Path(__file__).resolve().parent
runtime = root / "isolated-runtime"
env = dict(os.environ)
env["PYTHONPATH"] = str(runtime / "python/src")
os.execve(sys.executable, [sys.executable, str(runtime / "evals/run_multistep_memory.py"),
                         "run", "--output", str(root), *sys.argv[1:]], env)
''', encoding='utf-8')
    preparation = output / 'preparation'
    preparation.mkdir()
    script_copy = preparation / Path(__file__).name
    shutil.copy2(Path(__file__), script_copy)
    (preparation / 'report_multistep_memory.py').write_bytes(auditor_bytes)
    dump(output / 'prior-audits.json', audits)
    dump(output / 'prior-tree-sha256.json', dict(zip(prior_names, before)))
    dump(output / 'backend-evidence-sha256.json', backend_hashes)
    dump(output / 'prior-accounting.json', {'cohorts': accounting, 'unanswered_attempts': unknown,
        'unknown_billing_attempts': len(unknown), 'usage_complete': all(a['usage_complete'] for a in audits),
        'known_reported_usage': {name: sum(a['usage'][name] for a in audits)
                                 for name in ('prompt_tokens', 'completion_tokens', 'total_tokens')},
        'interpretation': 'Keep all prior responses and unknown-billing attempts as separate evidence and cost; restart selected tasks with original messages and no inherited partial session.'})
    continuation = {
        'cohort_id': output.name, 'reason': 'Both previous cohorts stopped after connection timeouts; finish only tasks without a prior final result.',
        'original_run': prior_names[0], 'root_freeze_sha256': sha(original / 'freeze.json'),
        'prior_cohorts': [{'run': name, 'evidence_sha256': {file: sha(path / file) if (path / file).exists() else None
                                                        for file in EVIDENCE}} for name, path in zip(prior_names, priors)],
        'previous_completed_tasks': completed, 'selected_tasks': [identity(row) for row in selected],
        'context_restarted': True, 'retained_prior_partial_responses': True,
        'initial_messages_byte_equivalent': True, 'backend_preparation_copied_unchanged': True,
        'fixture_recreated_from_frozen_create_function': True,
        'preparation_script': script_copy.relative_to(output).as_posix(), 'preparation_script_sha256': sha(script_copy),
        'auditor_script': 'preparation/report_multistep_memory.py', 'auditor_script_sha256': sha(preparation / 'report_multistep_memory.py'),
        'accounting_file': 'prior-accounting.json', 'prior_audits_file': 'prior-audits.json',
        'runtime_root': runtime.relative_to(output).as_posix(),
        'runtime_source_sha256': original_manifest['source_sha256'],
        'runtime_native_file': runtime_native.relative_to(output).as_posix(), 'runtime_native_sha256': sha(runtime_native),
        'runtime_support_sha256': inventory(sdk_copy),
        'runtime_support_provenance': 'Installed SDK package copied for imports; recorded original SDK source files and native binary verified against original freeze. Adapter support files were not independently frozen in the original experiment.',
        'launcher': launcher.relative_to(output).as_posix(), 'launcher_sha256': sha(launcher),
        'selection': 'Original input order, excluding every prior final result regardless of success/failure.',
        'limitations': 'Third restarted cohort, not one uninterrupted experiment; prior unknown-billing attempts remain unknown.'}
    manifest = deepcopy(original_manifest)
    manifest.update(frozen_at=datetime.now(timezone.utc).isoformat(), inputs_sha256=sha(output / 'inputs.jsonl'),
                    max_successful_calls=4 * len(selected), max_requests=12 * len(selected),
                    interval_seconds=21, continuation=continuation)
    dump(output / 'freeze.json', manifest)
    dump(output / 'status.json', {'status': 'frozen_offline', 'tasks': len(selected), 'continuation': True})
    # Exercise the actual original runner's verifier under the exact launch SDK
    # path. No client is constructed and no key is accessed.
    env = dict(os.environ)
    env['PYTHONPATH'] = str(runtime / 'python/src')
    code = ('import json,sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); '
            'import run_multistep_memory as r; r.verify_freeze(Path(sys.argv[2])); '
            'print(json.dumps({"root":str(r.ROOT),"native":r.native.__file__,"native_sha256":r.sha(Path(r.native.__file__))}))')
    checked = subprocess.run([sys.executable, '-c', code, str(runtime / 'evals'), str(output)],
                             cwd=runtime, env=env, text=True, capture_output=True, check=True)
    runtime_check = json.loads(checked.stdout)
    if Path(runtime_check['root']).resolve() != runtime or Path(runtime_check['native']).resolve() != runtime_native:
        raise ValueError('Frozen runtime verification imported an unexpected path')
    if any(inventory(path) != snapshot for path, snapshot in zip(priors, before)):
        raise ValueError('Prior cohort changed during preparation')
    dump(output / 'preparation-validation.json', {'prior_cohorts_unchanged': True,
        'prior_audits_passed': True, 'prior_completed_tasks': len(completed), 'selected_tasks': len(selected),
        'fresh_projects_verified': True, 'backend_evidence_unchanged': True,
        'original_runtime_verify_freeze_passed': True, 'runtime_imports': runtime_check,
        'prior_unknown_billing_attempts': len(unknown), 'api_calls': 0})
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prior', action='append', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--expected-unfinished', type=int, default=10)
    args = parser.parse_args()
    print(json.dumps(prepare_continuation(args.prior, args.output, expected_unfinished=args.expected_unfinished),
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
