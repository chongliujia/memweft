#!/usr/bin/env python3
"""Paired indexed v1/v2 SDK queries, writes, and million-record process concurrency.

All databases are disposable copies. Workers load exactly one native build per
process; concurrent readers use processes so the Python GIL cannot serialize them.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor
import hashlib
import importlib.util
import json
import math
import multiprocessing
from pathlib import Path
import platform
import resource
import sqlite3
import statistics
import subprocess
import sys
import time

QUERIES = [('rare', 'deployment port'), ('frequent', 'archive'),
           ('mixed_terms', 'archive deployment port'), ('absent', 'notpresentxyz'),
           ('empty', None)]
CONCURRENT_QUERIES = QUERIES[:3] + [('value_common', 'common')]


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stats(times):
    return {'n': len(times), 'p50_ms': statistics.median(times),
            'p95_ms': sorted(times)[math.ceil(.95 * len(times)) - 1], 'max_ms': max(times)}


def load_sdk(native):
    spec = importlib.util.spec_from_file_location('memweft._core', native.resolve())
    module = importlib.util.module_from_spec(spec)
    sys.modules['memweft._core'] = module
    spec.loader.exec_module(module)
    from memweft import Memory
    return Memory


def config(profile):
    if profile == 'shared':
        return {'read_pools': [{'pool_id': 'team', 'access': 'read_write'}], 'default_write_pool': 'team'}
    if profile == 'mixed':
        return {'read_pools': [{'pool_id': 'team', 'access': 'read_write'},
                               {'pool_id': 'private', 'access': 'read_write'}],
                'default_write_pool': 'private', 'conflict_policy': 'private_first'}
    return None


def content(context):
    return {k: getattr(context, k) for k in ('text', 'memories', 'messages', 'strategies')}


def context_digest(context):
    return hashlib.sha256(json.dumps(content(context), sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def integrity(path):
    with sqlite3.connect(path) as conn:
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        check = conn.execute('PRAGMA integrity_check').fetchone()[0]
        assert check == 'ok', check
        assert conn.execute('PRAGMA foreign_key_check').fetchone() is None
        return {'integrity_check': check, 'foreign_key_check': 'ok',
                'database_bytes': path.stat().st_size,
                'index_version': conn.execute('SELECT version FROM memweft_recall_version').fetchone()[0],
                'private_facts': conn.execute('SELECT count(*) FROM facts').fetchone()[0],
                'shared_facts': conn.execute('SELECT count(*) FROM memweft_pool_facts WHERE record IS NOT NULL').fetchone()[0]}


def worker(a):
    Memory = load_sdk(a.native)
    start = time.perf_counter()
    memory = Memory(str(a.database))
    open_ms = (time.perf_counter() - start) * 1000
    if a.mode == 'initialize':
        memory.close()
        dump(a.output / 'initialize.json', {'open_and_migration_ms': open_ms, **integrity(a.database)})
        return
    session = memory.user('scale', memory_config=config(a.profile)).session('query')
    queries = QUERIES + ([('value_common', 'common'), ('fallback', 'red blue')] if a.million else [])
    rows = []
    for name, query in queries:
        times = []
        for i in range(a.samples + 1):
            start = time.perf_counter()
            result = session.context(query=query, max_facts=10, max_tokens=1024)
            elapsed = (time.perf_counter() - start) * 1000
            if i:
                times.append(elapsed)
            assert result.report['recall']['inspected_facts'] <= 74
            assert len(result.report['omissions']) <= 64
        rows.append({'name': name, 'query': query, **stats(times), 'measurements_ms': times,
                     'context': content(result), 'context_sha256': context_digest(result), 'report': result.report})
        dump(a.output / 'queries.json', rows)
        print(a.profile, name, round(rows[-1]['p95_ms'], 3), flush=True)

    writes = []
    # Same public SDK single-record transactions on prepopulated, copied files.
    # Separate user scopes keep the timed retrieval fixture immutable.
    for trial in range(3):
        user = memory.user(f'write-benchmark-{trial}', memory_config=config(a.profile))
        for operation in ['insert', 'update']:
            times = []
            for i in range(a.writes):
                start = time.perf_counter()
                user.remember(f'{operation} archive record {i:08d}', key=f'write_{i:08d}')
                times.append((time.perf_counter() - start) * 1000)
            writes.append({'trial': trial, 'operation': operation, **stats(times),
                           'total_ms': sum(times), 'measurements_ms': times})
        facts = user.memories()
        assert len(facts) == a.writes
        assert all(f['value'] == f"update archive record {int(f['fact_key'].split('_')[1]):08d}" for f in facts)
    dump(a.output / 'writes.json', writes)
    memory.close()
    dump(a.output / 'metadata.json', {'native_sha256': sha(a.native), 'platform': platform.platform(),
         'python': sys.version, 'open_and_migration_ms': open_ms,
         'peak_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
         'file_after_writes': integrity(a.database)})


def concurrent_client(native, database, output, index, barrier, stop, rounds, expected):
    Memory = load_sdk(Path(native))
    with Memory(database) as memory:
        session = memory.user('scale').session('concurrent')
        pulse = memory.user('scale', agent_id=f'client-{index}', memory_config={
            'read_pools': [{'pool_id': 'live', 'access': 'read_write'}], 'default_write_pool': 'live'})
        barrier.wait(timeout=120)
        started = time.perf_counter()
        rows = []
        if index < 0:
            revision = 1
            generation = 0
            while not stop.is_set():
                generation += 1
                before = time.perf_counter()
                record = pulse.remember({'generation': generation, 'mirror': generation},
                                        key='pulse', expected_revision=revision)
                revision = record['revision']
                rows.append((time.perf_counter() - before) * 1000)
                # Fixed offered write rate, rather than unbounded write saturation.
                time.sleep(.01)
            result = {'role': 'writer', 'updates': generation, 'latency': stats(rows),
                      'start_monotonic': started, 'finish_monotonic': time.perf_counter()}
        else:
            live_reads = 0
            for i in range(rounds):
                name, query = CONCURRENT_QUERIES[(i + index) % len(CONCURRENT_QUERIES)]
                before = time.perf_counter()
                result = session.context(query=query, max_facts=10, max_tokens=1024)
                elapsed = (time.perf_counter() - before) * 1000
                assert context_digest(result) == expected[name], (index, name, 'changed context')
                live = pulse.session('live').context(query='pulse', max_facts=1)
                record = live.memories[0]
                value = record['value']
                assert value['generation'] == value['mirror']
                assert live.report['pools']['selected'][0]['revision'] == value['generation'] + 1
                live_reads += 1
                rows.append({'name': name, 'ms': elapsed,
                             'plan': result.report['recall'].get('ranking_plan', 'postings_aggregate')})
            result = {'role': 'reader', 'samples': rows, 'live_snapshot_checks': live_reads,
                      'start_monotonic': started, 'finish_monotonic': time.perf_counter()}
    dump(Path(output) / f'client-{index}.json', result)
    return result


def concurrency(a):
    Memory = load_sdk(a.native)
    with Memory(str(a.database)) as memory:
        memory.user('scale', memory_config={'read_pools': [{'pool_id': 'live', 'access': 'read_write'}],
                    'default_write_pool': 'live'}).remember({'generation': 0, 'mirror': 0}, key='pulse', expected_revision=0)
    expected = {row['name']: row['context_sha256'] for row in json.loads((a.output / 'queries.json').read_text())}
    context = multiprocessing.get_context('spawn')
    with context.Manager() as manager:
        barrier = manager.Barrier(a.readers + 1)
        stop = manager.Event()
        with ProcessPoolExecutor(a.readers + 1, mp_context=context) as executor:
            common = (str(a.native.resolve()), str(a.database.resolve()), str(a.output.resolve()))
            writer = executor.submit(concurrent_client, *common, -1, barrier, stop, a.rounds, expected)
            readers = [executor.submit(concurrent_client, *common, i, barrier, stop, a.rounds, expected)
                       for i in range(a.readers)]
            try:
                results = [task.result(timeout=900) for task in readers]
            finally:
                stop.set()
            write_result = writer.result(timeout=120)
    # Overlap is checked explicitly; processes must not silently run serially.
    assert max(r['start_monotonic'] for r in results) < min(r['finish_monotonic'] for r in results)
    times = [sample['ms'] for result in results for sample in result['samples']]
    duration = max(r['finish_monotonic'] for r in results) - min(r['start_monotonic'] for r in results)
    dump(a.output / 'concurrency.json', {'readers': a.readers, 'rounds': a.rounds,
         'query_latency': stats(times), 'reader_wall_seconds': duration,
         'query_throughput_per_second_including_validation': len(times) / duration,
         'query_context_checks': len(times), 'live_snapshot_checks': sum(r['live_snapshot_checks'] for r in results),
         'writer': write_result, 'integrity_after_concurrency': integrity(a.database),
         'method': 'Spawned processes with a start barrier; separate SDK clients. Query timing excludes live-pool validation.'})


def copy_db(source, target):
    with sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True) as old:
        with sqlite3.connect(target) as new:
            old.backup(new)


def launch(a, native, database, output, profile, mode, million=False):
    command = [sys.executable, str(Path(__file__).resolve()), '--mode', mode,
               '--native', str(native.resolve()), '--database', str(database.resolve()),
               '--output', str(output.resolve()), '--profile', profile, '--samples', str(a.samples),
               '--writes', str(a.writes), '--readers', str(a.readers), '--rounds', str(a.rounds)]
    if million:
        command.append('--million')
    with (output / f'{mode}.log').open('w') as log:
        subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=1800)


def parent(a):
    a.output.mkdir(parents=True, exist_ok=False)
    dump(a.output / 'status.json', {'status': 'running'})
    manifest = {'before': str(a.before), 'after': str(a.after), 'before_sha256': sha(a.before),
                'after_sha256': sha(a.after), 'script_sha256': sha(Path(__file__)),
                'samples': a.samples, 'writes_per_trial': a.writes, 'write_trials': 3,
                'readers': a.readers, 'reader_rounds': a.rounds,
                'concurrent_queries': CONCURRENT_QUERIES,
                'source_sha256': {str(p): sha(p) for p in [
                    Path('crates/memweft-store/src/indexed_recall.rs'),
                    Path('crates/memweft-store/src/sqlite.rs'), Path('crates/memweft-store/src/pools.rs'),
                    Path('crates/memweft/src/lib.rs'), Path('crates/memweft/src/pools.rs')]},
                'build_order': ['before', 'after'], 'limitations': 'Synthetic warm-cache data; chronological paired runs, not randomized; not a production SLO.'}
    dump(a.output / 'manifest.json', manifest)
    (a.output / Path(__file__).name).write_bytes(Path(__file__).read_bytes())
    combined = []
    try:
        profiles = ['private', 'shared', 'mixed'] if not a.million else ['private']
        if a.million:
            fixture = a.output / 'million-v1.db'
            copy_db(Path('data/evals/enterprise-storage-run1/scale.db'), fixture)
            with sqlite3.connect(fixture) as conn:
                assert conn.execute("SELECT count(*) FROM sqlite_master WHERE name='memweft_recall_version'").fetchone()[0] == 0
                conn.execute("DELETE FROM facts WHERE fact_key<>'z_deployment_port'")
                conn.execute("UPDATE facts SET value_json=json_quote('The deployment port is 17443. red blue')")
                conn.execute("""WITH RECURSIVE numbers(n) AS (VALUES(1) UNION ALL SELECT n+1 FROM numbers WHERE n<999999)
                    INSERT INTO facts(tenant_id,user_id,agent_id,fact_id,fact_key,value_json,status,
                      valid_from,valid_to,confidence,sources,scope_level,notes)
                    SELECT 'default','scale','default',printf('memweft:key:a_archive_%08d',n),
                      printf('a_archive_%08d',n),json_quote(printf('Archived inactive document %08d; owner archive-%d. common %s',n,n%100,
                        CASE WHEN n%2=0 THEN 'red' ELSE 'blue' END)),
                      'active',NULL,NULL,1,'[]','user','' FROM numbers""")
                assert conn.execute('SELECT count(*) FROM facts').fetchone()[0] == 1000000
            launch(a, a.before, fixture, a.output, 'private', 'initialize', True)
        for profile in profiles:
            if not a.million:
                fixture = Path('data/evals/indexed-recall-comparison-run2') / f'{profile}-after/memory.db'
            pair = {}
            for label, native in [('before', a.before), ('after', a.after)]:
                out = a.output / f'{profile}-{label}'
                out.mkdir()
                database = out / 'memory.db'
                copy_db(fixture, database)
                launch(a, native, database, out, profile, 'worker', a.million)
                if a.million:
                    launch(a, native, database, out, profile, 'concurrency', True)
                pair[label] = json.loads((out / 'queries.json').read_text())
                print(profile, label, 'completed', flush=True)
            for old, new in zip(pair['before'], pair['after'], strict=True):
                assert old['name'] == new['name'] and old['context'] == new['context'], (profile, old['name'])
                combined.append({'profile': profile, 'query': new['name'], 'context_identical': True,
                    'before_p50_ms': old['p50_ms'], 'after_p50_ms': new['p50_ms'],
                    'before_p95_ms': old['p95_ms'], 'after_p95_ms': new['p95_ms'],
                    'after_plan': new['report']['recall']['ranking_plan']})
            dump(a.output / 'comparison.json', combined)
        dump(a.output / 'status.json', {'status': 'completed'})
    except Exception as error:
        dump(a.output / 'status.json', {'status': 'failed', 'error': str(error)})
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--before', type=Path, default=Path('data/evals/indexed-v1-before-build/libmemweft_ffi.so'))
    parser.add_argument('--after', type=Path, default=Path('target/release/libmemweft_ffi.so'))
    parser.add_argument('--samples', type=int, default=30)
    parser.add_argument('--writes', type=int, default=1000)
    parser.add_argument('--readers', type=int, default=8)
    parser.add_argument('--rounds', type=int, default=30)
    parser.add_argument('--million', action='store_true')
    parser.add_argument('--mode', choices=['parent', 'worker', 'initialize', 'concurrency'], default='parent')
    parser.add_argument('--native', type=Path)
    parser.add_argument('--database', type=Path)
    parser.add_argument('--profile', choices=['private', 'shared', 'mixed'], default='private')
    a = parser.parse_args()
    if min(a.samples, a.writes, a.rounds) < 1 or a.readers < 2:
        parser.error('samples, writes and rounds must be positive; readers must be >=2')
    if a.mode != 'parent' and (a.native is None or a.database is None):
        parser.error('worker modes require --native and --database')
    if a.mode == 'parent':
        parent(a)
    elif a.mode == 'concurrency':
        concurrency(a)
    else:
        worker(a)


if __name__ == '__main__':
    main()
