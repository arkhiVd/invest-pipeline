"""BharatStock free-API adapter (T3.2a candidate accelerator/cross-check).

The official NSE filing/XBRL layer remains canonical. BharatStock is used to
apply broad current-metric filters before expensive historical enrichment.
Free contract verified 2026-08-25: 100 requests/day, page size <= 200.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import UTC, date
from datetime import datetime as dt
from pathlib import Path
from urllib import error, parse, request

from invest import db

log = logging.getLogger("invest.bharatstock")

BASE = "https://bharatstockapi.com"
SOURCE = "bharatstock_v1"
METHODOLOGY = "stock-source-2026.2-audit"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = PROJECT_ROOT / "data/invest.duckdb"
ENV_PATH = Path(os.environ.get("INVEST_ENV", PROJECT_ROOT / "invest.env"))
MAX_JSON_BYTES = 5 * 1024 * 1024

# Broad first-stage GARP gate. Historical growth/FCF and exact current
# profitability are deliberately left to the canonical local engine.
DEFAULT_FILTERS = ["market_cap.gt.1000", "pe_ratio.gt.7", "pe_ratio.lt.25"]
DEFAULT_MAX_PAGES = 5  # protects the 100/day free quota from an accidental broad crawl

SNAPSHOT_FIELDS = (
    "company_name",
    "sector",
    "exchange",
    "price",
    "high_52w",
    "distance_from_52w_high_pct",
    "price_to_50dma",
    "price_to_200dma",
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
    "current_ratio",
    "interest_coverage",
    "free_cash_flow",
    "eps",
    "promoter_holding",
    "fii_holding",
    "dii_holding",
)

# BharatStock expresses percentages as percentage points (TCS ROE=45.59,
# dividend_yield=2.83), while the canonical DB stores decimal fractions.
_PERCENT_POINT_FIELDS = {
    "distance_from_52w_high_pct",
    "roe",
    "roce",
    "dividend_yield",
    "operating_margin",
    "revenue_growth_yoy",
    "profit_growth_yoy",
    "eps_growth_yoy",
    "promoter_holding",
    "fii_holding",
    "dii_holding",
}


class SourceError(RuntimeError):
    """Remote contract/auth/transport failure safe to expose in logs."""


class _NoRedirect(request.HTTPRedirectHandler):
    """Never forward the API-key header to a redirect target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: PLR0913
        return None


def load_api_key(path: Path | None = None) -> str | None:
    """Load the key from process env or the protected invest.env file."""
    value = os.environ.get("BHARATSTOCKAPI")
    path = path or ENV_PATH
    if value:
        return value
    if not path.exists():
        return None
    if path.stat().st_mode & 0o077:
        log.warning("credential file permissions are broader than 0600: %s", path)
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, candidate = line.partition("=")
        if key.strip() == "BHARATSTOCKAPI":
            return candidate.strip().strip('"').strip("'") or None
    return None


def _request_json(
    path: str,
    params: dict | None = None,
    *,
    api_key: str | None = None,
    opener=None,
):
    key = api_key or load_api_key()
    if not key:
        raise SourceError("BHARATSTOCKAPI is not configured")
    query = parse.urlencode(params or {}, doseq=True)
    url = f"{BASE}{path}" + (f"?{query}" if query else "")
    req = request.Request(
        url,
        headers={
            "X-API-Key": key,
            "Accept": "application/json",
            "User-Agent": "invest-pipeline/0.1",
        },
    )
    opener = opener or request.build_opener(_NoRedirect())
    try:
        with opener.open(req, timeout=30) as response:
            body = response.read(MAX_JSON_BYTES + 1)
            if len(body) > MAX_JSON_BYTES:
                raise SourceError(f"BharatStock response too large for {path}")
            return json.loads(body)
    except error.HTTPError as exc:
        # Header credentials are not present in exc.url or this message.
        exc.close()
        raise SourceError(f"BharatStock HTTP {exc.code} for {path}") from exc
    except (error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise SourceError(f"BharatStock request failed for {path}: {type(exc).__name__}") from exc


def screen(
    filters: list[str],
    *,
    page: int = 1,
    page_size: int = 200,
    exchange: str | None = None,
    fetcher=None,
) -> tuple[list[dict], dict]:
    """Run one server-side AND screen and validate its pagination envelope."""
    if not 1 <= page_size <= 200:
        raise ValueError("page_size must be between 1 and 200")
    params = {"filter": filters, "page": page, "page_size": page_size}
    if exchange:
        params["exchange"] = exchange
    payload = fetcher(params) if fetcher else _request_json("/v1/screener", params)
    rows = payload.get("data") if isinstance(payload, dict) else None
    pagination = payload.get("pagination") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not isinstance(pagination, dict):
        raise SourceError("BharatStock screener contract changed: data/pagination missing")
    return rows, pagination


def screen_all(
    filters: list[str],
    *,
    page_size: int = 200,
    max_pages: int = DEFAULT_MAX_PAGES,
    exchange: str | None = None,
    fetcher=None,
) -> tuple[list[dict], dict]:
    """Fetch every result page, refusing a run that would exceed its page budget."""
    rows, pagination = screen(
        filters, page=1, page_size=page_size, exchange=exchange, fetcher=fetcher
    )
    try:
        total_pages = int(pagination["total_pages"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SourceError("BharatStock pagination missing valid total_pages") from exc
    if total_pages > max_pages:
        raise SourceError(
            f"BharatStock screen needs {total_pages} pages; max_pages={max_pages} quota guard"
        )
    for page in range(2, total_pages + 1):
        more, page_meta = screen(
            filters, page=page, page_size=page_size, exchange=exchange, fetcher=fetcher
        )
        try:
            returned_page = int(page_meta["page"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SourceError("BharatStock pagination missing valid page") from exc
        if returned_page != page:
            raise SourceError("BharatStock screener pagination contract changed")
        rows.extend(more)
    try:
        expected = int(pagination["total_items"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SourceError("BharatStock pagination missing valid total_items") from exc
    if len(rows) != expected:
        raise SourceError(f"BharatStock screen incomplete: expected {expected}, got {len(rows)}")
    return rows, pagination


def snapshot_row(item: dict, *, fetched_at: dt | None = None) -> dict:
    """Normalize one screener result into schema-v4 column names/units."""
    symbol = str(item.get("symbol") or "").strip().upper()
    if not symbol:
        raise SourceError("BharatStock screener row missing symbol")
    calculated = item.get("computed_at")
    try:
        as_of = date.fromisoformat(str(calculated)[:10]) if calculated else dt.now(UTC).date()
    except ValueError as exc:
        raise SourceError(f"invalid computed_at for {symbol}") from exc

    row = {
        "symbol": symbol,
        "as_of": as_of,
        "source": SOURCE,
        "market_cap_cr": item.get("market_cap"),
        "raw_json": json.dumps(item, sort_keys=True, separators=(",", ":")),
        "methodology_version": METHODOLOGY,
        "fetched_at": fetched_at or dt.now(UTC),
    }
    row.update({name: item.get(name) for name in SNAPSHOT_FIELDS})
    for name in _PERCENT_POINT_FIELDS:
        value = row.get(name)
        if value is not None:
            row[name] = float(value) / 100.0
    return row


def store_screen(conn, rows: list[dict], *, fetched_at: dt | None = None) -> int:
    """Idempotently store normalized snapshots; return number of source rows."""
    keys = set()
    for item in rows:
        row = snapshot_row(item, fetched_at=fetched_at)
        db.upsert_stock_fundamental(conn, **row)
        keys.add((row["symbol"], row["as_of"], row["source"]))
    return len(keys)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(prog="invest-bharatstock")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--filter", action="append", dest="filters")
    parser.add_argument(
        "--all-nse",
        action="store_true",
        help="fetch all NSE snapshots with no metric filter (explicit quota-bearing mode)",
    )
    parser.add_argument("--page-size", type=int, default=200)
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    args = parser.parse_args(argv)

    if args.all_nse and args.filters:
        parser.error("--all-nse and --filter are mutually exclusive")
    filters = [] if args.all_nse else (args.filters or DEFAULT_FILTERS)
    exchange = "NSE" if args.all_nse else None
    try:
        rows, pagination = screen_all(
            filters,
            page_size=args.page_size,
            max_pages=args.max_pages,
            exchange=exchange,
        )
        conn = db.connect(args.db)
        try:
            db.init_schema(conn)
            stored = store_screen(conn, rows)
        finally:
            conn.close()
    except (SourceError, ValueError) as exc:
        log.error("%s", exc)
        return 1

    print(
        f"BharatStock: stored={stored} total={pagination.get('total_items')} "
        f"page={pagination.get('page')}/{pagination.get('total_pages')}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
