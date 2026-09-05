"""Official point-in-time market-history adapters and disposable schema."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from datetime import UTC, date
from datetime import datetime as dt
from decimal import Decimal
from urllib import error, parse, request
from zoneinfo import ZoneInfo

import duckdb

from invest import nse_filings

SCHEMA_VERSION = 22
IST = ZoneInfo("Asia/Kolkata")
MAX_SOURCE_BYTES = 25 * 1024 * 1024
ADJUSTMENT_METHODOLOGY = "nse-action-adjusted-2026.1"
NSE_API = "https://www.nseindia.com/api/corporates-corporateActions"
ARCHIVE = "https://nsearchives.nseindia.com/content/equities"

_DDL = [
    """
    CREATE TABLE IF NOT EXISTS market_history_import (
        import_id TEXT PRIMARY KEY,
        source_type TEXT NOT NULL,
        source_url TEXT NOT NULL,
        content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
        coverage_start DATE,
        coverage_end DATE,
        row_count BIGINT NOT NULL CHECK(row_count>=0),
        source_row_count BIGINT NOT NULL CHECK(source_row_count>=row_count),
        duplicate_row_count BIGINT NOT NULL CHECK(duplicate_row_count>=0),
        excluded_row_count BIGINT NOT NULL CHECK(excluded_row_count>=0),
        exclusions_json TEXT NOT NULL,
        source_fingerprint TEXT NOT NULL CHECK(length(source_fingerprint)=64),
        fetched_at TIMESTAMPTZ NOT NULL,
        CHECK(source_row_count=row_count+duplicate_row_count+excluded_row_count),
        UNIQUE(source_type,content_sha256)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS market_corporate_action (
        import_id TEXT NOT NULL REFERENCES market_history_import(import_id),
        source_row_hash TEXT NOT NULL,
        symbol TEXT NOT NULL,
        isin TEXT,
        series TEXT,
        subject TEXT NOT NULL,
        ex_date DATE NOT NULL,
        record_date DATE,
        broadcast_at TIMESTAMPTZ,
        face_value DECIMAL(20,6),
        action_kind TEXT NOT NULL CHECK(action_kind IN (
            'BONUS','SPLIT','CONSOLIDATION','CASH_DISTRIBUTION',
            'RIGHTS','DEMERGER','NON_ADJUSTING','UNKNOWN'
        )),
        parse_status TEXT NOT NULL CHECK(parse_status IN ('SUPPORTED','UNSUPPORTED')),
        structural_factor DECIMAL(28,12),
        cash_amount DECIMAL(28,10),
        parse_reason TEXT NOT NULL,
        raw_json TEXT NOT NULL,
        CHECK(
            (parse_status='SUPPORTED' AND action_kind IN ('BONUS','SPLIT','CONSOLIDATION')
                AND structural_factor>0 AND cash_amount IS NULL)
            OR (parse_status='SUPPORTED' AND action_kind='CASH_DISTRIBUTION'
                AND cash_amount>0 AND structural_factor IS NULL)
            OR (parse_status='SUPPORTED' AND action_kind='NON_ADJUSTING'
                AND cash_amount IS NULL AND structural_factor IS NULL)
            OR (parse_status='UNSUPPORTED' AND structural_factor IS NULL)
        ),
        PRIMARY KEY(import_id,source_row_hash)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS security_lineage_event (
        import_id TEXT NOT NULL REFERENCES market_history_import(import_id),
        source_row_hash TEXT NOT NULL,
        event_type TEXT NOT NULL CHECK(event_type IN ('SYMBOL_CHANGE','NAME_CHANGE','DELISTING')),
        effective_date DATE NOT NULL,
        old_symbol TEXT,
        new_symbol TEXT,
        symbol TEXT,
        old_name TEXT,
        new_name TEXT,
        delisting_type TEXT,
        raw_row TEXT NOT NULL,
        PRIMARY KEY(import_id,source_row_hash),
        CHECK(
            (event_type='SYMBOL_CHANGE' AND old_symbol IS NOT NULL AND new_symbol IS NOT NULL
                AND length(old_symbol)>0 AND length(new_symbol)>0 AND old_symbol<>new_symbol)
            OR (event_type='NAME_CHANGE' AND symbol IS NOT NULL
                AND old_name IS NOT NULL AND new_name IS NOT NULL AND length(symbol)>0
                AND length(old_name)>0 AND length(new_name)>0 AND old_name<>new_name)
            OR (event_type='DELISTING' AND symbol IS NOT NULL AND delisting_type IS NOT NULL
                AND length(symbol)>0 AND length(delisting_type)>0)
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS adjusted_stock_price (
        symbol TEXT NOT NULL,
        trade_date DATE NOT NULL,
        adjusted_open DECIMAL(28,10) NOT NULL,
        adjusted_high DECIMAL(28,10) NOT NULL,
        adjusted_low DECIMAL(28,10) NOT NULL,
        adjusted_close DECIMAL(28,10) NOT NULL,
        cumulative_factor DECIMAL(28,12) NOT NULL CHECK(cumulative_factor>0),
        methodology_version TEXT NOT NULL,
        input_fingerprint TEXT NOT NULL CHECK(length(input_fingerprint)=64),
        calculated_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY(symbol,trade_date,methodology_version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS market_action_reconciliation (
        action_import_id TEXT NOT NULL,
        action_source_row_hash TEXT NOT NULL,
        symbol TEXT NOT NULL,
        coverage_start DATE NOT NULL,
        coverage_end DATE NOT NULL,
        previous_session DATE NOT NULL,
        ex_session DATE NOT NULL,
        raw_overnight_return DECIMAL(28,12) NOT NULL,
        adjusted_overnight_return DECIMAL(28,12) NOT NULL,
        methodology_version TEXT NOT NULL,
        input_fingerprint TEXT NOT NULL CHECK(length(input_fingerprint)=64),
        PRIMARY KEY(
            action_import_id,action_source_row_hash,coverage_start,coverage_end,methodology_version
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS adjusted_price_exclusion (
        symbol TEXT NOT NULL,
        coverage_start DATE NOT NULL,
        coverage_end DATE NOT NULL,
        reason TEXT NOT NULL,
        action_import_id TEXT,
        action_source_row_hash TEXT,
        input_fingerprint TEXT NOT NULL CHECK(length(input_fingerprint)=64),
        methodology_version TEXT NOT NULL,
        calculated_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY(symbol,coverage_start,coverage_end,methodology_version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS filing_availability (
        import_id TEXT NOT NULL REFERENCES market_history_import(import_id),
        source_row_hash TEXT NOT NULL,
        symbol TEXT NOT NULL,
        filing_type TEXT NOT NULL,
        xbrl_url TEXT NOT NULL,
        period_end DATE,
        available_at TIMESTAMPTZ NOT NULL,
        timestamp_field TEXT NOT NULL,
        raw_json TEXT NOT NULL,
        PRIMARY KEY(import_id,source_row_hash)
    )
    """,
]


class MarketHistoryError(ValueError):
    pass


def fetch_url(url: str, *, opener=None) -> bytes:
    opener = opener or request.build_opener()
    req = request.Request(
        url,
        headers={
            "User-Agent": nse_filings.USER_AGENT,
            "Accept": "application/json,text/csv,*/*",
            "Referer": "https://www.nseindia.com/",
        },
    )
    try:
        with opener.open(req, timeout=30) as response:
            raw = response.read(MAX_SOURCE_BYTES + 1)
    except (error.HTTPError, error.URLError, TimeoutError) as exc:
        raise MarketHistoryError(f"official source fetch failed: {type(exc).__name__}") from exc
    if len(raw) > MAX_SOURCE_BYTES:
        raise MarketHistoryError("official source exceeds size limit")
    return raw


def corporate_actions_url(start: date, end: date) -> str:
    if start > end:
        raise MarketHistoryError("corporate-action range is invalid")
    query = parse.urlencode(
        {
            "index": "equities",
            "from_date": start.strftime("%d-%m-%Y"),
            "to_date": end.strftime("%d-%m-%Y"),
        }
    )
    return f"{NSE_API}?{query}"


def fetch_corporate_actions(start: date, end: date, *, opener=None) -> dict:
    url = corporate_actions_url(start, end)
    return parse_corporate_actions(fetch_url(url, opener=opener), url)


def fetch_archive(name: str, *, opener=None) -> dict:
    parsers = {
        "delisted.csv": parse_delistings,
        "symbolchange.csv": parse_symbol_changes,
        "namechange.csv": parse_name_changes,
    }
    if name not in parsers:
        raise MarketHistoryError("archive source is not allowlisted")
    url = f"{ARCHIVE}/{name}"
    return parsers[name](fetch_url(url, opener=opener), url)


def _optional_text(value) -> str | None:
    text = str(value or "").strip()
    return None if not text or text in {"-", "NA", "N/A"} else text


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _date(value: str | None, *formats: str) -> date | None:
    text = (value or "").strip()
    if not text or text in {"-", "NA", "N/A"}:
        return None
    for fmt in formats:
        try:
            return dt.strptime(text.title(), fmt).date()
        except ValueError:
            pass
    raise MarketHistoryError(f"invalid source date: {text}")


def _timestamp(value: str | None) -> dt | None:
    text = (value or "").strip()
    if not text or text in {"-", "NA", "N/A"}:
        return None
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%m-%Y %H:%M:%S"):
        try:
            return dt.strptime(text.title(), fmt).replace(tzinfo=IST).astimezone(UTC)
        except ValueError:
            pass
    raise MarketHistoryError(f"invalid source timestamp: {text}")


def _source(
    raw: bytes,
    source_type: str,
    source_url: str,
    rows: list[dict],
    *,
    excluded_rows: list[dict] | None = None,
    source_row_count: int | None = None,
) -> dict:
    digest = _hash(raw)
    excluded_rows = excluded_rows or []
    source_row_count = (
        len(rows) + len(excluded_rows) if source_row_count is None else source_row_count
    )
    unique = {row["source_row_hash"]: row for row in rows}
    rows = list(unique.values())
    dates = [row["effective_date"] for row in rows if row.get("effective_date")]
    return {
        "source_type": source_type,
        "source_url": source_url,
        "content_sha256": digest,
        "source_fingerprint": _hash(
            json.dumps(rows, sort_keys=True, default=str, separators=(",", ":")).encode()
        ),
        "coverage_start": min(dates) if dates else None,
        "coverage_end": max(dates) if dates else None,
        "rows": rows,
        "source_row_count": source_row_count,
        "duplicate_rows": source_row_count - len(rows) - len(excluded_rows),
        "excluded_rows": excluded_rows,
    }


def classify_action_subject(subject: str) -> dict:
    text = " ".join(subject.upper().replace("RS.", "RS").replace("RE.", "RE").split())
    bonus = re.fullmatch(r"BONUS\s+(\d+)\s*:\s*(\d+)", text)
    if bonus:
        issued, held = map(Decimal, bonus.groups())
        if issued > 0 and held > 0:
            return {
                "action_kind": "BONUS",
                "parse_status": "SUPPORTED",
                "structural_factor": held / (held + issued),
                "cash_amount": None,
                "parse_reason": "exact bonus ratio",
            }
    split = re.fullmatch(
        r"FACE VALUE SPLIT \(SUB-DIVISION\) - FROM (?:RS|RE)\s*(\d+(?:\.\d+)?)"
        r"/-? PER SHARE TO (?:RS|RE)\s*(\d+(?:\.\d+)?)/-? PER SHARE",
        text,
    )
    consolidation = re.fullmatch(
        r"CONSOLIDATION OF EQUITY SHARES FROM (?:RS|RE)\s*(\d+(?:\.\d+)?)"
        r" PER SHARE TO (?:RS|RE)\s*(\d+(?:\.\d+)?) PER SHARE",
        text,
    )
    structural = split or consolidation
    if structural:
        old, new = map(Decimal, structural.groups())
        if old > 0 and new > 0:
            return {
                "action_kind": "SPLIT" if split else "CONSOLIDATION",
                "parse_status": "SUPPORTED",
                "structural_factor": new / old,
                "cash_amount": None,
                "parse_reason": "exact face-value ratio",
            }
    compound_tokens = (
        "RIGHT",
        "BONUS",
        "SPLIT",
        "SUB-DIVISION",
        "CONSOLIDATION",
        "DEMERGER",
        "MERGER",
        "AMALGAMATION",
        "BUY BACK",
    )
    if "DIVIDEND" in text and any(token in text for token in compound_tokens):
        return {
            "action_kind": "UNKNOWN",
            "parse_status": "UNSUPPORTED",
            "structural_factor": None,
            "cash_amount": None,
            "parse_reason": "compound action contains dividend and structural terms",
        }
    if "DISTRIBUTION" in text:
        return {
            "action_kind": "CASH_DISTRIBUTION",
            "parse_status": "UNSUPPORTED",
            "structural_factor": None,
            "cash_amount": None,
            "parse_reason": "compound distribution requires component accounting",
        }
    if "DIVIDEND" in text:
        amounts = re.findall(
            r"(?:SPECIAL\s+DIVIDEND|DIVIDEND)\s*-?\s*(?:RS|RE)\s*(\d+(?:\.\d+)?)"
            r"\s+PER\s+(?:SH(?:ARE)?|UNIT)",
            text,
        )
        if amounts:
            cash = sum((Decimal(value) for value in amounts), Decimal("0"))
            return {
                "action_kind": "CASH_DISTRIBUTION",
                "parse_status": "SUPPORTED",
                "structural_factor": None,
                "cash_amount": cash,
                "parse_reason": "explicit per-share dividend amount",
            }
        return {
            "action_kind": "CASH_DISTRIBUTION",
            "parse_status": "UNSUPPORTED",
            "structural_factor": None,
            "cash_amount": None,
            "parse_reason": "dividend amount is not unambiguous",
        }
    if "RIGHT" in text:
        kind, reason = "RIGHTS", "rights require issue-price and entitlement accounting"
    elif "DEMERGER" in text or "AMALGAM" in text or "MERGER" in text:
        kind, reason = "DEMERGER", "reorganization requires security allocation evidence"
    elif "BUY BACK" in text:
        kind, reason = "UNKNOWN", "buyback requires tender and cash-proceeds evidence"
    elif any(token in text for token in ("ANNUAL GENERAL MEETING", "BOARD MEETING")):
        return {
            "action_kind": "NON_ADJUSTING",
            "parse_status": "SUPPORTED",
            "structural_factor": None,
            "cash_amount": None,
            "parse_reason": "no deterministic share or cash adjustment",
        }
    else:
        kind, reason = "UNKNOWN", "action vocabulary is unsupported"
    return {
        "action_kind": kind,
        "parse_status": "UNSUPPORTED",
        "structural_factor": None,
        "cash_amount": None,
        "parse_reason": reason,
    }


def parse_corporate_actions(raw: bytes, source_url: str = NSE_API) -> dict:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MarketHistoryError("corporate-action payload is not JSON") from exc
    if not isinstance(payload, list):
        raise MarketHistoryError("corporate-action payload must be a list")
    rows = []
    for item in payload:
        required = {"symbol", "subject", "exDate"}
        if not isinstance(item, dict) or not required <= item.keys():
            raise MarketHistoryError("corporate-action row contract changed")
        symbol = nse_filings.valid_symbol(item["symbol"])
        ex_date = _date(item["exDate"], "%d-%b-%Y")
        canonical = json.dumps(item, sort_keys=True, separators=(",", ":"))
        classification = classify_action_subject(str(item["subject"]).strip())
        rows.append(
            {
                "source_row_hash": _hash(canonical.encode()),
                "symbol": symbol,
                "isin": (item.get("isin") or "").strip() or None,
                "series": (item.get("series") or "").strip().upper() or None,
                "subject": str(item["subject"]).strip(),
                "ex_date": ex_date,
                "record_date": _date(item.get("recDate"), "%d-%b-%Y"),
                "broadcast_at": _timestamp(item.get("caBroadcastDate")),
                "face_value": _optional_text(item.get("faceVal")),
                **classification,
                "raw_json": canonical,
                "effective_date": ex_date,
            }
        )
    return _source(raw, "NSE_CORPORATE_ACTION", source_url, rows)


def _csv_rows(raw: bytes) -> list[list[str]]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252")
    return [
        [cell.strip() for cell in row]
        for row in csv.reader(io.StringIO(text))
        if any(cell.strip() for cell in row)
    ]


def parse_delistings(raw: bytes, source_url: str = f"{ARCHIVE}/delisted.csv") -> dict:
    records = _csv_rows(raw)
    if not records or [cell.lower() for cell in records[0][:4]] != [
        "symbol",
        "company",
        "delisted date",
        "type of delisting",
    ]:
        raise MarketHistoryError("delisting CSV header changed")
    rows = []
    for record in records[1:]:
        if len(record) < 4:
            raise MarketHistoryError("delisting CSV row changed")
        effective = _date(record[2], "%d-%b-%Y", "%d-%b-%y", "%d-%m-%Y", "%d/%m/%Y")
        raw_row = json.dumps(record, separators=(",", ":"))
        rows.append(
            {
                "source_row_hash": _hash(raw_row.encode()),
                "event_type": "DELISTING",
                "effective_date": effective,
                "symbol": nse_filings.valid_symbol(record[0]),
                "old_symbol": None,
                "new_symbol": None,
                "old_name": record[1] or None,
                "new_name": None,
                "delisting_type": record[3] or None,
                "raw_row": raw_row,
            }
        )
    return _source(raw, "NSE_DELISTING", source_url, rows)


def parse_symbol_changes(raw: bytes, source_url: str = f"{ARCHIVE}/symbolchange.csv") -> dict:
    records = _csv_rows(raw)
    if not records:
        raise MarketHistoryError("symbol-change CSV is empty")
    rows = []
    for record in records:
        if len(record) < 4:
            raise MarketHistoryError("symbol-change CSV row changed")
        effective = _date(record[3], "%d-%b-%Y")
        raw_row = json.dumps(record, separators=(",", ":"))
        rows.append(
            {
                "source_row_hash": _hash(raw_row.encode()),
                "event_type": "SYMBOL_CHANGE",
                "effective_date": effective,
                "symbol": None,
                "old_symbol": nse_filings.valid_symbol(record[1]),
                "new_symbol": nse_filings.valid_symbol(record[2]),
                "old_name": record[0] or None,
                "new_name": None,
                "delisting_type": None,
                "raw_row": raw_row,
            }
        )
    return _source(raw, "NSE_SYMBOL_CHANGE", source_url, rows)


def parse_name_changes(raw: bytes, source_url: str = f"{ARCHIVE}/namechange.csv") -> dict:
    records = _csv_rows(raw)
    expected = ["NCH_SYMBOL", "NCH_PREV_NAME", "NCH_NEW_NAME", "NCH_DT"]
    if not records or [cell.strip().upper() for cell in records[0][:4]] != expected:
        raise MarketHistoryError("name-change CSV header changed")
    rows = []
    for record in records[1:]:
        if len(record) < 4:
            raise MarketHistoryError("name-change CSV row changed")
        effective = _date(record[3], "%d-%b-%Y")
        raw_row = json.dumps(record, separators=(",", ":"))
        rows.append(
            {
                "source_row_hash": _hash(raw_row.encode()),
                "event_type": "NAME_CHANGE",
                "effective_date": effective,
                "symbol": nse_filings.valid_symbol(record[0]),
                "old_symbol": None,
                "new_symbol": None,
                "old_name": record[1] or None,
                "new_name": record[2] or None,
                "delisting_type": None,
                "raw_row": raw_row,
            }
        )
    return _source(raw, "NSE_NAME_CHANGE", source_url, rows)


def parse_filing_availability(raw: bytes, filing_type: str, source_url: str) -> dict:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MarketHistoryError("filing payload is not JSON") from exc
    records = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        raise MarketHistoryError("filing payload row contract changed")
    fields = {
        "legacy": ("broadCastDate", "xbrl", "symbol", "toDate"),
        "integrated": ("broadcast_Date", "xbrl", "symbol", "qe_Date"),
        "shareholding": ("broadcastDate", "xbrl", "symbol", "date"),
    }
    if filing_type not in fields:
        raise MarketHistoryError("unsupported filing type")
    stamp_field, url_field, symbol_field, period_field = fields[filing_type]
    rows = []
    excluded_rows = []
    for position, item in enumerate(records):
        url = item.get(url_field) if isinstance(item, dict) else None
        stamp = item.get(stamp_field) if isinstance(item, dict) else None
        if not url or not nse_filings.ARCHIVE_RE.fullmatch(str(url)):
            excluded_rows.append({"position": position, "reason": "missing_or_invalid_xbrl_url"})
            continue
        if not stamp:
            raise MarketHistoryError("filing availability timestamp missing")
        canonical = json.dumps(item, sort_keys=True, separators=(",", ":"))
        available = _timestamp(stamp)
        period = _date(item.get(period_field), "%d-%b-%Y", "%Y-%m-%d")
        rows.append(
            {
                "source_row_hash": _hash(canonical.encode()),
                "symbol": nse_filings.valid_symbol(item[symbol_field]),
                "filing_type": filing_type,
                "xbrl_url": str(url),
                "period_end": period,
                "available_at": available,
                "timestamp_field": stamp_field,
                "raw_json": canonical,
                "effective_date": available.date(),
            }
        )
    return _source(
        raw,
        f"NSE_FILING_AVAILABILITY_{filing_type.upper()}",
        source_url,
        rows,
        excluded_rows=excluded_rows,
        source_row_count=len(records),
    )


def derive_adjusted_prices(
    conn: duckdb.DuckDBPyConnection,
    symbol: str,
    coverage_start: date,
    coverage_end: date,
) -> dict:
    symbol = nse_filings.valid_symbol(symbol)
    if coverage_start > coverage_end:
        raise MarketHistoryError("adjustment coverage is invalid")
    bars = conn.execute(
        "SELECT trade_date,open,high,low,close FROM stock_price "
        "WHERE symbol=? AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
        [symbol, coverage_start, coverage_end],
    ).fetchall()
    if not bars:
        raise MarketHistoryError("adjustment requires raw price bars")
    for trade_date, open_price, high, low, close in bars:
        values = (open_price, high, low, close)
        if any(value is None or Decimal(str(value)) <= 0 for value in values):
            raise MarketHistoryError(f"invalid raw OHLC on {trade_date}")
        if low > min(open_price, close) or high < max(open_price, close) or low > high:
            raise MarketHistoryError(f"invalid raw OHLC on {trade_date}")

    def excluded(reason: str, import_id: str, source_row_hash: str, evidence) -> dict:
        fingerprint = _hash(
            json.dumps(
                {
                    "bars": bars,
                    "evidence": evidence,
                    "reason": reason,
                    "methodology": ADJUSTMENT_METHODOLOGY,
                },
                default=str,
                separators=(",", ":"),
            ).encode()
        )
        return {
            "status": "EXCLUDED",
            "symbol": symbol,
            "coverage_start": coverage_start,
            "coverage_end": coverage_end,
            "reason": reason,
            "action_import_id": import_id,
            "action_source_row_hash": source_row_hash,
            "input_fingerprint": fingerprint,
            "rows": [],
        }

    lineage = conn.execute(
        "SELECT import_id,source_row_hash,event_type,effective_date,old_symbol,new_symbol,symbol "
        "FROM security_lineage_event WHERE effective_date BETWEEN ? AND ? AND ("
        "(event_type='SYMBOL_CHANGE' AND (old_symbol=? OR new_symbol=?)) OR "
        "(event_type='DELISTING' AND symbol=?)) ORDER BY effective_date,source_row_hash",
        [coverage_start, coverage_end, symbol, symbol, symbol],
    ).fetchall()
    if lineage:
        row = lineage[0]
        return excluded(f"unresolved security lineage event: {row[2]}", row[0], row[1], lineage)
    actions = conn.execute(
        "SELECT import_id,source_row_hash,ex_date,action_kind,parse_status,"
        "structural_factor,cash_amount,subject FROM market_corporate_action "
        "WHERE symbol=? AND ex_date>? AND ex_date<=? "
        "QUALIFY row_number() OVER (PARTITION BY source_row_hash ORDER BY import_id)=1 "
        "ORDER BY ex_date,source_row_hash",
        [symbol, coverage_start, coverage_end],
    ).fetchall()
    unsupported = [row for row in actions if row[4] == "UNSUPPORTED"]
    if unsupported:
        row = unsupported[0]
        return excluded(f"unsupported corporate action: {row[7]}", row[0], row[1], actions)

    event_factors: dict[date, Decimal] = {}
    for ex_date in sorted({row[2] for row in actions}):
        dated = [row for row in actions if row[2] == ex_date and row[3] != "NON_ADJUSTING"]
        structural = [Decimal(str(row[5])) for row in dated if row[5] is not None]
        cash = sum((Decimal(str(row[6])) for row in dated if row[6] is not None), Decimal("0"))
        if len(structural) > 1 or (structural and cash):
            row = dated[0]
            return excluded(
                "same-date corporate-action ordering is ambiguous", row[0], row[1], dated
            )
        factor = structural[0] if structural else Decimal("1")
        if cash:
            previous = next(
                (Decimal(str(row[4])) for row in reversed(bars) if row[0] < ex_date), None
            )
            if previous is None or previous <= cash:
                row = dated[0]
                return excluded("cash action lacks a valid previous close", row[0], row[1], dated)
            factor = (previous - cash) / previous
        if dated:
            event_factors[ex_date] = factor

    adjusted = []
    cumulative = Decimal("1")
    events_desc = sorted(event_factors.items(), reverse=True)
    event_index = 0
    for trade_date, open_price, high, low, close in reversed(bars):
        while event_index < len(events_desc) and events_desc[event_index][0] > trade_date:
            cumulative *= events_desc[event_index][1]
            event_index += 1
        adjusted_row = {
            "symbol": symbol,
            "trade_date": trade_date,
            "adjusted_open": Decimal(str(open_price)) * cumulative,
            "adjusted_high": Decimal(str(high)) * cumulative,
            "adjusted_low": Decimal(str(low)) * cumulative,
            "adjusted_close": Decimal(str(close)) * cumulative,
            "cumulative_factor": cumulative,
        }
        if (
            min(
                adjusted_row["adjusted_open"],
                adjusted_row["adjusted_high"],
                adjusted_row["adjusted_low"],
                adjusted_row["adjusted_close"],
            )
            <= 0
            or adjusted_row["adjusted_low"]
            > min(adjusted_row["adjusted_open"], adjusted_row["adjusted_close"])
            or adjusted_row["adjusted_high"]
            < max(adjusted_row["adjusted_open"], adjusted_row["adjusted_close"])
        ):
            raise MarketHistoryError(f"invalid adjusted OHLC on {trade_date}")
        adjusted.append(adjusted_row)
    adjusted.reverse()
    adjusted_by_date = {row["trade_date"]: row for row in adjusted}
    reconciliations = []
    for row in actions:
        if row[3] == "NON_ADJUSTING":
            continue
        previous = next((bar for bar in reversed(bars) if bar[0] < row[2]), None)
        ex_session = next((bar for bar in bars if bar[0] >= row[2]), None)
        if previous is None or ex_session is None:
            return excluded("corporate action lacks both session boundaries", row[0], row[1], row)
        raw_return = Decimal(str(ex_session[1])) / Decimal(str(previous[4])) - Decimal("1")
        adjusted_return = adjusted_by_date[ex_session[0]]["adjusted_open"] / adjusted_by_date[
            previous[0]
        ]["adjusted_close"] - Decimal("1")
        reconciliations.append(
            {
                "action_import_id": row[0],
                "action_source_row_hash": row[1],
                "symbol": symbol,
                "previous_session": previous[0],
                "ex_session": ex_session[0],
                "raw_overnight_return": raw_return,
                "adjusted_overnight_return": adjusted_return,
            }
        )
    inputs = {
        "bars": bars,
        "actions": actions,
        "methodology": ADJUSTMENT_METHODOLOGY,
    }
    fingerprint = _hash(json.dumps(inputs, default=str, separators=(",", ":")).encode())
    return {
        "status": "READY",
        "symbol": symbol,
        "coverage_start": coverage_start,
        "coverage_end": coverage_end,
        "input_fingerprint": fingerprint,
        "rows": adjusted,
        "reconciliations": reconciliations,
    }


def store_adjusted_prices(
    conn: duckdb.DuckDBPyConnection, result: dict, calculated_at: dt | None = None
) -> dict:
    calculated_at = calculated_at or dt.now(UTC)
    if result["status"] == "EXCLUDED":
        adjusted_count = conn.execute(
            "SELECT count(*) FROM adjusted_stock_price WHERE symbol=? "
            "AND trade_date BETWEEN ? AND ? AND methodology_version=?",
            [
                result["symbol"],
                result["coverage_start"],
                result["coverage_end"],
                ADJUSTMENT_METHODOLOGY,
            ],
        ).fetchone()[0]
        reconciliation_count = conn.execute(
            "SELECT count(*) FROM market_action_reconciliation WHERE symbol=? "
            "AND coverage_start<=? AND coverage_end>=? AND methodology_version=?",
            [
                result["symbol"],
                result["coverage_end"],
                result["coverage_start"],
                ADJUSTMENT_METHODOLOGY,
            ],
        ).fetchone()[0]
        if adjusted_count or reconciliation_count:
            raise MarketHistoryError("adjusted state conflicts with exclusion")
        existing = conn.execute(
            "SELECT reason,action_import_id,action_source_row_hash,input_fingerprint "
            "FROM adjusted_price_exclusion WHERE symbol=? AND coverage_start=? "
            "AND coverage_end=? AND methodology_version=?",
            [
                result["symbol"],
                result["coverage_start"],
                result["coverage_end"],
                ADJUSTMENT_METHODOLOGY,
            ],
        ).fetchone()
        identity = (
            result["reason"],
            result["action_import_id"],
            result["action_source_row_hash"],
            result["input_fingerprint"],
        )
        if existing:
            if existing != identity:
                raise MarketHistoryError("adjustment exclusion identity conflict")
            return {"status": "duplicate", "rows": 0}
        overlap = conn.execute(
            "SELECT count(*) FROM adjusted_price_exclusion WHERE symbol=? "
            "AND coverage_start<=? AND coverage_end>=? AND methodology_version=?",
            [
                result["symbol"],
                result["coverage_end"],
                result["coverage_start"],
                ADJUSTMENT_METHODOLOGY,
            ],
        ).fetchone()[0]
        if overlap:
            raise MarketHistoryError("overlapping adjustment exclusion conflict")
        conn.execute(
            "INSERT INTO adjusted_price_exclusion VALUES (?,?,?,?,?,?,?,?,?)",
            [
                result["symbol"],
                result["coverage_start"],
                result["coverage_end"],
                result["reason"],
                result["action_import_id"],
                result["action_source_row_hash"],
                result["input_fingerprint"],
                ADJUSTMENT_METHODOLOGY,
                calculated_at,
            ],
        )
        return {"status": "excluded", "rows": 0}
    if result["status"] != "READY":
        raise MarketHistoryError("unknown adjusted-price result status")
    exclusion_count = conn.execute(
        "SELECT count(*) FROM adjusted_price_exclusion WHERE symbol=? AND coverage_start<=? "
        "AND coverage_end>=? AND methodology_version=?",
        [
            result["symbol"],
            result["coverage_end"],
            result["coverage_start"],
            ADJUSTMENT_METHODOLOGY,
        ],
    ).fetchone()[0]
    if exclusion_count:
        raise MarketHistoryError("exclusion conflicts with adjusted rows")
    existing = conn.execute(
        "SELECT count(*),min(input_fingerprint),max(input_fingerprint) "
        "FROM adjusted_stock_price WHERE symbol=? AND trade_date BETWEEN ? AND ? "
        "AND methodology_version=?",
        [
            result["symbol"],
            result["coverage_start"],
            result["coverage_end"],
            ADJUSTMENT_METHODOLOGY,
        ],
    ).fetchone()
    if existing[0]:
        reconciliation = conn.execute(
            "SELECT count(*),min(input_fingerprint),max(input_fingerprint) "
            "FROM market_action_reconciliation WHERE symbol=? AND coverage_start=? "
            "AND coverage_end=? AND methodology_version=?",
            [
                result["symbol"],
                result["coverage_start"],
                result["coverage_end"],
                ADJUSTMENT_METHODOLOGY,
            ],
        ).fetchone()
        complete = (
            existing[0] == len(result["rows"])
            and reconciliation[0] == len(result["reconciliations"])
            and existing[1] == result["input_fingerprint"]
            and existing[2] == existing[1]
            and (not reconciliation[0] or reconciliation[1] == reconciliation[2] == existing[1])
        )
        if not complete:
            raise MarketHistoryError("adjusted-price replay fingerprint or count conflict")
        return {"status": "duplicate", "rows": existing[0]}
    conn.execute("BEGIN")
    try:
        for row in result["rows"]:
            conn.execute(
                "INSERT INTO adjusted_stock_price VALUES (?,?,?,?,?,?,?,?,?,?)",
                [
                    row["symbol"],
                    row["trade_date"],
                    row["adjusted_open"],
                    row["adjusted_high"],
                    row["adjusted_low"],
                    row["adjusted_close"],
                    row["cumulative_factor"],
                    ADJUSTMENT_METHODOLOGY,
                    result["input_fingerprint"],
                    calculated_at,
                ],
            )
        for row in result["reconciliations"]:
            conn.execute(
                "INSERT INTO market_action_reconciliation VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [
                    row["action_import_id"],
                    row["action_source_row_hash"],
                    row["symbol"],
                    result["coverage_start"],
                    result["coverage_end"],
                    row["previous_session"],
                    row["ex_session"],
                    row["raw_overnight_return"],
                    row["adjusted_overnight_return"],
                    ADJUSTMENT_METHODOLOGY,
                    result["input_fingerprint"],
                ],
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"status": "stored", "rows": len(result["rows"])}


def install_schema(conn: duckdb.DuckDBPyConnection) -> None:
    tables = {row[0] for row in conn.execute("SHOW TABLES").fetchall()}
    if "market_corporate_action" in tables:
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info('market_corporate_action')").fetchall()
        }
        legacy_reconciliation = False
        if "market_action_reconciliation" in tables:
            reconciliation_columns = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info('market_action_reconciliation')"
                ).fetchall()
            }
            legacy_reconciliation = "coverage_start" not in reconciliation_columns
        legacy_exclusion = False
        if "adjusted_price_exclusion" in tables:
            exclusion_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info('adjusted_price_exclusion')").fetchall()
            }
            legacy_exclusion = "input_fingerprint" not in exclusion_columns
        if "action_kind" not in columns or legacy_reconciliation or legacy_exclusion:
            raise RuntimeError(
                "pre-release disposable v21 market history must be recreated before v22"
            )
    conn.execute("BEGIN")
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


def store(conn: duckdb.DuckDBPyConnection, parsed: dict, fetched_at: dt | None = None) -> dict:
    allowed = {
        "NSE_CORPORATE_ACTION",
        "NSE_DELISTING",
        "NSE_SYMBOL_CHANGE",
        "NSE_NAME_CHANGE",
        "NSE_FILING_AVAILABILITY_LEGACY",
        "NSE_FILING_AVAILABILITY_INTEGRATED",
        "NSE_FILING_AVAILABILITY_SHAREHOLDING",
    }
    required = {
        "source_type",
        "source_url",
        "content_sha256",
        "source_fingerprint",
        "source_row_count",
        "duplicate_rows",
        "excluded_rows",
        "rows",
    }
    if not required <= set(parsed) or parsed.get("source_type") not in allowed:
        raise MarketHistoryError("parsed source type or structure is not allowlisted")
    fetched_at = fetched_at or dt.now(UTC)
    import_id = _hash(f"{parsed['source_type']}\n{parsed['content_sha256']}".encode())[:24]
    existing = conn.execute(
        "SELECT import_id FROM market_history_import WHERE source_type=? AND content_sha256=?",
        [parsed["source_type"], parsed["content_sha256"]],
    ).fetchone()
    if existing:
        return {"status": "duplicate", "import_id": existing[0], "rows": len(parsed["rows"])}
    conn.execute("BEGIN")
    try:
        conn.execute(
            "INSERT INTO market_history_import VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                import_id,
                parsed["source_type"],
                parsed["source_url"],
                parsed["content_sha256"],
                parsed["coverage_start"],
                parsed["coverage_end"],
                len(parsed["rows"]),
                parsed["source_row_count"],
                parsed["duplicate_rows"],
                len(parsed["excluded_rows"]),
                json.dumps(parsed["excluded_rows"], separators=(",", ":")),
                parsed["source_fingerprint"],
                fetched_at,
            ],
        )
        if parsed["source_type"] == "NSE_CORPORATE_ACTION":
            for row in parsed["rows"]:
                conn.execute(
                    "INSERT INTO market_corporate_action VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        import_id,
                        row["source_row_hash"],
                        row["symbol"],
                        row["isin"],
                        row["series"],
                        row["subject"],
                        row["ex_date"],
                        row["record_date"],
                        row["broadcast_at"],
                        row["face_value"],
                        row["action_kind"],
                        row["parse_status"],
                        row["structural_factor"],
                        row["cash_amount"],
                        row["parse_reason"],
                        row["raw_json"],
                    ],
                )
        elif parsed["source_type"].startswith("NSE_FILING_AVAILABILITY_"):
            for row in parsed["rows"]:
                conn.execute(
                    "INSERT INTO filing_availability VALUES (?,?,?,?,?,?,?,?,?)",
                    [
                        import_id,
                        row["source_row_hash"],
                        row["symbol"],
                        row["filing_type"],
                        row["xbrl_url"],
                        row["period_end"],
                        row["available_at"],
                        row["timestamp_field"],
                        row["raw_json"],
                    ],
                )
        elif parsed["source_type"] in {
            "NSE_DELISTING",
            "NSE_SYMBOL_CHANGE",
            "NSE_NAME_CHANGE",
        }:
            for row in parsed["rows"]:
                conn.execute(
                    "INSERT INTO security_lineage_event VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        import_id,
                        row["source_row_hash"],
                        row["event_type"],
                        row["effective_date"],
                        row["old_symbol"],
                        row["new_symbol"],
                        row["symbol"],
                        row["old_name"],
                        row["new_name"],
                        row["delisting_type"],
                        row["raw_row"],
                    ],
                )
        else:
            raise MarketHistoryError("source dispatch is not implemented")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"status": "stored", "import_id": import_id, "rows": len(parsed["rows"])}
