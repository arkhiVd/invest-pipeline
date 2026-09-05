"""T11.4 official dated FX parser and calendar fixtures."""

from datetime import date
from decimal import Decimal

import duckdb
import pytest

from invest import accounting, db, fx

HTML = b"""
<html><body><table>
<tr><td><b>Date</b></td><td><b>USD (INR / 1 USD)</b></td></tr>
<tr><td>09/07/2018</td><td align="right">68.5000</td></tr>
<tr><td>10/07/2018</td><td align="right">68.6200</td></tr>
<tr><td>13/07/2018</td><td align="right">68.5300</td></tr>
</table></body></html>
"""


def test_rbi_parser_labels_authority_boundary():
    rows = fx.parse_rbi_html(HTML)
    assert [(row["rate_date"], row["source_authority"]) for row in rows] == [
        (date(2018, 7, 9), "RBI"),
        (date(2018, 7, 10), "FBIL"),
        (date(2018, 7, 13), "FBIL"),
    ]


def test_previous_official_rate_calendar_rule_and_replay():
    conn = duckdb.connect()
    db.init_schema(conn)
    accounting.install_schema(conn)
    rows = fx.parse_rbi_html(HTML)
    first = fx.store(conn, HTML, rows)
    second = fx.store(conn, HTML, rows)
    assert first == second
    assert conn.execute("SELECT count(*) FROM portfolio_fx_rate").fetchone()[0] == 3
    saturday = fx.rate_for_date(conn, date(2018, 7, 14))
    assert saturday == {
        "rate": Decimal("68.5300000000"),
        "rate_date": date(2018, 7, 13),
        "source_authority": "FBIL",
    }
    with pytest.raises(fx.FxError, match="no official"):
        fx.rate_for_date(conn, date(2018, 7, 8))
    conn.close()


def test_rbi_parser_rejects_duplicate_and_missing_rows():
    duplicate = HTML.replace(
        b"</table>", b'<tr><td>10/07/2018</td><td align="right">68.62</td></tr></table>'
    )
    with pytest.raises(fx.FxError, match="duplicate"):
        fx.parse_rbi_html(duplicate)
    with pytest.raises(fx.FxError, match="table missing"):
        fx.parse_rbi_html(b"<html></html>")
