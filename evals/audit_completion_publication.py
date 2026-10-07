#!/usr/bin/env python3
"""Replay a published completion pilot from trusted, hash-matched local sources.

No API requests, credentials, imported snapshot code, or original data/ directory
are needed. Byte-level artifacts require the source and newline conventions of
the originating run; a differing checkout fails rather than ignoring hashes.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile

import run_completion_pilot as pilot


def audit_publication(report):
    report = Path(report)
    saved = pilot.read(report)
    traces = report.with_suffix('.traces.jsonl')
    if traces.stat().st_size > 8_000_000 or pilot.sha(traces) != saved['trace_sha256']:
        raise ValueError('Published trace size or hash differs')
    groups = {name: [] for name in ('freeze', 'suite', 'status', 'inputs', 'attempts',
                                   'rate_limits', 'responses', 'turns', 'results')}
    for row in pilot.rows(traces):
        if set(row) != {'kind', 'record'} or row['kind'] not in groups:
            raise ValueError('Unknown published record')
        groups[row['kind']].append(row['record'])
    if any(len(groups[name]) != 1 for name in ('freeze', 'suite', 'status')):
        raise ValueError('Expected one frozen contract, suite and terminal status')
    if (len(groups['inputs']) != 12 or len(groups['results']) != 12
            or len(groups['responses']) > 48 or len(groups['turns']) > 48
            or len(groups['attempts']) > 144 or len(groups['rate_limits']) > 144):
        raise ValueError('Published record budget differs')
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory)
        for name in ('freeze', 'suite', 'status'):
            pilot.dump(output / (name + '.json'), groups[name][0])
        for name in ('inputs', 'attempts', 'rate_limits', 'responses', 'turns', 'results'):
            for row in groups[name]:
                pilot.append(output / (name + '.jsonl'), row)
        # Copy only the known trusted implementation. Never execute code supplied
        # by evidence or use archive-provided paths as filesystem destinations.
        for source in pilot.source_paths():
            target = output / 'sources' / source.relative_to(pilot.ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
        pilot.verify(output)  # Checks identities before using them as paths.
        cases = {case['id']: case for case in pilot.suite()}
        cursor = 0
        for row in groups['inputs']:
            project = output / 'tasks' / row['case_id'] / row['mode'] / 'project'
            pilot.create_fixture(project, cases[row['case_id']])
            session = pilot.PilotSession(project, row)
            for turn in range(4):
                if cursor >= len(groups['responses']):
                    raise ValueError('Missing published response')
                response = groups['responses'][cursor]
                if (response['case_id'], response['mode'], response['round']) != (row['case_id'], row['mode'], turn):
                    raise ValueError('Published response order differs')
                session.step(response['response'])
                cursor += 1
                if session.status != 'active':
                    break
        # Rebuilds once more to independently compare all recorded tool results,
        # contexts, business grades, final bytes, accounting and journal hashes.
        reconstructed = pilot.audit(output)
        expected = {key: value for key, value in saved.items() if key != 'trace_sha256'}
        if reconstructed != expected:
            raise ValueError('Published summary or evidence hashes differ from replay')
    return {'verified': True, 'model_calls': 0, 'tasks': 12,
            'trace_sha256': saved['trace_sha256'], 'summary': reconstructed['summary']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    args = parser.parse_args()
    print(json.dumps(audit_publication(args.report), ensure_ascii=False, indent=2))
