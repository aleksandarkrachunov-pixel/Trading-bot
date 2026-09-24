from __future__ import annotations

import numpy as np
import pandas as pd

from ..indicators import rsi, sma
from .base import Strategy


class RsiMeanReversion(Strategy):
    """Buy oversold dips in an uptrend, exit when RSI recovers.

    Enter when RSI < `oversold` and price is above the long-term trend MA
    (avoids catching falling knives in bear markets). Exit when RSI > `exit_level`.
    """

    name = "rsi_mean_reversion"
    default_params = {"rsi_period": 14, "oversold": 30, "exit_level": 55, "trend_period": 200}

    @property
    def warmup_bars(self) -> int:
        return int(max(self.params["trend_period"], self.params["rsi_period"])) + 1

    def target_positions(self, df: pd.DataFrame) -> pd.Series:
        r = rsi(df["close"], int(self.params["rsi_period"]))
        trend = sma(df["close"], int(self.params["trend_period"]))
        entry = (r < self.params["oversold"]) & (df["close"] > trend)
        exit_ = r > self.params["exit_level"]

        # Stateful position: hold from entry until exit.
        state = np.where(entry, 1.0, np.where(exit_, 0.0, np.nan))
        pos = pd.Series(state, index=df.index).ffill().fillna(0).astype(int)
        return pos
