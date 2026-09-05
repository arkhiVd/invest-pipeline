"""Official NSE equity-master universe ingest (T3.2b).

Source: nsearchives EQUITY_L.csv (static archive file, no session warmup).
Every symbol passes the same strict validation as filing discovery so a
crafted master row can never become a filesystem or SQL hazard downstream.
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import sys
from datetime import UTC
from datetime import datetime as dt
from urllib import error, request

from invest import db, nse_filings

log = logging.getLogger("invest.universe")

PROJECT_ROOT = nse_filings.PROJECT_ROOT
DEFAULT_DB = nse_filings.DEFAULT_DB
UNIVERSE_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
SOURCE = "nse_equity_master"
MAX_CSV_BYTES = 5 * 1024 * 1024
USER_AGENT = nse_filings.USER_AGENT


class SourceError(RuntimeError):
    """Universe transport/contract failure safe to expose in logs."""


def fetch_universe_csv(opener=None) -> list[dict]:
    """Fetch and parse the official listed-equity master into clean rows."""
    opener = opener or request.build_opener()
    req = request.Request(
        UNIVERSE_URL,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/csv,*/*",
            "Referer": "https://www.nseindia.com/",
        },
    )
    try:
        with opener.open(req, timeout=30) as response:
            body = response.read(MAX_CSV_BYTES + 1)
    except error.HTTPError as exc:
        exc.close()
        raise SourceError(f"universe fetch HTTP {exc.code}") from exc
    except (error.URLError, TimeoutError) as exc:
        raise SourceError(f"universe fetch failed: {type(exc).__name__}") from exc
    if len(body) > MAX_CSV_BYTES:
        raise SourceError("universe CSV exceeds 5 MiB limit")
    if not body.lstrip().startswith(b"SYMBOL"):
        raise SourceError("universe CSV contract changed: unexpected header")

    reader = csv.DictReader(io.StringIO(body.decode("utf-8-sig")))
    out: list[dict] = []
    for raw in reader:
        cleaned = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        try:
            symbol = nse_filings.valid_symbol(cleaned.get("SYMBOL", ""))
        except ValueError as exc:
            raise SourceError(f"official universe row rejected: {exc}") from exc
        listing = None
        raw_listing = cleaned.get("DATE OF LISTING")
        if raw_listing:
            try:
                listing = dt.strptime(raw_listing.title(), "%d-%b-%Y").date()
            except ValueError:
                listing = None
        face_value = None
        try:
            face_value = float(cleaned["FACE VALUE"]) if cleaned.get("FACE VALUE") else None
        except ValueError:
            face_value = None
        out.append(
            {
                "symbol": symbol,
                "company_name": cleaned.get("NAME OF COMPANY") or None,
                "series": cleaned.get("SERIES") or None,
                "isin": cleaned.get("ISIN NUMBER") or None,
                "listing_date": listing,
                "face_value": face_value,
                "source": SOURCE,
            }
        )
    return out


def store(conn, rows: list[dict], *, fetched_at: dt | None = None) -> int:
    """Idempotently upsert the universe; returns distinct symbols stored."""
    fetched_at = fetched_at or dt.now(UTC)
    seen: set[str] = set()
    for row in rows:
        db.upsert_universe_row(conn, **{**row, "fetched_at": fetched_at})
        seen.add(row["symbol"])
    return len(seen)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(prog="invest-universe")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    args = parser.parse_args(argv)
    try:
        rows = fetch_universe_csv()
        conn = db.connect(args.db)
        try:
            db.init_schema(conn)
            stored = store(conn, rows)
            (equity,) = conn.execute(
                "SELECT COUNT(*) FROM stock_universe WHERE series = 'EQ'"
            ).fetchone()
        finally:
            conn.close()
    except SourceError as exc:
        log.error("%s", exc)
        return 1
    print(f"universe: parsed={len(rows)} stored={stored} eq_series={equity}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
