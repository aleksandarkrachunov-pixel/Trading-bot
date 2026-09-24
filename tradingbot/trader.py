"""Order/position logic shared by the backtester and the live engine."""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime

from .brokers.base import Broker, Fill
from .risk import RiskManager

log = logging.getLogger(__name__)


@dataclass
class PositionState:
    qty: float = 0.0
    entry_price: float = 0.0
    entry_time: str = ""
    entry_fee: float = 0.0
    stop: float = 0.0
    take_profit: float | None = None
    # After a stop-out, wait for the strategy to go flat before re-entering.
    wait_for_reset: bool = False

    @property
    def is_open(self) -> bool:
        return self.qty > 0


@dataclass
class Trade:
    entry_time: str
    exit_time: str
    entry_price: float
    exit_price: float
    qty: float
    pnl: float           # net of fees, quote currency
    return_pct: float
    exit_reason: str


@dataclass
class Trader:
    broker: Broker
    risk: RiskManager
    position: PositionState = field(default_factory=PositionState)
    trades: list[Trade] = field(default_factory=list)

    def equity(self, price: float) -> float:
        """Equity attributable to the bot: free quote + the position it opened."""
        quote, _ = self.broker.balances()
        return quote + self.position.qty * price

    # ---- actions ------------------------------------------------------------
    def rebalance(self, target: int, atr_value: float, price: float, now: datetime) -> Fill | None:
        """Move towards the strategy's target position, respecting risk rules."""
        pos = self.position
        if self.risk.state.halted:
            return self.exit(price, now, "kill_switch") if pos.is_open else None

        if target == 0:
            pos.wait_for_reset = False
            return self.exit(price, now, "signal") if pos.is_open else None

        if pos.is_open or pos.wait_for_reset:
            return None

        equity = self.equity(price)
        ok, reason = self.risk.can_open(equity)
        if not ok:
            log.info("Entry blocked: %s", reason)
            return None
        qty = self.risk.position_size(equity, price, atr_value)
        if qty <= 0:
            log.info("Entry skipped: position size too small")
            return None
        return self.enter(qty, atr_value, now)

    def enter(self, qty: float, atr_value: float, now: datetime) -> Fill:
        fill = self.broker.market_buy(qty)
        pos = self.position
        pos.qty = fill.qty
        pos.entry_price = fill.price
        pos.entry_time = str(now)
        pos.entry_fee = fill.fee
        pos.stop = self.risk.initial_stop(fill.price, atr_value)
        pos.take_profit = self.risk.take_profit(fill.price, atr_value)
        log.info(
            "BUY %.6f @ %.4f (fee %.4f) stop=%.4f tp=%s",
            fill.qty, fill.price, fill.fee, pos.stop,
            f"{pos.take_profit:.4f}" if pos.take_profit else "-",
        )
        return fill

    def exit(self, price: float, now: datetime, reason: str, fill_price: float | None = None) -> Fill:
        pos = self.position
        if fill_price is not None and hasattr(self.broker, "set_price"):
            self.broker.set_price(fill_price)  # paper broker: fill at the stop/TP level
        fill = self.broker.market_sell(pos.qty)
        cost = pos.qty * pos.entry_price + pos.entry_fee
        proceeds = fill.qty * fill.price - fill.fee
        # If only part was sold (e.g. exchange rounding), book PnL pro rata.
        pnl = proceeds - cost * (fill.qty / pos.qty)
        trade = Trade(
            entry_time=pos.entry_time, exit_time=str(now), entry_price=pos.entry_price,
            exit_price=fill.price, qty=fill.qty, pnl=pnl,
            return_pct=pnl / (cost * fill.qty / pos.qty), exit_reason=reason,
        )
        self.trades.append(trade)
        log.info("SELL %.6f @ %.4f reason=%s pnl=%.2f (%.2f%%)",
                 fill.qty, fill.price, reason, pnl, trade.return_pct * 100)
        self.position = PositionState(wait_for_reset=reason in ("stop_loss", "take_profit"))
        return fill

    def check_exits(self, open_: float, high: float, low: float, now: datetime) -> Fill | None:
        """Stop-loss / take-profit check. For live use pass the last price for all three."""
        pos = self.position
        if not pos.is_open:
            return None
        # Stop is checked first (conservative when both are touched in one bar).
        if low <= pos.stop:
            return self.exit(low, now, "stop_loss", fill_price=min(open_, pos.stop))
        if pos.take_profit is not None and high >= pos.take_profit:
            return self.exit(high, now, "take_profit", fill_price=max(open_, pos.take_profit))
        return None

    def trail(self, price: float, atr_value: float) -> None:
        if self.position.is_open:
            self.position.stop = self.risk.trail_stop(self.position.stop, price, atr_value)

    # ---- persistence ----------------------------------------------------------
    def to_dict(self) -> dict:
        return {"position": asdict(self.position), "trades": [asdict(t) for t in self.trades]}

    def load_dict(self, d: dict) -> None:
        self.position = PositionState(**d.get("position", {}))
        self.trades = [Trade(**t) for t in d.get("trades", [])]
