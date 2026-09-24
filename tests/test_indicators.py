import numpy as np
import pandas as pd

from tradingbot.data import synthetic_ohlcv
from tradingbot.indicators import atr, rsi, sma


def test_sma_basic():
    s = pd.Series([1, 2, 3, 4, 5], dtype=float)
    out = sma(s, 3)
    assert out.isna().sum() == 2
    assert out.iloc[-1] == 4


def test_rsi_bounds_and_extremes():
    up = pd.Series(np.arange(1, 50, dtype=float))
    assert rsi(up, 14).dropna().eq(100).all()
    df = synthetic_ohlcv(500)
    r = rsi(df["close"]).dropna()
    assert ((r >= 0) & (r <= 100)).all()


def test_indicators_are_causal():
    """Appending future bars must not change past indicator values."""
    df = synthetic_ohlcv(400)
    a_short = atr(df.iloc[:300]).dropna()
    a_long = atr(df).loc[a_short.index]
    pd.testing.assert_series_equal(a_short, a_long)
    r_short = rsi(df["close"].iloc[:300]).dropna()
    pd.testing.assert_series_equal(r_short, rsi(df["close"]).loc[r_short.index])
