"""Effective-dated delivery-equity research costs and slippage scenarios."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "execution_costs.json"
REQUIRED_RATES = {
    "brokerage_rate",
    "stt_buy_rate",
    "stt_sell_rate",
    "exchange_rate",
    "sebi_rate",
    "stamp_buy_rate",
    "gst_rate",
    "dp_sell_per_scrip",
}


class CostError(ValueError):
    pass


def _config_date(value, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise CostError(f"{label} is invalid") from exc


def load_config(path: Path = DEFAULT_CONFIG) -> dict:
    return validate_config(json.loads(path.read_text(encoding="utf-8")))


def validate_config(config: dict) -> dict:
    if not isinstance(config, dict):
        raise CostError("execution-cost config must be an object")
    required = {
        "methodology_version",
        "status",
        "historical_effective_dates_reconciled",
        "assumption",
        "source_url",
        "retrieved_on",
        "schedules",
        "slippage_scenarios_bps",
    }
    if set(config) != required:
        raise CostError("execution-cost config contract changed")
    if not isinstance(config["status"], str) or config["status"] not in {
        "EXACT",
        "ESTIMATED",
    }:
        raise CostError("execution-cost status is invalid")
    for field in ("methodology_version", "assumption", "source_url"):
        if not isinstance(config[field], str) or not config[field].strip():
            raise CostError("execution-cost methodology metadata is incomplete")
    if not config["source_url"].startswith("https://"):
        raise CostError("execution-cost source URL must use HTTPS")
    if not isinstance(config["historical_effective_dates_reconciled"], bool):
        raise CostError("historical reconciliation flag must be boolean")
    if config["status"] == "EXACT" and not config["historical_effective_dates_reconciled"]:
        raise CostError("exact costs require historical effective-date reconciliation")
    _config_date(config["retrieved_on"], "cost source retrieval date")
    scenarios = config["slippage_scenarios_bps"]
    if (
        not isinstance(scenarios, list)
        or not scenarios
        or any(isinstance(value, bool) or not isinstance(value, int) for value in scenarios)
    ):
        raise CostError("slippage scenarios must be a non-empty integer list")
    if sorted(set(scenarios)) != scenarios:
        raise CostError("slippage scenarios must be unique and ordered")
    if any(value < 0 or value > 100 for value in scenarios):
        raise CostError("slippage scenario is outside 0..100 bps")
    if not isinstance(config["schedules"], list) or not config["schedules"]:
        raise CostError("execution-cost schedules must be a non-empty list")
    previous_end = None
    for position, schedule in enumerate(config["schedules"]):
        if not isinstance(schedule, dict) or set(schedule) != {
            "valid_from",
            "valid_to",
            "authority",
            "evidence_status",
            *REQUIRED_RATES,
        }:
            raise CostError("execution-cost schedule contract changed")
        start = _config_date(schedule["valid_from"], "schedule start")
        end = _config_date(schedule["valid_to"], "schedule end") if schedule["valid_to"] else None
        if end is not None and start > end:
            raise CostError("execution-cost schedule range is invalid")
        if position and (previous_end is None or start <= previous_end):
            raise CostError("execution-cost schedules overlap or follow an open interval")
        previous_end = end
        if (
            not isinstance(schedule["authority"], str)
            or not schedule["authority"].strip()
            or not isinstance(schedule["evidence_status"], str)
            or schedule["evidence_status"] not in {"SOURCE_EFFECTIVE", "CURRENT_BACK_APPLIED"}
        ):
            raise CostError("execution-cost schedule evidence is incomplete")
        if config["status"] == "EXACT" and schedule["evidence_status"] != "SOURCE_EFFECTIVE":
            raise CostError("exact costs require source-effective schedules")
        for field in REQUIRED_RATES:
            if not isinstance(schedule[field], str):
                raise CostError("execution-cost rates must be Decimal strings")
            try:
                value = Decimal(schedule[field])
            except (InvalidOperation, ValueError) as exc:
                raise CostError("execution-cost rate is invalid") from exc
            if not value.is_finite() or value < 0:
                raise CostError("execution-cost rate must be finite and nonnegative")
        if Decimal(schedule["gst_rate"]) > 1:
            raise CostError("GST must be represented as a fraction")
    return config


def schedule_for(config: dict, trade_date: date) -> dict:
    matches = []
    for schedule in config["schedules"]:
        start = _config_date(schedule["valid_from"], "schedule start")
        end = _config_date(schedule["valid_to"], "schedule end") if schedule["valid_to"] else None
        if start <= trade_date and (end is None or trade_date <= end):
            matches.append(schedule)
    if len(matches) != 1:
        raise CostError("exactly one execution-cost schedule must cover the trade date")
    return matches[0]


def calculate(
    *,
    side: str,
    quantity: Decimal,
    unit_price: Decimal,
    trade_date: date,
    slippage_bps: int,
    config: dict | None = None,
) -> dict:
    config = load_config() if config is None else validate_config(config)
    if side not in {"BUY", "SELL"}:
        raise CostError("side must be BUY or SELL")
    if not quantity.is_finite() or not unit_price.is_finite():
        raise CostError("quantity and unit price must be finite")
    if quantity <= 0 or unit_price <= 0:
        raise CostError("quantity and unit price must be positive")
    if quantity != quantity.to_integral_value():
        raise CostError("delivery-equity quantity must be whole shares")
    if slippage_bps not in config["slippage_scenarios_bps"]:
        raise CostError("slippage must use a predeclared scenario")
    schedule = schedule_for(config, trade_date)
    rates = {field: Decimal(schedule[field]) for field in REQUIRED_RATES}
    reference_turnover = quantity * unit_price
    slippage_rate = Decimal(slippage_bps) / Decimal("10000")
    executed_unit_price = (
        unit_price * (Decimal("1") + slippage_rate)
        if side == "BUY"
        else unit_price * (Decimal("1") - slippage_rate)
    )
    turnover = quantity * executed_unit_price
    brokerage = turnover * rates["brokerage_rate"]
    stt = turnover * rates["stt_buy_rate" if side == "BUY" else "stt_sell_rate"]
    exchange = turnover * rates["exchange_rate"]
    sebi = turnover * rates["sebi_rate"]
    stamp = turnover * rates["stamp_buy_rate"] if side == "BUY" else Decimal("0")
    gst = (brokerage + exchange + sebi) * rates["gst_rate"]
    dp = rates["dp_sell_per_scrip"] if side == "SELL" else Decimal("0")
    slippage = abs(turnover - reference_turnover)
    components = {
        "brokerage": brokerage,
        "stt": stt,
        "exchange": exchange,
        "sebi": sebi,
        "stamp": stamp,
        "gst": gst,
        "dp": dp,
        "slippage": slippage,
    }
    total = sum(components.values(), Decimal("0"))
    return {
        "side": side,
        "trade_date": trade_date,
        "reference_unit_price": unit_price,
        "executed_unit_price": executed_unit_price,
        "reference_turnover": reference_turnover,
        "turnover": turnover,
        "components": components,
        "total_cost": total,
        "cash_effect": (
            -(reference_turnover + total) if side == "BUY" else reference_turnover - total
        ),
        "slippage_bps": slippage_bps,
        "status": config["status"],
        "assumption": config["assumption"],
        "methodology_version": config["methodology_version"],
        "source_url": config["source_url"],
        "retrieved_on": date.fromisoformat(config["retrieved_on"]),
        "schedule_valid_from": date.fromisoformat(schedule["valid_from"]),
        "schedule_valid_to": (
            date.fromisoformat(schedule["valid_to"]) if schedule["valid_to"] else None
        ),
        "schedule_authority": schedule["authority"],
        "schedule_evidence_status": schedule["evidence_status"],
    }
