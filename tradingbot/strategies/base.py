"""Strategy interface.

A strategy turns a DataFrame of *closed* OHLCV candles into a series of target
positions: 1 = be long, 0 = be flat. The value at bar i may only use data up
to and including bar i. The backtester acts on it at the next bar's open and
the live engine acts on it immediately after the candle closes, so both see
exactly the same decisions.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import pandas as pd


class Strategy(ABC):
    name: str = "base"
    default_params: dict[str, Any] = {}

    def __init__(self, **params: Any):
        unknown = set(params) - set(self.default_params)
        if unknown:
            raise ValueError(f"{self.name}: unknown params {sorted(unknown)}")
        self.params = {**self.default_params, **params}

    @property
    def warmup_bars(self) -> int:
        """Minimum number of bars before signals are meaningful."""
        return 50

    @abstractmethod
    def target_positions(self, df: pd.DataFrame) -> pd.Series:
        """Return a Series (same index as df) of 0/1 target positions."""

    def latest_target(self, df: pd.DataFrame) -> int:
        if len(df) < self.warmup_bars:
            return 0
        value = self.target_positions(df).iloc[-1]
        return 0 if pd.isna(value) else int(value)

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.params})"
