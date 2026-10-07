"""Synthetic fixtures: no network, credentials, or Discord messages."""
import unittest
import io
import json
from unittest.mock import patch
from datetime import datetime
import pandas as pd
import daily_review as bot


class ReviewTests(unittest.TestCase):
    def test_complete_json_is_read_despite_incorrect_content_length(self):
        class LengthMismatch(io.BytesIO):
            def read(self, size=-1):
                if size == -1:
                    raise bot.http.client.IncompleteRead(b'[]', 100)
                return super().read(size)
        with patch.object(bot.urllib.request, 'urlopen', return_value=LengthMismatch(b'[{"Code":"1234"}]')):
            self.assertEqual(bot.otc_json(), [{'Code':'1234'}])

    def test_truncated_json_is_never_used_as_quotes(self):
        with patch.object(bot.urllib.request, 'urlopen', side_effect=lambda *a, **k: io.BytesIO(b'[{"Code":"1234"')):
            with patch.object(bot.time, 'sleep'):
                with self.assertRaisesRegex(RuntimeError, '不完整'):
                    bot.otc_json()

    def setUp(self):
        self.stock = dict(code='1234', name='合成', close=100, pivot=100, support=98, cdp_resistance=102)
        self.quote = dict(code='1234', name='合成', open=100, high=104, low=97, close=103, change=3)

    def test_long_short_are_opposite_without_claiming_profit(self):
        long = bot.candidate_row(self.stock, self.quote, 'long')
        short = bot.candidate_row(self.stock, self.quote, 'short')
        self.assertAlmostEqual(long['direction_pct'], 3)
        self.assertAlmostEqual(short['direction_pct'], -3)
        self.assertEqual(long['touches'], '壓、界、撐')

    def test_adjusted_reference_is_excluded(self):
        row = bot.candidate_row(self.stock, {**self.quote, 'change': 2}, 'long')
        self.assertNotIn('direction_pct', row)
        self.assertIn('不一致', row['status'])

    def test_missing_and_invalid_quotes_are_not_zero_returns(self):
        self.assertNotIn('direction_pct', bot.candidate_row(self.stock, None, 'long'))
        self.assertEqual(bot.candidate_row(self.stock, {**self.quote, 'high': 99}, 'long')['status'], '行情無效')

    def test_next_day_selection_never_used_to_review_today(self):
        docs = [('2', 'shortlist.json', {'asof': '2026-10-07', 'stocks': [self.stock]}),
                ('1', 'shortlist.json', {'asof': '2026-10-06', 'stocks': [self.stock]})]
        groups = bot.select_candidates(docs, '2026-10-06')
        self.assertEqual(groups['short']['origin'], '1')
        self.assertEqual(groups['fib']['stocks'], [])

    def test_actual_monitor_snapshot_supplies_first_long_review(self):
        docs = [('1', 'monitor_candidates_5m.json', {'asof':'2026-10-06', 'stocks':[{**self.stock,'direction':'long'}]})]
        groups = bot.select_candidates(docs, '2026-10-06')
        self.assertEqual(len(groups['long']['stocks']), 1)

    def test_signal_dates_and_archived_duplicates(self):
        key = '2026-10-07:1234.TW:short:2026-10-07T09:25:00+08:00'
        docs = [('1','events_5m.json',{key:dict(price=100, stop=101, bar_at='2026-10-07T09:25:00+08:00')}),
                ('2','events_5m.json',{key:dict(price=100, stop=101, bar_at='2026-10-07T09:25:00+08:00')})]
        events, _ = bot.parsed_events(docs, '2026-10-07')
        self.assertEqual(len(events), 1)
        self.assertEqual(bot.parsed_events(docs, '2026-10-08')[0], [])
        rows = bot.signal_rows(events, {'1234':self.quote}, {})
        self.assertAlmostEqual(rows[0]['direction_pct'], -3)

    def test_repeat_notifications_count_once_per_stock_direction(self):
        entries = [dict(group='多空5分K',ticker='1234.TW',direction='long',end=f'2026-10-07T09:{minute}:00+08:00',price=price,stop=99,age=0)
                   for minute, price in [('30',100),('45',102)]]
        rows = bot.signal_rows(entries, {'1234':self.quote}, {})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['count'], 2)
        self.assertEqual(rows[0]['price'], 100)

    def test_unrecorded_signal_price_requires_exact_bar(self):
        entry = dict(group='RSI5分K',ticker='1234.TW',direction='long',end='2026-10-07T09:30:00+08:00',price=None,stop=None,age=None)
        self.assertIsNone(bot.signal_rows([entry], {'1234':self.quote}, {})[0]['direction_pct'])
        bars = {'1234.TW':pd.DataFrame({'Close':[100]},index=[pd.Timestamp('2026-10-07T09:25:00+08:00')])}
        row = bot.signal_rows([entry], {'1234':self.quote}, bars)[0]
        self.assertTrue(row['reconstructed'])
        self.assertAlmostEqual(row['direction_pct'], 3)

    def test_discord_limits_and_no_fabricated_winrate(self):
        group = dict(label='測試', direction='long', status='已留存', rows=[bot.candidate_row(self.stock,self.quote,'long')]*10)
        report = dict(day='2026-10-07',previous_day='2026-10-06',groups={'long':group},events=[],signals=[])
        parts = bot.payloads(report)
        self.assertTrue(all(len(p.get('embeds',[])) <= 5 for p in parts))
        self.assertIn('不是實際交易損益或勝率', parts[0]['content'])
        self.assertTrue(all(len(p.get('content','')) <= 2000 for p in parts))


if __name__ == '__main__':
    unittest.main()
