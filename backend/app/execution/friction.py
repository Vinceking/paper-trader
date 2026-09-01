"""The friction model. BUILD_SPEC §9.

Alpaca's paper endpoint does not charge market impact, latency slippage,
queue position, borrow fees, dividends, or regulatory fees, and it fills at
the quoted bid/ask without validating size against real liquidity. Left
alone, that is an optimistically biased simulator. This module corrects for
it, and the same code path is used by both live paper trading and the
backtester (CLAUDE.md rule 3) — there is no "clean mode".

Every component here is a small, pure, independently hand-computable
function on purpose: the Phase 2 acceptance criteria require unit tests
against hand-computed values, and that's only tractable if each cost is
isolated rather than buried inside one large formula.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.market_calendar import in_open_or_close_window


@dataclass(frozen=True)
class FrictionConfig:
    # Half-spread paid on market orders, widened for IEX-only data (§2.3).
    spread_multiplier: Decimal = Decimal("1.5")
    min_spread_bps: Decimal = Decimal("2.0")
    # Slippage as a fraction of ATR, scaled by order size vs. typical volume.
    slippage_atr_frac: Decimal = Decimal("0.05")
    # Commission: $0 at most retail brokers, but keep the hook.
    commission_per_share: Decimal = Decimal("0.0")
    commission_min: Decimal = Decimal("0.0")
    # Regulatory fees — SELLS ONLY. Rates are reset periodically; read them
    # from config rather than trusting these defaults indefinitely.
    # SEC Section 31: $20.60 per $1M of principal, effective April 4, 2026.
    sec_fee_rate: Decimal = Decimal("0.0000206")
    # FINRA Trading Activity Fee: per-share on sales, with a per-trade cap.
    taf_per_share: Decimal = Decimal("0.000166")
    taf_cap: Decimal = Decimal("8.30")
    # Extra penalty for trading in the first/last 5 minutes of the session.
    open_close_penalty_multiplier: Decimal = Decimal("2.0")


@dataclass(frozen=True)
class FrictionInput:
    """Everything apply_friction needs, decoupled from ingest/broker types."""

    side: str  # 'buy' | 'sell'
    qty: Decimal
    ts: datetime  # order timestamp, timezone-aware
    bid: Decimal
    ask: Decimal
    atr: Decimal
    # Typical volume for this symbol at this time of day, for market-impact
    # scaling. Not the same as the bar's own volume — using the bar being
    # traded into would make the impact estimate depend on itself.
    typical_bar_volume: Decimal


@dataclass(frozen=True)
class FillResult:
    qty: Decimal
    reference_price: Decimal  # mid at decision time
    fill_price: Decimal  # after slippage
    slippage_cost: Decimal
    spread_cost: Decimal
    commission: Decimal
    reg_fees: Decimal

    @property
    def total_friction(self) -> Decimal:
        return self.slippage_cost + self.spread_cost + self.commission + self.reg_fees


def mid_price(bid: Decimal, ask: Decimal) -> Decimal:
    return (bid + ask) / Decimal(2)


def half_spread_per_share(bid: Decimal, ask: Decimal, cfg: FrictionConfig) -> Decimal:
    """Half the effective spread, widened by `spread_multiplier`.

    The effective spread floors at `min_spread_bps` of mid so a razor-thin
    quoted spread (or a stale/locked one) never understates the real cost.
    """
    mid = mid_price(bid, ask)
    spread = max(ask - bid, mid * cfg.min_spread_bps / Decimal(10_000))
    return (spread / Decimal(2)) * cfg.spread_multiplier


def slippage_per_share(
    atr: Decimal, qty: Decimal, typical_bar_volume: Decimal, cfg: FrictionConfig,
) -> Decimal:
    """Market-impact slippage: a fraction of ATR, scaled by order size.

    `size_factor` is 0 for a vanishingly small order and 1 once the order is
    at least as large as typical volume for the bar. A non-positive
    `typical_bar_volume` (no liquidity data) is treated as the worst case
    (size_factor = 1) rather than dividing by zero.
    """
    if typical_bar_volume > 0:
        size_factor = min(qty / typical_bar_volume, Decimal(1))
    else:
        size_factor = Decimal(1)
    return atr * cfg.slippage_atr_frac * (Decimal("0.5") + size_factor)


def commission_cost(qty: Decimal, cfg: FrictionConfig) -> Decimal:
    return max(qty * cfg.commission_per_share, cfg.commission_min)


def sec_fee(principal: Decimal, cfg: FrictionConfig) -> Decimal:
    """SEC Section 31 fee. Sells only — callers must not apply this to buys."""
    return principal * cfg.sec_fee_rate


def finra_taf(qty: Decimal, cfg: FrictionConfig) -> Decimal:
    """FINRA Trading Activity Fee. Sells only, capped per trade."""
    return min(qty * cfg.taf_per_share, cfg.taf_cap)


def apply_friction(inp: FrictionInput, cfg: FrictionConfig | None = None) -> FillResult:
    cfg = cfg or FrictionConfig()

    mid = mid_price(inp.bid, inp.ask)
    half_spread = half_spread_per_share(inp.bid, inp.ask, cfg)
    slip = slippage_per_share(inp.atr, inp.qty, inp.typical_bar_volume, cfg)

    if in_open_or_close_window(inp.ts):
        half_spread *= cfg.open_close_penalty_multiplier
        slip *= cfg.open_close_penalty_multiplier

    direction = Decimal(1) if inp.side == "buy" else Decimal(-1)
    fill_price = mid + direction * (half_spread + slip)

    reg_fees = Decimal(0)
    if inp.side == "sell":
        principal = fill_price * inp.qty
        reg_fees = sec_fee(principal, cfg) + finra_taf(inp.qty, cfg)

    return FillResult(
        qty=inp.qty,
        reference_price=mid,
        fill_price=fill_price,
        slippage_cost=slip * inp.qty,
        spread_cost=half_spread * inp.qty,
        commission=commission_cost(inp.qty, cfg),
        reg_fees=reg_fees,
    )
