"""台股壓力未突破候選＋Discord延遲行情提醒。"""
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

# 選股門檻
MIN_VALUE = 100_000_000
MIN_SHARES = 2_000_000
MIN_SCORE = 60
TOP_N = 10

# K棒收盤距今超過35分鐘，不發訊號
MAX_BAR_AGE = 35
MAX_SIGNALS_PER_DAY = 5
SIGNAL_COOLDOWN_MINUTES = 15

LOG = logging.getLogger("short_bot")

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


def now_tw():
    return datetime.now(TZ)


def number(value):
    try:
        n = float(
            str(value).replace(",", "").replace(" ", "")
        )
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
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0"},
            )

            with urllib.request.urlopen(
                req, timeout=30
            ) as resp:
                return json.load(resp)

        except (
            urllib.error.URLError,
            ValueError,
            TimeoutError,
        ):
            if attempt == 2:
                raise RuntimeError(
                    "官方行情下載失敗"
                ) from None

            time.sleep(2 ** attempt)


def send_discord(content):
    hook = os.getenv("DISCORD_WEBHOOK_URL", "")
    parsed = urllib.parse.urlparse(hook)

    valid = (
        parsed.scheme == "https"
        and parsed.hostname
        in {"discord.com", "discordapp.com"}
        and parsed.path.startswith("/api/webhooks/")
    )

    if not valid:
        raise RuntimeError(
            "請在GitHub Secrets設定DISCORD_WEBHOOK_URL"
        )

    body = json.dumps(
        {
            "content": content[:1900],
            "allowed_mentions": {"parse": []},
        },
        ensure_ascii=False,
    ).encode()

    separator = "&" if parsed.query else "?"

    for attempt in range(3):
        try:
            req = urllib.request.Request(
                hook + separator + "wait=true",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "tw-short-bot",
                },
            )

            with urllib.request.urlopen(
                req, timeout=20
            ) as resp:
                resp.read()

            return

        except urllib.error.HTTPError as error:
            if error.code == 429 and attempt < 2:
                try:
                    delay = float(
                        json.load(error).get(
                            "retry_after", 2
                        )
                    )
                except (ValueError, TypeError):
                    delay = 2

                time.sleep(min(max(delay, 1), 30))
                continue

            raise RuntimeError(
                f"Discord發送失敗：HTTP {error.code}"
            ) from None

        except (
            urllib.error.URLError,
            TimeoutError,
        ):
            # 不輸出Webhook，也不自動重送未知結果。
            raise RuntimeError(
                "Discord連線失敗，發送結果未確認"
            ) from None


def save(name, data):
    STATE.mkdir(parents=True, exist_ok=True)

    path = STATE / name
    temporary = path.with_suffix(".tmp")

    temporary.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    temporary.replace(path)


def load(name, default):
    path = STATE / name

    if not path.exists():
        return default

    return json.loads(
        path.read_text(encoding="utf-8")
    )


def official_snapshot(market, expected=None):
    rows = get_json(SOURCES[market])

    if not isinstance(rows, list) or not rows:
        raise RuntimeError(
            f"{market}官方資料為空"
        )

    dates = {
        parse_date(pick(row, "Date", "日期"))
        for row in rows
    }

    if len(dates) != 1:
        raise RuntimeError(
            f"{market}官方資料日期不一致"
        )

    # 上市OpenAPI較慢時，改查指定日期的官方盤後表。
    if (
        market == "TW"
        and expected
        and next(iter(dates)) < expected
    ):
        url = (
            "https://www.twse.com.tw/rwd/zh/"
            "afterTrading/MI_INDEX?response=json"
            f"&date={expected:%Y%m%d}"
            "&type=ALLBUT0999"
        )

        payload = get_json(url)

        valid_date = (
            payload.get("stat") == "OK"
            and parse_date(
                payload.get("date", "")
            ) == expected
        )

        if valid_date:
            for table in payload.get("tables", []):
                fields = table.get("fields", [])

                if all(
                    field in fields
                    for field in (
                        "證券代號",
                        "收盤價",
                        "成交金額",
                    )
                ):
                    rows = [
                        {
                            **dict(zip(fields, row)),
                            "日期": expected.strftime(
                                "%Y%m%d"
                            ),
                        }
                        for row in table.get("data", [])
                    ]

                    dates = {expected}
                    break

    stocks = []

    for row in rows:
        code = str(
            pick(
                row,
                "Code",
                "SecuritiesCompanyCode",
                "證券代號",
            )
        )

        # 排除ETF、權證及非四位數代號。
        if not re.fullmatch(r"[1-9]\d{3}", code):
            continue

        close = number(
            pick(
                row,
                "ClosingPrice",
                "Close",
                "收盤價",
            )
        )

        shares = number(
            pick(
                row,
                "TradeVolume",
                "TradingShares",
                "成交股數",
            )
        )

        value = number(
            pick(
                row,
                "TradeValue",
                "TransactionAmount",
                "成交金額",
            )
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
                    pick(
                        row,
                        "Name",
                        "CompanyName",
                        "證券名稱",
                    )
                ),
                "ticker": f"{code}.{market}",
                "official_close": close,
                "value": value,
            }
        )

    return next(iter(dates)), stocks


def download(tickers, interval, period):
    import yfinance as yf

    return yf.download(
        tickers=tickers,
        period=period,
        interval=interval,
        auto_adjust=False,
        actions=False,
        progress=False,
        threads=4,
        group_by="ticker",
        timeout=20,
    )


def frame_for(data, ticker):
    if data is None or data.empty:
        return pd.DataFrame()

    if isinstance(data.columns, pd.MultiIndex):
        if ticker not in data.columns.get_level_values(0):
            return pd.DataFrame()

        data = data[ticker]

    return data.dropna(
        subset=[
            "Open",
            "High",
            "Low",
            "Close",
            "Volume",
        ]
    ).sort_index()


def daily_score(frame, stock, asof):
    if frame.empty:
        return None

    df = frame.copy()
    df = df[
        [timestamp.date() <= asof for timestamp in df.index]
    ]

    if (
        len(df) < 60
        or df.index[-1].date() != asof
    ):
        return None

    close_series = df["Close"].astype(float)
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    volume = df["Volume"].astype(float)
    opening = df["Open"].astype(float)

    close = float(close_series.iloc[-1])
    previous = float(close_series.iloc[-2])

    if close <= 0 or previous <= 0:
        return None

    # 核對Yahoo與官方收盤資料。
    if abs(close - stock["official_close"]) > max(
        0.05, close * 0.001
    ):
        return None

    # 壓力：前20交易日最高價，不含選股當日。
    resistance = float(
        high.iloc[-21:-1].max()
    )

    volume_base = float(
        volume.iloc[-21:-1].mean()
    )

    span = float(
        high.iloc[-1] - low.iloc[-1]
    )

    if (
        volume_base <= 0
        or resistance <= 0
        or span <= 0
    ):
        return None

    ratio = float(
        volume.iloc[-1] / volume_base
    )

    change = (close / previous - 1) * 100

    upper_wick = float(
        (
            high.iloc[-1]
            - max(opening.iloc[-1], close)
        ) / span
    )

    distance = (
        resistance / close - 1
    ) * 100

    # 接近壓力、留上影線、收回壓力下方。
    eligible = (
        high.iloc[-1] >= resistance * 0.98
        and close < resistance * 0.995
        and 0.5 <= distance <= 4
        and upper_wick >= 0.30
    )

    if not eligible or change <= -6:
        return None

    conditions = [
        (
            20,
            high.iloc[-1] >= resistance * 0.995,
            "觸及20日壓力附近",
        ),
        (
            20,
            close < resistance * 0.995,
            "收盤仍在壓力下方",
        ),
        (
            25,
            upper_wick >= 0.35,
            "上影線占區間至少35%",
        ),
        (
            15,
            ratio >= 1.3,
            "放量但未突破壓力",
        ),
        (
            10,
            (close - low.iloc[-1]) / span <= 0.4,
            "收在當日區間下緣40%",
        ),
        (
            10,
            close < opening.iloc[-1],
            "收盤低於開盤",
        ),
    ]

    score = sum(
        points
        for points, valid, _ in conditions
        if valid
    )

    if score < MIN_SCORE:
        return None

    return {
        **stock,
        "score": score,
        "close": close,
        "low": float(low.iloc[-1]),
        "high": float(high.iloc[-1]),
        "resistance": resistance,
        "change_pct": round(change, 2),
        "volume_ratio": round(ratio, 2),
        "reasons": [
            reason
            for _, valid, reason in conditions
            if valid
        ],
    }


def scan(asof):
    snapshots = {
        market: official_snapshot(market, asof)
        for market in SOURCES
    }

    if any(
        data_date != asof
        for data_date, _ in snapshots.values()
    ):
        dates = ", ".join(
            f"{market}: {data_date}"
            for market, (data_date, _) in snapshots.items()
        )

        send_discord(
            "⚠️ 偏空選股暫停\n"
            f"要求日期：{asof}\n"
            f"官方資料日期：{dates}\n"
            "可能休市或尚未更新，未產生新名單。"
        )
        return

    universe = [
        stock
        for _, rows in snapshots.values()
        for stock in rows
    ]

    results = []
    failures = 0

    for offset in range(0, len(universe), 20):
        batch = universe[offset:offset + 20]

        try:
            data = download(
                [stock["ticker"] for stock in batch],
                "1d",
                "6mo",
            )
        except Exception:
            failures += len(batch)
            continue

        for stock in batch:
            df = frame_for(data, stock["ticker"])

            if (
                len(df) < 60
                or df.index[-1].date() != asof
                or abs(float(df.iloc[-1]["Close"]) - stock["official_close"])
                > max(0.05, stock["official_close"] * 0.001)
            ):
                failures += 1
                continue

            item = daily_score(df, stock, asof)

            if item:
                results.append(item)

        time.sleep(1)

    coverage = (
        len(universe) - failures
    ) / max(len(universe), 1)

    if not universe or coverage < 0.9:
        send_discord(
            "⚠️ 選股暫停\n"
            f"流動性合格：{len(universe)}檔\n"
            f"歷史資料可用率：{coverage:.0%}\n"
            "資料不足，未發布新名單。"
        )
        raise RuntimeError("歷史行情覆蓋率不足")

    results.sort(
        key=lambda stock: (
            -stock["score"],
            -stock["value"],
            stock["ticker"],
        )
    )

    top = results[:TOP_N]

    report = {
        "asof": asof.isoformat(),
        "created_at": now_tw().isoformat(),
        "stocks": top,
        "eligible_universe": len(universe),
        "history_coverage": round(coverage, 4),
    }

    lines = [
        "📉 下一交易日壓力未突破候選｜最多10檔\n"
        f"資料日：{asof}\n"
        "分數是規則評分，不是下跌機率。"
    ]

    for rank, stock in enumerate(top, 1):
        reasons = "、".join(stock["reasons"][:3])

        lines.append(
            f"{rank}. {stock['name']} {stock['code']}"
            f"｜{stock['score']}分\n"
            f"收盤：{stock['close']:.2f}"
            f"｜20日壓力：{stock['resistance']:.2f}\n"
            f"{reasons}"
        )

    if not top:
        lines.append(
            "沒有符合條件的股票，不湊滿十檔。"
        )

    lines.append(
        "範圍：上市＋上櫃\n"
        "成交額≥1億、成交量≥2000張\n"
        f"歷史資料可用率：{coverage:.0%}\n"
        "僅技術面觀察，沒有隔日沖分點資料。\n"
        "尚未核對券商可空額度與交易限制。"
    )

    send_discord("\n\n".join(lines))
    save("shortlist.json", report)


def completed_bars(frame, current):
    if frame.empty:
        return frame

    df = frame.copy()
    index = pd.DatetimeIndex(df.index)

    if index.tz is None:
        index = index.tz_localize(TZ)
    else:
        index = index.tz_convert(TZ)

    df.index = index

    df = df[
        ~df.index.duplicated(keep="last")
    ].sort_index()

    keep = [
        timestamp.date() == current.date()
        and clock(9) <= timestamp.time() < clock(13, 30)
        and (
            timestamp.to_pydatetime()
            + timedelta(minutes=5)
            <= current
        )
        for timestamp in df.index
    ]

    return df[keep]


def tick_size(price):
    if price < 10:
        return 0.01
    if price < 50:
        return 0.05
    if price < 100:
        return 0.1
    if price < 500:
        return 0.5
    if price < 1000:
        return 1.0
    return 5.0


def short_signal(frame, stock, current):
    df = completed_bars(frame, current)

    if (
        len(df) < 6
        or df.index[0].time() != clock(9)
    ):
        return None

    # 缺少K棒時不判斷。
    if any(
        (
            df.index[index] - df.index[index - 1]
        ).total_seconds() != 300
        for index in range(1, len(df))
    ):
        return None

    bar_end = (
        df.index[-1].to_pydatetime()
        + timedelta(minutes=5)
    )

    age = (
        current - bar_end
    ).total_seconds() / 60

    if (
        age < 0
        or age > MAX_BAR_AGE
        or (df["Volume"] <= 0).any()
    ):
        return None

    # 大幅跳空低開不追空。
    if df.iloc[0]["Open"] <= stock["close"] * 0.97:
        return None

    resistance = stock["resistance"]

    # 今天曾有效突破壓力，就取消此類做空訊號。
    if (
        df["Close"] >= resistance * 1.002
    ).any():
        return None

    # 最近3根：測壓失敗，後2根量縮收跌。
    test = df.iloc[-3]
    first = df.iloc[-2]
    last = df.iloc[-1]

    volume_base = float(
        df.iloc[-6:-3]["Volume"].mean()
    )

    span = float(
        test["High"] - test["Low"]
    )

    if span <= 0 or volume_base <= 0:
        return None

    wick = (
        test["High"]
        - max(test["Open"], test["Close"])
    ) / span

    failed = (
        test["High"] >= resistance * 0.995
        and test["Close"] < resistance
        and wick >= 0.35
        and test["Volume"] >= volume_base * 1.3
    )

    exhaustion = (
        first["Volume"] <= test["Volume"] * 0.8
        and last["Volume"] <= test["Volume"] * 0.8
        and first["Close"] < first["Open"]
        and last["Close"] < last["Open"]
        and last["Close"] < first["Close"] < test["Close"]
        and last["Close"] < test["Low"]
        and last["Close"] < first["Low"]
    )

    # 使用5分K典型價格估算當日VWAP。
    typical = (
        df["High"] + df["Low"] + df["Close"]
    ) / 3

    vwap = float(
        (typical * df["Volume"]).sum()
        / df["Volume"].sum()
    )

    if (
        not failed
        or not exhaustion
        or last["Close"] >= vwap
    ):
        return None

    price = float(last["Close"])

    # 測壓高點加一個升降單位，作為失效價參考。
    stop = float(test["High"]) + tick_size(
        float(test["High"])
    )

    if (
        price <= 0
        or not 0 < (stop / price - 1) <= 0.015
    ):
        return None

    return {
        "price": price,
        "stop": stop,
        "vwap": vwap,
        "resistance": resistance,
        "bar_at": df.index[-1].isoformat(),
        "age_minutes": round(age, 1),
    }


def notification_allowed(signal, stock, current, events):
    """同一根K棒不重送；每檔每天最多五次，訊號K至少相隔15分鐘。"""
    prefix = f"{current.date()}:{stock['ticker']}"
    key = f"{prefix}:{signal['bar_at']}"
    prior = [
        value for event_key, value in events.items()
        if event_key == prefix or event_key.startswith(prefix + ":")
    ]
    if key in events or len(prior) >= MAX_SIGNALS_PER_DAY:
        return False
    bar_at = datetime.fromisoformat(signal["bar_at"])
    for value in prior:
        # 舊版每日一次的紀錄也納入計算；缺少時間時保守停止重送。
        if not value.get("bar_at"):
            return False
        previous = datetime.fromisoformat(value["bar_at"])
        if (bar_at - previous).total_seconds() < SIGNAL_COOLDOWN_MINUTES * 60:
            return False
    return True


def monitor(once=False):
    current = now_tw()

    if (
        current.weekday() >= 5
        or not clock(8, 45)
        <= current.time()
        <= clock(13, 30)
    ):
        LOG.info("目前不在監控時段")
        return

    shortlist = load("shortlist.json", {})

    if not shortlist or not shortlist.get("stocks"):
        send_discord(
            "⚠️ 沒有偏空候選名單。\n"
            "請先執行scan，確認有合格股票。"
        )
        return

    asof = date.fromisoformat(shortlist["asof"])

    if not 1 <= (current.date() - asof).days <= 7:
        send_discord(
            "⚠️ 候選日期不適用，監控停止。\n"
            "請重新選股。"
        )
        return

    snapshots = {
        market: official_snapshot(market, asof)[0]
        for market in SOURCES
    }

    if any(
        data_date != asof
        for data_date in snapshots.values()
    ):
        send_discord(
            "⚠️ 名單不是官方最新收盤資料。\n"
            "監控停止，請重新選股。"
        )
        return

    stocks = shortlist["stocks"]
    events = load("events.json", {})

    earliest = (
        current.date() - timedelta(days=7)
    ).isoformat()

    events = {
        key: value
        for key, value in events.items()
        if key[:10] >= earliest
    }

    names = "、".join(
        stock["name"] for stock in stocks
    )

    send_discord(
        f"🔎 偏空監控啟動｜{current:%Y-%m-%d %H:%M}\n"
        f"候選資料日：{asof}\n"
        f"{names}\n"
        "每60秒檢查5分鐘K，行情可能延遲。\n"
        "訊號是條件提醒，不是立即下單指令。"
    )

    seen_today = False
    failures = 0

    while True:
        current = now_tw()

        if current.time() > clock(13, 30):
            break

        if current.time() >= clock(9):
            try:
                data = download(
                    [
                        stock["ticker"]
                        for stock in stocks
                    ],
                    "5m",
                    "5d",
                )

                for stock in stocks:
                    df = frame_for(
                        data, stock["ticker"]
                    )

                    bars = completed_bars(df, current)

                    if not bars.empty:
                        seen_today = True

                    signal = short_signal(
                        df, stock, current
                    )

                    key = (
                        f"{current.date()}:{stock['ticker']}:{signal['bar_at']}"
                        if signal else ""
                    )

                    if signal and notification_allowed(signal, stock, current, events):
                        bar_at = datetime.fromisoformat(
                            signal["bar_at"]
                        )

                        bar_end = (
                            bar_at + timedelta(minutes=5)
                        )

                        event_prefix = f"{current.date()}:{stock['ticker']}"
                        notification_number = 1 + sum(
                            event_key == event_prefix
                            or event_key.startswith(event_prefix + ":")
                            for event_key in events
                        )

                        send_discord(
                            "📉 做空條件成立"
                            "（延遲行情提醒）\n"
                            f"{stock['name']} "
                            f"{stock['code']}\n"
                            "本日通知："
                            f"{notification_number}/{MAX_SIGNALS_PER_DAY}\n"
                            f"規則分數：{stock['score']}\n"
                            f"5分K起點：{bar_at:%H:%M}\n"
                            f"K棒收盤：{bar_end:%H:%M}\n"
                            f"觀察價：{signal['price']:.2f}\n"
                            "型態失效參考："
                            f"{signal['stop']:.2f}\n"
                            "壓力："
                            f"{signal['resistance']:.2f}\n"
                            f"VWAP：約{signal['vwap']:.2f}\n"
                            "條件：放量測壓留上影、"
                            "收盤未突破壓力；"
                            "後2根量縮收跌，"
                            "跌破測壓K低點且低於VWAP。\n"
                            f"推播時間：{current:%H:%M:%S}\n"
                            "K棒收盤距今："
                            f"{signal['age_minutes']:.1f}分\n"
                            "請核對券商現價、"
                            "可空額度及停損，再決定是否交易。"
                        )

                        events[key] = signal
                        save("events.json", events)

                failures = 0

            except Exception:
                failures += 1

                LOG.warning(
                    "本輪行情或推播失敗；"
                    "未輸出密鑰及敏感錯誤內容"
                )

                if failures >= 5:
                    send_discord(
                        "⚠️ 連續5輪監控失敗。\n"
                        "已停止，請查看Actions狀態。"
                    )

                    raise RuntimeError(
                        "監控連續失敗"
                    ) from None

            if (
                current.time() >= clock(9, 50)
                and not seen_today
            ):
                send_discord(
                    "⚠️ 09:50仍無今日盤中K棒。\n"
                    "可能休市或行情異常，已停止監控。\n"
                    "不使用昨日行情發訊號。"
                )
                break

        if once:
            break

        time.sleep(60)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(message)s",
    )

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "mode",
        choices=["scan", "monitor", "test"],
    )

    parser.add_argument(
        "--asof",
        help="手動指定資料日，例如2026-09-30",
    )

    parser.add_argument(
        "--once",
        action="store_true",
    )

    args = parser.parse_args()

    if args.mode == "test":
        send_discord(
            "✅ 台股偏空Discord測試成功。\n"
            "這是連線測試，"
            "尚未產生名單或做空訊號。"
        )

    elif args.mode == "scan":
        asof = (
            date.fromisoformat(args.asof)
            if args.asof
            else now_tw().date()
        )

        scan(asof)

    else:
        monitor(args.once)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        LOG.error(
            "執行失敗：請檢查Secret、"
            "資料日期及行情連線；"
            "未輸出敏感錯誤內容。"
        )
        raise SystemExit(1)
