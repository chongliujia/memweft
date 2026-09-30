"""Snapshot manifests use portable keys even when relative paths are Windows paths."""
import hashlib
from pathlib import Path, PureWindowsPath
import tempfile
import unittest
from unittest.mock import patch

from run_workflow_comparison import snapshot_sources as snapshot_comparison
from run_workflow_guard import snapshot_sources as snapshot_guard


class SnapshotPathTests(unittest.TestCase):
    def test_windows_relative_paths_keep_portable_keys_and_matching_bytes(self):
        relative_to = Path.relative_to

        def windows_relative(path, *args, **kwargs):
            return PureWindowsPath(relative_to(path, *args, **kwargs))

        for snapshot in (snapshot_comparison, snapshot_guard):
            with self.subTest(snapshot=snapshot.__module__), tempfile.TemporaryDirectory() as directory:
                output = Path(directory)
                with patch.object(Path, "relative_to", windows_relative):
                    hashes = snapshot(output)
                self.assertIn("evals/run_local.py", hashes)
                self.assertIn("python/src/memweft/__init__.py", hashes)
                for relative, digest in hashes.items():
                    self.assertNotIn("\\", relative)
                    self.assertEqual(hashlib.sha256((output / "sources" / relative).read_bytes()).hexdigest(), digest)


if __name__ == "__main__":
    unittest.main()
