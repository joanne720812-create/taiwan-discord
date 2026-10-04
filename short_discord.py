"""台股壓力未突破候選＋Discord延遲行情提醒。"""
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

# K棒收盤距今超過20分鐘，不發訊號
MAX_BAR_AGE = 20
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
            http.client.IncompleteRead,
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
            **({"content": content[:1900]} if isinstance(content, str) else content),
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
                message = json.load(resp)
            if isinstance(content, dict):
                expected = len(content.get("embeds", []))
                if len(message.get("embeds", [])) != expected:
                    raise RuntimeError("Discord卡片數量未確認")
                LOG.info("Discord cards accepted: %s", expected)
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
        **cdp_levels(high.iloc[-1], low.iloc[-1], close, asof),
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

    if top:
        send_embeds(f"📉 下一交易日偏空候選｜資料日 {asof}",
            [stock_card(item, f"#{rank} 偏空候選", f"前收 **{item['close']:.2f}**｜規則分數 {item['score']}\n"
                + "、".join(item['reasons'][:3]), "short",
                [{"name": "策略壓力（前20交易日高點）", "value": f"{item['resistance']:.2f}", "inline": True}])
             for rank, item in enumerate(top, 1)])
    else:
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
            + timedelta(minutes=1)
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
        ).total_seconds() != 60
        for index in range(1, len(df))
    ):
        return None

    bar_end = (
        df.index[-1].to_pydatetime()
        + timedelta(minutes=1)
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

    # 使用1分K典型價格估算當日VWAP。
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



def long_candidates(asof):
    """沿用盤後做多TOP10排行，核對兩市場同一資料日。"""
    import stock_discord as ranking
    listed = ranking.listed_for_date(asof.isoformat())
    otc = [item for row in get_json(ranking.TPEX)
           if (item := ranking.normalized(row, "上櫃")) and item["date"] == asof.isoformat()]
    current = [ranking.evaluate(item) for item in listed + otc
               if item["date"] == asof.isoformat() and re.fullmatch(r"[1-9]\d{3}", item["code"])]
    if {item["market"] for item in current} != {"上市", "上櫃"}:
        raise RuntimeError("做多名單兩市場日期不完整")
    top = sorted((item for item in current if item["score"] >= 60 and item["pct"] > 0),
                 key=lambda item: (item["score"], item["pct"], item["lots"]), reverse=True)[:10]
    return [{**item, "ticker": item["code"] + (".TW" if item["market"] == "上市" else ".TWO"),
             "direction": "long"} for item in top]


def long_signal(frame, stock, current):
    """完成前五根1分K後，放量突破開盤區間並站上VWAP。"""
    df = completed_bars(frame, current)
    if len(df) < 6 or df.index[0].time() != clock(9):
        return None
    if any((df.index[i] - df.index[i-1]).total_seconds() != 60 for i in range(1, len(df))):
        return None
    age = (current - (df.index[-1].to_pydatetime() + timedelta(minutes=1))).total_seconds() / 60
    if not 0 <= age <= MAX_BAR_AGE or (df["Volume"] <= 0).any():
        return None
    if df.iloc[0]["Open"] >= stock["close"] * 1.03:
        return None
    opening_high = float(df.iloc[:5]["High"].max())
    last, previous = df.iloc[-1], df.iloc[-2]
    vwap = float((((df["High"] + df["Low"] + df["Close"]) / 3) * df["Volume"]).sum() / df["Volume"].sum())
    volume_base = float(df.iloc[-6:-1]["Volume"].mean())
    price = float(last["Close"])
    stop = float(df.iloc[-3:]["Low"].min())
    if not (previous["Close"] <= opening_high < price and price > last["Open"]
            and price > vwap and price > stock["close"]
            and last["Volume"] >= volume_base * 1.3
            and 0 < 1 - stop / price <= 0.015):
        return None
    return {"price": price, "stop": stop, "vwap": vwap, "resistance": opening_high,
            "bar_at": df.index[-1].isoformat(), "age_minutes": round(age, 1)}



def cdp_levels(high, low, close, asof):
    high, low, close = float(high), float(low), float(close)
    if not all(math.isfinite(v) and v > 0 for v in (high, low, close)) or not low <= close <= high:
        raise ValueError("CDP基準行情無效")
    pivot = (high + low + 2 * close) / 4
    return {"cdp_resistance": 2 * pivot - low, "pivot": pivot,
            "support": 2 * pivot - high, "levels_date": str(asof)}


def attach_levels(stocks, asof):
    # 以候選資料日的完整日K計算下一交易日CDP；不使用當日未完成日K。
    if not stocks:
        return []
    data = download(sorted({item["ticker"] for item in stocks}), "1d", "6mo")
    result = []
    for stock in stocks:
        df = frame_for(data, stock["ticker"])
        df = df[[stamp.date() <= asof for stamp in df.index]]
        if df.empty or df.index[-1].date() != asof:
            raise RuntimeError("缺少前一交易日CDP基準，停止監控")
        row = df.iloc[-1]
        close = float(row["Close"])
        if abs(close - stock["close"]) > max(0.05, close * 0.001):
            raise RuntimeError("CDP基準收盤核對失敗")
        result.append({**stock, **cdp_levels(row["High"], row["Low"], close, asof)})
    return result


def volume_metrics(bars):
    """本根已收盤1分K量；均量只取同日緊鄰前20根，不含本根。"""
    if bars.empty:
        return {}
    current_volume = number(bars.iloc[-1]["Volume"])
    result = {"volume_shares": current_volume, "volume_base_shares": None,
              "volume_ratio_1m": None, "volume_base_count": 0}
    previous = bars.iloc[-21:-1]
    if len(previous) != 20:
        return result
    stamps = list(previous.index) + [bars.index[-1]]
    if any((stamps[i] - stamps[i-1]).total_seconds() != 60 for i in range(1, len(stamps))):
        return result
    volumes = [number(value) for value in previous["Volume"]]
    if any(value is None or value < 0 for value in volumes):
        return result
    base = sum(volumes) / 20
    result.update(volume_base_shares=base, volume_base_count=20)
    if base > 0 and current_volume is not None and current_volume >= 0:
        result["volume_ratio_1m"] = current_volume / base
    return result


def volume_fields(signal):
    volume = signal.get("volume_shares")
    base = signal.get("volume_base_shares")
    ratio = signal.get("volume_ratio_1m")
    return [
        {"name": "本根1分K成交量", "value": f"**{volume / 1000:,.2f} 張**" if volume is not None else "資料不足", "inline": True},
        {"name": "前20根1分K均量", "value": f"**{base / 1000:,.2f} 張**" if base is not None else "同日連續K棒不足20根", "inline": True},
        {"name": "放量倍數", "value": (f"**{ratio:.2f} 倍**" + (" 🟣 放量≥1.5倍" if ratio >= 1.5 else "")) if ratio is not None else "無法計算（資料不足或均量為0）", "inline": True},
    ]


def crossing_signal(frame, stock, current):
    df = completed_bars(frame, current)
    if len(df) < 2:
        return None
    if (df.index[-1] - df.index[-2]).total_seconds() != 60:
        return None
    bar_end = df.index[-1].to_pydatetime() + timedelta(minutes=1)
    age = (current - bar_end).total_seconds() / 60
    if not 0 <= age <= MAX_BAR_AGE or (df.iloc[-2:]["Volume"] <= 0).any():
        return None
    previous, price = map(float, df.iloc[-2:]["Close"])
    crossed = []
    for name, field in (("壓力 NH", "cdp_resistance"), ("交界 CDP", "pivot"), ("支撐 NL", "support")):
        level = stock[field]
        if previous <= level < price:
            crossed.append({"name": name, "level": level, "direction": "up"})
        elif previous >= level > price:
            crossed.append({"name": name, "level": level, "direction": "down"})
    if not crossed:
        return None
    return {"price": price, "previous": previous, "crossed": crossed,
            "bar_at": df.index[-1].isoformat(), "age_minutes": round(age, 1),
            **volume_metrics(df)}


def level_fields(stock):
    return [{"name": name, "value": f"**{stock[field]:.2f}**", "inline": True}
            for name, field in (("壓力 NH", "cdp_resistance"), ("交界 CDP", "pivot"), ("支撐 NL", "support"))]


def stock_card(stock, title, description, direction, extra=None, demo=False):
    return {"title": f"{title}｜{stock['code']} {stock['name']}",
            "description": description, "color": 0xFF253A if direction in ("up", "long") else 0x00B875,
            "fields": level_fields(stock) + (extra or []),
            "footer": {"text": ("示範數值，非真實行情或交易訊號" if demo else
                f"CDP基準 {stock['levels_date']}｜延遲行情，條件提醒")}}


def send_embeds(title, embeds):
    if not 1 <= len(embeds) <= 10:
        raise ValueError("卡片數量需介於1到10")
    characters = sum(len(e.get("title", "")) + len(e.get("description", ""))
        + len(e.get("footer", {}).get("text", ""))
        + sum(len(f["name"]) + len(f["value"]) for f in e.get("fields", [])) for e in embeds)
    if characters > 6000 or len(title) > 2000:
        raise ValueError("Discord卡片文字過長")
    send_discord({"content": title, "embeds": embeds})


def send_crossing(stock, signal, demo=False):
    direction = signal["crossed"][0]["direction"]
    lines = [f"**{'🔴 ⬆ 向上穿越' if item['direction'] == 'up' else '🟢 ⬇ 向下穿越'}｜{item['name']} {item['level']:.2f}**"
             for item in signal["crossed"]]
    end = datetime.fromisoformat(signal["bar_at"]) + timedelta(minutes=1)
    description = (f"前根收盤 {signal['previous']:.2f} → 本根收盤 **{signal['price']:.2f}**\n"
                   + "\n".join(lines) + f"\n1分K收盤：{end:%Y-%m-%d %H:%M}\n"
                   + ("格式示範，未觸發真實穿越。" if demo else
                      f"行情距今 {signal['age_minutes']:.1f} 分；價位穿越提醒。"))
    send_embeds("🧪 1分K卡片格式測試" if demo else "🔔 1分K價位穿越",
                [stock_card(stock, "示範上穿" if demo and direction == "up" else
                    "示範下穿" if demo else "上穿提醒" if direction == "up" else "下穿提醒",
                    description, direction, extra=volume_fields(signal), demo=demo)])


def send_strategy(stock, signal, current, notification_number):
    end = datetime.fromisoformat(signal["bar_at"]) + timedelta(minutes=1)
    long = stock["direction"] == "long"
    description = (f"觀察價 **{signal['price']:.2f}**｜1分K收盤 {end:%H:%M}\n"
        + ("放量上穿開盤前5根高點，站上VWAP與前收。" if long else
           "測壓失敗，後2根量縮收跌，下穿測壓K低點並位於VWAP下方。")
        + f"\n本日通知 {notification_number}/{MAX_SIGNALS_PER_DAY}｜規則分數 {stock['score']}"
        + f"\n行情距今 {signal['age_minutes']:.1f}分｜推播 {current:%H:%M:%S}")
    extra = [{"name": "策略壓力（開盤5分鐘高點）" if long else "策略壓力（前20交易日高點）",
              "value": f"{signal['resistance']:.2f}", "inline": True},
             {"name": "VWAP", "value": f"{signal['vwap']:.2f}", "inline": True},
             {"name": "型態失效參考", "value": f"{signal['stop']:.2f}", "inline": True}]
    send_embeds("📈 做多條件成立" if long else "📉 做空條件成立",
                [stock_card(stock, "做多1分K" if long else "做空1分K", description,
                            stock["direction"], extra + volume_fields(signal))])


def send_monitor_cards(stocks, asof):
    for direction in ("long", "short"):
        selected = [item for item in stocks if item["direction"] == direction]
        if selected:
            send_embeds(f"🔎 {'做多' if direction == 'long' else '做空'}1分K候選名單｜資料日 {asof}",
                [stock_card(item, f"#{rank}", f"前收 **{item['close']:.2f}**｜規則分數 {item['score']}\n"
                    "監控壓力／交界／支撐的上穿與下穿，及原有策略條件。", direction)
                 for rank, item in enumerate(selected, 1)])


def test_cards():
    send_embeds("✅ 做多＋做空1分K卡片連線測試", [{
        "title": "卡片格式已啟用", "color": 0x5865F2,
        "description": "平日台灣08:55起執行，09:00至13:30監控。每60秒檢查已收盤1分K。\n"
            "上穿：亮紅色；下穿：亮綠色，穿越文字加粗。附壓力NH、交界CDP、支撐NL、本根1分K量、前20根均量與放量倍數。\n"
            "行情可能延遲、排程可能晚啟動。以下是示範數值，未產生真實訊號。"}])
    stock = {"code": "示範", "name": "格式測試", **cdp_levels(110, 90, 100, "示範")}
    for previous, price in ((99, 101), (101, 99)):
        signal = {"previous": previous, "price": price, "age_minutes": 0,
                  "bar_at": now_tw().isoformat(), "volume_shares": 500000,
                  "volume_base_shares": 200000, "volume_ratio_1m": 2.5, "volume_base_count": 20, "crossed": [{"name": "交界 CDP", "level": 100,
                       "direction": "up" if price > previous else "down"}]}
        send_crossing(stock, signal, demo=True)


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

    if not shortlist:
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

    stocks = [{**item, "direction": "short"} for item in shortlist["stocks"]]
    stocks += long_candidates(asof)
    stocks = attach_levels(stocks, asof)
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
        f"🔎 做多＋做空1分K監控啟動｜{current:%Y-%m-%d %H:%M}\n"
        f"候選資料日：{asof}\n"
        f"{names}\n"
        "每60秒檢查1分鐘K，行情可能延遲。\n"
        "訊號是條件提醒，不是立即下單指令。"
    )

    send_monitor_cards(stocks, asof)

    seen_today = False
    failures = 0
    crossings = load("crossings.json", {})
    crossings = {key: value for key, value in crossings.items() if key[:10] >= earliest}

    while True:
        current = now_tw()

        if current.time() > clock(13, 30):
            break

        if current.time() >= clock(9):
            try:
                data = download(
                    sorted({stock["ticker"] for stock in stocks}),
                    "1m",
                    "5d",
                )

                for stock in stocks:
                    df = frame_for(
                        data, stock["ticker"]
                    )

                    bars = completed_bars(df, current)

                    if not bars.empty:
                        seen_today = True

                    crossing = crossing_signal(df, stock, current)
                    if crossing:
                        cross_key = f"{current.date()}:{stock['ticker']}:{crossing['bar_at']}"
                        if cross_key not in crossings:
                            send_crossing(stock, crossing)
                            crossings[cross_key] = crossing
                            save("crossings.json", crossings)

                    signal = (long_signal if stock["direction"] == "long" else short_signal)(df, stock, current)
                    if signal:
                        signal.update(volume_metrics(bars))
                    event_stock = {**stock, "ticker": stock["ticker"] + ":" + stock["direction"]}

                    key = (
                        f"{current.date()}:{event_stock['ticker']}:{signal['bar_at']}"
                        if signal else ""
                    )

                    if signal and notification_allowed(signal, event_stock, current, events):
                        bar_at = datetime.fromisoformat(
                            signal["bar_at"]
                        )

                        bar_end = (
                            bar_at + timedelta(minutes=1)
                        )

                        event_prefix = f"{current.date()}:{event_stock['ticker']}"
                        notification_number = 1 + sum(
                            event_key == event_prefix
                            or event_key.startswith(event_prefix + ":")
                            for event_key in events
                        )

                        send_strategy(stock, signal, current, notification_number)

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
        test_cards()

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

