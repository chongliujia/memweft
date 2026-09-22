#!/usr/bin/env python3
"""Rate-controlled multiprocess recall/write benchmark on disposable DB copies."""
import argparse
from concurrent.futures import ProcessPoolExecutor
import json
import math
import multiprocessing
import os
from pathlib import Path
import resource
import sqlite3
import subprocess
import sys
import time

from benchmark_bounded import CONCURRENT_QUERIES, context_digest, copy_db, dump, integrity, load_sdk, sha

LIVE = {'read_pools': [{'pool_id': 'pressure-live', 'access': 'read_write'}],
        'default_write_pool': 'pressure-live'}


def stats(values):
    if not values:
        return {'n': 0}
    values = sorted(values)
    return {'n': len(values), 'mean_ms': sum(values) / len(values),
            **{f'p{p}_ms': values[math.ceil(p / 100 * len(values)) - 1] for p in [50, 95, 99]},
            'max_ms': values[-1]}


def usage():
    r = resource.getrusage(resource.RUSAGE_SELF)
    io = {}
    if Path('/proc/self/io').exists():
        io = {k: int(v) for k, v in (line.split(':') for line in Path('/proc/self/io').read_text().splitlines())}
    return {'user_cpu_s': r.ru_utime, 'system_cpu_s': r.ru_stime,
            'major_faults': r.ru_majflt, 'minor_faults': r.ru_minflt, **io}


def client(native, database, output, index, readers, barrier, start_at, seconds, read_rate, write_rate, expected, options, monitor):
    Memory = load_sdk(Path(native))
    with Memory(database, **options) as memory:
        session = memory.user('scale').session('pressure')
        user = memory.user('scale', agent_id=f'pressure-{index}', memory_config=LIVE)
        live = user.session('live')
        if index >= 0:
            for name, query in CONCURRENT_QUERIES:
                assert context_digest(session.context(query=query, max_facts=10, max_tokens=1024)) == expected[name]
        dump(Path(output) / f'pid-{index}.json', {'pid': os.getpid()})
        barrier.wait(timeout=180)
        while start_at.value == 0:
            time.sleep(.001)
        start = start_at.value
        # Stagger readers so the target rate does not arrive in synchronized bursts.
        rate = write_rate if index < 0 else read_rate / readers
        period = 1 / rate if rate > 0 else 0
        deadline = start + (index / read_rate if index >= 0 and read_rate else 0)
        first_deadline = deadline
        end = start + seconds
        samples, skipped, revision, generation = [], 0, 1, 0
        before_usage = usage()
        maintenance, next_sample = [], start
        while deadline < end:
            now = time.perf_counter()
            if now >= end:
                break
            if now < deadline:
                time.sleep(deadline - now)
            before = time.perf_counter()
            if before >= end:
                break
            if index < 0:
                generation += 1
                record = user.remember({'generation': generation, 'mirror': generation},
                                       key='pulse', expected_revision=revision)
                revision = record['revision']
                elapsed = (time.perf_counter() - before) * 1000
                wal = Path(database + '-wal')
                samples.append({'ms': elapsed, 'lag_ms': (before-deadline)*1000,
                                'at_s': before-start, 'wal_file_bytes': wal.stat().st_size if wal.exists() else 0})
            else:
                name, query = CONCURRENT_QUERIES[(len(samples) + index) % len(CONCURRENT_QUERIES)]
                result = session.context(query=query, max_facts=10, max_tokens=1024)
                elapsed = (time.perf_counter() - before) * 1000
                assert context_digest(result) == expected[name]
                snapshot = live.context(query='pulse', max_facts=1)
                value = snapshot.memories[0]['value']
                assert value['generation'] == value['mirror']
                assert snapshot.report['pools']['selected'][0]['revision'] == value['generation'] + 1
                samples.append({'query': name, 'ms': elapsed, 'lag_ms': (before-deadline)*1000,
                                'at_s': before-start})
            if monitor and before >= next_sample:
                maintenance.append({'at_s':before-start,'status':memory.storage_status()})
                next_sample = before + 1
            if period:
                deadline += period
                now = min(time.perf_counter(), end)
                if deadline < now:
                    missed = math.ceil((now-deadline)/period)
                    skipped += missed
                    deadline += missed*period
            else:
                deadline = time.perf_counter()
        after_usage = usage()
        attempted = len(samples)
        offered = math.ceil((end-first_deadline)/period) if period else attempted
        result = {'pid': os.getpid(), 'index': index, 'samples': samples,
                  'offered_slots': offered, 'missed_slots': offered-attempted,
                  'start_monotonic': start, 'finish_monotonic': time.perf_counter(),
                  'resources': {k: after_usage[k]-v for k,v in before_usage.items()},
                  'latency': stats([s['ms'] for s in samples]),
                  'native_sha256': sha(Path(native))}
        if monitor:
            result['maintenance_samples'] = maintenance
            result['final_maintenance'] = memory.storage_status()
        result['windows'] = [
            {'from_s':offset,'to_s':min(seconds,offset+60),
             'latency':stats([s['ms'] for s in samples if offset <= s['at_s'] < offset+60]),
             'wal_max_bytes':max((s.get('wal_file_bytes',0) for s in samples if offset <= s['at_s'] < offset+60),default=0)}
            for offset in range(0,math.ceil(seconds),60)]
    dump(Path(output) / f'client-{index}.json', result)
    return result


def pin_reader(database, output, barrier, start_at, at, seconds):
    barrier.wait(timeout=180)
    while start_at.value == 0:
        time.sleep(.001)
    start = start_at.value
    time.sleep(max(0,start+at-time.perf_counter()))
    with sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True) as conn:
        conn.execute('BEGIN')
        query = "SELECT revision,record FROM memweft_pool_facts WHERE tenant_id='default' AND user_id='scale' AND pool_id='pressure-live' AND fact_key='pulse'"
        old = conn.execute(query).fetchone()
        began = time.perf_counter()
        time.sleep(seconds)
        assert conn.execute(query).fetchone() == old
        conn.execute('COMMIT')
        released = time.perf_counter()
        current = conn.execute(query).fetchone()
        assert current[0] > old[0]
    result = {'start_s':began-start,'release_s':released-start,'old_revision':old[0],
              'new_revision':current[0],'snapshot_stable':True}
    dump(Path(output)/'pinned-reader.json',result)
    return result


def worker(a):
    Memory = load_sdk(a.native)
    options = {} if a.background_checkpoint_ms is None else {
        'sqlite_options': {'background_checkpoint_ms': a.background_checkpoint_ms}}
    if a.wal_reclaim_threshold_bytes is not None:
        options['sqlite_options']['wal_reclaim_threshold_bytes'] = a.wal_reclaim_threshold_bytes
    with Memory(str(a.database), **options) as memory:
        memory.user('scale', memory_config=LIVE).remember({'generation': 0, 'mirror': 0}, key='pulse', expected_revision=0)
    expected = {r['name']: r['context_sha256'] for r in json.loads(a.queries.read_text())}
    ctx = multiprocessing.get_context('spawn')
    with ctx.Manager() as manager:
        pin = int(a.pin_reader_seconds > 0)
        barrier, start = manager.Barrier(a.readers+2+pin), manager.Value('d', 0.0)
        with ProcessPoolExecutor(a.readers+1+pin, mp_context=ctx) as executor:
            tasks = [executor.submit(client, str(a.native.resolve()), str(a.database.resolve()), str(a.output.resolve()),
                                     i, a.readers, barrier, start, a.seconds, a.read_rate, a.write_rate, expected, options, a.monitor_maintenance)
                     for i in range(-1, a.readers)]
            held = executor.submit(pin_reader,str(a.database),str(a.output),barrier,start,a.pin_reader_at,a.pin_reader_seconds) if pin else None
            barrier.wait(timeout=180)
            start.value = time.perf_counter() + .2
            clients = [t.result(timeout=a.seconds+300) for t in tasks]
            if held is not None:
                held.result(timeout=30)
    writer, readers = clients[0], clients[1:]
    samples = [s for r in readers for s in r['samples']]
    result = {'seconds': a.seconds, 'readers': a.readers, 'target_read_rate': a.read_rate,
              'target_write_rate': a.write_rate, 'query_latency': stats([s['ms'] for s in samples]),
              'query_scheduled_latency': stats([s['ms']+s['lag_ms'] for s in samples]),
              'query_checks': len(samples), 'live_checks': len(samples),
              'query_rate': len(samples)/a.seconds,
              'read_offered_slots': sum(r['offered_slots'] for r in readers),
              'read_missed_slots': sum(r['missed_slots'] for r in readers),
              'writer': {k:v for k,v in writer.items() if k not in ('samples','maintenance_samples')},
              'writer_wal_max_bytes': max((s['wal_file_bytes'] for s in writer['samples']), default=0),
              'reader_resources': [r['resources'] for r in readers],
              'native_sha256': sha(a.native), 'integrity': integrity(a.database)}
    dump(a.output / 'results.json', result)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--native', required=True, type=Path)
    p.add_argument('--source', type=Path, default=Path('data/evals/bounded-recall-million-run1/private-after/memory.db'))
    p.add_argument('--queries', type=Path, default=Path('data/evals/bounded-recall-million-run1/private-after/queries.json'))
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--seconds', type=float, default=60)
    p.add_argument('--readers', type=int, default=8)
    p.add_argument('--read-rate', type=float, default=200, help='Total primary queries/s; 0 means closed loop.')
    p.add_argument('--write-rate', type=float, default=50)
    p.add_argument('--worker', action='store_true')
    p.add_argument('--database', type=Path)
    p.add_argument('--strace', action='store_true', help='Diagnostic run only: record sync/lock/write syscalls per process.')
    p.add_argument('--background-checkpoint-ms', type=int)
    p.add_argument('--wal-reclaim-threshold-bytes', type=int)
    p.add_argument('--monitor-maintenance', action='store_true')
    p.add_argument('--pin-reader-at', type=float, default=60)
    p.add_argument('--pin-reader-seconds', type=float, default=0)
    a = p.parse_args()
    if a.seconds <= 0 or a.readers < 0 or a.read_rate < 0 or a.write_rate <= 0:
        p.error('invalid duration, reader count or rate')
    if a.wal_reclaim_threshold_bytes is not None and a.background_checkpoint_ms is None:
        p.error('WAL reclamation requires background checkpoint')
    if a.pin_reader_seconds < 0 or a.pin_reader_at < 0 or (a.pin_reader_seconds and a.pin_reader_at+a.pin_reader_seconds >= a.seconds):
        p.error('pinned reader must finish before the workload ends')
    if a.worker:
        worker(a)
        return
    a.output.mkdir(parents=True, exist_ok=False)
    dump(a.output / 'status.json', {'status': 'running'})
    manifest = {k: str(v) if isinstance(v, Path) else v for k,v in vars(a).items()}
    manifest.update({'native_sha256': sha(a.native), 'runner_sha256': sha(Path(__file__)),
                     'helper_sha256': sha(Path(__file__).with_name('benchmark_bounded.py'))})
    dump(a.output / 'manifest.json', manifest)
    for name in ['benchmark_pressure.py', 'benchmark_bounded.py']:
        (a.output / name).write_bytes(Path(__file__).with_name(name).read_bytes())
    try:
        copy_db(a.source, a.output / 'memory.db')
        command = [sys.executable, str(Path(__file__).resolve()), '--worker', '--native', str(a.native.resolve()),
                   '--database', str((a.output/'memory.db').resolve()), '--output', str(a.output.resolve()),
                   '--queries', str(a.queries.resolve()), '--seconds', str(a.seconds), '--readers', str(a.readers),
                   '--read-rate', str(a.read_rate), '--write-rate', str(a.write_rate)]
        if a.strace:
            command = ['strace', '-ff', '-tt', '-T', '-e', 'trace=fdatasync,fsync,pwrite64,fcntl',
                       '-o', str(a.output/'syscalls'), *command]
        if a.background_checkpoint_ms is not None:
            command += ['--background-checkpoint-ms', str(a.background_checkpoint_ms)]
        if a.wal_reclaim_threshold_bytes is not None:
            command += ['--wal-reclaim-threshold-bytes',str(a.wal_reclaim_threshold_bytes)]
        if a.monitor_maintenance:
            command += ['--monitor-maintenance']
        command += ['--pin-reader-at',str(a.pin_reader_at),'--pin-reader-seconds',str(a.pin_reader_seconds)]
        subprocess.run(command, check=True, timeout=a.seconds+600)
        dump(a.output / 'status.json', {'status': 'completed'})
        print(json.dumps(json.loads((a.output/'results.json').read_text()), ensure_ascii=False), flush=True)
    except Exception as error:
        dump(a.output / 'status.json', {'status': 'failed', 'error': str(error)})
        raise


if __name__ == '__main__':
    main()
