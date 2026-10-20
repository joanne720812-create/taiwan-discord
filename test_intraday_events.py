import copy
import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import pandas as pd
import fugle_intraday as fugle
import intraday_events as events


class EventTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 21, 9, 3, 10, tzinfo=fugle.TZ)
        self.stock = dict(ticker="2330.TW", code="2330", name="台積電", direction="long",
                          avg5_daily_shares=1_000_000, avg5_count=5, avg5_date="2026-10-20",
                          levels_date="2026-10-20", cdp_resistance=105, pivot=100, support=95)
        self.frame = pd.DataFrame({"Open": [100, 100, 101], "High": [100, 101, 101],
                                   "Low": [100, 100, 99.99], "Close": [100, 101, 99.99],
                                   "Volume": [1000, 2000, 3000]},
                                  index=pd.date_range("2026-10-21 09:00", periods=3, freq="min", tz=fugle.TZ))
        self.trade = dict(price=101, size=20, volume=999999, serial=123,
                          time=int((self.now - timedelta(seconds=10)).timestamp() * 1_000_000))
        self.payload = dict(date="2026-10-21", symbol="2330", exchange="TWSE", type="EQUITY",
                            data=[self.trade])

    def parse(self, payload=None, stock=None, since=None):
        return events.trade_events(payload or self.payload, stock or self.stock, self.now,
                                   since or self.now - timedelta(seconds=90))

    def test_inclusive_one_percent_each_direction(self):
        result = events.minute_events(self.frame, self.now)
        self.assertEqual([e["kind"] for e in result], ["up", "down"])
        self.assertAlmostEqual(result[1]["change"], -1)

    def test_no_alert_below_one_percent(self):
        self.frame.Close = [100, 100.99, 100.5]
        self.assertEqual(events.minute_events(self.frame, self.now), [])

    def test_unfinished_minute_ignored(self):
        result = events.minute_events(self.frame, self.now.replace(minute=2, second=30))
        self.assertEqual([e["kind"] for e in result], ["up"])

    def test_no_yesterday_gap_duplicate_naive_or_zero_volume(self):
        for mode in ("yesterday", "gap", "duplicate", "naive", "volume"):
            frame = self.frame.iloc[:2].copy()
            if mode == "yesterday": frame.index = frame.index - pd.Timedelta(days=1)
            if mode == "gap": frame.index = [frame.index[0], frame.index[1] + pd.Timedelta(minutes=1)]
            if mode == "duplicate": frame.index = [frame.index[0]] * 2
            if mode == "naive": frame.index = frame.index.tz_localize(None)
            if mode == "volume": frame.Volume = [0, 10]
            with self.subTest(mode=mode):
                self.assertEqual(events.minute_events(frame, self.now), [])

    def test_stale_minutes_suppressed(self):
        self.assertEqual(events.minute_events(self.frame, self.now + timedelta(minutes=6)), [])

    def test_giant_uses_single_size_and_inclusive_two_percent(self):
        found, gap, baseline = self.parse()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["shares"], 20_000)
        self.assertEqual(found[0]["threshold"], 20_000)
        self.assertFalse(gap)
        self.assertTrue(baseline)
        self.payload["data"][0]["size"] = 19
        self.assertEqual(self.parse()[0], [])  # huge cumulative volume is irrelevant

    def test_does_not_sum_multiple_trades(self):
        self.payload["data"] = [{**self.trade, "size": 12, "serial": i} for i in (1, 2)]
        self.assertEqual(self.parse()[0], [])

    def test_baseline_missing_bad_date_or_short_history_disables_only_large(self):
        for change in ({"avg5_count": 4}, {"avg5_daily_shares": 0},
                       {"avg5_daily_shares": float("nan")}, {"avg5_date": "2026-10-19"}):
            with self.subTest(change=change):
                found, _, ok = self.parse(stock={**self.stock, **change})
                self.assertEqual(found, [])
                self.assertFalse(ok)

    def test_old_trade_and_cursor_suppress_replay(self):
        self.assertEqual(self.parse(since=self.now)[0], [])
        self.payload["data"][0]["time"] -= 300_000_000
        self.assertEqual(self.parse()[0], [])

    def test_metadata_wrong_dates_symbols_trial_or_oddlot_rejected(self):
        for key, value in (("date", "2026-10-20"), ("symbol", "2484"),
                           ("exchange", "TPEx"), ("type", "ODDLOT")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.parse(payload={**self.payload, key: value})

    def test_future_trade_rejected(self):
        self.payload["data"][0]["time"] += 60_000_000
        with self.assertRaises(ValueError): self.parse()

    def test_full_page_reports_coverage_gap(self):
        self.payload["data"] = [{**self.trade, "serial": i} for i in range(500)]
        self.assertTrue(self.parse()[1])
        self.payload["data"][-1]["time"] -= 100_000_000
        self.assertFalse(self.parse()[1])

    def test_baseline_five_completed_days_excludes_current(self):
        frame = pd.DataFrame({"Volume": [1, 2, 3, 4, 5, 99999]},
                             index=pd.date_range("2026-10-16", periods=6))
        self.assertEqual(events.volume_baseline(frame, self.now.date() - timedelta(days=1))["avg5_daily_shares"], 3)
        self.assertEqual(events.volume_baseline(frame.iloc[:4], self.now.date()), {})

    def test_resample_reuses_one_minute_for_five_minute(self):
        rows = [dict(date=at.isoformat(), open=row.Open, high=row.High,
                     low=row.Low, close=row.Close, volume=row.Volume / 1000)
                for at, row in self.frame.iterrows()]
        payload = {**self.payload, "timeframe": "1", "data": rows}
        with patch.dict(os.environ, {"THREE_EVENT_ALERTS": "1", "FUGLE_API_KEY": "test"}), \
                patch.object(fugle, "request_json", return_value=payload) as get:
            result = fugle.download(["2330.TW"], self.now)
        self.assertEqual(get.call_count, 1)
        self.assertEqual(result["2330.TW"].iloc[0].Volume, 6000)
        self.assertEqual(len(fugle.minute_cache["2330.TW"][1]), 3)

    def test_routing_both_moves_and_giant_dedup_after_restart(self):
        store = {}
        base = Mock()
        base.now_tw.return_value = self.now
        base.load.side_effect = lambda key, default: copy.deepcopy(store.get(key, default))
        base.save.side_effect = lambda key, value: store.update({key: copy.deepcopy(value)})
        for direction in ("long", "short", None):
            stock = {**self.stock}
            if direction: stock["direction"] = direction
            else: stock.pop("direction")
            fugle.minute_cache["2330.TW"] = (self.now, self.frame)
            with patch.dict(os.environ, {"THREE_EVENT_ALERTS": "1", "INTRADAY_DATA_SOURCE": "fugle"}), \
                    patch.object(fugle, "request_json", return_value=self.payload):
                events.check_stock(stock, self.now, base)
                before = base.send_embeds.call_count
                events.check_stock(stock, self.now, base)
                self.assertEqual(base.send_embeds.call_count, before)
            calls = base.send_embeds.call_args_list[-3:]
            self.assertEqual(len(calls), 3)
            self.assertTrue(all(c.args[2] == direction for c in calls))
            self.assertTrue(all("2330 台積電" in c.args[1][0]["title"] for c in calls))

    def test_failed_trades_still_sends_minute_alerts(self):
        base = Mock()
        base.load.return_value = {}
        fugle.minute_cache["2330.TW"] = (self.now, self.frame)
        with patch.dict(os.environ, {"THREE_EVENT_ALERTS": "1", "INTRADAY_DATA_SOURCE": "fugle"}), \
                patch.object(fugle, "request_json", side_effect=RuntimeError("unavailable")):
            events.check_stock(self.stock, self.now, base)
        self.assertEqual(base.send_embeds.call_count, 2)
        base.send_discord.assert_called_once()


if __name__ == "__main__":
    unittest.main()
