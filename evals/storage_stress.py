#!/usr/bin/env python3
"""File-backed native SDK integrity and scale probes, no external service required."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
from pathlib import Path
import platform
import resource
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
from memweft import Memory
import memweft._core as native
from run_local import dump


def config(*, writable=True, mixed=False):
    pools = ([{'pool_id':'private','access':'read_write'}] if mixed else [])
    pools += [{'pool_id':'team','access':'read_write' if writable else 'read'}]
    return {'read_pools':pools,'default_write_pool':'private' if mixed else 'team' if writable else None}


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def stats(times):
    return {'n':len(times),'p50_ms':statistics.median(times),
            'p95_ms':sorted(times)[math.ceil(.95*len(times))-1],'max_ms':max(times)}


def concurrent_updates(path, workers, rounds):
    stores = [Memory(str(path)) for _ in range(workers)]
    users = [s.user('counter',agent_id=f'writer-{i}',memory_config=config()) for i,s in enumerate(stores)]
    users[0].remember(0,key='counter',expected_revision=0)
    barrier = threading.Barrier(workers, timeout=30)
    def increment(i):
        conflicts = 0
        latencies = []
        for _ in range(rounds):
            old = users[i].memories()[0]
            # All writers observe the same revision before attempting the CAS.
            barrier.wait()
            start = time.perf_counter()
            try:
                users[i].remember(old['value']+1,key='counter',expected_revision=old['revision'])
                success = 1
            except ValueError as error:
                if str(error) != 'conflict: pool team key counter changed':
                    raise
                conflicts += 1
                success = 0
            latencies.append((time.perf_counter()-start)*1000)
            barrier.wait()
            yield success, conflicts, latencies[-1]
    def worker(i):
        return list(increment(i))
    try:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(worker,range(workers)))
        for r in range(rounds):
            require(sum(rows[r][0] for rows in results)==1, f'CAS round {r} did not have exactly one winner')
        final = users[0].memories()[0]
        require(final['value']==rounds and final['revision']==rounds+1,'lost update or wrong revision')
        # Concurrent event retries use an identical event ID; all writes must return
        # the same persisted event, rather than duplicate chat history.
        def retry(i):
            for _ in range(20):
                stores[i].user('events',agent_id='same-agent').session('s').add_message('user','once',event_id='retry-event')
        with ThreadPoolExecutor(max_workers=workers) as executor:
            list(executor.map(retry,range(workers)))
        messages = stores[0].user('events',agent_id='same-agent').session('s').messages()
        require(len(messages)==1,'concurrent retry duplicated message')
        return {'workers':workers,'rounds':rounds,'attempts':workers*rounds,'successful_updates':rounds,
            'expected_conflicts':sum(r[-1][1] for r in results),'final_value':final['value'],
            'final_revision':final['revision'],'write_latency':stats([r[2] for rows in results for r in rows]),
            'idempotent_message_attempts':workers*20,'persisted_messages':len(messages)}
    finally:
        for s in stores: s.close()


def isolation(path, scopes=100):
    with Memory(str(path)) as memory:
        for i in range(scopes):
            writer = memory.user(f'user-{i%10}',tenant_id=f'tenant-{i//10}',agent_id='writer',memory_config=config(mixed=True))
            writer.remember(f'shared-canary-{i}',key='shared',pool_id='team')
            writer.remember(f'private-canary-{i}',key='private')
            writer.session('same-session').add_message('user',f'session-canary-{i}',event_id='same-event')
        blocked = 0
        for i in range(scopes):
            reader = memory.user(f'user-{i%10}',tenant_id=f'tenant-{i//10}',agent_id='reader',memory_config=config(writable=False))
            facts = reader.memories()
            require(len(facts)==1 and facts[0]['value']==f'shared-canary-{i}', f'scope leakage at {i}')
            require(reader.session('same-session').messages()==[], 'session leaked across agents')
            for operation in (lambda:reader.remember('overwrite',key='shared',pool_id='team'),
                              lambda:reader.forget('shared',pool_id='team')):
                try: operation()
                except ValueError as e:
                    require(str(e)=='invalid input: pool is not bound for writing','unexpected readonly error: '+str(e))
                    blocked += 1
                else: raise AssertionError('read-only binding allowed mutation')
    return {'tenant_user_scopes':scopes,'agents_per_scope':2,'private_and_session_leaks':0,'readonly_mutations_blocked':blocked,
            'boundary':'Application scope/config binding, not authentication against an untrusted database client.'}


def abrupt_exit(path):
    # Kill only this isolated writer after acknowledged operations. This verifies
    # process-crash recovery, not loss of OS page cache or power-failure durability.
    code = '''import sys, time
from memweft import Memory
m=Memory(sys.argv[1]); u=m.user('crash')
for i in range(100000):
 u.remember(i,key=f'ack-{i}')
 print(i,flush=True)
 time.sleep(.002)
'''
    process = subprocess.Popen([sys.executable,'-u','-c',code,str(path)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    watchdog = threading.Timer(30, process.kill)
    watchdog.start()
    acknowledged = []
    try:
        for _ in range(50):
            line = process.stdout.readline()
            if not line:
                raise RuntimeError('crash writer exited: '+process.stderr.read())
            acknowledged.append(int(line))
    finally:
        watchdog.cancel()
        process.kill()
        process.communicate(timeout=10)
    with Memory(str(path)) as memory:
        values = {f['fact_key']:f['value'] for f in memory.user('crash').memories()}
        require(all(values.get(f'ack-{i}')==i for i in acknowledged),'acknowledged commit lost after process kill')
    with sqlite3.connect(path) as conn:
        check = conn.execute('PRAGMA integrity_check').fetchone()[0]
    require(check=='ok','SQLite integrity failure')
    return {'acknowledged':len(acknowledged),'recovered':len(values),'integrity_check':check,'scope':'SIGKILL recovery; no simulated power loss'}


def scale(path, sizes, samples):
    rows = []
    with Memory(str(path)) as memory:
        u = memory.user('scale')
        u.remember('The deployment port is 17443.',key='z_deployment_port')
        count = 1
        for size in sizes:
            start = time.perf_counter()
            while count < size:
                u.remember(f'Archived inactive document {count:08d}; owner archive-{count%100}.',key=f'a_archive_{count:08d}')
                count += 1
            seed_ms = (time.perf_counter()-start)*1000
            times=[]
            for _ in range(samples):
                start = time.perf_counter()
                context = u.session('query').context(query='deployment port',max_facts=10,max_tokens=1024)
                times.append((time.perf_counter()-start)*1000)
                require(context.memories[0]['fact_key']=='z_deployment_port','target recall failed')
                require(context.report['estimated_tokens']<=1024,'budget invariant failed')
                recall = context.report['recall']
                if recall.get('retrieval') == 'sqlite_inverted_v1':
                    require(recall['inspected_facts']<=min(size,74),'candidate bound exceeded')
                    require(len(context.report['omissions'])<=64,'diagnostic bound exceeded')
                    require(recall['candidates_truncated']==(size>74),'incorrect candidate completeness flag')
                else:
                    require(recall['inspected_facts']==size,'unexpected scan size')
            row = {'facts':size,'added_facts_ms':seed_ms,'query':stats(times),
                'inspected_facts':context.report['recall']['inspected_facts'],
                'retrieval':context.report['recall'].get('retrieval','full_scan'),
                'report_json_bytes':len(json.dumps(context.report).encode()),
                'selected_facts':len(context.memories), 'peak_process_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                'database_bytes':path.stat().st_size, 'wal_bytes':Path(str(path)+'-wal').stat().st_size if Path(str(path)+'-wal').exists() else 0}
            rows.append(row)
            print(json.dumps(row),flush=True)
    with Memory(str(path)) as memory:
        require(len(memory.user('scale').memories())==sizes[-1],'restart lost records')
    return rows


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--sizes',type=int,nargs='+',default=[100,1000,10000,100000])
    p.add_argument('--samples',type=int,default=30)
    p.add_argument('--workers',type=int,default=8)
    p.add_argument('--rounds',type=int,default=100)
    a=p.parse_args()
    if a.sizes!=sorted(set(a.sizes)) or min(a.sizes)<1 or min(a.samples,a.workers,a.rounds)<1:
        p.error('positive increasing sizes and positive sample/worker/round counts required')
    a.output.mkdir(parents=True,exist_ok=False)
    dump(a.output/'status.json',{'status':'running'})
    dump(a.output/'metadata.json',{'platform':platform.platform(),'python':sys.version,
         'native_sha256':hashlib.sha256(Path(native.__file__).read_bytes()).hexdigest(),
         'source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
         'sqlite_durability':'WAL, synchronous=NORMAL as configured by the Rust store',
         'profile':'release (caller must install the release native extension)'} )
    (a.output/'storage_stress.py').write_bytes(Path(__file__).read_bytes())
    try:
        result = {'concurrency':concurrent_updates(a.output/'concurrency.db',a.workers,a.rounds)}
        dump(a.output/'results.json',result)
        result['isolation']=isolation(a.output/'isolation.db')
        dump(a.output/'results.json',result)
        result['process_crash']=abrupt_exit(a.output/'crash.db')
        dump(a.output/'results.json',result)
        result['scale']=scale(a.output/'scale.db',a.sizes,a.samples)
        dump(a.output/'results.json',result)
        dump(a.output/'status.json',{'status':'completed'})
    except Exception as e:
        dump(a.output/'status.json',{'status':'failed','error':str(e)})
        raise

if __name__=='__main__': main()
