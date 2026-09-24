import pandas as pd
import pytest

from tradingbot.backtest import run_backtest
from tradingbot.brokers.paper import PaperBroker
from tradingbot.config import Config, RiskConfig
from tradingbot.data import synthetic_ohlcv
from tradingbot.optimize import grid_search, parse_grid
from tradingbot.risk import RiskManager
from tradingbot.strategies import create_strategy
from tradingbot.trader import Trader


def test_paper_broker_fees_and_balances():
    b = PaperBroker("BTC/USDT", 1000, fee_rate=0.01, slippage=0)
    b.set_price(100)
    fill = b.market_buy(5)
    assert fill.qty == 5 and fill.fee == pytest.approx(5)
    assert b.cash == pytest.approx(1000 - 500 - 5)
    b.set_price(110)
    b.market_sell(5)
    assert b.position == 0
    assert b.cash == pytest.approx(495 + 550 - 5.5)


def test_paper_broker_never_overspends():
    b = PaperBroker("BTC/USDT", 100, fee_rate=0.001, slippage=0.001)
    b.set_price(10)
    b.market_buy(1000)
    assert b.cash >= -1e-9


def test_stop_loss_fills_at_stop_or_gap():
    cfg = Config()
    broker = PaperBroker("X/USDT", 10_000, fee_rate=0, slippage=0)
    trader = Trader(broker, RiskManager(cfg.risk))
    broker.set_price(100)
    trader.enter(10, atr_value=5, now=pd.Timestamp("2024-01-01"))
    assert trader.position.stop == pytest.approx(90)
    # gap down through the stop -> filled at the open, not the stop
    trader.check_exits(open_=85, high=88, low=80, now=pd.Timestamp("2024-01-02"))
    assert trader.trades[-1].exit_price == pytest.approx(85)
    assert trader.trades[-1].exit_reason == "stop_loss"
    assert trader.position.wait_for_reset


def test_backtest_runs_and_conserves_money():
    cfg = Config()
    cfg.backtest.fee_rate = 0
    cfg.backtest.slippage = 0
    df = synthetic_ohlcv(2000)
    res = run_backtest(df, create_strategy("sma_crossover"), cfg)
    assert len(res.equity) == len(df)
    realised = sum(t.pnl for t in res.trades)
    assert res.equity.iloc[-1] == pytest.approx(cfg.backtest.initial_cash + realised)
    assert -1 < res.stats["max_drawdown"] <= 0


def test_flat_market_no_trades():
    idx = pd.date_range("2024-01-01", periods=300, freq="h", tz="UTC")
    df = pd.DataFrame({"open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0, "volume": 1.0}, index=idx)
    res = run_backtest(df, create_strategy("sma_crossover"), Config())
    assert res.stats["num_trades"] == 0
    assert res.stats["final_equity"] == Config().backtest.initial_cash


def test_kill_switch_stops_trading():
    cfg = Config(risk=RiskConfig(max_drawdown=0.02, risk_per_trade=0.05, max_position_pct=1.0))
    df = synthetic_ohlcv(3000, seed=3)
    res = run_backtest(df, create_strategy("sma_crossover", {"fast": 5, "slow": 20}), cfg)
    assert res.stats["halted"]
    assert res.stats["max_drawdown"] > -0.2


def test_grid_search():
    df = synthetic_ohlcv(1500)
    out = grid_search(df, Config(), "sma_crossover", parse_grid("fast=10,20 slow=50,60"))
    assert len(out) == 4
    assert parse_grid("a=1,2.5 b=true") == {"a": [1, 2.5], "b": [True]}
