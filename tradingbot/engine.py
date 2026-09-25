"""Live / paper trading loop.

Every `poll_seconds`:
  1. fetch the latest prices and update equity / circuit breakers
  2. skip trading while the market is closed (stocks)
  3. check stop-loss and take-profit against the latest prices
  4. when a new candle has closed: run the strategy, trail the stop, rebalance
  5. persist state to disk so a restart resumes the same positions

The engine holds its positions in *slots*: one slot per stock, each with its own
Trader (position, stop, trade history). Without the scanner there is one slot on
`exchange.symbol`. With the scanner there are `scanner.max_positions` slots: while
a slot is flat, rescans assign it the best-ranked stock that no other slot holds;
a slot keeps its stock until the position is closed. Cash, the drawdown kill
switch and the daily loss limit are shared by the whole account.

Create a file named STOP in the state directory (or send /stop on Telegram)
to flatten and shut down.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .brokers.base import Broker
from .brokers.paper import PaperBroker
from .config import Config
from .data import drop_open_candle, fetch_ohlcv
from .indicators import atr
from .notify import Notifier
from .risk import RiskManager, RiskState
from .scanner import Scanner
from .strategies import Strategy
from .trader import Trader

log = logging.getLogger(__name__)


@dataclass
class Slot:
    broker: Broker | None  # None until the scanner first assigns this slot a stock
    trader: Trader
    active: bool = True    # has a stock to watch (idle scanner slots don't)
    last_bar: str = ""
    last_price: float | None = None

    @property
    def symbol(self) -> str | None:
        return self.broker.symbol if self.broker else None

    @property
    def is_open(self) -> bool:
        return self.trader.position.is_open


class Engine:
    def __init__(self, cfg: Config, strategy: Strategy, broker: Broker, market_data,
                 notifier: Notifier | None = None, label: str | None = None, scanner: Scanner | None = None):
        self.cfg = cfg
        self.strategy = strategy
        self.market_data = market_data  # ccxt exchange or YahooData, used for candles
        self.scanner = scanner
        self.notifier = notifier or Notifier()
        self.label = label or f"{cfg.engine.mode}-{cfg.exchange.name}"
        self.risk = RiskManager(cfg.risk)
        n = cfg.scanner.max_positions if scanner else 1
        self.slots = [Slot(broker, Trader(broker, self.risk, notifier=self.notifier), active=scanner is None)]
        self.slots += [Slot(None, Trader(None, self.risk, notifier=self.notifier), active=False) for _ in range(n - 1)]
        self.running = True
        self.last_price: float | None = None
        self.last_equity: float | None = None
        self.market_open: bool | None = None
        self._last_heartbeat = time.monotonic()

        safe_symbol = "scanner" if scanner else cfg.exchange.symbol.replace("/", "-")
        self.state_dir = Path(cfg.engine.state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.state_dir / f"{self.label}_{safe_symbol}.json"
        self.stop_file = self.state_dir / "STOP"
        self.resumed = self.state_file.exists()
        self.load_state()

    # ---- first slot (the only one without the scanner) --------------------------
    @property
    def broker(self) -> Broker:
        return self.slots[0].broker

    @broker.setter
    def broker(self, broker: Broker) -> None:
        self.slots[0].broker = self.slots[0].trader.broker = broker

    @property
    def trader(self) -> Trader:
        return self.slots[0].trader

    @property
    def last_bar(self) -> str:
        return self.slots[0].last_bar

    @property
    def symbol(self) -> str:
        """The stock of the first slot (the traded instrument without the scanner)."""
        return self.slots[0].symbol

    @property
    def open_slots(self) -> list[Slot]:
        return [s for s in self.slots if s.is_open]

    # ---- slots --------------------------------------------------------------------
    def _assign(self, slot: Slot, symbol: str) -> None:
        """Point a flat slot at `symbol`, creating a broker for it on the same account if needed."""
        if slot.broker is None:
            base = self.slots[0].broker
            slot.broker = base.sibling(symbol)
            if isinstance(slot.broker, PaperBroker):
                slot.broker._price_source = lambda b=slot.broker: self.market_data.last_price(b.symbol)
            slot.trader.broker = slot.broker
            slot.last_bar = ""
        elif slot.broker.symbol != symbol:
            slot.broker.set_symbol(symbol)
            slot.last_bar = ""  # act on the new stock's latest candle right away
            slot.trader.position.wait_for_reset = False  # that flag was about the old stock
        slot.active = True

    def _cash(self) -> float:
        return self.slots[0].broker.balances()[0]

    # ---- persistence ----------------------------------------------------------
    def load_state(self) -> None:
        if not self.state_file.exists():
            return
        d = json.loads(self.state_file.read_text())
        self.risk.state = RiskState.from_dict(d["risk"])
        paper = d.get("paper") or {}
        if "slots" in d:
            saved = d["slots"]
        else:  # single-position format from before slots existed
            saved = [{"symbol": d.get("symbol"), "active": True, "trader": d["trader"],
                      "last_bar": d.get("last_bar", ""), "paper_position": paper.get("position")}]
        # Never drop an open position, even if max_positions was lowered.
        while len(self.slots) < len(saved):
            self.slots.append(Slot(None, Trader(None, self.risk, notifier=self.notifier), active=False))
        for slot, sd in zip(self.slots, saved):
            symbol = sd.get("symbol")
            held = (sd["trader"].get("position") or {}).get("qty", 0) > 0
            if self.scanner and symbol and (sd.get("active", True) or held):
                self._assign(slot, symbol)
                slot.active = sd.get("active", True) or held
            # After _assign, which resets per-stock flags: the saved position (incl. wait_for_reset) wins.
            slot.trader.load_dict(sd["trader"])
            slot.last_bar = sd.get("last_bar", "")
            if isinstance(slot.broker, PaperBroker) and sd.get("paper_position") is not None:
                slot.broker.position = float(sd["paper_position"])
        if isinstance(self.broker, PaperBroker) and "cash" in paper:
            self.broker.cash = float(paper["cash"])
        held = ", ".join(f"{s.trader.position.qty:.6g} {s.symbol}" for s in self.open_slots) or "flat"
        log.info("Resumed state from %s (%s)", self.state_file, held)

    def save_state(self) -> None:
        d = {
            "updated": datetime.now(timezone.utc).isoformat(),
            "strategy": {"name": self.strategy.name, "params": self.strategy.params},
            "risk": self.risk.state.to_dict(),
            "slots": [],
        }
        for slot in self.slots:
            sd = {"symbol": slot.symbol, "active": slot.active, "trader": slot.trader.to_dict(),
                  "last_bar": slot.last_bar}
            if isinstance(slot.broker, PaperBroker):
                sd["paper_position"] = slot.broker.position
            d["slots"].append(sd)
        if isinstance(self.broker, PaperBroker):
            d["paper"] = {"cash": self.broker.cash}
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=2, default=str))
        os.replace(tmp, self.state_file)  # atomic

    def reconcile(self) -> None:
        """Make sure the positions we think we hold still exist at the broker."""
        for slot in self.open_slots:
            pos = slot.trader.position
            if isinstance(slot.broker, PaperBroker):
                continue
            _, held = slot.broker.balances()
            if held < pos.qty * 0.99:
                msg = (f"Saved position {pos.qty:.6g} {slot.symbol} but broker holds {held:.6g} "
                       "(sold manually?). Adjusting to what is held.")
                log.warning(msg)
                self.notifier.send(f"⚠️ {msg}")
                if held <= 0:
                    slot.trader.position = type(pos)()
                else:
                    pos.qty = held

    def adopt_positions(self) -> int:
        """At startup, take over held stocks from the universe that no slot tracks.

        Recovers from a lost or stale state file (new machine, wiped disk, a crash right
        after a fill): without this the bot thinks it is flat, may buy more stocks and
        leaves the held ones without a stop. Fills free slots only, largest position first.
        Each stop is rebuilt from ATR: from the entry price, or from the current price if
        that is higher and the trailing stop is on (the old stop would have trailed up).
        Returns the number of positions taken over.
        """
        held_positions = getattr(self.broker, "held_positions", None)
        if held_positions is None:
            return 0
        allowed = set(self.scanner.universe) if self.scanner else {self.symbol}
        tracked = {s.symbol for s in self.open_slots}
        candidates = sorted((p for p in held_positions() if p["ticker"] in allowed and p["ticker"] not in tracked),
                            key=lambda p: p["qty"] * p["avg_price"], reverse=True)
        free = [s for s in self.slots if not s.is_open]
        taken, others = candidates[:len(free)], [p["ticker"] for p in candidates[len(free):]]
        tf = self.cfg.exchange.timeframe
        for slot, p in zip(free, taken):
            self._assign(slot, p["ticker"])
            candles = drop_open_candle(fetch_ohlcv(self.market_data, slot.symbol, tf,
                                                   self.cfg.engine.history_bars + 1), tf)
            atr_value = float(atr(candles, self.cfg.risk.atr_period).iloc[-1])
            price = slot.broker.last_price()
            stop = self.risk.initial_stop(p["avg_price"], atr_value)
            if self.cfg.risk.trailing_stop:
                stop = max(stop, self.risk.initial_stop(price, atr_value))
            pos = slot.trader.position
            pos.qty, pos.entry_price, pos.stop = p["qty"], p["avg_price"], stop
            pos.entry_time = p["opened"] or str(datetime.now(timezone.utc))
            pos.take_profit = self.risk.take_profit(p["avg_price"], atr_value)
            msg = (f"♻️ Took over the untracked {p['qty']:.6g} {slot.symbol} held in the account "
                   f"(avg {p['avg_price']:.4f}, now {price:.4f}), stop {stop:.4f}")
            log.warning(msg)
            self.notifier.send(msg)
        if others:
            msg = f"Not managing these held stocks (all {len(self.slots)} slots are full): {', '.join(others)}"
            log.warning(msg)
            self.notifier.send(f"⚠️ {msg}")
        if taken:
            self.save_state()
        return len(taken)

    # ---- status / commands --------------------------------------------------------
    def status_text(self) -> str:
        trades = [t for s in self.slots for t in s.trader.trades]
        pnl = sum(t.pnl for t in trades)
        what = f"up to {len(self.slots)} of {len(self.scanner.universe)} stocks" if self.scanner else self.symbol
        lines = [
            f"📊 {self.label} · {what} {self.cfg.exchange.timeframe} · {self.strategy.name}",
            f"Equity: {self.last_equity:,.2f}" if self.last_equity is not None else "Equity: n/a",
        ]
        if not self.scanner:
            lines.append(f"Price: {self.last_price:.4f}" if self.last_price is not None else "Price: n/a")
        if self.market_open is not None:
            lines.append(f"Market: {'open' if self.market_open else 'closed'}")
        for slot in self.open_slots:
            pos = slot.trader.position
            upnl = (slot.last_price - pos.entry_price) * pos.qty if slot.last_price else 0.0
            name = f"{slot.symbol} " if self.scanner else ""
            lines.append(f"Position: {name}{pos.qty:.6g} @ {pos.entry_price:.4f} · stop {pos.stop:.4f} "
                         f"· uPnL {upnl:+,.2f}")
        if not self.open_slots:
            lines.append("Position: flat")
        wins = sum(t.pnl > 0 for t in trades)
        lines.append(f"Closed trades: {len(trades)} ({wins} wins) · realised PnL {pnl:+,.2f}")
        if self.scanner and self.scanner.results:
            picks = [r.symbol for r in self.scanner.eligible()[:3]]
            lines.append(f"Scanner: {len(self.scanner.results)} stocks, top picks {', '.join(picks) or 'none'}")
        if self.risk.state.halted:
            lines.append(f"🛑 HALTED: {self.risk.state.halt_reason}")
        return "\n".join(lines)

    def _cmd_stop(self) -> str:
        self.stop_file.touch()
        return "🛑 Stop requested: the bot will close its positions and shut down within one poll."

    # ---- main loop --------------------------------------------------------------
    def _handle_signal(self, signum, _frame):
        log.info("Received signal %s, shutting down after this iteration", signum)
        self.running = False

    def run(self, max_iterations: int | None = None) -> None:
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)
        what = (f"best {len(self.slots)} of {len(self.scanner.universe)} stocks" if self.scanner
                else self.symbol)
        log.info("Starting %s: %s %s with %r", self.label, what, self.cfg.exchange.timeframe, self.strategy)
        if self.cfg.telegram.commands:
            commands = {
                "status": self.status_text, "stop": self._cmd_stop,
                "help": lambda: "/status – account & positions\n/stop – close positions and shut down"
                                + ("\n/scan – latest stock ranking" if self.scanner else ""),
            }
            if self.scanner:
                commands["scan"] = self.scanner.summary
            self.notifier.start_commands(commands)
        if self.cfg.exchange.name == "trading212" and self.cfg.trading212.adopt_positions:
            try:
                self.adopt_positions()
            except Exception as e:
                log.warning("Could not take over held positions at startup: %s", e)
                self.notifier.send(f"⚠️ Could not take over held positions: {e}")
        try:
            self.reconcile()
        except Exception as e:
            log.warning("Could not reconcile positions at startup: %s", e)
        self.notifier.send(f"🤖 Bot started: {self.label} · {what} "
                           f"{self.cfg.exchange.timeframe} · {self.strategy!r}")
        iterations = 0
        while self.running:
            try:
                self.step()
            except Exception as e:  # keep the bot alive; state is saved each step
                log.exception("Error in trading step")
                if self.cfg.telegram.notify_errors:
                    self.notifier.send(f"⚠️ Error: {type(e).__name__}: {e}", key=type(e).__name__, throttle=1800)
            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                break
            self._sleep(self.cfg.engine.poll_seconds)
        self.save_state()
        log.info("Engine stopped")
        self.notifier.send(f"⏹ Bot stopped\n{self.status_text()}")
        self.notifier.close()

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while self.running and time.monotonic() < end:
            time.sleep(min(1.0, end - time.monotonic()))

    def _is_market_open(self) -> bool:
        check = getattr(self.market_data, "is_market_open", None)
        if check is None:
            return True
        watched = next((s.symbol for s in self.slots if s.active and s.broker), self.symbol)
        return bool(check(watched))

    def _heartbeat(self) -> None:
        hours = self.cfg.telegram.heartbeat_hours
        if hours and time.monotonic() - self._last_heartbeat >= hours * 3600:
            self._last_heartbeat = time.monotonic()
            self.notifier.send(self.status_text())

    def step(self) -> None:
        now = datetime.now(timezone.utc)
        for slot in self.slots:
            if slot.is_open or (slot.active and not self.scanner):
                slot.last_price = slot.broker.last_price()
        self.last_price = self.slots[0].last_price
        equity = self._cash() + sum(s.trader.position.qty * s.last_price for s in self.open_slots)
        self.last_equity = equity
        was_halted = self.risk.state.halted
        self.risk.update_equity(equity, now)
        if self.risk.state.halted and not was_halted:
            self.notifier.send(f"🛑 KILL SWITCH: {self.risk.state.halt_reason}\n"
                               "Trading halted. Review, then run `python -m tradingbot reset-halt`.")
        self._heartbeat()

        stop_requested = self.stop_file.exists()
        self.market_open = self._is_market_open()
        if not self.market_open:
            if stop_requested and not self.open_slots:
                self.running = False
            elif stop_requested:
                log.info("STOP requested; waiting for the market to open to sell")
            self.save_state()
            return

        if stop_requested:
            log.warning("STOP file found: flattening and shutting down")
            for slot in self.open_slots:
                slot.trader.exit(slot.last_price, now, "manual_stop")
            self.running = False
            self.save_state()
            return

        if self.risk.state.halted:
            for slot in self.open_slots:
                slot.trader.exit(slot.last_price, now, "kill_switch")

        try:
            for slot in self.open_slots:
                p = slot.last_price
                self._guarded(slot, slot.trader.check_exits, p, p, p, now)

            if (self.scanner and not self.risk.state.halted and len(self.open_slots) < len(self.slots)
                    and self.scanner.due()):
                self._rescan()

            for slot in self.slots:
                if slot.active and slot.broker is not None:
                    self._guarded(slot, self._process_slot, slot, now)
        finally:
            self.save_state()  # always record fills, even if a later stock failed

    def _guarded(self, slot: Slot, fn, *args) -> None:
        """Run one stock's work; a failure there must not stop the other positions."""
        try:
            fn(*args)
        except Exception as e:
            log.exception("Error on %s", slot.symbol)
            if self.cfg.telegram.notify_errors:
                self.notifier.send(f"⚠️ {slot.symbol}: {type(e).__name__}: {e}",
                                   key=f"{slot.symbol}{type(e).__name__}", throttle=1800)

    def _process_slot(self, slot: Slot, now: datetime) -> None:
        tf = self.cfg.exchange.timeframe
        candles = fetch_ohlcv(self.market_data, slot.symbol, tf, self.cfg.engine.history_bars + 1)
        candles = drop_open_candle(candles, tf)
        if candles.empty:
            log.warning("No closed candles received for %s", slot.symbol)
            return
        bar_time = str(candles.index[-1])
        if bar_time == slot.last_bar:
            return
        atr_value = float(atr(candles, self.cfg.risk.atr_period).iloc[-1])
        target = self.strategy.latest_target(candles)
        slot.trader.trail(float(candles["close"].iloc[-1]), atr_value)
        price = slot.last_price if slot.is_open and slot.last_price else slot.broker.last_price()
        slot.last_price = price
        log.info("New bar %s %s close=%.4f target=%d equity=%.2f stop=%s",
                 slot.symbol, bar_time, candles["close"].iloc[-1], target, self.last_equity,
                 f"{slot.trader.position.stop:.4f}" if slot.is_open else "-")
        max_value = None
        if len(self.slots) > 1:  # share the account: cap each position and leave cash for fees/FX
            max_value = min(self.last_equity * self.cfg.risk.max_position_pct / len(self.slots),
                            self._cash() * 0.95)
        slot.trader.rebalance(target, atr_value, price, now, equity=self.last_equity, max_value=max_value)
        slot.last_bar = bar_time

    def _rescan(self) -> None:
        """Rank the universe and give each flat slot the best stock that no other slot holds."""
        results = self.scanner.scan()
        # A stock stopped out today is skipped: its slot would otherwise sit idle waiting for the
        # trend to reset (no re-entry after a stop) while other picks go unbought.
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        stopped = {t.symbol for s in self.slots for t in s.trader.trades
                   if t.exit_reason == "stop_loss" and t.exit_time.startswith(today)}
        stopped |= {s.symbol for s in self.slots if not s.is_open and s.trader.position.wait_for_reset}
        held = {s.symbol for s in self.open_slots}
        picks = [r for r in self.scanner.eligible() if r.symbol not in held | stopped]
        log.info("Scanned %d stocks; eligible: %s", len(results),
                 ", ".join(f"{r.symbol} ({r.score:+.2f})" for r in picks[:5]) or "none")
        flat = [s for s in self.slots if not s.is_open]
        # A flat slot already watching one of the picks keeps it (no churn).
        pick_symbols = {r.symbol for r in picks}
        keep = [s for s in flat if s.active and s.symbol in pick_symbols]
        picks = [r for r in picks if r.symbol not in {s.symbol for s in keep}]
        for slot in flat:
            if slot in keep:
                continue
            if not picks:
                slot.active = False  # nothing worth buying: idle until the next scan
                continue
            r = picks.pop(0)
            old = slot.symbol if slot.active else None
            self._assign(slot, r.symbol)
            log.info("Slot %d now watching %s", self.slots.index(slot) + 1, r.symbol)
            self.notifier.send(f"🔎 {'Switching ' + old + ' → ' if old and old != r.symbol else 'Watching '}"
                               f"{r.symbol} (score {r.score:+.2f}, {r.momentum:+.1%} over "
                               f"{self.cfg.scanner.lookback_bars} bars)")
