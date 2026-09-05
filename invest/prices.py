"""Official NSE bhavcopy price-history ingest (T3.2b).

UDiFF daily files from nsearchives (verified live 2026-08-25):
  https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip
Only equity-series rows (EQ/BE/BZ) are stored; bonds, SGBs and T-bills
(series GB/SG/GS/TB/etc.) are dropped by series, not by instrument type.
A 404 means
non-trading day: interior dates still advance the watermark so holidays never
wedge the backfill, while the newest requested date stays pending so a
not-yet-published evening file is retried on the next run.
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import sys
import zipfile
from datetime import UTC, date, timedelta
from datetime import datetime as dt
from urllib import error, request
from zoneinfo import ZoneInfo

from invest import db, nse_filings

log = logging.getLogger("invest.prices")

PROJECT_ROOT = nse_filings.PROJECT_ROOT
DEFAULT_DB = nse_filings.DEFAULT_DB
URL_TEMPLATE = (
    "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{stamp}_F_0000.csv.zip"
)
SOURCE = "nse_bhavcopy_udiff"
MAX_ZIP_BYTES = 20 * 1024 * 1024
STORE_SERIES = {"EQ", "BE", "BZ"}
WATERMARK_KIND = "bhavcopy_daily"
IST = ZoneInfo("Asia/Kolkata")


class SourceError(RuntimeError):
    """Bhavcopy transport/contract failure safe to expose in logs."""


class NonTradingDay(Exception):
    """HTTP 404 for a requested date: no session that day."""


def bhavcopy_url(day: date) -> str:
    return URL_TEMPLATE.format(stamp=day.strftime("%Y%m%d"))


def fetch_bhavcopy(day: date, *, opener=None) -> bytes:
    opener = opener or request.build_opener()
    req = request.Request(
        bhavcopy_url(day),
        headers={"User-Agent": nse_filings.USER_AGENT, "Referer": "https://www.nseindia.com/"},
    )
    try:
        with opener.open(req, timeout=30) as response:
            body = response.read(MAX_ZIP_BYTES + 1)
    except error.HTTPError as exc:
        exc.close()
        if exc.code == 404:
            raise NonTradingDay(str(day)) from exc
        raise SourceError(f"bhavcopy HTTP {exc.code} for {day}") from exc
    except (error.URLError, TimeoutError) as exc:
        raise SourceError(f"bhavcopy fetch failed for {day}: {type(exc).__name__}") from exc
    if len(body) > MAX_ZIP_BYTES:
        raise SourceError(f"bhavcopy zip exceeds {MAX_ZIP_BYTES} bytes for {day}")
    if not zipfile.is_zipfile(io.BytesIO(body)):
        raise SourceError(f"bhavcopy payload is not a zip for {day}")
    return body


def parse_bhavcopy(zip_bytes: bytes) -> list[dict]:
    """Extract STK rows into normalized price bars."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as bundle:
        members = [n for n in bundle.namelist() if n.lower().endswith(".csv")]
        if len(members) != 1:
            raise SourceError("bhavcopy zip contract changed: expected one CSV member")
        info = bundle.getinfo(members[0])
        if info.file_size > nse_filings.MAX_JSON_BYTES * 2:  # 10 MiB decompressed cap
            raise SourceError("bhavcopy CSV member exceeds decompression limit")
        text = bundle.read(members[0]).decode("utf-8-sig")
    rows: list[dict] = []
    for raw in csv.DictReader(io.StringIO(text)):
        if (raw.get("SctySrs") or "").strip().upper() not in STORE_SERIES:
            continue
        try:
            trade_date = dt.strptime(raw["TradDt"].strip(), "%Y-%m-%d").date()
            symbol = nse_filings.valid_symbol(raw.get("TckrSymb", ""))
            bar = {
                "symbol": symbol,
                "trade_date": trade_date,
                "open": float(raw["OpnPric"]),
                "high": float(raw["HghPric"]),
                "low": float(raw["LwPric"]),
                "close": float(raw["ClsPric"]),
                "prev_close": float(raw["PrvsClsgPric"]) if raw.get("PrvsClsgPric") else None,
                "volume": int(float(raw["TtlTradgVol"])) if raw.get("TtlTradgVol") else None,
            }
        except (KeyError, ValueError) as exc:
            raise SourceError(f"bhavcopy row contract changed: {type(exc).__name__}") from exc
        rows.append(bar)
    return rows


def _advance_watermark(conn, day: date, detail: str, updated_at: dt) -> None:
    """Watermark only ever moves forward."""
    current = db.get_watermark(conn, WATERMARK_KIND)
    if current is None or day >= current:
        db.set_watermark(conn, WATERMARK_KIND, day, detail=detail, updated_at=updated_at)


def ingest_day(conn, day: date, *, fetched_at: dt | None = None, opener=None, is_tail=False) -> int:
    """Fetch+store one day; returns bars stored (0 on non-trading days)."""
    fetched_at = fetched_at or dt.now(UTC)
    try:
        bars = parse_bhavcopy(fetch_bhavcopy(day, opener=opener))
    except NonTradingDay:
        if is_tail:
            log.info("%s non-trading or not yet published; left pending", day)
        else:
            _advance_watermark(conn, day, "non-trading", fetched_at)
        return 0
    if not bars:
        # A published session always contains EQ trades; zero rows means the
        # series taxonomy drifted and silently skipping the day would lose it.
        raise SourceError(f"bhavcopy {day} parsed to zero equity-series rows")
    db.upsert_prices(conn, bars, source=SOURCE, fetched_at=fetched_at)
    _advance_watermark(conn, day, f"bars={len(bars)}", fetched_at)
    return len(bars)


def backfill(conn, start: date, end: date, *, fetched_at: dt | None = None, opener=None) -> int:
    """Ingest [start, end] calendar days; returns total bars stored."""
    if start > end:
        raise ValueError("start after end")
    total = 0
    day = start
    while day <= end:
        if day.weekday() < 5:  # fast path: weekends are never sessions
            total += ingest_day(
                conn, day, fetched_at=fetched_at, opener=opener, is_tail=(day == end)
            )
        else:
            _advance_watermark(conn, day, "weekend", fetched_at)
        day += timedelta(days=1)
    return total


def default_range(conn) -> tuple[date, date]:
    """Resume from watermark+1 (or 30 days back) through yesterday IST."""
    today_ist = dt.now(IST).date()
    end = today_ist - timedelta(days=1)
    watermark = db.get_watermark(conn, WATERMARK_KIND)
    if watermark is None:
        start = end - timedelta(days=29)
    else:
        start = min(watermark + timedelta(days=1), end)
    return start, end


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(prog="invest-prices")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--from", dest="start", type=date.fromisoformat)
    parser.add_argument("--to", dest="end", type=date.fromisoformat)
    args = parser.parse_args(argv)
    try:
        conn = db.connect(args.db)
        try:
            db.init_schema(conn)
            start, end = default_range(conn)
            if args.start:
                start = args.start
            if args.end:
                end = args.end
            total = backfill(conn, start, end)
            watermark = db.get_watermark(conn, WATERMARK_KIND)
            (bars,) = conn.execute("SELECT COUNT(*) FROM stock_price").fetchone()
        finally:
            conn.close()
    except (SourceError, ValueError) as exc:
        log.error("%s", exc)
        return 1
    print(
        f"prices: range={start}..{end} bars_added={total} total_bars={bars} watermark={watermark}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
