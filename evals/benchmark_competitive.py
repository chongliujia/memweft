#!/usr/bin/env python3
"""Recheck all saved SDK contexts against v2 before/after query optimization."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

from benchmark_bounded import config, content, copy_db, dump, integrity, load_sdk, sha
from benchmark_pressure import stats


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--before', type=Path, default=Path('data/evals/indexed-v2-before-tuning/libmemweft_ffi.so'))
    p.add_argument('--after', type=Path, default=Path('target/release/libmemweft_ffi.so'))
    p.add_argument('--samples', type=int, default=30)
    p.add_argument('--worker', action='store_true')
    p.add_argument('--native', type=Path)
    p.add_argument('--source', type=Path)
    p.add_argument('--profile', default='private')
    a = p.parse_args()
    if a.samples < 1:
        p.error('samples must be positive')
    if a.worker:
        Memory = load_sdk(a.native)
        expected = json.loads((a.source/'queries.json').read_text())
        rows = []
        with Memory(str(a.output/'memory.db')) as memory:
            session = memory.user('scale', memory_config=config(a.profile)).session('query')
            for original in expected:
                times = []
                for i in range(a.samples+1):
                    start = time.perf_counter()
                    result = session.context(query=original['query'], max_facts=10, max_tokens=1024)
                    elapsed = (time.perf_counter()-start)*1000
                    assert content(result) == original['context']
                    if i:
                        times.append(elapsed)
                rows.append({'name': original['name'], 'query': original['query'], **stats(times),
                             'samples_ms': times, 'plan': result.report['recall']['ranking_plan'],
                             'context': content(result), 'context_identical': True})
                dump(a.output/'queries.json', rows)
                print(a.profile,original['name'],round(rows[-1]['p95_ms'],3),flush=True)
        dump(a.output/'metadata.json', {'native_sha256': sha(a.native), 'integrity': integrity(a.output/'memory.db')})
        return
    a.output.mkdir(parents=True, exist_ok=False)
    dump(a.output/'status.json', {'status':'running'})
    manifest = {'before':str(a.before),'after':str(a.after),'before_sha256':sha(a.before),'after_sha256':sha(a.after),
                'samples':a.samples,'sources':{},'scripts':{}}
    for name in ['benchmark_competitive.py','benchmark_pressure.py','benchmark_bounded.py']:
        file = Path(__file__).with_name(name)
        (a.output/name).write_bytes(file.read_bytes())
        manifest['scripts'][name] = sha(file)
    for path in ['crates/memweft-store/src/indexed_recall.rs','crates/memweft-store/src/checkpoint.rs',
                 'crates/memweft-store/src/sqlite.rs','crates/memweft-ffi/src/lib.rs','python/src/memweft/client.py']:
        manifest['sources'][path] = sha(Path(path))
    dump(a.output/'manifest.json', manifest)
    try:
        for label,source,profile in [
            ('million',Path('data/evals/bounded-recall-million-run1/private-after'),'private'),
            *[(f'100k-{profile}',Path(f'data/evals/bounded-recall-100k-run1/{profile}-after'),profile)
              for profile in ['private','shared','mixed']]]:
            for build,native in [('before',a.before),('after',a.after)]:
                directory = a.output/f'{label}-{build}'
                directory.mkdir()
                copy_db(source/'memory.db', directory/'memory.db')
                subprocess.run([sys.executable,str(Path(__file__).resolve()),'--worker','--native',str(native.resolve()),
                                '--source',str(source.resolve()),'--output',str(directory.resolve()),
                                '--profile',profile,'--samples',str(a.samples)],check=True,timeout=900)
        dump(a.output/'status.json', {'status':'completed'})
    except Exception as error:
        dump(a.output/'status.json', {'status':'failed','error':str(error)})
        raise


if __name__ == '__main__':
    main()
