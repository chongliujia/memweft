#!/usr/bin/env python3
"""Paired complete SDK context benchmark, with old/new native builds in separate processes."""
import argparse
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import platform
import sqlite3
import statistics
import subprocess
import sys
import time

QUERIES=[('rare','deployment port'),('frequent','archive'),('mixed_terms','archive deployment port'),('absent','notpresentxyz'),('empty',None)]


def dump(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def worker(a):
    spec=importlib.util.spec_from_file_location('memweft._core',a.native.resolve())
    module=importlib.util.module_from_spec(spec);sys.modules['memweft._core']=module;spec.loader.exec_module(module)
    from memweft import Memory
    start=time.perf_counter();memory=Memory(str(a.database));open_ms=(time.perf_counter()-start)*1000
    config=None
    if a.profile=='shared':config={'read_pools':[{'pool_id':'team','access':'read'}],'default_write_pool':None}
    if a.profile=='mixed':config={'read_pools':[{'pool_id':'team','access':'read'},{'pool_id':'private','access':'read_write'}],
                                 'default_write_pool':'private','conflict_policy':'private_first'}
    chat=memory.user('scale',memory_config=config).session('query')
    results=[]
    queries=QUERIES if a.profile=='private' else [('rare','deployment port'),('absent','notpresentxyz')]
    for name,query in queries:
        times=[]; c=None
        for i in range(a.samples+1):
            start=time.perf_counter();c=chat.context(query=query,max_facts=10,max_tokens=1024)
            elapsed=(time.perf_counter()-start)*1000
            if i:times.append(elapsed)
        results.append({'name':name,'query':query,'samples':a.samples,'p50_ms':statistics.median(times),
            'p95_ms':sorted(times)[math.ceil(.95*len(times))-1],'measurements_ms':times,
            'context':{'text':c.text,'memories':c.memories,'messages':c.messages,'strategies':c.strategies},
            'report':c.report if c.report.get('recall',{}).get('retrieval')=='sqlite_inverted_v1' else {
                'recall':{k:v for k,v in c.report['recall'].items() if k!='selected'},
                'omission_count':len(c.report['omissions']),'shadow_count':len(c.report.get('pools',{}).get('shadowed',[]))},
            'report_json_bytes':len(json.dumps(c.report).encode())})
        dump(a.output/'results.json',results)
        print(a.profile,name,round(results[-1]['p95_ms'],3),flush=True)
    memory.close()
    with sqlite3.connect(a.database) as conn:
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        integrity=conn.execute('PRAGMA integrity_check').fetchone()[0]
    if integrity!='ok':raise AssertionError(integrity)
    dump(a.output/'metadata.json',{'native_sha256':sha(a.native),'platform':platform.platform(),'python':sys.version,
         'open_and_migration_ms':open_ms,'database_bytes':a.database.stat().st_size,'integrity_check':integrity,
         'profile':a.profile,'samples':a.samples,'timing':'Complete synchronous Python SDK context including Rust query, rendering and JSON transport; warm cache, single reader'})


def copy_db(source,target):
    with sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True) as old:
        with sqlite3.connect(target) as new:old.backup(new)


def parent(a):
    a.output.mkdir(parents=True,exist_ok=False)
    dump(a.output/'status.json',{'status':'running'})
    dump(a.output/'manifest.json',{'before':str(a.before),'after':str(a.after),'before_sha256':sha(a.before),'after_sha256':sha(a.after),
        'samples':a.samples,'source_database':str(a.source),'script_sha256':sha(Path(__file__)),
        'profile_order':['private','shared','mixed'],'build_order':['before','after'],
        'limitations':'Single-process warm-cache synthetic fixture. No model calls. One timed run per profile/build; chronological order is not randomized.'})
    (a.output/'benchmark_indexed.py').write_bytes(Path(__file__).read_bytes())
    try:
        combined=[]
        for profile in ('private','shared','mixed'):
            fixture=a.output/f'{profile}-fixture.db';copy_db(a.source,fixture)
            with sqlite3.connect(fixture) as conn:
                existing=conn.execute("SELECT count(*) FROM sqlite_master WHERE name='memweft_recall_version'").fetchone()[0]
                if existing:raise ValueError('source must be a legacy fixture, not an indexed database')
                if profile!='private':
                    rows=conn.execute('SELECT tenant_id,user_id,agent_id,fact_id,fact_key,value_json,status,confidence,sources,scope_level,notes FROM facts').fetchall()
                    records=[]
                    for tenant,user,agent,fid,key,value,status,confidence,sources,level,notes in rows:
                        record={'fact_id':fid,'fact_key':key,'value':json.loads(value),'status':status,
                            'validity':{'valid_from':None,'valid_to':None},'confidence':confidence,'sources':json.loads(sources),
                            'scope_level':level,'notes':notes,'pool_id':'team','revision':1,'writer_agent_id':agent}
                        records.append((tenant,user,'team',key,1,json.dumps(record,ensure_ascii=False,separators=(',',':'))))
                    conn.executemany('INSERT INTO memweft_pool_facts VALUES (?,?,?,?,?,?)',records)
                    if profile=='shared':conn.execute('DELETE FROM facts')
                conn.commit()
            pair={}
            for label,native in [('before',a.before),('after',a.after)]:
                out=a.output/f'{profile}-{label}';out.mkdir();db=out/'memory.db';copy_db(fixture,db)
                command=[sys.executable,str(Path(__file__).resolve()),'--worker','--native',str(native.resolve()),
                    '--database',str(db.resolve()),'--profile',profile,'--samples',str(a.samples),'--output',str(out.resolve())]
                with (out/'worker.log').open('w') as log:
                    subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=600)
                pair[label]={'metadata':json.loads((out/'metadata.json').read_text()),'results':json.loads((out/'results.json').read_text())}
            for old,new in zip(pair['before']['results'],pair['after']['results'],strict=True):
                if old['name']!=new['name'] or old['context']!=new['context']:raise AssertionError('context changed: '+profile+'/'+old['name'])
                if new['report']['recall']['inspected_facts']>74:raise AssertionError('unbounded candidates')
                combined.append({'profile':profile,'query':new['name'],'context_identical':True,
                    'before_p50_ms':old['p50_ms'],'after_p50_ms':new['p50_ms'],
                    'before_p95_ms':old['p95_ms'],'after_p95_ms':new['p95_ms'],
                    'p95_ratio':old['p95_ms']/new['p95_ms'],
                    'before_report_bytes':old['report_json_bytes'],'after_report_bytes':new['report_json_bytes'],
                    'loaded_candidates':new['report']['recall']['inspected_facts']})
            dump(a.output/'comparison.json',combined)
            print(profile,'context equivalence verified',flush=True)
        dump(a.output/'status.json',{'status':'completed'})
    except Exception as e:
        dump(a.output/'status.json',{'status':'failed','error':str(e)});raise


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--samples',type=int,default=30)
    p.add_argument('--source',type=Path,default=Path('data/evals/enterprise-storage-run1/scale.db'))
    p.add_argument('--before',type=Path,default=Path('data/evals/retrieval-before-build/libmemweft_ffi.so'))
    p.add_argument('--after',type=Path,default=Path('target/release/libmemweft_ffi.so'))
    p.add_argument('--worker',action='store_true');p.add_argument('--native',type=Path);p.add_argument('--database',type=Path)
    p.add_argument('--profile',choices=('private','shared','mixed'),default='private')
    a=p.parse_args()
    if a.samples<1:p.error('samples must be positive')
    if a.worker:worker(a)
    else:parent(a)

if __name__=='__main__':main()
