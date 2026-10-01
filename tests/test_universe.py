import pytest

from fakes import FakeResponse, FakeSession
from tradingbot.config import ScannerConfig
from tradingbot.scanner import Scanner
from tradingbot.strategies import create_strategy
from tradingbot.universe import fetch_screener, fetch_sp500, filter_small_caps, small_caps, to_t212


def quote(symbol, cap=1e9, price=10.0, volume=2e6, exchange="NMS", **kw):
    return {"symbol": symbol, "marketCap": cap, "regularMarketPrice": price, "averageDailyVolume3Month": volume,
            "exchange": exchange, "quoteType": "EQUITY", "currency": "USD", **kw}


def test_filters_size_liquidity_price_exchange_and_sp500():
    cfg = ScannerConfig(min_price=3.0)
    quotes = [
        quote("GOOD", volume=3e6),                      # $30M/day
        quote("LIQUID", volume=9e6),                    # most traded: first
        quote("BIG", cap=50e9),                         # large cap
        quote("TINY", cap=50e6),                        # micro cap
        quote("THIN", volume=100_000),                  # $1M/day: illiquid
        quote("PENNY", price=1.5, volume=9e7),
        quote("OTC", exchange="PNK"),
        quote("ETF", quoteType="ETF"),
        quote("SPX"),                                   # small but in the S&P 500 list
        quote("GOOD", volume=3e6),                      # duplicate from a second screener
    ]
    assert filter_small_caps(quotes, cfg, sp500={"SPX", "AAPL"}) == ["LIQUID", "GOOD"]
    assert "SPX" in filter_small_caps(quotes, cfg, sp500=None)  # only the list removes it


def router(pages, sp500_csv):
    def route(method, url, params, json):
        if "screener" in url:
            rows = pages[params["scrIds"]]
            start = params["start"]
            return FakeResponse(200, {"finance": {"result": [{"total": len(rows), "quotes": rows[start:start + 250]}]}})
        if "constituents.csv" in url:
            resp = FakeResponse(200, None)
            resp.text = sp500_csv
            return resp
        return FakeResponse(404, {})
    return route


class Session(FakeSession):
    def get(self, url, params=None, timeout=None):
        return self.request("GET", url, params=params)


SP500_CSV = "Symbol,Security\n" + "\n".join(f"S{i},Co {i}" for i in range(450)) + "\nBRK.B,Berkshire\nSMALLSP,Tiny member\n"


def test_small_caps_paginates_merges_screeners_and_excludes_sp500():
    many = [quote(f"X{i}", volume=1e6 + i) for i in range(300)]
    pages = {"aggressive_small_caps": many, "small_cap_gainers": [quote("SMALLSP"), quote("HOT", volume=5e7)]}
    session = Session(router(pages, SP500_CSV))
    assert len(fetch_screener("aggressive_small_caps", session=session)) == 300  # two pages
    assert "BRK-B" in fetch_sp500(session)
    got = small_caps(ScannerConfig(max_universe=50), session=session)
    assert got[0] == "HOT" and "SMALLSP" not in got and len(got) == 50


def test_incomplete_sp500_list_is_rejected_but_cap_ceiling_still_applies():
    pages = {"aggressive_small_caps": [quote("OK"), quote("HUGE", cap=3e11)], "small_cap_gainers": []}
    session = Session(router(pages, "Symbol\nAAPL\n"))
    with pytest.raises(ValueError):
        fetch_sp500(session)
    assert small_caps(ScannerConfig(), session=session) == ["OK"]


def test_to_t212():
    assert to_t212(["SOUN", "BRK-B"]) == ["SOUN_US_EQ", "BRK.B_US_EQ"]


def test_scanner_refreshes_universe_from_provider():
    calls = []

    def provider():
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("screener down")
        return [f"NEW{len(calls)}_US_EQ"]

    sc = Scanner(None, create_strategy("sma_crossover"), ScannerConfig(universe_refresh_hours=1), "1h", 100,
                 universe=["OLD_US_EQ"], universe_provider=provider)
    sc.refresh_universe(now=0)
    assert sc.universe == ["NEW1_US_EQ"]
    sc.refresh_universe(now=1800)                  # not due yet
    assert len(calls) == 1
    sc.refresh_universe(now=4000)                  # due, provider fails: keep the old list
    assert sc.universe == ["NEW1_US_EQ"]
    sc.refresh_universe(now=8000)
    assert sc.universe == ["NEW3_US_EQ"]
