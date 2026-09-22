"""Exercise the real graph without calling an external model."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'examples'))
from local_memory_agent import LocalMemoryAgent


class AgentGraphTests(unittest.TestCase):
    def test_retrieval_and_adopted_strategy_are_explicit_and_history_excluded(self):
        reads, calls = [], []

        class User:
            def session(self, thread):
                self.thread = thread
                return self

            def context(self, **options):
                reads.append((self.thread, options))
                return SimpleNamespace(text='scoped memory', memories=[{'key': 'fact'}],
                                       strategies=[{'version': 'adopted'}] if options['task_type'] else [],
                                       report={})

        def model(messages, **options):
            calls.append((messages, options))
            return {'content': '{}'}

        agent = LocalMemoryAgent(User(), model, 'system')
        empty = agent.ask('question', schema={}, task_type='triage', mode='none')
        self.assertFalse(reads)
        self.assertFalse(empty['context']['memories'])
        self.assertEqual([m['content'] for m in calls[-1][0]], ['system', 'question'])

        baseline = agent.ask('question', schema={}, task_type='triage', session_id='s')
        self.assertFalse(baseline['context']['strategies'])
        self.assertIsNone(reads[-1][1]['task_type'])
        adopted = agent.ask('question', schema={}, task_type='triage', mode='learned')
        self.assertEqual(adopted['context']['strategies'], [{'version': 'adopted'}])
        self.assertEqual(reads[-1][1]['task_type'], 'triage')
        self.assertTrue(all(not options['include_messages'] for _, options in reads))
        self.assertTrue(all(options['query'] == 'question' for _, options in reads))
        self.assertEqual(set(calls[-1][1]), {'tag', 'schema', 'repeat'})
        with self.assertRaises(ValueError):
            agent.ask('question', schema={}, mode='unreviewed')


if __name__ == '__main__':
    unittest.main()
