"""Market data: download from an exchange, load/save CSV, or generate synthetic prices."""
from __future__ import annotations

import logging
import time

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

COLUMNS = ["open", "high", "low", "close", "volume"]


def timeframe_seconds(timeframe: str) -> int:
    units = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
    return int(timeframe[:-1]) * units[timeframe[-1]]


def ohlcv_to_frame(rows: list[list]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["timestamp", *COLUMNS])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.drop_duplicates("timestamp").set_index("timestamp").sort_index()
    return df.astype(float)


def fetch_ohlcv(exchange, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
    """Most recent `limit` candles (the last one may still be forming)."""
    from .brokers.ccxt_broker import with_retries

    rows = with_retries(exchange.fetch_ohlcv, symbol, timeframe, limit=limit)
    return ohlcv_to_frame(rows)


def fetch_history(exchange, symbol: str, timeframe: str, since: str, until: str | None = None) -> pd.DataFrame:
    """Paginated download of historical candles for backtesting."""
    from .brokers.ccxt_broker import with_retries

    since_ms = int(pd.Timestamp(since, tz="UTC").timestamp() * 1000)
    until_ms = int(pd.Timestamp(until, tz="UTC").timestamp() * 1000) if until else None
    step = timeframe_seconds(timeframe) * 1000
    rows: list[list] = []
    while True:
        batch = with_retries(exchange.fetch_ohlcv, symbol, timeframe, since=since_ms, limit=1000)
        if not batch:
            break
        rows.extend(batch)
        last = batch[-1][0]
        log.info("Downloaded %d candles (up to %s)", len(rows), pd.Timestamp(last, unit="ms"))
        if (until_ms and last >= until_ms) or last + step > time.time() * 1000:
            break
        since_ms = last + step
    df = ohlcv_to_frame(rows)
    if until_ms:
        df = df[df.index < pd.Timestamp(until_ms, unit="ms", tz="UTC")]
    return df


def drop_open_candle(df: pd.DataFrame, timeframe: str, now: pd.Timestamp | None = None) -> pd.DataFrame:
    """Remove the last candle if it hasn't closed yet."""
    if df.empty:
        return df
    now = now or pd.Timestamp.now(tz="UTC")
    close_time = df.index[-1] + pd.Timedelta(seconds=timeframe_seconds(timeframe))
    return df.iloc[:-1] if close_time > now else df


def load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    ts = df["timestamp"]
    if np.issubdtype(ts.dtype, np.number):
        df["timestamp"] = pd.to_datetime(ts, unit="ms", utc=True)
    else:
        df["timestamp"] = pd.to_datetime(ts, utc=True)
    return df.set_index("timestamp").sort_index()[COLUMNS].astype(float)


def save_csv(df: pd.DataFrame, path: str) -> None:
    df.reset_index().to_csv(path, index=False)


def synthetic_ohlcv(bars: int = 2000, seed: int = 42, start_price: float = 100.0, freq: str = "1h") -> pd.DataFrame:
    """Regime-switching random walk — handy for testing without network access."""
    rng = np.random.default_rng(seed)
    drift = np.repeat(rng.normal(0, 0.002, bars // 200 + 1), 200)[:bars]
    returns = drift + rng.normal(0, 0.01, bars)
    close = start_price * np.exp(np.cumsum(returns))
    open_ = np.concatenate([[start_price], close[:-1]])
    spread = np.abs(rng.normal(0, 0.005, bars)) * close
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    volume = rng.uniform(100, 1000, bars)
    idx = pd.date_range("2024-01-01", periods=bars, freq=freq.replace("m", "min"), tz="UTC")
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=idx)
