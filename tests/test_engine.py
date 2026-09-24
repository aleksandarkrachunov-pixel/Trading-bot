import json

import pandas as pd

from tradingbot.brokers.paper import PaperBroker
from tradingbot.config import Config
from tradingbot.data import synthetic_ohlcv
from tradingbot.engine import Engine
from tradingbot.strategies import create_strategy


class FakeExchange:
    """Serves synthetic candles ending 'now' so every candle is closed."""

    def __init__(self, df):
        self.df = df
        self.cursor = 200

    def fetch_ohlcv(self, symbol, timeframe, limit=None, since=None):
        window = self.df.iloc[max(0, self.cursor - limit): self.cursor]
        return [[int(ts.timestamp() * 1000), *row] for ts, row in zip(window.index, window.to_numpy().tolist())]

    def price(self):
        return float(self.df["close"].iloc[self.cursor - 1])


def make_engine(tmp_path, df):
    cfg = Config()
    cfg.engine.state_dir = str(tmp_path)
    cfg.engine.history_bars = 150
    ex = FakeExchange(df)
    broker = PaperBroker("BTC/USDT", 10_000, price_source=ex.price)
    return Engine(cfg, create_strategy("sma_crossover"), broker, ex), ex


def test_engine_trades_and_persists_state(tmp_path):
    # Candles in the past so drop_open_candle keeps them all.
    df = synthetic_ohlcv(1000, seed=1)
    df.index = df.index - (df.index[-1] - pd.Timestamp.now(tz="UTC").floor("h")) - pd.Timedelta(hours=2)
    engine, ex = make_engine(tmp_path, df)
    for _ in range(600):
        engine.step()
        ex.cursor += 1
    assert engine.trader.trades, "expected the engine to trade on synthetic data"
    state = json.loads(engine.state_file.read_text())
    assert len(state["trader"]["trades"]) == len(engine.trader.trades)

    # A fresh engine resumes the same position and balances.
    resumed, _ = make_engine(tmp_path, df)
    assert resumed.trader.position == engine.trader.position
    assert resumed.broker.cash == engine.broker.cash
    assert resumed.last_bar == engine.last_bar


def test_stop_file_flattens(tmp_path):
    df = synthetic_ohlcv(400)
    df.index = df.index - (df.index[-1] - pd.Timestamp.now(tz="UTC").floor("h")) - pd.Timedelta(hours=2)
    engine, ex = make_engine(tmp_path, df)
    engine.trader.enter(1.0, atr_value=1.0, now=pd.Timestamp.now(tz="UTC"))
    (tmp_path / "STOP").touch()
    engine.step()
    assert not engine.trader.position.is_open
    assert not engine.running
