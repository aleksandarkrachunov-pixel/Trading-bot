"""Parameter grid search with an out-of-sample check.

Parameters are ranked on the training slice; the test slice shows whether
the edge survives on unseen data. A big train/test gap means overfitting.
"""
from __future__ import annotations

import itertools
import logging

import pandas as pd

from .backtest import run_backtest
from .config import Config
from .strategies import create_strategy

log = logging.getLogger(__name__)


def _parse_value(v: str):
    for cast in (int, float):
        try:
            return cast(v)
        except ValueError:
            pass
    return {"true": True, "false": False}.get(v.lower(), v)


def parse_grid(spec: str) -> dict[str, list]:
    """'fast=10,20 slow=50,100' -> {'fast': [10, 20], 'slow': [50, 100]}"""
    grid = {}
    for part in spec.split():
        key, values = part.split("=", 1)
        grid[key] = [_parse_value(v) for v in values.split(",")]
    return grid


def grid_search(df: pd.DataFrame, cfg: Config, strategy_name: str, grid: dict[str, list],
                metric: str = "sharpe", train_frac: float = 0.7) -> pd.DataFrame:
    split = int(len(df) * train_frac)
    train, test = df.iloc[:split], df.iloc[split:]
    rows = []
    keys = list(grid)
    for combo in itertools.product(*(grid[k] for k in keys)):
        params = {**cfg.strategy.params, **dict(zip(keys, combo))}
        try:
            strategy = create_strategy(strategy_name, params)
            tr = run_backtest(train, strategy, cfg).stats
            te = run_backtest(test, strategy, cfg).stats
        except ValueError as e:
            log.debug("Skipping %s: %s", params, e)
            continue
        rows.append({
            **dict(zip(keys, combo)),
            f"train_{metric}": tr[metric], "train_return": tr["total_return"],
            f"test_{metric}": te[metric], "test_return": te["total_return"],
            "test_max_dd": te["max_drawdown"], "test_trades": te["num_trades"],
        })
    if not rows:
        raise ValueError("No valid parameter combinations")
    return pd.DataFrame(rows).sort_values(f"train_{metric}", ascending=False).reset_index(drop=True)
