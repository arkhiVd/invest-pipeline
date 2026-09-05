"""TigZig MF ingest: snapshot refresh, name resolution, historical backfill.

Pacing: TigZig enforces 60 req/min + 500/day via headers (see
docs/spikes/t1.1-source-verification.md) — we stay well under both.
Normalization happens here so string formats never reach the DB.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import sys
import time
from datetime import date as date_cls
from pathlib import Path
from urllib import request as urlreq

from invest import db

log = logging.getLogger("invest.ingest")

TIGZIG_BASE = "https://api.tigzig.com/mf/v1"
MFAPI_BASE = "https://api.mfapi.in/mf"
MIN_CALL_INTERVAL_S = 1.1  # 60/min enforced; each call also takes ~2-5s
USER_AGENT = "invest-pipeline/0.1 (homelab; personal)"

# workbook abbreviations -> full AMFI tokens, applied during normalization
ABBREVIATIONS = {
    "sl": ["sun", "life"],
    "pru": ["prudential"],
    "fof": ["fund", "of", "fund"],
    "tig": ["tiger"],
    # workbook writes compounds without spaces; AMFI keeps them apart
    "midcap": ["mid", "cap"],
    "smallcap": ["small", "cap"],
    "largecap": ["large", "cap"],
    "flexicap": ["flexi", "cap"],
    "ultrashort": ["ultra", "short"],
}
GENERIC_TOKENS = {"fund", "-"}
DIRECT_HINTS = ("direct",)


def _get(url: str, timeout: int = 30) -> bytes:
    req = urlreq.Request(url, headers={"User-Agent": USER_AGENT})
    with urlreq.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_latest_snapshot() -> list[dict]:
    """Full universe, one row per scheme (~9.3MB CSV)."""
    raw = _get(f"{TIGZIG_BASE}/download?format=latest")
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8"))))


def fetch_scheme_history(scheme_code: int) -> dict:
    payload = json.loads(_get(f"{TIGZIG_BASE}/nav?scheme={scheme_code}"))
    if not payload.get("data"):
        msg = f"empty history for {scheme_code}"
        raise ValueError(msg)
    return payload


def fetch_scheme_history_mfapi(scheme_code: int) -> list[tuple[date_cls, float]]:
    """Fallback feed. Dates arrive as DD-MM-YYYY strings, navs as strings."""
    payload = json.loads(_get(f"{MFAPI_BASE}/{scheme_code}"))
    out = []
    for row in payload.get("data", []):
        d, m, y = row["date"].split("-")
        out.append((date_cls(int(y), int(m), int(d)), float(row["nav"])))
    if not out:
        msg = f"mfapi returned no data for {scheme_code}"
        raise ValueError(msg)
    return sorted(out)


def normalize_name(name: str) -> list[str]:
    lowered = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
    tokens: list[str] = []
    for tok in lowered.split():
        tokens.extend(ABBREVIATIONS.get(tok, [tok]))
    return [t for t in tokens if t not in GENERIC_TOKENS and not (len(t) == 1 and t.isalpha())]


def resolve_fund(
    fund_name: str,
    snapshot_rows: list[dict],
    prefer_direct: bool = True,
) -> dict | None:
    """Best snapshot match for a workbook fund name, or None.

    Scoring: every distinctive workbook token must appear in the candidate;
    ties prefer active schemes, then plan preference, then shorter names.

    Share-class policy (user decision 2026-08-25: all holdings are
    Direct-Growth): when any candidate carries direct+growth, restrict to
    those; never select IDCW/payout variants while a growth sibling exists.
    """
    want = [t for t in normalize_name(fund_name) if t not in {"100", "150", "50"}]
    scored: list[tuple[float, int, dict]] = []
    for row in snapshot_rows:
        cand_tokens = set(normalize_name(row["scheme_name"]))
        matched = sum(1 for t in want if t in cand_tokens)
        score = matched / max(len(want), 1)
        if score < 0.99 or score <= 0:
            continue
        is_active = row["is_active"] == "true"
        bonus = 0.0
        if is_active:
            bonus += 0.01
        if (not prefer_direct) or "direct" in cand_tokens:
            bonus += 0.008
        total = score + bonus - len(row["scheme_name"]) / 1e6
        scored.append((total, len(row["scheme_name"]), row))
    if not scored:
        return None

    def is_direct_growth(row: dict) -> bool:
        n = row["scheme_name"].lower()
        return "direct" in n and "growth" in n

    growth_pool = [s for s in scored if is_direct_growth(s[2])]
    pool = growth_pool or scored  # hard preference; IDCW only as last resort
    return max(pool, key=lambda s: s[0])[2]


def snapshot_to_scheme_row(row: dict, display_name: str | None = None) -> dict:
    def _num(key: str):
        val = row.get(key) or ""
        try:
            return float(val)
        except ValueError:
            return None

    def _d(key: str):
        val = row.get(key) or ""
        return date_cls.fromisoformat(val) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", val) else None

    return dict(
        scheme_code=int(row["scheme_code"]),
        display_name=display_name,
        name=row["scheme_name"],
        amc=row["amc"] or None,
        isin=row["isin"] or None,
        isin2=row["isin2"] or None,
        scheme_type=row["scheme_type"] or None,
        category=row["category"] or None,
        category_sub=row["category_sub"] or None,
        category_group_clean=row["category_group_clean"] or None,
        category_group=row["category_group"] or None,
        scheme_plan=row["scheme_plan"] or None,
        scheme_option=row["scheme_option"] or None,
        first_date=_d("first_date"),
        last_date=_d("last_date"),
        is_active=row["is_active"] == "true",
        is_stale=row["is_stale"] == "true",
        txic_code=row["txic_code"] or None,
        aaum_cr_quarterly_avg=_num("aaum_cr_quarterly_avg"),
        aaum_quarter=row["aaum_quarter"] or None,
        aaum_quarter_end=_d("aaum_quarter_end"),
    )


def backfill_history(
    conn,
    codes: list[int],
    min_interval_s: float = MIN_CALL_INTERVAL_S,
    fetcher=None,
) -> dict:
    """Pull full NAV history per scheme with pacing; returns counts.

    Fetcher is injectable for offline tests. Falls back to MFAPI once per
    scheme when TigZig fails.
    """
    fetcher = fetcher or fetch_scheme_history
    stats = {"schemes": 0, "nav_rows": 0, "fallback": []}
    for i, code in enumerate(codes):
        try:
            payload = fetcher(code)
            pairs = [(date_cls.fromisoformat(r["date"]), float(r["nav"])) for r in payload["data"]]
            name = payload.get("scheme_name")
        except Exception as exc:  # noqa: BLE001 — fallback path must catch all
            log.warning("tigzig failed for %s (%s); trying MFAPI", code, exc)
            try:
                pairs = fetch_scheme_history_mfapi(code)
            except Exception as exc2:  # noqa: BLE001
                log.error("both sources failed for %s: %s", code, exc2)
                continue
            stats["fallback"].append(code)
            name = None
        try:
            db.upsert_navs(conn, code, pairs)
        except Exception as exc:  # noqa: BLE001 — one bad scheme must not kill a run
            log.error("db load failed for %s: %s", code, exc)
            continue
        stats["schemes"] += 1
        stats["nav_rows"] += len(pairs)
        log.info(
            "backfill %d/%d code=%s rows=%d name=%s",
            i + 1,
            len(codes),
            code,
            len(pairs),
            name,
        )
        if i < len(codes) - 1:
            time.sleep(min_interval_s)
    return stats


def load_manual_overrides(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    return {k: int(v) for k, v in json.loads(path.read_text()).items()}


def load_watchlist(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return json.loads(path.read_text()).get("funds", [])


def load_ignore_set(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return set(json.loads(path.read_text()).get("ignore", []))


def register_scheme_code(
    conn, code: int, display_name: str | None, snapshot_rows: list[dict]
) -> None:
    """Ensure mf_scheme has `code`, enriched from snapshot when available."""
    if conn.execute("SELECT 1 FROM mf_scheme WHERE scheme_code = ?", [code]).fetchone():
        if display_name:
            conn.execute(
                "UPDATE mf_scheme SET display_name = ? WHERE scheme_code = ?",
                [display_name, code],
            )
        return
    match = next((r for r in snapshot_rows if int(r["scheme_code"]) == code), None)
    if match:
        db.upsert_scheme(conn, **snapshot_to_scheme_row(match, display_name))
    else:
        db.upsert_scheme(
            conn, scheme_code=code, name=display_name or str(code), display_name=display_name
        )


def _ensure_db_path(db_path: str) -> str:
    """DuckDB will not create missing parent directories itself."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    return db_path


def main(argv: list[str] | None = None) -> int:
    import argparse

    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser(prog="invest-ingest")
    parser.add_argument("command", choices=["refresh", "backfill"])
    parser.add_argument("--db", default="data/invest.duckdb")
    parser.add_argument("--snapshot-cache", default="data/latest_snapshot.csv")
    parser.add_argument("--overrides", default="config/scheme_map.json")
    parser.add_argument("--watchlist", default="config/watchlist.json")
    parser.add_argument("--ignore", default="config/ignore.json")
    parser.add_argument("--funds-config", default="config/funds.json")
    parser.add_argument(
        "--only-codes",
        default=None,
        help="comma-separated scheme codes: register + backfill just these",
    )
    args = parser.parse_args(argv)

    conn = db.connect(_ensure_db_path(args.db))
    db.init_schema(conn)

    cache = Path(args.snapshot_cache)
    if args.command == "refresh" or not cache.exists():
        rows = fetch_latest_snapshot()
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(rows))  # parsed rows reused by backfill
        log.info("snapshot fetched: %d schemes", len(rows))
    else:
        rows = json.loads(cache.read_text())
        log.info("snapshot from cache: %d schemes", len(rows))

    overrides = load_manual_overrides(Path(args.overrides))
    watchlist = load_watchlist(Path(args.watchlist))
    ignore = load_ignore_set(Path(args.ignore))

    if args.command == "refresh":
        for row in rows:
            db.upsert_scheme(conn, **snapshot_to_scheme_row(row))
        log.info("refresh complete: %d scheme rows upserted", len(rows))
        return 0

    # targeted top-up mode (e.g. newly added watchlist entries)
    if args.only_codes:
        codes = [int(x) for x in args.only_codes.split(",") if x.strip()]
        for code in codes:
            register_scheme_code(conn, code, None, rows)
        stats = backfill_history(conn, codes)
        log.info(
            "targeted backfill done: %d schemes, %d nav rows, fallback=%s",
            stats["schemes"],
            stats["nav_rows"],
            stats["fallback"],
        )
        return 0 if stats["schemes"] == len(codes) else 1

    # backfill: resolve configured funds and watchlist, then pull history
    configured = json.loads(Path(args.funds_config).read_text())
    if not isinstance(configured, list) or not all(
        isinstance(name, str) and name.strip() for name in configured
    ):
        raise ValueError("funds config must be a list of non-empty names")
    fund_names = sorted({name.strip() for name in configured})
    skipped_ignored = [f for f in fund_names if f in ignore]
    fund_names = [f for f in fund_names if f not in ignore]
    if skipped_ignored:
        log.info("ignored per config: %s", skipped_ignored)
    resolved: list[tuple[str, int]] = []
    unresolved: list[str] = []
    for fund in fund_names:
        if fund in overrides:
            code = overrides[fund]
            resolved.append((fund, code))
            continue
        hit = resolve_fund(fund, rows)
        if hit:
            code = int(hit["scheme_code"])
            resolved.append((fund, code))
        else:
            unresolved.append(fund)

    # watchlist additions (register metadata, then pull history)
    already = {code for _, code in resolved}
    added = 0
    for entry in watchlist:
        code = int(entry["code"])
        if code in already:
            continue
        register_scheme_code(conn, code, entry.get("name"), rows)
        resolved.append((entry.get("name") or str(code), code))
        already.add(code)
        added += 1
    log.info("watchlist: %d new codes merged (%d total tracked)", added, len(resolved))

    for fund, code in resolved:
        register_scheme_code(conn, code, fund, rows)
    log.warning(
        "resolved %d/%d; unresolved: %s", len(resolved), len(fund_names), unresolved or "none"
    )

    # only schemes already registered (resolved+overrides) get history pulls
    codes = [code for _, code in resolved]
    stats = backfill_history(conn, codes)
    log.info(
        "backfill done: %d schemes, %d nav rows, fallback=%s",
        stats["schemes"],
        stats["nav_rows"],
        stats["fallback"],
    )
    return 0 if stats["schemes"] == len(codes) else 1


if __name__ == "__main__":
    sys.exit(main())
