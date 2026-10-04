"""Opt-in integration checks for the actual pinned external library, no fakes."""
import json
import os
from pathlib import Path
import tempfile
import unittest

from external_lifecycle_baseline import Mem0StructuredStore, run_fixture, strategy_text


@unittest.skipUnless(os.environ.get('MEMWEFT_TEST_EXTERNAL_BASELINE') == '1',
                     'requires dedicated pinned Mem0 environment and real local embedding model')
class ExternalLifecycleIntegration(unittest.TestCase):
    def test_update_delete_reopen_scope_and_history(self):
        with tempfile.TemporaryDirectory() as root:
            with Mem0StructuredStore(root, 'owner') as store:
                source = store.put('source', 'policy', {'value': 8000, 'revision': 1})
                strategy = {'content': 'use port 8000', 'dependencies': {'policy': 1}}
                store.put('strategy', 'current', strategy)
                self.assertEqual(store.put('source', 'policy', {'value': 8443, 'revision': 2}), source)
                self.assertEqual(store.get('source', 'policy')['value'], 8443)
                self.assertEqual(store.get('strategy', 'current'), strategy)
                self.assertTrue(store.delete('source', 'policy'))
                self.assertIsNone(store.get('source', 'policy'))
                self.assertEqual(store.get('strategy', 'current'), strategy)
                self.assertEqual([h['event'] for h in store.history(source)], ['ADD', 'UPDATE', 'DELETE'])
                self.assertEqual(store.manifest()['llm_attempts'], 0)
            with Mem0StructuredStore(root, 'foreign') as store:
                self.assertEqual(store.list('strategy'), [])
                self.assertIsNone(store.get('strategy', 'current'))
            with Mem0StructuredStore(root, 'owner') as store:
                self.assertIsNone(store.get('source', 'policy'))
                self.assertEqual(store.get('strategy', 'current'), strategy)
                self.assertEqual(len(store.list('strategy')), 1)

    def test_fixture_preserves_native_behavior_and_marks_emulation(self):
        for event in ('update', 'forget', 'delete_recreate', 'concurrent_update', 'rollback', 'unrelated_update'):
            with self.subTest(event=event), tempfile.TemporaryDirectory() as root:
                spec = {'case_id': 'case-' + event, 'event': event,
                        'old': {'port': 8000}, 'new': {'port': 8443}}
                result = run_fixture(spec, Path(root) / 'fixture')
                self.assertEqual(result['active']['content'], strategy_text(spec['old']))
                self.assertEqual(result['stale_returned'], event != 'unrelated_update')
                if event == 'delete_recreate':
                    self.assertEqual(result['current_source']['revision'], 3)
                self.assertIsNone(result['blocked_lifecycle_operation'])
                self.assertEqual(bool(result['notes']), event in ('concurrent_update', 'rollback'))
                self.assertEqual(result['manifest']['llm_attempts'], 0)
                self.assertTrue(all(op['ok'] for op in result['operations']))
                self.assertEqual(json.loads((Path(root) / 'fixture/result.json').read_text())['active'], result['active'])


if __name__ == '__main__':
    unittest.main()
