#!/usr/bin/env python3
"""Run isolated enterprise task suites concurrently, preserving every failure."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
from compare_learning import compare
from run_local import ROOT, dump, validate_suite


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--binary', type=Path, default=ROOT/'target/release/memweft')
    p.add_argument('--base-url', default='http://127.0.0.1:8002/v1')
    p.add_argument('--model', default='qwen3-8b')
    p.add_argument('--workers', type=int, default=3)
    p.add_argument('--repeats', type=int, default=3)
    a = p.parse_args()
    if a.workers < 1 or a.repeats < 1:
        p.error('workers and repeats must be positive')
    output = a.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    names = ('support','incident','access','access-poisoned')
    fixture_dir = output/'fixtures'
    fixture_dir.mkdir()
    hashes = {}
    for name in names:
        source = ROOT/f'evals/scenarios/enterprise-v1-{name}.json'
        validate_suite(json.loads(source.read_bytes()))
        (fixture_dir/source.name).write_bytes(source.read_bytes())
        hashes[name] = hashlib.sha256(source.read_bytes()).hexdigest()
    dump(output/'manifest.json', {'workers':a.workers,'repeats':a.repeats,'temperature':0.2,
        'seeds':list(range(42,42+a.repeats)), 'suite_hashes':hashes,
        'source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'interpretation':'Domains are evaluated independently. Support replays V6; poisoned access reuses clean access held-outs. Concurrent model load affects latency.'})
    (output/'run_enterprise.py').write_bytes(Path(__file__).read_bytes())
    def worker(name):
        start = time.perf_counter()
        cmd = [sys.executable,str(ROOT/'evals/run_local.py'), '--suite',str(fixture_dir/f'enterprise-v1-{name}.json'),
            '--output',str(output/name),'--binary',str(a.binary.resolve()),'--base-url',a.base_url,'--model',a.model,
            '--output-contract','schema','--learning-profile','evidence_table',
            '--repeats',str(a.repeats),'--temperature','0.2','--seed-step','1']
        if name != 'support':
            cmd.append('--learning-only')
        with (output/f'{name}.log').open('w') as log:
            result = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT)
        return {'name':name, 'exit_code':result.returncode,'wall_seconds':time.perf_counter()-start}
    dump(output/'status.json',{'status':'running'})
    with ThreadPoolExecutor(max_workers=a.workers) as executor:
        results = list(executor.map(worker,names))
    dump(output/'workers.json', results)
    summaries = [compare([output/r['name']])['runs'][0] for r in results if r['exit_code'] == 0]
    dump(output/'summary.json',{'runs':summaries})
    failed = [r for r in results if r['exit_code']]
    dump(output/'status.json',{'status':'failed' if failed else 'completed','failed':failed})
    for r in summaries:
        print(r['metadata']['suite_version'], r['status'],
              {k:v for k,v in r['heldout'].items() if k != 'pairs'}, flush=True)
    if failed:
        raise SystemExit(1)

if __name__ == '__main__':
    main()
