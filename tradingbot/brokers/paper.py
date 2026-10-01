"""Simulated broker with fees and slippage. Used for backtests and paper trading."""
from __future__ import annotations

import itertools
from typing import Callable

from .base import Broker, Fill


class PaperBroker(Broker):
    def __init__(
        self,
        symbol: str,
        initial_cash: float,
        fee_rate: float = 0.001,
        slippage: float = 0.0005,
        price_source: Callable[[], float] | None = None,
        base_balance: float = 0.0,
        account: dict | None = None,
    ):
        super().__init__(symbol)
        self._account = account if account is not None else {"cash": float(initial_cash)}
        self.position = float(base_balance)
        self.fee_rate = fee_rate
        self.slippage = slippage
        self._price_source = price_source
        self._price: float | None = None
        self._ids = itertools.count(1)

    @property
    def cash(self) -> float:
        return self._account["cash"]

    @cash.setter
    def cash(self, value: float) -> None:
        self._account["cash"] = float(value)

    def sibling(self, symbol: str, price_source: Callable[[], float] | None = None) -> "PaperBroker":
        """Another broker on the same cash account, for holding a second stock."""
        return PaperBroker(symbol, 0.0, self.fee_rate, self.slippage, price_source, account=self._account)

    def set_price(self, price: float) -> None:
        self._price = float(price)

    def last_price(self) -> float:
        if self._price_source is not None:
            return float(self._price_source())
        if self._price is None:
            raise RuntimeError("PaperBroker has no price yet")
        return self._price

    def balances(self) -> tuple[float, float]:
        return self.cash, self.position

    def market_buy(self, qty: float, price: float | None = None) -> Fill:
        px = (price if price is not None else self.last_price()) * (1 + self.slippage)
        # Shrink the order if cash can't cover notional + fee.
        max_qty = self.cash / (px * (1 + self.fee_rate))
        qty = min(qty, max_qty)
        if qty <= 0:
            raise ValueError("insufficient cash")
        notional = qty * px
        fee = notional * self.fee_rate
        self.cash -= notional + fee
        self.position += qty
        return Fill("buy", qty, px, fee, f"paper-{next(self._ids)}")

    def market_sell(self, qty: float, price: float | None = None) -> Fill:
        qty = min(qty, self.position)
        if qty <= 0:
            raise ValueError("no position to sell")
        px = (price if price is not None else self.last_price()) * (1 - self.slippage)
        notional = qty * px
        fee = notional * self.fee_rate
        self.cash += notional - fee
        self.position -= qty
        if self.position < 1e-12:
            self.position = 0.0
        return Fill("sell", qty, px, fee, f"paper-{next(self._ids)}")

    # persistence for paper trading sessions
    def to_dict(self) -> dict:
        return {"cash": self.cash, "position": self.position}

    def load_dict(self, d: dict) -> None:
        self.cash = float(d["cash"])
        self.position = float(d["position"])
