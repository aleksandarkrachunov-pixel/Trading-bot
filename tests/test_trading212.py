import pytest

from fakes import FakeResponse, FakeSession
from tradingbot.brokers.trading212 import Trading212Broker, Trading212Client, Trading212Error

TICKER = "AAPL_US_EQ"


class FakeData:
    def __init__(self, price=200.0, fx=1.25):
        self.price, self.fx = price, fx

    def last_price(self, symbol):
        return self.price

    def currency(self, symbol):
        return "USD"

    def fx_rate(self, a, b):
        return 1.0 if a == b else self.fx


class FakeT212:
    """Simulates the Trading 212 demo API: orders fill after `fill_after` status polls."""

    def __init__(self, cash=1000.0, fill_after=1, reject=False, never_fill=False):
        self.cash = cash
        self.qty = 0.0
        self.orders = {}
        self.history = []
        self.next_id = 100
        self.fill_after = fill_after
        self.reject = reject
        self.never_fill = never_fill
        self.cancelled = []

    def __call__(self, method, url, params, json):
        path = url.split(".com", 1)[1]
        if path == "/api/v0/equity/account/summary":
            return FakeResponse(200, {"id": 1, "currency": "GBP", "totalValue": self.cash,
                                      "cash": {"availableToTrade": self.cash}})
        if path == "/api/v0/equity/metadata/instruments":
            return FakeResponse(200, [{"ticker": TICKER, "name": "Apple", "currencyCode": "USD", "type": "STOCK"}])
        if path == "/api/v0/equity/positions":
            if self.qty <= 0:
                return FakeResponse(200, [])
            return FakeResponse(200, [{"instrument": {"ticker": TICKER}, "quantity": self.qty,
                                       "quantityAvailableForTrading": self.qty}])
        if path == "/api/v0/equity/orders/market" and method == "POST":
            oid = self.next_id
            self.next_id += 1
            self.orders[oid] = {"id": oid, "status": "NEW", "quantity": json["quantity"], "polls": 0}
            return FakeResponse(200, {"id": oid, "status": "NEW", "ticker": json["ticker"],
                                      "quantity": json["quantity"]})
        if path.startswith("/api/v0/equity/orders/"):
            oid = int(path.rsplit("/", 1)[1])
            o = self.orders.get(oid)
            if method == "DELETE":
                self.cancelled.append(oid)
                if o and o["status"] == "NEW":
                    o["status"] = "CANCELLED"
                    self.history.append({"order": {"id": oid, "status": "CANCELLED", "filledQuantity": 0}})
                    del self.orders[oid]
                return FakeResponse(200, None)
            if o is None:
                return FakeResponse(404, {"message": "not found"})
            if self.reject:
                return FakeResponse(200, {**o, "status": "REJECTED"})
            o["polls"] += 1
            if o["polls"] >= self.fill_after and not self.never_fill:
                self._fill(oid)
                return FakeResponse(404, {"message": "not found"})
            return FakeResponse(200, {k: v for k, v in o.items() if k != "polls"})
        if path == "/api/v0/equity/history/orders":
            return FakeResponse(200, {"items": list(reversed(self.history)), "nextPagePath": None})
        return FakeResponse(500, {"error": path})

    def _fill(self, oid):
        o = self.orders.pop(oid)
        q = o["quantity"]
        price = 200.0
        self.qty += q
        self.cash -= q * price / 1.25
        self.history.append({"order": {"id": oid, "status": "FILLED", "filledQuantity": abs(q)},
                             "fill": {"price": price, "quantity": q}})


def make(api=None, **kw):
    api = api or FakeT212()
    session = FakeSession(api)
    client = Trading212Client("key", "secret", "demo", session=session, sleep=lambda s: None)
    broker = Trading212Broker(client, TICKER, FakeData(), sleep=lambda s: None, **kw)
    return broker, api, session


def test_auth_and_demo_url():
    broker, api, session = make()
    assert session.auth == ("key", "secret")
    assert session.calls[0][1].startswith("https://demo.trading212.com/api/v0/")
    assert broker.account_currency == "GBP" and broker.instrument_currency == "USD"


def test_balances_converted_to_instrument_currency():
    broker, api, _ = make()
    cash, qty = broker.balances()
    assert cash == pytest.approx(1000 * 1.25)
    assert qty == 0


def test_buy_then_sell_round_trip():
    broker, api, session = make()
    buy = broker.market_buy(1.23456)
    assert buy.qty == pytest.approx(1.23)  # rounded down to 2 decimals
    assert buy.price == 200.0
    posted = [c for c in session.calls if c[0] == "POST"]
    assert posted[0][3] == {"ticker": TICKER, "quantity": 1.23, "extendedHours": False}

    sell = broker.market_sell(buy.qty)
    assert sell.side == "sell" and sell.qty == pytest.approx(1.23)
    posted = [c for c in session.calls if c[0] == "POST"]
    assert posted[1][3]["quantity"] == pytest.approx(-1.23)  # negative quantity = sell
    assert api.qty == pytest.approx(0)


def test_sell_clamped_to_position():
    broker, api, _ = make()
    broker.market_buy(1.0)
    sell = broker.market_sell(5.0)
    assert sell.qty == pytest.approx(1.0)


def test_rejected_order_raises():
    broker, _, _ = make(FakeT212(reject=True))
    with pytest.raises(Trading212Error, match="REJECTED"):
        broker.market_buy(1)


def test_unfilled_order_is_cancelled():
    api = FakeT212(never_fill=True)
    broker, api, _ = make(api, order_timeout=0)
    with pytest.raises(Trading212Error):
        broker.market_buy(1)
    assert api.cancelled


def test_unknown_ticker():
    api = FakeT212()
    session = FakeSession(api)
    client = Trading212Client("k", "s", "demo", session=session, sleep=lambda s: None)
    with pytest.raises(ValueError, match="not found"):
        Trading212Broker(client, "NOPE_US_EQ", FakeData(), sleep=lambda s: None)


def test_rate_limit_retry():
    responses = [FakeResponse(429, {}, {"x-ratelimit-reset": "0"}),
                 FakeResponse(200, {"currency": "EUR", "cash": {}})]
    session = FakeSession(lambda *a: responses.pop(0))
    client = Trading212Client("k", "s", "demo", session=session, sleep=lambda s: None)
    assert client.account_summary()["currency"] == "EUR"
    assert len(session.calls) == 2


def test_errors_are_explained():
    session = FakeSession(lambda *a: FakeResponse(401, {"message": "bad"}))
    client = Trading212Client("k", "s", "live", session=session, sleep=lambda s: None)
    with pytest.raises(Trading212Error, match="bad API key"):
        client.account_summary()
    assert session.calls[0][1].startswith("https://live.trading212.com")


def test_order_post_not_retried_on_network_error():
    import requests

    def boom(*a):
        raise requests.ConnectionError("down")
    client = Trading212Client("k", "s", "demo", session=FakeSession(boom), sleep=lambda s: None)
    with pytest.raises(Trading212Error):
        client.place_market_order(TICKER, 1)
    assert len(client.session.calls) == 1
