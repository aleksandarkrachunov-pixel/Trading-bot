import pandas as pd
import pytest

from tradingbot.data import synthetic_ohlcv
from tradingbot.strategies import STRATEGIES, create_strategy


@pytest.mark.parametrize("name", sorted(STRATEGIES))
def test_no_lookahead(name):
    """Signals for past bars must not change when future bars arrive."""
    df = synthetic_ohlcv(1200, seed=7)
    strat = create_strategy(name)
    full = strat.target_positions(df)
    for cut in (400, 700, 1000):
        partial = strat.target_positions(df.iloc[:cut])
        pd.testing.assert_series_equal(partial, full.iloc[:cut], check_names=False)


@pytest.mark.parametrize("name", sorted(STRATEGIES))
def test_targets_are_binary(name):
    df = synthetic_ohlcv(800)
    out = create_strategy(name).target_positions(df)
    assert set(out.unique()) <= {0, 1}


def test_latest_target_needs_warmup():
    df = synthetic_ohlcv(30)
    assert create_strategy("sma_crossover").latest_target(df) == 0


def test_invalid_params():
    with pytest.raises(ValueError):
        create_strategy("sma_crossover", {"fast": 50, "slow": 20})
    with pytest.raises(ValueError):
        create_strategy("sma_crossover", {"nope": 1})
    with pytest.raises(ValueError):
        create_strategy("does_not_exist")
