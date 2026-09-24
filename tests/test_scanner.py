import numpy as np
import pandas as pd

from fakes import FakeSession
from test_trading212 import FakeData, FakeT212
from tradingbot.brokers.paper import PaperBroker
from tradingbot.brokers.trading212 import Trading212Broker, Trading212Client
from tradingbot.config import Config, ScannerConfig
from tradingbot.engine import Engine
from tradingbot.scanner import Scanner, format_table, score_candles
from tradingbot.strategies import create_strategy


def trend(drift, bars=300, seed=0, start=100.0):
    """Hourly candles with a given per-bar drift, ending two hours ago (all closed)."""
    rng = np.random.default_rng(seed)
    close = start * np.exp(np.cumsum(drift + rng.normal(0, 0.004, bars)))
    open_ = np.concatenate([[start], close[:-1]])
    end = pd.Timestamp.now(tz="UTC").floor("h") - pd.Timedelta(hours=2)
    idx = pd.date_range(end=end, periods=bars, freq="h", tz="UTC")
    return pd.DataFrame({"open": open_, "high": np.maximum(open_, close) * 1.001,
                         "low": np.minimum(open_, close) * 0.999, "close": close, "volume": 1000.0}, index=idx)


class MultiData:
    """ccxt-style candles for several symbols; unknown symbols raise like a bad ticker would."""

    def __init__(self, frames):
        self.frames = frames
        self.requests = []

    def fetch_ohlcv(self, symbol, timeframe, limit=None, since=None):
        self.requests.append(symbol)
        df = self.frames[symbol].iloc[-limit:]
        return [[int(ts.timestamp() * 1000), *row] for ts, row in zip(df.index, df.to_numpy().tolist())]

    def price(self, symbol):
        return float(self.frames[symbol]["close"].iloc[-1])


FRAMES = {
    "STEADY_US_EQ": trend(0.002, seed=1),    # strong, smooth uptrend
    "CHOPPY_US_EQ": trend(0.0005, seed=2),   # weak uptrend
    "FALLING_US_EQ": trend(-0.002, seed=3),  # downtrend: strategy says flat
}


def make_scanner(frames=FRAMES, universe=None, **cfg):
    data = MultiData(frames)
    sc = Scanner(data, create_strategy("sma_crossover"), ScannerConfig(enabled=True, **cfg), "1h", 200,
                 universe=universe or list(frames), sleep=lambda s: None)
    return sc, data


def test_score_prefers_steady_uptrend_and_needs_history():
    strategy = create_strategy("sma_crossover")
    up = score_candles(FRAMES["STEADY_US_EQ"], strategy, 120)
    down = score_candles(FRAMES["FALLING_US_EQ"], strategy, 120)
    assert up[0] > 0 and up[2] > 0 and up[3] == 1
    assert down[2] < 0 and down[3] == 0
    assert score_candles(FRAMES["STEADY_US_EQ"].iloc[:30], strategy, 120) is None


def test_scan_ranks_and_picks_best_eligible():
    sc, _ = make_scanner()
    results = sc.scan()
    assert [r.symbol for r in results][0] == "STEADY_US_EQ"
    assert results[-1].symbol == "FALLING_US_EQ"
    assert sc.best().symbol == "STEADY_US_EQ"
    assert "STEADY_US_EQ" in format_table(results, 5.0) and "BUY" in format_table(results, 5.0)
    assert "STEADY_US_EQ" in sc.summary()


def test_scan_skips_failing_tickers_and_respects_min_price():
    sc, _ = make_scanner(universe=["NOPE_US_EQ", "STEADY_US_EQ"], min_price=1e9)
    results = sc.scan()
    assert [r.symbol for r in results] == ["STEADY_US_EQ"]
    assert sc.best() is None  # too cheap for min_price


def test_scan_due_after_rescan_interval():
    sc, _ = make_scanner(rescan_minutes=60)
    assert sc.due()
    sc.scan()
    assert not sc.due()
    assert sc.due(now=sc.last_scan + 3601)


def make_engine(tmp_path, frames=FRAMES, start="FALLING_US_EQ"):
    cfg = Config()
    cfg.engine.state_dir = str(tmp_path)
    cfg.engine.history_bars = 200
    sc, data = make_scanner(frames)
    broker = PaperBroker(start, 10_000, price_source=lambda: data.price(broker.symbol))
    return Engine(cfg, create_strategy("sma_crossover"), broker, data, scanner=sc), data


def test_engine_switches_to_best_stock_and_buys(tmp_path):
    engine, _ = make_engine(tmp_path)
    engine.step()
    assert engine.symbol == "STEADY_US_EQ"
    assert engine.trader.position.is_open
    assert engine.state_file.name.endswith("_scanner.json")

    # Resuming restores the stock the scanner picked.
    resumed, _ = make_engine(tmp_path)
    assert resumed.symbol == "STEADY_US_EQ" and resumed.trader.position.is_open


def test_engine_holds_position_instead_of_rotating(tmp_path):
    engine, data = make_engine(tmp_path)
    engine.step()
    engine.scanner.last_scan = None  # rescan would be due, but we're in a position
    requests = len(data.requests)
    engine.step()
    assert engine.symbol == "STEADY_US_EQ"
    assert data.requests[requests:] == ["STEADY_US_EQ"]  # only the held stock's candles


def test_engine_stays_flat_when_nothing_is_eligible(tmp_path):
    frames = {"FALLING_US_EQ": FRAMES["FALLING_US_EQ"]}
    engine, _ = make_engine(tmp_path, frames)
    engine.step()
    assert engine.symbol == "FALLING_US_EQ" and not engine.trader.position.is_open


def test_trading212_set_symbol_and_tradable_filter():
    class MultiT212(FakeT212):
        def __call__(self, method, url, params, json):
            if url.endswith("/metadata/instruments"):
                from fakes import FakeResponse
                self.instrument_calls = getattr(self, "instrument_calls", 0) + 1
                return FakeResponse(200, [
                    {"ticker": "AAPL_US_EQ", "name": "Apple", "currencyCode": "USD", "shortName": "AAPL"},
                    {"ticker": "MSFT_US_EQ", "name": "Microsoft", "currencyCode": "USD", "shortName": "MSFT"},
                    {"ticker": "FB_US_EQ", "name": "Meta Platforms", "currencyCode": "USD", "shortName": "META"},
                    {"ticker": "CTRA_US_EQ", "name": "Alpha Metallurgical", "currencyCode": "USD",
                     "shortName": "AMR"},
                    {"ticker": "VODl_EQ", "name": "Vodafone", "currencyCode": "GBX"},
                ])
            return super().__call__(method, url, params, json)

    api = MultiT212()
    client = Trading212Client("k", "s", "demo", session=FakeSession(api), sleep=lambda s: None)
    data = FakeData()
    data.symbol_map = {}
    broker = Trading212Broker(client, "AAPL_US_EQ", data, sleep=lambda s: None)
    universe = ["MSFT_US_EQ", "VODl_EQ", "NOPE_US_EQ", "AAPL_US_EQ", "META_US_EQ", "CTRA_US_EQ"]
    assert broker.tradable(universe) == ["MSFT_US_EQ", "AAPL_US_EQ", "FB_US_EQ", "CTRA_US_EQ"]
    # Prices come from the market symbol, not the (stale) T212 ticker.
    assert data.symbol_map["FB_US_EQ"] == "META" and data.symbol_map["CTRA_US_EQ"] == "AMR"
    broker.set_symbol("MSFT_US_EQ")
    assert broker.symbol == "MSFT_US_EQ" and broker.instrument["name"] == "Microsoft"
    assert api.instrument_calls == 1  # instrument list is cached


def test_default_universe():
    from tradingbot.scanner import DEFAULT_UNIVERSE
    assert len(DEFAULT_UNIVERSE) == len(set(DEFAULT_UNIVERSE)) == 45
    assert {"FB_US_EQ", "AMD_US_EQ", "TSLA_US_EQ"} <= set(DEFAULT_UNIVERSE)
