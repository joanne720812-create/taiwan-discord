"""離線合成K棒測試；不讀秘密、不連行情或Discord。"""
import json
import unittest
from datetime import datetime, timedelta
import pandas as pd
import short_discord as bot


class FollowupTests(unittest.TestCase):
    def setUp(self):
        self.stamp = datetime(2026, 10, 6, 9, 25, tzinfo=bot.TZ)
        self.stock = {'code': '0000', 'name': '合成示範', 'direction': 'short',
                      **bot.cdp_levels(104, 96, 100, '2026-10-05')}
        self.pos = bot.start_followup(self.stock, {'price': 100, 'stop': 101,
                                                  'bar_at': self.stamp.isoformat()})

    def advance(self, pos, minute=5, price=99.8, low=99.7, high=100, stock=None, age=0):
        stamp = self.stamp+timedelta(minutes=minute)
        frame = pd.DataFrame([{'Open': price, 'High': high, 'Low': low,
                               'Close': price, 'Volume': 100}], index=[stamp])
        now = stamp+timedelta(minutes=5+age)
        return bot.advance_followup(frame, stock or self.stock, now, pos)

    def test_entry_targets_and_json_restart(self):
        self.assertEqual(self.pos['target'], 98)
        restored = json.loads(json.dumps(self.pos))
        p, cards, processed = self.advance(restored)
        self.assertTrue(processed)
        self.assertEqual(cards, [])
        self.assertEqual(p['last_bar'], (self.stamp+timedelta(minutes=5)).isoformat())

    def test_hold_and_three_levels(self):
        p = self.pos
        for minute in (5, 10, 15):
            p, cards, _ = self.advance(p, minute)
        self.assertEqual(len(cards), 1)
        self.assertIn('續抱', cards[0]['title'])
        self.assertEqual([x['name'] for x in cards[0]['fields'][:3]], ['🔴 壓力 NH', '🟡 交界 CDP', '🟢 支撐 NL'])
        self.assertIn('非實際持倉', cards[0]['description'])
        self.assertEqual(cards[0]['color'], 0x00B875)

    def test_move_applies_next_bar(self):
        # 本根高100.5高於新停損100但低於舊101；不可回溯判定已出場。
        p, cards, _ = self.advance(self.pos, price=99, low=98.9, high=100.5)
        self.assertEqual(p['stop'], 100)
        self.assertIn('移動', cards[0]['title'])
        self.assertIn('下一根', cards[0]['description'])
        p, cards, _ = self.advance(p, minute=10, price=99.8, low=99.7, high=100.1)
        self.assertIsNone(p)
        self.assertIn('回補', cards[0]['title'])

    def test_stop_never_widens(self):
        p = {**self.pos, 'stop': 99.5}
        p2, cards, _ = self.advance(p, price=99.1, low=99, high=99.4)
        self.assertEqual(p2['stop'], 99.5)
        self.assertEqual(cards, [])

    def test_long_side_move_and_exit(self):
        stock = {**self.stock, 'direction': 'long'}
        p = bot.start_followup(stock, {'price':100, 'stop':99, 'bar_at':self.stamp.isoformat()})
        self.assertEqual(p['target'],102)
        p, cards, _ = self.advance(p, price=101, low=99.5, high=101.1, stock=stock)
        self.assertEqual(p['stop'],100)
        self.assertEqual(cards[0]['color'],0xFF253A)
        p, cards, _ = self.advance(p, minute=10, price=101.9, low=101.8, high=102.1, stock=stock)
        self.assertIsNone(p)
        self.assertIn('賣出',cards[0]['title'])

    def test_ambiguous_bar_is_conservative(self):
        p, cards, _ = self.advance(self.pos, price=100, low=97, high=102)
        self.assertIsNone(p)
        self.assertIn('無法判定先後', cards[0]['description'])
        self.assertIn('保守列停損',cards[0]['description'])

    def test_gap_interrupts(self):
        p, cards, _ = self.advance(self.pos, minute=10)
        self.assertIsNone(p)
        self.assertIn('中斷',cards[0]['title'])

    def test_duplicate_and_stale_do_not_notify(self):
        p, cards, _ = self.advance(self.pos)
        same, cards, processed = self.advance(p)
        self.assertFalse(processed)
        self.assertEqual(cards, [])
        stale, cards, processed = self.advance(self.pos, age=21)
        self.assertFalse(processed)
        self.assertEqual(stale,self.pos)

    def test_time_exit(self):
        prev = self.stamp.replace(hour=13,minute=10)
        p = {**self.pos,'last_bar':prev.isoformat()}
        minute = int((prev+timedelta(minutes=5)-self.stamp).total_seconds()/60)
        p, cards, _ = self.advance(p,minute=minute)
        self.assertIsNone(p)
        self.assertIn('13:20',cards[0]['description'])

    def test_next_day_invalidates(self):
        p = {**self.pos,'day':'2026-10-05'}
        p, cards, _ = self.advance(p)
        self.assertIsNone(p)
        self.assertIn('失效',cards[0]['title'])

    def test_invalid_initial_risk(self):
        with self.assertRaises(ValueError):
            bot.start_followup(self.stock, {'price':100,'stop':99,'bar_at':self.stamp.isoformat()})

    def test_demo_is_marked_and_does_not_write_positions(self):
        sent = []
        original = bot.send_embeds
        try:
            bot.send_embeds = lambda title,cards: sent.extend(cards)
            bot.test_followup_cards()
        finally:
            bot.send_embeds = original
        self.assertEqual(len(sent),4)
        self.assertTrue(all('合成示範' in x['description'] for x in sent))
        self.assertTrue(all(len(x['fields']) == 5 for x in sent))


if __name__ == '__main__':
    unittest.main()
