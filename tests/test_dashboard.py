import inspect
from datetime import UTC, timedelta
from datetime import datetime as dt
from pathlib import Path

import duckdb
import pytest
from streamlit.testing.v1 import AppTest

from invest import (
    accounting,
    dashboard,
    db,
    kite,
    performance,
    ranking,
    tracking,
    ui_data,
    ui_projection,
)

APP = Path(__file__).parents[1] / "invest" / "dashboard.py"


def seed_rank(conn):
    component_weights = {item.name: item.weight for item in ranking.COMPONENTS}
    inputs = []
    for spec in ranking.INPUTS:
        inputs.append(
            {
                "field": spec.field,
                "raw_value": 1.0,
                "unit": spec.unit,
                "source": "fixture",
                "source_as_of": dt(2026, 8, 29).date(),
                "normalization_cohort": ranking.COHORT,
                "cohort_size": 100,
                "transform": ranking.TRANSFORM,
                "direction": spec.direction,
                "normalized_value": 0.5,
                "component": spec.component,
                "input_weight": spec.input_weight,
                "component_weight": component_weights[spec.component],
                "weighted_contribution": 0.5
                * spec.input_weight
                * component_weights[spec.component],
                "missing_status": "AVAILABLE",
            }
        )
    components = [
        {
            "component": item.name,
            "normalized_value": 1.0 if item.name == "evidence_completeness" else 0.5,
            "component_weight": item.weight,
            "weighted_contribution": 0.0 if item.weight == 0 else 0.5 * item.weight,
            "missing_status": "AVAILABLE",
        }
        for item in ranking.COMPONENTS
    ]
    ranking.persist(
        conn,
        {
            "methodology_version": ranking.METHODOLOGY,
            "source_as_of": dt(2026, 8, 29).date(),
            "survivors": [
                {
                    "symbol": "SAFE",
                    "score": 0.5,
                    "research_rank": 1,
                    "evidence_completeness": 1.0,
                    "status": "AVAILABLE",
                    "missing_components": [],
                    "inputs": inputs,
                    "components": components,
                }
            ],
        },
        recorded_at=dt(2026, 8, 29, 4, tzinfo=UTC),
    )


def projection(tmp_path):
    source = tmp_path / "source.duckdb"
    output = tmp_path / "ui.duckdb"
    conn = db.connect(str(source))
    db.init_schema(conn)
    tracking.install_schema(conn)
    ranking.install_schema(conn)
    accounting.install_schema(conn)
    conn.execute(
        "INSERT INTO portfolio_account VALUES (?,?,?,?,?,?)",
        ["vested", "VESTED", "US", "USD", "UNPROVEN", dt(2026, 8, 31, tzinfo=UTC)],
    )
    accounting.store_completeness(
        conn,
        account_id="vested",
        coverage_start=dt(2025, 12, 31).date(),
        coverage_end=dt(2026, 8, 31).date(),
        statuses={
            "transactions": "ESTIMATED",
            "cash_flows": "COMPLETE",
            "income": "COMPLETE",
            "valuations": "ESTIMATED",
            "corporate_actions": "MISSING",
            "fx": "COMPLETE",
        },
        assumptions=["cash carried three days"],
        exclusions=["managed-product membership unavailable"],
        residuals=[],
        methodology_version="portfolio-completeness-2026.1",
    )
    performance.store_result(
        conn,
        performance.result_payload(
            account_id="vested",
            metric="XIRR",
            status="ESTIMATED",
            value=0.289412,
            currency="USD",
            coverage_start=dt(2025, 12, 31).date(),
            coverage_end=dt(2026, 8, 31).date(),
            assumptions=["cash carried three days"],
            exclusions=[],
            residuals=[],
            inputs={"fixture": True},
        ),
        dt(2026, 8, 31, tzinfo=UTC),
    )
    allocation_result = performance.result_payload(
        account_id="vested",
        metric="ALLOCATION",
        status="EXACT",
        value=100,
        currency="USD",
        coverage_start=dt(2026, 8, 31).date(),
        coverage_end=dt(2026, 8, 31).date(),
        assumptions=[],
        exclusions=[],
        residuals=[],
        inputs={"fixture_allocation": True},
    )
    performance.store_result(conn, allocation_result, dt(2026, 8, 31, tzinfo=UTC))
    conn.execute(
        "INSERT INTO portfolio_allocation_result VALUES (?,?,?,?,?,?,?)",
        [
            allocation_result["result_id"],
            "INSTRUMENT",
            "SAFE",
            100,
            9545.09,
            1,
            dt(2026, 8, 31).date(),
        ],
    )
    seed_rank(conn)
    conn.execute(
        "INSERT INTO ingest_watermark VALUES ('prices','2026-08-29','fixture',current_timestamp)"
    )
    conn.execute(
        "INSERT INTO stock_research_score VALUES "
        "('SAFE','2026-08-29','m1','{malformed','{}','{}',10,"
        "'<script>alert(1)</script>','[]','fixture','abc',current_timestamp)"
    )
    kite.store_snapshot(
        conn,
        {"user_id": "fixture-user"},
        [
            {
                "exchange": "NSE",
                "tradingsymbol": "SAFE",
                "product": "CNC",
                "instrument_token": 1,
                "isin": "INE000A00001",
                "quantity": 1,
                "t1_quantity": 0,
                "used_quantity": 0,
                "average_price": 10,
                "last_price": 11,
                "close_price": 10,
                "pnl": 1,
                "day_change": 1,
                "day_change_percentage": 10,
            }
        ],
        {"net": [], "day": []},
        [],
        fetched_at=dt(2026, 8, 29, 3, tzinfo=UTC),
    )
    conn.close()
    ui_projection._publish(source, output)
    return output


def app(tmp_path, monkeypatch):
    path = projection(tmp_path)
    monkeypatch.setattr(ui_data, "UI_DB", path)
    return AppTest.from_file(APP).run(timeout=10)


def test_query_layer_denies_production_and_unknown_queries(tmp_path, monkeypatch):
    monkeypatch.setattr(ui_data, "UI_DB", ui_data.PRODUCTION_DB)
    with pytest.raises(ValueError, match="production"):
        ui_data.connect()
    monkeypatch.undo()
    with pytest.raises(ValueError, match="allowlist"):
        ui_data.query("SELECT secret")
    assert all("SELECT *" not in sql.upper() for sql in ui_data.QUERIES.values())
    assert all("LIMIT" in sql.upper() for sql in ui_data.QUERIES.values())


def test_home_and_every_page_render_static_projection(tmp_path, monkeypatch):
    at = app(tmp_path, monkeypatch)
    assert not at.exception
    assert at.title[0].value == "Invest research dashboard"
    assert any("No trade instruction" in item.value for item in at.caption)
    for page in (
        "pages/research.py",
        "pages/swing.py",
        "pages/mf_vbrs.py",
        "pages/india.py",
        "pages/us.py",
        "pages/news.py",
        "pages/symbol.py",
    ):
        at.switch_page(page).run(timeout=10)
        assert not at.exception, page


def test_us_page_shows_labeled_source_gated_performance(tmp_path, monkeypatch):
    at = app(tmp_path, monkeypatch)
    at.switch_page("pages/us.py").run(timeout=10)
    assert not at.exception
    assert any(item.value == "Portfolio accounting" for item in at.subheader)
    assert any("managed-product attribution" in item.value for item in at.warning)


def test_direct_namespaced_symbol_route(tmp_path, monkeypatch):
    at = app(tmp_path, monkeypatch)
    at.switch_page("pages/symbol.py")
    at.query_params = {"market": "IN", "ticker": "SAFE"}
    at.run(timeout=10)
    assert not at.exception
    assert any("IN namespace" in item.value for item in at.caption)
    assert not any("Malformed" in item.value or "Unknown" in item.value for item in at.error)


def test_malformed_narrative_is_not_rendered_as_html(tmp_path, monkeypatch):
    projection_path = projection(tmp_path)
    monkeypatch.setattr(ui_data, "UI_DB", projection_path)
    at = AppTest.from_string("from invest import dashboard\ndashboard.research()").run(timeout=10)
    assert not at.exception
    source = inspect.getsource(__import__("invest.dashboard", fromlist=["dashboard"]))
    assert "unsafe_allow_html" not in source
    assert any("untrusted narrative" in item.value for item in at.warning)


def test_missing_projection_is_explicit_error(tmp_path, monkeypatch):
    monkeypatch.setattr(ui_data, "UI_DB", tmp_path / "missing.duckdb")
    at = AppTest.from_file(APP).run(timeout=10)
    assert not at.exception
    assert at.error
    assert "Query unavailable" in at.error[0].value
    assert any(metric.value == "Unavailable" for metric in at.metric)


def test_navigation_contract_is_bookmarkable():
    source = inspect.getsource(dashboard.main)
    for route in (
        'url_path="research"',
        'url_path="swing"',
        'url_path="mf-vbrs"',
        'url_path="india"',
        'url_path="us"',
        'url_path="news"',
        'url_path="symbol"',
    ):
        assert route in source
    assert "st.navigation" in source
    assert "query_params" in source


def test_stale_label_and_localhost_launcher():
    assert dashboard._age_label(dt.now(UTC) - timedelta(days=4)) == "stale"
    assert dashboard._age_label(dt.now(UTC)) == "fresh"
    assert (
        dashboard._watermark_status({"detail": "fail-open source", "last_date": None})
        == "failed-open"
    )
    assert dashboard._watermark_status({"detail": "error", "last_date": None}) == "failed-loud"
    launcher = (Path(__file__).parents[1] / "deploy" / "run-dashboard.sh").read_text()
    assert "--server.address=127.0.0.1" in launcher
    assert "0.0.0.0" not in launcher
    assert "--server.fileWatcherType=none" in launcher


def test_symbol_validation_and_csv_formula_escaping():
    assert dashboard.SYMBOL_RE.fullmatch("ACME&CO")
    assert dashboard.SYMBOL_RE.fullmatch("ZEAL.B")
    assert not dashboard.SYMBOL_RE.fullmatch("../../secret")
    catalog = {"IN": {"ACME&CO"}, "US": {"NOVA"}}
    assert dashboard._requested_symbol(" nova ", "us", catalog) == ("US:NOVA", None)
    assert dashboard._requested_symbol("unknown", "US", catalog) == (
        "US:UNKNOWN",
        "unknown",
    )
    assert dashboard._requested_symbol("../../secret", "IN", catalog)[1] == "malformed"
    linked = dashboard._symbol_links(
        __import__("pandas").DataFrame({"symbol": ["ACME&CO"]}), "symbol", "IN"
    )
    assert linked.iloc[0].detail_url == "/symbol?market=IN&ticker=ACME%26CO"
    frame = __import__("pandas").DataFrame(
        {
            "symbol": ["SAFE", "SAFE", "SAFE"],
            "rationale": ["=HYPERLINK('bad')", "\t=1+1", " \r@SUM(1,1)"],
            "detail_url": ["x", "x", "x"],
        }
    )
    exported = dashboard._csv_bytes(frame).decode()
    assert "'=HYPERLINK" in exported
    assert "'\t=1+1" in exported
    assert "' \r@SUM" in exported
    assert "detail_url" not in exported


def test_projection_cannot_be_written(tmp_path):
    path = projection(tmp_path)
    conn = duckdb.connect(str(path), read_only=True)
    with pytest.raises(duckdb.InvalidInputException):
        conn.execute("DELETE FROM ingest_watermark")
    conn.close()


def test_rank_queries_preserve_full_latest_projected_run():
    assert "LIMIT 5000" in ui_data.QUERIES["research_rank"]
    assert "LIMIT 25000" in ui_data.QUERIES["rank_components"]
    assert "LIMIT 60000" in ui_data.QUERIES["rank_inputs"]


def test_research_page_shows_deterministic_rank_components_and_provenance(tmp_path, monkeypatch):
    at = app(tmp_path, monkeypatch)
    at.switch_page("pages/research.py").run(timeout=10)
    assert not at.exception
    subheaders = {item.value for item in at.subheader}
    captions = " ".join(item.value for item in at.caption)
    assert "Deterministic research rank" in subheaders
    assert "Rank components" in subheaders
    assert "Rank inputs and provenance" in subheaders
    assert "Codex scores and news are not rank inputs" in captions
    assert "No trade instruction is produced" in captions
    assert any("SAFE" in item.value.to_string() for item in at.dataframe)


def test_swing_page_uses_immutable_signal_events_not_transient_scan():
    assert "FROM signal_event" in ui_data.QUERIES["signal_history"]
    assert "ui_swing_signal" not in ui_data.QUERIES["signal_history"]
    source = inspect.getsource(dashboard.swing)
    assert '"signal_history"' in source
    assert '_table("swing_signals"' not in source
