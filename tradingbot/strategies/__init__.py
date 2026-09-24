from __future__ import annotations

from .base import Strategy
from .rsi_mean_reversion import RsiMeanReversion
from .sma_crossover import SmaCrossover

STRATEGIES: dict[str, type[Strategy]] = {
    SmaCrossover.name: SmaCrossover,
    RsiMeanReversion.name: RsiMeanReversion,
}


def create_strategy(name: str, params: dict | None = None) -> Strategy:
    try:
        cls = STRATEGIES[name]
    except KeyError:
        raise ValueError(f"Unknown strategy '{name}'. Available: {sorted(STRATEGIES)}") from None
    return cls(**(params or {}))


__all__ = ["Strategy", "STRATEGIES", "create_strategy", "SmaCrossover", "RsiMeanReversion"]
