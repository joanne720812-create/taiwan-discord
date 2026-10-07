"""Taiwan daily Fibonacci pullback/rebound candidates; observation cards only."""
import argparse
import hashlib
import json
import logging
import math
import os
import traceback
import urllib.error
import urllib.parse
import urllib.request

import rising5_discord as rising
import short_discord as base

LOG = logging.getLogger("fibonacci10")
EXPECTED_CHANNEL = "1556623631885271101"
STATE_FILE = "fibonacci10.json"


def fib_setup(df):
    """Ordered low -> high -> pullback; all anchors precede the signal day."""
    if len(df) < 61:
        return None
    w = df.iloc[-60:]
    if "Adj Close" in w:
        factors = w["Adj Close"] / w.Close
        if factors.min() <= 0 or factors.max() / factors.min() > 1.01:
            # Raw prices spanning material adjustment events are incomparable.
            return None
    peak = int(w.High.iloc[:-3].to_numpy().argmax())
    if peak < 5 or peak > 56 or peak < 29:
        return None
    low_pos = int(w.Low.iloc[:peak].to_numpy().argmin())
    low, high = float(w.Low.iloc[low_pos]), float(w.High.iloc[peak])
    span = high - low
    if low <= 0 or span / low < 0.10:
        return None
    f382, f50, f618 = (high - span * p for p in (0.382, 0.5, 0.618))
    after = w.iloc[peak + 1:]
    # Reject a new high after the selected anchor or a failed deep support.
    if float(after.High.max()) > high * 1.01 or float(after.Low.min()) < f618 * 0.99:
        return None
    recent = after.iloc[-10:]
    touched = (recent.Low <= f382 * 1.01) & (recent.High >= f618)
    if not bool(touched.any()):
        return None
    close, previous, opened = float(w.Close.iloc[-1]), float(w.Close.iloc[-2]), float(w.Open.iloc[-1])
    avg_volume = float(df.Volume.iloc[-21:-1].mean())
    relvol = float(w.Volume.iloc[-1]) / avg_volume if avg_volume > 0 else 0
    strength = float(rising.rsi(df.Close).iloc[-1])
    ma20, ma60 = float(df.Close.iloc[-20:].mean()), float(df.Close.iloc[-60:].mean())
    if not all(math.isfinite(v) for v in (close, relvol, strength, ma20, ma60)):
        return None
    if not (f50 < close <= high * 1.01 and close > previous and close > opened
            and relvol >= 1 and 45 <= strength < 70 and ma20 >= ma60):
        return None
    confirmed = close > float(w.High.iloc[-2]) and relvol >= 1.5
    score = round(30 + (30 if confirmed else 0) + min(relvol, 3) * 10
                  + (10 if close > ma20 else 0) + (10 if strength >= 50 else 0), 1)
    return dict(swing_low=low, swing_high=high,
                low_date=w.index[low_pos].date().isoformat(),
                high_date=w.index[peak].date().isoformat(),
                fib382=f382, fib50=f50, fib618=f618,
                target1272=low + span * 1.272, target1618=low + span * 1.618,
                daily_relvol=relvol, daily_rsi=strength, ma20=ma20, ma60=ma60,
                confirmed=confirmed, score=score)


def card(stock, rank):
    r, p, s = (stock[k] for k in ("fib382", "fib50", "fib618"))
    close, high = stock["official_close"], stock["swing_high"]
    fields = [dict(name=name, value=f"**{stock[key]:.2f}**", inline=True)
              for name, key in [("🔴 斐波上緣 38.2%", "fib382"),
                                ("🟡 斐波交界 50%", "fib50"),
                                ("🟢 斐波支撐 61.8%", "fib618")]]
    fields += [
        dict(name="📐 上升波段與延伸參考", inline=False,
             value=f"低點 {stock['swing_low']:.2f}（{stock['low_date']}）→高點 {high:.2f}（{stock['high_date']}）\n"
                   f"前高壓力 {high:.2f}｜1.272延伸 {stock['target1272']:.2f}｜1.618延伸 {stock['target1618']:.2f}"),
        dict(name="🔴 盤前做多策略", inline=False,
             value=f"回測 {p:.2f}～{r:.2f} 後，完成5分K站回 {r:.2f}、站上VWAP且量比≥1.5，才觀察進場。\n"
                   f"突破前高 {high:.2f} 後等回測守住，不追跳空。跌破回測低點或 {s:.2f} 暫停做多。\n"
                   f"前高先分批觀察；突破後再看延伸參考。預期空間小於停損距離2倍則略過。"),
        dict(name="🟢 盤前做空應變｜需另等轉弱", inline=False,
             value=f"前高 {high:.2f} 受阻，完成5分K跌回 {r:.2f} 且低於VWAP才觀察轉弱。\n"
                   f"若收破 {p:.2f}、反彈無法站回，可觀察 {s:.2f}；站回測壓高點停損。\n"
                   "不追跳空低開；先確認可空資格與額度。此名單為多方候選，並非已出現做空訊號。"),
        dict(name="📊 CDP另列｜不同於斐波", inline=False,
             value=f"🔴 壓力 {stock['resistance']:.2f}｜🟡 交界 {stock['pivot']:.2f}｜🟢 支撐 {stock['support']:.2f}"),
    ]
    return dict(title=f"#{rank}｜{stock['code']} {stock['name']}", color=0xE74C3C,
                description=f"收盤 **{close:g}**｜規則分數 {stock['score']:g}（非勝率）\n"
                            f"{'放量突破前日高點' if stock['confirmed'] else '回檔轉強觀察，尚未放量突破'}\n"
                            f"日量比 {stock['daily_relvol']:.2f}｜RSI {stock['daily_rsi']:.1f}",
                fields=fields, footer=dict(text=f"資料日 {stock['levels_date']}｜參考條件，非下單指令"))


def webhook():
    hook = os.getenv("DISCORD_WEBHOOK_URL", "")
    u = urllib.parse.urlparse(hook)
    if u.scheme != "https" or u.hostname not in {"discord.com", "discordapp.com"} or not u.path.startswith("/api/webhooks/"):
        raise RuntimeError("Missing valid Discord webhook")
    try:
        req = urllib.request.Request(hook, headers={"User-Agent": "Fibonacci10/1.0"})
        with urllib.request.urlopen(req, timeout=30) as response:
            info = json.load(response)
        if info.get("channel_id") != EXPECTED_CHANNEL:
            raise RuntimeError("Unexpected channel; no message sent")
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise RuntimeError("Cannot verify Discord destination") from None
    return hook


def post(hook, payload):
    embeds = payload.get("embeds", [])
    size = sum(len(e["title"]) + len(e["description"]) + len(e["footer"]["text"])
               + sum(len(f["name"]) + len(f["value"]) for f in e["fields"]) for e in embeds)
    if len(embeds) > 5 or size > 6000 or len(payload["content"]) > 2000:
        raise ValueError("Discord payload too large")
    payload["allowed_mentions"] = {"parse": []}
    try:
        sep = "&" if urllib.parse.urlparse(hook).query else "?"
        req = urllib.request.Request(hook + sep + "wait=true", data=json.dumps(payload, ensure_ascii=False).encode(),
                                     headers={"Content-Type": "application/json", "User-Agent": "Fibonacci10/1.0"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as response:
            accepted = json.load(response)
        if accepted.get("channel_id") != EXPECTED_CHANNEL or len(accepted.get("embeds", [])) != len(embeds):
            raise RuntimeError("Discord response mismatch")
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise RuntimeError("Discord delivery not confirmed; inspect run before retrying") from None
    LOG.info("Discord accepted %s cards in expected channel", len(embeds))


def scan(latest=False, notify=True):
    current = base.now_tw()
    if not latest and (current.weekday() >= 5 or current.hour < 16):
        LOG.info("Outside daily close scan window")
        return None
    snapshots = [base.official_snapshot(m) for m in base.SOURCES]
    newest = max(d for d, _ in snapshots)
    if newest > current.date():
        raise RuntimeError("Official data date is in the future")
    snapshots = [base.official_snapshot(m, newest) if day < newest else (day, rows)
                 for m, (day, rows) in zip(base.SOURCES, snapshots)]
    if len({day for day, _ in snapshots}) != 1:
        raise RuntimeError("TW/TWO dates differ; no list sent")
    asof = newest
    if not latest and asof != current.date():
        LOG.info("No official data for today; holiday or pending update")
        return None
    if (current.date() - asof).days > 7:
        raise RuntimeError("Official data too old")
    universe = [s for _, rows in snapshots for s in rows]
    valid, selected = 0, []
    for stock, df in rising.batches(universe, "1d", "6mo"):
        df = df[[i.date() <= asof for i in df.index]]
        if len(df) < 61 or df.index[-1].date() != asof:
            continue
        close = float(df.Close.iloc[-1])
        if abs(close - stock["official_close"]) > max(0.05, close * 0.001):
            continue
        if not all(math.isfinite(float(v)) and float(v) > 0 for v in df[["Open", "High", "Low", "Close"]].iloc[-60:].to_numpy().ravel()):
            continue
        try:
            levels = rising.cdp_levels(float(df.High.iloc[-1]), float(df.Low.iloc[-1]), close, asof.isoformat())
        except ValueError:
            LOG.warning("Invalid daily high/low/close for %s; excluded", stock["code"])
            continue
        valid += 1
        setup = fib_setup(df)
        if setup:
            selected.append(dict(stock, **setup, **levels))
    if not universe or valid / len(universe) < 0.9:
        raise RuntimeError(f"Insufficient history coverage {valid}/{len(universe)}; no list sent")
    selected.sort(key=lambda s: (-s["score"], -s["daily_relvol"], -s["value"], s["ticker"]))
    report = dict(asof=asof.isoformat(), valid=valid, total=len(universe), candidates=len(selected), stocks=selected[:10])
    previous = base.load(STATE_FILE, {})
    fingerprint = hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest()
    sent = previous.get("sent_batches", []) if previous.get("fingerprint") == fingerprint else []
    report.update(fingerprint=fingerprint, sent_batches=sent)
    base.save(STATE_FILE, report)
    LOG.info("Fib scan: date=%s valid=%s/%s candidates=%s top=%s", asof, valid, len(universe), len(selected), len(report["stocks"]))
    if not notify:
        return report
    hook = webhook()
    title = f"📐 斐波起漲股前10名｜資料日 {asof}｜合格 {len(report['stocks'])} 支"
    note = ("上市＋上櫃；成交≥2000張、金額≥1億元、股價≥10元。60根日K尋找先低後高、漲幅≥10%的波段，"
            "近10根回測38.2%～61.8%（容許1%價格誤差）後收回50%上方、收紅且高於昨收；20MA≥60MA、量比≥1、RSI45～70。\n"
            "依突破確認、量比及均線位置排序；最多10支，不足不硬湊。50%為常用中點。行情可能延遲，非保證起漲。")
    groups = [report["stocks"][i:i + 5] for i in range(0, len(report["stocks"]), 5)] or [[]]
    for batch, stocks in enumerate(groups):
        if batch in sent:
            LOG.info("Batch %s already accepted; skipping", batch + 1)
            continue
        payload = dict(content=title + f"｜第{batch + 1}批\n" + note,
                       embeds=[card(s, batch * 5 + i + 1) for i, s in enumerate(stocks)])
        if not stocks:
            payload["content"] += "\n今天沒有符合條件的斐波起漲候選。"
        post(hook, payload)
        sent.append(batch)
        base.save(STATE_FILE, report)
    return report


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["scan", "test", "dry-run"])
    args = parser.parse_args()
    try:
        scan(latest=args.mode != "scan", notify=args.mode != "dry-run")
    except Exception as error:
        LOG.error("Fibonacci scan failed (%s); see failed step. Secrets omitted.", type(error).__name__)
        for frame in traceback.extract_tb(error.__traceback__):
            LOG.error("Location: %s:%s in %s", os.path.basename(frame.filename), frame.lineno, frame.name)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
