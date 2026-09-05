"""Persist Phase 9 histories from deterministic nightly artifacts."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, timedelta
from datetime import datetime as dt
from pathlib import Path

from invest import alerts, db, screens, signals, tracking, universe, watchlist


def semantic_config(swing_config: dict, screen_config: dict) -> dict:
    return {
        "index": swing_config["universe_index"],
        "benchmark": swing_config["benchmark"],
        "top_n": swing_config["top_n"],
        "price_cap": swing_config["max_price"],
        "beta_window_days": swing_config["window_days"],
        "beta_min_observations": swing_config["min_observations"],
        "ema_fast": 10,
        "ema_slow": 21,
        "stop_mode": swing_config["stop_mode"],
        "risk_fraction": swing_config["risk_fraction"],
        "max_price_age_days": swing_config["max_price_age_days"],
        "constituent_source": watchlist.SOURCE_CONSTITUENTS,
        "constituent_min_count": swing_config["constituent_min_count"],
        "screen_universe_source": universe.SOURCE,
        "screens": screen_config["screens"],
    }


def _watchlist_candidates(conn, config: dict) -> tuple[object, list[dict]]:
    members = [
        row[0]
        for row in conn.execute(
            "SELECT symbol FROM index_constituent WHERE index_name=? ORDER BY symbol",
            [config["universe_index"]],
        ).fetchall()
    ]
    closes = watchlist.latest_closes(conn)
    latest = max((day for day, _close in closes.values()), default=None)
    if latest is None:
        raise ValueError("watchlist has no close evidence")
    usable = [
        day
        for symbol in members
        if symbol in closes
        for day in [closes[symbol][0]]
        if day >= latest - timedelta(days=config["max_price_age_days"])
    ]
    if not usable:
        raise ValueError("watchlist has no usable close evidence")
    cutoff = min(usable)
    qualified = conn.execute(
        "SELECT symbol FROM index_constituent WHERE index_name=? AND source=? "
        "AND CAST(fetched_at AS DATE) <= ? ORDER BY symbol",
        [config["universe_index"], watchlist.SOURCE_CONSTITUENTS, cutoff],
    ).fetchall()
    if len(qualified) != len(members):
        raise ValueError("constituent evidence is newer than canonical cutoff or unapproved")
    members = [row[0] for row in qualified]
    # Every persisted candidate uses only SQL-selected evidence at or before
    # the canonical cutoff. Do not clamp a later observation into this run.
    closes = watchlist.latest_closes(conn, cutoff=cutoff)
    rows = []
    rankable = []
    for symbol in members:
        close_info = closes.get(symbol)
        close_day, close = close_info if close_info else (None, None)
        beta_value, observations = watchlist.beta(
            conn,
            symbol,
            benchmark=config["benchmark"],
            window_days=config["window_days"],
            min_observations=config["min_observations"],
            cutoff=cutoff,
        )
        item = {
            "symbol": symbol,
            "rank": None,
            "close": close,
            "beta": beta_value,
            "observations": observations,
            "close_as_of": close_day,
            "rank_as_of": cutoff if beta_value is not None else None,
            "beta_as_of": cutoff if beta_value is not None else None,
        }
        rows.append(item)
        if (
            close is not None
            and close_day is not None
            and close_day >= cutoff - timedelta(days=config["max_price_age_days"])
            and beta_value is not None
            and close < config["max_price"]
        ):
            rankable.append(item)
    rankable.sort(key=lambda item: (-item["beta"], item["symbol"]))
    for rank, item in enumerate(rankable, 1):
        item["rank"] = rank
    return cutoff, rows


def _persist_watchlist(conn, config: dict, semantics: dict, recorded_at: dt) -> str:
    cutoff, candidates = _watchlist_candidates(conn, config)
    member_rows = conn.execute(
        "SELECT symbol,company_name,industry,isin,series FROM index_constituent "
        "WHERE index_name=? AND source=? AND CAST(fetched_at AS DATE) <= ? ORDER BY symbol",
        [config["universe_index"], watchlist.SOURCE_CONSTITUENTS, cutoff],
    ).fetchall()
    snapshot_id = tracking.persist_constituent_snapshot(
        conn,
        index_name=config["universe_index"],
        source_as_of=cutoff,
        fetched_at=recorded_at,
        source=watchlist.SOURCE_CONSTITUENTS,
        methodology_version=tracking.METHODOLOGY,
        semantic_config=semantics,
        members=[
            {
                "symbol": row[0],
                "company_name": row[1],
                "industry": row[2],
                "isin": row[3],
                "series": row[4],
            }
            for row in member_rows
        ],
    )
    result = tracking.persist_watchlist_run(
        conn,
        index_name=config["universe_index"],
        source_as_of=cutoff,
        canonical_cutoff=cutoff,
        methodology_version=tracking.METHODOLOGY,
        semantic_config=semantics,
        constituent_snapshot_id=snapshot_id,
        candidates=candidates,
        recorded_at=recorded_at,
    )
    return str(result["run_id"])


def _persist_screens(conn, config: dict, semantics: dict, recorded_at: dt, cutoff) -> None:
    universe = screens.build_universe(
        conn, cutoff=cutoff, universe_source=semantics["screen_universe_source"]
    )
    for screen_id, definition in config["screens"].items():
        results = []
        for symbol, row in universe.items():
            failed = []
            missing = []
            for field, condition in definition["conditions"].items():
                verdict = screens._check(condition, row.get(field))
                if verdict is None:
                    missing.append(field)
                elif not verdict:
                    failed.append(field)
            stale = ["fundamentals"] if row.get("fundamentals_stale") else []
            outcome = (
                "STALE_DATA"
                if stale
                else "MISSING_DATA"
                if missing
                else "PREDICATE_FAIL"
                if failed
                else "PASS"
            )
            results.append(
                {
                    "symbol": symbol,
                    "outcome": outcome,
                    "failed_predicates": failed,
                    "missing_fields": missing,
                    "stale_fields": stale,
                    "evidence_as_of": row.get("as_of"),
                    "source": row.get("source") or "NSE XBRL and bhavcopy",
                    "metrics": {field: row.get(field) for field in definition["conditions"]},
                }
            )
        tracking.persist_screen_evaluation(
            conn,
            screen_id=screen_id,
            source_as_of=cutoff,
            canonical_cutoff=cutoff,
            methodology_version=tracking.METHODOLOGY,
            semantic_config=semantics,
            expected_symbols=set(universe),
            results=results,
            recorded_at=recorded_at,
        )


def run(
    conn,
    *,
    send_alerts: bool = False,
    poster=None,
    signal_out: str | Path = "data/swing-latest.txt",
) -> dict:
    if conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] < 18:
        raise RuntimeError("tracking schema v18 is required")
    swing_config = watchlist.load_config()
    screen_config = screens.load_config()
    semantics = semantic_config(swing_config, screen_config)
    now = dt.now(UTC)
    tracking.register_methodology(conn, tracking.METHODOLOGY, semantics, registered_at=now)
    watchlist_run_id = _persist_watchlist(conn, swing_config, semantics, now)
    cutoff = conn.execute(
        "SELECT canonical_cutoff FROM watchlist_run WHERE run_id=?", [watchlist_run_id]
    ).fetchone()[0]
    _persist_screens(conn, screen_config, semantics, now, cutoff)
    signal_report = signals.run_scan(conn, swing_config, canonical_cutoff=cutoff)
    signal_result = tracking.persist_signal_run(
        conn,
        signal_report,
        watchlist_run_id=watchlist_run_id,
        methodology_version=tracking.METHODOLOGY,
        semantic_config=semantics,
        recorded_at=now,
    )
    # This is the sole nightly producer. The exact report is persisted first,
    # then made available as an artifact, and only then advances its watermark.
    watchlist.atomic_write(signal_out, signals.render(signal_report))
    signals.advance_watermark(conn, signal_report, updated_at=now)
    token, chat_id = alerts.load_credentials()
    queued = 0
    sent = 0
    if chat_id:
        queued = tracking.enqueue_change_alerts(conn, destination=chat_id, recorded_at=now)
    if send_alerts:
        if not token or not chat_id:
            raise ValueError("invest Telegram credentials are not configured")
        pending = conn.execute(
            "SELECT fingerprint FROM research_alert_delivery WHERE status IN "
            "('PENDING','FAILED_BEFORE_SEND') ORDER BY source_at,fingerprint"
        ).fetchall()
        for (alert_fp,) in pending:
            outcome = tracking.dispatch_telegram_alert(
                conn,
                fingerprint_value=alert_fp,
                bot_token=token,
                attempted_at=dt.now(UTC),
                poster=poster,
            )
            sent += outcome == "SENT"
            if outcome not in {"SENT", "NOT_CLAIMED"}:
                raise RuntimeError(f"Telegram tracking alert ended as {outcome}")
    return {
        "watchlist_run_id": watchlist_run_id,
        "signal_status": signal_result["status"],
        "queued": queued,
        "sent": sent,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="invest-tracking-run")
    parser.add_argument("--db", default=str(watchlist.DEFAULT_DB))
    parser.add_argument("--send-alerts", action="store_true")
    parser.add_argument("--signal-out", default="data/swing-latest.txt")
    args = parser.parse_args(argv)
    conn = db.connect(args.db)
    try:
        result = run(conn, send_alerts=args.send_alerts, signal_out=args.signal_out)
        print(f"tracking: {result}")
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"tracking failed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
