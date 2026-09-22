import asyncio
import unittest
import tempfile
from pathlib import Path
import selectors
import subprocess
import sys
import os
import json
import time
from memweft import Memory, AsyncMemory


def config(*, writable=True, mixed=False):
    pools = [{"pool_id": "team", "access": "read_write" if writable else "read"}]
    if mixed:
        pools.append({"pool_id": "private", "access": "read_write"})
    return {"read_pools": pools, "default_write_pool": "private" if mixed else ("team" if writable else None)}


class PoolTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'posix', 'requires POSIX process kill and symlink')
    def test_database_maintenance_leader_crash_and_alias_takeover(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'leader.db'
            code = '''import json,sys,time
from memweft import Memory
m=Memory(sys.argv[1],sqlite_options={"background_checkpoint_ms":100,"wal_reclaim_threshold_bytes":65536})
until=time.monotonic()+10
while m.storage_status()["checkpoint"]["progress"]["coordinator_role"]!="leader":
    if time.monotonic()>until: raise RuntimeError("no leader")
    time.sleep(.01)
print(json.dumps(m.storage_status()),flush=True)
for line in sys.stdin:
    print(json.dumps(m.storage_status()),flush=True)
'''
            process = subprocess.Popen([sys.executable, '-c', code, str(path)],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True, bufsize=1)
            def response():
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    self.assertTrue(selector.select(timeout=15), 'leader did not respond')
                line = process.stdout.readline()
                self.assertTrue(line, 'leader exited unexpectedly')
                return json.loads(line)['checkpoint']['progress']
            try:
                initial = response()
                alias = Path(directory) / 'alias.db'
                alias.symlink_to(path)
                options = {'background_checkpoint_ms': 100, 'wal_reclaim_threshold_bytes': 65536}
                with Memory(str(alias), sqlite_options=options) as follower:
                    def wait_role(role):
                        until = time.monotonic() + 10
                        while time.monotonic() < until:
                            state = follower.storage_status()['checkpoint']['progress']
                            if state['coordinator_role'] == role:
                                return state
                            time.sleep(.01)
                        self.fail(f'maintenance did not become {role}: {state}')
                    state = wait_role('follower')
                    self.assertEqual(state['passive_runs'], 0)
                    follower.user('u').remember('committed by follower', key='external')
                    until = time.monotonic() + 10
                    while True:
                        process.stdin.write('status\n'); process.stdin.flush()
                        state = response()
                        if state['passive_runs'] > initial['passive_runs']:
                            break
                        self.assertLess(time.monotonic(), until, 'leader missed external commit')
                        time.sleep(.02)
                    self.assertEqual(follower.storage_status()['checkpoint']['progress']['passive_runs'], 0)
                    process.kill(); process.wait(timeout=10)
                    state = wait_role('leader')
                    self.assertEqual(state['leadership_acquisitions'], 1)
                    self.assertEqual(follower.user('u').memories()[0]['value'], 'committed by follower')
                    self.assertEqual(len(list(Path(directory).glob('*.memweft-maintenance'))), 1)
            finally:
                if process.poll() is None:
                    process.kill(); process.wait(timeout=10)
                for stream in [process.stdin, process.stdout, process.stderr]:
                    stream.close()

    @unittest.skipUnless(os.name == 'posix', 'requires POSIX process kill')
    def test_background_checkpoint_process_crash_recovers_acknowledged_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'crash.db')
            code = '''import sys,time
from memweft import Memory
m=Memory(sys.argv[1],sqlite_options={"background_checkpoint_ms":60000})
u=m.user("crash")
for i in range(50):
    u.remember(i,key=f"key-{i}")
    print(i,flush=True)
time.sleep(60)
'''
            process = subprocess.Popen([sys.executable, '-c', code, path], stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True, bufsize=1)
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    # Each line is a commit acknowledgement. Reading the raw descriptor
                    # avoids TextIO buffering hiding ready lines from select().
                    acknowledged = b''
                    while acknowledged.count(b'\n') < 50:
                        self.assertTrue(selector.select(timeout=30), 'writer stopped acknowledging')
                        chunk = os.read(process.stdout.fileno(), 4096)
                        self.assertTrue(chunk, 'writer exited before all acknowledgements')
                        acknowledged += chunk
                self.assertEqual(acknowledged.splitlines(), [str(i).encode() for i in range(50)])
                self.assertGreater(Path(path+'-wal').stat().st_size, 0)
                process.kill()
                process.wait(timeout=10)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
                process.stdout.close()
                process.stderr.close()
            with Memory(path) as recovered:
                facts = recovered.user('crash').memories()
                self.assertEqual({f['value'] for f in facts}, set(range(50)))

    def test_background_checkpoint_options_and_shared_visibility(self):
        for options in [{"background_checkpoint_ms": 0}, {"unknown": 1}]:
            with self.assertRaises(ValueError):
                Memory(in_memory=True, sqlite_options=options)
        with self.assertRaises(ValueError):
            Memory(in_memory=True, sqlite_options={"background_checkpoint_ms": 100})
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'memory.db')
            options = {"background_checkpoint_ms": 100, "wal_reclaim_threshold_bytes": 65536}
            with Memory(path, sqlite_options=options) as writer, Memory(path, sqlite_options=options) as reader:
                status = writer.storage_status()
                self.assertEqual(status['sqlite_version'], '3.51.3')
                self.assertEqual(status['checkpoint']['wal_reclaim_threshold_bytes'], 65536)
                self.assertEqual(status['checkpoint']['reclaim_busy_timeout_ms'], 0)
                a = writer.user('u', memory_config=config())
                b = reader.user('u', agent_id='reader', memory_config=config(writable=False))
                a.remember('old', key='key', expected_revision=0)
                self.assertEqual(b.session('s').context().memories[0]['value'], 'old')
                a.remember('new', key='key', expected_revision=1)
                self.assertEqual(b.session('s').context().memories[0]['value'], 'new')
                with self.assertRaises(ValueError):
                    a.remember('stale', key='key', expected_revision=1)
                a.forget('key', expected_revision=2)
                self.assertEqual(b.session('s').context().memories, [])
            async def reopen():
                m = AsyncMemory(path, sqlite_options=options)
                try:
                    self.assertEqual((await m.storage_status())['checkpoint']['mode'], 'background')
                    u = m.user('u', memory_config=config())
                    record = await u.remember('recreated', key='key', expected_revision=0)
                    self.assertEqual(record['revision'], 4)
                finally:
                    m.close()
            asyncio.run(reopen())

    def test_mixed_config_sources_permissions_and_targeted_delete(self):
        with Memory(in_memory=True) as memory:
            a = memory.user("alice", agent_id="planner", memory_config=config(mixed=True))
            b = memory.user("alice", agent_id="executor", memory_config=config(writable=False))
            record = a.remember(8002, key="port", pool_id="team", expected_revision=0)
            self.assertEqual(record["revision"], 1)
            self.assertEqual(b.memories()[0]["writer_agent_id"], "planner")
            a.remember(9000, key="port")
            context = a.session("s").context(query="port")
            self.assertEqual(context.memories[0]["value"], 9000)
            self.assertEqual(context.explain()["pools"]["selected"][0]["pool_id"], "private")
            self.assertEqual(a.memories(pool_id="team")[0]["value"], 8002)
            for action in [lambda: b.remember(1, key="port", pool_id="team"),
                           lambda: b.forget("port", pool_id="team"),
                           lambda: b.memories(pool_id="private")]:
                with self.assertRaises(ValueError):
                    action()
            with self.assertRaises(ValueError):
                a.remember(1, key="port", pool_id="team", expected_revision=7)
            self.assertTrue(a.forget("port"))
            self.assertEqual(a.memories()[0]["value"], 8002)
            self.assertTrue(a.forget("port", pool_id="team", expected_revision=1))
            self.assertEqual(b.memories(), [])

    def test_async_pool_configuration_and_revision_options(self):
        async def run():
            async with AsyncMemory(in_memory=True) as memory:
                a = memory.user("alice", agent_id="planner", memory_config=config())
                b = memory.user("alice", agent_id="executor", memory_config=config())
                record = await a.remember("brief", key="style", expected_revision=0)
                self.assertEqual(record["pool_id"], "team")
                self.assertEqual((await b.memories(pool_id="team"))[0]["value"], "brief")
                await a.session("s").add_message("user", "private")
                self.assertEqual(await b.session("s").messages(), [])
                self.assertEqual((await b.session("s").context()).explain()["pools"]["selected"][0]["revision"], 1)
                self.assertTrue(await b.forget("style", expected_revision=1))
                self.assertEqual(await a.memories(), [])
        asyncio.run(run())
