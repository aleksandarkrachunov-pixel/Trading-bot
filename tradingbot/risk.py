"""Risk management shared by the backtester and the live engine.

- Volatility-based position sizing: lose at most `risk_per_trade` of equity if the stop is hit.
- ATR stop-loss (optionally trailing) and optional take-profit.
- Circuit breakers: max drawdown from peak halts trading until a manual reset;
  the daily loss limit blocks new entries for the rest of the UTC day.
"""
from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass
from datetime import datetime

from .config import RiskConfig

log = logging.getLogger(__name__)


@dataclass
class RiskState:
    peak_equity: float = 0.0
    day: str = ""
    day_start_equity: float = 0.0
    halted: bool = False
    halt_reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "RiskState":
        return cls(**d)


class RiskManager:
    def __init__(self, cfg: RiskConfig, state: RiskState | None = None):
        self.cfg = cfg
        self.state = state or RiskState()

    # ---- sizing & exits ---------------------------------------------------
    def position_size(self, equity: float, price: float, atr_value: float) -> float:
        """Base-currency quantity to buy. Returns 0 if the trade shouldn't be taken."""
        if equity <= 0 or price <= 0 or not math.isfinite(atr_value) or atr_value <= 0:
            return 0.0
        stop_distance = atr_value * self.cfg.stop_atr_multiple
        qty_by_risk = (equity * self.cfg.risk_per_trade) / stop_distance
        qty_by_cap = (equity * self.cfg.max_position_pct) / price
        qty = min(qty_by_risk, qty_by_cap)
        if qty * price < self.cfg.min_order_value:
            return 0.0
        return qty

    def initial_stop(self, entry_price: float, atr_value: float) -> float:
        return entry_price - atr_value * self.cfg.stop_atr_multiple

    def take_profit(self, entry_price: float, atr_value: float) -> float | None:
        m = self.cfg.take_profit_atr_multiple
        return entry_price + atr_value * m if m else None

    def trail_stop(self, current_stop: float, price: float, atr_value: float) -> float:
        if not self.cfg.trailing_stop or not math.isfinite(atr_value):
            return current_stop
        return max(current_stop, price - atr_value * self.cfg.stop_atr_multiple)

    # ---- circuit breakers -------------------------------------------------
    def update_equity(self, equity: float, now: datetime) -> None:
        s = self.state
        day = now.strftime("%Y-%m-%d")
        if s.day != day:
            s.day = day
            s.day_start_equity = equity
        s.peak_equity = max(s.peak_equity, equity)
        if not s.halted and s.peak_equity > 0:
            drawdown = 1 - equity / s.peak_equity
            if drawdown >= self.cfg.max_drawdown:
                s.halted = True
                s.halt_reason = (
                    f"max drawdown {drawdown:.1%} >= {self.cfg.max_drawdown:.0%} "
                    f"(peak {s.peak_equity:.2f}, now {equity:.2f})"
                )
                log.critical("KILL SWITCH: %s", s.halt_reason)

    def daily_loss_hit(self, equity: float) -> bool:
        s = self.state
        if s.day_start_equity <= 0:
            return False
        return 1 - equity / s.day_start_equity >= self.cfg.daily_loss_limit

    def can_open(self, equity: float) -> tuple[bool, str]:
        if self.state.halted:
            return False, f"halted: {self.state.halt_reason}"
        if self.daily_loss_hit(equity):
            return False, "daily loss limit reached"
        return True, ""

    def reset_halt(self) -> None:
        self.state.halted = False
        self.state.halt_reason = ""
        self.state.peak_equity = 0.0
