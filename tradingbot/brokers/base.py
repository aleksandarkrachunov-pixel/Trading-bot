from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class Fill:
    side: str        # "buy" | "sell"
    qty: float       # base currency
    price: float     # average fill price
    fee: float       # in quote currency
    order_id: str = ""


class Broker(ABC):
    """Spot-only, long-only broker interface."""

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.base, self.quote = symbol.split("/")

    @abstractmethod
    def balances(self) -> tuple[float, float]:
        """Return (free quote currency, free base currency)."""

    @abstractmethod
    def last_price(self) -> float: ...

    @abstractmethod
    def market_buy(self, qty: float) -> Fill: ...

    @abstractmethod
    def market_sell(self, qty: float) -> Fill: ...

    def equity(self, price: float | None = None) -> float:
        quote, base = self.balances()
        return quote + base * (price if price is not None else self.last_price())
