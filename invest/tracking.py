"""Phase 9 replay-safe research event persistence.

Schema v18 is deliberately excluded from db.init_schema(). Production migration
is a separate T9.7 approval gate; tests and disposable databases call
install_schema() explicitly.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, date, timedelta
from datetime import datetime as dt
from pathlib import Path
from typing import Any

import duckdb

METHODOLOGY = "tracking-2026.1"
SCHEMA_VERSION = 18
PRODUCTION_DB = Path("data/invest.duckdb")


class TrackingConflict(RuntimeError):
    """An immutable natural key was replayed with different semantic content."""


_DDL = (
    """
    CREATE TABLE IF NOT EXISTS tracking_methodology (
        methodology_version TEXT PRIMARY KEY,
        semantic_config_fingerprint TEXT NOT NULL,
        canonical_config_json TEXT NOT NULL,
        registered_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS index_constituent_snapshot (
        snapshot_id TEXT PRIMARY KEY,
        index_name TEXT NOT NULL,
        source_as_of DATE NOT NULL,
        fetched_at TIMESTAMPTZ NOT NULL,
        source TEXT NOT NULL,
        source_content_fingerprint TEXT NOT NULL,
        member_count INTEGER NOT NULL,
        validation_status TEXT NOT NULL CHECK (validation_status='VALIDATED'),
        UNIQUE(index_name,source_as_of,source_content_fingerprint)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS index_constituent_snapshot_member (
        snapshot_id TEXT NOT NULL REFERENCES index_constituent_snapshot(snapshot_id),
        symbol TEXT NOT NULL,
        company_name TEXT NOT NULL,
        industry TEXT,
        isin TEXT NOT NULL,
        series TEXT NOT NULL CHECK (series='EQ'),
        PRIMARY KEY(snapshot_id,symbol)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS watchlist_run (
        run_id TEXT PRIMARY KEY,
        index_name TEXT NOT NULL,
        source_as_of DATE NOT NULL,
        canonical_cutoff DATE NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL,
        methodology_version TEXT NOT NULL REFERENCES tracking_methodology(methodology_version),
        config_fingerprint TEXT NOT NULL,
        input_fingerprint TEXT NOT NULL,
        constituent_snapshot_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN (
            'ACCEPTED','REPLAY','REJECTED_OUT_OF_ORDER','CONFLICT'
        )),
        universe_count INTEGER NOT NULL,
        selected_count INTEGER NOT NULL,
        detail_json TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS watchlist_symbol_result (
        result_id TEXT NOT NULL UNIQUE,
        run_id TEXT NOT NULL REFERENCES watchlist_run(run_id),
        symbol TEXT NOT NULL,
        source_as_of DATE NOT NULL,
        methodology_version TEXT NOT NULL,
        result TEXT NOT NULL CHECK (result IN (
            'ADMITTED','CONTINUED','DROPPED_BELOW_TOP_N','PRICE_ABOVE_CAP','NO_CLOSE',
            'STALE_CLOSE','INSUFFICIENT_BETA','CONSTITUENT_REMOVED'
        )),
        rank INTEGER,
        close DOUBLE,
        beta DOUBLE,
        observations INTEGER,
        evidence_as_of DATE,
        reason_json TEXT NOT NULL,
        PRIMARY KEY(run_id,symbol)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS signal_run (
        run_id TEXT PRIMARY KEY,
        source_as_of DATE NOT NULL,
        canonical_cutoff DATE NOT NULL,
        started_at TIMESTAMPTZ NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL,
        methodology_version TEXT NOT NULL REFERENCES tracking_methodology(methodology_version),
        semantic_config_fingerprint TEXT NOT NULL,
        input_fingerprint TEXT NOT NULL,
        watchlist_run_id TEXT NOT NULL REFERENCES watchlist_run(run_id),
        status TEXT NOT NULL CHECK (status IN (
            'ACCEPTED','REPLAY','REJECTED_OUT_OF_ORDER','CONFLICT'
        )),
        scanned_count INTEGER NOT NULL,
        signal_count INTEGER NOT NULL,
        detail_json TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS signal_event (
        event_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES signal_run(run_id),
        symbol TEXT NOT NULL,
        signal_date DATE NOT NULL,
        source_as_of DATE NOT NULL,
        action TEXT NOT NULL CHECK (action IN ('ENTER','EXIT')),
        close DOUBLE NOT NULL,
        ema10 DOUBLE,
        ema21 DOUBLE,
        quantity INTEGER,
        sizing_stop DOUBLE,
        capital_to_deploy DOUBLE,
        maximum_loss_at_stop DOUBLE,
        sizing_gap_reason TEXT,
        methodology_version TEXT NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL,
        UNIQUE(methodology_version,symbol,signal_date,action)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS screen_evaluation_run (
        run_id TEXT PRIMARY KEY,
        screen_id TEXT NOT NULL,
        source_as_of DATE NOT NULL,
        canonical_cutoff DATE NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL,
        methodology_version TEXT NOT NULL REFERENCES tracking_methodology(methodology_version),
        config_fingerprint TEXT NOT NULL,
        input_fingerprint TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN (
            'ACCEPTED','REPLAY','REJECTED_OUT_OF_ORDER','CONFLICT'
        )),
        evaluated_count INTEGER NOT NULL,
        accepted_slot TEXT,
        UNIQUE(screen_id,methodology_version,source_as_of,accepted_slot),
        CHECK (
            (status='ACCEPTED' AND accepted_slot='ACCEPTED')
            OR (status<>'ACCEPTED' AND accepted_slot IS NULL)
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS screen_symbol_result (
        run_id TEXT NOT NULL REFERENCES screen_evaluation_run(run_id),
        screen_id TEXT NOT NULL,
        symbol TEXT NOT NULL,
        source_as_of DATE NOT NULL,
        methodology_version TEXT NOT NULL,
        outcome TEXT NOT NULL CHECK (outcome IN (
            'PASS','PREDICATE_FAIL','MISSING_DATA','STALE_DATA'
        )),
        failed_predicates_json TEXT NOT NULL,
        missing_fields_json TEXT NOT NULL,
        stale_fields_json TEXT NOT NULL,
        evidence_as_of DATE,
        source TEXT NOT NULL,
        metrics_json TEXT NOT NULL,
        PRIMARY KEY(run_id,symbol)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS screen_membership_event (
        event_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES screen_evaluation_run(run_id),
        screen_id TEXT NOT NULL,
        symbol TEXT NOT NULL,
        source_as_of DATE NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL,
        methodology_version TEXT NOT NULL,
        event_type TEXT NOT NULL CHECK (event_type IN (
            'ENTERED','CONTINUED','EXITED_PREDICATE','EXITED_MISSING_DATA',
            'EXITED_STALE_DATA','METHODOLOGY_RESET'
        )),
        previous_run_id TEXT,
        reason_json TEXT NOT NULL,
        UNIQUE(screen_id,symbol,methodology_version,source_as_of,event_type)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS research_position_identity (
        position_id TEXT PRIMARY KEY,
        origin_signal_event_id TEXT NOT NULL UNIQUE REFERENCES signal_event(event_id),
        methodology_version TEXT NOT NULL REFERENCES tracking_methodology(methodology_version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS market_price_evidence (
        evidence_id TEXT PRIMARY KEY,
        symbol TEXT NOT NULL,
        trade_date DATE NOT NULL,
        close DOUBLE NOT NULL CHECK (close>0),
        source TEXT NOT NULL,
        fetched_at TIMESTAMPTZ NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL,
        UNIQUE(symbol,trade_date,close,source,fetched_at)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS position_observation_event (
        event_id TEXT PRIMARY KEY,
        position_id TEXT NOT NULL REFERENCES research_position_identity(position_id),
        observation_type TEXT NOT NULL CHECK (observation_type IN (
            'EMA_EXIT','BELOW_ENTRY_SIZING_STOP'
        )),
        signal_event_id TEXT REFERENCES signal_event(event_id),
        price_evidence_id TEXT REFERENCES market_price_evidence(evidence_id),
        source_at TIMESTAMPTZ NOT NULL,
        observed_close DOUBLE NOT NULL,
        observed_ema10 DOUBLE,
        observed_ema21 DOUBLE,
        methodology_version TEXT NOT NULL REFERENCES tracking_methodology(methodology_version),
        recorded_at TIMESTAMPTZ NOT NULL,
        content_fingerprint TEXT NOT NULL,
        CHECK (observed_close>0),
        CHECK (
            (observation_type='EMA_EXIT' AND signal_event_id IS NOT NULL
             AND price_evidence_id IS NULL)
            OR
            (observation_type='BELOW_ENTRY_SIZING_STOP' AND signal_event_id IS NULL
             AND price_evidence_id IS NOT NULL)
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS broker_reconciliation_event (
        event_id TEXT PRIMARY KEY,
        broker_run_id TEXT NOT NULL REFERENCES broker_snapshot_run(run_id),
        position_id TEXT REFERENCES research_position_identity(position_id),
        market TEXT NOT NULL CHECK (market='IN'),
        symbol TEXT NOT NULL,
        reconciliation_type TEXT NOT NULL CHECK (reconciliation_type IN (
            'CONFIRMED_MISSING','UNTRACKED_PRESENT'
        )),
        broker_snapshot_at TIMESTAMPTZ NOT NULL,
        source_at TIMESTAMPTZ NOT NULL,
        methodology_version TEXT NOT NULL REFERENCES tracking_methodology(methodology_version),
        mapping_policy_version TEXT NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL,
        content_fingerprint TEXT NOT NULL,
        CHECK (
            (reconciliation_type='CONFIRMED_MISSING' AND position_id IS NOT NULL)
            OR
            (reconciliation_type='UNTRACKED_PRESENT' AND position_id IS NULL)
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS research_alert_delivery (
        fingerprint TEXT PRIMARY KEY,
        alert_type TEXT NOT NULL,
        subject_type TEXT NOT NULL,
        subject_id TEXT NOT NULL,
        causal_event_id TEXT NOT NULL,
        methodology_version TEXT NOT NULL REFERENCES tracking_methodology(methodology_version),
        source_at TIMESTAMPTZ NOT NULL,
        destination TEXT NOT NULL,
        message TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN (
            'PENDING','SENDING','SENT','FAILED_BEFORE_SEND','UNKNOWN_AFTER_SEND'
        )),
        attempts INTEGER NOT NULL CHECK (attempts>=0),
        claim_generation INTEGER NOT NULL CHECK (claim_generation>=0),
        claim_token TEXT,
        claimed_at TIMESTAMPTZ,
        claim_expires_at TIMESTAMPTZ,
        last_attempt_at TIMESTAMPTZ,
        sent_at TIMESTAMPTZ,
        last_error TEXT,
        CHECK (
            (status='SENDING' AND claim_token IS NOT NULL AND claimed_at IS NOT NULL
             AND claim_expires_at IS NOT NULL)
            OR
            (status<>'SENDING' AND claim_token IS NULL AND claimed_at IS NULL
             AND claim_expires_at IS NULL)
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS research_position_identity (
        position_id TEXT PRIMARY KEY,
        origin_signal_event_id TEXT NOT NULL UNIQUE REFERENCES signal_event(event_id),
        methodology_version TEXT NOT NULL REFERENCES tracking_methodology(methodology_version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS position_state_event (
        event_id TEXT PRIMARY KEY,
        position_id TEXT NOT NULL REFERENCES research_position_identity(position_id),
        from_state TEXT CHECK (from_state IS NULL OR from_state IN (
            'SIGNALLED','WATCHING','OPEN_CONFIRMED','CLOSED_CONFIRMED','IGNORED','EXPIRED'
        )),
        to_state TEXT NOT NULL CHECK (to_state IN (
            'SIGNALLED','WATCHING','OPEN_CONFIRMED','CLOSED_CONFIRMED','IGNORED','EXPIRED'
        )),
        source_at TIMESTAMPTZ NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL,
        methodology_version TEXT NOT NULL REFERENCES tracking_methodology(methodology_version),
        actor TEXT NOT NULL CHECK (actor IN ('SYSTEM_SIGNAL','OPERATOR')),
        operator_note TEXT,
        command_fingerprint TEXT NOT NULL UNIQUE,
        evidence_type TEXT NOT NULL CHECK (evidence_type IN ('SIGNAL_EVENT','OPERATOR_COMMAND')),
        evidence_id TEXT NOT NULL,
        signal_evidence_id TEXT REFERENCES signal_event(event_id),
        UNIQUE(position_id,event_id),
        CHECK (
            (actor='SYSTEM_SIGNAL' AND from_state IS NULL AND to_state='SIGNALLED'
             AND operator_note IS NULL AND evidence_type='SIGNAL_EVENT'
             AND signal_evidence_id IS NOT NULL AND evidence_id=signal_evidence_id)
            OR
            (actor='OPERATOR' AND from_state IS NOT NULL AND to_state<>'SIGNALLED'
             AND evidence_type='OPERATOR_COMMAND' AND signal_evidence_id IS NULL)
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS position_state_event_link (
        position_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        PRIMARY KEY(position_id,event_id),
        FOREIGN KEY(position_id,event_id) REFERENCES position_state_event(position_id,event_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS research_position (
        position_id TEXT PRIMARY KEY REFERENCES research_position_identity(position_id),
        market TEXT NOT NULL CHECK (market='IN'),
        symbol TEXT NOT NULL,
        origin_signal_event_id TEXT NOT NULL REFERENCES signal_event(event_id),
        current_state TEXT NOT NULL CHECK (current_state IN (
            'SIGNALLED','WATCHING','OPEN_CONFIRMED','CLOSED_CONFIRMED','IGNORED','EXPIRED'
        )),
        state_source_at TIMESTAMPTZ NOT NULL,
        state_event_id TEXT NOT NULL,
        methodology_version TEXT NOT NULL,
        entry_sizing_stop DOUBLE,
        active_slot TEXT,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        UNIQUE(market,symbol,methodology_version,active_slot),
        FOREIGN KEY(position_id,state_event_id)
            REFERENCES position_state_event_link(position_id,event_id),
        CHECK (
            (current_state IN ('CLOSED_CONFIRMED','IGNORED','EXPIRED') AND active_slot IS NULL)
            OR
            (current_state NOT IN ('CLOSED_CONFIRMED','IGNORED','EXPIRED')
             AND active_slot IS NOT NULL AND active_slot='ACTIVE')
        )
    )
    """,
)


def canonical_json(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True, separators=(",", ":"))


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _event_id(*parts: object) -> str:
    return fingerprint([str(part) for part in parts])


def install_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Install v18 on disposable databases; production remains blocked until T9.7."""
    database_path = conn.execute("PRAGMA database_list").fetchone()[2]
    if database_path and Path(database_path).resolve() == PRODUCTION_DB.resolve():
        raise PermissionError("production v18 migration requires the T9.7 approved command")
    conn.execute("BEGIN TRANSACTION")
    try:
        for statement in _DDL:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_migrations VALUES (?,?) ON CONFLICT DO NOTHING",
            [SCHEMA_VERSION, dt.now(UTC)],
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def register_methodology(
    conn: duckdb.DuckDBPyConnection,
    methodology_version: str,
    semantic_config: dict,
    *,
    registered_at: dt,
) -> str:
    if not methodology_version or registered_at.tzinfo is None:
        raise ValueError("methodology and timezone-aware registered_at are required")
    config_json = canonical_json(semantic_config)
    config_fp = fingerprint(semantic_config)
    existing = conn.execute(
        "SELECT semantic_config_fingerprint,canonical_config_json "
        "FROM tracking_methodology WHERE methodology_version=?",
        [methodology_version],
    ).fetchone()
    if existing:
        if existing != (config_fp, config_json):
            raise TrackingConflict("methodology semantic config changed without a new version")
        return "replay"
    conn.execute(
        "INSERT INTO tracking_methodology VALUES (?,?,?,?)",
        [methodology_version, config_fp, config_json, registered_at],
    )
    return "registered"


def _registered_config(
    conn: duckdb.DuckDBPyConnection, methodology_version: str, semantic_config: dict
) -> str:
    config_fp = fingerprint(semantic_config)
    row = conn.execute(
        "SELECT semantic_config_fingerprint FROM tracking_methodology WHERE methodology_version=?",
        [methodology_version],
    ).fetchone()
    if row is None:
        raise ValueError("tracking methodology is not registered")
    if row[0] != config_fp:
        raise TrackingConflict("semantic config does not match registered methodology")
    return config_fp


def persist_constituent_snapshot(
    conn: duckdb.DuckDBPyConnection,
    *,
    index_name: str,
    source_as_of: date,
    fetched_at: dt,
    source: str,
    methodology_version: str,
    semantic_config: dict,
    members: list[dict],
) -> str:
    """Persist one validated immutable official constituent snapshot."""
    _require_utc(fetched_at, "fetched_at")
    _registered_config(conn, methodology_version, semantic_config)
    minimum_count = int(semantic_config.get("constituent_min_count", 0))
    if (
        not index_name.strip()
        or semantic_config.get("index") != index_name
        or source != semantic_config.get("constituent_source")
        or minimum_count < 1
    ):
        raise ValueError("exact index, approved source, and positive minimum count are required")
    normalized = []
    for member in members:
        symbol = str(member.get("symbol", "")).strip().upper()
        series = str(member.get("series", "")).strip().upper()
        company_name = str(member.get("company_name", "")).strip()
        isin = str(member.get("isin", "")).strip().upper()
        if not symbol or series != "EQ" or not company_name or not isin:
            raise ValueError("constituent member contract is invalid")
        normalized.append(
            {
                "symbol": symbol,
                "company_name": company_name,
                "industry": str(member.get("industry") or "").strip() or None,
                "isin": isin,
                "series": series,
            }
        )
    normalized.sort(key=lambda item: item["symbol"])
    symbols = [item["symbol"] for item in normalized]
    if len(normalized) < minimum_count or len(symbols) != len(set(symbols)):
        raise ValueError("constituent snapshot is partial or contains duplicate symbols")
    content_fp = fingerprint(normalized)
    snapshot_id = fingerprint([index_name, source_as_of, content_fp])
    existing = conn.execute(
        "SELECT snapshot_id FROM index_constituent_snapshot WHERE index_name=? "
        "AND source_as_of=? AND source_content_fingerprint=?",
        [index_name, source_as_of, content_fp],
    ).fetchone()
    if existing:
        return existing[0]
    conn.execute("BEGIN TRANSACTION")
    try:
        conn.execute(
            "INSERT INTO index_constituent_snapshot VALUES (?,?,?,?,?,?,?,?)",
            [
                snapshot_id,
                index_name,
                source_as_of,
                fetched_at,
                source,
                content_fp,
                len(normalized),
                "VALIDATED",
            ],
        )
        conn.executemany(
            "INSERT INTO index_constituent_snapshot_member VALUES (?,?,?,?,?,?)",
            [
                [
                    snapshot_id,
                    item["symbol"],
                    item["company_name"],
                    item["industry"],
                    item["isin"],
                    item["series"],
                ]
                for item in normalized
            ],
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return snapshot_id


def watchlist_input_fingerprint(candidates: list[dict]) -> str:
    """Fingerprint the complete current-snapshot candidate evidence."""
    normalized = [
        {
            "symbol": str(item.get("symbol", "")).strip().upper(),
            "rank": item.get("rank"),
            "close": item.get("close"),
            "beta": item.get("beta"),
            "observations": item.get("observations"),
            "close_as_of": item.get("close_as_of"),
            "rank_as_of": item.get("rank_as_of"),
            "beta_as_of": item.get("beta_as_of"),
        }
        for item in candidates
    ]
    normalized.sort(key=lambda item: item["symbol"])
    return fingerprint(normalized)


def _watchlist_evidence_cutoff(candidates: list[dict], stale_days: int) -> date | None:
    close_dates = [item.get("close_as_of") for item in candidates if item.get("close") is not None]
    if not close_dates or any(not isinstance(day, date) for day in close_dates):
        return None
    latest = max(close_dates)
    usable = [day for day in close_dates if day >= latest - timedelta(days=stale_days)]
    return min(usable) if usable else None


def _watchlist_selected_count(candidates: list[dict], cutoff: date, config: dict) -> int:
    stale_days = int(config["max_price_age_days"])
    if _watchlist_evidence_cutoff(candidates, stale_days) != cutoff:
        raise ValueError("watchlist cutoff does not equal the bounded candidate close cutoff")
    return sum(
        1
        for item in candidates
        if isinstance(item.get("rank"), int)
        and item["rank"] <= int(config["top_n"])
        and item.get("close") is not None
        and item["close"] < float(config["price_cap"])
        and item.get("beta") is not None
        and (item.get("observations") or 0) >= int(config["beta_min_observations"])
        and item.get("close_as_of") is not None
        and item["close_as_of"] >= cutoff - timedelta(days=stale_days)
        and item.get("rank_as_of") is not None
        and item["rank_as_of"] <= cutoff
        and item.get("beta_as_of") is not None
        and item["beta_as_of"] <= cutoff
    )


def record_watchlist_run_header(
    conn: duckdb.DuckDBPyConnection,
    *,
    index_name: str,
    source_as_of: date,
    canonical_cutoff: date,
    methodology_version: str,
    semantic_config: dict,
    input_fingerprint: str,
    constituent_snapshot_id: str,
    universe_count: int,
    selected_count: int,
    recorded_at: dt,
) -> str:
    """Persist the immutable run header needed to bind signal facts.

    Complete watchlist result persistence is T9.5; this header already enforces
    the cutoff and semantic identity required by T9.2.
    """
    if source_as_of != canonical_cutoff:
        raise ValueError("watchlist source_as_of must equal canonical cutoff")
    if recorded_at.tzinfo is None:
        raise ValueError("recorded_at must be timezone-aware")
    config_fp = _registered_config(conn, methodology_version, semantic_config)
    if semantic_config.get("index") != index_name:
        raise TrackingConflict("watchlist index does not match semantic config")
    snapshot = conn.execute(
        "SELECT index_name,source_as_of,validation_status,source,member_count "
        "FROM index_constituent_snapshot WHERE snapshot_id=?",
        [constituent_snapshot_id],
    ).fetchone()
    if snapshot != (
        index_name,
        source_as_of,
        "VALIDATED",
        semantic_config.get("constituent_source"),
        universe_count,
    ) or universe_count < int(semantic_config.get("constituent_min_count", 0)):
        raise ValueError("watchlist requires a policy-matching validated snapshot")
    payload = {
        "index_name": index_name,
        "source_as_of": source_as_of,
        "canonical_cutoff": canonical_cutoff,
        "methodology_version": methodology_version,
        "config_fingerprint": config_fp,
        "input_fingerprint": input_fingerprint,
        "constituent_snapshot_id": constituent_snapshot_id,
        "universe_count": universe_count,
        "selected_count": selected_count,
    }
    run_id = fingerprint(payload)
    prior_attempt = conn.execute(
        "SELECT status FROM watchlist_run WHERE run_id=?", [run_id]
    ).fetchone()
    if prior_attempt:
        return run_id
    existing = conn.execute(
        "SELECT run_id,input_fingerprint FROM watchlist_run "
        "WHERE index_name=? AND methodology_version=? AND source_as_of=? "
        "AND status='ACCEPTED'",
        [index_name, methodology_version, source_as_of],
    ).fetchone()
    if existing and existing == (run_id, input_fingerprint):
        return existing[0]
    latest = conn.execute(
        "SELECT max(source_as_of) FROM watchlist_run WHERE index_name=? "
        "AND methodology_version=? AND status='ACCEPTED'",
        [index_name, methodology_version],
    ).fetchone()[0]
    status = (
        "CONFLICT"
        if existing
        else (
            "REJECTED_OUT_OF_ORDER" if latest is not None and source_as_of < latest else "ACCEPTED"
        )
    )
    conn.execute(
        "INSERT INTO watchlist_run VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            run_id,
            index_name,
            source_as_of,
            canonical_cutoff,
            recorded_at,
            methodology_version,
            config_fp,
            input_fingerprint,
            constituent_snapshot_id,
            status,
            universe_count,
            selected_count,
            canonical_json({}),
        ],
    )
    return run_id


def persist_watchlist_results(
    conn: duckdb.DuckDBPyConnection,
    *,
    run_id: str,
    candidates: list[dict],
    _manage_transaction: bool = True,
) -> dict[str, object]:
    """Persist one complete reason-coded watchlist result set atomically."""
    header = conn.execute(
        "SELECT w.index_name,w.source_as_of,w.canonical_cutoff,w.methodology_version,"
        "w.input_fingerprint,w.constituent_snapshot_id,w.status,m.canonical_config_json,"
        "w.universe_count,w.selected_count "
        "FROM watchlist_run w JOIN tracking_methodology m "
        "ON m.methodology_version=w.methodology_version WHERE w.run_id=?",
        [run_id],
    ).fetchone()
    if header is None or header[6] != "ACCEPTED":
        raise ValueError("accepted watchlist run is required")
    (
        index_name,
        source_as_of,
        cutoff,
        methodology,
        input_fp,
        snapshot_id,
        _,
        config_json,
        universe_count,
        selected_count,
    ) = header
    snapshot = conn.execute(
        "SELECT index_name,validation_status FROM index_constituent_snapshot WHERE snapshot_id=?",
        [snapshot_id],
    ).fetchone()
    if snapshot != (index_name, "VALIDATED"):
        raise ValueError("validated immutable constituent snapshot is required")
    if watchlist_input_fingerprint(candidates) != input_fp:
        raise TrackingConflict("watchlist candidates do not match run input fingerprint")
    members = {
        row[0]
        for row in conn.execute(
            "SELECT symbol FROM index_constituent_snapshot_member WHERE snapshot_id=?",
            [snapshot_id],
        ).fetchall()
    }
    rows_by_symbol = {}
    for item in candidates:
        symbol = str(item.get("symbol", "")).strip().upper()
        if not symbol or symbol in rows_by_symbol:
            raise ValueError("watchlist candidates contain a missing or duplicate symbol")
        evidence_dates = {
            "close_as_of": item.get("close_as_of"),
            "rank_as_of": item.get("rank_as_of"),
            "beta_as_of": item.get("beta_as_of"),
        }
        for field, evidence_day in evidence_dates.items():
            if evidence_day is not None and (
                not isinstance(evidence_day, date) or evidence_day > cutoff
            ):
                raise ValueError(f"watchlist {field} must be no later than cutoff")
        rows_by_symbol[symbol] = {
            "rank": item.get("rank"),
            "close": item.get("close"),
            "beta": item.get("beta"),
            "observations": item.get("observations"),
            **evidence_dates,
        }
    if set(rows_by_symbol) != members or universe_count != len(members):
        raise ValueError("watchlist candidates must cover the complete validated snapshot")
    config = json.loads(config_json)
    top_n = int(config["top_n"])
    price_cap = float(config["price_cap"])
    min_observations = int(config["beta_min_observations"])
    stale_days = int(config["max_price_age_days"])
    if _watchlist_evidence_cutoff(candidates, stale_days) != cutoff:
        raise ValueError("watchlist cutoff does not equal the bounded candidate close cutoff")
    previous_run = conn.execute(
        "SELECT w.run_id,w.universe_count,(SELECT count(*) FROM watchlist_symbol_result r "
        "WHERE r.run_id=w.run_id AND r.symbol IN (SELECT symbol FROM "
        "index_constituent_snapshot_member WHERE snapshot_id=w.constituent_snapshot_id)) "
        "FROM watchlist_run w WHERE w.index_name=? AND w.methodology_version=? "
        "AND w.status='ACCEPTED' AND w.source_as_of<? ORDER BY w.source_as_of DESC LIMIT 1",
        [index_name, methodology, source_as_of],
    ).fetchone()
    if previous_run and previous_run[1] != previous_run[2]:
        raise TrackingConflict("latest accepted watchlist run has incomplete results")
    previous_selected: set[str] = set()
    previous_snapshot_members: set[str] = set()
    previous_run_id = previous_run[0] if previous_run else None
    if previous_run_id:
        previous_selected = {
            row[0]
            for row in conn.execute(
                "SELECT symbol FROM watchlist_symbol_result WHERE run_id=? "
                "AND result IN ('ADMITTED','CONTINUED')",
                [previous_run_id],
            ).fetchall()
        }
        previous_snapshot_id = conn.execute(
            "SELECT constituent_snapshot_id FROM watchlist_run WHERE run_id=?",
            [previous_run_id],
        ).fetchone()[0]
        validated = conn.execute(
            "SELECT validation_status FROM index_constituent_snapshot WHERE snapshot_id=?",
            [previous_snapshot_id],
        ).fetchone()
        if validated != ("VALIDATED",):
            raise ValueError("previous constituent snapshot is not validated")
        previous_snapshot_members = {
            row[0]
            for row in conn.execute(
                "SELECT symbol FROM index_constituent_snapshot_member WHERE snapshot_id=?",
                [previous_snapshot_id],
            ).fetchall()
        }
    output = []
    for symbol in sorted(members):
        item = rows_by_symbol[symbol]
        rank = item["rank"]
        close = item["close"]
        beta_value = item["beta"]
        observations = item["observations"]
        close_as_of = item["close_as_of"]
        rank_as_of = item["rank_as_of"]
        beta_as_of = item["beta_as_of"]
        if close is None or close_as_of is None:
            result = "NO_CLOSE"
        elif close_as_of < cutoff - timedelta(days=stale_days):
            result = "STALE_CLOSE"
        elif beta_value is None or observations is None or observations < min_observations:
            result = "INSUFFICIENT_BETA"
        elif close >= price_cap:
            result = "PRICE_ABOVE_CAP"
        elif rank_as_of is None or beta_as_of is None:
            raise ValueError("eligible watchlist candidate requires rank and beta evidence times")
        elif not isinstance(rank, int) or rank < 1:
            raise ValueError("eligible watchlist candidate requires a positive integer rank")
        elif rank <= top_n:
            result = "CONTINUED" if symbol in previous_selected else "ADMITTED"
        else:
            result = "DROPPED_BELOW_TOP_N"
        output.append((symbol, result, item))
    if selected_count != sum(1 for _, result, _ in output if result in {"ADMITTED", "CONTINUED"}):
        raise TrackingConflict("watchlist selected_count does not match derived results")
    for symbol in sorted(previous_selected - members):
        if symbol not in previous_snapshot_members:
            raise TrackingConflict("constituent removal lacks previous immutable evidence")
        output.append(
            (
                symbol,
                "CONSTITUENT_REMOVED",
                {
                    "rank": None,
                    "close": None,
                    "beta": None,
                    "observations": None,
                    "close_as_of": None,
                    "rank_as_of": None,
                    "beta_as_of": None,
                },
            )
        )
    existing = conn.execute(
        "SELECT symbol,result,rank,close,beta,observations,evidence_as_of FROM "
        "watchlist_symbol_result WHERE run_id=? ORDER BY symbol",
        [run_id],
    ).fetchall()
    expected_rows = sorted(
        [
            (
                symbol,
                result,
                item["rank"],
                item["close"],
                item["beta"],
                item["observations"],
                item["close_as_of"],
            )
            for symbol, result, item in output
        ]
    )
    if existing:
        if existing != expected_rows:
            raise TrackingConflict("watchlist result replay has different content")
        return {"run_id": run_id, "status": "REPLAY", "result_count": len(output)}
    if _manage_transaction:
        conn.execute("BEGIN TRANSACTION")
    try:
        for symbol, result, item in output:
            reason = {
                "result": result,
                "top_n": top_n,
                "price_cap": price_cap,
                "minimum_observations": min_observations,
                "stale_days": stale_days,
                "previous_run_id": previous_run_id,
            }
            result_id = _event_id(run_id, symbol, result)
            conn.execute(
                "INSERT INTO watchlist_symbol_result VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    result_id,
                    run_id,
                    symbol,
                    source_as_of,
                    methodology,
                    result,
                    item["rank"],
                    item["close"],
                    item["beta"],
                    item["observations"],
                    item["close_as_of"],
                    canonical_json(
                        {
                            **reason,
                            "close_as_of": item["close_as_of"],
                            "rank_as_of": item["rank_as_of"],
                            "beta_as_of": item["beta_as_of"],
                        }
                    ),
                ],
            )
        if _manage_transaction:
            conn.execute("COMMIT")
    except Exception:
        if _manage_transaction:
            conn.execute("ROLLBACK")
        raise
    return {"run_id": run_id, "status": "ACCEPTED", "result_count": len(output)}


def persist_watchlist_run(
    conn: duckdb.DuckDBPyConnection,
    *,
    index_name: str,
    source_as_of: date,
    canonical_cutoff: date,
    methodology_version: str,
    semantic_config: dict,
    constituent_snapshot_id: str,
    candidates: list[dict],
    recorded_at: dt,
) -> dict[str, object]:
    """Atomically persist an accepted watchlist header and its complete results."""
    conn.execute("BEGIN TRANSACTION")
    try:
        run_id = record_watchlist_run_header(
            conn,
            index_name=index_name,
            source_as_of=source_as_of,
            canonical_cutoff=canonical_cutoff,
            methodology_version=methodology_version,
            semantic_config=semantic_config,
            input_fingerprint=watchlist_input_fingerprint(candidates),
            constituent_snapshot_id=constituent_snapshot_id,
            universe_count=len(candidates),
            selected_count=_watchlist_selected_count(candidates, canonical_cutoff, semantic_config),
            recorded_at=recorded_at,
        )
        result = persist_watchlist_results(
            conn,
            run_id=run_id,
            candidates=candidates,
            _manage_transaction=False,
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return result


def _normalize_signal(item: dict, cutoff: date) -> dict:
    action = str(item.get("action", "")).upper()
    if action not in {"ENTER", "EXIT"}:
        raise ValueError("signal action must be enter or exit")
    signal_date = item.get("date")
    if not isinstance(signal_date, date):
        raise ValueError("signal date is required")
    if signal_date > cutoff:
        raise ValueError("signal date is after canonical cutoff")
    symbol = str(item.get("symbol", "")).strip().upper()
    if not symbol:
        raise ValueError("signal symbol is required")
    sizing = item.get("sizing") or {}
    ema10 = item.get("ema_fast")
    ema21 = item.get("ema_slow")
    if ema10 is None or ema21 is None:
        raise ValueError("signal EMA values are required")
    normalized = {
        "symbol": symbol,
        "action": action,
        "signal_date": signal_date,
        "close": float(item["close"]),
        "ema10": float(ema10),
        "ema21": float(ema21),
        "quantity": None,
        "sizing_stop": None,
        "capital_to_deploy": None,
        "maximum_loss_at_stop": None,
        "sizing_gap_reason": None,
    }
    if action == "ENTER":
        quantity = sizing.get("quantity")
        reason = sizing.get("reason")
        complete = all(
            sizing.get(field) is not None
            for field in ("stop", "capital_to_deploy", "maximum_loss_at_stop")
        )
        if not isinstance(quantity, int) or quantity < 0:
            raise ValueError("entry sizing quantity must be a non-negative integer")
        if quantity > 0 and (not complete or reason):
            raise ValueError("sized entry requires complete values and no gap reason")
        if quantity == 0 and (not reason or complete):
            raise ValueError("unsized entry requires one gap reason and no sizing values")
        normalized.update(
            quantity=quantity,
            sizing_stop=sizing.get("stop"),
            capital_to_deploy=sizing.get("capital_to_deploy"),
            maximum_loss_at_stop=sizing.get("maximum_loss_at_stop"),
            sizing_gap_reason=reason,
        )
    return normalized


_TERMINAL_POSITION_STATES = {"CLOSED_CONFIRMED", "IGNORED", "EXPIRED"}
_OPERATOR_TRANSITIONS = {
    "SIGNALLED": {"WATCHING", "OPEN_CONFIRMED", "IGNORED", "EXPIRED"},
    "WATCHING": {"OPEN_CONFIRMED", "IGNORED", "EXPIRED"},
    "OPEN_CONFIRMED": {"CLOSED_CONFIRMED"},
}


def _require_utc(value: dt, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None or value.utcoffset().total_seconds() != 0:
        raise ValueError(f"{field} must be an explicit UTC timestamp")


def _start_signalled_lifecycle(
    conn: duckdb.DuckDBPyConnection,
    *,
    signal_event_id: str,
    signal: dict,
    methodology_version: str,
    recorded_at: dt,
) -> str | None:
    prior = conn.execute(
        "SELECT current_state,state_source_at FROM research_position WHERE market='IN' "
        "AND symbol=? AND methodology_version=? ORDER BY state_source_at DESC LIMIT 1",
        [signal["symbol"], methodology_version],
    ).fetchone()
    source_at = dt.combine(signal["signal_date"], dt.min.time(), tzinfo=UTC)
    if prior is not None and (prior[0] not in _TERMINAL_POSITION_STATES or source_at <= prior[1]):
        return None
    position_id = _event_id("POSITION", methodology_version, "IN", signal_event_id)
    command_fp = fingerprint(
        {
            "actor": "SYSTEM_SIGNAL",
            "evidence_id": signal_event_id,
            "methodology_version": methodology_version,
            "position_id": position_id,
            "source_at": source_at,
            "to_state": "SIGNALLED",
        }
    )
    state_event_id = _event_id(
        position_id, None, "SIGNALLED", source_at, "SYSTEM_SIGNAL", command_fp
    )
    conn.execute(
        "INSERT INTO research_position_identity VALUES (?,?,?)",
        [position_id, signal_event_id, methodology_version],
    )
    conn.execute(
        "INSERT INTO position_state_event VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            state_event_id,
            position_id,
            None,
            "SIGNALLED",
            source_at,
            recorded_at,
            methodology_version,
            "SYSTEM_SIGNAL",
            None,
            command_fp,
            "SIGNAL_EVENT",
            signal_event_id,
            signal_event_id,
        ],
    )
    conn.execute(
        "INSERT INTO position_state_event_link VALUES (?,?)",
        [position_id, state_event_id],
    )
    conn.execute(
        "INSERT INTO research_position VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            position_id,
            "IN",
            signal["symbol"],
            signal_event_id,
            "SIGNALLED",
            source_at,
            state_event_id,
            methodology_version,
            signal["sizing_stop"],
            "ACTIVE",
            recorded_at,
            recorded_at,
        ],
    )
    return position_id


def transition_position(
    conn: duckdb.DuckDBPyConnection,
    *,
    position_id: str,
    to_state: str,
    source_at: dt,
    recorded_at: dt,
    methodology_version: str,
    operator_note: str | None = None,
) -> dict[str, str]:
    """Apply one explicit operator transition with replay and ordering guards."""
    _require_utc(source_at, "source_at")
    _require_utc(recorded_at, "recorded_at")
    to_state = to_state.strip().upper()
    note = operator_note.strip() if operator_note else None
    command_fp = fingerprint(
        {
            "actor": "OPERATOR",
            "methodology_version": methodology_version,
            "operator_note": note,
            "position_id": position_id,
            "source_at": source_at,
            "to_state": to_state,
        }
    )
    replay = conn.execute(
        "SELECT event_id FROM position_state_event WHERE command_fingerprint=?",
        [command_fp],
    ).fetchone()
    if replay is not None:
        return {"event_id": replay[0], "status": "REPLAY"}
    row = conn.execute(
        "SELECT current_state,state_source_at,methodology_version FROM research_position "
        "WHERE position_id=?",
        [position_id],
    ).fetchone()
    if row is None:
        raise ValueError("research position does not exist")
    from_state, prior_source_at, position_methodology = row
    if position_methodology != methodology_version:
        raise TrackingConflict("position methodology does not match operator command")
    if from_state in _TERMINAL_POSITION_STATES or to_state not in _OPERATOR_TRANSITIONS.get(
        from_state, set()
    ):
        raise ValueError(f"illegal position transition {from_state} -> {to_state}")
    if source_at <= prior_source_at:
        raise ValueError("operator source_at must be newer than current state")
    if to_state in {"OPEN_CONFIRMED", "CLOSED_CONFIRMED"} and not note:
        raise ValueError("confirmed open/close requires a non-empty operator note")
    event_id = _event_id(position_id, from_state, to_state, source_at, "OPERATOR", command_fp)
    conn.execute("BEGIN TRANSACTION")
    try:
        conn.execute(
            "INSERT INTO position_state_event VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                event_id,
                position_id,
                from_state,
                to_state,
                source_at,
                recorded_at,
                methodology_version,
                "OPERATOR",
                note,
                command_fp,
                "OPERATOR_COMMAND",
                command_fp,
                None,
            ],
        )
        conn.execute("INSERT INTO position_state_event_link VALUES (?,?)", [position_id, event_id])
        updated = conn.execute(
            "UPDATE research_position SET current_state=?,state_source_at=?,state_event_id=?,"
            "active_slot=?,updated_at=? WHERE position_id=? AND current_state=? "
            "AND state_source_at=? AND methodology_version=? RETURNING position_id",
            [
                to_state,
                source_at,
                event_id,
                None if to_state in _TERMINAL_POSITION_STATES else "ACTIVE",
                recorded_at,
                position_id,
                from_state,
                prior_source_at,
                methodology_version,
            ],
        ).fetchone()
        if updated is None:
            raise TrackingConflict("position changed concurrently")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"event_id": event_id, "status": "APPLIED"}


def _normalize_screen_result(item: dict, cutoff: date) -> dict:
    symbol = str(item.get("symbol", "")).strip().upper()
    outcome = str(item.get("outcome", "")).strip().upper()
    if not symbol or outcome not in {"PASS", "PREDICATE_FAIL", "MISSING_DATA", "STALE_DATA"}:
        raise ValueError("screen symbol and valid outcome are required")
    failed = sorted(set(item.get("failed_predicates") or []))
    missing = sorted(set(item.get("missing_fields") or []))
    stale = sorted(set(item.get("stale_fields") or []))
    expected = (
        "STALE_DATA"
        if stale
        else "MISSING_DATA"
        if missing
        else "PREDICATE_FAIL"
        if failed
        else "PASS"
    )
    if outcome != expected:
        raise ValueError(f"screen outcome violates evidence precedence for {symbol}")
    evidence_as_of = item.get("evidence_as_of")
    if evidence_as_of is not None and (
        not isinstance(evidence_as_of, date) or evidence_as_of > cutoff
    ):
        raise ValueError("screen evidence_as_of must be a date no later than cutoff")
    source = str(item.get("source", "")).strip()
    if not source:
        raise ValueError("screen result source is required")
    return {
        "symbol": symbol,
        "outcome": outcome,
        "failed_predicates": failed,
        "missing_fields": missing,
        "stale_fields": stale,
        "evidence_as_of": evidence_as_of,
        "source": source,
        "metrics": item.get("metrics") or {},
    }


def persist_screen_evaluation(
    conn: duckdb.DuckDBPyConnection,
    *,
    screen_id: str,
    source_as_of: date,
    canonical_cutoff: date,
    methodology_version: str,
    semantic_config: dict,
    expected_symbols: set[str],
    results: list[dict],
    recorded_at: dt,
) -> dict[str, object]:
    """Persist complete screen outcomes and causal membership changes atomically."""
    _require_utc(recorded_at, "recorded_at")
    if not screen_id.strip() or source_as_of != canonical_cutoff:
        raise ValueError("screen id and matching canonical cutoff are required")
    config_fp = _registered_config(conn, methodology_version, semantic_config)
    normalized = [_normalize_screen_result(item, canonical_cutoff) for item in results]
    symbols = [item["symbol"] for item in normalized]
    if len(symbols) != len(set(symbols)):
        raise ValueError("screen evaluation contains a duplicate symbol")
    expected = {str(symbol).strip().upper() for symbol in expected_symbols if str(symbol).strip()}
    if set(symbols) != expected:
        missing = sorted(expected - set(symbols))
        extra = sorted(set(symbols) - expected)
        raise ValueError(f"screen results are incomplete: missing={missing}, extra={extra}")
    normalized.sort(key=lambda item: item["symbol"])
    input_fp = fingerprint(
        {
            "screen_id": screen_id,
            "source_as_of": source_as_of,
            "canonical_cutoff": canonical_cutoff,
            "methodology_version": methodology_version,
            "config_fingerprint": config_fp,
            "expected_symbols": sorted(expected),
            "results": normalized,
        }
    )
    run_id = fingerprint([screen_id, methodology_version, source_as_of, input_fp])
    response = {"run_id": run_id, "status": "ACCEPTED", "evaluated_count": len(normalized)}
    prior_attempt = conn.execute(
        "SELECT status FROM screen_evaluation_run WHERE run_id=?", [run_id]
    ).fetchone()
    if prior_attempt:
        if prior_attempt[0] == "CONFLICT":
            raise TrackingConflict("screen natural key has different content")
        return {
            **response,
            "status": "REPLAY" if prior_attempt[0] == "ACCEPTED" else prior_attempt[0],
        }
    conn.execute("BEGIN TRANSACTION")
    try:
        existing = conn.execute(
            "SELECT run_id,input_fingerprint FROM screen_evaluation_run WHERE screen_id=? "
            "AND methodology_version=? AND source_as_of=? AND status='ACCEPTED'",
            [screen_id, methodology_version, source_as_of],
        ).fetchone()
        latest = conn.execute(
            "SELECT max(source_as_of) FROM screen_evaluation_run WHERE screen_id=? "
            "AND methodology_version=? AND status='ACCEPTED'",
            [screen_id, methodology_version],
        ).fetchone()[0]
        status = (
            "CONFLICT"
            if existing
            else (
                "REJECTED_OUT_OF_ORDER"
                if latest is not None and source_as_of < latest
                else "ACCEPTED"
            )
        )
        conn.execute(
            "INSERT INTO screen_evaluation_run VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                run_id,
                screen_id,
                source_as_of,
                canonical_cutoff,
                recorded_at,
                methodology_version,
                config_fp,
                input_fp,
                status,
                len(normalized),
                "ACCEPTED" if status == "ACCEPTED" else None,
            ],
        )
        if status == "ACCEPTED":
            for item in normalized:
                conn.execute(
                    "INSERT INTO screen_symbol_result VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        run_id,
                        screen_id,
                        item["symbol"],
                        source_as_of,
                        methodology_version,
                        item["outcome"],
                        canonical_json(item["failed_predicates"]),
                        canonical_json(item["missing_fields"]),
                        canonical_json(item["stale_fields"]),
                        item["evidence_as_of"],
                        item["source"],
                        canonical_json(item["metrics"]),
                    ],
                )
            previous_same = conn.execute(
                "SELECT run_id FROM screen_evaluation_run WHERE screen_id=? "
                "AND methodology_version=? AND status='ACCEPTED' AND source_as_of<? "
                "ORDER BY source_as_of DESC LIMIT 1",
                [screen_id, methodology_version, source_as_of],
            ).fetchone()
            previous_any = conn.execute(
                "SELECT run_id,methodology_version FROM screen_evaluation_run WHERE screen_id=? "
                "AND methodology_version<>? AND status='ACCEPTED' "
                "ORDER BY source_as_of DESC,recorded_at DESC LIMIT 1",
                [screen_id, methodology_version],
            ).fetchone()
            event_rows: list[tuple[str, str, str | None, dict]] = []
            if previous_same is None and previous_any is not None:
                previous_symbols = {
                    row[0]
                    for row in conn.execute(
                        "SELECT symbol FROM screen_symbol_result WHERE run_id=?",
                        [previous_any[0]],
                    ).fetchall()
                }
                for symbol in sorted(previous_symbols | set(symbols)):
                    event_rows.append(
                        (
                            symbol,
                            "METHODOLOGY_RESET",
                            previous_any[0],
                            {
                                "from_methodology": previous_any[1],
                                "to_methodology": methodology_version,
                            },
                        )
                    )
            else:
                previous = {}
                previous_run_id = previous_same[0] if previous_same else None
                if previous_run_id:
                    previous = dict(
                        conn.execute(
                            "SELECT symbol,outcome FROM screen_symbol_result WHERE run_id=?",
                            [previous_run_id],
                        ).fetchall()
                    )
                for item in normalized:
                    old = previous.get(item["symbol"])
                    new = item["outcome"]
                    event_type = None
                    if new == "PASS":
                        event_type = "CONTINUED" if old == "PASS" else "ENTERED"
                    elif old == "PASS":
                        event_type = {
                            "PREDICATE_FAIL": "EXITED_PREDICATE",
                            "MISSING_DATA": "EXITED_MISSING_DATA",
                            "STALE_DATA": "EXITED_STALE_DATA",
                        }[new]
                    if event_type:
                        event_rows.append(
                            (item["symbol"], event_type, previous_run_id, {"outcome": new})
                        )
            for symbol, event_type, previous_run_id, reason in event_rows:
                event_id = _event_id(
                    screen_id, symbol, methodology_version, source_as_of, event_type
                )
                conn.execute(
                    "INSERT INTO screen_membership_event VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [
                        event_id,
                        run_id,
                        screen_id,
                        symbol,
                        source_as_of,
                        recorded_at,
                        methodology_version,
                        event_type,
                        previous_run_id,
                        canonical_json(reason),
                    ],
                )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    if status == "CONFLICT":
        raise TrackingConflict("screen natural key has different content")
    return {**response, "status": status}


def persist_signal_run(
    conn: duckdb.DuckDBPyConnection,
    report: dict,
    *,
    watchlist_run_id: str,
    methodology_version: str,
    semantic_config: dict,
    recorded_at: dt,
) -> dict[str, object]:
    """Persist one signal run and immutable generated events atomically."""
    _require_utc(recorded_at, "recorded_at")
    config_fp = _registered_config(conn, methodology_version, semantic_config)
    linked = conn.execute(
        "SELECT canonical_cutoff,methodology_version,config_fingerprint,status,index_name "
        "FROM watchlist_run WHERE run_id=?",
        [watchlist_run_id],
    ).fetchone()
    if linked is None or linked[3] != "ACCEPTED":
        raise ValueError("accepted watchlist run is required")
    cutoff = linked[0]
    if linked[1] != methodology_version or linked[2] != config_fp:
        raise TrackingConflict("signal and watchlist methodology differ")
    index_name = linked[4]
    if semantic_config.get("index") != index_name:
        raise TrackingConflict("signal index does not match semantic config")
    if report.get("as_of") != cutoff:
        raise ValueError("signal source_as_of must equal linked canonical cutoff")
    signals = [_normalize_signal(item, cutoff) for item in report.get("signals", [])]
    natural_keys = [(item["symbol"], item["signal_date"], item["action"]) for item in signals]
    if len(natural_keys) != len(set(natural_keys)):
        raise ValueError("signal artifact contains a duplicate natural key")
    payload = {
        "watchlist_run_id": watchlist_run_id,
        "source_as_of": cutoff,
        "canonical_cutoff": cutoff,
        "methodology_version": methodology_version,
        "semantic_config_fingerprint": config_fp,
        "scanned": int(report.get("scanned", 0)),
        "since": report.get("since"),
        "first_run": report.get("first_run"),
        "sizing_gaps": int(report.get("sizing_gaps", 0)),
        "signals": signals,
    }
    input_fp = fingerprint(payload)
    run_id = fingerprint(
        {
            "methodology_version": methodology_version,
            "source_as_of": cutoff,
            "input_fingerprint": input_fp,
        }
    )
    result = {"run_id": run_id, "status": "ACCEPTED", "signal_count": len(signals)}
    prior_attempt = conn.execute(
        "SELECT status FROM signal_run WHERE run_id=?", [run_id]
    ).fetchone()
    if prior_attempt:
        replay_status = "REPLAY" if prior_attempt[0] == "ACCEPTED" else prior_attempt[0]
        return {**result, "status": replay_status}

    existing = conn.execute(
        "SELECT run_id,input_fingerprint FROM signal_run "
        "WHERE methodology_version=? AND watchlist_run_id=? AND source_as_of=? "
        "AND status='ACCEPTED'",
        [methodology_version, watchlist_run_id, cutoff],
    ).fetchone()
    if existing and existing[1] == input_fp:
        return {**result, "run_id": existing[0], "status": "REPLAY"}

    latest = conn.execute(
        "SELECT max(s.source_as_of) FROM signal_run s JOIN watchlist_run w "
        "ON w.run_id=s.watchlist_run_id WHERE s.methodology_version=? "
        "AND w.index_name=? AND s.status='ACCEPTED'",
        [methodology_version, index_name],
    ).fetchone()[0]
    status = (
        "CONFLICT"
        if existing
        else ("REJECTED_OUT_OF_ORDER" if latest is not None and cutoff < latest else "ACCEPTED")
    )

    conn.execute("BEGIN TRANSACTION")
    try:
        conn.execute(
            "INSERT INTO signal_run VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                run_id,
                cutoff,
                cutoff,
                recorded_at,
                recorded_at,
                methodology_version,
                config_fp,
                input_fp,
                watchlist_run_id,
                status,
                int(report.get("scanned", 0)),
                len(signals),
                canonical_json(
                    {
                        "since": report.get("since"),
                        "first_run": report.get("first_run"),
                        "sizing_gaps": report.get("sizing_gaps", 0),
                    }
                ),
            ],
        )
        if status == "ACCEPTED":
            for signal in signals:
                event_id = _event_id(
                    methodology_version,
                    signal["symbol"],
                    signal["signal_date"],
                    signal["action"],
                )
                prior = conn.execute(
                    "SELECT close,ema10,ema21,quantity,sizing_stop,capital_to_deploy,"
                    "maximum_loss_at_stop,sizing_gap_reason FROM signal_event "
                    "WHERE methodology_version=? AND symbol=? AND signal_date=? AND action=?",
                    [
                        methodology_version,
                        signal["symbol"],
                        signal["signal_date"],
                        signal["action"],
                    ],
                ).fetchone()
                values = (
                    signal["close"],
                    signal["ema10"],
                    signal["ema21"],
                    signal["quantity"],
                    signal["sizing_stop"],
                    signal["capital_to_deploy"],
                    signal["maximum_loss_at_stop"],
                    signal["sizing_gap_reason"],
                )
                if prior is not None:
                    if prior != values:
                        raise TrackingConflict("signal natural key has different content")
                    continue
                conn.execute(
                    "INSERT INTO signal_event VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        event_id,
                        run_id,
                        signal["symbol"],
                        signal["signal_date"],
                        cutoff,
                        signal["action"],
                        *values,
                        methodology_version,
                        recorded_at,
                    ],
                )
                if signal["action"] == "ENTER":
                    _start_signalled_lifecycle(
                        conn,
                        signal_event_id=event_id,
                        signal=signal,
                        methodology_version=methodology_version,
                        recorded_at=recorded_at,
                    )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {**result, "status": status}


def persist_market_price_evidence(
    conn: duckdb.DuckDBPyConnection,
    *,
    symbol: str,
    trade_date: date,
    cutoff_at: dt,
    recorded_at: dt,
) -> str:
    """Freeze one stored close whose fetch time is no later than the cutoff."""
    _require_utc(cutoff_at, "cutoff_at")
    _require_utc(recorded_at, "recorded_at")
    row = conn.execute(
        "SELECT close,source,fetched_at FROM stock_price WHERE symbol=? AND trade_date=?",
        [symbol, trade_date],
    ).fetchone()
    if row is None or row[0] is None or row[0] <= 0:
        raise ValueError("positive stored price evidence is required")
    fetched_at = row[2]
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=UTC)
    if fetched_at > cutoff_at:
        raise ValueError("price evidence was fetched after the observation cutoff")
    evidence_id = _event_id("MARKET_PRICE", symbol, trade_date, row[0], row[1], fetched_at)
    conn.execute(
        "INSERT INTO market_price_evidence VALUES (?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
        [evidence_id, symbol, trade_date, row[0], row[1], fetched_at, recorded_at],
    )
    return evidence_id


def persist_position_observation(
    conn: duckdb.DuckDBPyConnection,
    *,
    position_id: str,
    observation_type: str,
    source_at: dt,
    observed_close: float,
    methodology_version: str,
    recorded_at: dt,
    signal_event_id: str | None = None,
    price_evidence_id: str | None = None,
    observed_ema10: float | None = None,
    observed_ema21: float | None = None,
) -> dict[str, str]:
    """Record an alertable observation without changing position state."""
    _require_utc(source_at, "source_at")
    _require_utc(recorded_at, "recorded_at")
    position = conn.execute(
        "SELECT market,symbol,current_state,state_source_at,methodology_version,"
        "entry_sizing_stop FROM research_position WHERE position_id=?",
        [position_id],
    ).fetchone()
    if position is None or position[0] != "IN" or position[2] != "OPEN_CONFIRMED":
        raise ValueError("observation requires an OPEN_CONFIRMED Indian position")
    symbol, confirmed_at, methodology, sizing_stop = (
        position[1],
        position[3],
        position[4],
        position[5],
    )
    if methodology != methodology_version:
        raise TrackingConflict("observation methodology does not match position")
    if source_at <= confirmed_at:
        raise ValueError("observation evidence must be newer than confirmation")
    if observed_close <= 0:
        raise ValueError("observed close must be positive")
    evidence_id: str
    if observation_type == "EMA_EXIT":
        signal = conn.execute(
            "SELECT symbol,action,source_as_of,methodology_version,close,ema10,ema21 "
            "FROM signal_event WHERE event_id=?",
            [signal_event_id],
        ).fetchone()
        if signal is None or signal[:4] != (
            symbol,
            "EXIT",
            source_at.date(),
            methodology_version,
        ):
            raise ValueError("EMA exit requires matching immutable EXIT signal evidence")
        if (observed_close, observed_ema10, observed_ema21) != (signal[4], signal[5], signal[6]):
            raise TrackingConflict("EMA observation does not match signal evidence")
        if price_evidence_id is not None:
            raise ValueError("EMA exit cannot use price evidence")
        evidence_id = str(signal_event_id)
    elif observation_type == "BELOW_ENTRY_SIZING_STOP":
        if sizing_stop is None or sizing_stop <= 0:
            raise ValueError("position has no valid immutable sizing stop")
        if observed_close >= sizing_stop or not price_evidence_id or signal_event_id is not None:
            raise ValueError(
                "stop observation requires a close below sizing stop and price evidence"
            )
        price_row = conn.execute(
            "SELECT symbol,trade_date,close,fetched_at FROM market_price_evidence "
            "WHERE evidence_id=?",
            [price_evidence_id],
        ).fetchone()
        if price_row is None or price_row[:3] != (symbol, source_at.date(), observed_close):
            raise ValueError("stop observation requires matching immutable price evidence")
        fetched_at = price_row[3]
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=UTC)
        if fetched_at > source_at:
            raise ValueError("price evidence is after the observation cutoff")
        evidence_id = price_evidence_id
    else:
        raise ValueError("unknown position observation type")
    content_fp = fingerprint(
        {
            "position_id": position_id,
            "observation_type": observation_type,
            "evidence_id": evidence_id,
            "source_at": source_at,
            "observed_close": observed_close,
            "observed_ema10": observed_ema10,
            "observed_ema21": observed_ema21,
            "methodology_version": methodology_version,
        }
    )
    event_id = _event_id(position_id, observation_type, evidence_id, source_at)
    existing = conn.execute(
        "SELECT content_fingerprint FROM position_observation_event WHERE event_id=?", [event_id]
    ).fetchone()
    if existing:
        if existing[0] != content_fp:
            raise TrackingConflict("observation natural key has different content")
        return {"event_id": event_id, "status": "REPLAY"}
    conn.execute(
        "INSERT INTO position_observation_event VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            event_id,
            position_id,
            observation_type,
            signal_event_id,
            price_evidence_id,
            source_at,
            observed_close,
            observed_ema10,
            observed_ema21,
            methodology_version,
            recorded_at,
            content_fp,
        ],
    )
    return {"event_id": event_id, "status": "ACCEPTED"}


def persist_broker_reconciliation(conn: duckdb.DuckDBPyConnection, **kwargs) -> dict[str, object]:
    """Run broker validation, classification, and inserts in one transaction."""
    conn.execute("BEGIN TRANSACTION")
    try:
        result = _persist_broker_reconciliation_transaction(conn, **kwargs)
        conn.execute("COMMIT")
        return result
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _persist_broker_reconciliation_transaction(
    conn: duckdb.DuckDBPyConnection,
    *,
    broker_run_id: str,
    source_at: dt,
    methodology_version: str,
    mapping_policy_version: str,
    max_age_days: int,
    recorded_at: dt,
) -> dict[str, object]:
    """Compare exact positive NSE holdings without mutating research positions."""
    from invest import kite

    _require_utc(source_at, "source_at")
    _require_utc(recorded_at, "recorded_at")
    if max_age_days < 0 or not mapping_policy_version.strip():
        raise ValueError("mapping policy and non-negative freshness limit are required")
    _registered_config_row = conn.execute(
        "SELECT 1 FROM tracking_methodology WHERE methodology_version=?",
        [methodology_version],
    ).fetchone()
    if _registered_config_row is None:
        raise ValueError("tracking methodology is not registered")
    latest = conn.execute(
        "SELECT run_id,snapshot_date,fetched_at FROM broker_snapshot_run "
        "ORDER BY snapshot_date DESC,fetched_at DESC LIMIT 1"
    ).fetchone()
    if (
        latest is None
        or latest[0] != broker_run_id
        or not kite.snapshot_integrity(conn, broker_run_id)
    ):
        raise ValueError("latest integrity-verified broker snapshot is required")
    snapshot_at = latest[2]
    if snapshot_at.tzinfo is None:
        snapshot_at = snapshot_at.replace(tzinfo=UTC)
    if (
        latest[1] > source_at.date()
        or snapshot_at > source_at
        or (source_at.date() - latest[1]).days > max_age_days
    ):
        raise ValueError("broker snapshot is stale or future-dated")
    holdings = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT tradingsymbol FROM broker_holding WHERE run_id=? "
            "AND exchange='NSE' AND quantity>0",
            [broker_run_id],
        ).fetchall()
    }
    open_positions = {
        row[1]: row[0]
        for row in conn.execute(
            "SELECT position_id,symbol FROM research_position WHERE market='IN' "
            "AND methodology_version=? AND current_state='OPEN_CONFIRMED'",
            [methodology_version],
        ).fetchall()
    }
    events = []
    for symbol in sorted(set(open_positions) - holdings):
        events.append((open_positions[symbol], symbol, "CONFIRMED_MISSING"))
    active_symbols = {
        row[0]
        for row in conn.execute(
            "SELECT symbol FROM research_position WHERE market='IN' "
            "AND current_state NOT IN ('CLOSED_CONFIRMED','IGNORED','EXPIRED')",
        ).fetchall()
    }
    for symbol in sorted(holdings - active_symbols):
        events.append((None, symbol, "UNTRACKED_PRESENT"))
    inserted = 0
    for position_id, symbol, event_type in events:
        event_id = _event_id(position_id or f"IN:{symbol}", event_type, broker_run_id)
        content_fp = fingerprint(
            {
                "broker_run_id": broker_run_id,
                "position_id": position_id,
                "market": "IN",
                "symbol": symbol,
                "reconciliation_type": event_type,
                "broker_snapshot_at": snapshot_at,
                "source_at": source_at,
                "methodology_version": methodology_version,
                "mapping_policy_version": mapping_policy_version,
            }
        )
        existing = conn.execute(
            "SELECT content_fingerprint FROM broker_reconciliation_event WHERE event_id=?",
            [event_id],
        ).fetchone()
        if existing:
            if existing[0] != content_fp:
                raise TrackingConflict("broker reconciliation natural key has different content")
            continue
        inserted += (
            conn.execute(
                "INSERT INTO broker_reconciliation_event VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                "RETURNING event_id",
                [
                    event_id,
                    broker_run_id,
                    position_id,
                    "IN",
                    symbol,
                    event_type,
                    snapshot_at,
                    source_at,
                    methodology_version,
                    mapping_policy_version,
                    recorded_at,
                    content_fp,
                ],
            ).fetchone()
            is not None
        )
    return {"status": "ACCEPTED" if inserted else "REPLAY", "event_count": inserted}


class DeliveryBeforeSend(RuntimeError):
    """The transport proved that no request bytes were transmitted."""


def enqueue_change_alerts(
    conn: duckdb.DuckDBPyConnection,
    *,
    destination: str,
    recorded_at: dt,
) -> int:
    """Queue alerts for new immutable screen/watchlist changes, never continuations."""
    _require_utc(recorded_at, "recorded_at")
    if not destination.strip():
        raise ValueError("alert destination is required")
    candidates: list[tuple[str, str, str, str, str, object, str]] = []
    for event_id, screen_id, symbol, source_at, methodology, event_type in conn.execute(
        "SELECT event_id,screen_id,symbol,source_as_of,methodology_version,event_type "
        "FROM screen_membership_event WHERE event_type IN "
        "('EXITED_PREDICATE','EXITED_MISSING_DATA','EXITED_STALE_DATA')"
    ).fetchall():
        candidates.append(
            (
                event_type,
                "SCREEN_SYMBOL",
                f"{screen_id}:{symbol}",
                event_id,
                methodology,
                source_at,
                f"{screen_id} {symbol}: {event_type} as of {source_at}. "
                f"Methodology {methodology}. Research context only. "
                "No trade instruction is produced.",
            )
        )
    for event_id, position_id, observation_type, source_at, methodology in conn.execute(
        "SELECT event_id,position_id,observation_type,source_at,methodology_version "
        "FROM position_observation_event"
    ).fetchall():
        candidates.append(
            (
                observation_type,
                "RESEARCH_POSITION",
                position_id,
                event_id,
                methodology,
                source_at,
                f"Research position {position_id[:12]}: {observation_type} as of {source_at}. "
                f"Methodology {methodology}. Research context only. "
                "No trade instruction is produced.",
            )
        )
    for event_id, position_id, symbol, event_type, source_at, methodology in conn.execute(
        "SELECT event_id,position_id,symbol,reconciliation_type,source_at,"
        "methodology_version FROM broker_reconciliation_event"
    ).fetchall():
        candidates.append(
            (
                event_type,
                "RESEARCH_POSITION" if position_id else "BROKER_SYMBOL",
                position_id or f"IN:{symbol}",
                event_id,
                methodology,
                source_at,
                f"IN {symbol}: {event_type} as of {source_at}. Methodology {methodology}. "
                "Research context only. No trade instruction is produced.",
            )
        )
    watch_rows = conn.execute(
        "SELECT r.result_id,r.run_id,r.symbol,r.source_as_of,r.methodology_version,r.result,"
        "w.index_name FROM watchlist_symbol_result r JOIN watchlist_run w ON w.run_id=r.run_id "
        "WHERE r.result<>'CONTINUED'"
    ).fetchall()
    for result_id, _run_id, symbol, source_at, methodology, result, index_name in watch_rows:
        previous = conn.execute(
            "SELECT r.result FROM watchlist_symbol_result r JOIN watchlist_run w "
            "ON w.run_id=r.run_id "
            "WHERE w.index_name=? AND r.methodology_version=? AND r.symbol=? "
            "AND w.status='ACCEPTED' AND r.source_as_of<? ORDER BY r.source_as_of DESC LIMIT 1",
            [index_name, methodology, symbol, source_at],
        ).fetchone()
        if result != "ADMITTED" and (previous is None or previous[0] == result):
            continue
        candidates.append(
            (
                "WATCHLIST_CHANGE",
                "WATCHLIST_SYMBOL",
                f"{index_name}:{symbol}",
                result_id,
                methodology,
                source_at,
                f"{index_name} {symbol}: {result} as of {source_at}. "
                f"Methodology {methodology}. Research context only. "
                "No trade instruction is produced.",
            )
        )
    inserted = 0
    conn.execute("BEGIN TRANSACTION")
    try:
        for (
            alert_type,
            subject_type,
            subject_id,
            causal_id,
            methodology,
            source_at,
            message,
        ) in candidates:
            alert_fp = fingerprint(
                [alert_type, subject_type, subject_id, causal_id, methodology, destination]
            )
            inserted += (
                conn.execute(
                    "INSERT INTO research_alert_delivery VALUES "
                    "(?,?,?,?,?,?,?,?,?,'PENDING',0,0,NULL,NULL,NULL,NULL,NULL,NULL) "
                    "ON CONFLICT DO NOTHING RETURNING fingerprint",
                    [
                        alert_fp,
                        alert_type,
                        subject_type,
                        subject_id,
                        causal_id,
                        methodology,
                        source_at,
                        destination,
                        message,
                    ],
                ).fetchone()
                is not None
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return inserted


def claim_alert(
    conn: duckdb.DuckDBPyConnection,
    *,
    fingerprint_value: str,
    claimed_at: dt,
    lease_seconds: int = 60,
) -> dict[str, object] | None:
    """Fence one retryable delivery and return its token/message."""
    _require_utc(claimed_at, "claimed_at")
    if lease_seconds < 1:
        raise ValueError("lease_seconds must be positive")
    token = secrets.token_hex(16)
    expires = claimed_at + timedelta(seconds=lease_seconds)
    row = conn.execute(
        "UPDATE research_alert_delivery SET status='SENDING',attempts=attempts+1,"
        "claim_generation=claim_generation+1,claim_token=?,claimed_at=?,claim_expires_at=?,"
        "last_attempt_at=?,last_error=NULL WHERE fingerprint=? "
        "AND status IN ('PENDING','FAILED_BEFORE_SEND') RETURNING "
        "claim_generation,claim_token,message,destination",
        [token, claimed_at, expires, claimed_at, fingerprint_value],
    ).fetchone()
    if row is None:
        return None
    return {"generation": row[0], "token": row[1], "message": row[2], "destination": row[3]}


def _fence_exists(conn, fingerprint_value: str, generation: int, token: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM research_alert_delivery WHERE fingerprint=? AND status='SENDING' "
            "AND claim_generation=? AND claim_token=?",
            [fingerprint_value, generation, token],
        ).fetchone()
        is not None
    )


def finalize_alert(
    conn: duckdb.DuckDBPyConnection,
    *,
    fingerprint_value: str,
    generation: int,
    token: str,
    status: str,
    finalized_at: dt,
    error: str | None = None,
) -> bool:
    """Finalize only the worker's current fence."""
    _require_utc(finalized_at, "finalized_at")
    if status not in {"SENT", "FAILED_BEFORE_SEND", "UNKNOWN_AFTER_SEND"}:
        raise ValueError("invalid delivery final status")
    row = conn.execute(
        "UPDATE research_alert_delivery SET status=?,claim_token=NULL,claimed_at=NULL,"
        "claim_expires_at=NULL,sent_at=?,last_error=? WHERE fingerprint=? AND status='SENDING' "
        "AND claim_generation=? AND claim_token=? RETURNING fingerprint",
        [
            status,
            finalized_at if status == "SENT" else None,
            error,
            fingerprint_value,
            generation,
            token,
        ],
    ).fetchone()
    return row is not None


def deliver_claimed_alert(
    conn: duckdb.DuckDBPyConnection,
    *,
    fingerprint_value: str,
    generation: int,
    token: str,
    sender,
    attempted_at: dt,
) -> str:
    """Recheck the fence, send once, and classify ambiguous failures conservatively."""
    _require_utc(attempted_at, "attempted_at")
    # DuckDB has no row locks. Keep one write transaction from the final fence
    # check through finalization so an expiry worker cannot invalidate the claim
    # while the transport is transmitting.
    conn.execute("BEGIN TRANSACTION")
    try:
        row = conn.execute(
            "SELECT destination,message,claim_expires_at FROM research_alert_delivery "
            "WHERE fingerprint=? AND status='SENDING' AND claim_generation=? AND claim_token=?",
            [fingerprint_value, generation, token],
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return "LOST_FENCE"
        if row[2] <= attempted_at:
            finalize_alert(
                conn,
                fingerprint_value=fingerprint_value,
                generation=generation,
                token=token,
                status="UNKNOWN_AFTER_SEND",
                finalized_at=attempted_at,
                error="delivery claim expired",
            )
            conn.execute("COMMIT")
            return "LOST_FENCE"
        try:
            sender(row[0], row[1])
        except DeliveryBeforeSend as exc:
            final_status = "FAILED_BEFORE_SEND"
            error = str(exc)
        except Exception as exc:
            final_status = "UNKNOWN_AFTER_SEND"
            error = str(exc)
        else:
            final_status = "SENT"
            error = None
        if not finalize_alert(
            conn,
            fingerprint_value=fingerprint_value,
            generation=generation,
            token=token,
            status=final_status,
            finalized_at=attempted_at,
            error=error,
        ):
            conn.execute("ROLLBACK")
            return "LOST_FENCE"
        conn.execute("COMMIT")
        return final_status
    except Exception:
        conn.execute("ROLLBACK")
        raise


def dispatch_telegram_alert(
    conn: duckdb.DuckDBPyConnection,
    *,
    fingerprint_value: str,
    bot_token: str,
    attempted_at: dt,
    poster=None,
) -> str:
    """Claim and deliver one ledger row through the existing Telegram adapter."""
    if not bot_token:
        raise ValueError("Telegram bot token is required before claiming delivery")
    claim = claim_alert(conn, fingerprint_value=fingerprint_value, claimed_at=attempted_at)
    if claim is None:
        return "NOT_CLAIMED"

    def sender(destination: str, message: str) -> None:
        from invest import alerts

        alerts.send_message(bot_token, destination, message, poster=poster)

    return deliver_claimed_alert(
        conn,
        fingerprint_value=fingerprint_value,
        generation=int(claim["generation"]),
        token=str(claim["token"]),
        sender=sender,
        attempted_at=attempted_at,
    )


def expire_alert_claims(conn: duckdb.DuckDBPyConnection, *, now: dt) -> int:
    """Convert expired in-flight claims to non-retryable unknown outcomes."""
    _require_utc(now, "now")
    rows = conn.execute(
        "SELECT fingerprint,claim_generation,claim_token FROM research_alert_delivery "
        "WHERE status='SENDING' AND claim_expires_at<=?",
        [now],
    ).fetchall()
    changed = 0
    for alert_fp, generation, token in rows:
        changed += finalize_alert(
            conn,
            fingerprint_value=alert_fp,
            generation=generation,
            token=token,
            status="UNKNOWN_AFTER_SEND",
            finalized_at=now,
            error="delivery claim expired",
        )
    return changed


def resolve_unknown_alert(
    conn: duckdb.DuckDBPyConnection,
    *,
    fingerprint_value: str,
    resolution: str,
    resolved_at: dt,
) -> bool:
    """Operator-only resolution of an ambiguous send; requeue accepts duplicate risk."""
    _require_utc(resolved_at, "resolved_at")
    if resolution not in {"SENT", "REQUEUE"}:
        raise ValueError("resolution must be SENT or REQUEUE")
    status = "SENT" if resolution == "SENT" else "PENDING"
    row = conn.execute(
        "UPDATE research_alert_delivery SET status=?,claim_generation=claim_generation+1,"
        "sent_at=?,last_error=NULL WHERE fingerprint=? AND status='UNKNOWN_AFTER_SEND' "
        "RETURNING fingerprint",
        [status, resolved_at if status == "SENT" else None, fingerprint_value],
    ).fetchone()
    return row is not None
