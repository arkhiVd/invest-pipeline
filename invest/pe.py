"""Nifty PE fetcher + store (T2.2). Source verified in
docs/spikes/t2.1-nifty-pe-source.md: NSE /api/AllIndices.

Behavior: warmup GET to nseindia.com to obtain cookies (the warmup itself 403s
by design; the jar it sets unlocks the API), then GET /api/AllIndices with a
browser-like UA. PE/PB/DY arrive as strings and are normalized here so string
formats never reach the DB. One row per day (PK nav_date, latest wins).
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import date as date_cls
from datetime import datetime as dt
from urllib import request as urlreq

from invest import db

log = logging.getLogger("invest.pe")

BASE = "https://www.nseindia.com"
API = f"{BASE}/api/AllIndices"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) invest-pipeline/0.1 (homelab; personal)"
SOURCE = "nse_allindices"


def _get(url: str, opener, timeout: int = 30) -> bytes:
    req = urlreq.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json", "Referer": f"{BASE}/"}
    )
    with opener.open(req, timeout=timeout) as resp:
        return resp.read()


def fetch_nifty_valuations(fetcher=None) -> dict:
    """Return {'pe': float|None, 'pb': ..., 'dy': ..., 'close': ...} for NIFTY 50."""
    if fetcher is not None:  # injectable for offline tests
        payload = json.loads(fetcher())
    else:
        from http.cookiejar import CookieJar

        opener = urlreq.build_opener(urlreq.HTTPCookieProcessor(CookieJar()))
        try:  # warmup always 403s on /; its cookie jar is what matters (spike §B)
            _get(BASE, opener)
        except Exception as exc:  # noqa: BLE001 — warmup failure is non-fatal
            log.warning("warmup fetch failed (%s); continuing", exc)
        payload = json.loads(_get(API, opener))

    row = next((r for r in payload.get("data", []) if r.get("index") == "NIFTY 50"), None)
    if row is None:
        msg = "NIFTY 50 row missing from AllIndices payload"
        raise ValueError(msg)

    def num(key):
        v = row.get(key)
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    return {"close": num("last"), "pe": num("pe"), "pb": num("pb"), "dy": num("dy")}


def store(conn, vals: dict, *, day: date_cls | None = None) -> None:
    """Upsert today's valuation row (idempotent; same-day refresh overwrites)."""
    day = day or date_cls.today()
    conn.execute(
        """
        INSERT INTO nifty_pe (nav_date, pe, pb, dy, close, source, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (nav_date) DO UPDATE SET
            pe = excluded.pe, pb = excluded.pb, dy = excluded.dy,
            close = excluded.close, source = excluded.source,
            fetched_at = excluded.fetched_at
        """,
        [day, vals.get("pe"), vals.get("pb"), vals.get("dy"), vals.get("close"), SOURCE, dt.now()],
    )


def latest(conn) -> tuple | None:
    return conn.execute(
        "SELECT nav_date, pe, pb, dy, close FROM nifty_pe ORDER BY nav_date DESC LIMIT 1"
    ).fetchone()


def main(argv: list[str] | None = None) -> int:
    import argparse

    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser(prog="invest-pe")
    parser.add_argument("--db", default="data/invest.duckdb")
    args = parser.parse_args(argv)

    conn = db.connect(args.db)
    db.init_schema(conn)
    vals = fetch_nifty_valuations()
    if vals.get("pe") is None:
        log.error("NIFTY 50 PE missing in payload; storing nothing")
        return 1
    store(conn, vals)
    log.info("stored %s: %s", date_cls.today(), vals)
    print(
        f"NIFTY 50 {date_cls.today()}: PE={vals['pe']} PB={vals['pb']} "
        f"DY={vals['dy']} last={vals['close']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
