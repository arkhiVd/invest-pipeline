"""Official RBI-hosted dated USD/INR reference-rate import."""

from __future__ import annotations

import hashlib
import html
import re
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

RBI_ARCHIVE = "https://www.rbi.org.in/scripts/ReferenceRateArchive.aspx"
FBIL_START = date(2018, 7, 10)


class FxError(RuntimeError):
    pass


def parse_rbi_html(raw: bytes):
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FxError("RBI response is not UTF-8") from exc
    if "USD (INR / 1 USD)" not in text:
        raise FxError("RBI USD table missing")
    pairs = re.findall(
        r"<td[^>]*>\s*(\d{2}/\d{2}/\d{4})\s*</td>\s*"
        r"<td[^>]*align=\"right\"[^>]*>\s*([0-9]+(?:\.[0-9]+)?)\s*</td>",
        text,
        flags=re.IGNORECASE,
    )
    if not pairs:
        raise FxError("RBI USD rows missing")
    rows = []
    seen = set()
    for day_text, rate_text in pairs:
        day = datetime.strptime(day_text, "%d/%m/%Y").date()
        try:
            rate = Decimal(rate_text)
        except InvalidOperation as exc:
            raise FxError("invalid RBI rate") from exc
        if rate <= 0 or day in seen:
            raise FxError("invalid or duplicate RBI rate row")
        seen.add(day)
        rows.append(
            {
                "rate_date": day,
                "rate": rate,
                "source_authority": "FBIL" if day >= FBIL_START else "RBI",
            }
        )
    rows.sort(key=lambda row: row["rate_date"])
    return rows


def _hidden_fields(text: str):
    fields = {}
    for name, value in re.findall(r'<input type="hidden" name="([^"]+)"[^>]*value="([^"]*)"', text):
        fields[name] = html.unescape(value)
    required = {"__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION"}
    if not required <= set(fields):
        raise FxError("RBI form fields changed")
    return fields


def fetch_rbi(from_date: date, to_date: date, *, opener=None, timeout=30):
    if from_date > to_date:
        raise FxError("FX date range is invalid")
    opener = opener or urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
    headers = {"User-Agent": "Mozilla/5.0 invest-personal-accounting"}
    first = opener.open(urllib.request.Request(RBI_ARCHIVE, headers=headers), timeout=timeout)
    initial = first.read()
    fields = _hidden_fields(initial.decode("utf-8"))
    fields.update(
        {
            "chkUSD": "on",
            "txtFromDate": from_date.strftime("%d/%m/%Y"),
            "txtToDate": to_date.strftime("%d/%m/%Y"),
            "btnSubmit": " GO ",
        }
    )
    request = urllib.request.Request(
        RBI_ARCHIVE,
        data=urllib.parse.urlencode(fields).encode(),
        headers=headers,
    )
    response = opener.open(request, timeout=timeout)
    raw = response.read()
    rows = parse_rbi_html(raw)
    if rows[0]["rate_date"] < from_date or rows[-1]["rate_date"] > to_date:
        raise FxError("RBI returned rows outside requested range")
    return raw, rows


def store(conn, raw: bytes, rows: list[dict], *, fetched_at=None):
    fetched_at = fetched_at or datetime.now(UTC)
    digest = hashlib.sha256(raw).hexdigest()
    conn.execute("BEGIN")
    try:
        for row in rows:
            conn.execute(
                "INSERT INTO portfolio_fx_rate VALUES (?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                [
                    row["rate_date"],
                    "USD",
                    "INR",
                    row["rate"],
                    "RBI_REFERENCE_RATE_ARCHIVE",
                    row["source_authority"],
                    digest,
                    fetched_at,
                ],
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"rows": len(rows), "content_sha256": digest}


def rate_for_date(conn, day: date):
    row = conn.execute(
        "SELECT rate,rate_date,source_authority FROM portfolio_fx_rate "
        "WHERE base_currency='USD' AND quote_currency='INR' AND rate_date<=? "
        "ORDER BY rate_date DESC LIMIT 1",
        [day],
    ).fetchone()
    if not row:
        raise FxError("no official USD/INR rate on or before date")
    return {"rate": Decimal(str(row[0])), "rate_date": row[1], "source_authority": row[2]}
