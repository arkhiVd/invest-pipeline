"""Deterministic Phase 11 portfolio-performance calculations."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

METHODOLOGY = "portfolio-performance-2026.1-estimated"


class PerformanceError(ValueError):
    pass


@dataclass(frozen=True)
class CashFlow:
    flow_date: date
    amount: Decimal


@dataclass(frozen=True)
class TwrPeriod:
    start_date: date
    end_date: date
    start_value: Decimal
    end_value: Decimal
    external_flow: Decimal = Decimal("0")


def xnpv(rate: float, flows: list[CashFlow]) -> float:
    if rate <= -1:
        raise PerformanceError("XIRR rate must be greater than -1")
    if not flows:
        raise PerformanceError("XIRR requires cash flows")
    origin = min(flow.flow_date for flow in flows)
    return sum(
        float(flow.amount) / ((1 + rate) ** ((flow.flow_date - origin).days / 365.0))
        for flow in flows
    )


def xirr(flows: list[CashFlow], *, tolerance: float = 1e-10) -> float:
    """Solve annualized irregular return by deterministic bracketed bisection."""
    if (
        len(flows) < 2
        or not any(flow.amount < 0 for flow in flows)
        or not any(flow.amount > 0 for flow in flows)
    ):
        raise PerformanceError("XIRR requires at least one positive and one negative flow")
    combined: dict[date, Decimal] = {}
    for flow in flows:
        if not flow.amount.is_finite():
            raise PerformanceError("XIRR flow must be finite")
        combined[flow.flow_date] = combined.get(flow.flow_date, Decimal("0")) + flow.amount
    normalized = [CashFlow(day, amount) for day, amount in sorted(combined.items()) if amount]
    low = -0.999999999
    high = 1.0
    low_value = xnpv(low, normalized)
    high_value = xnpv(high, normalized)
    while low_value * high_value > 0 and high < 1_000_000:
        high *= 2
        high_value = xnpv(high, normalized)
    if low_value * high_value > 0:
        raise PerformanceError("XIRR root is not bracketed")
    for _ in range(256):
        middle = (low + high) / 2
        value = xnpv(middle, normalized)
        if abs(value) <= tolerance or high - low <= tolerance:
            return middle
        if low_value * value <= 0:
            high = middle
        else:
            low = middle
            low_value = value
    raise PerformanceError("XIRR did not converge")


def twr(periods: list[TwrPeriod]) -> float:
    """Chain subperiod returns with end-of-period external-flow adjustment."""
    if not periods:
        raise PerformanceError("TWR requires valuation periods")
    factor = Decimal("1")
    previous_end = None
    for period in periods:
        if period.start_date >= period.end_date:
            raise PerformanceError("TWR period dates are invalid")
        if period.start_value <= 0 or period.end_value < 0:
            raise PerformanceError("TWR valuations are invalid")
        if previous_end is not None and previous_end != period.start_date:
            raise PerformanceError("TWR periods are not contiguous")
        subperiod = (period.end_value - period.external_flow) / period.start_value
        factor *= subperiod
        previous_end = period.end_date
    return float(factor - Decimal("1"))


def account_xirr_inputs(conn, account_id: str, terminal_date: date, terminal_value: Decimal):
    """Build investor-perspective flows from persisted external flows and terminal value."""
    rows = conn.execute(
        "SELECT CAST(event_at AS DATE), direction, amount "
        "FROM portfolio_cash_flow WHERE account_id=? AND CAST(event_at AS DATE)<=? "
        "ORDER BY event_at,event_id",
        [account_id, terminal_date],
    ).fetchall()
    flows = [
        CashFlow(day, -Decimal(str(amount)) if direction == "DEPOSIT" else Decimal(str(amount)))
        for day, direction, amount in rows
    ]
    flows.append(CashFlow(terminal_date, Decimal(str(terminal_value))))
    return flows


def calculate_account_xirr(
    conn,
    *,
    account_id: str,
    terminal_date: date,
    status: str,
    assumptions: list[str],
    exclusions: list[str],
    residuals: list[str],
):
    account = conn.execute(
        "SELECT native_currency FROM portfolio_account WHERE account_id=?", [account_id]
    ).fetchone()
    if not account:
        raise PerformanceError("unknown portfolio account")
    terminal_row = conn.execute(
        "SELECT sum(value),min(valuation_date),max(valuation_date) FROM ("
        "SELECT value,CAST(valued_at AS DATE) valuation_date,row_number() OVER ("
        "PARTITION BY coalesce(instrument_id,'__ACCOUNT_CASH__') ORDER BY valued_at DESC"
        ") AS recency FROM portfolio_valuation "
        "WHERE account_id=? AND CAST(valued_at AS DATE)<=?) WHERE recency=1",
        [account_id, terminal_date],
    ).fetchone()
    terminal, earliest_valuation, latest_valuation = terminal_row
    if terminal is None:
        return result_payload(
            account_id=account_id,
            metric="XIRR",
            status="UNAVAILABLE",
            value=None,
            currency=account[0],
            coverage_start=None,
            coverage_end=terminal_date,
            assumptions=assumptions,
            exclusions=[*exclusions, "terminal valuation unavailable"],
            residuals=residuals,
            inputs={"terminal_date": terminal_date, "terminal_value": None},
        )
    flows = account_xirr_inputs(conn, account_id, terminal_date, Decimal(str(terminal)))
    try:
        value = xirr(flows)
    except PerformanceError as exc:
        return result_payload(
            account_id=account_id,
            metric="XIRR",
            status="UNAVAILABLE",
            value=None,
            currency=account[0],
            coverage_start=min((flow.flow_date for flow in flows), default=None),
            coverage_end=terminal_date,
            assumptions=assumptions,
            exclusions=[*exclusions, str(exc)],
            residuals=residuals,
            inputs={
                "flows": [(flow.flow_date, flow.amount) for flow in flows],
                "terminal_valuation_dates": [earliest_valuation, latest_valuation],
            },
        )
    return result_payload(
        account_id=account_id,
        metric="XIRR",
        status=status,
        value=value,
        currency=account[0],
        coverage_start=min(flow.flow_date for flow in flows),
        coverage_end=terminal_date,
        assumptions=assumptions,
        exclusions=exclusions,
        residuals=residuals,
        inputs={
            "flows": [(flow.flow_date, flow.amount) for flow in flows],
            "terminal_valuation_dates": [earliest_valuation, latest_valuation],
        },
    )


def native_account_summary(conn, account_id: str, as_of: date):
    """Return native-currency broker totals without mixing account currencies."""
    currency_row = conn.execute(
        "SELECT native_currency FROM portfolio_account WHERE account_id=?", [account_id]
    ).fetchone()
    if not currency_row:
        raise PerformanceError("unknown portfolio account")
    realized_row = conn.execute(
        "SELECT count(*),sum(realized_pnl) FROM portfolio_tax_lot "
        "WHERE account_id=? AND disposed_date<=?",
        [account_id, as_of],
    ).fetchone()
    realized = realized_row[1] if realized_row[0] else None
    income = conn.execute(
        "SELECT coalesce(sum(gross_amount),0) FROM portfolio_income "
        "WHERE account_id=? AND CAST(event_at AS DATE)<=?",
        [account_id, as_of],
    ).fetchone()[0]
    fees = conn.execute(
        "SELECT coalesce(sum(amount),0) FROM portfolio_fee "
        "WHERE account_id=? AND CAST(event_at AS DATE)<=?",
        [account_id, as_of],
    ).fetchone()[0]
    valuations = conn.execute(
        "SELECT instrument_id,value,cost_basis,CAST(valued_at AS DATE) FROM ("
        "SELECT *,row_number() OVER (PARTITION BY coalesce(instrument_id,'__CASH__') "
        "ORDER BY valued_at DESC) recency FROM portfolio_valuation "
        "WHERE account_id=? AND CAST(valued_at AS DATE)<=?) WHERE recency=1",
        [account_id, as_of],
    ).fetchall()
    current_value = sum((Decimal(str(row[1])) for row in valuations), Decimal("0"))
    known_cost = [Decimal(str(row[2])) for row in valuations if row[2] is not None]
    unrealized = (
        sum(
            (
                Decimal(str(row[1])) - Decimal(str(row[2]))
                for row in valuations
                if row[2] is not None
            ),
            Decimal("0"),
        )
        if known_cost
        else None
    )
    return {
        "currency": currency_row[0],
        "as_of": as_of,
        "realized_return": None if realized is None else Decimal(str(realized)),
        "unrealized_return": unrealized,
        "income": Decimal(str(income)),
        "fees": Decimal(str(fees)),
        "current_value": current_value,
        "valuation_count": len(valuations),
        "cost_basis_count": len(known_cost),
    }


def native_allocation(conn, account_id: str, as_of: date):
    rows = conn.execute(
        "SELECT coalesce(i.instrument_type,'CASH'),coalesce(i.symbol,'CASH'),v.value,"
        "CAST(v.valued_at AS DATE) FROM (SELECT *,row_number() OVER ("
        "PARTITION BY coalesce(instrument_id,'__CASH__') ORDER BY valued_at DESC) recency "
        "FROM portfolio_valuation WHERE account_id=? AND CAST(valued_at AS DATE)<=?) v "
        "LEFT JOIN portfolio_instrument i ON i.instrument_id=v.instrument_id WHERE recency=1",
        [account_id, as_of],
    ).fetchall()
    total = sum((Decimal(str(row[2])) for row in rows), Decimal("0"))
    if total <= 0:
        raise PerformanceError("allocation requires a positive valuation")
    return [
        {
            "asset_class": row[0],
            "symbol": row[1],
            "native_value": Decimal(str(row[2])),
            "weight": Decimal(str(row[2])) / total,
            "source_as_of": row[3],
        }
        for row in rows
    ]


def managed_product_allocation(conn, account_id: str, as_of: date):
    """Attribute latest source valuations only through dated source memberships."""
    rows = conn.execute(
        "SELECT v.instrument_id,v.value,CAST(v.valued_at AS DATE),p.product_id,p.product_name,"
        "m.membership_id,m.import_id,ai.import_id FROM (SELECT *,row_number() OVER ("
        "PARTITION BY coalesce(instrument_id,'__CASH__') ORDER BY valued_at DESC) recency "
        "FROM portfolio_valuation WHERE account_id=? AND CAST(valued_at AS DATE)<=?) v "
        "LEFT JOIN managed_product_membership m ON m.instrument_id=v.instrument_id "
        "AND m.valid_from<=CAST(v.valued_at AS DATE) "
        "AND (m.valid_to IS NULL OR m.valid_to>=CAST(v.valued_at AS DATE)) "
        "LEFT JOIN managed_product p ON p.product_id=m.product_id AND p.account_id=? "
        "LEFT JOIN accounting_import_run ai ON ai.import_id=m.import_id AND ai.account_id=? "
        "WHERE recency=1 ORDER BY v.instrument_id,p.product_id",
        [account_id, as_of, account_id, account_id],
    ).fetchall()
    if not rows:
        return {
            "status": "UNAVAILABLE",
            "as_of": as_of,
            "allocation": [],
            "exclusions": ["source valuations unavailable"],
        }

    grouped: dict[str | None, list[tuple]] = {}
    for row in rows:
        grouped.setdefault(row[0], []).append(row)
    exclusions = []
    buckets: dict[tuple[str, str], Decimal] = {}
    source_dates = []
    evidence = []
    for instrument_id, matches in grouped.items():
        value = Decimal(str(matches[0][1]))
        source_dates.append(matches[0][2])
        if instrument_id is None:
            cash = ("CASH", "CASH")
            buckets[cash] = buckets.get(cash, Decimal("0")) + value
            evidence.append(("CASH", None, None, None, matches[0][2], value))
            continue
        valid = [
            row
            for row in matches
            if row[3] is not None and row[5] is not None and row[7] is not None
        ]
        if len(valid) != 1:
            reason = "missing" if not valid else "overlapping"
            exclusions.append(f"{instrument_id}: {reason} structured product membership")
            continue
        row = valid[0]
        product = (row[3], row[4])
        buckets[product] = buckets.get(product, Decimal("0")) + value
        evidence.append((instrument_id, row[3], row[5], row[6], row[2], value))
    if exclusions:
        return {
            "status": "UNAVAILABLE",
            "as_of": as_of,
            "allocation": [],
            "exclusions": exclusions,
        }
    total = sum(buckets.values(), Decimal("0"))
    if total <= 0:
        raise PerformanceError("managed-product allocation requires a positive valuation")
    allocation = [
        {
            "product_id": product_id,
            "product": product_name,
            "native_value": value,
            "weight": value / total,
            "source_as_of": max(source_dates),
        }
        for (product_id, product_name), value in sorted(buckets.items())
    ]
    fingerprint = hashlib.sha256(
        json.dumps(evidence, default=str, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "status": "EXACT",
        "as_of": as_of,
        "allocation": allocation,
        "exclusions": [],
        "input_fingerprint": fingerprint,
    }


def convert_allocation(allocation: list[dict], rate: Decimal):
    if rate <= 0:
        raise PerformanceError("allocation FX rate must be positive")
    return [{**row, "base_value": row["native_value"] * rate} for row in allocation]


def store_allocation(conn, result_id: str, allocation: list[dict]):
    for row in allocation:
        conn.execute(
            "INSERT INTO portfolio_allocation_result VALUES (?,?,?,?,?,?,?)",
            [
                result_id,
                "INSTRUMENT",
                row["symbol"],
                row["native_value"],
                row.get("base_value"),
                row["weight"],
                row["source_as_of"],
            ],
        )


def result_payload(
    *,
    account_id: str,
    metric: str,
    status: str,
    value: float | Decimal | None,
    currency: str | None,
    coverage_start: date | None,
    coverage_end: date | None,
    assumptions: list[str],
    exclusions: list[str],
    residuals: list[str],
    inputs,
):
    if status == "UNAVAILABLE" and value is not None:
        raise PerformanceError("unavailable result cannot have a value")
    if status != "UNAVAILABLE" and (value is None or not math.isfinite(float(value))):
        raise PerformanceError("available result requires a finite value")
    fingerprint = hashlib.sha256(
        json.dumps(inputs, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    natural = [account_id, metric, status, coverage_start, coverage_end, METHODOLOGY, fingerprint]
    result_id = hashlib.sha256(
        json.dumps(natural, default=str, separators=(",", ":")).encode()
    ).hexdigest()[:24]
    return {
        "result_id": result_id,
        "account_id": account_id,
        "metric": metric,
        "status": status,
        "value": None if value is None else Decimal(str(value)),
        "currency": currency,
        "coverage_start": coverage_start,
        "coverage_end": coverage_end,
        "methodology_version": METHODOLOGY,
        "assumptions_json": json.dumps(assumptions, separators=(",", ":")),
        "exclusions_json": json.dumps(exclusions, separators=(",", ":")),
        "residuals_json": json.dumps(residuals, separators=(",", ":")),
        "input_fingerprint": fingerprint,
    }


def store_result(conn, payload: dict, calculated_at) -> str:
    existing = conn.execute(
        "SELECT input_fingerprint FROM portfolio_performance_result WHERE result_id=?",
        [payload["result_id"]],
    ).fetchone()
    if existing:
        if existing[0] != payload["input_fingerprint"]:
            raise PerformanceError("performance result identity conflict")
        return "duplicate"
    conn.execute(
        "INSERT INTO portfolio_performance_result VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            payload["result_id"],
            payload["account_id"],
            payload["metric"],
            payload["status"],
            payload["value"],
            payload["currency"],
            payload["coverage_start"],
            payload["coverage_end"],
            payload["methodology_version"],
            payload["assumptions_json"],
            payload["exclusions_json"],
            payload["residuals_json"],
            payload["input_fingerprint"],
            calculated_at,
        ],
    )
    return "stored"
