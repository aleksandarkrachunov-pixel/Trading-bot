"""Live / paper trading loop.

Every `poll_seconds`:
  1. fetch the latest price and update equity / circuit breakers
  2. skip trading while the market is closed (stocks)
  3. check stop-loss and take-profit against the latest price
  4. when a new candle has closed: run the strategy, trail the stop, rebalance
  5. persist state to disk so a restart resumes the same position

With the stock scanner enabled, the engine rescans its universe while flat and
switches to the best-ranked stock; it stays on that stock until the position is
closed.

Create a file named STOP in the state directory (or send /stop on Telegram)
to flatten and shut down.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import time
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


class Engine:
    def __init__(self, cfg: Config, strategy: Strategy, broker: Broker, market_data,
                 notifier: Notifier | None = None, label: str | None = None, scanner: Scanner | None = None):
        self.cfg = cfg
        self.strategy = strategy
        self.broker = broker
        self.market_data = market_data  # ccxt exchange or YahooData, used for candles
        self.scanner = scanner
        self.notifier = notifier or Notifier()
        self.label = label or f"{cfg.engine.mode}-{cfg.exchange.name}"
        self.risk = RiskManager(cfg.risk)
        self.trader = Trader(broker, self.risk, notifier=self.notifier)
        self.last_bar: str = ""
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
        self.load_state()

    @property
    def symbol(self) -> str:
        """The instrument currently traded (changes when the scanner switches stocks)."""
        return self.broker.symbol

    # ---- persistence ----------------------------------------------------------
    def load_state(self) -> None:
        if not self.state_file.exists():
            return
        d = json.loads(self.state_file.read_text())
        self.risk.state = RiskState.from_dict(d["risk"])
        self.trader.load_dict(d["trader"])
        self.last_bar = d.get("last_bar", "")
        if self.scanner and d.get("symbol") and d["symbol"] != self.broker.symbol:
            self.broker.set_symbol(d["symbol"])
        if isinstance(self.broker, PaperBroker) and "paper" in d:
            self.broker.load_dict(d["paper"])
        log.info("Resumed state from %s (position qty=%.6f)", self.state_file, self.trader.position.qty)

    def save_state(self) -> None:
        d = {
            "updated": datetime.now(timezone.utc).isoformat(),
            "strategy": {"name": self.strategy.name, "params": self.strategy.params},
            "risk": self.risk.state.to_dict(),
            "trader": self.trader.to_dict(),
            "last_bar": self.last_bar,
            "symbol": self.broker.symbol,
        }
        if isinstance(self.broker, PaperBroker):
            d["paper"] = self.broker.to_dict()
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=2, default=str))
        os.replace(tmp, self.state_file)  # atomic

    def reconcile(self) -> None:
        """Make sure the position we think we hold still exists at the broker."""
        pos = self.trader.position
        if not pos.is_open or isinstance(self.broker, PaperBroker):
            return
        _, held = self.broker.balances()
        if held < pos.qty * 0.99:
            msg = (f"Saved position {pos.qty:.6g} {self.broker.symbol} but broker holds {held:.6g} "
                   "(sold manually?). Adjusting to what is held.")
            log.warning(msg)
            self.notifier.send(f"⚠️ {msg}")
            if held <= 0:
                self.trader.position = type(pos)()
            else:
                pos.qty = held

    # ---- status / commands --------------------------------------------------------
    def status_text(self) -> str:
        pos = self.trader.position
        trades = self.trader.trades
        pnl = sum(t.pnl for t in trades)
        lines = [
            f"📊 {self.label} · {self.symbol} {self.cfg.exchange.timeframe} · {self.strategy.name}",
            f"Equity: {self.last_equity:,.2f}" if self.last_equity is not None else "Equity: n/a",
            f"Price: {self.last_price:.4f}" if self.last_price is not None else "Price: n/a",
        ]
        if self.market_open is not None:
            lines.append(f"Market: {'open' if self.market_open else 'closed'}")
        if pos.is_open:
            upnl = (self.last_price - pos.entry_price) * pos.qty if self.last_price else 0.0
            lines.append(f"Position: {pos.qty:.6g} @ {pos.entry_price:.4f} · stop {pos.stop:.4f} · uPnL {upnl:+,.2f}")
        else:
            lines.append("Position: flat")
        wins = sum(t.pnl > 0 for t in trades)
        lines.append(f"Closed trades: {len(trades)} ({wins} wins) · realised PnL {pnl:+,.2f}")
        if self.scanner and self.scanner.results:
            best = self.scanner.best()
            lines.append(f"Scanner: {len(self.scanner.results)} stocks, best {best.symbol if best else 'none'}")
        if self.risk.state.halted:
            lines.append(f"🛑 HALTED: {self.risk.state.halt_reason}")
        return "\n".join(lines)

    def _cmd_stop(self) -> str:
        self.stop_file.touch()
        return "🛑 Stop requested: the bot will close its position and shut down within one poll."

    # ---- main loop --------------------------------------------------------------
    def _handle_signal(self, signum, _frame):
        log.info("Received signal %s, shutting down after this iteration", signum)
        self.running = False

    def run(self, max_iterations: int | None = None) -> None:
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)
        log.info("Starting %s: %s %s with %r", self.label, "scanner" if self.scanner else self.symbol,
                 self.cfg.exchange.timeframe, self.strategy)
        if self.cfg.telegram.commands:
            commands = {
                "status": self.status_text, "stop": self._cmd_stop,
                "help": lambda: "/status – account & position\n/stop – close position and shut down"
                                + ("\n/scan – latest stock ranking" if self.scanner else ""),
            }
            if self.scanner:
                commands["scan"] = self.scanner.summary
            self.notifier.start_commands(commands)
        try:
            self.reconcile()
        except Exception as e:
            log.warning("Could not reconcile position at startup: %s", e)
        what = f"best of {len(self.scanner.universe)} stocks" if self.scanner else self.symbol
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
        return True if check is None else bool(check(self.symbol))

    def _heartbeat(self) -> None:
        hours = self.cfg.telegram.heartbeat_hours
        if hours and time.monotonic() - self._last_heartbeat >= hours * 3600:
            self._last_heartbeat = time.monotonic()
            self.notifier.send(self.status_text())

    def step(self) -> None:
        now = datetime.now(timezone.utc)
        price = self.broker.last_price()
        equity = self.trader.equity(price)
        self.last_price, self.last_equity = price, equity
        was_halted = self.risk.state.halted
        self.risk.update_equity(equity, now)
        if self.risk.state.halted and not was_halted:
            self.notifier.send(f"🛑 KILL SWITCH: {self.risk.state.halt_reason}\n"
                               "Trading halted. Review, then run `python -m tradingbot reset-halt`.")
        self._heartbeat()

        stop_requested = self.stop_file.exists()
        self.market_open = self._is_market_open()
        if not self.market_open:
            if stop_requested and not self.trader.position.is_open:
                self.running = False
            elif stop_requested:
                log.info("STOP requested; waiting for the market to open to sell")
            self.save_state()
            return

        if stop_requested:
            log.warning("STOP file found: flattening and shutting down")
            if self.trader.position.is_open:
                self.trader.exit(price, now, "manual_stop")
            self.running = False
            self.save_state()
            return

        if self.risk.state.halted and self.trader.position.is_open:
            self.trader.exit(price, now, "kill_switch")

        self.trader.check_exits(price, price, price, now)

        if self.scanner and not self.trader.position.is_open and not self.risk.state.halted and self.scanner.due():
            if self._rescan():
                price = self.broker.last_price()

        candles = fetch_ohlcv(self.market_data, self.symbol,
                              self.cfg.exchange.timeframe, self.cfg.engine.history_bars + 1)
        candles = drop_open_candle(candles, self.cfg.exchange.timeframe)
        if candles.empty:
            log.warning("No closed candles received")
            return
        bar_time = str(candles.index[-1])

        if bar_time != self.last_bar:
            atr_value = float(atr(candles, self.cfg.risk.atr_period).iloc[-1])
            target = self.strategy.latest_target(candles)
            self.trader.trail(float(candles["close"].iloc[-1]), atr_value)
            log.info("New bar %s close=%.4f target=%d equity=%.2f stop=%s",
                     bar_time, candles["close"].iloc[-1], target, equity,
                     f"{self.trader.position.stop:.4f}" if self.trader.position.is_open else "-")
            self.trader.rebalance(target, atr_value, price, now)
            self.last_bar = bar_time

        self.save_state()

    def _rescan(self) -> bool:
        """Rank the universe and switch to the best stock. Returns True if the symbol changed."""
        results = self.scanner.scan()
        best = self.scanner.best()
        log.info("Scanned %d stocks; best: %s", len(results),
                 f"{best.symbol} (score {best.score:+.2f}, {best.momentum:+.1%})" if best else "none eligible")
        if best is None or best.symbol == self.symbol:
            return False
        old = self.symbol
        self.broker.set_symbol(best.symbol)
        self.last_bar = ""  # act on the new stock's latest candle right away
        self.trader.position.wait_for_reset = False  # that flag was about the old stock
        self.notifier.send(f"🔎 Switching {old} → {best.symbol} (score {best.score:+.2f}, "
                           f"{best.momentum:+.1%} over {self.cfg.scanner.lookback_bars} bars)")
        self.save_state()
        return True
