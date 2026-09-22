#!/usr/bin/env python3
"""Isolated index research, NOT an SDK backend. Reads the scale fixture read-only.

Compare weighted postings and FTS5 BM25 over identical deduplicated tokens.
The native baseline includes context composition/reporting; prototypes only
return fact triples. Their timings are deliberately reported as different scopes.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import platform
import sqlite3
import statistics
import time


def terms(text):
    result=set(); word=[]; cjk=[]
    def flush():
        if word: result.add(''.join(word)); word.clear()
        if len(cjk)==1: result.add(cjk[0])
        else: result.update(''.join(cjk[i:i+2]) for i in range(len(cjk)-1))
        cjk.clear()
    for char in text.lower():
        n=ord(char)
        if 0x3400<=n<=0x4dbf or 0x4e00<=n<=0x9fff or 0xf900<=n<=0xfaff or 0x20000<=n<=0x323af:
            if word: flush()
            cjk.append(char)
        elif char.isalnum():
            if cjk: flush()
            word.append(char)
        else: flush()
    flush()
    return result


def weights(key,value):
    keys,values=terms(key),terms(value)
    return {term:2*int(term in keys)+int(term in values) for term in keys|values}


def encoded(text): return ' '.join('t'+term.encode().hex() for term in sorted(terms(text)))


def initialize(conn):
    conn.executescript('''
    PRAGMA journal_mode=WAL;
    PRAGMA synchronous=NORMAL;
    CREATE TABLE docs(id INTEGER PRIMARY KEY, scope TEXT, fact_id TEXT, fact_key TEXT, value_json TEXT);
    CREATE INDEX doc_order ON docs(scope,fact_key,fact_id);
    CREATE TABLE postings(scope TEXT,term TEXT,doc_id INTEGER,weight INTEGER,
      PRIMARY KEY(scope,term,doc_id)) WITHOUT ROWID;
    CREATE VIRTUAL TABLE search USING fts5(key_terms,value_terms);
    ''')


def build(conn,rows,scope):
    with conn:
        for index,(fact_id,key,value) in enumerate(rows,1):
            conn.execute('INSERT INTO docs VALUES (?,?,?,?,?)',(index,scope,fact_id,key,value))
            conn.executemany('INSERT INTO postings VALUES (?,?,?,?)',
                [(scope,term,index,weight) for term,weight in weights(key,value).items()])
            conn.execute('INSERT INTO search(rowid,key_terms,value_terms) VALUES (?,?,?)',(index,encoded(key),encoded(value)))


def fill(conn,scope,found,k):
    if len(found)<k:
        ids={r[0] for r in found}
        # <= k positive results can displace at most k prefix records.
        for row in conn.execute('SELECT fact_id,fact_key,value_json FROM docs WHERE scope=? ORDER BY fact_key,fact_id LIMIT ?', (scope,2*k)):
            if row[0] not in ids:
                found.append(row)
                if len(found)==k: break
    return found


def indexed(conn,scope,query,k=10):
    query_terms=sorted(terms(query)); found=[]
    if query_terms:
        placeholders=','.join('?' for _ in query_terms)
        sql=f'''SELECT d.fact_id,d.fact_key,d.value_json,SUM(p.weight) AS score
          FROM postings p JOIN docs d ON d.id=p.doc_id
          WHERE p.scope=? AND p.term IN ({placeholders})
          GROUP BY p.doc_id ORDER BY score DESC,d.fact_key,d.fact_id LIMIT ?'''
        found=[r[:3] for r in conn.execute(sql,[scope,*query_terms,k])]
    return fill(conn,scope,found,k)


def bm25(conn,scope,query,k=10):
    query_terms=sorted(terms(query)); found=[]
    if query_terms:
        match=' OR '.join('"t'+term.encode().hex()+'"' for term in query_terms)
        # The research corpus contains one private scope. The WHERE filter still
        # precedes LIMIT logically; production needs scoped statistics and tests.
        found=list(conn.execute('''SELECT d.fact_id,d.fact_key,d.value_json FROM search
          CROSS JOIN docs d ON d.id=search.rowid
          WHERE search MATCH ? AND rank MATCH 'bm25(2.0,1.0)' AND d.scope=?
          ORDER BY rank,d.fact_key,d.fact_id LIMIT ?''',(match,scope,k)))
    return fill(conn,scope,found,k)


def exhaustive(rows,query,k=10):
    q=terms(query)
    return sorted(rows,key=lambda row:(-sum(weight for term,weight in weights(row[1],row[2]).items() if term in q),row[1],row[0]))[:k]


def measure(fn,repeats):
    fn()  # warm cache and verify before timing
    times=[]; result=None
    for _ in range(repeats):
        start=time.perf_counter();result=fn();times.append((time.perf_counter()-start)*1000)
    return result,{'samples':repeats,'p50_ms':statistics.median(times),'p95_ms':sorted(times)[math.ceil(.95*len(times))-1], 'measurements_ms':times}


def dump(path,value): path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,default=Path('data/evals/enterprise-storage-run1/scale.db'))
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--samples',type=int,default=30)
    p.add_argument('--native-samples',type=int,default=5)
    a=p.parse_args()
    if min(a.samples,a.native_samples)<1:p.error('sample counts must be positive')
    a.output.mkdir(parents=True,exist_ok=False)
    dump(a.output/'status.json',{'status':'running'})
    (a.output/'retrieval_probe.py').write_bytes(Path(__file__).read_bytes())
    try:
        source=sqlite3.connect(a.source.resolve().as_uri()+'?mode=ro',uri=True)
        scopes=source.execute('SELECT DISTINCT tenant_id,user_id,agent_id FROM facts').fetchall()
        if scopes!=[('default','scale','default')]: raise ValueError('expected the single-scope scale fixture')
        # Snapshot read-only source for baseline SDK; schema initialization only
        # touches this copy. Neither experiment mutates the original fixture.
        with sqlite3.connect(a.output/'baseline.db') as copy: source.backup(copy)
        at=int(time.time()*1000)
        rows=source.execute('''SELECT fact_id,fact_key,value_json FROM facts WHERE status='active'
            AND (valid_from IS NULL OR valid_from<=?) AND (valid_to IS NULL OR valid_to>=?)
            ORDER BY fact_key,fact_id''',(at,at)).fetchall()
        source.close()
        if len({r[1] for r in rows})!=len(rows): raise ValueError('prototype corpus requires unique keys')
        scope=json.dumps(scopes[0],separators=(',',':'))
        conn=sqlite3.connect(a.output/'index.db');initialize(conn)
        start=time.perf_counter();build(conn,rows,scope);build_seconds=time.perf_counter()-start
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        from memweft import Memory
        import memweft._core as native
        metadata={'scope':'Private single-scope static corpus only; not a production candidate implementation.',
            'source':str(a.source),'rows':len(rows),'sqlite':sqlite3.sqlite_version,'platform':platform.platform(),
            'corpus_sha256':hashlib.sha256(json.dumps(rows,ensure_ascii=False).encode()).hexdigest(),
            'source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'native_sha256':hashlib.sha256(Path(native.__file__).read_bytes()).hexdigest(),
            'index_build_seconds':build_seconds,'index_bytes':(a.output/'index.db').stat().st_size,
            'storage_by_structure':[dict(zip(('name','bytes'),r)) for r in conn.execute('SELECT name,SUM(pgsize) FROM dbstat GROUP BY name')],
            'postings':conn.execute('SELECT count(*) FROM postings').fetchone()[0],
            'timing_scopes':{'native':'Full SDK context, max_facts=10/max_tokens=1024; includes reporting and rendering',
                 'postings':'Query terms, SQL score/top-k, fetch fact triples, zero-score fill; no context/report',
                 'fts5_bm25':'Same fetch scope; different ranking over deduplicated tokens; not equivalent to Elasticsearch BM25 configuration'},
            'limitations':['warm cache','single reader','static corpus','no shared-pool merge or mutation consistency','Python tokenizer mirror validated only on supplied corpus; not a Rust Unicode equivalence proof','no WAND/MAXSCORE implemented or benchmarked']}
        dump(a.output/'metadata.json',metadata)
        results=[]
        with Memory(str(a.output/'baseline.db')) as memory:
            chat=memory.user('scale').session('query')
            for name,query in [('rare','deployment port'),('frequent','archive'),('mixed','archive deployment port'),('absent','notpresentxyz')]:
                expected=exhaustive(rows,query)
                actual,index_time=measure(lambda:indexed(conn,scope,query),a.samples)
                if actual!=expected: raise AssertionError('postings changed ranking: '+name)
                fts,fts_time=measure(lambda:bm25(conn,scope,query),a.samples)
                baseline,baseline_time=measure(lambda:chat.context(query=query,max_facts=10,max_tokens=1024),a.native_samples)
                if [f['fact_id'] for f in baseline.memories]!=[r[0] for r in expected]: raise AssertionError('reference differs from SDK: '+name)
                matching=conn.execute('SELECT count(DISTINCT doc_id) FROM postings WHERE scope=? AND term IN (SELECT value FROM json_each(?))',(scope,json.dumps(sorted(terms(query))))).fetchone()[0]
                result={'name':name,'query':query,'matching_facts':matching,'postings_exact_ids':True,
                    'native_context':baseline_time,'postings_retrieval':index_time,'fts5_bm25_retrieval':fts_time,
                    'fts5_same_order_as_legacy':[r[0] for r in fts]==[r[0] for r in expected],
                    'legacy_top_ids':[r[0] for r in expected],'fts5_top_ids':[r[0] for r in fts],
                    'native_report_bytes':len(json.dumps(baseline.report).encode())}
                results.append(result);dump(a.output/'results.json',results)
                print(name,{k:round(v['p95_ms'],3) for k,v in result.items() if isinstance(v,dict) and 'p95_ms' in v},flush=True)
        conn.close()
        dump(a.output/'status.json',{'status':'completed'})
    except Exception as e:
        dump(a.output/'status.json',{'status':'failed','error':str(e)});raise

if __name__=='__main__':main()
