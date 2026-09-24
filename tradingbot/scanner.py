"""Stock scanner: rank a universe of tickers and pick the best one to trade.

Each ticker gets a risk-adjusted momentum score over `lookback_bars` closed candles:

    score = return over the lookback / volatility over the lookback

so a steady +8% climb beats a choppy +8%. A ticker is only eligible if the
configured strategy currently says "be long" (target 1) and the score is
positive. The engine uses `Scanner.best()` while it is flat and switches to
the winner; once in a position it stays on that ticker until the exit.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass

import numpy as np

from .config import ScannerConfig
from .data import drop_open_candle, fetch_ohlcv
from .strategies import Strategy

log = logging.getLogger(__name__)

# Large, liquid US stocks available on Trading 212 (used when scanner.universe is empty).
# Meta is listed under its old ticker, FB_US_EQ.
DEFAULT_UNIVERSE = [f"{t}_US_EQ" for t in (
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "FB", "TSLA", "AVGO", "AMD", "NFLX",
    "ORCL", "CRM", "ADBE", "QCOM", "INTC", "CSCO", "PLTR", "UBER", "SHOP", "MU",
    "JPM", "BAC", "V", "MA", "GS", "UNH", "LLY", "JNJ", "ABBV", "MRK",
    "PFE", "TMO", "XOM", "CVX", "WMT", "COST", "PG", "KO", "PEP", "HD",
    "MCD", "NKE", "DIS", "CAT", "BA",
)]


@dataclass
class ScanResult:
    symbol: str
    price: float
    momentum: float    # total return over the lookback
    volatility: float  # stdev of log returns scaled to the lookback
    score: float
    signal: int        # strategy target on the latest closed candle

    def eligible(self, min_price: float) -> bool:
        return self.signal == 1 and self.score > 0 and self.price >= min_price


def score_candles(df, strategy: Strategy, lookback: int) -> tuple[float, float, float, int] | None:
    """(momentum, volatility, score, signal) or None if there isn't enough history."""
    if len(df) < max(lookback + 1, strategy.warmup_bars):
        return None
    close = df["close"].to_numpy()
    window = close[-(lookback + 1):]
    momentum = window[-1] / window[0] - 1
    volatility = float(np.std(np.diff(np.log(window)), ddof=1)) * math.sqrt(lookback)
    score = momentum / volatility if volatility > 0 else 0.0
    return momentum, volatility, score, strategy.latest_target(df)


class Scanner:
    def __init__(self, data, strategy: Strategy, cfg: ScannerConfig, timeframe: str, history_bars: int,
                 universe: list[str] | None = None, sleep=time.sleep):
        self.data = data  # anything with ccxt-style fetch_ohlcv (YahooData for stocks)
        self.strategy = strategy
        self.cfg = cfg
        self.timeframe = timeframe
        self.history_bars = max(history_bars, cfg.lookback_bars + 1)
        self.universe = list(universe if universe is not None else (cfg.universe or DEFAULT_UNIVERSE))
        self._sleep = sleep
        self.results: list[ScanResult] = []
        self.last_scan: float | None = None  # time.time() of the last completed scan

    def scan(self) -> list[ScanResult]:
        """Score every ticker in the universe, best first. Tickers that fail are skipped."""
        results = []
        for i, symbol in enumerate(self.universe):
            if i:
                self._sleep(self.cfg.request_delay)  # be gentle with the free data feed
            try:
                df = fetch_ohlcv(self.data, symbol, self.timeframe, self.history_bars + 1)
                df = drop_open_candle(df, self.timeframe)
                scored = score_candles(df, self.strategy, self.cfg.lookback_bars)
            except Exception as e:
                log.warning("Scanner: skipping %s (%s)", symbol, e)
                continue
            if scored is None:
                log.info("Scanner: skipping %s (not enough history)", symbol)
                continue
            results.append(ScanResult(symbol, float(df["close"].iloc[-1]), *scored))
        results.sort(key=lambda r: r.score, reverse=True)
        self.results, self.last_scan = results, time.time()
        return results

    def due(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return self.last_scan is None or now - self.last_scan >= self.cfg.rescan_minutes * 60

    def best(self) -> ScanResult | None:
        return next((r for r in self.results if r.eligible(self.cfg.min_price)), None)

    def summary(self, top: int = 5) -> str:
        if not self.results:
            return "No scan yet"
        lines = [f"🔎 Top {min(top, len(self.results))} of {len(self.results)} scanned:"]
        for r in self.results[:top]:
            mark = "✅" if r.eligible(self.cfg.min_price) else "·"
            lines.append(f"{mark} {r.symbol}: score {r.score:+.2f} · {r.momentum:+.1%} · {r.price:.2f}")
        return "\n".join(lines)


def format_table(results: list[ScanResult], min_price: float, top: int | None = None) -> str:
    rows = results[:top] if top else results
    out = [f"{'#':>3}  {'ticker':<12} {'price':>10} {'return':>8} {'vol':>7} {'score':>7}  signal"]
    for i, r in enumerate(rows, 1):
        signal = "BUY" if r.eligible(min_price) else ("long" if r.signal else "flat")
        out.append(f"{i:>3}  {r.symbol:<12} {r.price:>10.2f} {r.momentum:>+8.1%} {r.volatility:>7.1%} "
                   f"{r.score:>+7.2f}  {signal}")
    return "\n".join(out)
