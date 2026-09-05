from datetime import UTC, date
from datetime import datetime as dt

import duckdb
import pytest

from invest import db, ranking


def test_methodology_weights_and_exclusions_are_explicit():
    config = ranking.semantic_config()
    assert sum(item["weight"] for item in config["components"]) == pytest.approx(1.0)
    assert (
        next(item for item in config["components"] if item["name"] == "evidence_completeness")[
            "weight"
        ]
        == 0.0
    )
    assert {"peg", "rsi", "price_to_all_time_high", "roe", "eps_growth_yoy"} <= set(
        config["excluded"]
    )
    assert all(item["field"] not in config["excluded"] for item in config["inputs"])


def test_midrank_percentiles_preserve_ties_and_direction():
    values = {"A": 10.0, "B": 20.0, "C": 20.0, "D": 40.0}
    higher = ranking.midrank_percentiles(values, higher=True)
    lower = ranking.midrank_percentiles(values, higher=False)
    assert higher == {"D": 1.0, "B": 0.5, "C": 0.5, "A": 0.0}
    assert lower == {"A": 1.0, "B": 0.5, "C": 0.5, "D": 0.0}


def test_added_cohort_member_changes_declared_percentile_without_survivor_normalization():
    baseline = ranking.midrank_percentiles({"A": 1.0, "B": 2.0}, higher=True)
    expanded = ranking.midrank_percentiles({"A": 1.0, "B": 2.0, "C": 3.0}, higher=True)
    assert baseline["B"] == 1.0
    assert expanded["B"] == 0.5


def test_sector_boundary_uses_30_valid_observations_and_visible_fallback():
    assert ranking.select_normalization_cohort(sector="Technology", sector_valid_count=30) == (
        "sector:Technology",
        False,
    )
    assert ranking.select_normalization_cohort(sector="Technology", sector_valid_count=29) == (
        "active_eq",
        True,
    )
    assert ranking.select_normalization_cohort(sector="UNKNOWN", sector_valid_count=100) == (
        "active_eq",
        False,
    )
    assert ranking.select_normalization_cohort(sector=None, sector_valid_count=0) == (
        "active_eq",
        False,
    )


def _universe():
    rows = {}
    for symbol, offset in (("A", 0.0), ("B", 1.0), ("C", 2.0)):
        rows[symbol] = {
            "symbol": symbol,
            "pe_ratio": 10.0 + offset,
            "pb_ratio": 1.0 + offset,
            "roce": 0.20 + offset / 100,
            "operating_margin": 0.15 + offset / 100,
            "debt_to_equity": 0.3 + offset / 10,
            "interest_coverage": 8.0 + offset,
            "current_ratio": 1.5 + offset / 10,
            "piotroski_score": 6 + offset,
            "price_to_52w_high": 0.8 + offset / 100,
            "price_above_50dma": offset > 0,
            "revenue_growth_yoy": 0.12 + offset / 100,
            "profit_growth_yoy": 0.10 + offset / 100,
        }
    return rows


def test_hand_computed_component_weights_produce_exact_scores_and_ranks(monkeypatch):
    universe = _universe()
    provenance = {
        (symbol, spec.field): (spec.source_kind, date(2026, 8, 30))
        for symbol in universe
        for spec in ranking.INPUTS
    }
    monkeypatch.setattr(ranking.screens, "build_universe", lambda _conn: universe)
    monkeypatch.setattr(
        ranking.screens,
        "load_config",
        lambda: {"screens": {"garp": {"conditions": {}}}},
    )
    monkeypatch.setattr(
        ranking.screens,
        "evaluate_screen",
        lambda rows, _conditions: {
            "survivors": [{"symbol": symbol} for symbol in rows],
            "gaps": {},
            "evaluated": len(rows),
        },
    )
    monkeypatch.setattr(ranking, "_provenance", lambda _conn, _rows: provenance)

    rows = {row["symbol"]: row for row in ranking.calculate(object())["survivors"]}
    assert rows["A"]["score"] == pytest.approx(0.33)
    assert rows["B"]["score"] == pytest.approx(0.50625)
    assert rows["C"]["score"] == pytest.approx(0.66375)
    assert {symbol: row["research_rank"] for symbol, row in rows.items()} == {
        "A": 3,
        "B": 2,
        "C": 1,
    }


def test_calculate_uses_active_eq_and_missing_required_input_fails_composite(monkeypatch):
    universe = _universe()
    universe["A"]["current_ratio"] = None
    provenance = {
        (symbol, spec.field): (spec.source_kind, date(2026, 8, 30))
        for symbol in universe
        for spec in ranking.INPUTS
    }
    monkeypatch.setattr(ranking.screens, "build_universe", lambda _conn: universe)
    monkeypatch.setattr(
        ranking.screens,
        "load_config",
        lambda: {"screens": {"garp": {"conditions": {}}}},
    )
    monkeypatch.setattr(
        ranking.screens,
        "evaluate_screen",
        lambda _universe, _conditions: {
            "survivors": [{"symbol": "A"}, {"symbol": "B"}],
            "gaps": {},
            "evaluated": 3,
        },
    )
    monkeypatch.setattr(ranking, "_provenance", lambda _conn, _rows: provenance)

    result = ranking.calculate(object())
    rows = {row["symbol"]: row for row in result["survivors"]}
    assert rows["A"]["status"] == "MISSING"
    assert rows["A"]["score"] is None
    assert rows["A"]["research_rank"] is None
    assert rows["A"]["missing_components"] == ["financial_strength"]
    assert rows["B"]["status"] == "AVAILABLE"
    assert rows["B"]["research_rank"] == 1
    pe = next(item for item in rows["B"]["inputs"] if item["field"] == "pe_ratio")
    assert pe["normalization_cohort"] == "active_eq"
    assert pe["cohort_size"] == 3
    assert pe["source"] == "market"
    evidence = next(
        item for item in rows["B"]["components"] if item["component"] == "evidence_completeness"
    )
    assert evidence["component_weight"] == 0.0
    assert evidence["weighted_contribution"] == 0.0


def test_ranking_does_not_mutate_binary_screen_membership(monkeypatch):
    universe = _universe()
    conditions = {"pe_ratio": {"lt": 12.0}}
    config = {"screens": {"value": {"conditions": conditions}}}
    provenance = {
        (symbol, spec.field): (spec.source_kind, date(2026, 8, 30))
        for symbol in universe
        for spec in ranking.INPUTS
    }
    before = ranking.screens.evaluate_screen(universe, conditions)
    monkeypatch.setattr(ranking.screens, "build_universe", lambda _conn: universe)
    monkeypatch.setattr(ranking.screens, "load_config", lambda: config)
    monkeypatch.setattr(ranking, "_provenance", lambda _conn, _rows: provenance)

    ranked = ranking.calculate(object())

    after = ranking.screens.evaluate_screen(universe, conditions)
    expected_symbols = {row["symbol"] for row in before["survivors"]}
    assert before == after
    assert expected_symbols == {row["symbol"] for row in ranked["survivors"]}
    assert expected_symbols == {"A", "B"}


def test_persist_is_atomic_replay_safe_and_reconstructable(monkeypatch):
    universe = _universe()
    provenance = {
        (symbol, spec.field): (spec.source_kind, date(2026, 8, 30))
        for symbol in universe
        for spec in ranking.INPUTS
    }
    monkeypatch.setattr(ranking.screens, "build_universe", lambda _conn: universe)
    monkeypatch.setattr(
        ranking.screens,
        "load_config",
        lambda: {"screens": {"garp": {"conditions": {}}}},
    )
    monkeypatch.setattr(
        ranking.screens,
        "evaluate_screen",
        lambda _universe, _conditions: {
            "survivors": [{"symbol": "A"}, {"symbol": "B"}],
            "gaps": {},
            "evaluated": 3,
        },
    )
    monkeypatch.setattr(ranking, "_provenance", lambda _conn, _rows: provenance)
    result = ranking.calculate(object())
    conn = duckdb.connect(":memory:")
    db.init_schema(conn)
    ranking.install_schema(conn)
    now = dt(2026, 8, 30, 12, tzinfo=UTC)

    accepted = ranking.persist(conn, result, recorded_at=now)
    assert accepted["status"] == "ACCEPTED"
    assert ranking.persist(conn, result, recorded_at=now)["status"] == "REPLAY"
    assert conn.execute("SELECT max(version) FROM schema_migrations").fetchone() == (19,)
    assert conn.execute("SELECT count(*) FROM ranking_input").fetchone() == (
        2 * len(ranking.INPUTS),
    )
    stored = conn.execute(ranking.RECONSTRUCTION_SQL).fetchall()
    assert all(exact for _run_id, _symbol, _score, _rebuilt, exact in stored)
    assert conn.execute("SELECT distinct normalization_cohort FROM ranking_input").fetchall() == [
        ("active_eq",)
    ]
    conn.execute("BEGIN")
    conn.execute(
        "INSERT INTO ranking_component VALUES (?,?,?,?,?,?,?)",
        [accepted["run_id"], "A", "extra", 0.0, 0.0, 0.0, "AVAILABLE"],
    )
    extra_component_check = conn.execute(ranking.RECONSTRUCTION_SQL).fetchall()
    assert not next(row[4] for row in extra_component_check if row[1] == "A")
    conn.execute("ROLLBACK")
    conn.execute("BEGIN")
    conn.execute(
        "DELETE FROM ranking_input WHERE run_id=? AND symbol='A' AND field='pe_ratio'",
        [accepted["run_id"]],
    )
    missing_row_check = conn.execute(ranking.RECONSTRUCTION_SQL).fetchall()
    assert not next(row[4] for row in missing_row_check if row[1] == "A")
    conn.execute("ROLLBACK")

    malformed = {**result, "survivors": [dict(row) for row in result["survivors"]]}
    malformed["survivors"][0] = {**malformed["survivors"][0], "score": 0.123}
    with pytest.raises(ValueError, match="score"):
        ranking.persist(conn, malformed, recorded_at=now)

    universe["A"]["pe_ratio"] = 99.0
    changed = ranking.calculate(object())
    with pytest.raises(ranking.RankingConflict):
        ranking.persist(conn, changed, recorded_at=now)

    assert ranking.verify_run(conn, accepted["run_id"])
    conn.execute(
        "UPDATE ranking_input SET raw_value=999 WHERE run_id=? AND symbol='A' AND field='pe_ratio'",
        [accepted["run_id"]],
    )
    assert not ranking.verify_run(conn, accepted["run_id"])
    with pytest.raises(ranking.RankingConflict, match="integrity"):
        ranking.persist(conn, result, recorded_at=now)
