"""3374 精材: 2026-10-08 premarket plan and date-limited 5m alerts."""
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

BASELINE = date(2026, 10, 7)
TARGET = date(2026, 10, 8)
TICKER = "3374.TWO"
FILE = "stock3374.json"
EVENTS = "stock3374_events.json"
LOG = logging.getLogger("stock3374")


def verify_channel(expected):
    hook = os.getenv("DISCORD_WEBHOOK_URL", "")
    u = urllib.parse.urlparse(hook)
    if u.scheme != "https" or u.hostname not in {"discord.com", "discordapp.com"} or not u.path.startswith("/api/webhooks/"):
        raise RuntimeError("Missing valid webhook")
    if base.get_json(hook).get("channel_id") != expected:
        raise RuntimeError("Unexpected channel; nothing sent")


def baseline():
    rows = base.get_json(base.SOURCES["TWO"])
    row = next((r for r in rows if str(base.pick(r, "SecuritiesCompanyCode", "Code", "證券代號")) == "3374"), None)
    if not row or base.parse_date(base.pick(row, "Date", "日期")) != BASELINE:
        raise RuntimeError("Official baseline date unavailable")
    official = base.number(base.pick(row, "Close", "ClosingPrice", "收盤價"))
    df = base.frame_for(base.download([TICKER], "1d", "6mo"), TICKER)
    df = df[[i.date() <= BASELINE for i in df.index]]
    if df.empty or df.index[-1].date() != BASELINE:
        raise RuntimeError("Daily baseline missing")
    daily = df.iloc[-1]
    close = float(daily.Close)
    if official is None or abs(close - official) > max(.05, close * .001):
        raise RuntimeError("Official close mismatch")
    stock = dict(code="3374", name="精材", ticker=TICKER, close=official,
                 high=float(daily.High), low=float(daily.Low), target_date=str(TARGET),
                 **base.cdp_levels(daily.High, daily.Low, close, BASELINE))
    LOG.info("3374 baseline %s high=%.2f low=%.2f close=%.2f NH=%.2f CDP=%.2f NL=%.2f", BASELINE,
             stock["high"], stock["low"], stock["close"], stock["cdp_resistance"], stock["pivot"], stock["support"])
    return stock


def plan_cards(s):
    r, p, n = s["cdp_resistance"], s["pivot"], s["support"]
    common = f"明日 {TARGET}｜基準 {BASELINE} 高{s['high']:.2f}／低{s['low']:.2f}／收{s['close']:.2f}\n"
    rule = "09:00～09:15先觀察。進場條件只看已完成5分K，前20根5分K均量至少1.5倍；最近3根停損距離≤1.5%，目標2R。"
    long = base.stock_card(s, "🔴 明日盤前做多策略", common + rule, "long", [
        dict(name="🔴 做多觸發", inline=False,
             value=f"5分K收盤上穿交界 {p:.2f} 或壓力 {r:.2f}，收紅且站上當日VWAP，才觀察進場。\n"
                   f"交界上穿先看壓力 {r:.2f}；壓力突破等回測守住。開盤≥{s['close']*1.03:.2f}不追多。"),
        dict(name="🛡️ 停損與停利", inline=False,
             value="最近3根完成5分K低點作初始停損；訊號價與停損差距超過1.5%則不發進場條件。\n"
                   "以初始風險2倍為停利參考；行情觸及停損、2R或13:20後會發模擬出場提醒。"),
    ])
    short = base.stock_card(s, "🟢 明日盤前做空策略", common + rule, "short", [
        dict(name="🟢 做空觸發", inline=False,
             value=f"5分K收盤下穿交界 {p:.2f} 或支撐 {n:.2f}，收黑且低於當日VWAP，才觀察進場。\n"
                   f"交界下穿先看支撐 {n:.2f}；支撐跌破等反彈未站回。開盤≤{s['close']*.97:.2f}不追空。"),
        dict(name="🛡️ 停損與回補", inline=False,
             value="最近3根完成5分K高點作初始停損；距離超過1.5%則不發進場條件。\n"
                   "以初始風險2倍為回補參考；先確認券商可空與當沖資格。"),
    ])
    return [long, short]


def prepare():
    s = baseline()
    old = base.load(FILE, {})
    s["plan_sent"] = old.get("plan_sent", False) if old.get("target_date") == str(TARGET) else False
    base.save(FILE, s)
    verify_channel("1556623631885271101")
    if not s["plan_sent"]:
        base.send_embeds(f"📋 3374 精材｜{TARGET} 多空盤前策略｜條件計畫，非已觸發訊號", plan_cards(s))
        s["plan_sent"] = True
        base.save(FILE, s)


def strategy(frame, s, current, direction):
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
    if not 0 <= age <= 20:
        return None
    prior = frame.copy()
    ix = pd.DatetimeIndex(prior.index)
    prior.index = ix.tz_localize(base.TZ) if ix.tz is None else ix.tz_convert(base.TZ)
    prior = prior[~prior.index.duplicated(keep="last")].sort_index()
    prior = prior[prior.index <= bars.index[-1]]
    history = prior.Volume.iloc[-21:-1]
    if len(history) < 20 or (history <= 0).any():
        return None
    ratio = float(bars.Volume.iloc[-1]) / float(history.mean())
    typical = (bars.High + bars.Low + bars.Close) / 3
    vwap = float((typical * bars.Volume).sum() / bars.Volume.sum())
    last, previous = bars.iloc[-1], float(bars.Close.iloc[-2])
    price, opened = float(last.Close), float(last.Open)
    long = direction == "long"
    opening = float(bars.Open.iloc[0])
    if (long and opening >= s["close"] * 1.03) or (not long and opening <= s["close"] * .97):
        return None
    levels = [s["pivot"], s["cdp_resistance"] if long else s["support"]]
    crossed = [v for v in levels if (previous <= v < price if long else previous >= v > price)]
    stop = float(recent.Low.min() if long else recent.High.max())
    risk = price - stop if long else stop - price
    if not (crossed and ratio >= 1.5 and 0 < risk / price <= .015
            and (price > opened and price > vwap if long else price < opened and price < vwap)):
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
        base.send_embeds(f"✅ 3374 精材｜{TARGET} 多空5分K推播設定成功｜這是設定確認，非即時訊號",
                         [base.stock_card(s, "明日精材專屬監控", "09:00～13:30每60秒檢查5分K；09:15起判斷多空進場條件。\n"
                          "另有觸價、完成5分K上穿／下穿，以及訊號後的模擬續抱／停損／2R出場提醒。\n"
                          "只監控精材，僅2026-10-08執行。行情可能延遲；沒有下單或实际持倉紀錄。", "long")])
        return
    state = base.load(EVENTS, dict(events={}, crossings={}, positions={}))
    if not state.get("started"):
        base.send_discord(f"🔎 3374 精材多空5分K監控啟動｜{TARGET}｜CDP基準{BASELINE}\n每60秒检查；觸價與收盤穿越是價位提醒，進場條件需額外符合VWAP、量比與停損距離。")
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
            for kind, fn, send in [("touch", base.touch_signal, base.send_touch), ("cross", base.crossing_signal, base.send_crossing)]:
                hit = fn(frame, s, current)
                if hit:
                    for item in hit["crossed"]:
                        key = f"{kind}:{hit['bar_at']}:{item['name']}:{item['direction']}"
                        if key not in state["crossings"]:
                            state["crossings"][key] = True
                            base.save(EVENTS, state)
                            send(s, {**hit, "crossed": [item]})
            for direction in ("long", "short"):
                stock = {**s, "direction": direction}
                if direction in state["positions"]:
                    pos, cards, changed = base.advance_followup(frame, stock, current, state["positions"][direction])
                    if changed:
                        if pos is None:
                            state["positions"].pop(direction, None)
                        else:
                            state["positions"][direction] = pos
                        base.save(EVENTS, state)
                        for c in cards:
                            base.send_embeds("🔔 精材5分K訊號模擬追蹤", [c])
                    continue
                hit = strategy(frame, s, current, direction)
                ident = {**stock, "ticker": TICKER + ":" + direction}
                if hit and current.time() < clock(13) and not state["positions"] and base.notification_allowed(hit, ident, current, state["events"]):
                    pos = base.start_followup(stock, hit)
                    state["events"][f"{current.date()}:{ident['ticker']}:{hit['bar_at']}"] = hit
                    state["positions"][direction] = pos
                    base.save(EVENTS, state)
                    c = base.stock_card(s, "🔴 精材做多條件成立" if direction == "long" else "🟢 精材做空條件成立",
                        f"完成5分K收盤 {hit['price']:.2f}｜{hit['bar_at']}\n"
                        f"{'上穿' if direction == 'long' else '下穿'} {hit['trigger']:.2f}｜VWAP {hit['vwap']:.2f}｜量比 {hit['volume_ratio_5m']:.2f}\n"
                        f"初始停損 {hit['stop']:.2f}｜2R參考 {pos['target']:.2f}\n開始模擬追蹤，非實際持倉；請核對即時報價。", direction, base.volume_fields(hit))
                    base.send_embeds("🔔 3374 精材多空5分K", [c])
            failures = 0
        except Exception:
            failures += 1
            LOG.warning("Monitor round failed; secrets omitted")
            if failures >= 5:
                raise RuntimeError("Five failed rounds") from None
        if current.time() >= clock(9, 50) and not seen:
            base.send_discord("⚠️ 精材09:50仍無今日有效K棒；可能休市、停牌或資料異常，已停止，沒有通知不代表沒有訊號。")
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
        LOG.error("3374 task failed (%s)", type(error).__name__)
        for f in traceback.extract_tb(error.__traceback__):
            LOG.error("Location: %s:%s in %s", os.path.basename(f.filename), f.lineno, f.name)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
