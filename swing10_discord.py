"""台股波段買點前10名＋Discord推播＋篩選診斷紀錄。"""

import argparse
import http.client
import json
import logging
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from collections import Counter
from datetime import datetime, date, time as clock
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd


TZ = ZoneInfo("Asia/Taipei")
STATE = Path(os.getenv("STATE_DIR", "state"))

MIN_VALUE = 100_000_000
MIN_SHARES = 2_000_000
TOP_N = 10

SOURCES = {
    "TW": (
        "https://openapi.twse.com.tw/v1/"
        "exchangeReport/STOCK_DAY_ALL"
    ),
    "TWO": (
        "https://www.tpex.org.tw/openapi/v1/"
        "tpex_mainboard_daily_close_quotes"
    ),
}


def log(message):
    print(message, flush=True)


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
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0"},
            )

            with urllib.request.urlopen(
                request, timeout=30
            ) as response:
                return json.load(response)

        except (urllib.error.URLError, ValueError, TimeoutError, http.client.IncompleteRead):
            if attempt == 2:
                raise RuntimeError("官方行情下載失敗") from None

            time.sleep(2 ** attempt)


def send_discord(content):
    hook = os.getenv("DISCORD_WEBHOOK_URL", "")
    parsed = urllib.parse.urlparse(hook)

    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"discord.com", "discordapp.com"}
        or not parsed.path.startswith("/api/webhooks/")
    ):
        raise RuntimeError(
            "請在 GitHub Secrets 設定 DISCORD_WEBHOOK_URL"
        )

    if len(content) > 1900:
        for start in range(0, len(content), 1900):
            send_discord(content[start:start + 1900])
        return

    body = json.dumps(
        {
            "content": content,
            "allowed_mentions": {"parse": []},
        },
        ensure_ascii=False,
    ).encode("utf-8")

    endpoint = hook + ("&" if parsed.query else "?") + "wait=true"

    for attempt in range(3):
        try:
            request = urllib.request.Request(
                endpoint,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "tw-swing-bot",
                },
            )

            with urllib.request.urlopen(
                request, timeout=20
            ) as response:
                response.read()

            return

        except urllib.error.HTTPError as error:
            if error.code == 429 and attempt < 2:
                try:
                    delay = float(
                        json.load(error).get("retry_after", 2)
                    )
                    if not math.isfinite(delay):
                        delay = 2
                except (ValueError, TypeError, AttributeError):
                    delay = 2

                time.sleep(min(max(delay, 1), 30))
                continue

            raise RuntimeError(
                f"Discord 發送失敗（HTTP {error.code}）"
            ) from None

        except (urllib.error.URLError, TimeoutError):
            raise RuntimeError(
                "Discord 連線失敗，發送結果未確認"
            ) from None


def save(name, data):
    STATE.mkdir(parents=True, exist_ok=True)

    path = STATE / name
    temporary = path.with_suffix(".tmp")

    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def official_snapshot(market, expected=None):
    rows = get_json(SOURCES[market])

    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f"{market} 官方資料為空")

    dates = {
        parse_date(pick(row, "Date", "日期"))
        for row in rows
    }

    if len(dates) != 1:
        raise RuntimeError(f"{market} 官方資料日期不一致")

    # 上市 OpenAPI 日期落後時，嘗試官方指定日期盤後表。
    if market == "TW" and expected and next(iter(dates)) < expected:
        url = (
            "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
            f"?response=json&date={expected:%Y%m%d}"
            "&type=ALLBUT0999"
        )
        payload = get_json(url)

        if (
            isinstance(payload, dict)
            and payload.get("stat") == "OK"
            and parse_date(payload.get("date", "")) == expected
        ):
            for table in payload.get("tables", []):
                fields = table.get("fields", [])

                if all(
                    field in fields
                    for field in ("證券代號", "收盤價", "成交金額")
                ):
                    rows = [
                        {
                            **dict(zip(fields, values)),
                            "日期": expected.strftime("%Y%m%d"),
                        }
                        for values in table.get("data", [])
                    ]
                    if rows:
                        dates = {expected}
                    break

    stocks = []

    for row in rows:
        code = str(
            pick(row, "Code", "SecuritiesCompanyCode", "證券代號")
        ).strip()

        # 排除不符合此代號格式的證券。
        if not re.fullmatch(r"[1-9]\d{3}", code):
            continue

        close = number(
            pick(row, "ClosingPrice", "Close", "收盤價")
        )
        shares = number(
            pick(row, "TradeVolume", "TradingShares", "成交股數")
        )
        value = number(
            pick(row, "TradeValue", "TransactionAmount", "成交金額")
        )

        if None in (close, shares, value):
            continue

        if (
            close < 10
            or shares < MIN_SHARES
            or value < MIN_VALUE
        ):
            continue

        stocks.append(
            {
                "code": code,
                "name": str(
                    pick(row, "Name", "CompanyName", "證券名稱")
                ),
                "ticker": f"{code}.{market}",
                "official_close": close,
                "value": value,
            }
        )

    snapshot_date = next(iter(dates))

    log(
        f"{market}｜資料日期 {snapshot_date}｜"
        f"官方資料 {len(rows)}筆｜初篩通過 {len(stocks)}檔"
    )

    return snapshot_date, stocks


def download(tickers):
    import yfinance as yf

    return yf.download(
        tickers=tickers,
        period="6mo",
        interval="1d",
        auto_adjust=False,
        actions=False,
        progress=False,
        threads=4,
        group_by="ticker",
        timeout=20,
    )


def frame_for(data, ticker, asof):
    if data is None or data.empty:
        return pd.DataFrame()

    if isinstance(data.columns, pd.MultiIndex):
        if ticker not in data.columns.get_level_values(0):
            return pd.DataFrame()

        data = data[ticker]

    required = ["Open", "High", "Low", "Close", "Volume"]

    if not all(column in data.columns for column in required):
        return pd.DataFrame()

    frame = data.dropna(subset=required).sort_index().copy()
    return frame.loc[
        [timestamp.date() <= asof for timestamp in frame.index]
    ]


def daily_score(df, stock):
    close, high, low, volume = (
        df[column].astype(float)
        for column in ("Close", "High", "Low", "Volume")
    )

    price = float(close.iloc[-1])
    ma20 = float(close.tail(20).mean())
    ma60 = float(close.tail(60).mean())
    prior20 = float(close.iloc[-21:-1].mean())
    base_volume = float(volume.iloc[-21:-1].mean())

    if base_volume <= 0:
        return None, "歷史平均成交量無效"

    if not price > ma20 > ma60:
        return None, "未符合收盤價＞20日均線＞60日均線"

    if ma20 <= prior20:
        return None, "20日均線未上升"

    ratio = float(volume.iloc[-1] / base_volume)
    level = float(high.iloc[-21:-1].max())
    span = float(high.iloc[-1] - low.iloc[-1])

    if span <= 0:
        return None, "當日高低價差無效"

    close_position = (price - float(low.iloc[-1])) / span

    if close_position < 0.65:
        return None, "收盤未位於當日振幅上方65%以上"

    breakout = (
        close.iloc[-2] <= level
        and level < price <= level * 1.03
        and ratio >= 1.5
    )

    pullback = (
        low.iloc[-2] <= prior20 * 1.015
        and close.iloc[-2] >= prior20 * 0.99
        and price > high.iloc[-2]
        and ratio >= 1.2
        and price <= ma20 * 1.05
    )

    if not (breakout or pullback):
        return None, "未符合放量突破或回測轉強"

    stop = min(float(low.tail(3).min()), ma20) * 0.995
    risk = (price - stop) / price

    if not 0 < risk <= 0.08:
        return None, "停損距離不在0%至8%之間"

    score = (
        60
        + (15 if breakout else 10)
        + min(15, max(0, (ratio - 1) * 10))
        + (10 if risk <= 0.05 else 0)
    )

    result = {
        **stock,
        "score": round(score, 1),
        "close": price,
        "stop": stop,
        "trigger": level if breakout else float(high.iloc[-2]),
        "volume_ratio": round(ratio, 2),
        "risk_pct": round(risk * 100, 2),
        "pattern": "放量突破" if breakout else "回測轉強",
    }

    return result, None



def observation_score(df, stock):
    values = df[['Open', 'High', 'Low', 'Close', 'Volume']].tail(65)
    if not all(math.isfinite(float(x)) for x in values.to_numpy().flat):
        return None
    if (values[['Open', 'High', 'Low', 'Close']] <= 0).any().any() or (values['Volume'] < 0).any():
        return None
    if (values['High'] < values[['Open', 'Low', 'Close']].max(axis=1)).any() or (values['Low'] > values[['Open', 'High', 'Close']].min(axis=1)).any():
        return None
    close, high, low, volume = (df[k].astype(float) for k in ('Close', 'High', 'Low', 'Volume'))
    price = float(close.iloc[-1])
    ma20, ma60 = float(close.tail(20).mean()), float(close.tail(60).mean())
    prior20 = float(close.iloc[-21:-1].mean())
    base = float(volume.iloc[-21:-1].mean())
    if base <= 0:
        return None
    ratio = float(volume.iloc[-1] / base)
    level = float(high.iloc[-21:-1].max())
    span = float(high.iloc[-1] - low.iloc[-1])
    position = (price - float(low.iloc[-1])) / span if span > 0 else .5
    score = (20 * (price > ma20) + 20 * (ma20 > ma60) + 15 * (ma20 > prior20)
             + 15 * max(0, min(1, position)) + 15 * max(0, min(1, ratio / 1.5))
             + 15 * max(0, 1 - abs(price / level - 1) / .1))
    return {**stock, 'score': round(score, 1), 'close': price, 'trigger': level,
            'volume_ratio': round(ratio, 2), 'status': 'watch',
            'pattern': '等待突破或回測確認' if price > ma20 > ma60 and ma20 > prior20 else '趨勢或買點尚未確認'}


def select_top(results, observations):
    key = lambda item: (-item['score'], -item['value'], item['ticker'])
    top, seen = [], set()
    for item in sorted(results, key=key):
        if item['ticker'] not in seen:
            top.append({**item, 'status': 'confirmed'})
            seen.add(item['ticker'])
        if len(top) == TOP_N:
            return top
    for item in sorted(observations, key=key):
        if item['ticker'] not in seen:
            top.append(item)
            seen.add(item['ticker'])
        if len(top) == TOP_N:
            break
    return top

def scan(asof, dry_run=False):
    current = now_tw()

    if asof > current.date():
        raise RuntimeError("資料日期不能是未來日期")

    if asof == current.date() and current.time() < clock(15):
        raise RuntimeError("請於台灣時間15:00後執行")

    log(f"開始正式選股｜指定資料日期：{asof}")

    snapshots = {
        market: official_snapshot(market, asof)
        for market in SOURCES
    }

    if any(
        snapshot_date != asof
        for snapshot_date, _ in snapshots.values()
    ):
        raise RuntimeError(
            "兩市場資料日期未齊全或休市，未發布名單"
        )

    universe = [
        stock
        for _, stocks in snapshots.values()
        for stock in stocks
    ]

    if not universe:
        raise RuntimeError(
            "初步篩選後為0檔，請檢查官方資料與成交量單位；"
            "未發布買點名單"
        )

    log(f"上市＋上櫃初篩通過：{len(universe)}檔")

    results = []
    observations = []
    failures = 0
    excluded = Counter()

    for offset in range(0, len(universe), 20):
        batch = universe[offset:offset + 20]

        log(
            f"下載歷史資料："
            f"{offset + 1}～{offset + len(batch)}"
            f"／{len(universe)}檔"
        )

        try:
            data = download([stock["ticker"] for stock in batch])
        except Exception:
            failures += len(batch)
            excluded["歷史資料下載批次失敗"] += len(batch)
            log("本批歷史資料下載失敗")
            continue

        for stock in batch:
            df = frame_for(data, stock["ticker"], asof)

            if len(df) < 65:
                failures += 1
                excluded["歷史資料少於65根日K"] += 1
                continue

            if df.index[-1].date() != asof:
                failures += 1
                excluded["歷史資料最後日期不符"] += 1
                continue

            price = float(df.iloc[-1]["Close"])
            tolerance = max(0.05, stock["official_close"] * 0.001)

            if abs(price - stock["official_close"]) > tolerance:
                failures += 1
                excluded["歷史收盤價與官方收盤价不符"] += 1
                continue

            # 保留原本規則：
            # 近65根日K內還原比例有變化的股票暫時排除。
            if "Adj Close" in df:
                factor = (
                    df["Adj Close"].astype(float)
                    / df["Close"].astype(float)
                ).tail(65)

                if (
                    factor.isna().any()
                    or not all(math.isfinite(x) for x in factor)
                ):
                    failures += 1
                    excluded["還原價格比例資料無效"] += 1
                    continue

                if factor.max() - factor.min() > 0.0001:
                    excluded["近65日還原價格比例變化"] += 1
                    continue

            observation = observation_score(df, stock)
            if observation is None:
                failures += 1
                excluded['價格或成交量資料無效'] += 1
                continue
            observations.append(observation)
            item, reason = daily_score(df, stock)

            if item:
                results.append(item)
            else:
                excluded[reason] += 1

        time.sleep(1)

    coverage = (len(universe) - failures) / len(universe)

    log("")
    log("========== 篩選統計 ==========")
    log(f"初篩通過：{len(universe)}檔")
    log(f"歷史資料未通過：{failures}檔")
    log(f"歷史資料可用率：{coverage:.1%}")

    for reason, count in excluded.items():
        log(f"{reason}：{count}檔")

    log(f"符合全部買點條件：{len(results)}檔")
    log("各股票僅記錄第一個未通過的條件。")
    log("==============================")

    diagnostics = {
        "asof": str(asof),
        "created_at": now_tw().isoformat(),
        "universe_count": len(universe),
        "history_failures": failures,
        "coverage": coverage,
        "exclusions": dict(excluded),
        "qualified_count": len(results),
    }
    save("swing_diagnostics.json", diagnostics)

    if coverage < 0.9:
        raise RuntimeError(
            "歷史資料可用率低於90%，未發布名單"
        )

    results.sort(
        key=lambda item: (
            -item["score"],
            -item["value"],
            item["ticker"],
        )
    )
    top = select_top(results, observations)

    lines = [
        f"📈 波段觀察前10名｜{len(top)}檔\n"
        f"資料日期：{asof}｜收盤確認，非即時進場指令"
    ]

    confirmed_count = sum(stock['status'] == 'confirmed' for stock in top)
    lines.append(f'✅ 已符合買點 {confirmed_count}檔｜👀 等待買點 {len(top)-confirmed_count}檔')
    for rank, stock in enumerate(top, 1):
        if stock['status'] == 'confirmed':
            detail = (f"✅ 已符合買點｜{stock['pattern']}\n"
                      f"收盤 {stock['close']:.2f}｜觸發線 {stock['trigger']:.2f}\n"
                      f"停損參考 {stock['stop']:.2f}｜風險距離 {stock['risk_pct']:.2f}%\n"
                      f"量比 {stock['volume_ratio']:.2f}｜買點分數 {stock['score']}")
        else:
            detail = (f"👀 等待買點｜{stock['pattern']}\n"
                      f"收盤 {stock['close']:.2f}｜前20日高點 {stock['trigger']:.2f}\n"
                      f"量比 {stock['volume_ratio']:.2f}｜觀察分數 {stock['score']}\n"
                      '尚未符合買點，觀察價不是買進指令')
        lines.append(f"{rank}. **{stock['name']} {stock['code']}**\n{detail}")
    if len(top) < TOP_N:
        lines.append(f'通過資料檢查的股票不足10檔，本次僅列{len(top)}檔。')
    lines.append('買點分數與觀察分數用途不同，不跨類別比較。')

    adjustment_count = excluded["近65日還原價格比例變化"]

    lines.append(
        f"篩選統計：初篩 {len(universe)}檔"
        f"｜歷史資料未通過 {failures}檔"
        f"｜還原比例變化排除 {adjustment_count}檔"
        f"｜符合買點 {len(results)}檔"
    )

    lines.append(
        "上市＋上櫃；成交額至少1億、成交量至少2000張；"
        f"歷史可用率{coverage:.0%}。\n"
        "歷史可用率不代表買點通過率。分數不是獲利機率。"
        "價格僅參考、未依升降單位取整；"
        "隔日跳空需重新評估，不自動下單。"
    )

    report = {
        "asof": str(asof),
        "created_at": now_tw().isoformat(),
        "stocks": top,
        "coverage": coverage,
        "diagnostics": diagnostics,
    }
    save("swing_shortlist.json", report)

    message = "\n\n".join(lines)

    if dry_run:
        log(message)
    else:
        send_discord(message)
        log(f"Discord 已發送：{len(top)}檔候選")


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "mode",
        choices=["scan", "test"],
        default="scan",
        nargs="?",
    )
    parser.add_argument(
        "--asof",
        type=date.fromisoformat,
        default=now_tw().date(),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
    )

    args = parser.parse_args()

    try:
        if args.mode == "test":
            message = (
                "✅ 波段買點 Discord 連線測試成功"
                "（不代表行情已驗證）"
            )
            if args.dry_run:
                log(message)
            else:
                send_discord(message)
                log("Discord 連線測試訊息已發送")
        else:
            scan(args.asof, args.dry_run)

    except Exception as error:
        # 不列印連線網址，避免洩漏 webhook。
        log(f"執行失敗｜錯誤類型：{type(error).__name__}")
        if isinstance(error, RuntimeError):
            log(str(error))
        log("請核對資料日期、行情連線及設定。")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
