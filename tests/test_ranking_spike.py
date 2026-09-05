import pytest

from invest import ranking_spike


def test_metric_contract_has_units_direction_and_exclusions():
    fields = {metric.field for metric in ranking_spike.METRICS}
    assert len(fields) == len(ranking_spike.METRICS)
    assert {metric.direction for metric in ranking_spike.METRICS} == {"higher", "lower"}
    assert all(metric.unit for metric in ranking_spike.METRICS)
    assert "rsi" not in fields
    assert "price_to_all_time_high" not in fields


def test_one_member_instability_exposes_small_cohort_percentile_jump():
    result = ranking_spike.one_member_instability(
        {f"S{number}": float(number) for number in range(7)}, higher=True
    )
    assert result["cohort_size"] == 7
    assert result["max_rank_move"] == 1
    assert result["max_percentile_move"] == pytest.approx(1 / 6, abs=1e-6)


def test_tiny_cohorts_do_not_claim_stability():
    assert ranking_spike.one_member_instability({"A": 1.0, "B": 2.0}, higher=True) == {
        "cohort_size": 2,
        "max_rank_move": 0,
        "max_percentile_move": 0.0,
    }
