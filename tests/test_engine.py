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


class RecordingNotifier:
    enabled = True

    def __init__(self):
        self.messages = []

    def send(self, text, key=None, throttle=0):
        self.messages.append(text)

    def start_commands(self, handlers):
        self.handlers = handlers

    def close(self, timeout=5):
        pass


def test_engine_sends_trade_alerts(tmp_path):
    df = synthetic_ohlcv(1000, seed=1)
    df.index = df.index - (df.index[-1] - pd.Timestamp.now(tz="UTC").floor("h")) - pd.Timedelta(hours=2)
    engine, ex = make_engine(tmp_path, df)
    notifier = RecordingNotifier()
    engine.notifier = engine.trader.notifier = notifier
    for _ in range(300):
        engine.step()
        ex.cursor += 1
    assert any(m.startswith("🟢 BUY") for m in notifier.messages)
    assert "Position:" in engine.status_text()


def test_market_closed_blocks_trading(tmp_path):
    df = synthetic_ohlcv(1000, seed=1)
    df.index = df.index - (df.index[-1] - pd.Timestamp.now(tz="UTC").floor("h")) - pd.Timedelta(hours=2)
    engine, ex = make_engine(tmp_path, df)
    ex.is_market_open = lambda symbol: False
    for _ in range(300):
        engine.step()
        ex.cursor += 1
    assert not engine.trader.trades and not engine.trader.position.is_open
    assert engine.market_open is False


def test_telegram_stop_command(tmp_path):
    df = synthetic_ohlcv(400)
    df.index = df.index - (df.index[-1] - pd.Timestamp.now(tz="UTC").floor("h")) - pd.Timedelta(hours=2)
    engine, ex = make_engine(tmp_path, df)
    engine.trader.enter(1.0, atr_value=1.0, now=pd.Timestamp.now(tz="UTC"))
    engine._cmd_stop()
    engine.step()
    assert not engine.trader.position.is_open and not engine.running


def test_reconcile_adjusts_missing_position(tmp_path):
    df = synthetic_ohlcv(400)
    engine, ex = make_engine(tmp_path, df)
    engine.trader.enter(2.0, atr_value=1.0, now=pd.Timestamp.now(tz="UTC"))

    class NotPaper:  # pretend the broker is a real one whose position was sold manually
        symbol = "BTC/USDT"
        def balances(self):
            return 100.0, 0.0
    engine.broker = NotPaper()
    engine.reconcile()
    assert not engine.trader.position.is_open
