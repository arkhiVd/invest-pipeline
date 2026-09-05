"""Publish the fixed, privacy-safe DuckDB projection used by the local UI."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime as dt
from pathlib import Path

import duckdb

from invest import kite, ranking, screens, swing, vested, watchlist

SOURCE_DB = Path.cwd() / "data/invest.duckdb"
UI_DB = Path.cwd() / "data/ui/invest-ui.duckdb"
PROJECTION_VERSION = "ui-projection-2026.1"

SENSITIVE_VALUE_RE = re.compile(
    r"(?:\b[A-Z]{2}[A-Z0-9]{9}[0-9]\b|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|"
    r"(?i:(?:access|request)[_-]?token|government[_ -]?id|"
    r"account[_ -]?(?:number|sha256)|DENIED[_-])|\b[a-fA-F0-9]{64}\b)"
)
HASH_COLUMNS = frozenset(
    {
        "article_id",
        "content_sha256",
        "prompt_sha256",
        "source_fingerprint",
        "projection_fingerprint",
        "semantic_config_fingerprint",
        "run_id",
        "event_id",
        "position_id",
        "origin_signal_event_id",
        "state_event_id",
        "result_id",
        "previous_run_id",
        "watchlist_run_id",
        "constituent_snapshot_id",
    }
)

DENIED_NAMES = frozenset(
    {
        "account_sha256",
        "instrument_token",
        "isin",
        "isin2",
        "raw_json",
        "raw_path",
        "source_sha256",
        "user_id",
        "government_id",
        "account_number",
        "account_id",
        "email",
        "folio",
        "access_token",
        "request_token",
    }
)


@dataclass(frozen=True)
class Dataset:
    columns: tuple[str, ...]
    suffix: str = ""
    query: str | None = None


DERIVED_COLUMNS = {
    "ui_screen_survivor": (
        "symbol",
        "screen_id",
        "as_of",
        "source",
        "methodology_version",
        "predicates_json",
        "metrics_json",
    ),
    "ui_swing_watchlist": (
        "rank",
        "symbol",
        "close",
        "as_of",
        "beta",
        "observations",
        "ema10",
        "ema21",
        "ema_state",
        "freshness",
        "gap_reason",
    ),
}

DATASETS: dict[str, Dataset] = {
    "schema_migrations": Dataset(("version", "applied_at")),
    "ingest_watermark": Dataset(("kind", "last_date", "detail", "updated_at")),
    "mf_scheme": Dataset(
        (
            "scheme_code",
            "display_name",
            "name",
            "amc",
            "scheme_type",
            "category",
            "category_sub",
            "category_group_clean",
            "category_group",
            "scheme_plan",
            "scheme_option",
            "first_date",
            "last_date",
            "is_active",
            "is_stale",
            "aaum_cr_quarterly_avg",
            "aaum_quarter",
            "aaum_quarter_end",
        )
    ),
    "mf_return_metrics": Dataset(
        (
            "scheme_code",
            "lookback",
            "fund_return",
            "category_avg_return",
            "result",
            "benchmark",
            "frequency",
            "methodology_version",
            "sources",
            "calculated_at",
            "note",
        )
    ),
    "mf_risk_metrics": Dataset(
        (
            "scheme_code",
            "lookback",
            "sd",
            "category_sd",
            "volatility_class",
            "beta",
            "category_beta",
            "risk_profile",
            "sharpe",
            "upside_cr",
            "category_upside_cr",
            "upside_result",
            "downside_cr",
            "category_downside_cr",
            "downside_result",
            "benchmark",
            "frequency",
            "methodology_version",
            "sources",
            "calculated_at",
            "note",
        )
    ),
    "nifty_pe": Dataset(
        ("nav_date", "pe", "pb", "dy", "close", "source", "fetched_at"),
        " ORDER BY nav_date DESC LIMIT 730",
    ),
    "stock_universe": Dataset(
        (
            "symbol",
            "company_name",
            "series",
            "listing_date",
            "face_value",
            "is_active",
            "source",
            "fetched_at",
        )
    ),
    "stock_fundamentals": Dataset(
        (
            "symbol",
            "as_of",
            "source",
            "company_name",
            "sector",
            "exchange",
            "price",
            "high_52w",
            "distance_from_52w_high_pct",
            "price_to_50dma",
            "price_to_200dma",
            "market_cap_cr",
            "pe_ratio",
            "pb_ratio",
            "roe",
            "roce",
            "dividend_yield",
            "peg_ratio",
            "operating_margin",
            "revenue_growth_yoy",
            "profit_growth_yoy",
            "eps_growth_yoy",
            "debt_to_equity",
            "interest_coverage",
            "promoter_holding",
            "fii_holding",
            "dii_holding",
            "promoter_pledged",
            "methodology_version",
            "fetched_at",
            "avg_roe_3y",
            "avg_roe_5y",
            "avg_roce_3y",
            "avg_roce_5y",
            "revenue_cagr_3y",
            "profit_cagr_3y",
            "eps_cagr_3y",
            "current_ratio",
            "free_cash_flow",
            "free_cash_flow_3y",
            "eps",
            "eps_previous",
            "piotroski_score",
        )
    ),
    "stock_price": Dataset(
        (
            "symbol",
            "trade_date",
            "open",
            "high",
            "low",
            "close",
            "prev_close",
            "volume",
            "source",
            "fetched_at",
        ),
        " WHERE trade_date >= (SELECT max(trade_date) - INTERVAL 400 DAY FROM stock_price) AND symbol IN (SELECT symbol FROM index_constituent WHERE upper(index_name) = 'NIFTY 100' UNION SELECT symbol FROM stock_research_score)",  # noqa: E501
    ),
    "index_close": Dataset(
        ("index_name", "trade_date", "close", "source", "fetched_at"),
        " WHERE trade_date >= (SELECT max(trade_date) - INTERVAL 400 DAY FROM index_close)",
    ),
    "index_constituent": Dataset(
        ("index_name", "symbol", "company_name", "industry", "series", "source", "fetched_at")
    ),
    "stock_research_score": Dataset(
        (
            "symbol",
            "snapshot_date",
            "methodology_version",
            "screens_json",
            "metrics_json",
            "components_json",
            "total_score",
            "rationale",
            "red_flags_json",
            "model",
            "prompt_sha256",
            "scored_at",
        ),
        " WHERE snapshot_date = (SELECT max(snapshot_date) FROM stock_research_score)",
    ),
    "stock_research_run": Dataset(
        (
            "run_id",
            "started_at",
            "candidate_count",
            "budget_calls",
            "attempted_calls",
            "stored_scores",
            "detail",
        ),
        " ORDER BY started_at DESC LIMIT 100",
    ),
    "broker_snapshot_run": Dataset(
        (
            "run_id",
            "broker",
            "snapshot_date",
            "content_sha256",
            "holding_count",
            "position_count",
            "mf_holding_count",
            "fetched_at",
        ),
        " WHERE run_id = (SELECT run_id FROM broker_snapshot_run ORDER BY snapshot_date DESC, fetched_at DESC LIMIT 1)",  # noqa: E501
    ),
    "broker_holding": Dataset(
        (
            "run_id",
            "exchange",
            "tradingsymbol",
            "product",
            "quantity",
            "t1_quantity",
            "used_quantity",
            "average_price",
            "last_price",
            "close_price",
            "pnl",
            "day_change",
            "day_change_percentage",
        ),
        " WHERE run_id = (SELECT run_id FROM broker_snapshot_run ORDER BY snapshot_date DESC, fetched_at DESC LIMIT 1)",  # noqa: E501
    ),
    "broker_mf_holding": Dataset(
        (
            "run_id",
            "fund",
            "quantity",
            "pledged_quantity",
            "average_price",
            "last_price",
            "pnl",
            "last_price_date",
            "tracked_name",
            "mapping_status",
        ),
        query="""
        SELECT h.run_id,h.fund,h.quantity,h.pledged_quantity,h.average_price,
               h.last_price,h.pnl,h.last_price_date,
               CASE WHEN count(s.scheme_code)=1
                    THEN max(coalesce(s.display_name,s.name)) END tracked_name,
               CASE WHEN count(s.scheme_code)=0 THEN 'untracked'
                    WHEN count(s.scheme_code)=1 THEN 'tracked' ELSE 'ambiguous' END mapping_status
        FROM broker_mf_holding h LEFT JOIN mf_scheme s
          ON h.tradingsymbol IN (s.isin,s.isin2)
        WHERE h.run_id=(SELECT run_id FROM broker_snapshot_run
                        ORDER BY snapshot_date DESC,fetched_at DESC LIMIT 1)
        GROUP BY h.run_id,h.fund,h.quantity,h.pledged_quantity,h.average_price,
                 h.last_price,h.pnl,h.last_price_date
        """,
    ),
    "news_article": Dataset(
        ("article_id", "title", "url", "publisher", "source_feed", "published_at", "fetched_at"),
        " WHERE published_at >= (SELECT max(published_at) - INTERVAL 90 DAY FROM news_article) ORDER BY published_at DESC LIMIT 500",  # noqa: E501
    ),
    "news_article_entity": Dataset(
        ("article_id", "symbol", "match_reason", "prefilter_version"),
        " WHERE article_id IN (SELECT article_id FROM news_article WHERE published_at >= (SELECT max(published_at) - INTERVAL 90 DAY FROM news_article) ORDER BY published_at DESC LIMIT 500)",  # noqa: E501
    ),
    "news_classification": Dataset(
        (
            "article_id",
            "symbol",
            "methodology_version",
            "sentiment",
            "event_type",
            "materiality",
            "rationale",
            "cited_url",
            "evidence_scope",
            "model",
            "classified_at",
        ),
        " WHERE article_id IN (SELECT article_id FROM news_article WHERE published_at >= (SELECT max(published_at) - INTERVAL 90 DAY FROM news_article) ORDER BY published_at DESC LIMIT 500)",  # noqa: E501
    ),
    "news_run": Dataset(
        (
            "run_id",
            "started_at",
            "target_count",
            "fetched_items",
            "inserted_articles",
            "prefilter_survivors",
            "budget_calls",
            "attempted_calls",
            "stored_classifications",
            "detail",
        ),
        " ORDER BY started_at DESC LIMIT 100",
    ),
    "vested_snapshot_run": Dataset(
        (
            "run_id",
            "provider",
            "snapshot_date",
            "content_sha256",
            "holding_count",
            "current_value_usd",
            "invested_usd",
            "imported_at",
        ),
        " WHERE run_id = (SELECT run_id FROM vested_snapshot_run ORDER BY snapshot_date DESC, imported_at DESC LIMIT 1)",  # noqa: E501
    ),
    "vested_holding": Dataset(
        (
            "run_id",
            "ticker",
            "name",
            "quantity",
            "current_price_usd",
            "current_value_usd",
            "average_cost_usd",
            "invested_usd",
            "return_usd",
            "return_pct",
        ),
        " WHERE run_id = (SELECT run_id FROM vested_snapshot_run ORDER BY snapshot_date DESC, imported_at DESC LIMIT 1)",  # noqa: E501
    ),
    "signal_run": Dataset(
        (
            "run_id",
            "source_as_of",
            "canonical_cutoff",
            "recorded_at",
            "methodology_version",
            "status",
            "scanned_count",
            "signal_count",
        ),
        " ORDER BY source_as_of DESC LIMIT 100",
    ),  # noqa: E501
    "signal_event": Dataset(
        (
            "event_id",
            "run_id",
            "symbol",
            "signal_date",
            "source_as_of",
            "action",
            "close",
            "ema10",
            "ema21",
            "quantity",
            "sizing_stop",
            "capital_to_deploy",
            "maximum_loss_at_stop",
            "sizing_gap_reason",
            "methodology_version",
            "recorded_at",
        ),
        " ORDER BY signal_date DESC LIMIT 500",
    ),  # noqa: E501
    "research_position": Dataset(
        (
            "position_id",
            "market",
            "symbol",
            "origin_signal_event_id",
            "current_state",
            "state_source_at",
            "state_event_id",
            "methodology_version",
            "entry_sizing_stop",
            "created_at",
            "updated_at",
        ),
        " ORDER BY updated_at DESC LIMIT 500",
    ),  # noqa: E501
    "position_state_event": Dataset(
        (
            "event_id",
            "position_id",
            "from_state",
            "to_state",
            "source_at",
            "recorded_at",
            "methodology_version",
            "actor",
            "evidence_type",
        ),
        " ORDER BY source_at DESC LIMIT 1000",
    ),  # noqa: E501
    "screen_membership_event": Dataset(
        (
            "event_id",
            "run_id",
            "screen_id",
            "symbol",
            "source_as_of",
            "recorded_at",
            "methodology_version",
            "event_type",
            "previous_run_id",
        ),
        " ORDER BY source_as_of DESC LIMIT 2000",
    ),  # noqa: E501
    "watchlist_run": Dataset(
        (
            "run_id",
            "index_name",
            "source_as_of",
            "canonical_cutoff",
            "recorded_at",
            "methodology_version",
            "status",
            "universe_count",
            "selected_count",
        ),
        " ORDER BY source_as_of DESC LIMIT 100",
    ),  # noqa: E501
    "watchlist_symbol_result": Dataset(
        (
            "result_id",
            "run_id",
            "symbol",
            "source_as_of",
            "methodology_version",
            "result",
            "rank",
            "close",
            "beta",
            "observations",
            "evidence_as_of",
        ),
        " ORDER BY source_as_of DESC LIMIT 5000",
    ),  # noqa: E501
    "ranking_methodology": Dataset(
        ("methodology_version", "semantic_config_fingerprint", "registered_at")
    ),
    "ranking_run": Dataset(
        (
            "run_id",
            "source_as_of",
            "recorded_at",
            "methodology_version",
            "survivor_count",
            "available_count",
        ),
        " ORDER BY source_as_of DESC,recorded_at DESC LIMIT 100",
    ),
    "ranking_symbol": Dataset(
        (
            "run_id",
            "symbol",
            "score",
            "research_rank",
            "evidence_completeness",
            "status",
            "missing_components_json",
        ),
        " WHERE run_id IN (SELECT run_id FROM ranking_run ORDER BY source_as_of DESC,recorded_at DESC LIMIT 100) ORDER BY (SELECT source_as_of FROM ranking_run r WHERE r.run_id=ranking_symbol.run_id) DESC,research_rank NULLS LAST,symbol LIMIT 5000",  # noqa: E501
    ),
    "ranking_component": Dataset(
        (
            "run_id",
            "symbol",
            "component",
            "normalized_value",
            "component_weight",
            "weighted_contribution",
            "missing_status",
        ),
        " WHERE run_id IN (SELECT run_id FROM ranking_run ORDER BY source_as_of DESC,recorded_at DESC LIMIT 100) ORDER BY (SELECT source_as_of FROM ranking_run r WHERE r.run_id=ranking_component.run_id) DESC,symbol,component LIMIT 25000",  # noqa: E501
    ),
    "ranking_input": Dataset(
        (
            "run_id",
            "symbol",
            "field",
            "raw_value",
            "unit",
            "source",
            "source_as_of",
            "normalization_cohort",
            "cohort_size",
            "transform",
            "direction",
            "normalized_value",
            "component",
            "input_weight",
            "component_weight",
            "weighted_contribution",
            "missing_status",
        ),
        " WHERE run_id IN (SELECT run_id FROM ranking_run ORDER BY source_as_of DESC,recorded_at DESC LIMIT 100) ORDER BY (SELECT source_as_of FROM ranking_run r WHERE r.run_id=ranking_input.run_id) DESC,symbol,component,field LIMIT 60000",  # noqa: E501
    ),
    "ui_portfolio_performance": Dataset(
        (
            "result_id",
            "provider",
            "account_scope",
            "metric",
            "status",
            "value",
            "currency",
            "coverage_start",
            "coverage_end",
            "methodology_version",
            "assumptions_json",
            "exclusions_json",
            "residuals_json",
            "calculated_at",
        ),
        query="SELECT r.result_id,a.provider,a.account_scope,r.metric,r.status,r.value,"
        "r.currency,r.coverage_start,r.coverage_end,r.methodology_version,"
        "r.assumptions_json,r.exclusions_json,r.residuals_json,r.calculated_at "
        "FROM portfolio_performance_result r JOIN portfolio_account a USING(account_id) "
        "QUALIFY row_number() OVER (PARTITION BY r.account_id,r.metric "
        "ORDER BY r.calculated_at DESC,r.result_id)=1",
    ),
    "ui_portfolio_allocation": Dataset(
        (
            "result_id",
            "dimension",
            "bucket",
            "native_value",
            "base_value",
            "weight",
            "source_as_of",
        ),
        query="SELECT a.result_id,a.dimension,a.bucket,a.native_value,a.base_value,a.weight,"
        "a.source_as_of FROM portfolio_allocation_result a WHERE a.result_id IN ("
        "SELECT result_id FROM portfolio_performance_result QUALIFY row_number() OVER ("
        "PARTITION BY account_id,metric ORDER BY calculated_at DESC,result_id)=1)",
    ),
    "ui_accounting_completeness": Dataset(
        (
            "provider",
            "account_scope",
            "coverage_start",
            "coverage_end",
            "transactions_status",
            "cash_flows_status",
            "income_status",
            "valuations_status",
            "corporate_actions_status",
            "fx_status",
            "assumptions_json",
            "exclusions_json",
            "residuals_json",
            "methodology_version",
            "assessed_at",
        ),
        query="SELECT a.provider,a.account_scope,c.coverage_start,c.coverage_end,"
        "c.transactions_status,c.cash_flows_status,c.income_status,c.valuations_status,"
        "c.corporate_actions_status,c.fx_status,c.assumptions_json,c.exclusions_json,"
        "c.residuals_json,c.methodology_version,c.assessed_at FROM accounting_completeness c "
        "JOIN portfolio_account a USING(account_id) QUALIFY row_number() OVER ("
        "PARTITION BY c.account_id ORDER BY c.assessed_at DESC,c.evidence_id)=1",
    ),
}


def _select(table: str, dataset: Dataset) -> str:
    return dataset.query or f"SELECT {', '.join(dataset.columns)} FROM {table}{dataset.suffix}"


def _fingerprint(conn: duckdb.DuckDBPyConnection, *, source: bool = False) -> str:
    digest = hashlib.sha256()
    datasets = (
        DATASETS
        if source
        else {
            **DATASETS,
            **{name: Dataset(columns) for name, columns in DERIVED_COLUMNS.items()},
        }
    )
    for table, dataset in datasets.items():
        base = (
            _select(table, dataset)
            if source
            else (f"SELECT {', '.join(dataset.columns)} FROM {table}")
        )
        columns = ", ".join(dataset.columns)
        cursor = conn.execute(f"SELECT {columns} FROM ({base}) projected ORDER BY ALL")
        while rows := cursor.fetchmany(1000):
            for row in rows:
                digest.update(json.dumps(row, default=str, separators=(",", ":")).encode())
                digest.update(b"\n")
    return digest.hexdigest()


def _build_screen_projection(conn: duckdb.DuckDBPyConnection) -> int:
    conn.execute(
        "CREATE TABLE ui_screen_survivor (symbol TEXT, screen_id TEXT, as_of DATE, "
        "source TEXT, methodology_version TEXT, predicates_json TEXT, metrics_json TEXT)"
    )
    config = screens.load_config()
    universe = screens.build_universe(conn)
    for screen_id, definition in config["screens"].items():
        result = screens.evaluate_screen(universe, definition["conditions"])
        for survivor in result["survivors"]:
            row = universe[survivor["symbol"]]
            conn.execute(
                "INSERT INTO ui_screen_survivor VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    survivor["symbol"],
                    screen_id,
                    row.get("as_of"),
                    row.get("source"),
                    row.get("methodology_version"),
                    json.dumps(definition["conditions"], sort_keys=True),
                    json.dumps(row, default=str, sort_keys=True),
                ],
            )
    return conn.execute("SELECT count(*) FROM ui_screen_survivor").fetchone()[0]


def _build_swing_projection(conn: duckdb.DuckDBPyConnection) -> dict[str, int]:
    conn.execute(
        "CREATE TABLE ui_swing_watchlist (rank INTEGER, symbol TEXT, close DOUBLE, "
        "as_of DATE, beta DOUBLE, observations INTEGER, ema10 DOUBLE, ema21 DOUBLE, "
        "ema_state TEXT, freshness TEXT, gap_reason TEXT)"
    )
    try:
        config = watchlist.load_config()
        report = watchlist.build_watchlist(conn, config)
    except ValueError:
        return {"ui_swing_watchlist": 0}
    for item in report["picks"]:
        prices = conn.execute(
            "SELECT close FROM stock_price WHERE symbol=? AND close IS NOT NULL "
            "ORDER BY trade_date",
            [item["symbol"]],
        ).fetchall()
        points = swing.ema_crossover([float(row[0]) for row in prices])
        point = points[-1] if points else None
        state = "unavailable"
        if point and point.fast_ema is not None and point.slow_ema is not None:
            state = "above" if point.fast_ema > point.slow_ema else "below_or_equal"
        conn.execute(
            "INSERT INTO ui_swing_watchlist VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'fresh', NULL)",
            [
                item["rank"],
                item["symbol"],
                item["close"],
                item["as_of"],
                item["beta"],
                item["observations"],
                point.fast_ema if point else None,
                point.slow_ema if point else None,
                state,
            ],
        )
    gap_groups = {
        "no_close": report["gaps"]["no_close"],
        "insufficient_beta": report["gaps"]["insufficient_beta"],
        "stale_close": report["gaps"]["stale_close"],
        "price_above_cap": [row["symbol"] for row in report["excluded_by_price"]],
    }
    for reason, symbols in gap_groups.items():
        for symbol in symbols:
            conn.execute(
                "INSERT INTO ui_swing_watchlist "
                "(symbol,freshness,gap_reason,ema_state) VALUES (?, ?, ?, 'unavailable')",
                [symbol, "stale" if reason == "stale_close" else "missing", reason],
            )
    return {
        "ui_swing_watchlist": conn.execute("SELECT count(*) FROM ui_swing_watchlist").fetchone()[0]
    }


def _latest_integrity(conn: duckdb.DuckDBPyConnection) -> dict[str, object]:
    result: dict[str, object] = {}
    rank_run = conn.execute(
        "SELECT run_id FROM ranking_run ORDER BY source_as_of DESC,recorded_at DESC LIMIT 1"
    ).fetchone()
    if rank_run:
        if not ranking.verify_run(conn, rank_run[0]):
            raise RuntimeError("latest ranking run failed integrity")
        result["ranking_run_id"] = rank_run[0]
    broker = conn.execute(
        "SELECT run_id FROM broker_snapshot_run ORDER BY snapshot_date DESC, fetched_at DESC LIMIT 1"  # noqa: E501
    ).fetchone()
    if broker:
        if not kite.snapshot_integrity(conn, broker[0]):
            raise RuntimeError("latest broker snapshot failed integrity")
        result["broker_run_id"] = broker[0]
        result["broker_holding_value"] = conn.execute(
            "SELECT coalesce(sum(quantity * last_price), 0) FROM broker_holding WHERE run_id=?",
            [broker[0]],
        ).fetchone()[0]
        result["broker_mf_value"] = conn.execute(
            "SELECT coalesce(sum(quantity * last_price), 0) FROM broker_mf_holding WHERE run_id=?",
            [broker[0]],
        ).fetchone()[0]
    vested_run = conn.execute(
        "SELECT run_id FROM vested_snapshot_run ORDER BY snapshot_date DESC, imported_at DESC LIMIT 1"  # noqa: E501
    ).fetchone()
    if vested_run:
        if not vested.integrity(conn, vested_run[0]):
            raise RuntimeError("latest Vested snapshot failed integrity")
        result["vested_run_id"] = vested_run[0]
        result["vested_current_value_usd"] = conn.execute(
            "SELECT current_value_usd FROM vested_snapshot_run WHERE run_id=?",
            [vested_run[0]],
        ).fetchone()[0]
        result["vested_invested_usd"] = conn.execute(
            "SELECT invested_usd FROM vested_snapshot_run WHERE run_id=?",
            [vested_run[0]],
        ).fetchone()[0]
    return result


def _publish(source: Path, output: Path) -> dict[str, object]:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp.{os.getpid()}")
    if temporary.exists():
        temporary.unlink()
    src = None
    dst = None
    try:
        src = duckdb.connect(str(source), read_only=True, config={"lock_configuration": True})
        src.execute("BEGIN TRANSACTION")
        integrity = _latest_integrity(src)
        dst = duckdb.connect(str(temporary))
        os.chmod(temporary, 0o600)
        row_counts: dict[str, int] = {}
        for table, dataset in DATASETS.items():
            if dataset.query is None:
                actual = {row[1] for row in src.execute(f"PRAGMA table_info('{table}')").fetchall()}
                missing = sorted(set(dataset.columns) - actual)
                if missing:
                    raise RuntimeError(f"source schema drift for {table}: missing {missing}")
            query = _select(table, dataset)
            description = src.execute(f"DESCRIBE {query}").fetchall()
            definitions = ", ".join(f'"{row[0]}" {row[1]}' for row in description)
            dst.execute(f'CREATE TABLE "{table}" ({definitions})')
            frame = src.execute(query).fetchdf()
            if not frame.empty:
                dst.append(table, frame)
            row_counts[table] = len(frame)
        row_counts["ui_screen_survivor"] = _build_screen_projection(dst)
        row_counts.update(_build_swing_projection(dst))
        source_schema = src.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
        published_at = dt.now(UTC)
        dst.execute(
            "CREATE TABLE projection_metadata (projection_version TEXT NOT NULL, source_schema_version INTEGER NOT NULL, published_at TIMESTAMPTZ NOT NULL, source_fingerprint TEXT NOT NULL, projection_fingerprint TEXT, row_counts_json TEXT NOT NULL, integrity_json TEXT NOT NULL)"  # noqa: E501
        )
        source_fp = _fingerprint(src, source=True)
        dst.execute(
            "INSERT INTO projection_metadata VALUES (?, ?, ?, ?, NULL, ?, ?)",
            [
                PROJECTION_VERSION,
                source_schema,
                published_at,
                source_fp,
                json.dumps(row_counts, sort_keys=True),
                json.dumps(integrity, sort_keys=True),
            ],
        )
        projection_fp = _fingerprint(dst)
        dst.execute("UPDATE projection_metadata SET projection_fingerprint=?", [projection_fp])
        src.execute("COMMIT")
        src.close()
        src = None
        dst.close()
        dst = None
        _verify(temporary, row_counts, projection_fp, integrity)
        os.replace(temporary, output)
        os.chmod(output, 0o600)
        return {
            "published_at": published_at,
            "row_counts": row_counts,
            "fingerprint": projection_fp,
            **integrity,
        }
    finally:
        if dst is not None:
            dst.close()
        if src is not None:
            src.close()
        if temporary.exists():
            temporary.unlink()


def _verify_article_ids(conn: duckdb.DuckDBPyConnection) -> None:
    for article_id, url, title in conn.execute(
        "SELECT article_id,url,title FROM news_article"
    ).fetchall():
        expected = hashlib.sha256(f"{url}\n{title}".encode()).hexdigest()
        if article_id != expected:
            raise RuntimeError("projection contains an invalid news article identifier")


def _verify(
    path: Path,
    expected_counts: dict[str, int],
    expected_fingerprint: str,
    expected_integrity: dict[str, object],
) -> None:
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise RuntimeError("projection must be mode 0600")
    conn = duckdb.connect(str(path), read_only=True)
    try:
        tables = {row[0] for row in conn.execute("SHOW TABLES").fetchall()}
        expected_tables = set(DATASETS) | set(DERIVED_COLUMNS) | {"projection_metadata"}
        if tables != expected_tables:
            raise RuntimeError("projection table allowlist mismatch")
        _verify_article_ids(conn)
        columns = {
            row[0].lower()
            for row in conn.execute("SELECT column_name FROM information_schema.columns").fetchall()
        }
        denied = sorted(columns & DENIED_NAMES)
        if denied:
            raise RuntimeError(f"projection contains denied columns: {denied}")
        for table in tables:
            selected = [
                row[1]
                for row in conn.execute(f"PRAGMA table_info('{table}')").fetchall()
                if row[1] not in HASH_COLUMNS
            ]
            if not selected:
                continue
            selected_sql = ", ".join(f'CAST("{name}" AS VARCHAR)' for name in selected)
            rows = conn.execute(f'SELECT {selected_sql} FROM "{table}"').fetchall()
            if any(
                SENSITIVE_VALUE_RE.search(value)
                for row in rows
                for value in row
                if isinstance(value, str)
            ):
                raise RuntimeError(f"projection contains a protected value in {table}")
        actual_counts = {
            table: conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            for table in (*DATASETS, *DERIVED_COLUMNS)
        }
        if actual_counts != expected_counts:
            raise RuntimeError("projection row-count verification failed")
        if _fingerprint(conn) != expected_fingerprint:
            raise RuntimeError("projection fingerprint verification failed")
        if "broker_run_id" in expected_integrity:
            broker_value = conn.execute(
                "SELECT coalesce(sum(quantity * last_price), 0) FROM broker_holding"
            ).fetchone()[0]
            mf_value = conn.execute(
                "SELECT coalesce(sum(quantity * last_price), 0) FROM broker_mf_holding"
            ).fetchone()[0]
            if not math.isclose(
                broker_value,
                expected_integrity["broker_holding_value"],
                rel_tol=1e-12,
                abs_tol=1e-9,
            ) or not math.isclose(
                mf_value,
                expected_integrity["broker_mf_value"],
                rel_tol=1e-12,
                abs_tol=1e-9,
            ):
                raise RuntimeError("projected broker totals verification failed")
        if "vested_run_id" in expected_integrity:
            totals = conn.execute(
                "SELECT current_value_usd, invested_usd FROM vested_snapshot_run"
            ).fetchone()
            expected = (
                expected_integrity["vested_current_value_usd"],
                expected_integrity["vested_invested_usd"],
            )
            if totals != expected:
                raise RuntimeError("projected Vested totals verification failed")
    finally:
        conn.close()


def publish() -> dict[str, object]:
    """Publish from and to fixed production paths. The CLI accepts no paths."""
    return _publish(SOURCE_DB, UI_DB)


def main() -> int:
    result = publish()
    print(f"UI projection published: fingerprint={result['fingerprint']} tables={len(DATASETS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
