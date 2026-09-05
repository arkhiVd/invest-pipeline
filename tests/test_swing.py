from decimal import Decimal

import pytest

from invest import swing


def test_ema_uses_sma_seed_then_recursive_formula():
    values = swing.ema_series([1, 2, 3, 4], 3)
    assert values[:2] == [None, None]
    assert values[2] == pytest.approx(2.0)
    assert values[3] == pytest.approx(3.0)  # alpha=1/2: 4*.5 + 2*.5


def test_close_confirmed_crossover_emits_only_new_enter_and_exit():
    points = swing.ema_crossover([3, 2, 1, 2, 3, 2, 1], fast_period=2, slow_period=3)
    assert [point.signal for point in points] == [
        swing.CrossoverSignal.NONE,
        swing.CrossoverSignal.NONE,
        swing.CrossoverSignal.NONE,  # first comparable state is not a cross
        swing.CrossoverSignal.NONE,
        swing.CrossoverSignal.ENTER,
        swing.CrossoverSignal.EXIT,
        swing.CrossoverSignal.NONE,
    ]
    assert points[2].state == swing.EmaState.BEARISH
    assert points[4].state == swing.EmaState.BULLISH


def test_preexisting_bullish_state_does_not_emit_entry():
    points = swing.ema_crossover([1, 2, 3, 4, 5], fast_period=2, slow_period=3)
    assert points[2].state == swing.EmaState.BULLISH
    assert all(point.signal == swing.CrossoverSignal.NONE for point in points)


def test_equal_emas_are_neutral_and_subsequent_strict_cross_enters():
    points = swing.ema_crossover([1, 1, 1, 2], fast_period=2, slow_period=3)
    assert points[2].state == swing.EmaState.NEUTRAL
    assert points[3].signal == swing.CrossoverSignal.ENTER


def test_equal_emas_are_neutral_and_subsequent_strict_drop_exits():
    points = swing.ema_crossover([1, 1, 1, 0.5], fast_period=2, slow_period=3)
    assert points[2].state == swing.EmaState.NEUTRAL
    assert points[3].signal == swing.CrossoverSignal.EXIT


def test_ema_rejects_invalid_periods_and_prices():
    for period in (0, -1, True, 1.5):
        with pytest.raises(ValueError):
            swing.ema_series([1, 2], period)
    for prices in ([1, 0], [1, -1], [1, float("nan")], [1, float("inf")]):
        with pytest.raises(ValueError):
            swing.ema_series(prices, 2)
    with pytest.raises(ValueError, match="shorter"):
        swing.ema_crossover([1, 2], fast_period=3, slow_period=3)


def test_ema_short_history_is_explicitly_unavailable():
    assert swing.ema_series([100, 101], 3) == [None, None]
    points = swing.ema_crossover([100, 101], fast_period=2, slow_period=3)
    assert all(point.state == swing.EmaState.UNKNOWN for point in points)


def test_position_size_matches_2_percent_hand_fixture():
    result = swing.position_size(100_000, 101, 95)
    assert result.risk_fraction == Decimal("0.02")
    assert result.risk_budget == Decimal("2000.00")
    assert result.risk_per_share == Decimal("6")
    assert result.risk_limited_quantity == 333
    assert result.quantity == 333
    assert result.capital_to_deploy == Decimal("33633")
    assert result.maximum_loss_at_stop == Decimal("1998")


def test_position_size_matches_high_price_hand_fixture():
    result = swing.position_size(100_000, 4278, 4050)
    assert result.quantity == 8
    assert result.capital_to_deploy == Decimal("34224")
    assert result.maximum_loss_at_stop == Decimal("1824")


def test_position_size_caps_tight_stop_at_available_cash_without_leverage():
    result = swing.position_size(100_000, 100, 99)
    assert result.risk_limited_quantity == 2000
    assert result.affordable_quantity == 1000
    assert result.quantity == 1000
    assert result.capital_to_deploy == Decimal("100000")
    assert result.maximum_loss_at_stop == Decimal("1000")


def test_position_size_can_honestly_return_zero_shares():
    result = swing.position_size(1_000, 900, 100, risk_fraction="0.02")
    assert result.risk_budget == Decimal("20.00")
    assert result.quantity == 0
    assert result.capital_to_deploy == 0
    assert result.maximum_loss_at_stop == 0


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ((0, 100, 90), "capital"),
        ((1000, 0, 0), "entry"),
        ((1000, 100, 100), "stop"),
        ((1000, 100, 101), "stop"),
        ((1000, 100, 0), "stop"),
        ((1000, 100, -1), "stop"),
    ],
)
def test_position_size_rejects_invalid_long_trade_inputs(args, message):
    with pytest.raises(ValueError, match=message):
        swing.position_size(*args)


@pytest.mark.parametrize("risk", [0, -0.1, 1.01, float("nan"), float("inf")])
def test_position_size_rejects_invalid_risk(risk):
    with pytest.raises(ValueError, match="risk_fraction"):
        swing.position_size(1000, 100, 90, risk_fraction=risk)
