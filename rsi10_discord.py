"""Daily RSI shortlist and full-session completed-5m-bar observation; no orders."""
import argparse
import json
import logging
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, time as clock

import pandas as pd
import short_discord as base
from rising5_discord import rsi, batches, cdp_levels

LOG = logging.getLogger("rsi10")
CHANNEL_ID = "1556650418623352872"
REPORT = "rsi10_candidates.json"
EVENTS = "rsi10_events.json"
MAX_AGE_MINUTES = 30


def send(payload, dry_run=False):
    if isinstance(payload, str):
        payload = {"content": payload}
    cards = payload.get("embeds", [])
    groups, group, size = [], [], 0
    for card in cards:
        chars = (len(card.get("title", "")) + len(card.get("description", ""))
                 + len(card.get("footer", {}).get("text", ""))
                 + len(card.get("author", {}).get("name", ""))
                 + sum(len(field.get("name", "")) + len(field.get("value", ""))
                       for field in card.get("fields", [])))
        if chars > 6000:
            raise ValueError("One card exceeds Discord embed character limit")
        if group and (len(group) >= 5 or size + chars > 6000):
            groups.append(group)
            group, size = [], 0
        group.append(card)
        size += chars
    if group:
        groups.append(group)
    if len(groups) > 1:
        for offset, batch in enumerate(groups):
            send(dict(payload, embeds=batch, content=payload.get("content", "") if offset == 0 else "📋 RSI圖卡（續）"), dry_run)
        return
    payload["allowed_mentions"] = {"parse": []}
    if len(payload.get("content", "")) > 2000:
        raise ValueError("Notification too long")
    if dry_run:
        print(json.dumps(payload, ensure_ascii=False))
        return
    hook = os.environ.get("DISCORD_RSI_WEBHOOK_URL", "")
    parsed = urllib.parse.urlparse(hook)
    if parsed.scheme != "https" or parsed.hostname not in {"discord.com", "discordapp.com"} or not parsed.path.startswith("/api/webhooks/"):
        raise RuntimeError("Missing RSI channel webhook")
    # Verify destination before sending; never fall back to existing channels.
    try:
        with urllib.request.urlopen(urllib.request.Request(hook, headers={"User-Agent": "RSI-observer"}), timeout=20) as response:
            metadata = json.load(response)
        if str(metadata.get("channel_id")) != CHANNEL_ID:
            raise RuntimeError("RSI webhook points to unexpected channel")
        url = hook + ("&" if parsed.query else "?") + "wait=true"
        body = json.dumps(payload, ensure_ascii=False).encode()
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "User-Agent": "RSI-observer"})
                with urllib.request.urlopen(req, timeout=20) as response:
                    message = json.load(response)
                if str(message.get("channel_id")) != CHANNEL_ID or not message.get("id"):
                    raise RuntimeError("Discord delivery was not confirmed")
                LOG.info("Discord accepted message in RSI channel")
                return
            except urllib.error.HTTPError as error:
                if error.code != 429 or attempt == 2:
                    try:
                        details = json.load(error)
                        LOG.error("Discord HTTP %s payload errors: %s", error.code, details.get("errors", {}))
                    except (ValueError, TypeError):
                        LOG.error("Discord HTTP %s", error.code)
                    raise
                try:
                    pause = float(json.load(error).get("retry_after", 2))
                except (ValueError, TypeError):
                    pause = 2
                time.sleep(min(max(pause, 1), 30))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        # Never print URLs containing credentials.
        raise RuntimeError("RSI Discord connection failed") from None


def candidate(frame, stock, asof):
    if frame.empty or len(frame) < 40 or frame.index[-1].date() != asof:
        return None, "missing_history"
    if frame.index.duplicated().any():
        return None, "duplicate_dates"
    close = float(frame.Close.iloc[-1])
    if not math.isfinite(close) or abs(close - stock["official_close"]) > max(.05, close * .001):
        return None, "close_mismatch"
    # Raw-price RSI matches the Pine definition. Skip corporate-action windows
    # where price gaps may mechanically alter RSI and moving averages.
    if "Adj Close" in frame:
        factor = (frame["Adj Close"] / frame.Close).tail(40)
        if not all(math.isfinite(float(x)) and x > 0 for x in factor):
            return None, "invalid_adjustment"
        if float(factor.max() - factor.min()) > .0001:
            return None, "corporate_action"
    strength = rsi(frame.Close)
    dr, prior = float(strength.iloc[-1]), float(strength.iloc[-2])
    avg = float(frame.Volume.iloc[-6:-1].mean())
    vr = float(frame.Volume.iloc[-1]) / avg if avg > 0 else float("nan")
    ma5 = float(frame.Close.iloc[-5:].mean())
    recent = strength.iloc[-10:]
    if not all(math.isfinite(x) for x in (dr, prior, vr, ma5)) or recent.isna().any():
        return None, "invalid_indicators"
    if float(recent.max()) < 50 and vr < 1:
        return None, "persistent_weak"
    if dr < 50 or vr < 1 or close <= ma5 or dr <= prior:
        return None, "not_qualified"
    levels = cdp_levels(float(frame.High.iloc[-1]), float(frame.Low.iloc[-1]), close, asof.isoformat())
    item = dict(stock, daily_rsi=dr, daily_relvol=vr, rsi_change=dr-prior,
                ma5=ma5, **levels)
    return item, "qualified"


def official_snapshots(today, latest):
    snapshots = [base.official_snapshot(m, None if latest else today) for m in base.SOURCES]
    dates = {d for d, _ in snapshots}
    if latest and len(dates) > 1:
        # Align lagging OpenAPI data to the newest confirmed official date.
        # The shared TW adapter validates the dated TWSE closing-table fallback.
        newest = max(dates)
        if newest > today:
            raise RuntimeError("Official data is future-dated")
        snapshots = [base.official_snapshot(m, newest) if d < newest else (d, rows)
                     for m, (d, rows) in zip(base.SOURCES, snapshots)]
    return snapshots


def scan(latest=False, dry_run=False, notify=True, slot="daily"):
    current = base.now_tw()
    snapshots = official_snapshots(current.date(), latest)
    dates = {d for d, _ in snapshots}
    if len(dates) != 1:
        raise RuntimeError("Official market dates differ; shortlist not refreshed")
    asof = next(iter(dates))
    if not latest and asof != current.date():
        LOG.info("No official closing data for today; holiday or pending update")
        return None
    if asof > current.date() or (current.date() - asof).days > 10:
        raise RuntimeError("Official data is stale or future-dated")
    if slot == "morning" and asof >= current.date():
        raise RuntimeError("Morning baseline must be a completed prior trading day")
    universe = [s for _, rows in snapshots for s in rows]
    top, counts = [], {}
    data_failures = 0
    for stock, frame in batches(universe, "1d", "6mo"):
        frame = frame[[i.date() <= asof for i in frame.index]]
        item, reason = candidate(frame, stock, asof)
        counts[reason] = counts.get(reason, 0) + 1
        if reason in {"missing_history", "duplicate_dates", "close_mismatch", "invalid_adjustment", "invalid_indicators"}:
            data_failures += 1
        if item:
            top.append(item)
    coverage = 1 - data_failures / max(len(universe), 1)
    if not universe or coverage < .9:
        raise RuntimeError(f"Historical coverage insufficient: {coverage:.1%}; no shortlist published")
    # Ranking is rule-based, not a probability of limit-up or profit.
    top.sort(key=lambda s: (-s["daily_relvol"], -s["rsi_change"], -s["value"], s["ticker"]))
    report = dict(asof=asof.isoformat(), stocks=top[:10], coverage=coverage,
                  total=len(universe), counts=counts, created_at=current.isoformat())
    base.save(REPORT, report)
    if notify:
        events = base.load(EVENTS, {})
        key = f"{current.date()}:{slot}:{asof}:cards-v2-strategy"
        if dry_run or events.get("last_list") != key:
            message = daily_payload(report, slot)
            send(message, dry_run)
            if not dry_run:
                events["last_list"] = key
                base.save(EVENTS, events)
    LOG.info("RSI shortlist: date=%s candidates=%s coverage=%.1f%% counts=%s", asof, len(report["stocks"]), coverage*100, counts)
    return report


def premarket_strategy(stock):
    p, r, s = (stock[k] for k in ("pivot", "resistance", "support"))
    return (
        f"回測做多：回測交界{p:.2f}後，完成5分K重新站上，且RSI≥60、量比≥1.5，再觀察。\n"
        f"突破做多：完成5分K收上壓力{r:.2f}，回測守住再觀察；開在壓力上方先等回測，不追第一根。\n"
        f"停損條件：回測單跌破回測低點；突破單跌回{r:.2f}下方。跌破交界{p:.2f}暫停做多；跌破支撐{s:.2f}排除。\n"
        f"停利參考：回測單接近{r:.2f}分批；突破單用完成5分K低點移動保護。\n"
        "進場前確認即時行情與可成交價；報酬空間不足停損距離2倍則略過。"
    )


def daily_payload(report, slot):
    label = "盤前候選" if slot == "morning" else "下一交易日觀察候選"
    cards = []
    for n, stock in enumerate(report["stocks"], 1):
        cards.append({
            "title": f"{n:02d}｜{stock['code']} {stock['name']}",
            "color": 0xE74C3C,
            "description": f"日線資料日：{report['asof']}\n{label}｜日RSI14量價篩選",
            "fields": [
                {"name": "收盤價", "value": f"**{stock['official_close']:g}**", "inline": True},
                {"name": "日線 RSI14", "value": f"**{stock['daily_rsi']:.1f}**", "inline": True},
                {"name": "日量比", "value": f"**{stock['daily_relvol']:.2f} 倍**", "inline": True},
                {"name": "🔴 壓力", "value": f"**{stock['resistance']:.2f}**", "inline": True},
                {"name": "🟡 交界", "value": f"**{stock['pivot']:.2f}**", "inline": True},
                {"name": "🟢 支撐", "value": f"**{stock['support']:.2f}**", "inline": True},
                {"name": "📋 盤前策略｜條件成立才觀察", "value": premarket_strategy(stock), "inline": False},
            ],
            "footer": {"text": "日線CDP價位｜候選排序不是漲停機率｜非買進指令"},
        })
    content = (f"📋 **RSI強勢觀察｜{label} {len(cards)}檔圖卡**\n"
               f"日線資料日：{report['asof']}\n"
               "條件：日RSI14≥50、日量≥前5日均量、收盤>5日線、RSI較前日上升。\n"
               f"上市＋上櫃流動性初篩{report['total']}檔；歷史資料可用率{report['coverage']:.0%}。\n"
               "監看9:00～13:30完成5分K；RSI≥60且量比≥1.5倍，每根符合都提醒，持續強勢也通知。行情與排程可能延遲，不保證漲停。")
    if not cards:
        content += "\n本次沒有符合股票，不湊滿10檔。"
    return {"content": content, "embeds": cards}


def morning_signals(frame, stock, current, full_session=False):
    if frame.empty:
        return []
    df = frame.copy()
    index = pd.DatetimeIndex(df.index)
    df.index = index.tz_localize(base.TZ) if index.tz is None else index.tz_convert(base.TZ)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df[(df.index + pd.Timedelta(minutes=5)) <= current]
    strength = rsi(df.Close)
    volume_ratio = df.Volume / df.Volume.shift(1).rolling(20, min_periods=20).mean()
    found = []
    for idx in df.index:
        start = idx.to_pydatetime()
        end = start + timedelta(minutes=5)
        if start.date() != current.date() or (not clock(9) <= start.time() < clock(13,30) if full_session else start.time() not in {clock(9), clock(9,5), clock(9,10)}):
            continue
        dr, vr = float(strength.loc[idx]), float(volume_ratio.loc[idx])
        if not math.isfinite(dr) or not math.isfinite(vr):
            continue
        lag = (current - end).total_seconds() / 60
        if not 0 <= lag <= MAX_AGE_MINUTES:
            continue
        status = "重點觀察" if dr >= 60 and vr >= 1.5 else "暫時排除" if dr < 50 else "等待量價轉強"
        levels = cdp_levels(float(df.loc[idx, "High"]), float(df.loc[idx, "Low"]), float(df.loc[idx, "Close"]), end.isoformat())
        found.append(dict(stock, intraday_levels=levels, rsi5=dr, volume_ratio=vr, close=float(df.loc[idx, "Close"]),
                          bar_end=end.isoformat(), lag_minutes=lag, status=status))
    return found


def observe(report, events, dry_run=False):
    for stock, frame in batches(report["stocks"], "5m", "5d"):
        hits = morning_signals(frame, stock, base.now_tw(), full_session=True)
        if not hits:
            continue
        ticker = stock["ticker"]
        # Process newly available completed bars once, within the freshness limit.
        for hit in hits:
            previous = events.setdefault("observed", {}).get(ticker)
            if previous and previous["bar_end"] >= hit["bar_end"]:
                continue
            events["observed"][ticker] = hit
            end = datetime.fromisoformat(hit["bar_end"])
            key = f"{ticker}:{hit['bar_end']}"
            if hit["status"] != "重點觀察" or key in events.setdefault("sent", []):
                continue
            sent_time = base.now_tw()
            lag = (sent_time-end).total_seconds()/60
            if not 0 <= lag <= MAX_AGE_MINUTES:
                continue
            levels = hit["intraday_levels"]
            send(f"🔴 **RSI盤中5分K強勢｜{stock['code']} {stock['name']}**\n"
                 f"K棒收盤：{end:%Y-%m-%d %H:%M}｜訊號收盤價{hit['close']:g}\n"
                 f"送出時間：{sent_time:%H:%M:%S}｜行情距今{lag:.1f}分鐘\n"
                 f"5分RSI{hit['rsi5']:.1f}｜量比{hit['volume_ratio']:.2f}倍\n"
                 f"**5分K壓力{levels['resistance']:.2f}｜交界{levels['pivot']:.2f}｜支撐{levels['support']:.2f}**\n"
                 f"上述價位由這根完成K棒的高、低、收盤計算CDP，供下一根觀察。\n"
                 f"日線壓力{stock['resistance']:.2f}｜交界{stock['pivot']:.2f}｜支撐{stock['support']:.2f}\n"
                 "條件：RSI≥60且量比≥1.5倍；每根符合條件的完成K棒均推播，持續強勢也通知，同根K棒不重複。行情可能延遲，非買進指令。", dry_run)
            if not dry_run:
                events["sent"].append(key)
                base.save(EVENTS, events)
    base.save(EVENTS, events)


def monitor():
    current = base.now_tw()
    if current.weekday() >= 5:
        return
    if current.time() >= clock(13, 55):
        send("⚠️ RSI盤中監控排程啟動過晚，已超過13:55；今天不補發過期強勢訊號。")
        return
    report = scan(latest=True, slot="morning")
    events = base.load(EVENTS, {})
    day = current.date().isoformat()
    if events.get("date") != day:
        events = dict(date=day, last_list=events.get("last_list"), sent=[], observed={})
    while base.now_tw().time() < clock(13,55):
        if base.now_tw().time() >= clock(9,5):
            observe(report, events)
        time.sleep(60)
    if not events.get("summary"):
        observed = events.get("observed", {})
        lines = [f"📌 RSI全日盤中觀察結束｜{day}", f"候選{len(report['stocks'])}檔｜收到有效盤中K棒{len(observed)}檔｜強勢提醒共{len(events.get('sent', []))}次"]
        for stock in report["stocks"]:
            hit = observed.get(stock["ticker"])
            lines.append(f"{stock['code']} {stock['name']}｜" + (hit["status"] if hit else "無可用盤中K棒，無法判定"))
        lines.append("未收到K棒可能為休市、停牌、無成交或資料缺漏；不能視為沒有訊號。最新狀態不代表先前提醒仍成立。")
        send("\n".join(lines))
        events["summary"] = True
        base.save(EVENTS, events)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["preview", "connection", "latest", "scan", "monitor"])
    mode = parser.parse_args().mode
    try:
        if mode == "preview":
            scan(latest=True, dry_run=True, slot="preview")
        elif mode == "connection":
            send("✅ RSI強勢觀察自動推播連線測試成功。\n排程：週一至週五8:25啟動盤前選股與開盤監控、17:15更新盤後候選。\n已延伸監看9:00～13:30完成5分K；符合強勢才通知，附5分K與日線壓力／交界／支撐。行情和排程可能延遲，不保證每天有強勢訊號。")
        elif mode == "latest":
            cached = base.load(REPORT, {})
            cached_date = datetime.fromisoformat(cached["asof"]).date() if cached.get("asof") else None
            age = (base.now_tw().date() - cached_date).days if cached_date else None
            if age is not None and 0 <= age <= 1 and cached.get("coverage", 0) >= .9 and cached.get("stocks"):
                payload = daily_payload(cached, "daily")
                payload["content"] = "🔁 已驗證名單補發｜沿用原資料日與股票，加入盤前策略。\n" + payload["content"]
                send(payload)
                LOG.info("Resent verified RSI cards: date=%s candidates=%s", cached["asof"], len(cached["stocks"]))
            else:
                scan(latest=True)
        elif mode == "scan":
            scan()
        else:
            monitor()
    except Exception as error:
        LOG.error("RSI task failed: %s", type(error).__name__)
        if isinstance(error, RuntimeError):
            LOG.error("%s", error)
        if mode != "preview":
            try:
                send("⚠️ RSI強勢觀察執行失敗，本次未確認完成。請查看GitHub執行紀錄；請勿把沒有通知視為沒有訊號。")
            except Exception:
                LOG.error("Error notification could not be confirmed")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
