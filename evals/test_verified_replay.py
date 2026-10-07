import json
from pathlib import Path
import shutil
import tempfile
import unittest

from replay_verified_completion import ROOT, replay, sha

OVERVIEW = ROOT / 'evals/reports/2026-10-06-multistep-memory-overview.json'


class HistoricalReplayTests(unittest.TestCase):
    def test_original_sixty_finals_replayed_without_accepting_unverified_failures(self):
        originals = [OVERVIEW]
        for row in json.loads(OVERVIEW.read_text(encoding='utf-8'))['cohorts']:
            report = OVERVIEW.parent / row['report']
            originals.extend((report, report.with_suffix('.traces.jsonl')))
        before = {str(path): sha(path) for path in originals}
        result = replay(OVERVIEW)
        self.assertEqual(result['tasks'], 60)
        self.assertEqual(result['historical_strict_success'], 51)
        self.assertEqual(result['historical_failures_accepted'], 0)
        self.assertEqual(result['model_api_calls'], 0)
        failed = [r for r in result['case_results'] if not r['historical_success']]
        self.assertEqual(len(failed), 9)
        self.assertTrue(all(not r['public_verification']['verified'] for r in failed))
        self.assertTrue(all(r['historical_rounds'] + r['rounds_remaining'] == 4 for r in result['case_results']))
        self.assertEqual({str(path): sha(path) for path in originals}, before)

    def test_modified_trace_rejected_before_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            overview = json.loads(OVERVIEW.read_text(encoding='utf-8'))
            first = overview['cohorts'][0]
            overview['cohorts'] = [first]
            source = OVERVIEW.parent / first['report']
            shutil.copyfile(source, root / source.name)
            (root / source.with_suffix('.traces.jsonl').name).write_text('{}\n', encoding='utf-8')
            (root / 'overview.json').write_text(json.dumps(overview), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'Published trace hash changed'):
                replay(root / 'overview.json')


if __name__ == '__main__':
    unittest.main()
