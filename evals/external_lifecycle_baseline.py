#!/usr/bin/env python3
"""Pinned Mem0 OSS structured-CRUD baseline, without extraction or retrieval scoring.

Install mem0ai==2.2.1 and its dependencies in a separate Python environment. This
adapter uses Mem0's actual add(infer=False), update, get_all, get, delete and
history APIs with local Qdrant persistence and real local MiniLM embeddings.
No hosted Mem0 service, LLM response, semantic search, entity linking, dependency
invalidation, atomic publication or native rollback is claimed by this track.

The adapter adds (kind, key) addressing and JSON serialization. Those addresses
are application metadata, not native Mem0 source-dependency semantics. An unused
LLM client is configured with a literal non-secret and its generate method raises
if called. Telemetry is disabled before Mem0 import. The optional fixture reports
source deletion separately from retained audit history.
"""
from __future__ import annotations

import argparse
from functools import lru_cache
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import time

MEM0_VERSION = '2.2.1'
MEM0_WHEEL_SHA256 = 'fe91bb91ac8926231993a4aa58df00a60c6c74c709e6a338fe399500776eef4d'
DEFAULT_MODEL = Path.home() / '.cache/torch/sentence_transformers/sentence-transformers_all-MiniLM-L12-v2'


def strategy_text(settings):
    return '先前验证的配置建议：' + json.dumps(settings, ensure_ascii=False, sort_keys=True)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


@lru_cache(maxsize=4)
def _model_manifest(path):
    root = Path(path)
    names = ('pytorch_model.bin', 'config.json', 'modules.json', 'tokenizer.json',
             'tokenizer_config.json', 'special_tokens_map.json', 'vocab.txt',
             'config_sentence_transformers.json', 'sentence_bert_config.json',
             '1_Pooling/config.json')
    hashes = {}
    for name in names:
        target = root / name
        if not target.is_file():
            raise FileNotFoundError(f'Local embedding model file is missing: {name}')
        digest = hashlib.sha256()
        with target.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(block)
        hashes[name] = digest.hexdigest()
    return {'name': 'sentence-transformers/all-MiniLM-L12-v2', 'dimensions': 384,
            'device': 'cpu', 'local_files_sha256': hashes}


class Mem0StructuredStore:
    """Small sequential fixture store; not a concurrent transactional adapter.

    Scoped exact listing is bounded to 10,000 records and raises at that limit.
    (kind,key) uniqueness is checked by this adapter, not atomically by Mem0.
    """
    def __init__(self, path, scope, embedding_model_path=None):
        if not isinstance(scope, str) or not scope.strip():
            raise ValueError('scope must be a nonempty string')
        if importlib.metadata.version('mem0ai') != MEM0_VERSION:
            raise RuntimeError(f'This baseline requires mem0ai=={MEM0_VERSION}')
        self.path, self.scope = Path(path).resolve(), scope
        self.path.mkdir(parents=True, exist_ok=True)
        model_path = Path(embedding_model_path or os.environ.get('MEMWEFT_BASELINE_EMBEDDING_PATH') or DEFAULT_MODEL).resolve()
        self.model_manifest = _model_manifest(str(model_path))
        os.environ['MEM0_TELEMETRY'] = 'false'
        os.environ['MEM0_DIR'] = str(self.path / 'mem0-config')
        os.environ['HF_HUB_OFFLINE'] = '1'
        os.environ['TRANSFORMERS_OFFLINE'] = '1'
        os.environ['TRANSFORMERS_CACHE'] = str(self.path / 'transformers-cache')
        os.environ['TOKENIZERS_PARALLELISM'] = 'false'
        from mem0 import Memory
        from mem0.memory import telemetry
        if telemetry.MEM0_TELEMETRY:
            raise RuntimeError('Mem0 was already imported with telemetry enabled; use a fresh subprocess')
        begin = time.perf_counter()
        self.memory = Memory.from_config({
            'vector_store': {'provider': 'qdrant', 'config': {
                'collection_name': 'structured_lifecycle',
                'path': str(self.path / 'qdrant'), 'embedding_model_dims': 384,
                'on_disk': True}},
            'embedder': {'provider': 'huggingface', 'config': {
                'model': str(model_path), 'embedding_dims': 384,
                'model_kwargs': {'device': 'cpu'}}},
            'llm': {'provider': 'openai', 'config': {
                'model': 'unused-structured-crud', 'api_key': 'unused-local-only',
                'openai_base_url': 'http://127.0.0.1:1/v1'}},
            'history_db_path': str(self.path / 'history.sqlite'),
        })
        self.llm_attempts = 0
        def reject_llm(*args, **kwargs):
            self.llm_attempts += 1
            raise AssertionError('Structured baseline unexpectedly attempted LLM inference')
        self.memory.llm.generate_response = reject_llm
        self.init_ms = (time.perf_counter() - begin) * 1000
        self.operations = []
        self.closed = False

    def _call(self, operation, fn):
        begin = time.perf_counter()
        try:
            value = fn()
        except Exception as exc:
            self.operations.append({'operation': operation, 'ms': (time.perf_counter() - begin) * 1000,
                                    'ok': False, 'error_type': type(exc).__name__})
            raise
        self.operations.append({'operation': operation, 'ms': (time.perf_counter() - begin) * 1000,
                                'ok': True})
        return value

    def _rows(self, kind, key=None):
        filters = {'user_id': self.scope, 'fixture_kind': kind}
        if key is not None:
            filters['fixture_key'] = key
        rows = self._call('mem0.get_all', lambda: self.memory.get_all(filters=filters, top_k=10000))['results']
        if len(rows) >= 10000:
            raise RuntimeError('Fixture scope exceeds the explicitly supported listing bound')
        return rows

    def _row(self, kind, key):
        rows = self._rows(kind, key)
        if len(rows) > 1:
            raise RuntimeError('Duplicate fixture address; concurrent adapter use is unsupported')
        return rows[0] if rows else None

    def put(self, kind, key, value):
        text = _json(value)
        row = self._row(kind, key)
        metadata = {'fixture_kind': kind, 'fixture_key': key}
        if row:
            self._call('mem0.update', lambda: self.memory.update(row['id'], text=text, metadata=metadata))
            return row['id']
        result = self._call('mem0.add(infer=False)', lambda: self.memory.add(
            text, user_id=self.scope, metadata=metadata, infer=False))
        if len(result['results']) != 1:
            raise AssertionError('Mem0 infer=False must preserve one structured record')
        return result['results'][0]['id']

    def get(self, kind, key):
        row = self._row(kind, key)
        if row is None:
            return None
        result = self._call('mem0.get', lambda: self.memory.get(row['id']))
        return json.loads(result['memory'])

    def delete(self, kind, key):
        row = self._row(kind, key)
        if row is None:
            return False
        self._call('mem0.delete', lambda: self.memory.delete(row['id']))
        return True

    def list(self, kind):
        rows = self._rows(kind)
        return [json.loads(r['memory']) for r in sorted(rows, key=lambda r: r['metadata']['fixture_key'])]

    def history(self, memory_id):
        return self._call('mem0.history', lambda: self.memory.history(memory_id))

    def manifest(self):
        packages = ('mem0ai', 'qdrant-client', 'sentence-transformers', 'transformers',
                    'torch', 'numpy', 'huggingface-hub', 'pydantic', 'openai')
        return {'system': 'Mem0 OSS', 'track': 'structured CRUD with exact metadata addressing',
                'python': platform.python_version(),
                'packages': {p: importlib.metadata.version(p) for p in packages},
                'mem0_official_wheel_sha256': MEM0_WHEEL_SHA256,
                'embedding': self.model_manifest, 'telemetry': False,
                'llm_calls': 0, 'llm_attempts': self.llm_attempts,
                'semantic_search_calls': 0, 'extraction': False, 'init_ms': self.init_ms,
                'adapter_features': ['JSON serialization', 'scope/kind/key addressing',
                                     'sequential update-or-add', 'explicit Qdrant client close'],
                'outside_common_contract': ['atomic source/derivative invalidation',
                                            'atomic publication', 'native strategy rollback',
                                            'hard erasure of history']}

    def close(self):
        if self.closed:
            return
        self.memory.close()
        # Mem0 2.2.1 closes history SQLite only; release the local Qdrant lock too.
        self.memory.vector_store.client.close()
        self.memory.llm.client.close()
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def run_fixture(spec, directory, embedding_model_path=None):
    """Actual native CRUD, closed/reopened before the final current read.

    Inputs are public synthetic structured sources, not extracted documents. The
    optional publication/rollback schedules are explicit application emulations.
    """
    directory = Path(directory)
    if directory.exists() and any(directory.iterdir()):
        raise ValueError('Use a fresh output directory; fixture must not reuse state')
    directory.mkdir(parents=True, exist_ok=True)
    scope, event = spec['case_id'], spec['event']
    if event not in {'update', 'forget', 'delete_recreate', 'concurrent_update', 'rollback', 'unrelated_update'}:
        raise ValueError(f'Unsupported fixture event: {event}')
    old = {'version': 'v1', 'content': strategy_text(spec['old']), 'dependencies': {'policy': 1}}
    notes, operations = [], []
    with Mem0StructuredStore(directory / 'db', scope, embedding_model_path) as store:
        source_id = store.put('source', 'policy', {'value': spec['old'], 'revision': 1})
        store.put('source', 'archive', {'value': 'archival label a', 'revision': 1})
        strategy_id = store.put('strategy', 'current', old)
        initial = {'source': store.get('source', 'policy'), 'active': store.get('strategy', 'current')}
        if event in {'forget', 'delete_recreate'}:
            store.delete('source', 'policy')
        if event not in {'forget', 'unrelated_update'}:
            # Revisions are application metadata supplied by the common schedule;
            # Mem0 does not natively create these monotonic source revisions.
            revision = 3 if event == 'delete_recreate' else 2
            store.put('source', 'policy', {'value': spec['new'], 'revision': revision})
        if event == 'unrelated_update':
            store.put('source', 'archive', {'value': 'archival label b', 'revision': 2})
        if event == 'concurrent_update':
            store.put('strategy', 'current', old)
            notes.append('Publication after source update is application CRUD emulation, not native atomic publication')
        if event == 'rollback':
            store.put('strategy', 'current', {'version': 'v2', 'content': strategy_text(spec['new']),
                                             'dependencies': {'policy': 2}})
            store.put('strategy', 'current', old)
            notes.append('Choosing the old strategy is application CRUD emulation, not native rollback')
        history = store.history(source_id)
        manifest = store.manifest()
        operations.extend(store.operations)
    with Mem0StructuredStore(directory / 'db', scope, embedding_model_path) as store:
        current = store.get('source', 'policy')
        active = store.get('strategy', 'current')
        operations.extend(store.operations)
        reopen_manifest = store.manifest()
    stale = active is not None and (current is None or active['dependencies']['policy'] != current['revision'])
    result = {'case_id': scope, 'event': event, 'initial': initial, 'current_source': current,
              'active': active, 'stale_returned': stale, 'source_memory_id': source_id,
              'strategy_memory_id': strategy_id, 'source_history': history,
              'operations': operations, 'manifest': manifest,
              'reopen_init_ms': reopen_manifest['init_ms'], 'notes': notes,
              'source_update_did_not_implicitly_rewrite_separate_strategy': active == old,
              'blocked_lifecycle_operation': None}
    (directory / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture-spec', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--embedding-model-path', type=Path)
    args = parser.parse_args()
    result = run_fixture(json.loads(args.fixture_spec.read_text()), args.output, args.embedding_model_path)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == '__main__':
    main()
