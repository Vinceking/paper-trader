"""The indicator pipeline. BUILD_SPEC §8.4.

Computed once per symbol per bar, shared across every strategy — this is
the "raw material" `signals.features` is built from. No-lookahead by
construction: `compute_indicators` only ever looks at the `history` it is
given and reports values as of the LAST bar in it. The caller is what
guarantees no-lookahead in practice (app/strategies/engine.py only ever
calls this on finalized bars, per BUILD_SPEC §7.2/§7.3) — but even handed a
history that includes future bars by mistake, appending more bars after the
one being evaluated can never change an already-computed snapshot, because
nothing here ever looks past `history[-1]`.

Standard indicators (EMA/SMA/RSI/MACD/ATR/Bollinger/ADX) use
`pandas-ta-classic` (BUILD_SPEC §3) — well-established formulas, so no
reason to hand-roll them. Session/microstructure indicators that library
doesn't cover in a session-aware way (VWAP + bands, opening range, gap,
regime classification) are hand-rolled against `app.market_calendar`'s
session boundaries.

Timeframe-agnostic: this module doesn't know or care whether `history` is
1Min, 5Min, or 1Day bars — it just computes indicators over whatever series
it's handed. Strategies with a daily regime/trend filter (EMA crossover,
RSI(2) — see BUILD_SPEC §8.3) call this twice: once for their own primary
timeframe, once for a daily history. Session-relative fields (VWAP bands,
opening range, gap, minutes_since_open) are only meaningful intraday and
are left `None` when `history` looks like daily bars.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Literal

import pandas as pd
import pandas_ta_classic as ta

from app.ingest.bars import FinalBar
from app.market_calendar import NY, SESSION_OPEN, start_of_trading_day

Regime = Literal["trend_up", "trend_down", "chop"]

# Regime classification needs a 2-point EMA(50) slope, which itself needs
# ~51 bars of warm-up (50 for the EMA plus one more to diff against) — this
# mirrors that real dependency rather than an arbitrary "enough bars" guess.
_MIN_BARS_FOR_REGIME = 51
_ADX_TREND_THRESHOLD = 20.0
_OPENING_RANGE_MINUTES = 15
_VOLUME_LOOKBACK = 20


@dataclass(frozen=True)
class SpreadQuote:
    """Current bid/ask, for spread_bps. Optional — bars alone don't carry it."""

    bid: Decimal
    ask: Decimal


@dataclass(frozen=True)
class IndicatorSnapshot:
    ema_9: float | None
    ema_21: float | None
    ema_50: float | None
    ema_200: float | None
    sma_5: float | None
    sma_20: float | None
    sma_200: float | None
    rsi_2: float | None
    rsi_14: float | None
    macd_line: float | None
    macd_signal: float | None
    macd_hist: float | None
    atr_14: float | None
    bb_basis: float | None
    bb_upper: float | None
    bb_lower: float | None
    # Session VWAP + volume-weighted std bands. None on non-intraday history.
    vwap: float | None
    vwap_upper_1: float | None
    vwap_lower_1: float | None
    vwap_upper_2: float | None
    vwap_lower_2: float | None
    volume_zscore_20: float | None
    relative_volume_20: float | None
    opening_range_high: float | None
    opening_range_low: float | None
    gap_pct: float | None
    spread_bps: float | None
    minutes_since_open: float | None
    adx_14: float | None
    regime: Regime | None


def _last_or_none(result) -> float | None:
    """pandas-ta returns None outright when history is too short, otherwise
    a Series/DataFrame whose tail may still be NaN during warm-up."""
    if result is None:
        return None
    value = result.iloc[-1]
    if isinstance(value, pd.Series):  # shouldn't happen for our calls, but be safe
        return None
    return None if pd.isna(value) else float(value)


def _to_frame(history: Sequence[FinalBar]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [float(b.open) for b in history],
            "high": [float(b.high) for b in history],
            "low": [float(b.low) for b in history],
            "close": [float(b.close) for b in history],
            "volume": [float(b.volume) for b in history],
        },
        index=pd.DatetimeIndex([b.ts for b in history]),
    )


def _is_intraday(history: Sequence[FinalBar]) -> bool:
    """Heuristic: bars less than a day apart. Daily-bar strategies (RSI(2))
    get None for session-relative fields rather than a nonsensical value."""
    return history[-1].timeframe != "1Day"


def _session_slice(history: Sequence[FinalBar]) -> list[FinalBar]:
    """Bars belonging to the same trading day as the last bar, earliest first."""
    last = history[-1]
    day_start = start_of_trading_day(last.ts)
    day_end = day_start + timedelta(days=1)
    return [b for b in history if day_start <= b.ts < day_end]


def _session_vwap_and_bands(session_bars: list[FinalBar]) -> tuple[float | None, ...]:
    if not session_bars:
        return (None, None, None, None, None)

    typical = [float((b.high + b.low + b.close) / 3) for b in session_bars]
    volume = [float(b.volume) for b in session_bars]
    total_volume = sum(volume)
    if total_volume <= 0:
        return (None, None, None, None, None)

    pairs = list(zip(typical, volume, strict=True))
    vwap = sum(tp * v for tp, v in pairs) / total_volume
    variance = sum(v * (tp - vwap) ** 2 for tp, v in pairs) / total_volume
    std = math.sqrt(variance)

    return (vwap, vwap + std, vwap - std, vwap + 2 * std, vwap - 2 * std)


def _opening_range(session_bars: list[FinalBar]) -> tuple[float | None, float | None]:
    if not session_bars:
        return (None, None)
    day_start = start_of_trading_day(session_bars[-1].ts)
    window_end = day_start.astimezone(NY).replace(
        hour=SESSION_OPEN.hour, minute=SESSION_OPEN.minute,
    ) + timedelta(minutes=_OPENING_RANGE_MINUTES)
    window_bars = [b for b in session_bars if b.ts < window_end]
    if not window_bars:
        return (None, None)
    return (
        float(max(b.high for b in window_bars)),
        float(min(b.low for b in window_bars)),
    )


def _gap_pct(history: Sequence[FinalBar], session_bars: list[FinalBar]) -> float | None:
    if not session_bars:
        return None
    prior_bars = [b for b in history if b.ts < session_bars[0].ts]
    if not prior_bars:
        return None
    prev_close = prior_bars[-1].close
    if prev_close == 0:
        return None
    today_open = session_bars[0].open
    return float((today_open - prev_close) / prev_close * 100)


def _minutes_since_open(session_bars: list[FinalBar]) -> float | None:
    if not session_bars:
        return None
    last = session_bars[-1]
    day_start_ny = start_of_trading_day(last.ts).astimezone(NY)
    open_dt = day_start_ny.replace(hour=SESSION_OPEN.hour, minute=SESSION_OPEN.minute)
    return (last.ts.astimezone(NY) - open_dt).total_seconds() / 60


def _classify_regime(adx_14: float | None, ema_50_series) -> Regime | None:
    if adx_14 is None or ema_50_series is None or len(ema_50_series.dropna()) < 2:
        return None
    recent = ema_50_series.dropna()
    slope = recent.iloc[-1] - recent.iloc[-2]
    if adx_14 < _ADX_TREND_THRESHOLD:
        return "chop"
    return "trend_up" if slope > 0 else "trend_down"


def compute_indicators(
    history: Sequence[FinalBar],
    quote: SpreadQuote | None = None,
) -> IndicatorSnapshot:
    if not history:
        raise ValueError("compute_indicators requires at least one bar")

    df = _to_frame(history)
    close, high, low, volume = df["close"], df["high"], df["low"], df["volume"]

    ema_9 = _last_or_none(ta.ema(close, length=9))
    ema_21 = _last_or_none(ta.ema(close, length=21))
    ema_50_series = ta.ema(close, length=50)
    ema_50 = _last_or_none(ema_50_series)
    ema_200 = _last_or_none(ta.ema(close, length=200))

    sma_5 = _last_or_none(ta.sma(close, length=5))
    sma_20 = _last_or_none(ta.sma(close, length=20))
    sma_200 = _last_or_none(ta.sma(close, length=200))

    rsi_2 = _last_or_none(ta.rsi(close, length=2))
    rsi_14 = _last_or_none(ta.rsi(close, length=14))

    macd_df = ta.macd(close, fast=12, slow=26, signal=9)
    if macd_df is None:
        macd_line = macd_signal = macd_hist = None
    else:
        macd_line = _last_or_none(macd_df["MACD_12_26_9"])
        macd_signal = _last_or_none(macd_df["MACDs_12_26_9"])
        macd_hist = _last_or_none(macd_df["MACDh_12_26_9"])

    atr_14 = _last_or_none(ta.atr(high, low, close, length=14))

    bb_df = ta.bbands(close, length=20, std=2)
    if bb_df is None:
        bb_basis = bb_upper = bb_lower = None
    else:
        bb_basis = _last_or_none(bb_df["BBM_20_2.0"])
        bb_upper = _last_or_none(bb_df["BBU_20_2.0"])
        bb_lower = _last_or_none(bb_df["BBL_20_2.0"])

    adx_df = ta.adx(high, low, close, length=14)
    adx_14 = None if adx_df is None else _last_or_none(adx_df["ADX_14"])

    volume_roll = volume.rolling(_VOLUME_LOOKBACK)
    vol_mean = volume_roll.mean().iloc[-1]
    vol_std = volume_roll.std().iloc[-1]
    volume_zscore_20 = (
        None
        if pd.isna(vol_mean) or pd.isna(vol_std) or vol_std == 0
        else float((volume.iloc[-1] - vol_mean) / vol_std)
    )
    relative_volume_20 = (
        None if pd.isna(vol_mean) or vol_mean == 0 else float(volume.iloc[-1] / vol_mean)
    )

    intraday = _is_intraday(history)
    session_bars = _session_slice(history) if intraday else []

    vwap, vwap_u1, vwap_l1, vwap_u2, vwap_l2 = (
        _session_vwap_and_bands(session_bars) if intraday else (None, None, None, None, None)
    )
    or_high, or_low = _opening_range(session_bars) if intraday else (None, None)
    gap_pct = _gap_pct(history, session_bars) if intraday else None
    minutes_since_open = _minutes_since_open(session_bars) if intraday else None

    spread_bps = None
    if quote is not None and quote.bid > 0 and quote.ask > 0:
        mid = (quote.bid + quote.ask) / Decimal(2)
        spread_bps = float((quote.ask - quote.bid) / mid * Decimal(10_000))

    regime = (
        _classify_regime(adx_14, ema_50_series) if len(history) >= _MIN_BARS_FOR_REGIME else None
    )

    return IndicatorSnapshot(
        ema_9=ema_9, ema_21=ema_21, ema_50=ema_50, ema_200=ema_200,
        sma_5=sma_5, sma_20=sma_20, sma_200=sma_200,
        rsi_2=rsi_2, rsi_14=rsi_14,
        macd_line=macd_line, macd_signal=macd_signal, macd_hist=macd_hist,
        atr_14=atr_14,
        bb_basis=bb_basis, bb_upper=bb_upper, bb_lower=bb_lower,
        vwap=vwap, vwap_upper_1=vwap_u1, vwap_lower_1=vwap_l1,
        vwap_upper_2=vwap_u2, vwap_lower_2=vwap_l2,
        volume_zscore_20=volume_zscore_20, relative_volume_20=relative_volume_20,
        opening_range_high=or_high, opening_range_low=or_low,
        gap_pct=gap_pct, spread_bps=spread_bps,
        minutes_since_open=minutes_since_open,
        adx_14=adx_14, regime=regime,
    )
