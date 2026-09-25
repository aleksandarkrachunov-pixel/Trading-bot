import json

import numpy as np
import pytest
import pandas as pd

from fakes import FakeSession
from test_trading212 import FakeData, FakeT212
from tradingbot.brokers.paper import PaperBroker
from tradingbot.brokers.trading212 import Trading212Broker, Trading212Client
from tradingbot.config import Config, ScannerConfig
from tradingbot.engine import Engine
from tradingbot.scanner import Scanner, format_table, score_candles
from tradingbot.strategies import create_strategy


def trend(drift, bars=300, seed=0, start=100.0):
    """Hourly candles with a given per-bar drift, ending two hours ago (all closed)."""
    rng = np.random.default_rng(seed)
    close = start * np.exp(np.cumsum(drift + rng.normal(0, 0.004, bars)))
    open_ = np.concatenate([[start], close[:-1]])
    end = pd.Timestamp.now(tz="UTC").floor("h") - pd.Timedelta(hours=2)
    idx = pd.date_range(end=end, periods=bars, freq="h", tz="UTC")
    return pd.DataFrame({"open": open_, "high": np.maximum(open_, close) * 1.001,
                         "low": np.minimum(open_, close) * 0.999, "close": close, "volume": 1000.0}, index=idx)


class MultiData:
    """ccxt-style candles for several symbols; unknown symbols raise like a bad ticker would."""

    def __init__(self, frames):
        self.frames = frames
        self.requests = []

    def fetch_ohlcv(self, symbol, timeframe, limit=None, since=None):
        self.requests.append(symbol)
        df = self.frames[symbol].iloc[-limit:]
        return [[int(ts.timestamp() * 1000), *row] for ts, row in zip(df.index, df.to_numpy().tolist())]

    def price(self, symbol):
        return float(self.frames[symbol]["close"].iloc[-1])

    last_price = price


FRAMES = {
    "STEADY_US_EQ": trend(0.002, seed=1),    # strong, smooth uptrend
    "CHOPPY_US_EQ": trend(0.0005, seed=2),   # weak uptrend
    "FALLING_US_EQ": trend(-0.002, seed=3),  # downtrend: strategy says flat
}


def make_scanner(frames=FRAMES, universe=None, **cfg):
    data = MultiData(frames)
    sc = Scanner(data, create_strategy("sma_crossover"), ScannerConfig(enabled=True, **cfg), "1h", 200,
                 universe=universe or list(frames), sleep=lambda s: None)
    return sc, data


def test_score_prefers_steady_uptrend_and_needs_history():
    strategy = create_strategy("sma_crossover")
    up = score_candles(FRAMES["STEADY_US_EQ"], strategy, 120)
    down = score_candles(FRAMES["FALLING_US_EQ"], strategy, 120)
    assert up[0] > 0 and up[2] > 0 and up[3] == 1
    assert down[2] < 0 and down[3] == 0
    assert score_candles(FRAMES["STEADY_US_EQ"].iloc[:30], strategy, 120) is None


def test_scan_ranks_and_picks_best_eligible():
    sc, _ = make_scanner()
    results = sc.scan()
    assert [r.symbol for r in results][0] == "STEADY_US_EQ"
    assert results[-1].symbol == "FALLING_US_EQ"
    assert sc.best().symbol == "STEADY_US_EQ"
    assert "STEADY_US_EQ" in format_table(results, 5.0) and "BUY" in format_table(results, 5.0)
    assert "STEADY_US_EQ" in sc.summary()


def test_scan_skips_failing_tickers_and_respects_min_price():
    sc, _ = make_scanner(universe=["NOPE_US_EQ", "STEADY_US_EQ"], min_price=1e9)
    results = sc.scan()
    assert [r.symbol for r in results] == ["STEADY_US_EQ"]
    assert sc.best() is None  # too cheap for min_price


def test_scan_due_after_rescan_interval():
    sc, _ = make_scanner(rescan_minutes=60)
    assert sc.due()
    sc.scan()
    assert not sc.due()
    assert sc.due(now=sc.last_scan + 3601)


def make_engine(tmp_path, frames=FRAMES, start="FALLING_US_EQ", max_positions=1):
    cfg = Config()
    cfg.engine.state_dir = str(tmp_path)
    cfg.engine.history_bars = 200
    cfg.scanner.max_positions = max_positions
    sc, data = make_scanner(frames)
    broker = PaperBroker(start, 10_000, price_source=lambda: data.price(broker.symbol))
    engine = Engine(cfg, create_strategy("sma_crossover"), broker, data, scanner=sc)
    engine.scanner.cfg.max_positions = max_positions
    return engine, data


def test_engine_switches_to_best_stock_and_buys(tmp_path):
    engine, _ = make_engine(tmp_path)
    engine.step()
    assert engine.symbol == "STEADY_US_EQ"
    assert engine.trader.position.is_open
    assert engine.state_file.name.endswith("_scanner.json")

    # Resuming restores the stock the scanner picked.
    resumed, _ = make_engine(tmp_path)
    assert resumed.symbol == "STEADY_US_EQ" and resumed.trader.position.is_open


def test_engine_holds_position_instead_of_rotating(tmp_path):
    engine, data = make_engine(tmp_path)
    engine.step()
    engine.scanner.last_scan = None  # rescan would be due, but we're in a position
    requests = len(data.requests)
    engine.step()
    assert engine.symbol == "STEADY_US_EQ"
    assert data.requests[requests:] == ["STEADY_US_EQ"]  # only the held stock's candles


def test_engine_stays_flat_when_nothing_is_eligible(tmp_path):
    frames = {"FALLING_US_EQ": FRAMES["FALLING_US_EQ"]}
    engine, _ = make_engine(tmp_path, frames)
    engine.step()
    assert engine.symbol == "FALLING_US_EQ" and not engine.trader.position.is_open


def api_calls(api):
    return [url for _, url, _, _ in api_calls.session.calls]


def test_trading212_set_symbol_and_tradable_filter():
    class MultiT212(FakeT212):
        def __call__(self, method, url, params, json):
            if url.endswith("/metadata/instruments"):
                from fakes import FakeResponse
                self.instrument_calls = getattr(self, "instrument_calls", 0) + 1
                return FakeResponse(200, [
                    {"ticker": "AAPL_US_EQ", "name": "Apple", "currencyCode": "USD", "shortName": "AAPL"},
                    {"ticker": "MSFT_US_EQ", "name": "Microsoft", "currencyCode": "USD", "shortName": "MSFT"},
                    {"ticker": "FB_US_EQ", "name": "Meta Platforms", "currencyCode": "USD", "shortName": "META"},
                    {"ticker": "CTRA_US_EQ", "name": "Alpha Metallurgical", "currencyCode": "USD",
                     "shortName": "AMR"},
                    {"ticker": "VODl_EQ", "name": "Vodafone", "currencyCode": "GBX"},
                ])
            return super().__call__(method, url, params, json)

    api = MultiT212()
    session = FakeSession(api)
    api_calls.session = session
    client = Trading212Client("k", "s", "demo", session=session, sleep=lambda s: None)
    data = FakeData()
    data.symbol_map = {}
    broker = Trading212Broker(client, "AAPL_US_EQ", data, sleep=lambda s: None)
    universe = ["MSFT_US_EQ", "VODl_EQ", "NOPE_US_EQ", "AAPL_US_EQ", "META_US_EQ", "CTRA_US_EQ"]
    assert broker.tradable(universe) == ["MSFT_US_EQ", "AAPL_US_EQ", "FB_US_EQ", "CTRA_US_EQ"]
    # Prices come from the market symbol, not the (stale) T212 ticker.
    assert data.symbol_map["FB_US_EQ"] == "META" and data.symbol_map["CTRA_US_EQ"] == "AMR"
    broker.set_symbol("MSFT_US_EQ")
    assert broker.symbol == "MSFT_US_EQ" and broker.instrument["name"] == "Microsoft"
    other = broker.sibling("FB_US_EQ")
    assert other.symbol == "FB_US_EQ" and broker.symbol == "MSFT_US_EQ"
    summaries = sum(1 for c in api_calls(api) if c.endswith("/account/summary"))
    other.balances()
    broker.balances()
    assert sum(1 for c in api_calls(api) if c.endswith("/account/summary")) == summaries  # shared cache
    assert api.instrument_calls == 1  # instrument list is cached and shared


def test_default_universe():
    from tradingbot.scanner import DEFAULT_UNIVERSE
    assert len(DEFAULT_UNIVERSE) == len(set(DEFAULT_UNIVERSE)) == 45
    assert {"FB_US_EQ", "AMD_US_EQ", "TSLA_US_EQ"} <= set(DEFAULT_UNIVERSE)


MORE = {**FRAMES, "RISING_US_EQ": trend(0.0015, seed=4), "CLIMB_US_EQ": trend(0.001, seed=5)}


def test_engine_holds_several_positions_sharing_cash(tmp_path):
    engine, _ = make_engine(tmp_path, MORE, max_positions=3)
    engine.step()
    held = [s.symbol for s in engine.open_slots]
    assert len(held) == 3 and len(set(held)) == 3
    assert "FALLING_US_EQ" not in held
    assert held == [r.symbol for r in engine.scanner.eligible()[:3]]
    # One cash account: every buy came out of it, and no position exceeds its share.
    value = sum(s.trader.position.qty * s.last_price for s in engine.open_slots)
    assert engine.broker.cash == pytest.approx(10_000 - value, abs=50)  # minus fees and slippage
    for s in engine.open_slots:
        assert s.trader.position.qty * s.last_price <= 10_000 * engine.cfg.risk.max_position_pct / 3 * 1.01

    resumed, _ = make_engine(tmp_path, MORE, max_positions=3)
    assert [s.symbol for s in resumed.open_slots] == held
    assert resumed.broker.cash == pytest.approx(engine.broker.cash)
    assert "RISING_US_EQ" in resumed.status_text() or "CLIMB_US_EQ" in resumed.status_text()


def test_free_slot_goes_idle_when_nothing_else_is_eligible(tmp_path):
    frames = {k: MORE[k] for k in ("STEADY_US_EQ", "RISING_US_EQ", "FALLING_US_EQ")}  # 2 uptrends
    engine, _ = make_engine(tmp_path, frames, max_positions=3)
    engine.step()
    assert len(engine.open_slots) == 2
    idle = [s for s in engine.slots if not s.is_open]
    assert len(idle) == 1 and not idle[0].active


def test_kill_switch_closes_every_position(tmp_path):
    engine, _ = make_engine(tmp_path, MORE, max_positions=3)
    engine.step()
    engine.risk.state.halted, engine.risk.state.halt_reason = True, "test"
    engine.step()
    assert not engine.open_slots
    assert all(t.exit_reason == "kill_switch" for s in engine.slots for t in s.trader.trades)


def test_loads_single_position_state_from_before_slots(tmp_path):
    engine, _ = make_engine(tmp_path)
    old = {"risk": engine.risk.state.to_dict(), "last_bar": "2026-01-01", "symbol": "STEADY_US_EQ",
           "trader": {"position": {"qty": 3.0, "entry_price": 100.0, "stop": 90.0}, "trades": []},
           "paper": {"cash": 9700.0, "position": 3.0}}
    engine.state_file.write_text(json.dumps(old))
    resumed, _ = make_engine(tmp_path, max_positions=2)
    assert resumed.symbol == "STEADY_US_EQ" and resumed.trader.position.qty == 3.0
    assert resumed.broker.cash == 9700.0 and resumed.broker.position == 3.0
    assert resumed.last_bar == "2026-01-01" and len(resumed.slots) == 2


class AdoptBroker(PaperBroker):
    """Paper broker that reports positions already held in the account."""

    def __init__(self, held, **kw):
        super().__init__("FALLING_US_EQ", 10_000, **kw)
        self.held = held

    def held_positions(self):
        return self.held


def test_fresh_start_takes_over_held_positions(tmp_path):
    cfg = Config()
    cfg.engine.state_dir = str(tmp_path)
    cfg.engine.history_bars = 200
    cfg.scanner.max_positions = 2
    sc, data = make_scanner(MORE)
    held = [{"ticker": "STEADY_US_EQ", "qty": 5.0, "avg_price": 100.0, "opened": "2026-09-24T19:32"},
            {"ticker": "CLIMB_US_EQ", "qty": 1.0, "avg_price": 100.0, "opened": ""},
            {"ticker": "RISING_US_EQ", "qty": 2.0, "avg_price": 100.0, "opened": ""},
            {"ticker": "MANUAL_US_EQ", "qty": 50.0, "avg_price": 100.0, "opened": ""}]  # not in universe
    broker = AdoptBroker(held, price_source=lambda: data.price(broker.symbol))
    engine = Engine(cfg, create_strategy("sma_crossover"), broker, data, scanner=sc)
    notifier = []
    engine.notifier.send = lambda text, **kw: notifier.append(text)
    assert engine.adopt_positions() == 2
    got = {s.symbol: s.trader.position for s in engine.open_slots}
    assert set(got) == {"STEADY_US_EQ", "RISING_US_EQ"}  # the two largest in the universe
    steady = got["STEADY_US_EQ"]
    assert steady.qty == 5.0 and steady.entry_price == 100.0 and steady.entry_time == "2026-09-24T19:32"
    assert 0 < steady.stop < data.price("STEADY_US_EQ")
    assert any("CLIMB_US_EQ" in m for m in notifier)  # told which one it isn't managing
    # A restart doesn't take over again what it already tracks (and both slots are full).
    again = Engine(cfg, create_strategy("sma_crossover"), AdoptBroker(held), data, scanner=sc)
    assert again.resumed and again.adopt_positions() == 0
    assert {s.symbol for s in again.open_slots} == {"STEADY_US_EQ", "RISING_US_EQ"}

    # A fill the state file missed is picked up when a slot is free.
    cfg.scanner.max_positions = 3
    third = Engine(cfg, create_strategy("sma_crossover"), AdoptBroker(held), data, scanner=sc)
    assert third.adopt_positions() == 1
    assert {s.symbol for s in third.open_slots} == {"STEADY_US_EQ", "RISING_US_EQ", "CLIMB_US_EQ"}


def test_paper_and_trading212_siblings_share_the_account():
    a = PaperBroker("A", 1000.0)
    b = a.sibling("B")
    b.set_price(10.0)
    b.market_buy(5)
    assert a.cash == b.cash < 1000.0 and a.position == 0 and b.position == 5


def test_one_failing_stock_does_not_block_others_or_the_save(tmp_path):
    engine, _ = make_engine(tmp_path, MORE, max_positions=3)
    original = engine._process_slot

    def flaky(slot, now):
        if slot is engine.slots[1]:
            raise RuntimeError("order rejected")
        original(slot, now)
    engine._process_slot = flaky
    engine.step()
    assert engine.slots[0].is_open and engine.slots[2].is_open and not engine.slots[1].is_open
    state = json.loads(engine.state_file.read_text())
    assert sum(s["trader"]["position"]["qty"] > 0 for s in state["slots"]) == 2


def test_stock_stopped_out_today_is_skipped_so_the_slot_is_reused(tmp_path):
    frames = {**MORE, "FOURTH_US_EQ": trend(0.0008, seed=6)}
    engine, _ = make_engine(tmp_path, frames, max_positions=3)
    engine.step()
    assert len(engine.scanner.eligible()) >= 4
    slot = next(s for s in engine.slots if s.symbol == "RISING_US_EQ")
    p = slot.trader.position.stop - 1  # price gaps below the stop
    slot.trader.check_exits(p, p, p, pd.Timestamp.now(tz="UTC"))
    assert not slot.is_open and slot.trader.trades[-1].symbol == "RISING_US_EQ"
    engine.scanner.last_scan = None
    engine._rescan()
    # RISING is still the top-ranked stock, but the free slot moves on to the next pick.
    assert slot.symbol == "FOURTH_US_EQ" and slot.active
    assert slot.symbol not in {s.symbol for s in engine.open_slots}
