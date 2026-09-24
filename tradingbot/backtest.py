"""Bar-by-bar backtester.

Signals are computed from closed bar i and executed at the open of bar i+1,
so there is no look-ahead. Stops/take-profits are checked against each bar's
high/low. Execution uses the same Trader + RiskManager as live trading.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .brokers.paper import PaperBroker
from .config import Config
from .indicators import atr
from .risk import RiskManager
from .strategies import Strategy
from .trader import Trade, Trader


@dataclass
class BacktestResult:
    equity: pd.Series
    trades: list[Trade]
    stats: dict

    def trades_frame(self) -> pd.DataFrame:
        return pd.DataFrame([t.__dict__ for t in self.trades])


def periods_per_year(index: pd.DatetimeIndex) -> float:
    if len(index) < 2:
        return 1.0
    step = (index[-1] - index[0]).total_seconds() / (len(index) - 1)
    return 365 * 24 * 3600 / step


def compute_stats(equity: pd.Series, trades: list[Trade], prices: pd.Series, initial_cash: float) -> dict:
    rets = equity.pct_change().dropna()
    ppy = periods_per_year(equity.index)
    years = max((equity.index[-1] - equity.index[0]).total_seconds() / (365 * 24 * 3600), 1e-9)
    final = float(equity.iloc[-1])
    total_return = final / initial_cash - 1
    drawdown = equity / equity.cummax() - 1
    std = rets.std()
    downside = rets[rets < 0].std()
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in losses)
    return {
        "start": str(equity.index[0]),
        "end": str(equity.index[-1]),
        "initial_equity": initial_cash,
        "final_equity": final,
        "total_return": total_return,
        "cagr": (final / initial_cash) ** (1 / years) - 1 if final > 0 else -1.0,
        "sharpe": float(rets.mean() / std * math.sqrt(ppy)) if std > 0 else 0.0,
        "sortino": float(rets.mean() / downside * math.sqrt(ppy)) if downside and downside > 0 else 0.0,
        "max_drawdown": float(drawdown.min()),
        "num_trades": len(trades),
        "win_rate": len(wins) / len(trades) if trades else 0.0,
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else float("inf") if gross_win > 0 else 0.0,
        "avg_trade_return": float(np.mean([t.return_pct for t in trades])) if trades else 0.0,
        "exposure": None,  # filled in by run_backtest
        "buy_and_hold_return": float(prices.iloc[-1] / prices.iloc[0] - 1),
    }


def run_backtest(df: pd.DataFrame, strategy: Strategy, cfg: Config) -> BacktestResult:
    if len(df) <= strategy.warmup_bars:
        raise ValueError(f"Need more than {strategy.warmup_bars} bars, got {len(df)}")

    bt = cfg.backtest
    broker = PaperBroker(cfg.exchange.symbol, bt.initial_cash, bt.fee_rate, bt.slippage)
    risk = RiskManager(cfg.risk)
    trader = Trader(broker, risk)

    targets = strategy.target_positions(df).fillna(0).astype(int).to_numpy()
    atrs = atr(df, cfg.risk.atr_period).to_numpy()
    opens, highs, lows, closes = (df[c].to_numpy() for c in ("open", "high", "low", "close"))
    index = df.index

    equity = np.empty(len(df))
    in_market = np.zeros(len(df), dtype=bool)
    equity[0] = bt.initial_cash
    risk.update_equity(bt.initial_cash, index[0].to_pydatetime())

    for i in range(1, len(df)):
        now = index[i].to_pydatetime()
        # 1) act at the open on the previous bar's signal
        broker.set_price(opens[i])
        target = targets[i - 1] if i - 1 >= strategy.warmup_bars else 0
        trader.rebalance(target, atrs[i - 1], opens[i], now)
        # 2) intrabar stop-loss / take-profit
        trader.check_exits(opens[i], highs[i], lows[i], now)
        # 3) end of bar bookkeeping
        broker.set_price(closes[i])
        trader.trail(closes[i], atrs[i])
        equity[i] = trader.equity(closes[i])
        in_market[i] = trader.position.is_open
        risk.update_equity(equity[i], now)

    # Close any open position at the final close so stats reflect realised PnL.
    if trader.position.is_open:
        trader.exit(closes[-1], index[-1].to_pydatetime(), "end_of_data")
        equity[-1] = trader.equity(closes[-1])

    equity_series = pd.Series(equity, index=index, name="equity")
    stats = compute_stats(equity_series, trader.trades, df["close"], bt.initial_cash)
    stats["exposure"] = float(in_market.mean())
    stats["halted"] = risk.state.halted
    return BacktestResult(equity_series, trader.trades, stats)


def format_stats(stats: dict) -> str:
    pct = {"total_return", "cagr", "max_drawdown", "win_rate", "avg_trade_return", "exposure", "buy_and_hold_return"}
    lines = []
    for k, v in stats.items():
        if k in pct and v is not None:
            s = f"{v:.2%}"
        elif isinstance(v, float):
            s = f"{v:,.2f}"
        else:
            s = str(v)
        lines.append(f"  {k:<22}{s}")
    return "\n".join(lines)
