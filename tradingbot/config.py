"""Configuration loading: YAML file + environment variables for secrets."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml


@dataclass
class ExchangeConfig:
    name: str = "binance"
    symbol: str = "BTC/USDT"
    timeframe: str = "1h"
    sandbox: bool = False  # use the exchange's testnet if it has one


@dataclass
class StrategyConfig:
    name: str = "sma_crossover"
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class RiskConfig:
    risk_per_trade: float = 0.01       # fraction of equity lost if stop is hit
    max_position_pct: float = 0.95     # cap on position value / equity
    atr_period: int = 14
    stop_atr_multiple: float = 2.0     # stop distance = ATR * multiple
    take_profit_atr_multiple: float | None = None  # None disables take-profit
    trailing_stop: bool = True
    max_drawdown: float = 0.20         # halt trading when equity falls this far from peak
    daily_loss_limit: float = 0.05     # no new entries after losing this much in a UTC day
    min_order_value: float = 10.0      # skip orders smaller than this (quote currency)


@dataclass
class BacktestConfig:
    initial_cash: float = 10_000.0
    fee_rate: float = 0.001            # 0.1% per side
    slippage: float = 0.0005           # 0.05% adverse fill


@dataclass
class EngineConfig:
    mode: str = "paper"                # paper | live
    poll_seconds: int = 30
    history_bars: int = 300
    state_dir: str = "state"
    log_dir: str = "logs"


@dataclass
class Trading212Config:
    environment: str = "demo"          # demo (practice money) | live (real money)
    data_symbol: str = ""              # Yahoo Finance symbol for prices; derived from the ticker if empty
    extended_hours: bool = False
    quantity_decimals: int = 2         # fractional share precision accepted for your instrument
    order_timeout: int = 60            # seconds to wait for a fill before cancelling


@dataclass
class ScannerConfig:
    enabled: bool = False              # trade the best stock from `universe` instead of exchange.symbol
    universe: list[str] = field(default_factory=list)  # tickers to scan; empty = built-in large-cap US list
    lookback_bars: int = 120           # momentum window, in candles of exchange.timeframe
    rescan_minutes: float = 60         # how often to rescan while flat
    min_price: float = 5.0             # ignore penny stocks
    request_delay: float = 0.3         # seconds between price-data requests


@dataclass
class TelegramConfig:
    enabled: bool = False              # needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in the environment
    commands: bool = True              # accept /status and /stop from your chat
    heartbeat_hours: float = 24        # periodic status message; 0 disables
    notify_errors: bool = True


@dataclass
class Config:
    exchange: ExchangeConfig = field(default_factory=ExchangeConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)
    trading212: Trading212Config = field(default_factory=Trading212Config)
    scanner: ScannerConfig = field(default_factory=ScannerConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)

    @property
    def api_key(self) -> str | None:
        return os.environ.get("EXCHANGE_API_KEY") or None

    @property
    def api_secret(self) -> str | None:
        return os.environ.get("EXCHANGE_API_SECRET") or None

    @property
    def api_password(self) -> str | None:
        return os.environ.get("EXCHANGE_API_PASSWORD") or None


def _build(cls, data: dict[str, Any] | None):
    data = data or {}
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"Unknown {cls.__name__} keys: {sorted(unknown)}")
    return cls(**data)


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal .env loader (no extra dependency). Existing env vars win."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_config(path: str | Path | None = None) -> Config:
    load_dotenv()
    raw: dict[str, Any] = {}
    if path:
        with open(path) as fh:
            raw = yaml.safe_load(fh) or {}
    cfg = Config(
        exchange=_build(ExchangeConfig, raw.get("exchange")),
        strategy=_build(StrategyConfig, raw.get("strategy")),
        risk=_build(RiskConfig, raw.get("risk")),
        backtest=_build(BacktestConfig, raw.get("backtest")),
        engine=_build(EngineConfig, raw.get("engine")),
        trading212=_build(Trading212Config, raw.get("trading212")),
        scanner=_build(ScannerConfig, raw.get("scanner")),
        telegram=_build(TelegramConfig, raw.get("telegram")),
    )
    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    r = cfg.risk
    if not 0 < r.risk_per_trade <= 0.1:
        raise ValueError("risk.risk_per_trade must be in (0, 0.1]")
    if not 0 < r.max_position_pct <= 1:
        raise ValueError("risk.max_position_pct must be in (0, 1] (no leverage)")
    if not 0 < r.max_drawdown < 1:
        raise ValueError("risk.max_drawdown must be in (0, 1)")
    if r.stop_atr_multiple <= 0:
        raise ValueError("risk.stop_atr_multiple must be > 0")
    if cfg.engine.mode not in ("paper", "live"):
        raise ValueError("engine.mode must be 'paper' or 'live'")
    if cfg.trading212.environment not in ("demo", "live"):
        raise ValueError("trading212.environment must be 'demo' or 'live'")
    if cfg.scanner.lookback_bars < 2:
        raise ValueError("scanner.lookback_bars must be >= 2")
