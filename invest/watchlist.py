"""T4.2 swing watchlist: official NIFTY 100 members, NIFTY 50 beta, price cap.

Sources verified live 2026-08-26:
  https://www.niftyindices.com/IndexConstituent/ind_nifty100list.csv
  https://nsearchives.nseindia.com/content/indices/ind_close_all_DDMMYYYY.csv

Fail-closed rules: missing constituents abort the build; a symbol without a
current close or with fewer than ``min_observations`` aligned daily returns
has no beta and is reported as a gap, never silently ranked. Beta uses simple
daily returns joined on shared trade dates over the trailing window.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import sys
from datetime import UTC, date, timedelta
from datetime import datetime as dt
from math import ceil
from urllib import error, request

from invest import db, nse_filings, prices

log = logging.getLogger("invest.watchlist")

PROJECT_ROOT = nse_filings.PROJECT_ROOT
DEFAULT_DB = nse_filings.DEFAULT_DB
DEFAULT_CONFIG = os.path.join(PROJECT_ROOT, "config", "swing.json")
CONSTITUENT_URL = "https://www.niftyindices.com/IndexConstituent/ind_nifty100list.csv"
INDEX_URL_TEMPLATE = "https://nsearchives.nseindia.com/content/indices/ind_close_all_{stamp}.csv"
SOURCE_CONSTITUENTS = "niftyindices_constituents"
SOURCE_INDEX = "nse_index_close"
WATERMARK_KIND = "index_close_daily"
MAX_CSV_BYTES = 20 * 1024 * 1024
CONFIG_KEYS = {
    "universe_index": str,
    "benchmark": str,
    "window_days": int,
    "min_observations": int,
    "max_price": (int, float),
    "top_n": int,
    "max_price_age_days": int,
    "constituent_min_count": int,
}


class SourceError(RuntimeError):
    """Transport/contract failure safe to expose in logs."""


class NonTradingDay(SourceError):
    """HTTP 404 for a requested date: no session that day (or not yet published)."""


def load_config(path: str | None = None) -> dict:
    with open(path or DEFAULT_CONFIG, encoding="utf-8") as handle:
        config = json.load(handle)
    missing = CONFIG_KEYS.keys() - config.keys()
    if missing:
        raise ValueError(f"swing config missing keys: {sorted(missing)}")
    for key, types in CONFIG_KEYS.items():
        if isinstance(config[key], bool) or not isinstance(config[key], types):
            raise ValueError(f"swing config {key} has wrong type")
    if config["window_days"] < 2:
        raise ValueError("window_days must be >= 2")
    if not 2 <= config["min_observations"] <= config["window_days"]:
        raise ValueError("min_observations must be within [2, window_days]")
    if config["max_price"] <= 0 or config["top_n"] < 1:
        raise ValueError("max_price must be positive and top_n >= 1")
    if config["max_price_age_days"] < 1:
        raise ValueError("max_price_age_days must be >= 1")
    if config["constituent_min_count"] < 1:
        raise ValueError("constituent_min_count must be >= 1")
    if not config["universe_index"] or not config["benchmark"]:
        raise ValueError("index names must be non-empty")
    if config["universe_index"] == config["benchmark"]:
        raise ValueError("universe_index and benchmark must differ")
    return config


def _open_opener(opener=None):
    return opener or request.build_opener()


def fetch_csv(url: str, *, opener=None, referer: str | None = None) -> bytes:
    opener = _open_opener(opener)
    headers = {"User-Agent": nse_filings.USER_AGENT}
    if referer:
        headers["Referer"] = referer
    req = request.Request(url, headers=headers)
    try:
        with opener.open(req, timeout=30) as response:
            body = response.read(MAX_CSV_BYTES + 1)
    except error.HTTPError as exc:
        exc.close()
        if exc.code == 404:
            raise NonTradingDay(url) from exc
        raise SourceError(f"HTTP {exc.code} for {url}") from exc
    except (error.URLError, TimeoutError) as exc:
        raise SourceError(f"fetch failed for {url}: {type(exc).__name__}") from exc
    if len(body) > MAX_CSV_BYTES:
        raise SourceError(f"payload exceeds {MAX_CSV_BYTES} bytes for {url}")
    return body


def parse_constituents(payload: bytes) -> list[dict]:
    reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig")))
    rows: list[dict] = []
    seen: set[str] = set()
    for raw in reader:
        try:
            symbol = nse_filings.valid_symbol(raw["Symbol"])
            row = {
                "company_name": raw["Company Name"].strip(),
                "industry": (raw.get("Industry") or "").strip() or None,
                "symbol": symbol,
                "isin": raw["ISIN Code"].strip(),
                "series": raw["Series"].strip().upper(),
            }
        except (KeyError, ValueError) as exc:
            raise SourceError(f"constituent CSV contract changed: {type(exc).__name__}") from exc
        if row["symbol"] in seen:
            log.warning("duplicate constituent symbol dropped: %s", row["symbol"])
            continue
        seen.add(row["symbol"])
        rows.append(row)
    if not rows:
        raise SourceError("constituent CSV parsed to zero rows")
    return rows


def store_constituents(
    conn,
    index_name: str,
    rows: list[dict],
    *,
    fetched_at: dt,
    minimum_count: int = 1,
) -> dict:
    """Full-membership refresh that rejects partial source snapshots."""
    if len(rows) < minimum_count:
        raise SourceError(
            f"{index_name} constituent snapshot has {len(rows)} rows; minimum is {minimum_count}"
        )
    invalid_series = sorted(row["symbol"] for row in rows if row["series"] != "EQ")
    if invalid_series:
        raise SourceError(
            f"{index_name} constituent snapshot has non-EQ series: {invalid_series[:5]}"
        )
    existing = {
        row[0]
        for row in conn.execute(
            "SELECT symbol FROM index_constituent WHERE index_name = ?", [index_name]
        ).fetchall()
    }
    incoming = {row["symbol"] for row in rows}
    removed = existing - incoming
    if removed:
        conn.executemany(
            "DELETE FROM index_constituent WHERE index_name = ? AND symbol = ?",
            [(index_name, symbol) for symbol in sorted(removed)],
        )
    added = 0
    for row in rows:
        changed = conn.execute(
            """
            INSERT INTO index_constituent
                (index_name, symbol, company_name, industry, isin, series, source, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (index_name, symbol) DO UPDATE SET
                company_name = excluded.company_name,
                industry = excluded.industry,
                isin = excluded.isin,
                series = excluded.series,
                source = excluded.source,
                fetched_at = excluded.fetched_at
            WHERE (company_name, industry, isin, series, source)
                IS DISTINCT FROM (excluded.company_name, excluded.industry,
                                  excluded.isin, excluded.series, excluded.source)
            RETURNING 1
            """,
            [
                index_name,
                row["symbol"],
                row["company_name"],
                row["industry"],
                row["isin"],
                row["series"],
                SOURCE_CONSTITUENTS,
                fetched_at,
            ],
        ).fetchone()
        if changed:
            added += 1
    return {"members": len(incoming), "removed": len(removed), "written": added}


def parse_index_close(payload: bytes, index_name: str) -> float:
    expected = " ".join(index_name.split()).casefold()
    for raw in csv.DictReader(io.StringIO(payload.decode("utf-8-sig"))):
        actual = " ".join((raw.get("Index Name") or "").split()).casefold()
        if actual != expected:
            continue
        try:
            return float(raw["Closing Index Value"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SourceError(
                f"index CSV contract changed for {index_name}: {type(exc).__name__}"
            ) from exc
    raise SourceError(f"{index_name} row missing from index close CSV")


def ingest_index_day(
    conn,
    day: date,
    *,
    index_name: str,
    fetched_at: dt | None = None,
    opener=None,
    is_tail: bool = False,
) -> bool:
    """Store one benchmark close; returns False on non-trading days.

    A 404 never advances the watermark: it may be a holiday or a late file.
    The watermark only moves on confirmed sessions; a gap day whose file
    published late is abandoned for beta purposes — alignment drops its
    pairs, so staleness can only reduce coverage, never distort values.
    """
    fetched_at = fetched_at or dt.now(UTC)
    url = INDEX_URL_TEMPLATE.format(stamp=day.strftime("%d%m%Y"))
    try:
        payload = fetch_csv(url, opener=_open_opener(opener))
    except NonTradingDay:
        if is_tail:
            log.info("%s non-trading or not yet published; left pending", day)
        return False
    close = parse_index_close(payload, index_name)
    conn.execute(
        """
        INSERT INTO index_close (index_name, trade_date, close, source, fetched_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (index_name, trade_date) DO UPDATE SET
            close = excluded.close,
            source = excluded.source,
            fetched_at = excluded.fetched_at
        WHERE close IS DISTINCT FROM excluded.close
        """,
        [index_name, day, close, SOURCE_INDEX, fetched_at],
    )
    _advance_watermark(conn, day, f"close={close}", fetched_at)
    return True


def _advance_watermark(conn, day: date, detail: str, updated_at: dt) -> None:
    current = db.get_watermark(conn, WATERMARK_KIND)
    if current is None or day >= current:
        db.set_watermark(conn, WATERMARK_KIND, day, detail=detail, updated_at=updated_at)


def backfill_index(
    conn,
    start: date,
    end: date,
    *,
    index_name: str,
    fetched_at: dt | None = None,
    opener=None,
) -> int:
    if start > end:
        raise ValueError("start after end")
    stored = 0
    day = start
    while day <= end:
        if day.weekday() < 5:
            stored += int(
                ingest_index_day(
                    conn,
                    day,
                    index_name=index_name,
                    fetched_at=fetched_at,
                    opener=opener,
                    is_tail=(day == end),
                )
            )
        else:
            _advance_watermark(conn, day, "weekend", fetched_at)
        day += timedelta(days=1)
    return stored


def index_refresh_range(conn, *, min_observations: int) -> tuple[date, date]:
    """Return a range large enough to make first-run beta usable.

    The archive is one file per calendar date. Convert required trading
    observations to calendar days and add 60 days for exchange holidays and
    publication gaps. A fresh T4.2 database must not need months of nightly
    accumulation before its minimum-observation gate can pass.
    """
    today_ist = dt.now(prices.IST).date()
    end = today_ist - timedelta(days=1)
    watermark = db.get_watermark(conn, WATERMARK_KIND)
    if watermark is None:
        bootstrap_days = ceil(min_observations * 7 / 5) + 60
        return end - timedelta(days=bootstrap_days), end
    return min(watermark + timedelta(days=1), end), end


def _consecutive_predecessors(dates: list[date]) -> dict[date, date]:
    return {current: previous for previous, current in zip(dates, dates[1:], strict=False)}


def beta(
    conn,
    symbol: str,
    *,
    benchmark: str,
    window_days: int,
    min_observations: int,
    cutoff: date | None = None,
) -> tuple[float | None, int]:
    """OLS beta of daily stock returns vs benchmark over the trailing window.

    A pair is formed only where both series share the same consecutive base
    date, so every observation spans exactly one session in BOTH series. A
    stock-side hole drops that pair instead of pairing mismatched horizons.
    """
    stock = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT trade_date, close FROM stock_price WHERE symbol = ? AND close IS NOT NULL "
            "AND (?::DATE IS NULL OR trade_date <= ?::DATE)",
            [symbol, cutoff, cutoff],
        ).fetchall()
    }
    market = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT trade_date, close FROM index_close WHERE index_name = ? "
            "AND (?::DATE IS NULL OR trade_date <= ?::DATE)",
            [benchmark, cutoff, cutoff],
        ).fetchall()
    }
    stock_pred = _consecutive_predecessors(sorted(stock))
    market_pred = _consecutive_predecessors(sorted(market))
    aligned = sorted(stock_pred.keys() & market_pred.keys())[-window_days:]
    pairs = []
    for day in aligned:
        base_stock = stock_pred[day]
        base_market = market_pred[day]
        if base_stock != base_market:
            continue
        if stock[base_stock] <= 0 or market[base_market] <= 0:
            continue
        pairs.append(
            (market[day] / market[base_market] - 1.0, stock[day] / stock[base_stock] - 1.0)
        )
    count = len(pairs)
    if count < min_observations:
        return None, count
    mean_market = sum(m for m, _ in pairs) / count
    mean_stock = sum(s for _, s in pairs) / count
    covariance = sum((m - mean_market) * (s - mean_stock) for m, s in pairs) / (count - 1)
    variance = sum((m - mean_market) ** 2 for m, _ in pairs) / (count - 1)
    if variance <= 0:
        return None, count
    return covariance / variance, count


def latest_closes(conn, *, cutoff: date | None = None) -> dict[str, tuple[date, float]]:
    rows = conn.execute(
        """
        SELECT p.symbol, p.trade_date, p.close FROM stock_price p
        JOIN (
            SELECT symbol, MAX(trade_date) AS last_date FROM stock_price
            WHERE close IS NOT NULL AND (?::DATE IS NULL OR trade_date <= ?::DATE)
            GROUP BY symbol
        ) latest ON p.symbol = latest.symbol AND p.trade_date = latest.last_date
        """,
        [cutoff, cutoff],
    ).fetchall()
    return {row[0]: (row[1], row[2]) for row in rows}


def build_watchlist(conn, config: dict, *, cutoff: date | None = None) -> dict:
    universe = config["universe_index"]
    members = [
        row[0]
        for row in conn.execute(
            "SELECT symbol FROM index_constituent WHERE index_name = ? ORDER BY symbol",
            [universe],
        ).fetchall()
    ]
    if not members:
        raise ValueError(f"no constituents stored for {universe}; run refresh first")
    closes = latest_closes(conn, cutoff=cutoff)
    picks: list[dict] = []
    excluded_price: list[dict] = []
    no_close: list[str] = []
    insufficient_beta: list[str] = []
    stale_close: list[str] = []
    price_reference = max((day for day, _ in closes.values()), default=None)
    for symbol in members:
        close_info = closes.get(symbol)
        if close_info is None:
            no_close.append(symbol)
            continue
        close_day, close = close_info
        if price_reference is not None and close_day < price_reference - timedelta(
            days=config["max_price_age_days"]
        ):
            stale_close.append(symbol)
            continue
        beta_value, observations = beta(
            conn,
            symbol,
            benchmark=config["benchmark"],
            window_days=config["window_days"],
            min_observations=config["min_observations"],
            cutoff=cutoff,
        )
        if beta_value is None:
            insufficient_beta.append(symbol)
            continue
        if close >= config["max_price"]:
            excluded_price.append({"symbol": symbol, "close": close})
            continue
        picks.append(
            {
                "symbol": symbol,
                "close": close,
                "as_of": close_day,
                "beta": beta_value,
                "observations": observations,
            }
        )
    picks.sort(key=lambda item: (-item["beta"], item["symbol"]))
    selected = picks[: config["top_n"]]
    for rank, item in enumerate(selected, 1):
        item["rank"] = rank
    excluded_price.sort(key=lambda item: item["symbol"])
    as_of = max((item["as_of"] for item in selected), default=None)
    return {
        "universe": universe,
        "benchmark": config["benchmark"],
        "as_of": as_of,
        "members": len(members),
        "picks": selected,
        "excluded_by_price": excluded_price,
        "gaps": {
            "no_close": sorted(no_close),
            "insufficient_beta": sorted(insufficient_beta),
            "stale_close": sorted(stale_close),
        },
    }


def render(report: dict) -> str:
    lines = [
        f"SWING WATCHLIST as_of={report['as_of']} universe={report['universe']} "
        f"benchmark={report['benchmark']} members={report['members']}",
        f"{'rank':>4}  {'symbol':<16} {'close':>10} {'beta':>6} {'obs':>4}",
    ]
    for item in report["picks"]:
        lines.append(
            f"{item['rank']:>4}  {item['symbol']:<16} {item['close']:>10.2f} "
            f"{item['beta']:>6.2f} {item['observations']:>4}"
        )
    gaps = report["gaps"]
    lines.append(
        f"price_excluded={len(report['excluded_by_price'])} "
        f"gaps(no_close={len(gaps['no_close'])}, "
        f"insufficient_beta={len(gaps['insufficient_beta'])}, "
        f"stale_close={len(gaps['stale_close'])})"
    )
    return "\n".join(lines)


def atomic_write(path: str, text: str) -> None:
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text + "\n")
    os.replace(tmp, path)


def refresh(conn, config: dict, *, opener=None, fetched_at: dt | None = None) -> dict:
    # Phases commit independently by design; a failed index backfill leaves
    # constituents refreshed and resumes idempotently on the next run.
    fetched_at = fetched_at or dt.now(UTC)
    payload = fetch_csv(
        CONSTITUENT_URL, opener=_open_opener(opener), referer="https://www.niftyindices.com/"
    )
    membership = store_constituents(
        conn,
        config["universe_index"],
        parse_constituents(payload),
        fetched_at=fetched_at,
        minimum_count=config["constituent_min_count"],
    )
    start, end = index_refresh_range(conn, min_observations=config["min_observations"])
    sessions = backfill_index(
        conn, start, end, index_name=config["benchmark"], fetched_at=fetched_at, opener=opener
    )
    return {"membership": membership, "range": (start, end), "sessions": sessions}


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(prog="invest-watchlist")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--out", help="write report to this file atomically")
    parser.add_argument("--report-only", action="store_true", help="skip network refresh")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        conn = db.connect(args.db)
        try:
            db.init_schema(conn)
            if not args.report_only:
                stats = refresh(conn, config)
                log.info("refresh: %s", stats)
            report = build_watchlist(conn, config)
        finally:
            conn.close()
    except (SourceError, ValueError, OSError) as exc:
        log.error("%s", exc)
        return 1
    text = render(report)
    print(text)
    if args.out:
        atomic_write(args.out, text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
