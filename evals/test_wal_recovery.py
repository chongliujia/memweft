import copy
import unittest

from benchmark_pressure import stats
from benchmark_wal_recovery import next_slot, slots
from report_wal_recovery import validate_client, validate_recovery


class RecoveryEvidenceTests(unittest.TestCase):
    def test_slow_request_skips_arrivals_without_inflating_throughput(self):
        self.assertEqual(slots(1, 25, 0.03), 25)
        self.assertEqual(slots(1, 25, 0.04), 24)
        self.assertEqual(slots(0.01, 25, 0.03), 0)
        self.assertEqual(next_slot(0, 0.21, 25), 6)
        self.assertEqual(next_slot(8, 0.1, 25), 9)
        self.assertEqual(next_slot(0, 0.2, 25), 5)

    def test_duplicate_slots_wrong_revisions_and_false_counts_are_rejected(self):
        config = {"read_rate": 2, "write_rate": 2, "readers": 1, "writers": 1, "seconds": 2}
        samples = [{"slot": 0, "at_s": 0.001, "lag_ms": 1, "ms": 10, "revision": 2},
                   {"slot": 2, "at_s": 1.001, "lag_ms": 1, "ms": 20, "revision": 3}]
        good = {"kind": "writer", "index": 0, "offered_slots": 4, "completed": 2,
                "missed_slots": 2, "samples": samples, "last_revision": 3,
                "latency": stats([10, 20]), "scheduled_latency": stats([11, 21])}
        validate_client(good, config)
        for field, value in [("completed", 4), ("missed_slots", 0), ("last_revision", 4)]:
            bad = copy.deepcopy(good)
            bad[field] = value
            with self.assertRaises(AssertionError):
                validate_client(bad, config)
        for field, value in [("slot", 0), ("revision", 4), ("at_s", 0.5)]:
            bad = copy.deepcopy(good)
            bad["samples"][1][field] = value
            with self.assertRaises(AssertionError):
                validate_client(bad, config)

    def test_final_close_cleanup_cannot_count_as_idle_recovery(self):
        monitor = [{"at_s": 0, "phase": "load", "wal_bytes": 100000},
                   {"at_s": 10.1, "phase": "recovery", "wal_bytes": 0},
                   {"at_s": 15.1, "phase": "recovery", "wal_bytes": 0}]
        result = {"recovery_start_s": 10, "measured_end_s": 15.11,
                  "config": {"recovery_seconds": 5}}
        client = {"load_finished_s": 9.9, "connection_close_after_s": 15.2}
        validate_recovery(monitor, [client], result)
        client["connection_close_after_s"] = 10.05
        with self.assertRaises(AssertionError):
            validate_recovery(monitor, [client], result)


if __name__ == "__main__":
    unittest.main()
