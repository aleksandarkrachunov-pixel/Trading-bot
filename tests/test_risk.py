from datetime import datetime, timedelta, timezone

import pytest

from tradingbot.config import RiskConfig
from tradingbot.risk import RiskManager

T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


def test_position_size_risk_based():
    rm = RiskManager(RiskConfig(risk_per_trade=0.01, stop_atr_multiple=2, max_position_pct=1.0))
    # risk 100 on 10k equity; stop distance 2*5=10 -> 10 units
    assert rm.position_size(10_000, 100, 5) == pytest.approx(10)


def test_position_size_capped():
    rm = RiskManager(RiskConfig(risk_per_trade=0.05, stop_atr_multiple=1, max_position_pct=0.5))
    # uncapped would be 500 / 0.1 = 5000 units = 500k notional; cap is 5000 notional
    assert rm.position_size(10_000, 100, 0.1) == pytest.approx(50)


def test_position_size_rejects_bad_inputs():
    rm = RiskManager(RiskConfig())
    assert rm.position_size(10_000, 100, float("nan")) == 0
    assert rm.position_size(10_000, 100, 0) == 0
    assert rm.position_size(5, 100, 1) == 0  # below min_order_value


def test_trailing_stop_only_moves_up():
    rm = RiskManager(RiskConfig(stop_atr_multiple=2))
    assert rm.trail_stop(90, 120, 5) == 110
    assert rm.trail_stop(110, 100, 5) == 110


def test_max_drawdown_kill_switch():
    rm = RiskManager(RiskConfig(max_drawdown=0.2, daily_loss_limit=0.5))
    rm.update_equity(10_000, T0)
    rm.update_equity(12_000, T0)
    rm.update_equity(9_700, T0)
    assert not rm.state.halted
    rm.update_equity(9_500, T0)  # -20.8% from 12k peak
    assert rm.state.halted
    assert not rm.can_open(9_500)[0]
    rm.reset_halt()
    assert rm.can_open(9_500)[0]


def test_daily_loss_limit_resets_next_day():
    rm = RiskManager(RiskConfig(daily_loss_limit=0.05, max_drawdown=0.5))
    rm.update_equity(10_000, T0)
    rm.update_equity(9_400, T0 + timedelta(hours=5))
    assert not rm.can_open(9_400)[0]
    rm.update_equity(9_400, T0 + timedelta(days=1))
    assert rm.can_open(9_400)[0]
