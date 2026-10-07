"""2455 全新: 2026-10-08 short premarket plan and date-limited 5m alerts."""
import argparse
import logging
import math
import os
import time
import traceback
import urllib.parse
from datetime import date, datetime, timedelta, time as clock

import pandas as pd
import short_discord as base
import stock_discord as ranking

BASELINE = date(2026, 10, 7)
TARGET = date(2026, 10, 8)
TICKER = "2455.TW"
FILE = "stock2455.json"
EVENTS = "stock2455_events.json"
LOG = logging.getLogger("stock2455")


def verify_channel(expected):
    hook = os.getenv("DISCORD_WEBHOOK_URL", "")
    u = urllib.parse.urlparse(hook)
    if u.scheme != "https" or u.hostname not in {"discord.com", "discordapp.com"} or not u.path.startswith("/api/webhooks/"):
        raise RuntimeError("Missing valid webhook")
    if base.get_json(hook).get("channel_id") != expected:
        raise RuntimeError("Unexpected channel; nothing sent")


def baseline():
    rows = ranking.listed_for_date(str(BASELINE))
    row = next((r for r in rows if r['code'] == '2455'), None)
    if not row or row['date'] != str(BASELINE):
        raise RuntimeError("Official baseline date unavailable")
    stock = dict(code="2455", name="全新", ticker=TICKER, close=row['close'],
                 high=row['high'], low=row['low'], target_date=str(TARGET), direction='short',
                 **base.cdp_levels(row['high'], row['low'], row['close'], BASELINE))
    LOG.info("2455 baseline %s high=%.2f low=%.2f close=%.2f NH=%.2f CDP=%.2f NL=%.2f", BASELINE,
             stock["high"], stock["low"], stock["close"], stock["cdp_resistance"], stock["pivot"], stock["support"])
    return stock


def plan_cards(s):
    r, p, n = s['cdp_resistance'], s['pivot'], s['support']
    extreme = p - (s['high'] - s['low'])
    return [base.stock_card(s, "🟢 全新2455｜10/8盤前做空策略",
        f"交易日 {TARGET}｜基準 {BASELINE} 高{s['high']:.2f}／低{s['low']:.2f}／收{s['close']:.2f}\n"
        "09:00～09:15先觀察；只有下列條件全部成立，才發做空條件通知。", 'short', [
        dict(name='撐壓位置', inline=False,
             value=f"壓力：NH {r:.2f}～昨高 {s['high']:.2f}；交界CDP {p:.2f}。\n"
                   f"支撐：昨低 {s['low']:.2f}～NL {n:.2f}；延伸AL {extreme:.2f}為計算參考。"),
        dict(name='做空觸發條件', inline=False,
             value=f"已完成5分K由上收盤下穿CDP {p:.2f}或NL {n:.2f}，收黑且低於當日VWAP。\n"
                   "本根量≥前20根有效5分K均量1.5倍（可含前一交易日）；最近3根K棒連續且有效。\n"
                   "K棒收盤距今≤5分鐘；09:15起判斷，13:00起不發新進場條件。"),
        dict(name='風險與失效條件', inline=False,
             value="最近3根完成5分K最高價作初始停損，訊號價至停損≤1.5%；2R作回補參考。\n"
                   f"開盤較昨收下跌≥3%不追空。5分K站回CDP {p:.2f}／VWAP，先停止追空觀察。\n"
                   "上穿壓力提醒表示空方受壓，並非做多進場訊號。"),
        dict(name='推播方式', inline=False,
             value="每60秒檢查Yahoo 5分K；每根新完成K棒發摘要，另發撐壓觸價／穿越、做空條件及模擬續抱／停損／2R回補。\n"
                   "資料可能延遲，不能保證交易所即時；請核對券商報價及可空資格。沒有自動下單。"),
    ])]


def prepare():
    s = baseline()
    old = base.load(FILE, {})
    s["plan_sent"] = old.get("plan_sent", False) if old.get("target_date") == str(TARGET) else False
    base.save(FILE, s)
    verify_channel("1556623631885271101")
    if not s["plan_sent"]:
        base.send_embeds(f"📋 2455 全新｜{TARGET} 做空盤前策略｜條件計畫，非已觸發訊號", plan_cards(s))
        s["plan_sent"] = True
        base.save(FILE, s)


def strategy(frame, s, current, direction="short"):
    if direction != "short" or current.date() != TARGET or not clock(9, 15) <= current.time() < clock(13):
        return None
    bars = base.completed_bars(frame, current)
    if len(bars) < 3 or (bars.Volume <= 0).any():
        return None
    recent = bars.iloc[-3:]
    for _, row in recent.iterrows():
        values = [float(row[k]) for k in ("Open", "High", "Low", "Close", "Volume")]
        if not all(math.isfinite(v) and v > 0 for v in values):
            return None
        opened, high, low, closed, _ = values
        if not low <= min(opened, closed) <= max(opened, closed) <= high:
            return None
    if any((recent.index[i] - recent.index[i-1]).total_seconds() != 300 for i in range(1, 3)):
        return None
    end = bars.index[-1].to_pydatetime() + timedelta(minutes=5)
    age = (current - end).total_seconds() / 60
    if not 0 <= age <= 5:
        return None
    prior = frame.copy()
    ix = pd.DatetimeIndex(prior.index)
    prior.index = ix.tz_localize(base.TZ) if ix.tz is None else ix.tz_convert(base.TZ)
    prior = prior[~prior.index.duplicated(keep="last")].sort_index()
    prior = prior[prior.index <= bars.index[-1]]
    history = prior.Volume.iloc[-21:-1]
    if len(history) < 20 or not all(math.isfinite(float(v)) and v > 0 for v in history):
        return None
    ratio = float(bars.Volume.iloc[-1]) / float(history.mean())
    typical = (bars.High + bars.Low + bars.Close) / 3
    vwap = float((typical * bars.Volume).sum() / bars.Volume.sum())
    last, previous = bars.iloc[-1], float(bars.Close.iloc[-2])
    price, opened = float(last.Close), float(last.Open)
    opening = float(bars.Open.iloc[0])
    if opening <= s["close"] * .97:
        return None
    levels = [s["pivot"], s["support"]]
    crossed = [v for v in levels if previous >= v > price]
    stop = float(recent.High.max())
    risk = stop - price
    if not (crossed and ratio >= 1.5 and 0 < risk / price <= .015
            and price < opened and price < vwap):
        return None
    return dict(price=price, stop=stop, vwap=vwap, bar_at=bars.index[-1].isoformat(),
                age_minutes=round(age, 1), trigger=crossed[-1], volume_ratio_5m=ratio,
                volume_shares=float(last.Volume), volume_base_shares=float(history.mean()), volume_base_count=20)


def monitor(connection=False):
    if not connection and base.now_tw().date() != TARGET:
        LOG.info("Date-limited monitor: no action outside %s", TARGET)
        return
    if not connection and not clock(8, 30) <= base.now_tw().time() <= clock(13, 35):
        LOG.info("Outside target session")
        return
    s = base.load(FILE, {})
    if s.get("target_date") != str(TARGET) or s.get("levels_date") != str(BASELINE):
        raise RuntimeError("Prepared baseline missing")
    verify_channel("1556619339573231726")
    if connection:
        base.send_embeds(f"✅ 2455 全新｜{TARGET} 做空5分K推播設定成功｜這是設定確認，非即時訊號",
                         [base.stock_card(s, "明日全新專屬監控", "09:00～13:30每60秒檢查5分K，每根新完成K棒發摘要；09:15起判斷做空進場條件。\n"
                          "另有觸價、完成5分K上穿／下穿，以及訊號後的模擬續抱／停損／2R出場提醒。\n"
                          "只監控全新做空，僅2026-10-08執行。行情可能延遲；沒有下單或实际持倉紀錄。", "short")])
        return
    state = base.load(EVENTS, dict(events={}, crossings={}, positions={}))
    if not state.get("started"):
        base.send_discord(f"🔎 2455 全新做空5分K監控啟動｜{TARGET}｜CDP基準{BASELINE}\n每60秒检查；觸價與收盤穿越是價位提醒，進場條件需額外符合VWAP、量比與停損距離。")
        state["started"] = True
        base.save(EVENTS, state)
    failures, seen = 0, False
    while base.now_tw().date() == TARGET and base.now_tw().time() <= clock(13, 35):
        current = base.now_tw()
        if current.time() < clock(9):
            time.sleep(30)
            continue
        try:
            frame = base.frame_for(base.download([TICKER], "5m", "5d"), TICKER)
            seen = seen or not base.completed_bars(frame, current).empty
            bars = base.completed_bars(frame, current)
            if not bars.empty:
                stamp = bars.index[-1]
                age = (current - (stamp.to_pydatetime()+timedelta(minutes=5))).total_seconds()/60
                key = stamp.isoformat()
                if 0 <= age <= 20 and key != state.get('last_summary') and bars.Volume.sum() > 0:
                    last = bars.iloc[-1]
                    vwap = float((((bars.High+bars.Low+bars.Close)/3)*bars.Volume).sum()/bars.Volume.sum())
                    base.send_embeds('🕯️ 2455 全新｜完成5分K摘要（非進場訊號）' if age <= 5 else '🕒 2455 全新｜延遲5分K摘要（停止新進場條件）', [base.stock_card(s,
                        '全新做空觀察｜5分K更新',
                        f"K棒收盤 {(stamp+timedelta(minutes=5)):%H:%M}｜距今{age:.1f}分\n"
                        f"開{last.Open:.2f}／高{last.High:.2f}／低{last.Low:.2f}／收{last.Close:.2f}\n"
                        f"VWAP {vwap:.2f}｜{'低於VWAP' if last.Close<vwap else '站上VWAP，停止追空觀察'}\n"
                        '行情可能延遲；此摘要不表示做空條件成立。', 'short', base.volume_fields(base.volume_metrics(bars)))])
                    state['last_summary'] = key
                    base.save(EVENTS, state)
            for kind, fn, send in [("touch", base.touch_signal, base.send_touch), ("cross", base.crossing_signal, base.send_crossing)]:
                hit = fn(frame, s, current)
                if hit:
                    for item in hit["crossed"]:
                        key = f"{kind}:{hit['bar_at']}:{item['name']}:{item['direction']}"
                        if key not in state["crossings"]:
                            send(s, {**hit, "crossed": [item]})
                            state["crossings"][key] = True
                            base.save(EVENTS, state)
            for direction in ("short",):
                stock = {**s, "direction": direction}
                if direction in state["positions"]:
                    pos, cards, changed = base.advance_followup(frame, stock, current, state["positions"][direction])
                    if changed:
                        for c in cards:
                            base.send_embeds("🔔 全新做空5分K訊號模擬追蹤", [c])
                        if pos is None:
                            state["positions"].pop(direction, None)
                        else:
                            state["positions"][direction] = pos
                        base.save(EVENTS, state)
                    continue
                hit = strategy(frame, s, current, direction)
                ident = {**stock, "ticker": TICKER + ":" + direction}
                if hit and current.time() < clock(13) and not state["positions"] and base.notification_allowed(hit, ident, current, state["events"]):
                    pos = base.start_followup(stock, hit)
                    c = base.stock_card(s, "🟢 全新做空條件成立",
                        f"完成5分K收盤 {hit['price']:.2f}｜{hit['bar_at']}\n"
                        f"下穿 {hit['trigger']:.2f}｜VWAP {hit['vwap']:.2f}｜量比 {hit['volume_ratio_5m']:.2f}\n"
                        f"初始停損 {hit['stop']:.2f}｜2R參考 {pos['target']:.2f}\n開始模擬追蹤，非實際持倉；請核對即時報價。", direction, base.volume_fields(hit))
                    base.send_embeds("🔔 2455 全新做空5分K", [c])
                    state["events"][f"{current.date()}:{ident['ticker']}:{hit['bar_at']}"] = hit
                    state["positions"][direction] = pos
                    base.save(EVENTS, state)
            failures = 0
        except Exception:
            failures += 1
            LOG.warning("Monitor round failed; secrets omitted")
            if failures >= 5:
                raise RuntimeError("Five failed rounds") from None
        if current.time() >= clock(9, 50) and not seen:
            base.send_discord("⚠️ 全新09:50仍無今日有效K棒；可能休市、停牌或資料異常，已停止，沒有通知不代表沒有訊號。")
            return
        time.sleep(60)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("mode", choices=["prepare", "monitor", "connection"])
    mode = p.parse_args().mode
    try:
        prepare() if mode == "prepare" else monitor(connection=mode == "connection")
    except Exception as error:
        LOG.error("2455 task failed (%s)", type(error).__name__)
        for f in traceback.extract_tb(error.__traceback__):
            LOG.error("Location: %s:%s in %s", os.path.basename(f.filename), f.lineno, f.name)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
