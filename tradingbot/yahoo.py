"""Yahoo Finance market data (candles, last price, market hours, FX).

Trading 212's API has no price data, so stocks/ETFs use Yahoo's public chart
endpoint. `YahooData.fetch_ohlcv` mirrors ccxt's signature so the engine and
the history downloader can use either source.
"""
from __future__ import annotations

import logging
import math
import re
import time

import requests

from .data import timeframe_seconds

log = logging.getLogger(__name__)

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
INTERVALS = {"1m": "1m", "2m": "2m", "5m": "5m", "15m": "15m", "30m": "30m",
             "1h": "60m", "60m": "60m", "90m": "90m", "1d": "1d", "1w": "1wk"}
# Yahoo caps how far back intraday data goes.
MAX_LOOKBACK_DAYS = {"1m": 7, "2m": 59, "5m": 59, "15m": 59, "30m": 59, "60m": 729, "90m": 59}

# Trading 212 ticker suffix -> Yahoo exchange suffix
T212_SUFFIXES = {"l": ".L", "d": ".DE", "p": ".PA", "a": ".AS", "m": ".MC", "s": ".SW", "e": ".MI"}


def t212_to_yahoo(ticker: str) -> str:
    """AAPL_US_EQ -> AAPL, VUSAl_EQ -> VUSA.L, SAPd_EQ -> SAP.DE."""
    if "/" in ticker:  # already a pair like BTC/USD
        return ticker.replace("/", "-")
    m = re.fullmatch(r"([A-Z0-9.]+)_US_EQ", ticker)
    if m:
        return m.group(1).replace(".", "-")
    m = re.fullmatch(r"([A-Z0-9.]+)([a-z])_EQ", ticker)
    if m and m.group(2) in T212_SUFFIXES:
        return m.group(1) + T212_SUFFIXES[m.group(2)]
    if re.fullmatch(r"[A-Z0-9.\-^=]+", ticker):
        return ticker  # looks like a Yahoo symbol already
    raise ValueError(f"Can't derive a Yahoo symbol from '{ticker}'; set trading212.data_symbol")


class YahooData:
    def __init__(self, symbol_map: dict[str, str] | None = None, session: requests.Session | None = None,
                 cache_seconds: float = 5.0):
        self.symbol_map = symbol_map or {}
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", "Mozilla/5.0 (tradingbot)")
        self.cache_seconds = cache_seconds
        self._meta_cache: dict[str, tuple[float, dict]] = {}
        self.id = "yahoo"

    def resolve(self, symbol: str) -> str:
        return self.symbol_map.get(symbol) or t212_to_yahoo(symbol)

    def _chart(self, symbol: str, params: dict) -> dict:
        url = CHART_URL.format(symbol=self.resolve(symbol))
        delay = 2.0
        for attempt in range(4):
            try:
                r = self.session.get(url, params=params, timeout=15)
                if r.status_code == 429 or r.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {r.status_code}")
                r.raise_for_status()
                data = r.json()["chart"]
                if data.get("error"):
                    raise ValueError(f"Yahoo error for {symbol}: {data['error']}")
                return data["result"][0]
            except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
                if attempt == 3:
                    raise
                log.warning("Yahoo request failed (%s), retrying in %.0fs", e, delay)
                time.sleep(delay)
                delay *= 2
        raise RuntimeError("unreachable")

    # ---- ccxt-compatible candles ------------------------------------------------
    def fetch_ohlcv(self, symbol: str, timeframe: str, since: int | None = None, limit: int | None = None) -> list[list]:
        interval = INTERVALS.get(timeframe)
        if not interval:
            raise ValueError(f"Yahoo doesn't support timeframe '{timeframe}'. Use one of {sorted(INTERVALS)}")
        now = int(time.time())
        if since is not None:
            start = since // 1000
        else:
            tf = timeframe_seconds(timeframe)
            # ~6.5 trading hours/day, 5 days/week: pad generously to get `limit` bars.
            bars_per_day = max(1.0, 6.5 * 3600 / tf) if tf < 86400 else 5 / 7
            days = math.ceil((limit or 300) / bars_per_day * 7 / 5) + 5
            start = now - days * 86400
        max_days = MAX_LOOKBACK_DAYS.get(interval)
        if max_days:
            start = max(start, now - max_days * 86400)
        result = self._chart(symbol, {"interval": interval, "period1": start, "period2": now,
                                      "includePrePost": "false", "events": "div,splits"})
        self._meta_cache[symbol] = (time.monotonic(), result.get("meta", {}))
        rows = []
        quote = (result.get("indicators", {}).get("quote") or [{}])[0]
        for i, ts in enumerate(result.get("timestamp") or []):
            vals = [quote.get(k, [None])[i] if i < len(quote.get(k, [])) else None
                    for k in ("open", "high", "low", "close", "volume")]
            if any(v is None for v in vals[:4]):
                continue  # Yahoo pads gaps with nulls
            rows.append([ts * 1000, *[float(v or 0) for v in vals]])
        if limit and since is None:
            rows = rows[-limit:]
        return rows

    # ---- quotes & market hours ----------------------------------------------------
    def meta(self, symbol: str) -> dict:
        cached = self._meta_cache.get(symbol)
        if cached and time.monotonic() - cached[0] < self.cache_seconds:
            return cached[1]
        result = self._chart(symbol, {"interval": "1m", "range": "1d"})
        meta = result.get("meta", {})
        self._meta_cache[symbol] = (time.monotonic(), meta)
        return meta

    def last_price(self, symbol: str) -> float:
        price = self.meta(symbol).get("regularMarketPrice")
        if price is None:
            raise ValueError(f"No price for {symbol}")
        return float(price)

    def currency(self, symbol: str) -> str:
        return self.meta(symbol).get("currency", "")

    def is_market_open(self, symbol: str, now: float | None = None) -> bool:
        period = (self.meta(symbol).get("currentTradingPeriod") or {}).get("regular") or {}
        start, end = period.get("start"), period.get("end")
        if start is None or end is None:
            return True  # unknown (e.g. crypto): assume 24/7
        now = time.time() if now is None else now
        return start <= now < end

    def fx_rate(self, from_ccy: str, to_ccy: str) -> float:
        """Units of `to_ccy` per 1 `from_ccy`. Handles GBX (pence)."""
        scale = 1.0
        if to_ccy == "GBX":
            to_ccy, scale = "GBP", 100.0
        if from_ccy == "GBX":
            from_ccy, scale = "GBP", scale / 100.0
        if from_ccy == to_ccy:
            return scale
        return scale * self.last_price(f"{from_ccy}{to_ccy}=X")
