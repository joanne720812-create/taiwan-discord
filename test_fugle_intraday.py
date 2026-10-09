import copy
import os
import unittest
from datetime import datetime
from unittest.mock import patch

import fugle_intraday as fugle
import short_discord as bot


class FugleTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 9, 9, 7, tzinfo=fugle.TZ)
        self.payload = {"date": "2026-10-09", "symbol": "2484", "type": "EQUITY",
                        "exchange": "TWSE", "timeframe": "5", "data": [
            {"date": "2026-10-09T09:00:00+08:00", "open": 90, "high": 92,
             "low": 89, "close": 91, "volume": 12},
            {"date": "2026-10-09T09:05:00+08:00", "open": 91, "high": 93,
             "low": 90, "close": 92, "volume": 3}]}

    def test_units_and_incomplete_bar(self):
        frame = fugle.parse_candles(self.payload, "2484.TW", self.now)
        self.assertEqual(frame.iloc[0].Volume, 12000)
        completed = bot.completed_bars(frame, self.now)
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed.iloc[-1].Close, 91)

    def test_metadata_rejected(self):
        for key, wrong in (("symbol", "8042"), ("date", "2026-10-08"),
                           ("timeframe", "1"), ("exchange", "TPEx"), ("type", "ODDLOT")):
            with self.subTest(key=key):
                payload = {**self.payload, key: wrong}
                with self.assertRaises(ValueError):
                    fugle.parse_candles(payload, "2484.TW", self.now)

    def test_invalid_bars_rejected(self):
        for key, wrong in (("volume", -1), ("close", float("nan")), ("high", 88),
                           ("date", "2026-10-09T09:03:00+08:00"),
                           ("date", "2026-10-09T09:10:00+08:00"),
                           ("date", "2026-10-09T09:00:00")):
            with self.subTest(key=key, wrong=wrong):
                payload = copy.deepcopy(self.payload)
                payload["data"][0][key] = wrong
                with self.assertRaises(ValueError):
                    fugle.parse_candles(payload, "2484.TW", self.now)

    def test_empty_and_duplicate(self):
        self.assertTrue(fugle.parse_candles({**self.payload, "data": []}, "2484.TW", self.now).empty)
        payload = {**self.payload, "data": [self.payload["data"][0]] * 2}
        with self.assertRaises(ValueError):
            fugle.parse_candles(payload, "2484.TW", self.now)

    def test_tpex(self):
        payload = {**self.payload, "exchange": "TPEx", "symbol": "8042"}
        self.assertEqual(len(fugle.parse_candles(payload, "8042.TWO", self.now)), 2)

    def test_missing_key_fails_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "Missing FUGLE_API_KEY"):
                fugle.download(["2484.TW"], self.now)

    def test_live_routing(self):
        with patch.dict(os.environ, {"INTRADAY_DATA_SOURCE": "fugle"}), patch.object(fugle, "download", return_value="fugle") as fetch:
            self.assertEqual(bot.download(["2484.TW"], "5m", "5d"), "fugle")
            fetch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
