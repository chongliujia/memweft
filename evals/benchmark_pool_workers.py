#!/usr/bin/env python3
"""macOS-only retired-worker probe; run each native build in a fresh process.

Runs 120 short-lived in-memory stores; ps observes only this process's threads.
No API calls. Timings are descriptive, not throughput or shutdown guarantees.
"""
import hashlib,json,os,subprocess,time
from pathlib import Path
from memweft import Memory
import memweft._core as native

def threads():
 text=subprocess.check_output(['ps','-M','-p',str(os.getpid())],text=True)
 return len(text.splitlines())-1

before=threads();start=time.perf_counter()
for _ in range(120):
 with Memory(in_memory=True) as memory:
  memory.user('probe').remember('ok',key='value')
elapsed=(time.perf_counter()-start)*1000
after=threads();time.sleep(1)
print(json.dumps({'stores':120,'threads_before':before,'threads_after_close':after,'threads_after_1_second':threads(),
 'elapsed_ms':elapsed,'native_sha256':hashlib.sha256(Path(native.__file__).read_bytes()).hexdigest(),
 'measurement':'macOS ps -M rows; sequential in-memory stores, no model calls; descriptive single sample'}))
