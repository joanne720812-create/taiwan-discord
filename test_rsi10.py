import json
import os
import unittest
from datetime import datetime, date
from unittest.mock import patch

import pandas as pd
import rsi10_discord as bot


class RsiObservationTests(unittest.TestCase):
    def frame(self):
        prior = pd.date_range("2026-10-02 11:30", periods=24, freq="5min", tz=bot.base.TZ)
        current = pd.date_range("2026-10-05 09:00", periods=4, freq="5min", tz=bot.base.TZ)
        idx = prior.append(current)
        return pd.DataFrame({"Open":100., "High":101., "Low":99., "Close":100., "Volume":[100]*24+[150,200,200,200]}, index=idx)

    def now(self, hh, mm):
        return datetime(2026,10,5,hh,mm,tzinfo=bot.base.TZ)

    def fixed_rsi(self, values, length=14):
        return pd.Series(60., index=values.index)

    def test_incomplete_bar_does_not_alert(self):
        with patch.object(bot,"rsi", self.fixed_rsi):
            self.assertEqual(bot.morning_signals(self.frame(), {}, self.now(9,4)), [])

    def test_first_bar_exactly_confirmed_at_0905(self):
        with patch.object(bot,"rsi", self.fixed_rsi):
            hits=bot.morning_signals(self.frame(),{},self.now(9,5))
        self.assertEqual(len(hits),1)
        self.assertEqual(hits[0]["status"],"重點觀察")
        self.assertEqual(hits[0]["lag_minutes"],0)
        self.assertEqual(hits[0]["volume_ratio"],1.5)

    def test_0915_start_is_excluded(self):
        with patch.object(bot,"rsi", self.fixed_rsi):
            hits=bot.morning_signals(self.frame(),{},self.now(9,20))
        self.assertEqual(len(hits),3)
        self.assertEqual(datetime.fromisoformat(hits[-1]["bar_end"]).minute,15)

    def test_stale_bar_does_not_alert(self):
        with patch.object(bot,"rsi", self.fixed_rsi):
            self.assertEqual(bot.morning_signals(self.frame(),{},self.now(9,46)),[])

    def test_insufficient_prior_volume_cannot_alert(self):
        with patch.object(bot,"rsi",self.fixed_rsi):
            self.assertEqual(bot.morning_signals(self.frame().tail(4),{},self.now(9,15)),[])

    def test_wrong_discord_channel_is_rejected_before_post(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self,*args): return None
            def read(self): return json.dumps({"channel_id":"wrong-channel"}).encode()
        with patch.dict(os.environ,{"DISCORD_RSI_WEBHOOK_URL":"https://discord.com/api/webhooks/test/test"}), patch.object(bot.urllib.request,"urlopen",return_value=Response()) as request:
            with self.assertRaisesRegex(RuntimeError,"unexpected channel"):
                bot.send("test")
            self.assertEqual(request.call_count,1)

    def test_daily_raw_close_must_match_official(self):
        idx=pd.date_range("2026-08-27",periods=40,freq="D")
        frame=pd.DataFrame({"Close":100.,"High":101.,"Low":99.,"Volume":100.},index=idx)
        _,reason=bot.candidate(frame,{"official_close":101.},idx[-1].date())
        self.assertEqual(reason,"close_mismatch")

    def test_lagging_official_market_uses_dated_fallback(self):
        today=date(2026,10,5)
        old=date(2026,10,2)
        with patch.object(bot.base,"official_snapshot",side_effect=[(old,[]),(today,[]),(today,[])]) as source:
            snapshots=bot.official_snapshots(today,True)
        self.assertEqual({d for d,_ in snapshots},{today})
        self.assertEqual(source.call_args_list[-1].args,("TW",today))


    def test_future_history_is_not_accepted_as_baseline(self):
        idx=pd.date_range("2026-08-27",periods=40,freq="D")
        frame=pd.DataFrame({"Close":100.},index=idx)
        _,reason=bot.candidate(frame,{},date(2026,10,4))
        self.assertEqual(reason,"missing_history")


if __name__ == "__main__":
    unittest.main()
