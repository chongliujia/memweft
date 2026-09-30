from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from langgraph.store.base import Item, SearchItem
from memweft.adapters.store import _item, _parse_timestamp


class StoreTimestampTests(unittest.TestCase):
    def test_real_rust_nanosecond_timestamp(self):
        value = "2026-09-30T11:05:35.073120371Z"
        expected = datetime(2026, 9, 30, 11, 5, 35, 73120, tzinfo=timezone.utc)
        self.assertEqual(_parse_timestamp(value), expected)
        # Check the input to fromisoformat itself, even on newer Python versions.
        with patch("memweft.adapters.store.datetime", wraps=datetime) as parser:
            self.assertEqual(_parse_timestamp(value), expected)
            parser.fromisoformat.assert_called_once_with("2026-09-30T11:05:35.073120+00:00")

    def test_absent_and_variable_fractional_precision(self):
        for fraction, microsecond in [
            ("", 0), (".1", 100000), (".12", 120000), (".123", 123000),
            (".1234", 123400), (".12345", 123450), (".123456", 123456),
            (".1234567", 123456), (".12345678", 123456), (".999999999", 999999),
        ]:
            with self.subTest(fraction=fraction):
                result = _parse_timestamp(f"2026-09-30T11:05:35{fraction}Z")
                self.assertEqual(result, datetime(2026, 9, 30, 11, 5, 35, microsecond, timezone.utc))

    def test_numeric_timezone_offsets_are_preserved(self):
        for offset, minutes in [("+00:00", 0), ("+05:30", 330), ("-07:00", -420)]:
            for fraction, microsecond in [("", 0), (".7", 700000), (".794661123", 794661)]:
                with self.subTest(offset=offset, fraction=fraction):
                    result = _parse_timestamp(f"2026-09-30T11:05:35{fraction}{offset}")
                    self.assertEqual(result.hour, 11)
                    self.assertEqual(result.microsecond, microsecond)
                    self.assertEqual(result.utcoffset(), timedelta(minutes=minutes))

    def test_get_and_search_items_parse_both_timestamps(self):
        data = {
            "value": {"enabled": True}, "key": "k", "namespace": ["preferences"],
            "created_at": "2026-09-30T11:05:35.073120371Z",
            "updated_at": "2026-09-30T11:05:49.794661Z",
        }
        for search, item_type in [(False, Item), (True, SearchItem)]:
            with self.subTest(search=search):
                result = _item(data, search=search)
                self.assertIsInstance(result, item_type)
                self.assertEqual(result.created_at.microsecond, 73120)
                self.assertEqual(result.updated_at.microsecond, 794661)
                self.assertEqual(result.created_at.utcoffset(), timedelta(0))
                self.assertEqual(result.updated_at.utcoffset(), timedelta(0))
        self.assertIsNone(_item(None))

    def test_invalid_timestamps_are_rejected(self):
        for value in [
            "", "not a timestamp", "2026-09-30", "2026-09-30T11:05:35",
            "2026-02-30T11:05:35Z", "2026-09-30T25:05:35Z",
            "2026-09-30T11:05:35.Z", "2026-09-30T11:05:35.1234567890Z",
            "2026-09-30T11:05:35.xyzZ", "2026-09-30T11:05:35+24:00",
            "2026-09-30T11:05:35+00:60", "2026-09-30T11:05:35Zjunk",
        ]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                _parse_timestamp(value)

    def test_non_string_timestamps_are_rejected(self):
        for value in [None, 0, True, [], {}]:
            with self.subTest(value=value), self.assertRaises(TypeError):
                _parse_timestamp(value)


if __name__ == "__main__":
    unittest.main()
