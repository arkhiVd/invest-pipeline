"""Transparent synthetic-first oracle for the approved 10/21 EMA research rule."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from hashlib import sha256

from invest import execution_costs

METHODOLOGY = "ema-10-21-close-confirmed-2026.1"
FAST = 10
SLOW = 21


class OracleError(ValueError):
    pass


@dataclass(frozen=True)
class Bar:
    session: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    adjustment_status: str = "SYNTHETIC_ADJUSTED"


@dataclass(frozen=True)
class EmaPoint:
    session: date
    close: Decimal
    ema10: Decimal | None
    ema21: Decimal | None


def _ema(values: list[Decimal], period: int) -> list[Decimal | None]:
    result: list[Decimal | None] = [None] * len(values)
    if len(values) < period:
        return result
    seed = sum(values[:period], Decimal("0")) / Decimal(period)
    result[period - 1] = seed
    alpha = Decimal("2") / Decimal(period + 1)
    current = seed
    for index in range(period, len(values)):
        current = values[index] * alpha + current * (Decimal("1") - alpha)
        result[index] = current
    return result


def ema_points(bars: list[Bar]) -> list[EmaPoint]:
    closes = [bar.close for bar in bars]
    fast = _ema(closes, FAST)
    slow = _ema(closes, SLOW)
    return [
        EmaPoint(bar.session, bar.close, fast[index], slow[index]) for index, bar in enumerate(bars)
    ]


def _validate_bars(bars: list[Bar], expected_sessions: list[date]) -> None:
    if [bar.session for bar in bars] != expected_sessions:
        raise OracleError("bars do not exactly match the expected exchange sessions")
    if len(set(expected_sessions)) != len(expected_sessions) or expected_sessions != sorted(
        expected_sessions
    ):
        raise OracleError("expected sessions must be unique and ordered")
    for bar in bars:
        values = (bar.open, bar.high, bar.low, bar.close)
        if bar.adjustment_status not in {"ADJUSTED", "SYNTHETIC_ADJUSTED"}:
            raise OracleError("oracle requires action-adjusted bars")
        if any(not value.is_finite() or value <= 0 for value in values):
            raise OracleError(f"invalid OHLC on {bar.session}")
        if bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close):
            raise OracleError(f"invalid OHLC on {bar.session}")


def _cross_up(previous: EmaPoint, current: EmaPoint) -> bool:
    return (
        previous.ema10 is not None
        and previous.ema21 is not None
        and current.ema10 is not None
        and current.ema21 is not None
        and previous.ema10 <= previous.ema21
        and current.ema10 > current.ema21
    )


def _cross_down(previous: EmaPoint, current: EmaPoint) -> bool:
    return (
        previous.ema10 is not None
        and previous.ema21 is not None
        and current.ema10 is not None
        and current.ema21 is not None
        and previous.ema10 >= previous.ema21
        and current.ema10 < current.ema21
    )


def cross_signal(previous: EmaPoint, current: EmaPoint) -> str | None:
    if _cross_up(previous, current):
        return "ENTRY"
    if _cross_down(previous, current):
        return "EXIT"
    return None


def _cost(
    side: str,
    quantity: int,
    price: Decimal,
    session: date,
    slippage_bps: int,
    cost_config: dict,
) -> dict:
    return execution_costs.calculate(
        side=side,
        quantity=Decimal(quantity),
        unit_price=price,
        trade_date=session,
        slippage_bps=slippage_bps,
        config=cost_config,
    )


def _entry_quantity(
    *,
    capital: Decimal,
    risk_fraction: Decimal,
    reference_price: Decimal,
    stop: Decimal,
    session: date,
    slippage_bps: int,
    cost_config: dict,
) -> tuple[int, dict] | None:
    executed_price = reference_price * (Decimal("1") + Decimal(slippage_bps) / Decimal("10000"))
    risk_per_share = executed_price - stop
    if risk_per_share <= 0:
        return None
    risk_budget = capital * risk_fraction
    quantity = int((risk_budget / risk_per_share).to_integral_value(rounding=ROUND_FLOOR))
    quantity = min(
        quantity,
        int((capital / executed_price).to_integral_value(rounding=ROUND_FLOOR)),
    )
    while quantity > 0:
        cost = _cost("BUY", quantity, reference_price, session, slippage_bps, cost_config)
        if -cost["cash_effect"] <= capital:
            return quantity, cost
        quantity -= 1
    return None


def run(
    bars: list[Bar],
    *,
    expected_sessions: list[date],
    benchmark_bars: list[Bar],
    capital: Decimal,
    risk_fraction: Decimal,
    slippage_bps: int,
    cost_config: dict,
    execution: str = "NEXT_OPEN",
    symbol: str | None = None,
    instrument_status: str = "ACTIVE",
) -> dict:
    """Run one-symbol, long-only research accounting with no leverage."""
    _validate_bars(bars, expected_sessions)
    _validate_bars(benchmark_bars, expected_sessions)
    if len(bars) <= SLOW:
        raise OracleError("oracle requires at least one signal-eligible session after EMA warm-up")
    if execution not in {"NEXT_OPEN", "SAME_CLOSE"}:
        raise OracleError("unsupported execution convention")
    if instrument_status != "ACTIVE":
        raise OracleError("instrument history is not proven active for the complete interval")
    if capital <= 0 or not Decimal("0") < risk_fraction <= Decimal("1"):
        raise OracleError("capital and risk fraction are invalid")
    points = ema_points(bars)
    market_payload = {
        "bars": [
            [bar.session.isoformat(), str(bar.open), str(bar.high), str(bar.low), str(bar.close)]
            for bar in bars
        ],
        "benchmark_bars": [
            [bar.session.isoformat(), str(bar.open), str(bar.high), str(bar.low), str(bar.close)]
            for bar in benchmark_bars
        ],
    }
    market_data_identity = sha256(
        json.dumps(market_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    identity_payload = {
        **market_payload,
        "symbol": symbol,
        "instrument_status": instrument_status,
        "capital": str(capital),
        "risk_fraction": str(risk_fraction),
        "slippage_bps": slippage_bps,
        "cost_config": cost_config,
    }
    run_identity = sha256(
        json.dumps(identity_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    cash = capital
    position = None
    pending = None
    trades = []
    exclusions = []
    equity_curve = []
    position_sessions = 0
    reference_turnover = Decimal("0")

    def execute_order(order: dict, reference: Decimal, trade_session: date) -> None:
        nonlocal cash, position, reference_turnover
        if order["side"] == "BUY":
            sized = _entry_quantity(
                capital=cash,
                risk_fraction=risk_fraction,
                reference_price=reference,
                stop=order["stop"],
                session=trade_session,
                slippage_bps=slippage_bps,
                cost_config=cost_config,
            )
            if sized is None:
                exclusions.append(
                    {
                        "session": trade_session,
                        "reason": "entry stop or capital prevents sizing",
                    }
                )
                return
            quantity, buy_cost = sized
            cash += buy_cost["cash_effect"]
            reference_turnover += Decimal(quantity) * reference
            if cash < 0:
                raise OracleError("no-leverage invariant failed")
            position = {
                "quantity": quantity,
                "signal_session": order["signal_session"],
                "entry_session": trade_session,
                "entry_reference": reference,
                "entry_executed": buy_cost["executed_unit_price"],
                "entry_cost": buy_cost["total_cost"],
                "stop": order["stop"],
                "cash_before": cash - buy_cost["cash_effect"],
            }
            return
        sell = _cost(
            "SELL",
            position["quantity"],
            reference,
            trade_session,
            slippage_bps,
            cost_config,
        )
        cash += sell["cash_effect"]
        reference_turnover += Decimal(position["quantity"]) * reference
        trades.append(
            _trade(position, trade_session, reference, sell, order.get("reason", "EMA_CROSS"), cash)
        )
        position = None

    for index, bar in enumerate(bars):
        point = points[index]
        if (
            pending
            and pending["execute_index"] == index
            and pending["side"] == "SELL"
            and position is not None
            and bar.open <= position["stop"]
        ):
            execute_order({**pending, "reason": "STOP"}, bar.open, bar.session)
            pending = None
        elif pending and pending["execute_index"] == index:
            execute_order(pending, bar.open, bar.session)
            pending = None

        if position and bar.low <= position["stop"]:
            reference = bar.open if bar.open <= position["stop"] else position["stop"]
            sell = _cost(
                "SELL",
                position["quantity"],
                reference,
                bar.session,
                slippage_bps,
                cost_config,
            )
            cash += sell["cash_effect"]
            reference_turnover += Decimal(position["quantity"]) * reference
            trades.append(_trade(position, bar.session, reference, sell, "STOP", cash))
            position = None
            pending = None

        if position is not None:
            position_sessions += 1
        equity_curve.append(
            {
                "session": bar.session,
                "value": cash
                + (Decimal(position["quantity"]) * bar.close if position is not None else 0),
                "invested": position is not None,
            }
        )

        if index == 0 or point.ema21 is None:
            continue
        previous = points[index - 1]
        signal = cross_signal(previous, point)
        if position is None and pending is None and signal == "ENTRY":
            execute_index = index + 1
            if execution == "NEXT_OPEN" and execute_index >= len(bars):
                exclusions.append(
                    {"session": bar.session, "reason": "entry signal lacks next session"}
                )
            else:
                order = {
                    "side": "BUY",
                    "execute_index": execute_index,
                    "signal_session": bar.session,
                    "stop": point.ema21,
                }
                if execution == "SAME_CLOSE":
                    execute_order(order, bar.close, bar.session)
                else:
                    pending = order
        elif position is not None and pending is None and signal == "EXIT":
            execute_index = index + 1
            if execution == "NEXT_OPEN" and execute_index >= len(bars):
                exclusions.append(
                    {"session": bar.session, "reason": "exit signal lacks next session"}
                )
            else:
                order = {
                    "side": "SELL",
                    "execute_index": execute_index,
                    "signal_session": bar.session,
                }
                if execution == "SAME_CLOSE":
                    execute_order(order, bar.close, bar.session)
                else:
                    pending = order

    terminal_value = cash
    if position:
        terminal_value += Decimal(position["quantity"]) * bars[-1].close
    return {
        "methodology_version": METHODOLOGY,
        "run_identity": run_identity,
        "market_data_identity": market_data_identity,
        "symbol": symbol,
        "instrument_status": instrument_status,
        "execution": execution,
        "initial_capital": capital,
        "terminal_value": terminal_value,
        "ending_cash": cash,
        "net_return": terminal_value / capital - Decimal("1"),
        "gross_return": _gross_return(trades, position, bars[-1].close, capital),
        "benchmark_return": benchmark_bars[-1].close / benchmark_bars[0].close - Decimal("1"),
        "benchmark_treatment": "adjusted close-to-close, frictionless, same sessions",
        "cash_return": Decimal("0"),
        "trades": trades,
        "open_position": position,
        "exclusions": exclusions,
        "equity_curve": equity_curve,
        "position_sessions": position_sessions,
        "reference_turnover": reference_turnover,
        "ema": points,
        "cost_status": cost_config["status"],
        "slippage_bps": slippage_bps,
    }


def _gross_return(
    trades: list[dict], position: dict | None, terminal_close: Decimal, capital: Decimal
) -> Decimal:
    pnl = sum((trade["gross_pnl"] for trade in trades), Decimal("0"))
    if position is not None:
        pnl += Decimal(position["quantity"]) * (terminal_close - position["entry_reference"])
    return pnl / capital


def _maximum_drawdown(equity_curve: list[dict]) -> Decimal:
    peak = equity_curve[0]["value"]
    maximum = Decimal("0")
    for point in equity_curve:
        peak = max(peak, point["value"])
        maximum = max(maximum, Decimal("1") - point["value"] / peak)
    return maximum


def report(
    *,
    symbol: str,
    delayed: dict,
    same_close: dict,
    bars: list[Bar],
    benchmark_bars: list[Bar],
    expected_sessions: list[date],
) -> dict:
    """Build the bounded T12.3 output for one synthetic or source-proven interval."""
    if delayed["execution"] != "NEXT_OPEN" or same_close["execution"] != "SAME_CLOSE":
        raise OracleError("report requires delayed and same-close results")
    if delayed["methodology_version"] != same_close["methodology_version"]:
        raise OracleError("report methodologies differ")
    if delayed["run_identity"] != same_close["run_identity"]:
        raise OracleError("report inputs differ")
    if not symbol or delayed["symbol"] != symbol or same_close["symbol"] != symbol:
        raise OracleError("report symbol differs")
    if not bars or [bar.session for bar in bars] != expected_sessions:
        raise OracleError("report bars do not match expected sessions")
    market_payload = {
        "bars": [
            [bar.session.isoformat(), str(bar.open), str(bar.high), str(bar.low), str(bar.close)]
            for bar in bars
        ],
        "benchmark_bars": [
            [bar.session.isoformat(), str(bar.open), str(bar.high), str(bar.low), str(bar.close)]
            for bar in benchmark_bars
        ],
    }
    market_identity = sha256(
        json.dumps(market_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if market_identity != delayed["market_data_identity"]:
        raise OracleError("report market data differs")

    closed = delayed["trades"]
    session_index = {session: index for index, session in enumerate(expected_sessions)}
    contributions = [
        {
            "symbol": symbol,
            "entry_session": trade["entry_session"],
            "exit_session": trade["exit_session"],
            "gross_pnl": trade["gross_pnl"],
            "net_pnl": trade["net_pnl"],
        }
        for trade in closed
    ]
    return {
        "methodology_version": delayed["methodology_version"],
        "research_status": "LIMITED_WINDOW_DIAGNOSTIC",
        "warning": (
            "Research context only. No trade instruction is produced. This limited-window "
            "diagnostic is not long-horizon evidence."
        ),
        "symbol": symbol,
        "coverage": {
            "start": bars[0].session,
            "end": bars[-1].session,
            "expected_sessions": len(expected_sessions),
            "observed_sessions": len(bars),
            "coverage_status": "COMPLETE",
            "coverage_ratio": Decimal(len(bars)) / Decimal(len(expected_sessions)),
            "missing_expected_sessions": 0,
            "ema_warmup_sessions": SLOW,
            "signal_eligible_sessions": max(0, len(bars) - SLOW),
        },
        "sample": {
            "entries": len(closed) + int(delayed["open_position"] is not None),
            "completed_exits": len(closed),
            "open_positions": int(delayed["open_position"] is not None),
            "holding_durations_sessions": [
                session_index[trade["exit_session"]] - session_index[trade["entry_session"]]
                for trade in closed
            ],
            "open_holding_duration_sessions": (
                len(expected_sessions) - session_index[delayed["open_position"]["entry_session"]]
                if delayed["open_position"] is not None
                else None
            ),
        },
        "returns": {
            "gross": delayed["gross_return"],
            "net": delayed["net_return"],
            "benchmark": delayed["benchmark_return"],
        },
        "maximum_drawdown": _maximum_drawdown(delayed["equity_curve"]),
        "turnover_ratio": delayed["reference_turnover"] / delayed["initial_capital"],
        "close_exposure_ratio": Decimal(delayed["position_sessions"]) / Decimal(len(bars)),
        "execution_delay_sensitivity": {
            "next_open_net_return": delayed["net_return"],
            "same_close_net_return": same_close["net_return"],
            "difference": delayed["net_return"] - same_close["net_return"],
            "same_close_label": "UPPER_BOUND_DIAGNOSTIC",
        },
        "contributions": contributions,
        "exclusions": list(delayed["exclusions"]),
        "cost_status": delayed["cost_status"],
        "slippage_bps": delayed["slippage_bps"],
    }


def _trade(position: dict, session: date, reference: Decimal, sell: dict, reason: str, cash):
    quantity = Decimal(position["quantity"])
    gross_pnl = quantity * (reference - position["entry_reference"])
    net_pnl = cash - position["cash_before"]
    return {
        "signal_session": position["signal_session"],
        "entry_session": position["entry_session"],
        "exit_session": session,
        "quantity": int(quantity),
        "entry_reference": position["entry_reference"],
        "entry_executed": position["entry_executed"],
        "exit_reference": reference,
        "exit_executed": sell["executed_unit_price"],
        "sizing_stop": position["stop"],
        "exit_reason": reason,
        "gross_pnl": gross_pnl,
        "net_pnl": net_pnl,
        "entry_cost": position["entry_cost"],
        "exit_cost": sell["total_cost"],
    }
