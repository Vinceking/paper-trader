"""The strategy registry. BUILD_SPEC §6 repo structure.

Maps each strategy's `slug` (also the value stored in `strategies.slug`) to
its class, so a `StrategyRecord` row can be turned into a running `Strategy`
instance without a chain of if/elif on the slug string anywhere else in the
codebase.
"""

from __future__ import annotations

from app.strategies.base import Strategy
from app.strategies.ema_cross import EmaCrossoverRegimeFilter
from app.strategies.orb import OpeningRangeBreakout
from app.strategies.rsi2 import Rsi2MeanReversion
from app.strategies.vwap_reversion import VwapReversionStrategy

STRATEGIES: dict[str, type[Strategy]] = {
    OpeningRangeBreakout.slug: OpeningRangeBreakout,
    VwapReversionStrategy.slug: VwapReversionStrategy,
    EmaCrossoverRegimeFilter.slug: EmaCrossoverRegimeFilter,
    Rsi2MeanReversion.slug: Rsi2MeanReversion,
}


def create_strategy(slug: str, params: dict | None = None) -> Strategy:
    try:
        strategy_cls = STRATEGIES[slug]
    except KeyError:
        raise ValueError(
            f"unknown strategy slug {slug!r}; known slugs: {sorted(STRATEGIES)}"
        ) from None
    return strategy_cls(params)
