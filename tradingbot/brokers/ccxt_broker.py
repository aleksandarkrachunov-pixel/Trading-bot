"""Live broker backed by any ccxt-supported exchange (spot market orders)."""
from __future__ import annotations

import logging
import time

import ccxt

from .base import Broker, Fill

log = logging.getLogger(__name__)


def make_exchange(name: str, api_key=None, secret=None, password=None, sandbox=False):
    try:
        cls = getattr(ccxt, name)
    except AttributeError:
        raise ValueError(f"Unknown ccxt exchange '{name}'") from None
    params = {"enableRateLimit": True, "options": {"defaultType": "spot"}}
    if api_key:
        params.update(apiKey=api_key, secret=secret)
    if password:
        params["password"] = password
    exchange = cls(params)
    if sandbox:
        exchange.set_sandbox_mode(True)
    return exchange


def with_retries(fn, *args, attempts: int = 4, **kwargs):
    """Retry transient network errors with exponential backoff."""
    delay = 2.0
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except (ccxt.NetworkError, ccxt.ExchangeNotAvailable, ccxt.RequestTimeout) as e:
            if i == attempts - 1:
                raise
            log.warning("Transient error (%s), retrying in %.0fs", e, delay)
            time.sleep(delay)
            delay *= 2


class CcxtBroker(Broker):
    def __init__(self, exchange, symbol: str):
        super().__init__(symbol)
        self.exchange = exchange
        with_retries(self.exchange.load_markets)
        if symbol not in self.exchange.markets:
            raise ValueError(f"{symbol} not listed on {self.exchange.id}")
        self.market = self.exchange.markets[symbol]

    def balances(self) -> tuple[float, float]:
        bal = with_retries(self.exchange.fetch_balance)
        free = bal.get("free", {})
        return float(free.get(self.quote) or 0.0), float(free.get(self.base) or 0.0)

    def last_price(self) -> float:
        ticker = with_retries(self.exchange.fetch_ticker, self.symbol)
        return float(ticker["last"])

    def _amount(self, qty: float) -> float:
        amount = float(self.exchange.amount_to_precision(self.symbol, qty))
        min_amount = (self.market.get("limits", {}).get("amount", {}) or {}).get("min")
        if min_amount and amount < min_amount:
            raise ValueError(f"order qty {amount} below exchange minimum {min_amount}")
        return amount

    def _to_fill(self, order: dict, side: str) -> Fill:
        # Some exchanges return a sparse response; fetch the final state.
        if not order.get("filled") and order.get("id"):
            try:
                order = with_retries(self.exchange.fetch_order, order["id"], self.symbol)
            except ccxt.BaseError as e:  # not all exchanges support fetch_order
                log.warning("fetch_order failed: %s", e)
        filled = float(order.get("filled") or order.get("amount") or 0.0)
        price = float(order.get("average") or order.get("price") or self.last_price())
        fee = 0.0
        for f in order.get("fees") or ([order["fee"]] if order.get("fee") else []):
            if f and f.get("cost"):
                cost = float(f["cost"])
                fee += cost * price if f.get("currency") == self.base else cost
        return Fill(side, filled, price, fee, str(order.get("id", "")))

    def market_buy(self, qty: float) -> Fill:
        amount = self._amount(qty)
        log.info("LIVE market BUY %s %s", amount, self.symbol)
        order = self.exchange.create_order(self.symbol, "market", "buy", amount)
        return self._to_fill(order, "buy")

    def market_sell(self, qty: float) -> Fill:
        amount = self._amount(qty)
        log.info("LIVE market SELL %s %s", amount, self.symbol)
        order = self.exchange.create_order(self.symbol, "market", "sell", amount)
        return self._to_fill(order, "sell")
