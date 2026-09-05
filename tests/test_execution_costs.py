import json
from datetime import date
from decimal import Decimal

import pytest

from invest import execution_costs


def test_hand_worked_delivery_buy_and_sell_costs():
    config = execution_costs.load_config()
    buy = execution_costs.calculate(
        side="BUY",
        quantity=Decimal("100"),
        unit_price=Decimal("100"),
        trade_date=date(2025, 1, 2),
        slippage_bps=5,
        config=config,
    )
    assert buy["reference_turnover"] == Decimal("10000")
    assert buy["turnover"] == Decimal("10005.0000")
    assert buy["executed_unit_price"] == Decimal("100.0500")
    assert buy["components"] == {
        "brokerage": Decimal("0"),
        "stt": Decimal("10.0050000"),
        "exchange": Decimal("0.30715350000"),
        "sebi": Decimal("0.0100050000"),
        "stamp": Decimal("1.500750000"),
        "gst": Decimal("0.0570885300000"),
        "dp": Decimal("0"),
        "slippage": Decimal("5"),
    }
    assert buy["total_cost"] == Decimal("16.8799970300000")
    assert buy["cash_effect"] == Decimal("-10016.8799970300000")

    sell = execution_costs.calculate(
        side="SELL",
        quantity=Decimal("100"),
        unit_price=Decimal("100"),
        trade_date=date(2025, 1, 2),
        slippage_bps=5,
        config=config,
    )
    assert sell["components"]["stamp"] == 0
    assert sell["components"]["dp"] == Decimal("15.34")
    assert sell["turnover"] == Decimal("9995.0000")
    assert sell["total_cost"] == Decimal("30.7088729700000")
    assert sell["cash_effect"] == Decimal("9969.2911270300000")
    assert sell["status"] == "ESTIMATED"
    assert "Current Zerodha/NSE" in sell["assumption"]
    assert sell["schedule_evidence_status"] == "CURRENT_BACK_APPLIED"
    assert sell["schedule_valid_from"] == date(2025, 1, 1)
    assert sell["retrieved_on"] == date(2026, 8, 31)


def test_cost_schedule_and_scenarios_fail_closed(tmp_path):
    config = execution_costs.load_config()
    with pytest.raises(execution_costs.CostError, match="exactly one"):
        execution_costs.schedule_for(config, date(2024, 12, 31))
    with pytest.raises(execution_costs.CostError, match="predeclared"):
        execution_costs.calculate(
            side="BUY",
            quantity=Decimal("1"),
            unit_price=Decimal("100"),
            trade_date=date(2025, 1, 2),
            slippage_bps=7,
            config=config,
        )
    changed = {**config, "slippage_scenarios_bps": [5, 0, 5]}
    path = tmp_path / "costs.json"
    path.write_text(json.dumps(changed))
    with pytest.raises(execution_costs.CostError, match="unique and ordered"):
        execution_costs.load_config(path)
    for changed, message in [
        ({**config, "schedules": []}, "non-empty"),
        ({**config, "status": []}, "status is invalid"),
        ({**config, "source_url": None}, "metadata is incomplete"),
        (
            {
                **config,
                "status": "EXACT",
                "historical_effective_dates_reconciled": False,
            },
            "exact costs require",
        ),
        (
            {
                **config,
                "schedules": [{**config["schedules"][0], "exchange_rate": float("nan")}],
            },
            "Decimal strings",
        ),
        (
            {
                **config,
                "schedules": [{**config["schedules"][0], "sebi_rate": "Infinity"}],
            },
            "finite",
        ),
    ]:
        path.write_text(json.dumps(changed))
        with pytest.raises(execution_costs.CostError, match=message):
            execution_costs.load_config(path)
    with pytest.raises(execution_costs.CostError, match="whole shares"):
        execution_costs.calculate(
            side="BUY",
            quantity=Decimal("1.5"),
            unit_price=Decimal("100"),
            trade_date=date(2025, 1, 2),
            slippage_bps=5,
            config=config,
        )
    for bad in (Decimal("NaN"), Decimal("Infinity")):
        with pytest.raises(execution_costs.CostError, match="finite"):
            execution_costs.calculate(
                side="BUY",
                quantity=Decimal("1"),
                unit_price=bad,
                trade_date=date(2025, 1, 2),
                slippage_bps=5,
                config=config,
            )
    malformed = {**config, "schedules": [{**config["schedules"][0], "gst_rate": 0.18}]}
    with pytest.raises(execution_costs.CostError, match="Decimal strings"):
        execution_costs.calculate(
            side="BUY",
            quantity=Decimal("1"),
            unit_price=Decimal("100"),
            trade_date=date(2025, 1, 2),
            slippage_bps=5,
            config=malformed,
        )


def test_cost_config_uses_decimal_strings_and_has_no_trade_api():
    config = execution_costs.load_config()
    assert config["methodology_version"] == "india-delivery-cost-2026.1-estimated"
    assert config["slippage_scenarios_bps"] == [0, 5, 10]
    assert not hasattr(execution_costs, "place_order")
    for schedule in config["schedules"]:
        for field in execution_costs.REQUIRED_RATES:
            assert isinstance(schedule[field], str)
