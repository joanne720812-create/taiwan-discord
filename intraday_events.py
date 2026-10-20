"""Independent one-minute moves and single-trade volume alerts (share units).

REST polling is not a complete tick feed. A bounded trades response may omit
trades in an active stock; report that gap rather than claiming no large trades.
"""
import logging
import math
import os
from datetime import datetime, timedelta, time as clock

import pandas as pd

LOG = logging.getLogger(__name__)
STATE_FILE = "intraday_events_v1.json"
MAX_AGE = 180


def enabled():
    return os.getenv("THREE_EVENT_ALERTS") == "1"


def volume_baseline(frame, asof):
    """Last five completed trading-day daily volumes, including asof, in shares."""
    df = frame[[stamp.date() <= asof for stamp in frame.index]].sort_index()
    if len(df) < 5 or df.index[-1].date() != asof or df.index.has_duplicates:
        return {}
    values = [float(v) for v in df.Volume.iloc[-5:]]
    if not all(math.isfinite(v) and v > 0 for v in values):
        return {}
    return {"avg5_daily_shares": sum(values) / 5,
            "avg5_date": asof.isoformat(), "avg5_count": 5}


def minute_events(frame, current):
    if frame.empty:
        return []
    index = pd.DatetimeIndex(frame.index)
    if index.tz is None or index.has_duplicates:
        return []
    frame = frame.sort_index()
    frame = frame[(frame.index.date == current.date()) &
                  (frame.index.time >= clock(9)) & (frame.index.time < clock(13, 30)) &
                  (frame.index + pd.Timedelta(minutes=1) <= current)]
    found = []
    for i in range(max(1, len(frame) - 4), len(frame)):
        at = frame.index[i]
        end = at.to_pydatetime() + timedelta(minutes=1)
        if (at - frame.index[i - 1]).total_seconds() != 60 or not 0 <= (current - end).total_seconds() <= MAX_AGE:
            continue
        previous, price = float(frame.Close.iloc[i - 1]), float(frame.Close.iloc[i])
        if not all(math.isfinite(v) and v > 0 for v in (previous, price)):
            continue
        if frame.Volume.iloc[i - 1] <= 0 or frame.Volume.iloc[i] <= 0:
            continue
        change = (price / previous - 1) * 100
        kind = "up" if change >= 1 - 1e-10 else "down" if change <= -1 + 1e-10 else None
        if kind:
            found.append({"kind": kind, "at": end.isoformat(), "price": price,
                          "previous": previous, "change": change,
                          "key": f"{kind}:{end.isoformat()}"})
    return found


def trade_events(payload, stock, current, since):
    symbol, market = stock["ticker"].split(".")
    if (payload.get("date") != current.date().isoformat() or payload.get("symbol") != symbol
            or payload.get("exchange") != {"TW": "TWSE", "TWO": "TPEx"}[market]
            or payload.get("type") != "EQUITY"):
        raise ValueError("Trade metadata mismatch")
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise ValueError("Missing trade rows")
    found, stamps = [], []
    baseline = stock.get("avg5_daily_shares", 0)
    baseline_ok = (math.isfinite(baseline) and baseline > 0 and stock.get("avg5_count") == 5
                   and stock.get("avg5_date") == stock.get("levels_date"))
    for row in rows:
        stamp = datetime.fromtimestamp(float(row["time"]) / 1_000_000, current.tzinfo)
        if stamp.date() != current.date() or stamp > current:
            raise ValueError("Trade timestamp mismatch")
        if not clock(9) <= stamp.time() <= clock(13, 30):
            continue
        stamps.append(stamp)
        size, price = float(row["size"]) * 1000, float(row["price"])
        if not all(math.isfinite(v) and v > 0 for v in (size, price)) or "serial" not in row:
            raise ValueError("Invalid single trade")
        if stamp <= since or (current - stamp).total_seconds() > MAX_AGE:
            continue
        if baseline_ok and size >= baseline * .02 - 1e-8:
            found.append({"kind": "large", "at": stamp.isoformat(), "price": price,
                          "shares": size, "threshold": baseline * .02, "avg5": baseline,
                          "key": f"large:{stamp.isoformat()}:{row['serial']}"})
    # A full response which does not reach the previous cursor is incomplete.
    gap = len(rows) >= 500 and (not stamps or min(stamps) > since)
    return found, gap, baseline_ok


def event_card(stock, event):
    names = {"up": "🔴 瞬間拉抬", "down": "🟢 瞬間殺盤", "large": "🟡 盤中單筆巨量"}
    at = datetime.fromisoformat(event["at"])
    description = f"**{names[event['kind']]}**｜{at:%m/%d %H:%M:%S}\n股價 **{event['price']:.2f}**\n"
    if event["kind"] == "large":
        description += (f"單筆 **{event['shares']/1000:,.2f} 張** ≥ 門檻 **{event['threshold']/1000:,.2f} 張**\n"
                        f"前5交易日平均日量 {event['avg5']/1000:,.2f} 張 × 2%（基準 {stock['avg5_date']}）\n"
                        "巨量本身不代表主動買進或賣出。")
    else:
        description += (f"前一分鐘收盤 {event['previous']:.2f} → 本分鐘收盤 {event['price']:.2f}\n"
                        f"變動 **{event['change']:+.2f}%**｜相鄰已完成1分K比較。")
    direction = stock.get("direction", "rising")
    label = {"long": "做多候選", "short": "做空候選", "rising": "起漲股"}[direction]
    description += f"\n所屬：{label}｜獨立異動提醒，未代表原5分K策略已符合。"
    resistance = stock.get("cdp_resistance", stock.get("resistance"))
    fields = [{"name": label, "value": f"**{value:.2f}**", "inline": True}
              for label, value in (("🔴 壓力 NH", resistance), ("🟡 交界 CDP", stock.get("pivot")),
                                   ("🟢 支撐 NL", stock.get("support"))) if value is not None]
    return {"title": f"{names[event['kind']]}｜{stock['code']} {stock['name']}",
            "description": description, "fields": fields,
            "color": {"up": 0xFF253A, "down": 0x00B875, "large": 0xF1C40F}[event["kind"]],
            "footer": {"text": "富果REST輪詢，可能延遲或漏筆；條件提醒，非下單指令"}}


def check_stock(stock, current, base):
    """Uses the one-minute response cached by the existing five-minute sweep."""
    if not enabled():
        return
    import fugle_intraday as fugle
    if os.getenv("INTRADAY_DATA_SOURCE") != "fugle":
        raise RuntimeError("Three events require Fugle data")
    if current.weekday() >= 5 or not clock(9) <= current.time() <= clock(13, 30):
        return
    state = base.load(STATE_FILE, {})
    if state.get("date") != current.date().isoformat():
        state = {"date": current.date().isoformat(), "sent": [], "cursors": {}, "warnings": []}
    ticker = stock["ticker"]
    scope = f"{ticker}:{stock.get('direction', 'rising')}"

    def warn(code, text):
        key = scope + ":" + code
        if key not in state["warnings"]:
            base.send_discord(f"⚠️ {stock['code']} {stock['name']}｜{text}", stock.get("direction"))
            state["warnings"].append(key)
            base.save(STATE_FILE, state)

    cached = fugle.minute_cache.get(ticker)
    if not cached or (current - cached[0]).total_seconds() > MAX_AGE:
        warn("minute", "1分K資料不足，暫不判斷拉抬／殺盤。")
        return
    found = minute_events(cached[1], current)
    since = datetime.fromisoformat(state["cursors"].get(scope, (current - timedelta(seconds=MAX_AGE)).isoformat()))
    try:
        payload = fugle.request_json(ticker, "trades", "limit=500&sort=desc")
        found_trades, gap, baseline_ok = trade_events(payload, stock, base.now_tw(), since)
        found.extend(found_trades)
        if gap:
            warn("gap", "成交明細500筆未覆蓋上次檢查時間，期間可能漏掉巨量；不代表沒有訊號。")
        if not baseline_ok:
            warn("baseline", "缺少有效前5交易日均量，單筆巨量暫停偵測；1分K異動仍檢查。")
        # Query start is a conservative cursor: trades during the request are rechecked.
        state["cursors"][scope] = current.isoformat()
    except Exception:
        LOG.warning("Trade events unavailable for %s; no provider error body logged", ticker)
        warn("trades", "成交明細下載或驗證失敗，單筆巨量暫時無法確認；1分K異動仍檢查。")
    for event in found:
        key = scope + ":" + event["key"]
        if key in state["sent"]:
            continue
        card = event_card(stock, event)
        base.send_embeds("🔔 盤中三項異動偵測", [card], stock.get("direction"))
        state["sent"].append(key)
        base.save(STATE_FILE, state)
    base.save(STATE_FILE, state)
