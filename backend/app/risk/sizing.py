"""Fixed-fractional position sizing. BUILD_SPEC §7.4.

    risk_dollars = equity * risk_per_trade_pct
    stop_distance = abs(entry_price - stop_price)
    qty = floor(risk_dollars / stop_distance)

Pure and total: a degenerate stop (zero distance) returns 0 shares rather
than raising, so the risk engine can treat "can't be sized" and "sized to
zero" as the same veto without a special case.
"""

from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal


def fixed_fractional_qty(
    equity: Decimal,
    risk_per_trade_pct: Decimal,
    entry_price: Decimal,
    stop_price: Decimal,
) -> Decimal:
    stop_distance = abs(entry_price - stop_price)
    if stop_distance <= 0:
        return Decimal(0)
    risk_dollars = equity * risk_per_trade_pct
    return (risk_dollars / stop_distance).to_integral_value(rounding=ROUND_FLOOR)


def clamp_to_max_position(
    qty: Decimal,
    entry_price: Decimal,
    equity: Decimal,
    max_position_pct: Decimal,
) -> Decimal:
    """Clamp qty down (never up) so notional never exceeds max_position_pct of equity."""
    if entry_price <= 0:
        return qty
    max_notional = equity * max_position_pct
    if qty * entry_price <= max_notional:
        return qty
    return (max_notional / entry_price).to_integral_value(rounding=ROUND_FLOOR)
