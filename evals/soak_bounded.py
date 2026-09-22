#!/usr/bin/env python3
"""Duration-based, multi-process follow-up to benchmark_bounded's short burst."""
import argparse
from concurrent.futures import ProcessPoolExecutor
import json
import multiprocessing
from pathlib import Path
import subprocess
import sys
import time

from benchmark_bounded import (CONCURRENT_QUERIES, context_digest, copy_db,
                               dump, integrity, load_sdk, sha, stats)

LIVE = {'read_pools': [{'pool_id': 'soak-live', 'access': 'read_write'}], 'default_write_pool': 'soak-live'}


def client(native, database, output, index, barrier, stop, seconds, expected):
    Memory = load_sdk(Path(native))
    with Memory(database) as memory:
        chat = memory.user('scale').session('sustained')
        live_user = memory.user('scale', agent_id=f'soak-{index}', memory_config=LIVE)
        live = live_user.session('live')
        if index >= 0:
            for name, query in CONCURRENT_QUERIES:
                assert context_digest(chat.context(query=query, max_facts=10, max_tokens=1024)) == expected[name]
        barrier.wait(timeout=180)
        start = time.perf_counter()
        samples = []
        if index < 0:
            revision, generation = 1, 0
            while not stop.is_set():
                generation += 1
                before = time.perf_counter()
                record = live_user.remember({'generation': generation, 'mirror': generation},
                                            key='pulse', expected_revision=revision)
                revision = record['revision']
                samples.append((time.perf_counter() - before) * 1000)
                time.sleep(.01)
            result = {'role': 'writer', 'updates': generation, 'latency': stats(samples), 'measurements_ms': samples}
        else:
            i = 0
            while time.perf_counter() - start < seconds:
                name, query = CONCURRENT_QUERIES[(i + index) % len(CONCURRENT_QUERIES)]
                before = time.perf_counter()
                context = chat.context(query=query, max_facts=10, max_tokens=1024)
                elapsed = (time.perf_counter() - before) * 1000
                assert context_digest(context) == expected[name]
                snapshot = live.context(query='pulse', max_facts=1)
                value = snapshot.memories[0]['value']
                assert value['generation'] == value['mirror']
                assert snapshot.report['pools']['selected'][0]['revision'] == value['generation'] + 1
                samples.append({'query': name, 'ms': elapsed})
                i += 1
            result = {'role': 'reader', 'samples': samples, 'context_checks': i, 'live_snapshot_checks': i}
        result.update({'start_monotonic': start, 'finish_monotonic': time.perf_counter(), 'native_sha256': sha(Path(native))})
    dump(Path(output) / f'client-{index}.json', result)
    return result


def worker(a):
    Memory = load_sdk(a.native)
    with Memory(str(a.database)) as memory:
        memory.user('scale', memory_config=LIVE).remember({'generation': 0, 'mirror': 0}, key='pulse', expected_revision=0)
    expected = {row['name']: row['context_sha256'] for row in json.loads((a.output / 'queries.json').read_text())}
    context = multiprocessing.get_context('spawn')
    with context.Manager() as manager:
        barrier, stop = manager.Barrier(a.readers + 1), manager.Event()
        with ProcessPoolExecutor(a.readers + 1, mp_context=context) as executor:
            args = (str(a.native.resolve()), str(a.database.resolve()), str(a.output.resolve()))
            writer = executor.submit(client, *args, -1, barrier, stop, a.seconds, expected)
            futures = [executor.submit(client, *args, i, barrier, stop, a.seconds, expected) for i in range(a.readers)]
            try:
                readers = [f.result(timeout=a.seconds + 300) for f in futures]
            finally:
                stop.set()
            writes = writer.result(timeout=120)
    assert all(r['finish_monotonic'] - r['start_monotonic'] >= a.seconds for r in readers)
    assert max(r['start_monotonic'] for r in readers) < min(r['finish_monotonic'] for r in readers)
    assert all(r['native_sha256'] == sha(a.native) for r in [*readers, writes])
    wall = max(r['finish_monotonic'] for r in readers) - min(r['start_monotonic'] for r in readers)
    samples = [sample for r in readers for sample in r['samples']]
    result = {'readers': a.readers, 'minimum_reader_seconds': a.seconds, 'reader_wall_seconds': wall,
              'query_latency': stats([s['ms'] for s in samples]),
              'by_query': {name: stats([s['ms'] for s in samples if s['query'] == name]) for name, _ in CONCURRENT_QUERIES},
              'queries_per_second_including_validation': len(samples) / wall,
              'context_checks': len(samples), 'live_snapshot_checks': sum(r['live_snapshot_checks'] for r in readers),
              'writer': {k: v for k,v in writes.items() if k != 'measurements_ms'},
              'native_sha256': sha(a.native), 'integrity': integrity(a.database),
              'method': 'Closed-loop processes, same query mix, >=60 seconds per reader; primary query timing excludes extra live-pool validation.'}
    dump(a.output / 'results.json', result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--input', type=Path, default=Path('data/evals/bounded-recall-million-run1'))
    parser.add_argument('--seconds', type=float, default=60)
    parser.add_argument('--readers', type=int, default=8)
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--native', type=Path)
    parser.add_argument('--database', type=Path)
    a = parser.parse_args()
    if a.seconds < 60 or a.readers < 2:
        parser.error('seconds must be >=60 and readers >=2')
    if a.worker:
        worker(a)
        return
    a.output.mkdir(parents=True, exist_ok=False)
    dump(a.output / 'status.json', {'status': 'running'})
    manifest = json.loads((a.input / 'manifest.json').read_text())
    manifest.update({'soak_script_sha256': sha(Path(__file__)),
                     'helper_script_sha256': sha(Path(__file__).with_name('benchmark_bounded.py')),
                     'seconds': a.seconds, 'readers': a.readers, 'input_directory': str(a.input)})
    dump(a.output / 'manifest.json', manifest)
    for name in ['soak_bounded.py', 'benchmark_bounded.py']:
        (a.output / name).write_bytes(Path(__file__).with_name(name).read_bytes())
    try:
        for build in ['before', 'after']:
            out = a.output / build
            out.mkdir()
            source = a.input / f'private-{build}'
            native = Path(manifest[build])
            assert sha(native) == manifest[build + '_sha256']
            copy_db(source / 'memory.db', out / 'memory.db')
            (out / 'queries.json').write_bytes((source / 'queries.json').read_bytes())
            command = [sys.executable, str(Path(__file__).resolve()), '--worker', '--native', str(native.resolve()),
                       '--database', str((out/'memory.db').resolve()), '--output', str(out.resolve()),
                       '--seconds', str(a.seconds), '--readers', str(a.readers)]
            with (out / 'worker.log').open('w') as log:
                subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=a.seconds+600)
            print(build, 'sustained test completed', flush=True)
        dump(a.output / 'status.json', {'status': 'completed'})
    except Exception as error:
        dump(a.output / 'status.json', {'status': 'failed', 'error': str(error)})
        raise


if __name__ == '__main__':
    main()
