"""Daily Taiwan liquid-stock refresh + confirmed 5m season-MA breakout alerts."""
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

LOG = logging.getLogger("rising5")
STATE_FILE = "rising5.json"
EVENT_FILE = "rising5_events.json"


def cdp_levels(high, low, close, source_date):
    if not all(math.isfinite(v) and v > 0 for v in (high, low, close)) or not low <= close <= high:
        raise ValueError("前一交易日高低收不完整，不能計算撐壓")
    pivot = (high + low + 2 * close) / 4
    return dict(resistance=2 * pivot - low, pivot=pivot,
                support=2 * pivot - high, levels_date=source_date)


def level_text(stock):
    if not all(k in stock for k in ("resistance", "pivot", "support", "levels_date")):
        return "撐壓待重新選股更新"
    return (f'🔴 壓力(NH){stock["resistance"]:.2f}｜🟡 交界(CDP){stock["pivot"]:.2f}｜'
            f'🟢 支撐(NL){stock["support"]:.2f}（基準{stock["levels_date"]}）')


def premarket_fields(stock):
    r, p, s = (stock[key] for key in ("resistance", "pivot", "support"))
    return [
        dict(name="🔴 盤前做多策略", value=(
            f"回測：完成5分K重新站上交界{p:.2f}，且站上當日VWAP、量比≥1.5，再觀察。\n"
            f"突破：收上壓力{r:.2f}後回測守住；跳空開高先等回測。\n"
            f"停損：跌破回測低點；突破單跌回{r:.2f}下方。跌破交界暫停做多。\n"
            f"停利：回測單接近{r:.2f}分批；突破單用完成5分K低點移動保護。"
        ), inline=False),
        dict(name="🟢 盤前做空策略｜轉弱才觀察", value=(
            f"反彈受阻：測試壓力{r:.2f}未站穩，再收破交界{p:.2f}且低於當日VWAP，才觀察。\n"
            f"跌破：收破支撐{s:.2f}後反彈無法站回再觀察；跳空低開不追空。\n"
            f"停損：站回測壓高點；跌破單重新站回{s:.2f}上方。\n"
            f"停利：交界、支撐分批回補；續跌用完成5分K高點移動保護。"
        ), inline=False),
        dict(name="📌 執行前確認", value=(
            "這是起漲候選的多空應變計畫，做空需另等轉弱，並確認券商可空資格與額度。"
            "完成5分K收盤才判斷，核對即時行情；預期空間不足停損距離2倍則略過。"
        ), inline=False),
    ]


def card_payload(title, stocks, kind, note=""):
    if not 1 <= len(stocks) <= 10:
        raise ValueError("每則卡片通知需包含1至10檔股票")
    embeds = []
    for rank, stock in enumerate(stocks, 1):
        if kind == "daily":
            description = (f'**收盤 {stock["official_close"]:g}**\n'
                           f'日量比 {stock["daily_relvol"]:.2f} 倍｜日RSI {stock["daily_rsi"]:.1f}')
        else:
            bar = datetime.fromisoformat(stock["bar_end"])
            description = (f'**5分K訊號收盤 {stock["close"]:g}**｜{bar.strftime("%m/%d %H:%M")}\n'
                           f'季線 {stock["ma60"]:.2f}｜量比 {stock["volume_ratio"]:.2f} 倍｜RSI {stock["rsi5"]:.1f}')
        fields = [dict(name=label, value=f'**{stock[key]:.2f}**', inline=True)
                  for label, key in [("🔴 壓力 NH", "resistance"), ("🟡 交界 CDP", "pivot"), ("🟢 支撐 NL", "support")]]
        if kind == "daily":
            fields.extend(premarket_fields(stock))
        embeds.append(dict(title=f'#{rank}｜{stock["code"]} {stock["name"]}',
                           color=0xE74C3C, description=description, fields=fields,
                           footer=dict(text=f'撐壓基準 {stock["levels_date"]}｜CDP計算參考，非下單指令')))
    payload = dict(content=title + ("\n" + note if note else ""), embeds=embeds,
                   allowed_mentions=dict(parse=[]))
    size = sum(len(e["title"]) + len(e["description"]) + len(e["footer"]["text"]) +
               sum(len(f["name"]) + len(f["value"]) for f in e["fields"]) for e in embeds)
    if len(payload["content"]) > 2000 or size > 6000:
        raise ValueError("卡片通知超出Discord字數限制")
    return payload


def send_cards(title, stocks, kind, note=""):
    if len(stocks) > 5:
        for offset in range(0, len(stocks), 5):
            send_cards(title + f"｜第{offset // 5 + 1}批", stocks[offset:offset + 5], kind, note)
        return
    payload = card_payload(title, stocks, kind, note)
    hook = os.getenv("DISCORD_WEBHOOK_URL", "")
    parsed = urllib.parse.urlparse(hook)
    if parsed.scheme != "https" or parsed.hostname not in {"discord.com", "discordapp.com"} or not parsed.path.startswith("/api/webhooks/"):
        raise RuntimeError("請設定DISCORD_WEBHOOK_URL")
    separator = "&" if parsed.query else "?"
    body = json.dumps(payload, ensure_ascii=False).encode()
    for attempt in range(3):
        try:
            request = urllib.request.Request(hook + separator + "wait=true", data=body,
                                             headers={"Content-Type": "application/json", "User-Agent": "rising5-cards"})
            with urllib.request.urlopen(request, timeout=20) as response:
                message = json.load(response)
            count = len(message.get("embeds", []))
            if count != len(stocks):
                raise RuntimeError("Discord回傳卡片數量不符")
            LOG.info("Discord cards accepted: %s stock cards", count)
            return
        except urllib.error.HTTPError as error:
            if error.code == 429 and attempt < 2:
                try:
                    delay = float(json.load(error).get("retry_after", 2))
                except (ValueError, TypeError):
                    delay = 2
                time.sleep(min(max(delay, 1), 30))
                continue
            raise RuntimeError(f"Discord卡片發送失敗：HTTP {error.code}") from None
        except (urllib.error.URLError, TimeoutError):
            raise RuntimeError("Discord連線失敗，卡片發送結果未確認") from None


def rsi(series, length=14):
    # Wilder RMA seeded with the first length price changes.
    changes = series.astype(float).diff().dropna()
    result = pd.Series(float("nan"), index=series.index)
    if len(changes) < length:
        return result
    gain = changes.clip(lower=0)
    loss = -changes.clip(upper=0)
    avg_g = float(gain.iloc[:length].mean())
    avg_l = float(loss.iloc[:length].mean())
    for i in range(length - 1, len(changes)):
        if i >= length:
            avg_g = (avg_g * (length - 1) + float(gain.iloc[i])) / length
            avg_l = (avg_l * (length - 1) + float(loss.iloc[i])) / length
        result.loc[changes.index[i]] = (100.0 if avg_g > 0 else 50.0) if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    return result


def batches(stocks, interval, period):
    for start in range(0, len(stocks), 20):
        batch = stocks[start:start + 20]
        try:
            data = base.download([s["ticker"] for s in batch], interval, period)
        except Exception:
            LOG.warning("Download failed for batch %s", start)
            for stock in batch:
                yield stock, pd.DataFrame()
            continue
        for stock in batch:
            yield stock, base.frame_for(data, stock["ticker"])
        time.sleep(1)


def scan(latest=False, notify=True):
    current = base.now_tw()
    if not latest and current.weekday() >= 5:
        LOG.info("Weekend: no new daily list")
        return None
    snapshots = [base.official_snapshot(market) for market in base.SOURCES]
    dates = {d for d, _ in snapshots}
    if len(dates) > 1:
        newest = max(dates)
        if newest > current.date():
            raise RuntimeError("官方行情日期超前；不更新名單")
        snapshots = [base.official_snapshot(market, newest) if day < newest else (day, rows)
                     for market, (day, rows) in zip(base.SOURCES, snapshots)]
        dates = {day for day, _ in snapshots}
    if len(dates) != 1:
        raise RuntimeError("上市上櫃資料日期不同；不更新名單")
    asof = next(iter(dates))
    if not latest and asof != current.date():
        LOG.info("No official data for today (holiday or not updated): %s", asof)
        return None
    if (current.date() - asof).days > 10:
        raise RuntimeError("官方資料過舊，不更新名單")
    universe = [s for _, rows in snapshots for s in rows]
    pool, candidates = [], []
    failures = 0
    for stock, df in batches(universe, "1d", "6mo"):
        df = df[[i.date() <= asof for i in df.index]]
        if len(df) < 61 or df.index[-1].date() != asof:
            failures += 1
            continue
        close = float(df.Close.iloc[-1])
        if abs(close - stock["official_close"]) > max(0.05, close * 0.001):
            failures += 1
            continue
        ma = float(df.Close.iloc[-60:].mean())
        prev_ma = float(df.Close.iloc[-61:-1].mean())
        avg_vol = float(df.Volume.iloc[-21:-1].mean())
        rel = float(df.Volume.iloc[-1]) / avg_vol if avg_vol > 0 else 0
        daily_rsi = float(rsi(df.Close).iloc[-1])
        if not all(math.isfinite(v) and v > 0 for v in (ma, prev_ma, avg_vol)):
            failures += 1
            continue
        try:
            levels = cdp_levels(float(df.High.iloc[-1]), float(df.Low.iloc[-1]), close, asof.isoformat())
        except ValueError:
            failures += 1
            continue
        item = dict(stock, ma60=ma, prior_ma60=prev_ma, daily_rsi=daily_rsi, daily_relvol=rel, **levels)
        pool.append(item)
        if ma < close <= ma * 1.1 and rel >= 1.5 and 50 <= daily_rsi < 70:
            candidates.append(item)
    coverage = len(pool) / max(len(universe), 1)
    if not universe or coverage < 0.9:
        raise RuntimeError(f"行情覆蓋率不足：{len(pool)}/{len(universe)}；不覆蓋既有名單")
    candidates.sort(key=lambda s: (-s["daily_relvol"], -s["value"], s["ticker"]))
    report = dict(asof=asof.isoformat(), refreshed_at=current.isoformat(), pool=pool,
                  top10=candidates[:10], total=len(universe), failures=failures)
    base.save(STATE_FILE, report)
    if notify:
        title = "📋 起漲候選自動更新｜資料日 " + asof.isoformat()
        note = (f"上市＋上櫃，前日量≥2000張、金額≥1億元、股價≥10元；監控{len(pool)}檔有效行情。\n"
                "日線預選：季線上方10%內、量比≥1.5、RSI50–70。盤中監控整個合格池，新股票符合也通知。\n延遲行情觀察；非保證上漲，不自動下單。")
        if report["top10"]:
            send_cards(title, report["top10"], "daily", note)
        else:
            base.send_discord(title + "\n目前沒有同時符合日線預選條件的股票，不硬湊10檔。\n" + note)
    LOG.info("Daily scan complete: %s, pool=%s, top10=%s, coverage=%.1f%%", asof, len(pool), len(report["top10"]), coverage * 100)
    return report


def signal(df, stock, current, asof):
    if df.empty:
        return None
    df = df.copy()
    idx = pd.DatetimeIndex(df.index)
    df.index = idx.tz_localize(base.TZ) if idx.tz is None else idx.tz_convert(base.TZ)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    # Keep previous sessions for RSI, average volume and previous close.
    df = df[df.index + pd.Timedelta(minutes=5) <= current]
    if len(df) < 22:
        return None
    bar = df.index[-1].to_pydatetime()
    end = bar + timedelta(minutes=5)
    if bar.date() != current.date() or not clock(9) <= bar.time() < clock(13, 30):
        return None
    if not 0 <= (current - end).total_seconds() <= 20 * 60:
        return None
    if asof >= current.date() or (current.date() - asof).days > 7:
        return None
    ma = stock["ma60"]
    previous_ma = ma if df.index[-2].date() == bar.date() else stock["prior_ma60"]
    close, previous = float(df.Close.iloc[-1]), float(df.Close.iloc[-2])
    avg = float(df.Volume.iloc[-21:-1].mean())
    ratio = float(df.Volume.iloc[-1]) / avg if avg > 0 else 0
    strength = float(rsi(df.Close).iloc[-1])
    if close > ma and previous <= previous_ma and ratio >= 1.5 and math.isfinite(strength) and strength < 70:
        return dict(stock, close=close, volume_ratio=ratio, rsi5=strength, bar_end=end.isoformat())
    return None


def sweep(report, events):
    current = base.now_tw()
    asof = datetime.fromisoformat(report["asof"]).date()
    found, valid = [], 0
    for stock, df in batches(report["pool"], "5m", "5d"):
        if not df.empty:
            valid += 1
        # Recheck recent candles so a slow download sweep cannot skip a cross.
        for end_index in range(max(22, len(df) - 5), len(df) + 1):
            hit = signal(df.iloc[:end_index], stock, base.now_tw(), asof)
            if hit:
                key = f'{hit["ticker"]}:{hit["bar_end"]}'
                if key not in events["sent"]:
                    found.append((key, hit))
    found.sort(key=lambda p: (-p[1]["volume_ratio"], -p[1]["value"], p[1]["ticker"]))
    # Deliver every new match in groups of ten; never drop matches due to rank.
    for start in range(0, len(found), 10):
        group = found[start:start + 10]
        send_cards("🔔 新起漲條件符合｜5分K收盤確認", [s for _, s in group], "signal",
                   "新突破前一交易日60日季線＋前20根5分K均量1.5倍＋RSI14<70。\n雲端自動換股監控；可能延遲，不是下單指令。")
        for key, s in group:
            events["sent"].append(key)
            events["matched"][s["ticker"]] = s
        base.save(EVENT_FILE, events)
    top = sorted(events["matched"].values(), key=lambda s: (-s["volume_ratio"], -s["value"], s["ticker"]))[:10]
    codes = [s["ticker"] for s in top]
    if codes != events.get("top_codes", []) and top:
        # Restore levels if older notification state predates the card format.
        levels_by_ticker = {s["ticker"]: s for s in report["pool"]}
        top = [dict(levels_by_ticker.get(s["ticker"], {}), **s) for s in top]
        send_cards("📌 今日5分K符合訊號名單更新｜最多10檔", top, "signal",
                   "這是今日曾符合的訊號排行，不表示此刻仍符合；TradingView固定名單不會同步換股。")
        events["top_codes"] = codes
        base.save(EVENT_FILE, events)
    LOG.info("5m sweep: valid=%s/%s, new signals=%s", valid, len(report["pool"]), len(found))
    if valid < len(report["pool"]) * 0.9 and not events.get("warned"):
        base.send_discord(f"⚠️ 起漲5分K行情不足：{valid}/{len(report['pool'])}檔；缺資料股票不發訊號。")
        events["warned"] = True
        base.save(EVENT_FILE, events)


def monitor():
    current = base.now_tw()
    if current.weekday() >= 5:
        return
    # Refresh from latest official close each morning; no fixed symbols.
    report = scan(latest=True, notify=False)
    asof = datetime.fromisoformat(report["asof"]).date()
    if asof >= current.date():
        LOG.info("No previous-session baseline available")
        return
    day = current.date().isoformat()
    events = base.load(EVENT_FILE, {})
    if events.get("date") != day:
        events = dict(date=day, sent=[], matched={}, top_codes=[])
    if not events.get("started"):
        base.send_discord(f"🟢 起漲5分K雲端監控啟動｜{day}\n前一交易日資料{asof}，流動性合格且歷史資料驗證通過{len(report['pool'])}檔；自動偵測新股票，符合才通知。")
        events["started"] = True
        base.save(EVENT_FILE, events)
    next_check = base.now_tw()
    while base.now_tw().time() < clock(13, 50):
        current = base.now_tw()
        if current.time() >= clock(9, 5) and current >= next_check:
            next_check = current + timedelta(minutes=5)
            sweep(report, events)
        time.sleep(60)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["test", "scan", "monitor"])
    args = parser.parse_args()
    if args.mode == "test":
        report = scan(latest=True)
        base.send_discord(f"✅ 自動換股＋5分K監控設定測試成功\n資料日{report['asof']}；有效監控池{len(report['pool'])}檔，日線候選{len(report['top10'])}檔。\n盤後自動更新，交易日盤中符合條件才推播。今天若休市，這是設定測試，沒有即時買進訊號。")
    elif args.mode == "scan":
        scan()
    else:
        monitor()


if __name__ == "__main__":
    main()
