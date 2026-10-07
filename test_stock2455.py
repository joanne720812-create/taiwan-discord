"""Synthetic completed candles: no quotes, webhook or orders."""
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
import pandas as pd
import stock2455_discord as task
import daily_review as review


class Short2455Tests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 8, 9, 15, tzinfo=task.base.TZ)
        self.stock = dict(code='2455', name='全新', ticker='2455.TW', close=559,
                          high=575, low=547, target_date='2026-10-08',
                          **task.base.cdp_levels(575, 547, 559, '2026-10-07'))
        previous = pd.date_range('2026-10-07 11:55', periods=20, freq='5min', tz=task.base.TZ)
        today = pd.date_range('2026-10-08 09:00', periods=3, freq='5min', tz=task.base.TZ)
        rows = [dict(Open=563, High=564, Low=561, Close=563, Volume=100)] * 20
        rows += [dict(Open=563, High=564, Low=562, Close=563, Volume=100),
                 dict(Open=563, High=563, Low=560, Close=561, Volume=100),
                 dict(Open=561, High=562, Low=558, Close=559, Volume=200)]
        self.frame = pd.DataFrame(rows, index=previous.append(today))

    def test_short_trigger_and_levels(self):
        hit = task.strategy(self.frame, self.stock, self.now)
        self.assertIsNotNone(hit)
        self.assertEqual((self.stock['pivot'], self.stock['cdp_resistance'], self.stock['support']), (560, 573, 545))
        self.assertEqual((hit['trigger'], hit['stop'], hit['volume_ratio_5m']), (560, 564, 2))
        self.assertEqual(task.base.start_followup({**self.stock, 'direction':'short'}, hit)['target'], 549)

    def test_long_stale_other_day_and_cutoff_rejected(self):
        self.assertIsNone(task.strategy(self.frame, self.stock, self.now, 'long'))
        for delta in (timedelta(minutes=5, seconds=1), timedelta(days=1), timedelta(hours=4)):
            self.assertIsNone(task.strategy(self.frame, self.stock, self.now + delta))
        self.assertIsNone(task.strategy(self.frame, self.stock, self.now-timedelta(seconds=1)))

    def test_low_volume_large_risk_and_red_candle_rejected(self):
        for field, value in [('Volume', 100), ('High', 580), ('Open', 558), ('Low', 570)]:
            changed = self.frame.copy()
            changed.loc[changed.index[-1], field] = value
            self.assertIsNone(task.strategy(changed, self.stock, self.now))

    def test_gap_down_and_missing_history_rejected(self):
        changed = self.frame.copy()
        changed.loc[changed.index[-3], 'Open'] = 540
        changed.loc[changed.index[-3], 'Low'] = 539
        self.assertIsNone(task.strategy(changed, self.stock, self.now))
        self.assertIsNone(task.strategy(self.frame.iloc[-3:], self.stock, self.now))

    def test_official_baseline_date_is_required(self):
        with patch.object(task.ranking, 'listed_for_date', return_value=[dict(code='2455', date='2026-10-06')]):
            with self.assertRaises(RuntimeError):
                task.baseline()

    def test_project_in_daily_review(self):
        hit = task.strategy(self.frame, self.stock, self.now)
        docs = [('0001-stock2455-state.zip', 'stock2455.json', self.stock),
                ('0001-stock2455-state.zip', 'stock2455_events.json',
                 {'events': {f'2026-10-08:2455.TW:short:{hit["bar_at"]}': hit},
                  'crossings': {'touch:example': {**hit, 'crossed': []}}})]
        candidate = review.select_candidates(docs, '2026-10-07')['2455']
        self.assertEqual(candidate['direction'], 'short')
        events, notices = review.parsed_events(docs, '2026-10-08')
        self.assertEqual(events[0]['group'], '全新做空5分K')
        self.assertEqual(notices[0]['kind'], '觸价')
        report = dict(day='2026-10-08', previous_day='2026-10-07', groups={},
                      events=events, notices=notices,
                      signals=review.signal_rows(events, {'2455':dict(name='全新', close=550)}, {}))
        parts = review.payloads(report)
        signal = next(p for p in parts if '全新做空5分K｜訊號復盤表' in p.get('content',''))
        self.assertIn('2455', signal['embeds'][0]['title'])


if __name__ == '__main__':
    unittest.main()
