from __future__ import annotations

import pandas as pd

from ..indicators import ema, sma
from .base import Strategy


class SmaCrossover(Strategy):
    """Trend following: long while the fast MA is above the slow MA."""

    name = "sma_crossover"
    default_params = {"fast": 20, "slow": 50, "use_ema": False}

    def __init__(self, **params):
        super().__init__(**params)
        if self.params["fast"] >= self.params["slow"]:
            raise ValueError("fast period must be smaller than slow period")

    @property
    def warmup_bars(self) -> int:
        return int(self.params["slow"]) + 1

    def target_positions(self, df: pd.DataFrame) -> pd.Series:
        ma = ema if self.params["use_ema"] else sma
        fast = ma(df["close"], int(self.params["fast"]))
        slow = ma(df["close"], int(self.params["slow"]))
        pos = (fast > slow).astype(int)
        return pos.where(slow.notna(), 0)
