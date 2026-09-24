"""Trading 212 broker (Public API v0, Invest / Stocks ISA accounts).

- Auth: HTTP Basic with API key + secret (generated in the Trading 212 app).
- demo = https://demo.trading212.com (practice money), live = https://live.trading212.com
- Market orders only; a negative quantity sells.
- The API has no price data: prices come from Yahoo Finance (see yahoo.py).
- Balances are converted into the instrument's currency so the risk maths
  (price * qty vs. equity) stays in one currency.
"""
from __future__ import annotations

import copy
import logging
import math
import re
import threading
import time

import requests

from ..yahoo import YahooData
from .base import Broker, Fill

log = logging.getLogger(__name__)

BASE_URLS = {"demo": "https://demo.trading212.com", "live": "https://live.trading212.com"}

# Minimum seconds between calls, per endpoint (from the API docs).
RATE_LIMITS = {
    "/api/v0/equity/account/summary": 5.0,
    "/api/v0/equity/positions": 1.0,
    "/api/v0/equity/orders/market": 1.2,
    "/api/v0/equity/orders/{id}": 1.0,
    "/api/v0/equity/history/orders": 10.0,
    "/api/v0/equity/metadata/instruments": 50.0,
}


class Trading212Error(RuntimeError):
    pass


class Trading212Client:
    def __init__(self, api_key: str, api_secret: str, environment: str = "demo",
                 session: requests.Session | None = None, sleep=time.sleep):
        if environment not in BASE_URLS:
            raise ValueError("environment must be 'demo' or 'live'")
        self.environment = environment
        self.base_url = BASE_URLS[environment]
        self.session = session or requests.Session()
        self.session.auth = (api_key, api_secret)
        self._last_call: dict[str, float] = {}
        self._lock = threading.Lock()
        self._sleep = sleep

    def request(self, method: str, path: str, *, rate_key: str | None = None,
                params: dict | None = None, json: dict | None = None, allow_404: bool = False):
        key = rate_key or path
        for attempt in range(5):
            with self._lock:
                wait = RATE_LIMITS.get(key, 1.0) - (time.monotonic() - self._last_call.get(key, -1e9))
                if wait > 0:
                    self._sleep(wait)
                self._last_call[key] = time.monotonic()
            try:
                r = self.session.request(method, self.base_url + path, params=params, json=json, timeout=20)
            except (requests.ConnectionError, requests.Timeout) as e:
                if method != "GET":  # order placement is not idempotent: never blindly retry
                    raise Trading212Error(f"{method} {path} failed: {e}") from e
                log.warning("Trading 212 %s failed (%s), retrying", path, e)
                self._sleep(2 ** attempt)
                continue
            if r.status_code == 404 and allow_404:
                return None
            if r.status_code == 429:
                reset = r.headers.get("x-ratelimit-reset")
                delay = max(1.0, float(reset) - time.time()) if reset else 5.0
                log.warning("Trading 212 rate limit on %s, waiting %.0fs", path, delay)
                self._sleep(min(delay, 60))
                continue
            if r.status_code >= 500 and method == "GET":
                self._sleep(2 ** attempt)
                continue
            if r.status_code >= 400:
                hints = {401: "bad API key/secret", 403: "API key is missing a permission (scope)"}
                raise Trading212Error(f"{method} {path} -> HTTP {r.status_code} "
                                      f"({hints.get(r.status_code, 'error')}): {r.text[:300]}")
            return r.json() if r.content else None
        raise Trading212Error(f"{method} {path}: gave up after retries")

    # ---- endpoints ------------------------------------------------------------------
    def account_summary(self) -> dict:
        return self.request("GET", "/api/v0/equity/account/summary")

    def positions(self, ticker: str | None = None) -> list[dict]:
        return self.request("GET", "/api/v0/equity/positions", params={"ticker": ticker} if ticker else None) or []

    def instruments(self) -> list[dict]:
        return self.request("GET", "/api/v0/equity/metadata/instruments")

    def place_market_order(self, ticker: str, quantity: float, extended_hours: bool = False) -> dict:
        return self.request("POST", "/api/v0/equity/orders/market",
                            json={"ticker": ticker, "quantity": quantity, "extendedHours": extended_hours})

    def get_order(self, order_id: int) -> dict | None:
        """Pending order, or None once it is no longer pending (filled/cancelled)."""
        return self.request("GET", f"/api/v0/equity/orders/{order_id}",
                            rate_key="/api/v0/equity/orders/{id}", allow_404=True)

    def cancel_order(self, order_id: int) -> None:
        self.request("DELETE", f"/api/v0/equity/orders/{order_id}",
                     rate_key="/api/v0/equity/orders/{id}", allow_404=True)

    def historical_orders(self, ticker: str | None = None, limit: int = 20) -> list[dict]:
        params = {"limit": limit, **({"ticker": ticker} if ticker else {})}
        data = self.request("GET", "/api/v0/equity/history/orders", params=params) or {}
        return data.get("items", [])


class Trading212Broker(Broker):
    def __init__(self, client: Trading212Client, ticker: str, data: YahooData,
                 extended_hours: bool = False, quantity_decimals: int = 2, order_timeout: float = 60,
                 sleep=time.sleep):
        super().__init__(ticker)
        self.client = client
        self.data = data
        self.extended_hours = extended_hours
        self.quantity_decimals = quantity_decimals
        self.order_timeout = order_timeout
        self._sleep = sleep
        self._cache: dict = {"summary": None}  # shared with sibling brokers

        summary = self.account_summary()
        self.account_currency = summary.get("currency", "")
        self.set_symbol(ticker)

    def set_symbol(self, ticker: str) -> None:
        instrument = self._find_instrument(ticker)
        self.map_price_symbol(ticker)
        super().set_symbol(ticker)
        self.instrument = instrument
        self.instrument_currency = (instrument.get("currencyCode") or self.data.currency(ticker)
                                    or self.account_currency)
        self.quote = self.instrument_currency
        log.info("Trading 212 %s account (%s): %s [%s], instrument currency %s",
                 self.client.environment.upper(), self.account_currency, ticker,
                 instrument.get("name", "?"), self.instrument_currency)

    def sibling(self, ticker: str) -> "Trading212Broker":
        """Another broker on the same account for a second stock (shares API client and caches)."""
        other = copy.copy(self)
        other.set_symbol(ticker)
        return other

    def instruments(self) -> dict[str, dict]:
        """All tradable instruments by ticker (fetched once: the endpoint allows 1 call / 50s)."""
        if "instruments" not in self._cache:
            self._cache["instruments"] = {i["ticker"]: i for i in self.client.instruments() if i.get("ticker")}
        return self._cache["instruments"]

    def _find_instrument(self, ticker: str) -> dict:
        inst = self.instruments().get(ticker)
        if inst is None:
            raise ValueError(f"Ticker '{ticker}' not found on Trading 212. Use the exact ticker, e.g. AAPL_US_EQ")
        return inst

    def tradable(self, tickers: list[str]) -> list[str]:
        """The subset of `tickers` on Trading 212 in the current instrument's currency.

        Keeping one currency means equity, the drawdown kill switch and position sizing
        stay comparable when the scanner switches between stocks.
        """
        insts = self.instruments()
        by_symbol = {i.get("shortName"): t for t, i in insts.items() if t.endswith("_US_EQ")}
        keep, dropped = [], []
        for t in tickers:
            if t not in insts:  # e.g. META_US_EQ -> FB_US_EQ (T212 kept the old ticker)
                t = by_symbol.get(t.removesuffix("_US_EQ"), t)
            inst = insts.get(t)
            ok = inst is not None and (inst.get("currencyCode") or self.instrument_currency) == self.instrument_currency
            if ok and t not in keep:
                keep.append(t)
                self.map_price_symbol(t)
            elif not ok:
                dropped.append(t)
        if dropped:
            log.warning("Scanner: not on Trading 212 or not in %s, skipped: %s",
                        self.instrument_currency, ", ".join(dropped))
        return keep

    def map_price_symbol(self, ticker: str) -> None:
        """Price US stocks by their market symbol (T212 `shortName`), not the T212 ticker.

        The ticker can be stale after a rename: FB_US_EQ is Meta (META), and CTRA_US_EQ
        is Alpha Metallurgical (AMR), not Coterra.
        """
        symbol_map = getattr(self.data, "symbol_map", None)
        short = (self.instruments().get(ticker) or {}).get("shortName") or ""
        if (symbol_map is not None and ticker not in symbol_map and ticker.endswith("_US_EQ")
                and re.fullmatch(r"[A-Z][A-Z0-9.]*", short)):
            symbol_map[ticker] = short.replace(".", "-")

    # ---- balances & prices ------------------------------------------------------------
    def account_summary(self, max_age: float = 5.0) -> dict:
        """Cached account summary (the endpoint allows 1 request / 5s)."""
        cached = self._cache["summary"]
        if cached is None or time.monotonic() - cached[0] > max_age:
            cached = self._cache["summary"] = (time.monotonic(), self.client.account_summary())
        return cached[1]

    def _fx(self) -> float:
        """Instrument-currency units per 1 account-currency unit."""
        return self.data.fx_rate(self.account_currency, self.instrument_currency)

    def position_qty(self) -> float:
        for p in self.client.positions(self.symbol):
            if (p.get("instrument") or {}).get("ticker", p.get("ticker")) == self.symbol:
                return float(p.get("quantityAvailableForTrading", p.get("quantity")) or 0.0)
        return 0.0

    def held_positions(self) -> list[dict]:
        """Every position in the account: [{ticker, qty, avg_price (instrument currency), opened}]."""
        out = []
        for p in self.client.positions():
            ticker = (p.get("instrument") or {}).get("ticker", p.get("ticker"))
            qty = float(p.get("quantityAvailableForTrading", p.get("quantity")) or 0.0)
            avg = p.get("averagePricePaid", p.get("averagePrice"))
            if ticker and qty > 0 and avg:
                out.append({"ticker": ticker, "qty": qty, "avg_price": float(avg),
                            "opened": p.get("createdAt") or p.get("initialFillDate") or ""})
        return out

    def balances(self) -> tuple[float, float]:
        cash = float((self.account_summary().get("cash") or {}).get("availableToTrade") or 0.0)
        return cash * self._fx(), self.position_qty()

    def last_price(self) -> float:
        return self.data.last_price(self.symbol)

    # ---- orders -----------------------------------------------------------------------
    def _round_down(self, qty: float) -> float:
        f = 10 ** self.quantity_decimals
        return math.floor(qty * f + 1e-9) / f

    def market_buy(self, qty: float) -> Fill:
        return self._execute(self._round_down(qty), "buy")

    def market_sell(self, qty: float) -> Fill:
        qty = self._round_down(min(qty, self.position_qty()))
        return self._execute(qty, "sell")

    def _execute(self, qty: float, side: str) -> Fill:
        if qty <= 0:
            raise ValueError(f"order quantity rounds to 0 (quantity_decimals={self.quantity_decimals})")
        signed = qty if side == "buy" else -qty
        before = self.position_qty()
        log.info("Trading 212 %s market %s %s x %s", self.client.environment.upper(), side.upper(), qty, self.symbol)
        self._cache["summary"] = None  # cash changes after this order
        order = self.client.place_market_order(self.symbol, signed, self.extended_hours)
        order_id = order["id"]

        deadline = time.monotonic() + self.order_timeout
        while True:
            pending = self.client.get_order(order_id)
            if pending is None or pending.get("status") == "FILLED":
                break
            if pending.get("status") in ("CANCELLED", "REJECTED"):
                raise Trading212Error(f"order {order_id} {pending['status']}")
            if time.monotonic() > deadline:
                log.warning("Order %s not filled after %ss, cancelling", order_id, self.order_timeout)
                self.client.cancel_order(order_id)
                self._sleep(2)
                if self.client.get_order(order_id) is not None:
                    raise Trading212Error(f"order {order_id} still pending after cancel request")
                break
            self._sleep(1)

        return self._resolve_fill(order_id, side, qty, before)

    def _resolve_fill(self, order_id: int, side: str, requested: float, before: float) -> Fill:
        """Find the executed quantity/price from order history, falling back to the position delta."""
        for attempt in range(3):
            for item in self.client.historical_orders(self.symbol):
                o = item.get("order") or {}
                if o.get("id") != order_id:
                    continue
                if o.get("status") in ("CANCELLED", "REJECTED") and not o.get("filledQuantity"):
                    raise Trading212Error(f"order {order_id} {o['status']}")
                fill = item.get("fill") or {}
                qty = abs(float(fill.get("quantity") or o.get("filledQuantity") or 0))
                price = fill.get("price")
                if price is None and o.get("filledValue") and qty:
                    price = float(o["filledValue"]) / qty
                if qty > 0 and price:
                    return Fill(side, qty, float(price), 0.0, str(order_id))
            self._sleep(2)  # history can lag a moment behind the fill

        delta = abs(self.position_qty() - before)
        if delta <= 0:
            raise Trading212Error(f"order {order_id}: no fill found in history and position unchanged")
        price = self.last_price()
        log.warning("Order %s not in history yet; using position delta %.6f @ last price %.4f", order_id, delta, price)
        return Fill(side, min(delta, requested), price, 0.0, str(order_id))
