"""Controlled NSE XBRL crawl across the EQ universe (T3.2b-ii).

Bounded by design: a run processes at most --limit pending symbols, sleeps
between archive downloads (throttle + jitter), fails soft per symbol, and
resumes later because a symbol is "done" once it has any stock_filing rows.
Symbols that publish nothing retainable are tombstoned (stock_crawl_skip) so
they cannot wedge the queue.
Selection keeps only what historical metrics need: newest 8 legacy annuals
(consolidated preferred), all integrated Q4-consolidated filings plus the
newest integrated quarter, and the 2 newest shareholding filings.
"""

from __future__ import annotations

import argparse
import logging
import random
import sys
import time
from collections.abc import Callable
from datetime import UTC, date
from datetime import datetime as dt

from invest import db, nse_filings
from invest.nse_filings import FilingRef

log = logging.getLogger("invest.xbrl_crawl")

DEFAULT_DB = nse_filings.DEFAULT_DB
LEGACY_KEEP = 8
SHAREHOLDING_KEEP = 2
THROTTLE_RANGE = (0.35, 0.7)
MAX_CONSECUTIVE_FAILURES = 10
RETRYABLE_SKIP_DAYS = 30


def select_filings(refs: list[FilingRef]) -> list[FilingRef]:
    """Apply the retention policy; deterministic order, deduped by URL."""
    epoch = date(1, 1, 1)  # undated filings sort as oldest, never as newest

    def newest(rows: list[FilingRef], count: int) -> list[FilingRef]:
        return sorted(
            rows,
            key=lambda r: (r.period_end or epoch, r.xbrl_url),
            reverse=True,
        )[:count]

    legacy = [r for r in refs if r.filing_type == "financial_annual_legacy"]
    consolidated = [r for r in legacy if (r.consolidation or "").strip().lower() == "consolidated"]
    legacy_keep = newest(consolidated or legacy, LEGACY_KEEP)

    integrated = [r for r in refs if r.filing_type == "financial_integrated"]
    int_cons = [r for r in integrated if (r.consolidation or "").strip().lower() == "consolidated"]
    q4 = [r for r in int_cons if r.period_end and r.period_end.month == 3]
    latest_quarter = newest(int_cons, 1)
    integrated_keep = list({r.xbrl_url: r for r in q4 + latest_quarter}.values())

    shareholding = [r for r in refs if r.filing_type == "shareholding"]
    share_keep = newest(shareholding, SHAREHOLDING_KEEP)
    chosen: dict[str, FilingRef] = {}
    for ref in sorted(
        legacy_keep + integrated_keep + share_keep,
        key=lambda r: (r.filing_type, r.period_end is None, r.period_end, r.xbrl_url),
        reverse=False,
    ):
        chosen[ref.xbrl_url] = ref
    return list(chosen.values())


def pending_symbols(conn, limit: int) -> list[str]:
    """EQ symbols with no stored filing and no tombstone, oldest-listed first."""
    done = {row[0] for row in conn.execute("SELECT DISTINCT symbol FROM stock_filing").fetchall()}
    skipped = {
        row[0]
        for row in conn.execute(
            """
            SELECT symbol FROM stock_crawl_skip
            WHERE reason != 'all_selected_xbrl_404'
               OR checked_at > current_timestamp - (? * INTERVAL 1 DAY)
            """,
            [RETRYABLE_SKIP_DAYS],
        ).fetchall()
    }
    universe = conn.execute(
        "SELECT symbol FROM stock_universe WHERE series = 'EQ' "
        "ORDER BY listing_date NULLS LAST, symbol"
    ).fetchall()
    queue = [row[0] for row in universe if row[0] not in done and row[0] not in skipped]
    return queue[:limit]


def ingest_symbol(
    conn,
    symbol: str,
    *,
    opener=None,
    sleep: Callable[[float], None] = time.sleep,
    keep_raw: bool = False,
    fetched_at=None,
) -> dict:
    """Discover, select, download, and parse one symbol's retained filings."""
    if opener is None:
        opener = nse_filings._opener()

    def get(path, params):
        return nse_filings._get_json(opener, path, params)

    refs = nse_filings.discover(symbol, fetcher=get)
    chosen = select_filings(refs)
    if not chosen:
        # Permanent zero: every discovered ref was a placeholder or the
        # company publishes nothing retainable. Tombstone so the queue
        # cannot wedge here; manual DELETE re-queues for a later retry.
        db.upsert_crawl_skip(
            conn,
            symbol=symbol,
            reason="no_retained_refs",
            checked_at=fetched_at or dt.now(UTC),
        )
        log.info("%s: tombstoned (no retained filings after selection)", symbol)
        return {
            "filings": 0,
            "downloads": 0,
            "contexts": 0,
            "facts": 0,
            "skipped": True,
        }
    contexts_total = facts_total = downloads = not_found = 0
    for ref in chosen:
        try:
            xml = nse_filings.fetch_xbrl(ref.xbrl_url, opener=opener)
            summary = nse_filings.ingest_filing(
                conn, ref, xml, fetched_at=fetched_at, keep_raw=keep_raw
            )
        except nse_filings.SourceError as exc:
            # NSE discovery can advertise old archive URLs that permanently
            # return 404. Skip only that immutable ref; auth/rate/transport
            # failures still abort the symbol and feed the circuit breaker.
            if getattr(exc.__cause__, "code", None) != 404:
                raise
            not_found += 1
            log.warning(
                "%s: skipping archived 404 %s",
                symbol,
                ref.xbrl_url.rsplit("/", 1)[-1],
            )
            continue
        except ValueError as exc:  # single malformed ref must not sink the symbol
            log.warning("%s: skipping %s (%s)", symbol, ref.xbrl_url.rsplit("/", 1)[-1], exc)
            continue
        contexts_total += summary["contexts"]
        facts_total += summary["facts"]
        downloads += 1
        sleep(random.uniform(*THROTTLE_RANGE))
    if downloads > 0:
        conn.execute("DELETE FROM stock_crawl_skip WHERE symbol = ?", [symbol])
    elif not_found == len(chosen):
        db.upsert_crawl_skip(
            conn,
            symbol=symbol,
            reason="all_selected_xbrl_404",
            checked_at=fetched_at or dt.now(UTC),
        )
        log.info("%s: tombstoned (all selected XBRLs returned 404)", symbol)
    return {
        "filings": downloads,
        "downloads": downloads,
        "contexts": contexts_total,
        "facts": facts_total,
        "skipped": downloads == 0,
    }


def crawl(
    conn,
    symbols: list[str],
    *,
    opener=None,
    sleep: Callable[[float], None] = time.sleep,
    keep_raw: bool = False,
    fetched_at=None,
) -> dict:
    """Run the bounded crawl; returns aggregate stats with failed symbols."""
    ok = 0
    failed: list[str] = []
    totals = {"filings": 0, "contexts": 0, "facts": 0}
    skipped = 0
    consecutive_failures = 0
    aborted = False
    for index, symbol in enumerate(symbols, start=1):
        try:
            stats = ingest_symbol(
                conn,
                symbol,
                opener=opener,
                sleep=sleep,
                keep_raw=keep_raw,
                fetched_at=fetched_at,
            )
        except Exception as exc:  # noqa: BLE001 - one bad symbol must not stop the batch
            log.warning("symbol %s failed (%s: %s)", symbol, type(exc).__name__, exc)
            failed.append(symbol)
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                log.error(
                    "crawl circuit opened after %d consecutive symbol failures; "
                    "%d symbols remain pending",
                    consecutive_failures,
                    len(symbols) - index,
                )
                aborted = True
                break
            continue
        consecutive_failures = 0
        ok += 1
        skipped += bool(stats.pop("skipped"))
        for key in totals:
            totals[key] += stats[key]
        log.info(
            "[%d/%d] %s filings=%d contexts=%d facts=%d",
            index,
            len(symbols),
            symbol,
            stats["filings"],
            stats["contexts"],
            stats["facts"],
        )
        sleep(random.uniform(*THROTTLE_RANGE))
    return {
        "symbols_ok": ok,
        "failed": failed,
        "skipped": skipped,
        "aborted": aborted,
        **totals,
    }


def recycle_taxonomy(conn, taxonomy: str) -> int:
    """Delete stored financial filings of a taxonomy so their symbols re-enter
    the pending queue and re-ingest under the current fact-set. Shareholding
    rows (taxonomy 'shp') are untouched. Returns deleted filing count."""
    urls = [
        row[0]
        for row in conn.execute(
            "SELECT xbrl_url FROM stock_filing WHERE taxonomy = ? "
            "AND filing_type != 'shareholding'",
            [taxonomy],
        ).fetchall()
    ]
    if not urls:
        return 0
    symbols = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT symbol FROM stock_filing WHERE taxonomy = ?", [taxonomy]
        ).fetchall()
    }
    conn.executemany("DELETE FROM stock_filing_fact WHERE xbrl_url = ?", [(u,) for u in urls])
    conn.executemany("DELETE FROM stock_filing_context WHERE xbrl_url = ?", [(u,) for u in urls])
    conn.executemany("DELETE FROM stock_filing WHERE xbrl_url = ?", [(u,) for u in urls])
    # A tombstone would block the re-crawl; permanent-zero logic does not
    # apply to symbols we know have filings.
    conn.executemany("DELETE FROM stock_crawl_skip WHERE symbol = ?", [(s,) for s in symbols])
    log.info("recycled %d %s filing(s) across %d symbol(s)", len(urls), taxonomy, len(symbols))
    return len(urls)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(prog="invest-xbrl-crawl")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--symbols", help="comma-separated override of the pending queue")
    parser.add_argument("--keep-raw", action="store_true", help="also retain raw XBRL files")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit nonzero on any per-symbol failure (full rebuild mode)",
    )
    parser.add_argument(
        "--recycle-taxonomy",
        help="delete stored financial filings of this taxonomy (e.g. banking) "
        "so affected symbols re-ingest under the current fact-set, then exit",
    )
    args = parser.parse_args(argv)

    conn = db.connect(args.db)
    try:
        db.init_schema(conn)
        if args.recycle_taxonomy:
            removed = recycle_taxonomy(conn, args.recycle_taxonomy)
            print(f"crawl: recycled {removed} {args.recycle_taxonomy} filing(s)")
            return 0
        if args.symbols:
            symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        else:
            symbols = pending_symbols(conn, max(0, args.limit))
        if not symbols:
            print("crawl: queue empty; nothing to do")
            return 0
        log.info("crawling %d symbol(s)", len(symbols))
        stats = crawl(conn, symbols, keep_raw=args.keep_raw)
        (manifest,) = conn.execute("SELECT COUNT(*) FROM stock_filing").fetchone()
        (facts,) = conn.execute("SELECT COUNT(*) FROM stock_filing_fact").fetchone()
        (tombstones,) = conn.execute("SELECT COUNT(*) FROM stock_crawl_skip").fetchone()
    finally:
        conn.close()
    print(
        f"crawl: requested={len(symbols)} ok={stats['symbols_ok']} "
        f"failed={len(stats['failed'])} {stats['failed'][:10]} | "
        f"filings={stats['filings']} contexts={stats['contexts']} facts={stats['facts']} | "
        f"totals: manifest={manifest} facts={facts} tombstones={tombstones}"
    )
    # Small per-symbol failure sets remain resumable. A broadly failing run
    # must be nonzero so a rebuild orchestrator cannot print false success.
    failure_limit = 0 if args.strict else max(10, len(symbols) // 4)
    return 1 if stats["aborted"] or len(stats["failed"]) > failure_limit else 0


if __name__ == "__main__":
    sys.exit(main())
