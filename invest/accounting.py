"""Phase 11 portfolio-accounting schema and immutable source-import ledger."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime as dt

import duckdb

SCHEMA_VERSION = 20

_DDL = [
    """
    CREATE TABLE IF NOT EXISTS portfolio_account (
        account_id TEXT PRIMARY KEY,
        provider TEXT NOT NULL CHECK (provider IN ('ZERODHA', 'VESTED')),
        account_scope TEXT NOT NULL,
        native_currency TEXT NOT NULL CHECK (length(native_currency) = 3),
        exact_status TEXT NOT NULL CHECK (exact_status IN ('UNPROVEN', 'COMPLETE')),
        created_at TIMESTAMP NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS portfolio_instrument (
        instrument_id TEXT PRIMARY KEY,
        market TEXT NOT NULL,
        symbol TEXT NOT NULL,
        instrument_type TEXT NOT NULL,
        native_currency TEXT NOT NULL CHECK (length(native_currency) = 3),
        source_identity_hash TEXT NOT NULL,
        UNIQUE (market, source_identity_hash)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS accounting_import_run (
        import_id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES portfolio_account(account_id),
        source_type TEXT NOT NULL,
        content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
        coverage_start DATE,
        coverage_end DATE,
        row_count BIGINT NOT NULL CHECK (row_count >= 0),
        source_fingerprint TEXT NOT NULL CHECK (length(source_fingerprint) = 64),
        imported_at TIMESTAMP NOT NULL,
        supersedes_import_id TEXT REFERENCES accounting_import_run(import_id),
        correction_reason TEXT,
        UNIQUE (account_id, source_type, content_sha256),
        CHECK ((supersedes_import_id IS NULL) = (correction_reason IS NULL)),
        CHECK (coverage_start IS NULL OR coverage_end IS NULL OR coverage_start <= coverage_end)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS portfolio_transaction (
        event_id TEXT PRIMARY KEY,
        import_id TEXT NOT NULL REFERENCES accounting_import_run(import_id),
        account_id TEXT NOT NULL REFERENCES portfolio_account(account_id),
        instrument_id TEXT NOT NULL REFERENCES portfolio_instrument(instrument_id),
        event_at TIMESTAMP NOT NULL,
        side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
        quantity DECIMAL(28, 10) NOT NULL CHECK (quantity > 0),
        unit_price DECIMAL(28, 10) NOT NULL CHECK (unit_price >= 0),
        gross_amount DECIMAL(28, 10) NOT NULL CHECK (gross_amount >= 0),
        currency TEXT NOT NULL CHECK (length(currency) = 3),
        source_event_hash TEXT NOT NULL,
        UNIQUE (import_id, source_event_hash)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS portfolio_cash_flow (
        event_id TEXT PRIMARY KEY,
        import_id TEXT NOT NULL REFERENCES accounting_import_run(import_id),
        account_id TEXT NOT NULL REFERENCES portfolio_account(account_id),
        event_at TIMESTAMP NOT NULL,
        direction TEXT NOT NULL CHECK (direction IN ('DEPOSIT', 'WITHDRAWAL')),
        amount DECIMAL(28, 10) NOT NULL CHECK (amount > 0),
        currency TEXT NOT NULL CHECK (length(currency) = 3),
        date_evidence TEXT NOT NULL CHECK (
            date_evidence IN (
                'SOURCE_PAYMENT_DATE', 'SOURCE_POSTING_DATE', 'SUBSTITUTED_EX_DATE'
            )
        ),
        source_event_hash TEXT NOT NULL,
        UNIQUE (import_id, source_event_hash)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS portfolio_income (
        event_id TEXT PRIMARY KEY,
        import_id TEXT NOT NULL REFERENCES accounting_import_run(import_id),
        account_id TEXT NOT NULL REFERENCES portfolio_account(account_id),
        instrument_id TEXT REFERENCES portfolio_instrument(instrument_id),
        event_at TIMESTAMP NOT NULL,
        income_type TEXT NOT NULL CHECK (income_type IN ('DIVIDEND', 'INTEREST', 'OTHER')),
        gross_amount DECIMAL(28, 10) NOT NULL CHECK (gross_amount >= 0),
        currency TEXT NOT NULL CHECK (length(currency) = 3),
        date_evidence TEXT NOT NULL CHECK (
            date_evidence IN (
                'SOURCE_PAYMENT_DATE', 'SOURCE_POSTING_DATE', 'SUBSTITUTED_EX_DATE'
            )
        ),
        source_event_hash TEXT NOT NULL,
        UNIQUE (import_id, source_event_hash)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS portfolio_fee (
        event_id TEXT PRIMARY KEY,
        import_id TEXT NOT NULL REFERENCES accounting_import_run(import_id),
        account_id TEXT NOT NULL REFERENCES portfolio_account(account_id),
        event_at TIMESTAMP NOT NULL,
        fee_type TEXT NOT NULL,
        amount DECIMAL(28, 10) NOT NULL CHECK (amount >= 0),
        currency TEXT NOT NULL CHECK (length(currency) = 3),
        source_event_hash TEXT NOT NULL,
        UNIQUE (import_id, source_event_hash)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS portfolio_tax (
        event_id TEXT PRIMARY KEY,
        import_id TEXT NOT NULL REFERENCES accounting_import_run(import_id),
        account_id TEXT NOT NULL REFERENCES portfolio_account(account_id),
        event_at TIMESTAMP NOT NULL,
        tax_type TEXT NOT NULL,
        amount DECIMAL(28, 10) NOT NULL CHECK (amount >= 0),
        currency TEXT NOT NULL CHECK (length(currency) = 3),
        source_event_hash TEXT NOT NULL,
        UNIQUE (import_id, source_event_hash)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS portfolio_corporate_action (
        event_id TEXT PRIMARY KEY,
        import_id TEXT NOT NULL REFERENCES accounting_import_run(import_id),
        account_id TEXT NOT NULL REFERENCES portfolio_account(account_id),
        instrument_id TEXT NOT NULL REFERENCES portfolio_instrument(instrument_id),
        effective_date DATE NOT NULL,
        action_type TEXT NOT NULL,
        quantity_delta DECIMAL(28, 10),
        cash_amount DECIMAL(28, 10),
        currency TEXT,
        evidence_status TEXT NOT NULL CHECK (evidence_status IN ('SOURCE', 'RECONSTRUCTED')),
        source_event_hash TEXT NOT NULL,
        UNIQUE (import_id, source_event_hash)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS portfolio_valuation (
        valuation_id TEXT PRIMARY KEY,
        import_id TEXT NOT NULL REFERENCES accounting_import_run(import_id),
        account_id TEXT NOT NULL REFERENCES portfolio_account(account_id),
        instrument_id TEXT REFERENCES portfolio_instrument(instrument_id),
        valued_at TIMESTAMP NOT NULL,
        value DECIMAL(28, 10) NOT NULL,
        cost_basis DECIMAL(28, 10),
        currency TEXT NOT NULL CHECK (length(currency) = 3),
        valuation_evidence TEXT NOT NULL CHECK (
            valuation_evidence IN ('SOURCE_SNAPSHOT', 'RECONSTRUCTED_MARKET_PRICE')
        ),
        source_event_hash TEXT NOT NULL,
        UNIQUE (import_id, source_event_hash)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS portfolio_fx_rate (
        rate_date DATE NOT NULL,
        base_currency TEXT NOT NULL,
        quote_currency TEXT NOT NULL,
        rate DECIMAL(28, 10) NOT NULL CHECK (rate > 0),
        source TEXT NOT NULL,
        source_authority TEXT NOT NULL,
        content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
        fetched_at TIMESTAMP NOT NULL,
        PRIMARY KEY (rate_date, base_currency, quote_currency, source)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS portfolio_tax_lot (
        lot_id TEXT PRIMARY KEY,
        import_id TEXT NOT NULL REFERENCES accounting_import_run(import_id),
        account_id TEXT NOT NULL REFERENCES portfolio_account(account_id),
        instrument_id TEXT NOT NULL REFERENCES portfolio_instrument(instrument_id),
        acquired_date DATE NOT NULL,
        disposed_date DATE,
        quantity DECIMAL(28, 10) NOT NULL CHECK (quantity > 0),
        cost_basis DECIMAL(28, 10) NOT NULL CHECK (cost_basis >= 0),
        proceeds DECIMAL(28, 10),
        realized_pnl DECIMAL(28, 10),
        lot_status TEXT NOT NULL CHECK (lot_status IN ('OPEN', 'REALIZED')),
        source_event_hash TEXT NOT NULL,
        UNIQUE (import_id, source_event_hash),
        CHECK (
            (lot_status = 'OPEN' AND disposed_date IS NULL AND proceeds IS NULL)
            OR (lot_status = 'REALIZED' AND disposed_date IS NOT NULL AND proceeds IS NOT NULL)
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS broker_reported_summary (
        summary_id TEXT PRIMARY KEY,
        import_id TEXT NOT NULL REFERENCES accounting_import_run(import_id),
        account_id TEXT NOT NULL REFERENCES portfolio_account(account_id),
        segment TEXT NOT NULL,
        period_start DATE NOT NULL,
        period_end DATE NOT NULL,
        summary_type TEXT NOT NULL,
        amount DECIMAL(28, 10) NOT NULL,
        currency TEXT NOT NULL CHECK (length(currency) = 3),
        source_event_hash TEXT NOT NULL,
        UNIQUE (import_id, source_event_hash),
        CHECK (period_start <= period_end)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS accounting_completeness (
        evidence_id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES portfolio_account(account_id),
        assessed_at TIMESTAMP NOT NULL,
        coverage_start DATE,
        coverage_end DATE,
        transactions_status TEXT NOT NULL CHECK (
            transactions_status IN ('COMPLETE', 'ESTIMATED', 'MISSING')
        ),
        cash_flows_status TEXT NOT NULL CHECK (
            cash_flows_status IN ('COMPLETE', 'ESTIMATED', 'MISSING')
        ),
        income_status TEXT NOT NULL CHECK (
            income_status IN ('COMPLETE', 'ESTIMATED', 'MISSING')
        ),
        valuations_status TEXT NOT NULL CHECK (
            valuations_status IN ('COMPLETE', 'ESTIMATED', 'MISSING')
        ),
        corporate_actions_status TEXT NOT NULL CHECK (
            corporate_actions_status IN ('COMPLETE', 'ESTIMATED', 'MISSING')
        ),
        fx_status TEXT NOT NULL CHECK (
            fx_status IN ('COMPLETE', 'ESTIMATED', 'MISSING', 'NOT_APPLICABLE')
        ),
        assumptions_json TEXT NOT NULL,
        exclusions_json TEXT NOT NULL,
        residuals_json TEXT NOT NULL,
        methodology_version TEXT NOT NULL,
        CHECK (coverage_start IS NULL OR coverage_end IS NULL OR coverage_start <= coverage_end)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS portfolio_performance_result (
        result_id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES portfolio_account(account_id),
        metric TEXT NOT NULL CHECK (
            metric IN (
                'XIRR', 'TWR', 'REALIZED_RETURN', 'UNREALIZED_RETURN',
                'INCOME', 'FEES', 'ALLOCATION'
            )
        ),
        status TEXT NOT NULL CHECK (status IN ('EXACT', 'ESTIMATED', 'UNAVAILABLE')),
        value DECIMAL(28, 12),
        currency TEXT,
        coverage_start DATE,
        coverage_end DATE,
        methodology_version TEXT NOT NULL,
        assumptions_json TEXT NOT NULL,
        exclusions_json TEXT NOT NULL,
        residuals_json TEXT NOT NULL,
        input_fingerprint TEXT NOT NULL CHECK (length(input_fingerprint) = 64),
        calculated_at TIMESTAMP NOT NULL,
        CHECK ((status = 'UNAVAILABLE') = (value IS NULL)),
        CHECK (coverage_start IS NULL OR coverage_end IS NULL OR coverage_start <= coverage_end)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS portfolio_allocation_result (
        result_id TEXT NOT NULL REFERENCES portfolio_performance_result(result_id),
        dimension TEXT NOT NULL,
        bucket TEXT NOT NULL,
        native_value DECIMAL(28, 10) NOT NULL,
        base_value DECIMAL(28, 10),
        weight DECIMAL(28, 12) NOT NULL CHECK (weight >= 0 AND weight <= 1),
        source_as_of DATE NOT NULL,
        PRIMARY KEY (result_id, dimension, bucket)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS managed_product (
        product_id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES portfolio_account(account_id),
        product_name TEXT NOT NULL,
        source_identity_hash TEXT NOT NULL,
        UNIQUE (account_id, source_identity_hash)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS managed_product_membership (
        membership_id TEXT PRIMARY KEY,
        import_id TEXT NOT NULL REFERENCES accounting_import_run(import_id),
        product_id TEXT NOT NULL REFERENCES managed_product(product_id),
        instrument_id TEXT NOT NULL REFERENCES portfolio_instrument(instrument_id),
        valid_from DATE NOT NULL,
        valid_to DATE,
        evidence_status TEXT NOT NULL CHECK (evidence_status = 'SOURCE'),
        CHECK (valid_to IS NULL OR valid_from <= valid_to)
    )
    """,
]


def store_completeness(
    conn: duckdb.DuckDBPyConnection,
    *,
    account_id: str,
    coverage_start,
    coverage_end,
    statuses: dict[str, str],
    assumptions: list[str],
    exclusions: list[str],
    residuals: list[str],
    methodology_version: str,
    now=None,
) -> str:
    """Persist one immutable, explicit exact/estimated completeness assessment."""
    import hashlib
    import json

    required = {
        "transactions",
        "cash_flows",
        "income",
        "valuations",
        "corporate_actions",
        "fx",
    }
    if set(statuses) != required or not methodology_version:
        raise ValueError("completeness assessment fields are incomplete")
    now = now or dt.now(UTC)
    payload = {
        "account_id": account_id,
        "coverage_start": str(coverage_start) if coverage_start else None,
        "coverage_end": str(coverage_end) if coverage_end else None,
        "statuses": statuses,
        "assumptions": assumptions,
        "exclusions": exclusions,
        "residuals": residuals,
        "methodology_version": methodology_version,
    }
    evidence_id = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:24]
    conn.execute(
        "INSERT INTO accounting_completeness VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT DO NOTHING",
        [
            evidence_id,
            account_id,
            now,
            coverage_start,
            coverage_end,
            statuses["transactions"],
            statuses["cash_flows"],
            statuses["income"],
            statuses["valuations"],
            statuses["corporate_actions"],
            statuses["fx"],
            json.dumps(assumptions, separators=(",", ":")),
            json.dumps(exclusions, separators=(",", ":")),
            json.dumps(residuals, separators=(",", ":")),
            methodology_version,
        ],
    )
    return evidence_id


def install_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Install schema v20 atomically on a disposable or explicitly approved DB."""
    conn.execute("BEGIN")
    try:
        for statement in _DDL:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_migrations VALUES (?, ?) ON CONFLICT DO NOTHING",
            [SCHEMA_VERSION, dt.now(UTC)],
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
