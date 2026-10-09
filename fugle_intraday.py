"""Fugle regular-lot five-minute candles in the bot's canonical share units."""
import json
import math
import os
import re
import time
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

TZ = ZoneInfo("Asia/Taipei")
_last_request = 0.0


def parse_candles(payload, ticker, current):
    symbol, market = ticker.split(".")
    if (payload.get("symbol") != symbol
            or payload.get("exchange") != {"TW": "TWSE", "TWO": "TPEx"}[market]
            or str(payload.get("timeframe")) != "5"
            or payload.get("date") != current.date().isoformat()
            or payload.get("type") != "EQUITY"):
        raise ValueError("Fugle candle metadata mismatch")
    rows = []
    for item in payload["data"]:
        stamp = datetime.fromisoformat(item["date"])
        if stamp.tzinfo is None:
            raise ValueError("Candle timezone missing")
        stamp = stamp.astimezone(TZ)
        values = [float(item[k]) for k in ("open", "high", "low", "close", "volume")]
        opening, high, low, close, volume = values
        if (not all(math.isfinite(v) for v in values)
                or min(opening, high, low, close) <= 0 or volume < 0
                or not low <= min(opening, close) <= max(opening, close) <= high
                or stamp.date() != current.date() or stamp > current
                or stamp.minute % 5 or stamp.second or stamp.microsecond):
            raise ValueError("Invalid Fugle candle")
        rows.append([stamp, opening, high, low, close, volume * 1000])
    frame = pd.DataFrame(rows, columns=["Date", "Open", "High", "Low", "Close", "Volume"])
    frame = frame.set_index("Date").sort_index()
    if frame.index.has_duplicates:
        raise ValueError("Duplicate Fugle candles")
    return frame


def download(tickers, current):
    global _last_request
    key = os.environ.get("FUGLE_API_KEY", "")
    if not key:
        raise RuntimeError("Missing FUGLE_API_KEY")
    frames = {}
    for ticker in tickers:
        if not re.fullmatch(r"[1-9]\d{3}\.(TW|TWO)", ticker):
            raise ValueError("Unsupported ticker")
        # Keep this monitor below the basic plan's 60 requests/minute quota.
        time.sleep(max(0, 1.1 - (time.monotonic() - _last_request)))
        _last_request = time.monotonic()
        req = urllib.request.Request(
            "https://api.fugle.tw/marketdata/v1.0/stock/intraday/candles/"
            + ticker.split(".")[0] + "?timeframe=5&sort=asc",
            headers={"X-API-KEY": key, "User-Agent": "taiwan-discord"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                payload = json.load(response)
            frames[ticker] = parse_candles(payload, ticker, current)
        except Exception:
            # Never log request headers, keys, or provider error bodies.
            raise RuntimeError("Fugle intraday download failed") from None
    return pd.concat(frames, axis=1) if frames else pd.DataFrame()
