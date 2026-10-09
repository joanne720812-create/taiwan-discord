import os
import unittest
from datetime import datetime
from unittest.mock import patch

import pandas as pd
import rising5_discord as rising


class RisingFugleTests(unittest.TestCase):
    def setUp(self):
        self.current = datetime(2026, 10, 12, 9, 7, tzinfo=rising.base.TZ)
        self.stock = dict(ticker="2484.TW", code="2484", name="希華", ma60=10,
                          prior_ma60=10, value=100000000)
        index = pd.date_range("2026-10-08 11:40", periods=22, freq="5min", tz=rising.base.TZ)
        closes = [9.8, 10.0] * 10 + [9.8, 9.9]
        self.history = pd.DataFrame(dict(Open=closes, High=[10.1]*22, Low=[9.7]*22,
                                         Close=closes, Volume=[1000]*22), index=index)
        self.live = pd.DataFrame(dict(Open=[9.9, 10.05], High=[10.1, 10.2], Low=[9.8, 10],
                                      Close=[10.05, 10.15], Volume=[2000, 9000]),
                                 index=pd.date_range("2026-10-12 09:00", periods=2,
                                                     freq="5min", tz=rising.base.TZ))

    def test_opening_signal_has_previous_history_and_ignores_unfinished_bar(self):
        frame = rising.merge_history(self.history, self.live, self.current)
        hit = rising.signal(frame, self.stock, self.current, datetime(2026, 10, 8).date())
        self.assertIsNotNone(hit)
        self.assertEqual(hit["close"], 10.05)
        self.assertEqual(hit["volume_ratio"], 2)
        self.assertEqual(hit["bar_end"], "2026-10-12T09:05:00+08:00")

    def test_no_history_suppresses_early_signal(self):
        self.assertIsNone(rising.signal(self.live, self.stock, self.current,
                                       datetime(2026, 10, 8).date()))

    def test_old_history_cannot_emit_current_day_signal(self):
        self.assertIsNone(rising.signal(self.history, self.stock, self.current,
                                       datetime(2026, 10, 8).date()))
        self.assertTrue(rising.merge_history(self.history, pd.DataFrame(), self.current).empty)

    def test_live_replaces_any_same_day_history(self):
        history = pd.concat([self.history, self.live * 100])
        frame = rising.merge_history(history, self.live, self.current)
        self.assertFalse(frame.index.has_duplicates)
        self.assertEqual(frame.iloc[-1].Close, 10.15)

    def test_top10_never_fills_missing_candidates_from_full_pool(self):
        with patch.dict(os.environ, {"RISING_MONITOR_SCOPE": "top10"}):
            self.assertEqual(rising.monitored_stocks({"top10": [self.stock], "pool": [self.stock]*346}), [self.stock])
            self.assertEqual(rising.monitored_stocks({"top10": [], "pool": [self.stock]}), [])
            self.assertEqual(len(rising.monitored_stocks({"top10": [self.stock]*20, "pool": []})), 10)

    def test_fugle_rejects_full_pool_and_isolates_one_failed_symbol(self):
        second = dict(self.stock, ticker="8042.TWO")
        with patch.dict(os.environ, {"INTRADAY_DATA_SOURCE": "fugle"}), \
                patch.object(rising.base, "now_tw", return_value=self.current), \
                patch.object(rising.base, "download", side_effect=[RuntimeError("redacted"), self.live]) as fetch:
            results = list(rising.intraday_frames([self.stock, second], {second["ticker"]: self.history}))
            self.assertTrue(results[0][1].empty)
            self.assertEqual(len(results[1][1]), 24)
            self.assertEqual(fetch.call_count, 2)
            with self.assertRaises(RuntimeError):
                list(rising.intraday_frames([self.stock]*11, {}))

    def test_repeated_sweep_does_not_resend_signals(self):
        frame = rising.merge_history(self.history, self.live, self.current)
        report = dict(asof="2026-10-08", pool=[self.stock], top10=[self.stock])
        events = dict(sent=[], matched={}, top_codes=[])
        with patch.dict(os.environ, {"INTRADAY_DATA_SOURCE": "fugle", "RISING_MONITOR_SCOPE": "top10"}), \
                patch.object(rising.base, "now_tw", return_value=self.current), \
                patch.object(rising, "intraday_frames", side_effect=lambda *args: iter([(self.stock, frame)])), \
                patch.object(rising, "send_cards") as send, patch.object(rising.base, "save"):
            rising.sweep(report, events)
            self.assertEqual(len(events["sent"]), 1)
            count = send.call_count
            rising.sweep(report, events)
            self.assertEqual(send.call_count, count)


if __name__ == "__main__":
    unittest.main()
