"""Strategy registry tests."""

from __future__ import annotations

import pytest

from app.strategies.ema_cross import EmaCrossoverRegimeFilter
from app.strategies.orb import OpeningRangeBreakout
from app.strategies.registry import STRATEGIES, create_strategy
from app.strategies.rsi2 import Rsi2MeanReversion
from app.strategies.vwap_reversion import VwapReversionStrategy


def test_all_four_starter_strategies_registered():
    assert set(STRATEGIES) == {"orb", "vwap_reversion", "ema_cross", "rsi2"}


def test_create_strategy_returns_the_right_class():
    assert isinstance(create_strategy("orb"), OpeningRangeBreakout)
    assert isinstance(create_strategy("vwap_reversion"), VwapReversionStrategy)
    assert isinstance(create_strategy("ema_cross"), EmaCrossoverRegimeFilter)
    assert isinstance(create_strategy("rsi2"), Rsi2MeanReversion)


def test_create_strategy_applies_param_overrides():
    strat = create_strategy("orb", {"min_relative_volume": 2.0})
    assert strat.params["min_relative_volume"] == 2.0
    assert strat.params["min_range_atr_mult"] == strat.default_params["min_range_atr_mult"]


def test_create_strategy_rejects_unknown_slug():
    with pytest.raises(ValueError, match="unknown strategy slug"):
        create_strategy("not_a_real_strategy")
