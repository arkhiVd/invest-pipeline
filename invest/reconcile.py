"""Deterministic all-EQ NSE filing reconciliation with persisted evidence."""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import time
from datetime import UTC, timedelta
from datetime import datetime as dt

from invest import db, nse_filings, xbrl_crawl

log = logging.getLogger("invest.reconcile")
POLICY_VERSION = "stock-crawl-policy-2026.2"
DEFAULT_DB = nse_filings.DEFAULT_DB
MAX_CONSECUTIVE_FAILURES = 10
RECHECK_404_DAYS = 30


def _record_ref(conn, ref, outcome: str, detail: str | None, checked_at: dt) -> None:
    db.upsert_crawl_ref(
        conn,
        symbol=ref.symbol,
        xbrl_url=ref.xbrl_url,
        filing_type=ref.filing_type,
        period_end=ref.period_end,
        outcome=outcome,
        detail=detail,
        policy_version=POLICY_VERSION,
        checked_at=checked_at,
    )


def reconcile_symbol(
    conn,
    symbol: str,
    *,
    discoverer=None,
    fetcher=None,
    ingester=None,
    opener=None,
    checked_at: dt | None = None,
    sleep=lambda _seconds: None,
) -> dict:
    """Reconcile one symbol and persist section/ref-level evidence."""
    symbol = nse_filings.valid_symbol(symbol)
    checked_at = checked_at or dt.now(UTC)
    opener = opener or nse_filings._opener()

    if discoverer is None:

        def get(path, params):
            return nse_filings._get_json(opener, path, params)

        discovery = nse_filings.discover_with_status(symbol, fetcher=get)
    else:
        discovery = discoverer(symbol)
    fetcher = fetcher or (lambda ref: nse_filings.fetch_xbrl(ref.xbrl_url, opener=opener))
    ingester = ingester or (
        lambda ref, xml: nse_filings.ingest_filing(
            conn, ref, xml, fetched_at=checked_at, keep_raw=False
        )
    )

    chosen = xbrl_crawl.select_filings(list(discovery.refs))
    existing = {
        row[0]
        for row in conn.execute(
            "SELECT xbrl_url FROM stock_filing WHERE symbol = ?", [symbol]
        ).fetchall()
    }
    evidence = {
        row[0]: (row[1], row[2])
        for row in conn.execute(
            "SELECT xbrl_url, outcome, checked_at FROM stock_crawl_ref "
            "WHERE symbol = ? AND policy_version = ?",
            [symbol, POLICY_VERSION],
        ).fetchall()
    }
    stored: set[str] = set()
    not_found: set[str] = set()
    errors: list[str] = []

    for ref in chosen:
        # Existing filing rows predate the reconciliation policy and may have
        # narrow facts/broken contexts. Trust them only after this policy has
        # explicitly refreshed and recorded the URL.
        prior = evidence.get(ref.xbrl_url)
        prior_outcome = prior[0] if prior else None
        prior_checked = prior[1] if prior else None
        if ref.xbrl_url in existing and prior_outcome == "stored":
            stored.add(ref.xbrl_url)
            continue
        threshold = checked_at.replace(tzinfo=None) - timedelta(days=RECHECK_404_DAYS)
        if prior_outcome == "http_404" and prior_checked is not None and prior_checked > threshold:
            not_found.add(ref.xbrl_url)
            continue
        try:
            xml = fetcher(ref)
            summary = ingester(ref, xml)
            if ref.filing_type.startswith("financial_") and (
                not isinstance(summary, dict) or summary.get("facts", 0) <= 0
            ):
                raise ValueError("zero parsed financial facts")
        except nse_filings.SourceError as exc:
            status = getattr(exc.__cause__, "code", None)
            if status == 404:
                not_found.add(ref.xbrl_url)
                epoch = checked_at.date().toordinal() // RECHECK_404_DAYS
                _record_ref(conn, ref, "http_404", f"HTTP 404 epoch={epoch}", checked_at)
                continue
            detail = f"{type(exc).__name__}:HTTP {status}" if status else type(exc).__name__
            errors.append(detail)
            _record_ref(conn, ref, "error", detail, checked_at)
            break
        except (ValueError, OSError) as exc:
            detail = type(exc).__name__
            errors.append(detail)
            _record_ref(conn, ref, "error", detail, checked_at)
            break
        stored.add(ref.xbrl_url)
        _record_ref(conn, ref, "stored", None, checked_at)
        sleep(random.uniform(*xbrl_crawl.THROTTLE_RANGE))

    sections_ok = discovery.legacy_ok and discovery.integrated_ok and discovery.shareholding_ok
    complete = sections_ok and not errors and len(stored) + len(not_found) == len(chosen)
    financial_selected = sum(ref.filing_type.startswith("financial_") for ref in chosen)
    financial_stored = sum(
        ref.filing_type.startswith("financial_") and ref.xbrl_url in stored for ref in chosen
    )
    detail = json.dumps(
        {
            "discovery_errors": list(discovery.errors),
            "reconcile_errors": errors,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    db.upsert_crawl_status(
        conn,
        symbol=symbol,
        policy_version=POLICY_VERSION,
        legacy_ok=discovery.legacy_ok,
        integrated_ok=discovery.integrated_ok,
        shareholding_ok=discovery.shareholding_ok,
        legacy_refs=discovery.legacy_refs,
        integrated_refs=discovery.integrated_refs,
        shareholding_refs=discovery.shareholding_refs,
        selected_refs=len(chosen),
        stored_refs=len(stored),
        not_found_refs=len(not_found),
        financial_selected=financial_selected,
        financial_stored=financial_stored,
        complete=complete,
        usable_financial=financial_stored > 0,
        detail=detail,
        checked_at=checked_at,
    )
    return {
        "symbol": symbol,
        "complete": complete,
        "usable_financial": financial_stored > 0,
        "selected": len(chosen),
        "stored": len(stored),
        "not_found": len(not_found),
        "errors": errors,
        "section_errors": list(discovery.errors),
    }


def pending_symbols(conn, limit: int, *, all_symbols: bool = False) -> list[str]:
    """All EQ for audit, or symbols lacking current complete+usable evidence."""
    if all_symbols:
        return [
            row[0]
            for row in conn.execute(
                "SELECT symbol FROM stock_universe WHERE series='EQ' "
                "AND COALESCE(is_active, TRUE) ORDER BY symbol"
            ).fetchall()
        ][:limit]
    return [
        row[0]
        for row in conn.execute(
            """
            SELECT u.symbol FROM stock_universe u
            LEFT JOIN stock_crawl_status s ON s.symbol=u.symbol
            WHERE u.series='EQ' AND COALESCE(u.is_active, TRUE)
              AND (s.symbol IS NULL OR s.policy_version != ?
                   OR NOT s.complete OR NOT s.usable_financial)
            ORDER BY u.symbol
            LIMIT ?
            """,
            [POLICY_VERSION, limit],
        ).fetchall()
    ]


def run(conn, symbols: list[str], **kwargs) -> dict:
    complete = usable = failed = consecutive_failures = 0
    aborted = False
    results = []
    for index, symbol in enumerate(symbols, 1):
        result = reconcile_symbol(conn, symbol, **kwargs)
        results.append(result)
        complete += result["complete"]
        usable += result["usable_financial"]
        failed += not result["complete"]
        if result["errors"] or result["section_errors"]:
            consecutive_failures += 1
        else:
            consecutive_failures = 0
        log.info(
            "[%d/%d] %s complete=%s usable=%s selected=%d stored=%d 404=%d",
            index,
            len(symbols),
            symbol,
            result["complete"],
            result["usable_financial"],
            result["selected"],
            result["stored"],
            result["not_found"],
        )
        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            log.error(
                "reconciliation circuit opened after %d consecutive failures; %d symbols remain",
                consecutive_failures,
                len(symbols) - index,
            )
            aborted = True
            break
    return {
        "requested": len(symbols),
        "processed": len(results),
        "complete": complete,
        "usable": usable,
        "failed": failed,
        "aborted": aborted,
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(prog="invest-reconcile")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--all", action="store_true", help="audit every active EQ symbol")
    parser.add_argument("--symbols", help="comma-separated override")
    parser.add_argument("--strict", action="store_true", help="nonzero if any symbol is incomplete")
    args = parser.parse_args(argv)
    conn = db.connect(args.db)
    try:
        db.init_schema(conn)
        if args.symbols:
            symbols = [nse_filings.valid_symbol(s) for s in args.symbols.split(",") if s.strip()]
        else:
            symbols = pending_symbols(conn, max(0, args.limit), all_symbols=args.all)
        stats = run(conn, symbols, sleep=time.sleep)
    finally:
        conn.close()
    print(
        f"reconcile: requested={stats['requested']} processed={stats['processed']} "
        f"complete={stats['complete']} usable={stats['usable']} "
        f"failed={stats['failed']} aborted={stats['aborted']} policy={POLICY_VERSION}"
    )
    return 1 if args.strict and (stats["failed"] or stats["aborted"]) else 0


if __name__ == "__main__":
    sys.exit(main())
