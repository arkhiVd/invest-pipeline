"""Deterministic swing-trading primitives (research only, never execution)."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum

DecimalLike = Decimal | int | float | str


class EmaState(StrEnum):
    UNKNOWN = "unknown"
    BEARISH = "bearish"
    NEUTRAL = "neutral"
    BULLISH = "bullish"


class CrossoverSignal(StrEnum):
    NONE = "none"
    ENTER = "enter"
    EXIT = "exit"


@dataclass(frozen=True)
class EmaPoint:
    index: int
    close: float
    fast_ema: float | None
    slow_ema: float | None
    state: EmaState
    signal: CrossoverSignal


@dataclass(frozen=True)
class PositionSize:
    capital: Decimal
    entry_price: Decimal
    stop_price: Decimal
    risk_fraction: Decimal
    risk_budget: Decimal
    risk_per_share: Decimal
    risk_limited_quantity: int
    affordable_quantity: int
    quantity: int
    capital_to_deploy: Decimal
    maximum_loss_at_stop: Decimal


def _prices(values: Iterable[float]) -> list[float]:
    result = [float(value) for value in values]
    if any(not math.isfinite(value) or value <= 0 for value in result):
        raise ValueError("prices must be finite and positive")
    return result


def ema_series(values: Iterable[float], period: int) -> list[float | None]:
    """EMA with an SMA seed after ``period`` observations.

    This explicit seed avoids library-dependent warm-up behavior. Values before
    the seed are unavailable rather than silently approximated.
    """
    if isinstance(period, bool) or not isinstance(period, int) or period < 1:
        raise ValueError("EMA period must be a positive integer")
    prices = _prices(values)
    output: list[float | None] = [None] * len(prices)
    if len(prices) < period:
        return output
    seed = sum(prices[:period]) / period
    output[period - 1] = seed
    alpha = 2.0 / (period + 1.0)
    previous = seed
    for index in range(period, len(prices)):
        previous = alpha * prices[index] + (1.0 - alpha) * previous
        output[index] = previous
    return output


def _state(fast: float | None, slow: float | None) -> EmaState:
    if fast is None or slow is None:
        return EmaState.UNKNOWN
    if fast > slow:
        return EmaState.BULLISH
    if fast < slow:
        return EmaState.BEARISH
    return EmaState.NEUTRAL


def ema_crossover(
    closes: Iterable[float], *, fast_period: int = 10, slow_period: int = 21
) -> list[EmaPoint]:
    """Return close-confirmed EMA states and new crossover signals.

    No signal is emitted at the first point where both EMAs become available:
    a pre-existing bullish relationship is not a newly observed crossover.
    Equality is neutral and arms a subsequent strict cross.
    """
    if fast_period >= slow_period:
        raise ValueError("fast EMA period must be shorter than slow EMA period")
    prices = _prices(closes)
    fast_values = ema_series(prices, fast_period)
    slow_values = ema_series(prices, slow_period)
    points: list[EmaPoint] = []
    prior_comparable: tuple[float, float] | None = None
    for index, (close, fast, slow) in enumerate(zip(prices, fast_values, slow_values, strict=True)):
        signal = CrossoverSignal.NONE
        if fast is not None and slow is not None:
            if prior_comparable is not None:
                prior_fast, prior_slow = prior_comparable
                if prior_fast <= prior_slow and fast > slow:
                    signal = CrossoverSignal.ENTER
                elif prior_fast >= prior_slow and fast < slow:
                    signal = CrossoverSignal.EXIT
            prior_comparable = (fast, slow)
        points.append(
            EmaPoint(
                index=index,
                close=close,
                fast_ema=fast,
                slow_ema=slow,
                state=_state(fast, slow),
                signal=signal,
            )
        )
    return points


def _decimal(value: DecimalLike, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


def position_size(
    capital: DecimalLike,
    entry_price: DecimalLike,
    stop_price: DecimalLike,
    *,
    risk_fraction: DecimalLike = Decimal("0.02"),
) -> PositionSize:
    """Size a long trade by account risk, capped at available cash.

    Quantity is ``floor(capital * risk_fraction / (entry - stop))`` and is
    additionally capped so capital deployed cannot exceed account capital.
    This models no leverage and whole NSE equity shares only.
    """
    capital = _decimal(capital, "capital")
    entry = _decimal(entry_price, "entry_price")
    stop = _decimal(stop_price, "stop_price")
    risk = _decimal(risk_fraction, "risk_fraction")
    if capital <= 0 or entry <= 0 or stop <= 0:
        raise ValueError("capital, entry, and stop must be positive")
    if stop >= entry:
        raise ValueError("long-trade stop must be below entry")
    if risk <= 0 or risk > 1:
        raise ValueError("risk_fraction must be in (0, 1]")

    risk_budget = capital * risk
    risk_per_share = entry - stop
    risk_quantity = int((risk_budget / risk_per_share).to_integral_value(rounding=ROUND_FLOOR))
    affordable_quantity = int((capital / entry).to_integral_value(rounding=ROUND_FLOOR))
    quantity = min(risk_quantity, affordable_quantity)
    return PositionSize(
        capital=capital,
        entry_price=entry,
        stop_price=stop,
        risk_fraction=risk,
        risk_budget=risk_budget,
        risk_per_share=risk_per_share,
        risk_limited_quantity=risk_quantity,
        affordable_quantity=affordable_quantity,
        quantity=quantity,
        capital_to_deploy=entry * quantity,
        maximum_loss_at_stop=risk_per_share * quantity,
    )
