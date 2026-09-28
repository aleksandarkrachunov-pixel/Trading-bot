"""Dynamic stock universes for the scanner.

`small_caps()` builds a list of liquid US small caps from Yahoo Finance's
predefined screeners and removes every S&P 500 member:

- market cap between `min_market_cap` and `max_market_cap` (default $300M-$2B),
- price >= `min_price` and average daily traded value >= `min_dollar_volume`,
- listed on a US exchange, in USD,
- not in the S&P 500 (constituent list from the public datasets/s-and-p-500-companies
  CSV). If that list can't be downloaded, the market-cap ceiling still keeps S&P 500
  companies out: the index only admits companies worth many billions.

The result is sorted by traded value (most liquid first) and capped at `max_universe`.
"""
from __future__ import annotations

import csv
import io
import logging

import requests

log = logging.getLogger(__name__)

SCREENER_URL = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
SP500_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
US_EXCHANGES = {"NMS", "NGM", "NCM", "NYQ", "ASE", "PCX", "BTS"}  # Nasdaq tiers, NYSE, NYSE American/Arca, Cboe


def _session(session: requests.Session | None) -> requests.Session:
    if session is None:
        session = requests.Session()
        session.headers["User-Agent"] = "Mozilla/5.0 (tradingbot)"  # Yahoo rejects python-requests
    return session


def fetch_screener(scr_id: str, limit: int = 1000, session: requests.Session | None = None) -> list[dict]:
    """All quotes of a Yahoo predefined screener (paginated, 250 per page)."""
    session = _session(session)
    quotes: list[dict] = []
    while len(quotes) < limit:
        r = session.get(SCREENER_URL, params={"scrIds": scr_id, "count": 250, "start": len(quotes)}, timeout=20)
        r.raise_for_status()
        result = (r.json().get("finance", {}).get("result") or [{}])[0]
        page = result.get("quotes") or []
        quotes.extend(page)
        if not page or len(quotes) >= (result.get("total") or 0):
            break
    return quotes[:limit]


def fetch_sp500(session: requests.Session | None = None) -> set[str]:
    """Current S&P 500 tickers in Yahoo form (BRK.B -> BRK-B)."""
    r = _session(session).get(SP500_URL, timeout=20)
    r.raise_for_status()
    rows = csv.DictReader(io.StringIO(r.text))
    symbols = {row["Symbol"].strip().upper().replace(".", "-") for row in rows if row.get("Symbol")}
    if len(symbols) < 400:  # sanity check: a broken download must not look like "nothing to exclude"
        raise ValueError(f"S&P 500 list looks incomplete ({len(symbols)} symbols)")
    return symbols


def filter_small_caps(quotes: list[dict], cfg, sp500: set[str] | None) -> list[str]:
    """Yahoo symbols passing the small-cap filters, most liquid first."""
    picked: dict[str, float] = {}
    for q in quotes:
        symbol = (q.get("symbol") or "").upper()
        cap, price = q.get("marketCap"), q.get("regularMarketPrice")
        volume = q.get("averageDailyVolume3Month") or q.get("averageDailyVolume10Day")
        if not symbol or not cap or not price or not volume:
            continue
        if q.get("quoteType") != "EQUITY" or q.get("currency") != "USD" or q.get("exchange") not in US_EXCHANGES:
            continue
        if not cfg.min_market_cap <= cap <= cfg.max_market_cap:
            continue
        if price < cfg.min_price or price * volume < cfg.min_dollar_volume:
            continue
        if sp500 is not None and symbol in sp500:
            continue
        picked[symbol] = max(picked.get(symbol, 0.0), price * volume)
    return sorted(picked, key=picked.get, reverse=True)[:cfg.max_universe]


def small_caps(cfg, session: requests.Session | None = None) -> list[str]:
    """Liquid US small caps outside the S&P 500, as Yahoo symbols (see module docstring)."""
    session = _session(session)
    quotes: list[dict] = []
    for scr_id in cfg.screeners:
        try:
            quotes.extend(fetch_screener(scr_id, session=session))
        except Exception as e:
            log.warning("Screener %s failed: %s", scr_id, e)
    if not quotes:
        raise RuntimeError("no small-cap screener returned data")
    sp500 = None
    if cfg.exclude_sp500:
        try:
            sp500 = fetch_sp500(session)
        except Exception as e:
            log.warning("Could not load the S&P 500 list (%s); relying on the $%.1fB market-cap ceiling",
                        e, cfg.max_market_cap / 1e9)
    symbols = filter_small_caps(quotes, cfg, sp500)
    log.info("Small-cap universe: %d stocks (from %d screener rows)", len(symbols), len(quotes))
    return symbols


def to_t212(symbols: list[str]) -> list[str]:
    """Yahoo US symbols -> Trading 212 tickers (BRK-B -> BRK.B_US_EQ).

    Trading212Broker.tradable() then drops the ones Trading 212 doesn't list and fixes
    tickers it kept from before a rename.
    """
    return [f"{s.replace('-', '.')}_US_EQ" for s in symbols]
