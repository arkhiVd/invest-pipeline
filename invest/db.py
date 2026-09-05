"""DuckDB schema and idempotent load helpers (schema v7).

Design rules (SPEC.md):
- Every metric row carries benchmark / frequency / lookback /
  methodology_version / calculated_at as NOT NULL.
- All loads are upserts: replaying identical input changes nothing.

v2 (T1.5): nullable `note` on both metric tables — machine-readable
insufficiency reasons.
v3 (T2.2): `nifty_pe` daily index-valuation snapshots (PE/PB/DY strings
normalized to floats at ingest; one row per day, latest wins).
v4 (T3.2a): stock-fundamental snapshots plus retained NSE filing metadata.
v5 (T3.2b): official universe, daily price history, ingest watermarks.
v6 (T3.2b): parsed XBRL filing contexts and facts (compact canonical store).
v7 (T3.2b): crawl-skip tombstones for symbols with no retainable filings,
so the pending queue cannot wedge on permanent zeros.
v8 (T3.2c): computed-fundamental columns (3Y/5Y averages, 3Y CAGRs) on
stock_fundamentals for the XBRL-derived source.
v9 (T3 audit correction): direct EPS/current-ratio, 3Y FCF, and canonical
shareholding fields needed by exact screen predicates.
v10 (T3 reconciliation): persisted per-section discovery status and selected
URL outcomes; crawl completeness no longer means "any filing exists".
v11 (T4.2): official index closes and constituent membership.
v12 (T3.5): immutable survivor snapshots, component scoring, and per-run call
accounting for the LLM budget gate.
v13 (T3.6): content-fingerprint delivery ledger for idempotent digests.
v14 (T5.2): content-addressed broker snapshot, holding, and position tables.
v15 (T5.6): Coin mutual-fund holdings within the same atomic broker snapshot.
v16 (T6): immutable RSS articles, deterministic entity links, bounded headline
classification attempts, and schema-constrained classifications.
v17 (T7): content-addressed Vested US-equity snapshots and holdings.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import UTC, date
from datetime import datetime as dt

import duckdb

SCHEMA_VERSION = 17

_DDL = [
    """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version    INTEGER PRIMARY KEY,
        applied_at TIMESTAMP NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mf_scheme (
        scheme_code          BIGINT PRIMARY KEY,
        display_name         TEXT,
        name                 TEXT NOT NULL,
        amc                  TEXT,
        isin                 TEXT,
        isin2                TEXT,
        scheme_type          TEXT,
        category             TEXT,
        category_sub         TEXT,
        category_group_clean TEXT,
        category_group       TEXT,
        scheme_plan          TEXT,
        scheme_option        TEXT,
        first_date           DATE,
        last_date            DATE,
        is_active            BOOLEAN DEFAULT TRUE,
        is_stale             BOOLEAN DEFAULT FALSE,
        txic_code            TEXT,
        aaum_cr_quarterly_avg DOUBLE,
        aaum_quarter         TEXT,
        aaum_quarter_end     DATE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mf_nav (
        scheme_code BIGINT NOT NULL REFERENCES mf_scheme(scheme_code),
        nav_date    DATE   NOT NULL,
        nav         DOUBLE NOT NULL,
        PRIMARY KEY (scheme_code, nav_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mf_return_metrics (
        scheme_code        BIGINT NOT NULL REFERENCES mf_scheme(scheme_code),
        lookback           TEXT   NOT NULL,
        fund_return        DOUBLE NOT NULL,
        category_avg_return DOUBLE,
        result             TEXT,
        -- mandatory methodology fields (SPEC)
        benchmark          TEXT   NOT NULL,
        frequency          TEXT   NOT NULL,
        methodology_version TEXT  NOT NULL,
        sources            TEXT,
        calculated_at      TIMESTAMP NOT NULL,
        PRIMARY KEY (scheme_code, lookback)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mf_risk_metrics (
        scheme_code        BIGINT NOT NULL REFERENCES mf_scheme(scheme_code),
        lookback           TEXT   NOT NULL,
        sd                 DOUBLE,
        category_sd        DOUBLE,
        volatility_class   TEXT,
        beta               DOUBLE,
        category_beta      DOUBLE,
        risk_profile       TEXT,
        sharpe             DOUBLE,
        upside_cr          DOUBLE,
        category_upside_cr DOUBLE,
        upside_result      TEXT,
        downside_cr        DOUBLE,
        category_downside_cr DOUBLE,
        downside_result    TEXT,
        -- mandatory methodology fields (SPEC)
        benchmark          TEXT   NOT NULL,
        frequency          TEXT   NOT NULL,
        methodology_version TEXT  NOT NULL,
        sources            TEXT,
        calculated_at      TIMESTAMP NOT NULL,
        PRIMARY KEY (scheme_code, lookback, benchmark, frequency)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS nifty_pe (
        nav_date    DATE PRIMARY KEY,
        pe          DOUBLE,
        pb          DOUBLE,
        dy          DOUBLE,
        close       DOUBLE,
        source      TEXT NOT NULL,
        fetched_at  TIMESTAMP NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stock_fundamentals (
        symbol                    TEXT NOT NULL,
        as_of                     DATE NOT NULL,
        source                    TEXT NOT NULL,
        company_name              TEXT,
        sector                    TEXT,
        exchange                  TEXT,
        price                     DOUBLE,
        high_52w                  DOUBLE,
        distance_from_52w_high_pct DOUBLE,
        price_to_50dma            DOUBLE,
        price_to_200dma           DOUBLE,
        market_cap_cr             DOUBLE,
        pe_ratio                  DOUBLE,
        pb_ratio                  DOUBLE,
        roe                       DOUBLE,
        roce                      DOUBLE,
        dividend_yield            DOUBLE,
        peg_ratio                 DOUBLE,
        operating_margin          DOUBLE,
        revenue_growth_yoy        DOUBLE,
        profit_growth_yoy         DOUBLE,
        eps_growth_yoy            DOUBLE,
        debt_to_equity            DOUBLE,
        interest_coverage         DOUBLE,
        promoter_holding          DOUBLE,
        fii_holding               DOUBLE,
        dii_holding               DOUBLE,
        promoter_pledged          BOOLEAN,
        raw_json                  TEXT,
        methodology_version       TEXT NOT NULL,
        fetched_at                TIMESTAMP NOT NULL,
        PRIMARY KEY (symbol, as_of, source)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stock_filing (
        xbrl_url       TEXT PRIMARY KEY,
        symbol         TEXT NOT NULL,
        source         TEXT NOT NULL,
        filing_type    TEXT NOT NULL,
        period_end     DATE,
        consolidation  TEXT,
        taxonomy       TEXT,
        content_sha256 TEXT,
        raw_path       TEXT,
        fetched_at     TIMESTAMP NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stock_universe (
        symbol       TEXT PRIMARY KEY,
        company_name TEXT,
        series       TEXT,
        isin         TEXT,
        listing_date DATE,
        face_value   DOUBLE,
        is_active    BOOLEAN DEFAULT TRUE,
        source       TEXT NOT NULL,
        fetched_at   TIMESTAMP NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stock_price (
        symbol     TEXT NOT NULL,
        trade_date DATE NOT NULL,
        open       DOUBLE,
        high       DOUBLE,
        low        DOUBLE,
        close      DOUBLE,
        prev_close DOUBLE,
        volume     BIGINT,
        source     TEXT NOT NULL,
        fetched_at TIMESTAMP NOT NULL,
        PRIMARY KEY (symbol, trade_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ingest_watermark (
        kind       TEXT PRIMARY KEY,
        last_date  DATE NOT NULL,
        detail     TEXT,
        updated_at TIMESTAMP NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stock_filing_context (
        xbrl_url   TEXT NOT NULL,
        context_id TEXT NOT NULL,
        start_date DATE,
        end_date   DATE,
        instant    DATE,
        dimensions TEXT,
        PRIMARY KEY (xbrl_url, context_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stock_filing_fact (
        xbrl_url    TEXT NOT NULL,
        fact_name   TEXT NOT NULL,
        context_ref TEXT NOT NULL,
        value       TEXT,
        unit_ref    TEXT,
        decimals    TEXT,
        PRIMARY KEY (xbrl_url, fact_name, context_ref)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS index_close (
        index_name TEXT NOT NULL,
        trade_date DATE NOT NULL,
        close      DOUBLE NOT NULL,
        source     TEXT NOT NULL,
        fetched_at TIMESTAMP NOT NULL,
        PRIMARY KEY (index_name, trade_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS index_constituent (
        index_name   TEXT NOT NULL,
        symbol       TEXT NOT NULL,
        company_name TEXT NOT NULL,
        industry     TEXT,
        isin         TEXT NOT NULL,
        series       TEXT NOT NULL,
        source       TEXT NOT NULL,
        fetched_at   TIMESTAMP NOT NULL,
        PRIMARY KEY (index_name, symbol)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stock_crawl_skip (
        symbol     TEXT PRIMARY KEY,
        reason     TEXT NOT NULL,
        checked_at TIMESTAMP NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stock_crawl_status (
        symbol               TEXT PRIMARY KEY,
        policy_version       TEXT NOT NULL,
        legacy_ok            BOOLEAN NOT NULL,
        integrated_ok        BOOLEAN NOT NULL,
        shareholding_ok      BOOLEAN NOT NULL,
        legacy_refs          INTEGER NOT NULL,
        integrated_refs      INTEGER NOT NULL,
        shareholding_refs    INTEGER NOT NULL,
        selected_refs        INTEGER NOT NULL,
        stored_refs          INTEGER NOT NULL,
        not_found_refs       INTEGER NOT NULL,
        financial_selected   INTEGER NOT NULL,
        financial_stored     INTEGER NOT NULL,
        complete             BOOLEAN NOT NULL,
        usable_financial     BOOLEAN NOT NULL,
        detail               TEXT,
        checked_at           TIMESTAMP NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stock_crawl_ref (
        symbol          TEXT NOT NULL,
        xbrl_url         TEXT NOT NULL,
        filing_type      TEXT NOT NULL,
        period_end       DATE,
        outcome          TEXT NOT NULL,
        detail           TEXT,
        policy_version   TEXT NOT NULL,
        checked_at       TIMESTAMP NOT NULL,
        PRIMARY KEY (symbol, xbrl_url)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stock_research_score (
        symbol              TEXT NOT NULL,
        snapshot_date       DATE NOT NULL,
        methodology_version TEXT NOT NULL,
        screens_json        TEXT NOT NULL,
        metrics_json        TEXT NOT NULL,
        components_json     TEXT NOT NULL,
        total_score         INTEGER NOT NULL,
        rationale           TEXT NOT NULL,
        red_flags_json      TEXT NOT NULL,
        model               TEXT NOT NULL,
        prompt_sha256       TEXT NOT NULL,
        scored_at           TIMESTAMP NOT NULL,
        PRIMARY KEY (symbol, snapshot_date, methodology_version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stock_research_run (
        run_id               TEXT PRIMARY KEY,
        started_at           TIMESTAMP NOT NULL,
        candidate_count      INTEGER NOT NULL,
        budget_calls         INTEGER NOT NULL,
        attempted_calls      INTEGER NOT NULL,
        stored_scores        INTEGER NOT NULL,
        detail               TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stock_research_attempt (
        symbol               TEXT NOT NULL,
        snapshot_date        DATE NOT NULL,
        methodology_version  TEXT NOT NULL,
        attempts             INTEGER NOT NULL,
        last_error           TEXT,
        last_attempt_at      TIMESTAMP NOT NULL,
        PRIMARY KEY (symbol, snapshot_date, methodology_version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stock_research_delivery (
        content_sha256 TEXT PRIMARY KEY,
        channel        TEXT NOT NULL,
        delivered_at   TIMESTAMP NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS broker_snapshot_run (
        run_id          TEXT PRIMARY KEY,
        broker          TEXT NOT NULL,
        account_sha256  TEXT NOT NULL,
        snapshot_date   DATE NOT NULL,
        content_sha256  TEXT NOT NULL,
        holding_count   INTEGER NOT NULL,
        position_count  INTEGER NOT NULL,
        mf_holding_count INTEGER NOT NULL,
        fetched_at      TIMESTAMP NOT NULL,
        UNIQUE (broker, account_sha256, snapshot_date, content_sha256)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS broker_holding (
        run_id                TEXT NOT NULL REFERENCES broker_snapshot_run(run_id),
        exchange              TEXT NOT NULL,
        tradingsymbol         TEXT NOT NULL,
        product               TEXT NOT NULL,
        instrument_token      BIGINT,
        isin                  TEXT,
        quantity              DOUBLE NOT NULL,
        t1_quantity           DOUBLE NOT NULL,
        used_quantity         DOUBLE NOT NULL,
        average_price         DOUBLE NOT NULL,
        last_price            DOUBLE NOT NULL,
        close_price           DOUBLE NOT NULL,
        pnl                   DOUBLE NOT NULL,
        day_change            DOUBLE NOT NULL,
        day_change_percentage DOUBLE NOT NULL,
        PRIMARY KEY (run_id, exchange, tradingsymbol, product)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS broker_mf_holding (
        run_id             TEXT NOT NULL REFERENCES broker_snapshot_run(run_id),
        tradingsymbol      TEXT NOT NULL,
        fund               TEXT NOT NULL,
        quantity           DOUBLE NOT NULL,
        pledged_quantity   DOUBLE NOT NULL,
        average_price      DOUBLE NOT NULL,
        last_price         DOUBLE NOT NULL,
        pnl                DOUBLE NOT NULL,
        last_price_date    DATE,
        PRIMARY KEY (run_id, tradingsymbol, fund)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS broker_position (
        run_id             TEXT NOT NULL REFERENCES broker_snapshot_run(run_id),
        scope              TEXT NOT NULL,
        exchange           TEXT NOT NULL,
        tradingsymbol      TEXT NOT NULL,
        product            TEXT NOT NULL,
        instrument_token   BIGINT,
        quantity           DOUBLE NOT NULL,
        overnight_quantity DOUBLE NOT NULL,
        multiplier         DOUBLE NOT NULL,
        average_price      DOUBLE NOT NULL,
        last_price         DOUBLE NOT NULL,
        close_price        DOUBLE NOT NULL,
        pnl                DOUBLE NOT NULL,
        m2m                DOUBLE NOT NULL,
        unrealised         DOUBLE NOT NULL,
        realised           DOUBLE NOT NULL,
        buy_quantity       DOUBLE NOT NULL,
        buy_price          DOUBLE NOT NULL,
        buy_value          DOUBLE NOT NULL,
        sell_quantity      DOUBLE NOT NULL,
        sell_price         DOUBLE NOT NULL,
        sell_value         DOUBLE NOT NULL,
        PRIMARY KEY (run_id, scope, exchange, tradingsymbol, product)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS news_article (
        article_id     TEXT PRIMARY KEY,
        title          TEXT NOT NULL,
        url            TEXT NOT NULL,
        publisher      TEXT NOT NULL,
        source_feed    TEXT NOT NULL,
        published_at   TIMESTAMP NOT NULL,
        fetched_at     TIMESTAMP NOT NULL,
        UNIQUE (url, title)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS news_article_entity (
        article_id         TEXT NOT NULL REFERENCES news_article(article_id),
        symbol             TEXT NOT NULL REFERENCES stock_universe(symbol),
        match_reason       TEXT NOT NULL,
        prefilter_version  TEXT NOT NULL,
        PRIMARY KEY (article_id, symbol)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS news_classification (
        article_id          TEXT NOT NULL REFERENCES news_article(article_id),
        symbol              TEXT NOT NULL,
        methodology_version TEXT NOT NULL,
        sentiment           TEXT NOT NULL CHECK (sentiment IN ('positive', 'negative', 'neutral')),
        event_type          TEXT NOT NULL CHECK (event_type IN (
            'earnings', 'contract', 'corporate_action', 'governance',
            'regulatory', 'management', 'analyst', 'other'
        )),
        materiality         INTEGER NOT NULL CHECK (materiality BETWEEN 0 AND 3),
        rationale           TEXT NOT NULL,
        cited_url           TEXT NOT NULL,
        evidence_scope      TEXT NOT NULL CHECK (evidence_scope = 'headline-only'),
        model               TEXT NOT NULL,
        classified_at       TIMESTAMP NOT NULL,
        PRIMARY KEY (article_id, symbol, methodology_version),
        FOREIGN KEY (article_id, symbol) REFERENCES news_article_entity(article_id, symbol)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS news_classification_attempt (
        article_id          TEXT NOT NULL,
        symbol              TEXT NOT NULL,
        methodology_version TEXT NOT NULL,
        attempts            INTEGER NOT NULL,
        last_error          TEXT,
        last_attempt_at     TIMESTAMP NOT NULL,
        PRIMARY KEY (article_id, symbol, methodology_version),
        FOREIGN KEY (article_id, symbol) REFERENCES news_article_entity(article_id, symbol)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS vested_snapshot_run (
        run_id TEXT PRIMARY KEY, provider TEXT NOT NULL, snapshot_date DATE NOT NULL,
        source_sha256 TEXT NOT NULL UNIQUE, content_sha256 TEXT NOT NULL,
        holding_count INTEGER NOT NULL, current_value_usd DOUBLE NOT NULL,
        invested_usd DOUBLE NOT NULL, imported_at TIMESTAMP NOT NULL,
        UNIQUE (snapshot_date, content_sha256)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS vested_holding (
        run_id TEXT NOT NULL REFERENCES vested_snapshot_run(run_id),
        ticker TEXT NOT NULL, name TEXT NOT NULL, quantity DOUBLE NOT NULL,
        current_price_usd DOUBLE NOT NULL, current_value_usd DOUBLE NOT NULL,
        average_cost_usd DOUBLE NOT NULL, invested_usd DOUBLE NOT NULL,
        return_usd DOUBLE NOT NULL, return_pct DOUBLE NOT NULL,
        PRIMARY KEY (run_id, ticker)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS news_run (
        run_id               TEXT PRIMARY KEY,
        started_at           TIMESTAMP NOT NULL,
        target_count         INTEGER NOT NULL,
        fetched_items        INTEGER NOT NULL,
        inserted_articles    INTEGER NOT NULL,
        prefilter_survivors  INTEGER NOT NULL,
        budget_calls         INTEGER NOT NULL,
        attempted_calls      INTEGER NOT NULL,
        stored_classifications INTEGER NOT NULL,
        detail               TEXT
    )
    """,
]

# Applied after _DDL, in key order; each version recorded once.
# v1 = baseline DDL above (recorded for fresh installs).
_MIGRATIONS: dict[int, list[str]] = {
    1: [],
    2: [
        "ALTER TABLE mf_return_metrics ADD COLUMN IF NOT EXISTS note TEXT",
        "ALTER TABLE mf_risk_metrics ADD COLUMN IF NOT EXISTS note TEXT",
    ],
    3: [],  # nifty_pe table ships in baseline DDL (CREATE IF NOT EXISTS)
    4: [],  # stock tables ship in baseline DDL (CREATE IF NOT EXISTS)
    5: [],  # universe/price/watermark tables ship in baseline DDL likewise
    6: [],  # parsed-filing context/fact tables ship in baseline DDL likewise
    7: [],  # crawl-skip tombstone table ships in baseline DDL likewise
    8: [
        "ALTER TABLE stock_fundamentals ADD COLUMN IF NOT EXISTS avg_roe_3y DOUBLE",
        "ALTER TABLE stock_fundamentals ADD COLUMN IF NOT EXISTS avg_roe_5y DOUBLE",
        "ALTER TABLE stock_fundamentals ADD COLUMN IF NOT EXISTS avg_roce_3y DOUBLE",
        "ALTER TABLE stock_fundamentals ADD COLUMN IF NOT EXISTS avg_roce_5y DOUBLE",
        "ALTER TABLE stock_fundamentals ADD COLUMN IF NOT EXISTS revenue_cagr_3y DOUBLE",
        "ALTER TABLE stock_fundamentals ADD COLUMN IF NOT EXISTS profit_cagr_3y DOUBLE",
        "ALTER TABLE stock_fundamentals ADD COLUMN IF NOT EXISTS eps_cagr_3y DOUBLE",
    ],
    9: [
        "ALTER TABLE stock_fundamentals ADD COLUMN IF NOT EXISTS current_ratio DOUBLE",
        "ALTER TABLE stock_fundamentals ADD COLUMN IF NOT EXISTS free_cash_flow DOUBLE",
        "ALTER TABLE stock_fundamentals ADD COLUMN IF NOT EXISTS free_cash_flow_3y DOUBLE",
        "ALTER TABLE stock_fundamentals ADD COLUMN IF NOT EXISTS eps DOUBLE",
        "ALTER TABLE stock_fundamentals ADD COLUMN IF NOT EXISTS eps_previous DOUBLE",
        "ALTER TABLE stock_fundamentals ADD COLUMN IF NOT EXISTS piotroski_score INTEGER",
    ],
    10: [],  # crawl status/ref tables ship in baseline DDL
    11: [],  # index_close/index_constituent tables ship in baseline DDL
    12: [],  # research score/run tables ship in baseline DDL
    13: [],  # research delivery ledger ships in baseline DDL
    14: [],  # read-only broker snapshot tables ship in baseline DDL
    15: [
        "ALTER TABLE broker_snapshot_run ADD COLUMN IF NOT EXISTS "
        "mf_holding_count INTEGER DEFAULT 0"
    ],
    16: [],  # news tables ship in baseline DDL
    17: [],  # Vested snapshot tables ship in baseline DDL
}

_MANDATORY_COLS = ("benchmark", "frequency", "methodology_version", "calculated_at")


def connect(path: str | None = None) -> duckdb.DuckDBPyConnection:
    """Open a DuckDB connection; None means in-memory (used by tests)."""
    return duckdb.connect(path)


def init_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Apply DDL + migrations idempotently, recording each version once."""
    for stmt in _DDL:
        conn.execute(stmt)
    for version in sorted(_MIGRATIONS):
        for stmt in _MIGRATIONS[version]:
            conn.execute(stmt)
        conn.execute(
            "INSERT INTO schema_migrations VALUES (?, ?) ON CONFLICT DO NOTHING",
            [version, dt.now(UTC)],
        )


def upsert_scheme(conn: duckdb.DuckDBPyConnection, **row) -> None:
    """Insert-or-update one mf_scheme row keyed by scheme_code."""
    row.setdefault("is_active", True)
    row.setdefault("is_stale", False)
    cols = list(row)
    placeholders = ", ".join(["?"] * len(cols))
    updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "scheme_code")
    conn.execute(
        f"INSERT INTO mf_scheme ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT (scheme_code) DO UPDATE SET {updates}",
        list(row.values()),
    )


def upsert_navs(
    conn: duckdb.DuckDBPyConnection,
    scheme_code: int,
    pairs: Iterable[tuple[date, float]],
) -> None:
    """Insert-or-update NAV rows keyed by (scheme_code, nav_date)."""
    conn.executemany(
        "INSERT INTO mf_nav (scheme_code, nav_date, nav) VALUES (?, ?, ?) "
        "ON CONFLICT (scheme_code, nav_date) DO UPDATE SET nav = excluded.nav",
        [(scheme_code, d, float(v)) for d, v in pairs],
    )


def upsert_return_metric(conn: duckdb.DuckDBPyConnection, **row) -> None:
    _check_mandatory(row)
    cols = list(row)
    placeholders = ", ".join(["?"] * len(cols))
    key_cols = ("scheme_code", "lookback")
    updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c not in key_cols)
    conn.execute(
        f"INSERT INTO mf_return_metrics ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT (scheme_code, lookback) DO UPDATE SET {updates}",
        list(row.values()),
    )


def upsert_risk_metric(conn: duckdb.DuckDBPyConnection, **row) -> None:
    _check_mandatory(row)
    cols = list(row)
    placeholders = ", ".join(["?"] * len(cols))
    key_cols = ("scheme_code", "lookback", "benchmark", "frequency")
    updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c not in key_cols)
    conn.execute(
        f"INSERT INTO mf_risk_metrics ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT (scheme_code, lookback, benchmark, frequency) "
        f"DO UPDATE SET {updates}",
        list(row.values()),
    )


def _validate_table_columns(
    conn: duckdb.DuckDBPyConnection, table: str, columns: Iterable[str]
) -> None:
    allowed = {r[1] for r in conn.execute(f"PRAGMA table_info('{table}')").fetchall()}
    unknown = sorted(set(columns) - allowed)
    if unknown:
        raise ValueError(f"unknown {table} columns: {unknown}")


def upsert_stock_fundamental(conn: duckdb.DuckDBPyConnection, **row) -> None:
    """Upsert one source snapshot; partial metrics stay nullable for enrichment."""
    required = ("symbol", "as_of", "source", "methodology_version", "fetched_at")
    missing = [c for c in required if row.get(c) is None]
    if missing:
        raise ValueError(f"stock snapshot missing required fields: {missing}")
    cols = list(row)
    _validate_table_columns(conn, "stock_fundamentals", cols)
    placeholders = ", ".join(["?"] * len(cols))
    key_cols = ("symbol", "as_of", "source")
    updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c not in key_cols)
    compare_cols = [c for c in cols if c not in (*key_cols, "fetched_at")]
    changed = " OR ".join(
        f"stock_fundamentals.{c} IS DISTINCT FROM excluded.{c}" for c in compare_cols
    )
    conn.execute(
        f"INSERT INTO stock_fundamentals ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT (symbol, as_of, source) DO UPDATE SET {updates} WHERE {changed}",
        list(row.values()),
    )


def _upsert_crawl_evidence(
    conn: duckdb.DuckDBPyConnection, table: str, key_cols: tuple[str, ...], row: dict
) -> None:
    """No-churn upsert for deterministic reconciliation evidence."""
    cols = list(row)
    _validate_table_columns(conn, table, cols)
    placeholders = ", ".join(["?"] * len(cols))
    updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c not in key_cols)
    compare_cols = [c for c in cols if c not in (*key_cols, "checked_at")]
    changed = " OR ".join(f"{table}.{c} IS DISTINCT FROM excluded.{c}" for c in compare_cols)
    conn.execute(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT ({', '.join(key_cols)}) DO UPDATE SET {updates} WHERE {changed}",
        list(row.values()),
    )


def upsert_crawl_status(conn: duckdb.DuckDBPyConnection, **row) -> None:
    required = ("symbol", "policy_version", "complete", "checked_at")
    missing = [c for c in required if row.get(c) is None]
    if missing:
        raise ValueError(f"crawl status missing required fields: {missing}")
    _upsert_crawl_evidence(conn, "stock_crawl_status", ("symbol",), row)


def upsert_crawl_ref(conn: duckdb.DuckDBPyConnection, **row) -> None:
    required = ("symbol", "xbrl_url", "outcome", "policy_version", "checked_at")
    missing = [c for c in required if row.get(c) is None]
    if missing:
        raise ValueError(f"crawl ref missing required fields: {missing}")
    _upsert_crawl_evidence(conn, "stock_crawl_ref", ("symbol", "xbrl_url"), row)


def upsert_stock_filing(conn: duckdb.DuckDBPyConnection, **row) -> None:
    """Upsert retained NSE filing metadata keyed by immutable archive URL."""
    required = ("xbrl_url", "symbol", "source", "filing_type", "fetched_at")
    missing = [c for c in required if row.get(c) is None]
    if missing:
        raise ValueError(f"stock filing missing required fields: {missing}")
    cols = list(row)
    _validate_table_columns(conn, "stock_filing", cols)
    placeholders = ", ".join(["?"] * len(cols))
    updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "xbrl_url")
    compare_cols = [c for c in cols if c not in ("xbrl_url", "fetched_at")]
    changed = " OR ".join(f"stock_filing.{c} IS DISTINCT FROM excluded.{c}" for c in compare_cols)
    conn.execute(
        f"INSERT INTO stock_filing ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT (xbrl_url) DO UPDATE SET {updates} WHERE {changed}",
        list(row.values()),
    )


def upsert_universe_row(conn: duckdb.DuckDBPyConnection, **row) -> None:
    """Upsert one listed-equity master row keyed by symbol."""
    required = ("symbol", "source", "fetched_at")
    missing = [c for c in required if row.get(c) is None]
    if missing:
        raise ValueError(f"universe row missing required fields: {missing}")
    cols = list(row)
    _validate_table_columns(conn, "stock_universe", cols)
    placeholders = ", ".join(["?"] * len(cols))
    updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "symbol")
    compare_cols = [c for c in cols if c not in ("symbol", "fetched_at")]
    changed = " OR ".join(f"stock_universe.{c} IS DISTINCT FROM excluded.{c}" for c in compare_cols)
    conn.execute(
        f"INSERT INTO stock_universe ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT (symbol) DO UPDATE SET {updates} WHERE {changed}",
        list(row.values()),
    )


def upsert_crawl_skip(conn: duckdb.DuckDBPyConnection, **row) -> None:
    """Tombstone one symbol as permanently crawl-empty (e.g. no real XBRL refs).

    Skipped symbols leave the pending queue; clearing a tombstone is a manual
    DELETE FROM stock_crawl_skip. checked_at never churns on identical replay.
    """
    required = ("symbol", "reason", "checked_at")
    missing = [c for c in required if row.get(c) is None]
    if missing:
        raise ValueError(f"crawl skip missing required fields: {missing}")
    cols = list(row)
    _validate_table_columns(conn, "stock_crawl_skip", cols)
    placeholders = ", ".join(["?"] * len(cols))
    updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "symbol")
    compare_cols = [c for c in cols if c not in ("symbol", "checked_at")]
    changed = " OR ".join(
        f"stock_crawl_skip.{c} IS DISTINCT FROM excluded.{c}" for c in compare_cols
    )
    conn.execute(
        f"INSERT INTO stock_crawl_skip ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT (symbol) DO UPDATE SET {updates} WHERE {changed}",
        list(row.values()),
    )


_PRICE_COLUMNS = (
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
)


def upsert_prices(
    conn: duckdb.DuckDBPyConnection, rows: Iterable[dict], *, source: str, fetched_at: dt
) -> int:
    """Bulk-upsert daily bars keyed by (symbol, trade_date); returns row count."""
    prepared = []
    for row in rows:
        missing = [c for c in ("symbol", "trade_date") if row.get(c) is None]
        if missing:
            raise ValueError(f"price row missing required fields: {missing}")
        prepared.append([row.get(c) for c in _PRICE_COLUMNS[:8]] + [source, fetched_at])
    if not prepared:
        return 0
    updates = ", ".join(f"{c} = excluded.{c}" for c in _PRICE_COLUMNS[2:])
    changed_terms = ["stock_price.source IS DISTINCT FROM excluded.source"] + [
        f"{c} IS DISTINCT FROM excluded.{c}" for c in _PRICE_COLUMNS[2:8]
    ]
    conn.executemany(
        f"INSERT INTO stock_price ({', '.join(_PRICE_COLUMNS)}) "
        f"VALUES ({', '.join(['?'] * len(_PRICE_COLUMNS))}) "
        f"ON CONFLICT (symbol, trade_date) DO UPDATE SET {updates} "
        f"WHERE {' OR '.join(changed_terms)}",
        prepared,
    )
    return len(prepared)


def get_watermark(conn: duckdb.DuckDBPyConnection, kind: str) -> date | None:
    row = conn.execute("SELECT last_date FROM ingest_watermark WHERE kind = ?", [kind]).fetchone()
    return row[0] if row else None


def set_watermark(
    conn: duckdb.DuckDBPyConnection,
    kind: str,
    last_date: date,
    *,
    detail: str | None = None,
    updated_at: dt | None = None,
) -> None:
    conn.execute(
        "INSERT INTO ingest_watermark (kind, last_date, detail, updated_at) "
        "VALUES (?, ?, ?, ?) ON CONFLICT (kind) DO UPDATE SET "
        "last_date = excluded.last_date, detail = excluded.detail, "
        "updated_at = excluded.updated_at "
        "WHERE ingest_watermark.last_date IS DISTINCT FROM excluded.last_date "
        "OR ingest_watermark.detail IS DISTINCT FROM excluded.detail",
        [kind, last_date, detail, updated_at or dt.now(UTC)],
    )


def upsert_filing_contexts(conn: duckdb.DuckDBPyConnection, xbrl_url: str, contexts: dict) -> int:
    """Bulk-store parsed XBRL context periods/dimensions for one filing."""
    rows = []
    for ctx in contexts.values():
        dims = ",".join(f"{d}={v}" for d, v in ctx.dimensions) or None
        rows.append(
            [
                xbrl_url,
                ctx.context_id,
                _as_date(ctx.start_date),
                _as_date(ctx.end_date),
                _as_date(ctx.instant),
                dims,
            ]
        )
    if not rows:
        return 0
    cols = ("xbrl_url", "context_id", "start_date", "end_date", "instant", "dimensions")
    conn.executemany(
        f"INSERT INTO stock_filing_context ({', '.join(cols)}) "
        f"VALUES ({', '.join(['?'] * len(cols))}) ON CONFLICT DO UPDATE SET "
        "start_date = excluded.start_date, end_date = excluded.end_date, "
        "instant = excluded.instant, dimensions = excluded.dimensions",
        rows,
    )
    return len(rows)


def upsert_filing_facts(conn: duckdb.DuckDBPyConnection, xbrl_url: str, facts: dict) -> int:
    """Bulk-store extracted fact values with their context/unit identity."""
    rows = []
    for group in facts.values():
        for f in group:
            rows.append([xbrl_url, f.name, f.context_ref or "", f.value, f.unit_ref, f.decimals])
    if not rows:
        return 0
    cols = ("xbrl_url", "fact_name", "context_ref", "value", "unit_ref", "decimals")
    conn.executemany(
        f"INSERT INTO stock_filing_fact ({', '.join(cols)}) "
        f"VALUES ({', '.join(['?'] * len(cols))}) ON CONFLICT DO UPDATE SET "
        "value = excluded.value, unit_ref = excluded.unit_ref, decimals = excluded.decimals",
        rows,
    )
    return len(rows)


def _as_date(value: str | None):
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _check_mandatory(row: dict) -> None:
    missing = [c for c in _MANDATORY_COLS if row.get(c) is None]
    if missing:
        msg = f"metric rows require {sorted(_MANDATORY_COLS)}; missing {missing}"
        raise ValueError(msg)


def fingerprint(conn: duckdb.DuckDBPyConnection, table: str) -> tuple[int, str]:
    """Deterministic (row_count, sha256) over a table's full contents."""
    rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    ordered = sorted(rows, key=lambda r: json.dumps(r, default=str))
    blob = "\n".join(json.dumps(r, default=str) for r in ordered).encode()
    return len(rows), hashlib.sha256(blob).hexdigest()


def metric_violation_count(conn: duckdb.DuckDBPyConnection) -> int:
    """SPEC assertion: zero metric rows missing methodology fields.

    Columns are NOT NULL so violations normally fail at insert time; this
    query remains the documented post-load check.
    """
    cond = " OR ".join(f"{c} IS NULL" for c in _MANDATORY_COLS)
    total = 0
    for table in ("mf_return_metrics", "mf_risk_metrics"):
        (n,) = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {cond}").fetchone()
        total += n
    return total
