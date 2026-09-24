import time

import pytest

from fakes import FakeResponse, FakeSession
from tradingbot.yahoo import YahooData, t212_to_yahoo


@pytest.mark.parametrize("ticker,expected", [
    ("AAPL_US_EQ", "AAPL"), ("BRK.B_US_EQ", "BRK-B"), ("VUSAl_EQ", "VUSA.L"),
    ("SAPd_EQ", "SAP.DE"), ("EURUSD=X", "EURUSD=X"), ("BTC/USD", "BTC-USD"),
])
def test_t212_to_yahoo(ticker, expected):
    assert t212_to_yahoo(ticker) == expected


def chart(meta=None, ts=(), **cols):
    return {"chart": {"error": None, "result": [{
        "meta": meta or {}, "timestamp": list(ts),
        "indicators": {"quote": [cols]},
    }]}}


def test_fetch_ohlcv_drops_null_rows_and_limits():
    body = chart(ts=[1000, 2000, 3000, 4000], open=[1, None, 3, 4], high=[2, 2, 4, 5],
                 low=[0.5, 1, 2, 3], close=[1.5, 1.8, 3.5, 4.5], volume=[10, 10, None, 10])
    y = YahooData(session=FakeSession(lambda *a: FakeResponse(200, body)))
    rows = y.fetch_ohlcv("AAPL_US_EQ", "1h", limit=2)
    assert [r[0] for r in rows] == [3_000_000, 4_000_000]
    assert rows[0][5] == 0.0  # missing volume -> 0
    method, url, params, _ = y.session.calls[0]
    assert url.endswith("/AAPL") and params["interval"] == "60m"


def test_market_hours_and_price():
    now = time.time()
    meta = {"regularMarketPrice": 187.5, "currency": "USD",
            "currentTradingPeriod": {"regular": {"start": now - 60, "end": now + 60}}}
    y = YahooData(session=FakeSession(lambda *a: FakeResponse(200, chart(meta))))
    assert y.last_price("AAPL") == 187.5
    assert y.is_market_open("AAPL")
    assert not y.is_market_open("AAPL", now=now + 3600)


def test_fx_rate_handles_gbx():
    y = YahooData(session=FakeSession(lambda *a: FakeResponse(200, chart({"regularMarketPrice": 1.25}))))
    assert y.fx_rate("GBP", "GBX") == 100
    assert y.fx_rate("USD", "USD") == 1
    assert y.fx_rate("GBP", "USD") == 1.25
    assert y.fx_rate("EUR", "GBX") == pytest.approx(125)


def test_unsupported_timeframe():
    with pytest.raises(ValueError):
        YahooData(session=FakeSession(None)).fetch_ohlcv("AAPL", "4h", limit=10)
