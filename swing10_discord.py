"""台股強勢觀察前10名 + 盤中壓力未突破做空條件提醒（延遲行情）。"""
import argparse
import json
import logging
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, date, timedelta, time as clock
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

TZ = ZoneInfo("Asia/Taipei")
STATE = Path(os.getenv("STATE_DIR", "state"))
MIN_VALUE = 100_000_000  # 當日成交金額至少一億元
MIN_SHARES = 2_000_000  # 至少兩千張
MIN_SCORE = 60
TOP_N = 10
MAX_BAR_AGE = 20  # 容許延遲行情；超過 20 分鐘不推訊號
LOG = logging.getLogger("strong10_bot")
SOURCES = {
    "TW": "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
    "TWO": "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes",
}


def now_tw():
    return datetime.now(TZ)


def number(value):
    try:
        n = float(str(value).replace(",", "").replace(" ", ""))
        return n if math.isfinite(n) else None
    except (ValueError, TypeError):
        return None


def parse_date(value):
    digits = re.sub(r"\D", "", str(value))
    if len(digits) == 7:
        digits = str(int(digits[:3]) + 1911) + digits[3:]
    return datetime.strptime(digits, "%Y%m%d").date()


def pick(row, *keys):
    for key in keys:
        if key in row:
            return row[key]
    raise ValueError("官方資料欄位改變，請更新程式")


def get_json(url):
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except (urllib.error.URLError, ValueError, TimeoutError):
            if attempt == 2:
                raise RuntimeError("官方行情下載失敗") from None
            time.sleep(2 ** attempt)


def send_discord(content):
    hook = os.getenv("DISCORD_WEBHOOK_URL", "")
    p = urllib.parse.urlparse(hook)
    if p.scheme != "https" or p.hostname not in {"discord.com", "discordapp.com"} or not p.path.startswith("/api/webhooks/"):
        raise RuntimeError("請在 GitHub Secrets 設定 DISCORD_WEBHOOK_URL")
    if len(content) > 1900:
        for start in range(0, len(content), 1900):
            send_discord(content[start:start + 1900])
        return
    body = json.dumps({"content": content, "allowed_mentions": {"parse": []}}, ensure_ascii=False).encode()
    for attempt in range(3):
        try:
            req = urllib.request.Request(hook + ("&" if p.query else "?") + "wait=true", data=body,
                                         headers={"Content-Type": "application/json", "User-Agent": "tw-short-bot"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                resp.read()
            return
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                try:
                    delay = float(json.load(e).get("retry_after", 2))
                except (ValueError, TypeError):
                    delay = 2
                time.sleep(min(max(delay, 1), 30))
                continue
            raise RuntimeError(f"Discord 發送失敗（HTTP {e.code}）") from None
        except (urllib.error.URLError, TimeoutError):
            # 避免回應遺失時自動重送造成連續通知；不輸出 webhook URL。
            raise RuntimeError("Discord 連線失敗，發送結果未確認") from None


def save(name, data):
    STATE.mkdir(parents=True, exist_ok=True)
    path = STATE / name
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load(name, default):
    path = STATE / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def official_snapshot(market, expected=None):
    rows = get_json(SOURCES[market])
    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f"{market} 官方資料為空")
    dates = {parse_date(pick(r, "Date", "日期")) for r in rows}
    if len(dates) != 1:
        raise RuntimeError(f"{market} 官方資料日期不一致")
    # OpenAPI 偶爾晚一日，上市改查官方指定日期盤後表，仍嚴格核對日期。
    if market == "TW" and expected and next(iter(dates)) < expected:
        url = ("https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?response=json"
               f"&date={expected:%Y%m%d}&type=ALLBUT0999")
        payload = get_json(url)
        if payload.get("stat") == "OK" and parse_date(payload.get("date", "")) == expected:
            for table in payload.get("tables", []):
                fields = table.get("fields", [])
                if "證券代號" in fields and "收盤價" in fields and "成交金額" in fields:
                    rows = [{**dict(zip(fields, row)), "日期": expected.strftime("%Y%m%d")}
                            for row in table.get("data", [])]
                    dates = {expected}
                    break
    out = []
    for r in rows:
        code = str(pick(r, "Code", "SecuritiesCompanyCode", "證券代號"))
        if not re.fullmatch(r"[1-9]\d{3}", code):
            continue  # 排除 ETF、權證及非四位數代號
        close = number(pick(r, "ClosingPrice", "Close", "收盤價"))
        shares = number(pick(r, "TradeVolume", "TradingShares", "成交股數"))
        value = number(pick(r, "TradeValue", "TransactionAmount", "成交金額"))
        if None in (close, shares, value) or close < 10 or shares < MIN_SHARES or value < MIN_VALUE:
            continue
        out.append({"code": code, "name": str(pick(r, "Name", "CompanyName", "證券名稱")),
                    "ticker": f"{code}.{market}", "official_close": close, "value": value})
    return next(iter(dates)), out


def download(tickers, interval, period):
    import yfinance as yf
    return yf.download(tickers=tickers, period=period, interval=interval, auto_adjust=False,
                       actions=False, progress=False, threads=4, group_by="ticker", timeout=20)


def frame_for(data, ticker):
    if data is None or data.empty:
        return pd.DataFrame()
    if isinstance(data.columns, pd.MultiIndex):
        if ticker not in data.columns.get_level_values(0):
            return pd.DataFrame()
        data = data[ticker]
    return data.dropna(subset=["Open", "High", "Low", "Close", "Volume"]).sort_index()


def daily_score(frame, stock, asof):
    df = frame.loc[[d.date() <= asof for d in frame.index]].copy()
    if len(df) < 65 or df.index[-1].date() != asof:
        return None
    c, h, l, v = (df[k].astype(float) for k in ('Close','High','Low','Volume'))
    price = float(c.iloc[-1])
    if abs(price-stock['official_close']) > max(.05,price*.001):
        return None
    ma20, ma60 = float(c.tail(20).mean()), float(c.tail(60).mean())
    prior20 = float(c.iloc[-21:-1].mean())
    base = float(v.iloc[-21:-1].mean())
    if base <= 0 or not price > ma20 > ma60 or ma20 <= prior20:
        return None
    ratio = float(v.iloc[-1]/base)
    level = float(h.iloc[-21:-1].max())
    span = float(h.iloc[-1]-l.iloc[-1])
    if span <= 0 or (price-float(l.iloc[-1]))/span < .65:
        return None
    # 突破須今天首次收過前20日高點，且距離突破線不超過3%。
    breakout = c.iloc[-2] <= level and level < price <= level*1.03 and ratio >= 1.5
    # 回測須昨日靠近20日線、今日收過昨日高點，且距20日線不超過5%。
    pullback = (l.iloc[-2] <= prior20*1.015 and c.iloc[-2] >= prior20*.99
                and price > h.iloc[-2] and ratio >= 1.2 and price <= ma20*1.05)
    if not (breakout or pullback):
        return None
    stop = min(float(l.tail(3).min()), ma20)*.995
    risk = (price-stop)/price
    if not 0 < risk <= .08:
        return None
    score = 60 + (15 if breakout else 10) + min(15, max(0,(ratio-1)*10)) + (10 if risk<=.05 else 0)
    return {**stock,'score':round(score,1),'close':price,'stop':stop,
            'trigger':level if breakout else float(h.iloc[-2]),
            'volume_ratio':round(ratio,2),'risk_pct':round(risk*100,2),
            'pattern':'放量突破' if breakout else '回測轉強'}


def scan(asof, dry_run=False):
    if asof == now_tw().date() and now_tw().time() < clock(15):
        raise RuntimeError('請於台灣時間15:00後執行')
    snapshots = {m:official_snapshot(m,asof) for m in SOURCES}
    if any(d != asof for d,_ in snapshots.values()):
        raise RuntimeError('兩市場資料日期未齊全或休市，未發布名單')
    universe = [r for _,rows in snapshots.values() for r in rows]
    results, failures = [], 0
    for offset in range(0,len(universe),20):
        batch = universe[offset:offset+20]
        try:
            data = download([r['ticker'] for r in batch],'1d','6mo')
        except Exception:
            failures += len(batch)
            continue
        for stock in batch:
            df = frame_for(data,stock['ticker'])
            if len(df)<65 or df.index[-1].date()!=asof or abs(float(df.iloc[-1]['Close'])-stock['official_close'])>max(.05,stock['official_close']*.001):
                failures += 1
                continue
            # 近期除權息造成未調整價格跳動時排除，避免誤判買點。
            if 'Adj Close' in df:
                factor = df['Adj Close']/df['Close']
                if factor.tail(65).max()-factor.tail(65).min() > .0001:
                    continue
            item = daily_score(df,stock,asof)
            if item:
                results.append(item)
        time.sleep(1)
    coverage = (len(universe)-failures)/max(1,len(universe))
    if not universe or coverage < .9:
        raise RuntimeError('歷史資料可用率低於90%，未發布名單')
    results.sort(key=lambda x:(-x['score'],-x['value'],x['ticker']))
    top = results[:10]
    lines = [f'📈 波段買點候選｜{len(top)}檔\n資料日期：{asof}｜收盤確認，非即時進場指令']
    for i,r in enumerate(top,1):
        lines.append(f"{i}. {r['name']} {r['code']}｜{r['pattern']}\n收盤 {r['close']:.2f}｜觸發線 {r['trigger']:.2f}\n停損參考 {r['stop']:.2f}｜風險距離 {r['risk_pct']:.2f}%\n量比 {r['volume_ratio']:.2f}｜規則分數 {r['score']}")
    if not top:
        lines.append('今天沒有符合條件的股票，不湊滿10檔。')
    lines.append(f'上市＋上櫃；成交額至少1億、成交量至少2000張；歷史可用率{coverage:.0%}。\n分數不是獲利機率。價格僅參考、未依升降單位取整；隔日跳空需重新評估，不自動下單。')
    report = {'asof':str(asof),'created_at':now_tw().isoformat(),'stocks':top,'coverage':coverage}
    save('swing_shortlist.json',report)
    message = '\n\n'.join(lines)
    if dry_run:
        print(message)
    else:
        send_discord(message)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode',choices=['scan','test'],default='scan',nargs='?')
    parser.add_argument('--asof',type=date.fromisoformat,default=now_tw().date())
    parser.add_argument('--dry-run',action='store_true')
    args = parser.parse_args()
    try:
        if args.mode=='test':
            send_discord('✅ 波段買點 Discord 連線測試成功（不代表行情已驗證）')
        else:
            scan(args.asof,args.dry_run)
    except Exception:
        logging.exception('執行失敗：請核對資料日期、行情連線及Secret；沒有發布新名單。')
        raise SystemExit(1)
