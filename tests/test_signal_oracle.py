import inspect
from datetime import date, timedelta
from decimal import Decimal

import pytest

from invest import signal_oracle


def zero_cost_config():
    return {
        "methodology_version": "fixture-exact",
        "status": "EXACT",
        "historical_effective_dates_reconciled": True,
        "assumption": "hand-worked zero-cost fixture",
        "source_url": "https://example.invalid/fixture",
        "retrieved_on": "2026-08-31",
        "schedules": [
            {
                "valid_from": "2025-01-01",
                "valid_to": None,
                "authority": "hand-worked fixture",
                "evidence_status": "SOURCE_EFFECTIVE",
                "brokerage_rate": "0",
                "stt_buy_rate": "0",
                "stt_sell_rate": "0",
                "exchange_rate": "0",
                "sebi_rate": "0",
                "stamp_buy_rate": "0",
                "gst_rate": "0",
                "dp_sell_per_scrip": "0",
            }
        ],
        "slippage_scenarios_bps": [0],
    }


def fixture_bars():
    start = date(2025, 1, 1)
    closes = [Decimal("100")] * 21 + [Decimal("102"), Decimal("103"), Decimal("70")]
    bars = []
    for index, close in enumerate(closes):
        if index == 21:
            open_price, high, low = Decimal("101"), Decimal("103"), Decimal("100")
        elif index == 22:
            open_price, high, low = Decimal("103"), Decimal("104"), Decimal("102")
        elif index == 23:
            open_price, high, low = Decimal("101"), Decimal("102"), Decimal("69")
        else:
            open_price = high = low = close
        bars.append(signal_oracle.Bar(start + timedelta(days=index), open_price, high, low, close))
    return bars


def test_first_release_hypothesis_is_fixed_without_sweep_parameters():
    assert (signal_oracle.FAST, signal_oracle.SLOW) == (10, 21)
    parameters = inspect.signature(signal_oracle.run).parameters
    assert "fast_period" not in parameters
    assert "slow_period" not in parameters
    assert "parameter_grid" not in parameters
    assert signal_oracle.METHODOLOGY == "ema-10-21-close-confirmed-2026.1"


def test_sma_seeded_ema_is_hand_worked():
    bars = fixture_bars()
    points = signal_oracle.ema_points(bars)
    assert points[8].ema10 is None
    assert points[9].ema10 == Decimal("100")
    assert points[19].ema21 is None
    assert points[20].ema21 == Decimal("100")
    assert points[21].ema10 == Decimal("100.3636363636363636363636364")
    assert points[21].ema21 == Decimal("100.1818181818181818181818182")


def test_strict_crossing_handles_equality_and_both_directions():
    equal = signal_oracle.EmaPoint(date(2025, 1, 1), Decimal("100"), Decimal("100"), Decimal("100"))
    up = signal_oracle.EmaPoint(date(2025, 1, 2), Decimal("101"), Decimal("101"), Decimal("100"))
    down = signal_oracle.EmaPoint(date(2025, 1, 3), Decimal("99"), Decimal("99"), Decimal("100"))
    assert signal_oracle.cross_signal(equal, equal) is None
    assert signal_oracle.cross_signal(equal, up) == "ENTRY"
    assert signal_oracle.cross_signal(up, equal) is None
    assert signal_oracle.cross_signal(equal, down) == "EXIT"


def test_close_signal_enters_next_open_and_fixed_ema21_stop():
    bars = fixture_bars()
    result = signal_oracle.run(
        bars,
        expected_sessions=[bar.session for bar in bars],
        benchmark_bars=bars,
        capital=Decimal("100000"),
        risk_fraction=Decimal("0.02"),
        slippage_bps=0,
        cost_config=zero_cost_config(),
    )
    assert len(result["trades"]) == 1
    trade = result["trades"][0]
    assert trade["signal_session"] == bars[21].session
    assert trade["entry_session"] == bars[22].session
    assert trade["entry_reference"] == Decimal("103")
    assert trade["quantity"] == 709
    assert trade["sizing_stop"] == Decimal("100.1818181818181818181818182")
    assert trade["exit_session"] == bars[23].session
    assert trade["exit_reference"] == trade["sizing_stop"]
    assert trade["exit_reason"] == "STOP"
    assert trade["gross_pnl"] == Decimal("-1998.090909090909090909090896")
    assert trade["net_pnl"] == Decimal("-1998.09090909090909090909090")
    assert result["terminal_value"] == Decimal("98001.90909090909090909090910")
    assert result["gross_return"] == Decimal("-0.01998090909090909090909090896")
    assert result["net_return"] == Decimal("-0.019980909090909090909090909")
    assert len(result["trades"]) == 1  # stop wins over the same-close EMA exit
    assert result["open_position"] is None
    assert result["benchmark_return"] == Decimal("-0.3")
    assert result["cash_return"] == 0
    assert result["terminal_value"] < result["initial_capital"]


def test_same_close_diagnostic_differs_from_next_session_execution():
    bars = fixture_bars()
    kwargs = {
        "expected_sessions": [bar.session for bar in bars],
        "benchmark_bars": bars,
        "capital": Decimal("100000"),
        "risk_fraction": Decimal("0.02"),
        "slippage_bps": 0,
        "cost_config": zero_cost_config(),
    }
    delayed = signal_oracle.run(bars, execution="NEXT_OPEN", **kwargs)
    same_close = signal_oracle.run(bars, execution="SAME_CLOSE", **kwargs)
    assert delayed["trades"][0]["entry_reference"] == Decimal("103")
    assert delayed["trades"][0]["quantity"] == 709
    assert delayed["net_return"] == Decimal("-0.019980909090909090909090909")
    assert same_close["trades"][0]["entry_reference"] == Decimal("102")
    assert same_close["trades"][0]["quantity"] == 980
    assert same_close["net_return"] == Decimal("-0.0178181818181818181818181816")
    assert abs(delayed["net_return"] - same_close["net_return"]) > Decimal("0.002")


def test_future_bar_cannot_change_prior_signal_or_next_open_entry():
    bars = fixture_bars()
    prefix = bars[:23]
    kwargs = {
        "capital": Decimal("100000"),
        "risk_fraction": Decimal("0.02"),
        "slippage_bps": 0,
        "cost_config": zero_cost_config(),
    }
    prefix_result = signal_oracle.run(
        prefix,
        expected_sessions=[bar.session for bar in prefix],
        benchmark_bars=prefix,
        **kwargs,
    )
    full_result = signal_oracle.run(
        bars,
        expected_sessions=[bar.session for bar in bars],
        benchmark_bars=bars,
        **kwargs,
    )

    assert prefix_result["open_position"]["signal_session"] == bars[21].session
    assert prefix_result["open_position"]["entry_session"] == bars[22].session
    assert prefix_result["open_position"]["entry_reference"] == Decimal("103")
    assert full_result["trades"][0]["signal_session"] == bars[21].session
    assert full_result["trades"][0]["entry_session"] == bars[22].session
    assert full_result["trades"][0]["entry_reference"] == Decimal("103")


def test_delisted_or_unproven_instrument_history_fails_closed():
    bars = fixture_bars()
    kwargs = {
        "expected_sessions": [bar.session for bar in bars],
        "benchmark_bars": bars,
        "capital": Decimal("100000"),
        "risk_fraction": Decimal("0.02"),
        "slippage_bps": 0,
        "cost_config": zero_cost_config(),
    }
    for status in ("DELISTED", "UNKNOWN"):
        with pytest.raises(signal_oracle.OracleError, match="not proven active"):
            signal_oracle.run(bars, instrument_status=status, **kwargs)


def test_pending_ema_exit_gap_below_stop_is_labeled_stop():
    closes = [Decimal("100")] * 21 + [Decimal("102"), Decimal("103")] + [Decimal("120")] * 20
    closes += [Decimal(value) for value in (118, 116, 114, 112, 110, 108)]
    start = date(2025, 1, 1)
    bars = []
    for index, close in enumerate(closes):
        open_price = closes[index - 1] if index else close
        if index == 48:
            open_price = Decimal("99")
        bars.append(
            signal_oracle.Bar(
                start + timedelta(days=index),
                open_price,
                max(open_price, close),
                min(open_price, close),
                close,
            )
        )
    result = signal_oracle.run(
        bars,
        expected_sessions=[bar.session for bar in bars],
        benchmark_bars=bars,
        capital=Decimal("100000"),
        risk_fraction=Decimal("0.02"),
        slippage_bps=0,
        cost_config=zero_cost_config(),
    )
    assert result["trades"][0]["exit_reason"] == "STOP"
    assert result["trades"][0]["exit_reference"] == Decimal("99")


def test_gap_through_stop_executes_at_worse_open():
    bars = fixture_bars()
    last = bars[-1]
    bars[-1] = signal_oracle.Bar(
        last.session, Decimal("99"), Decimal("101"), Decimal("68"), Decimal("70")
    )
    result = signal_oracle.run(
        bars,
        expected_sessions=[bar.session for bar in bars],
        benchmark_bars=bars,
        capital=Decimal("100000"),
        risk_fraction=Decimal("0.02"),
        slippage_bps=0,
        cost_config=zero_cost_config(),
    )
    assert result["trades"][0]["exit_reference"] == Decimal("99")


def test_nonzero_costs_slippage_and_cash_cap_reduce_affordable_quantity():
    from invest import execution_costs

    bars = fixture_bars()
    zero = signal_oracle.run(
        bars[:23],
        expected_sessions=[bar.session for bar in bars[:23]],
        benchmark_bars=bars[:23],
        capital=Decimal("100000"),
        risk_fraction=Decimal("0.02"),
        slippage_bps=0,
        cost_config=zero_cost_config(),
    )
    charged = signal_oracle.run(
        bars[:23],
        expected_sessions=[bar.session for bar in bars[:23]],
        benchmark_bars=bars[:23],
        capital=Decimal("100000"),
        risk_fraction=Decimal("0.02"),
        slippage_bps=5,
        cost_config=execution_costs.load_config(),
    )
    assert zero["open_position"] is not None
    assert charged["open_position"] is not None
    assert charged["open_position"]["entry_executed"] > Decimal("103")
    assert charged["open_position"]["entry_cost"] > 0
    assert charged["open_position"]["quantity"] < zero["open_position"]["quantity"]
    assert charged["ending_cash"] >= 0
    assert charged["cost_status"] == "ESTIMATED"


def test_open_terminal_position_is_marked_without_forced_liquidation():
    bars = fixture_bars()[:23]
    result = signal_oracle.run(
        bars,
        expected_sessions=[bar.session for bar in bars],
        benchmark_bars=bars,
        capital=Decimal("100000"),
        risk_fraction=Decimal("0.02"),
        slippage_bps=0,
        cost_config=zero_cost_config(),
        symbol="FIXTURE",
    )
    assert result["trades"] == []
    assert result["open_position"] is not None
    assert result["terminal_value"] == Decimal("100000")

    same_close = signal_oracle.run(
        bars,
        expected_sessions=[bar.session for bar in bars],
        benchmark_bars=bars,
        capital=Decimal("100000"),
        risk_fraction=Decimal("0.02"),
        slippage_bps=0,
        cost_config=zero_cost_config(),
        execution="SAME_CLOSE",
        symbol="FIXTURE",
    )
    output = signal_oracle.report(
        symbol="FIXTURE",
        delayed=result,
        same_close=same_close,
        bars=bars,
        benchmark_bars=bars,
        expected_sessions=[bar.session for bar in bars],
    )
    assert output["sample"]["open_holding_duration_sessions"] == 1


def test_t12_3_report_covers_returns_risk_activity_timing_and_exclusions():
    bars = fixture_bars()
    kwargs = {
        "expected_sessions": [bar.session for bar in bars],
        "benchmark_bars": bars,
        "capital": Decimal("100000"),
        "risk_fraction": Decimal("0.02"),
        "slippage_bps": 0,
        "cost_config": zero_cost_config(),
        "symbol": "FIXTURE",
    }
    delayed = signal_oracle.run(bars, execution="NEXT_OPEN", **kwargs)
    same_close = signal_oracle.run(bars, execution="SAME_CLOSE", **kwargs)
    report = signal_oracle.report(
        symbol="FIXTURE",
        delayed=delayed,
        same_close=same_close,
        bars=bars,
        benchmark_bars=bars,
        expected_sessions=kwargs["expected_sessions"],
    )

    assert report["research_status"] == "LIMITED_WINDOW_DIAGNOSTIC"
    assert "No trade instruction" in report["warning"]
    assert report["coverage"] == {
        "start": bars[0].session,
        "end": bars[-1].session,
        "expected_sessions": 24,
        "observed_sessions": 24,
        "coverage_status": "COMPLETE",
        "coverage_ratio": Decimal("1"),
        "missing_expected_sessions": 0,
        "ema_warmup_sessions": 21,
        "signal_eligible_sessions": 3,
    }
    assert report["sample"] == {
        "entries": 1,
        "completed_exits": 1,
        "open_positions": 0,
        "holding_durations_sessions": [1],
        "open_holding_duration_sessions": None,
    }
    assert report["returns"]["gross"] == delayed["gross_return"]
    assert report["returns"]["net"] == delayed["net_return"]
    assert report["maximum_drawdown"] > 0
    assert report["turnover_ratio"] > 0
    assert report["close_exposure_ratio"] == Decimal("1") / Decimal("24")
    sensitivity = report["execution_delay_sensitivity"]
    assert sensitivity["difference"] == delayed["net_return"] - same_close["net_return"]
    assert sensitivity["same_close_label"] == "UPPER_BOUND_DIAGNOSTIC"
    assert report["contributions"][0]["symbol"] == "FIXTURE"
    assert report["contributions"][0]["net_pnl"] == delayed["trades"][0]["net_pnl"]
    assert report["exclusions"] == []


def test_report_rejects_mismatched_execution_inputs():
    bars = fixture_bars()
    kwargs = {
        "expected_sessions": [bar.session for bar in bars],
        "benchmark_bars": bars,
        "capital": Decimal("100000"),
        "risk_fraction": Decimal("0.02"),
        "slippage_bps": 0,
        "cost_config": zero_cost_config(),
        "symbol": "FIXTURE",
    }
    delayed = signal_oracle.run(bars, execution="NEXT_OPEN", **kwargs)
    with pytest.raises(signal_oracle.OracleError, match="delayed and same-close"):
        signal_oracle.report(
            symbol="FIXTURE",
            delayed=delayed,
            same_close=delayed,
            bars=bars,
            benchmark_bars=bars,
            expected_sessions=kwargs["expected_sessions"],
        )

    unrelated = signal_oracle.run(
        bars, execution="SAME_CLOSE", **{**kwargs, "capital": Decimal("200000")}
    )
    with pytest.raises(signal_oracle.OracleError, match="inputs differ"):
        signal_oracle.report(
            symbol="FIXTURE",
            delayed=delayed,
            same_close=unrelated,
            bars=bars,
            benchmark_bars=bars,
            expected_sessions=kwargs["expected_sessions"],
        )


def test_empty_and_sub_warmup_intervals_fail_closed():
    bars = fixture_bars()
    kwargs = {
        "capital": Decimal("100000"),
        "risk_fraction": Decimal("0.02"),
        "slippage_bps": 0,
        "cost_config": zero_cost_config(),
    }
    for incomplete in ([], bars[:21]):
        with pytest.raises(signal_oracle.OracleError, match="signal-eligible session"):
            signal_oracle.run(
                incomplete,
                expected_sessions=[bar.session for bar in incomplete],
                benchmark_bars=incomplete,
                **kwargs,
            )


def test_missing_sessions_raw_bars_and_unexecutable_signal_fail_closed():
    bars = fixture_bars()
    interior_gap = bars[:10] + bars[11:]
    with pytest.raises(signal_oracle.OracleError, match="expected exchange sessions"):
        signal_oracle.run(
            interior_gap,
            expected_sessions=[bar.session for bar in bars],
            benchmark_bars=bars,
            capital=Decimal("100000"),
            risk_fraction=Decimal("0.02"),
            slippage_bps=0,
            cost_config=zero_cost_config(),
        )
    with pytest.raises(signal_oracle.OracleError, match="expected exchange sessions"):
        signal_oracle.run(
            bars,
            expected_sessions=[bar.session for bar in bars[:-1]],
            benchmark_bars=bars,
            capital=Decimal("100000"),
            risk_fraction=Decimal("0.02"),
            slippage_bps=0,
            cost_config=zero_cost_config(),
        )
    for adjustment_status in ("RAW", "ACTION_UNRESOLVED"):
        unresolved = list(bars)
        unresolved[0] = signal_oracle.Bar(
            unresolved[0].session,
            unresolved[0].open,
            unresolved[0].high,
            unresolved[0].low,
            unresolved[0].close,
            adjustment_status,
        )
        with pytest.raises(signal_oracle.OracleError, match="action-adjusted"):
            signal_oracle.run(
                unresolved,
                expected_sessions=[bar.session for bar in unresolved],
                benchmark_bars=bars,
                capital=Decimal("100000"),
                risk_fraction=Decimal("0.02"),
                slippage_bps=0,
                cost_config=zero_cost_config(),
            )
    truncated = bars[:22]
    result = signal_oracle.run(
        truncated,
        expected_sessions=[bar.session for bar in truncated],
        benchmark_bars=truncated,
        capital=Decimal("100000"),
        risk_fraction=Decimal("0.02"),
        slippage_bps=0,
        cost_config=zero_cost_config(),
    )
    assert result["trades"] == []
    assert result["exclusions"] == [
        {"session": truncated[-1].session, "reason": "entry signal lacks next session"}
    ]
