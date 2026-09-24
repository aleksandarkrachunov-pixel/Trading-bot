"""Live / paper trading loop.

Every `poll_seconds`:
  1. fetch the latest price and update equity / circuit breakers
  2. check stop-loss and take-profit against the latest price
  3. when a new candle has closed: run the strategy, trail the stop, rebalance
  4. persist state to disk so a restart resumes the same position

Create a file named STOP in the state directory to flatten and shut down.
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
from .risk import RiskManager, RiskState
from .strategies import Strategy
from .trader import Trader

log = logging.getLogger(__name__)


class Engine:
    def __init__(self, cfg: Config, strategy: Strategy, broker: Broker, market_data):
        self.cfg = cfg
        self.strategy = strategy
        self.broker = broker
        self.market_data = market_data  # ccxt exchange used for candles
        self.risk = RiskManager(cfg.risk)
        self.trader = Trader(broker, self.risk)
        self.last_bar: str = ""
        self.running = True

        safe_symbol = cfg.exchange.symbol.replace("/", "-")
        self.state_dir = Path(cfg.engine.state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.state_dir / f"{cfg.engine.mode}_{cfg.exchange.name}_{safe_symbol}.json"
        self.stop_file = self.state_dir / "STOP"
        self.load_state()

    # ---- persistence ----------------------------------------------------------
    def load_state(self) -> None:
        if not self.state_file.exists():
            return
        d = json.loads(self.state_file.read_text())
        self.risk.state = RiskState.from_dict(d["risk"])
        self.trader.load_dict(d["trader"])
        self.last_bar = d.get("last_bar", "")
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
        }
        if isinstance(self.broker, PaperBroker):
            d["paper"] = self.broker.to_dict()
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=2, default=str))
        os.replace(tmp, self.state_file)  # atomic

    # ---- main loop --------------------------------------------------------------
    def _handle_signal(self, signum, _frame):
        log.info("Received signal %s, shutting down after this iteration", signum)
        self.running = False

    def run(self, max_iterations: int | None = None) -> None:
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)
        log.info("Starting %s trading: %s %s %s with %r",
                 self.cfg.engine.mode.upper(), self.cfg.exchange.name,
                 self.cfg.exchange.symbol, self.cfg.exchange.timeframe, self.strategy)
        iterations = 0
        while self.running:
            try:
                self.step()
            except Exception:  # keep the bot alive; state is saved each step
                log.exception("Error in trading step")
            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                break
            self._sleep(self.cfg.engine.poll_seconds)
        self.save_state()
        log.info("Engine stopped")

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while self.running and time.monotonic() < end:
            time.sleep(min(1.0, end - time.monotonic()))

    def step(self) -> None:
        now = datetime.now(timezone.utc)
        price = self.broker.last_price()
        equity = self.trader.equity(price)
        self.risk.update_equity(equity, now)

        if self.stop_file.exists():
            log.warning("STOP file found: flattening and shutting down")
            if self.trader.position.is_open:
                self.trader.exit(price, now, "manual_stop")
            self.running = False
            self.save_state()
            return

        if self.risk.state.halted and self.trader.position.is_open:
            self.trader.exit(price, now, "kill_switch")

        self.trader.check_exits(price, price, price, now)

        candles = fetch_ohlcv(self.market_data, self.cfg.exchange.symbol,
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
