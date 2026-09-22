#!/usr/bin/env python3
"""Compare completed legacy/boundary runs on an identical pinned suite."""
import argparse
import json
from pathlib import Path


def load(path):
    return json.loads(path.read_text(encoding='utf-8'))


def generalization_verdict(job, effect):
    """Report a held-out check; never feed it back into candidate selection."""
    n = effect.get('n', 0)
    gain = (effect['adopted_passed'] - effect['baseline_passed']) / n if n else None
    passed = (job['status'] == 'accepted' and n > 0 and gain > 0
              and gain >= job['policy']['min_gain'] and effect['regressed'] == 0)
    return {'passed': passed, 'mean_gain': gain, 'regressed_observations': effect.get('regressed', 0),
            'scope': 'held-out task transfer only; not production readiness or cross-domain generalization'}


def compare(directories):
    runs = []
    for directory in directories:
        status = load(directory / 'status.json')
        if status['status'] != 'completed':
            raise ValueError(f'incomplete run: {directory}')
        metadata = load(directory / 'metadata.json')
        job = load(directory / 'learning.json')['job']
        calls = [json.loads(line) for line in (directory / 'calls.jsonl').read_text().splitlines()]
        groups = load(directory / 'summary.json')['groups']
        evidence = (job['evaluation'] or {}).get('cases', [])
        runs.append({
            'directory': str(directory), 'metadata': metadata,
            'profile': metadata['learning_profile'], 'status': job['status'], 'reason': job['reason'],
            'policy': job['policy'], 'candidate': job['proposal']['content'],
            'training_case_ids': load(directory / 'proposal_input.json')['training_case_ids'],
            'validation': {'n': len(evidence),
                'baseline_passed': sum(c['baseline_score'] == 1 for c in evidence),
                'candidate_passed': sum(c['candidate_score'] == 1 for c in evidence),
                'regressed': sum(c['candidate_score'] < c['baseline_score'] for c in evidence)},
            'heldout': load(directory / 'learning_effect.json'),
            'groups': groups,
            'protocol': {'passed': sum(g['protocol_passed'] for g in groups),
                         'n': sum(g['protocol_evaluated'] for g in groups)},
            'adoption_check': load(directory / 'adoption_check.json'),
            'task_scope_check': load(directory / 'task_scope_check.json'),
            'rollback_verified': load(directory / 'rollback.json')['verified'] if job['status'] == 'accepted' else None,
            'model_calls': len(calls),
            'total_tokens': sum(c['usage']['total_tokens'] for c in calls),
        })
        runs[-1]['generalization_check'] = generalization_verdict(job, runs[-1]['heldout'])
        training_path = directory / 'training_rounds.json'
        if training_path.exists():
            training = load(training_path)
            runs[-1]['training'] = {'selected_round': training['selected_round'], 'max_rounds': training['max_rounds'],
                'rounds': [{k: r[k] for k in ('round','passed','n','proposer_call_id')} for r in training['rounds']]}
        if (directory / 'training_gate.json').exists():
            runs[-1]['training_gate'] = load(directory / 'training_gate.json')
    shared_fields = ('suite_sha256','binary_sha256','runner_sha256','contract_module_sha256',
                     'repeats','seed','temperature','max_tokens','enable_thinking','evaluator',
                     'model','base_url','output_contract')
    shared_fields += tuple(k for k in ('evidence_module_sha256','learning_only','seed_step') if any(k in r['metadata'] for r in runs))
    if any(r['metadata'].get(k) != runs[0]['metadata'].get(k) for r in runs[1:] for k in shared_fields):
        raise ValueError('runs differ in suite, core, runner or model-call settings')
    if any(r['policy'] != runs[0]['policy'] for r in runs[1:]):
        raise ValueError('adoption policies differ')
    return {'matched_fields': list(shared_fields), 'runs': runs,
            'interpretation': ('The primary comparison is each run\'s held-out pre-learning baseline versus its actually adopted strategy. '
                'Cross-profile comparisons can change training coverage, proposal instructions and token limits; their individual contributions are not isolated. '
                'Repeats at temperature zero are not independent samples.')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runs', type=Path, nargs='+')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = compare(args.runs)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    for run in result['runs']:
        val, test = run['validation'], run['heldout']
        print(f"{run['profile']}: {run['status']}; validation {val['baseline_passed']} -> {val['candidate_passed']}/{val['n']}, regressions={val['regressed']}; heldout {test['baseline_passed']} -> {test['adopted_passed']}/{test['n']}, regressions={test['regressed']}")


if __name__ == '__main__':
    main()
