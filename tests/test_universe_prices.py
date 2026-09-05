"""T3.2b universe and bhavcopy price ingest contracts; offline fixtures."""

import io
import urllib.error
import zipfile
from datetime import UTC, date
from datetime import datetime as dt

import duckdb
import pytest

from invest import db, prices, universe

FETCHED = dt(2026, 8, 25, 12, tzinfo=UTC)

UNIVERSE_CSV = (
    b"SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT,"
    b" ISIN NUMBER, FACE VALUE\n"
    b"ZEAL,Zeal Synthetic Industries,EQ,06-OCT-2008,5,1,INE000Z01001,10\n"
    b"TESTLTD,Test Limited,BE,01-JAN-2020,1,1,INE999A01010,1\n"
)

BHAV_CSV = (
    "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,"
    "FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,"
    "LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnInt,TtlTradgVol,"
    "TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4\n"
    "2026-08-21,2026-08-21,CM,NSE,STK,2885,INE000Z01001,ZEAL,EQ,,,,,,1400.0,1425.0,"
    "1395.0,1421.35,1418.0,1408.9,,1421.0,,,651,10210251.76,88,F1,1,,,,,,\n"
    "2026-08-21,2026-08-21,CM,NSE,STK,19078,IN0020200104,SGBJUN28,GB,,,,,,15455.01,15769.0,"
    "15400.01,15728.16,15768.99,15400.01,,15774.45,,,205,3221024.50,54,F1,1,,,,,,\n"
)


def bhav_zip(csv_text: str = BHAV_CSV) -> bytes:
    buffer = io.BytesIO()
    name = "BhavCopy_NSE_CM_0_0_0_20260821_F_0000.csv"
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr(name, csv_text)
    return buffer.getvalue()


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size):
        return self.body


class FakeOpener:
    """Stands in for a urlopen-style opener object with .open()."""

    def __init__(self, bodies_by_stamp: dict[str, bytes | int]):
        self.bodies = bodies_by_stamp
        self.requested: list[str] = []

    def open(self, req, timeout=30):  # noqa: ARG002 - signature mirrors urllib
        stamp = req.full_url.split("_")[6]
        self.requested.append(stamp)
        payload = self.bodies[stamp]
        if isinstance(payload, int):  # HTTP status code
            raise urllib.error.HTTPError(req.full_url, payload, "nf", {}, None)
        return FakeResponse(payload)


class FixedBodyOpener:
    """Returns one fixed body/status for every requested URL."""

    def __init__(self, body: bytes | int):
        self.body = body

    def open(self, req, timeout=30):  # noqa: ARG002 - signature mirrors urllib
        if isinstance(self.body, int):
            raise urllib.error.HTTPError(req.full_url, self.body, "nf", {}, None)
        return FakeResponse(self.body)


@pytest.fixture()
def conn():
    c = duckdb.connect()
    db.init_schema(c)
    yield c
    c.close()


def test_universe_rows_are_cleaned_and_validated():
    rows = universe.fetch_universe_csv(opener=FixedBodyOpener(UNIVERSE_CSV))
    assert [r["symbol"] for r in rows] == ["ZEAL", "TESTLTD"]
    reliance = rows[0]
    assert reliance["series"] == "EQ"
    assert reliance["listing_date"] == date(2008, 10, 6)
    assert reliance["isin"] == "INE000Z01001"
    assert reliance["face_value"] == pytest.approx(10.0)


def test_universe_rejects_contract_drift():
    drifted = b"symbol,company\nFOO,Bar\n"
    with pytest.raises(universe.SourceError, match="contract changed"):
        universe.fetch_universe_csv(opener=FixedBodyOpener(drifted))


def test_universe_store_is_idempotent(conn):
    rows = [
        {
            "symbol": "ZEAL",
            "company_name": "Zeal Synthetic Industries",
            "series": "EQ",
            "isin": "INE000Z01001",
            "listing_date": date(2008, 10, 6),
            "face_value": 10.0,
            "source": universe.SOURCE,
        }
    ]
    assert universe.store(conn, rows, fetched_at=FETCHED) == 1
    before = db.fingerprint(conn, "stock_universe")
    universe.store(conn, rows, fetched_at=dt(2026, 8, 25, 13, tzinfo=UTC))
    assert db.fingerprint(conn, "stock_universe") == before


def test_bhavcopy_parse_keeps_stocks_and_drops_bonds():
    bars = prices.parse_bhavcopy(bhav_zip())
    assert [b["symbol"] for b in bars] == ["ZEAL"]
    bar = bars[0]
    assert bar["trade_date"] == date(2026, 8, 21)
    assert bar["close"] == pytest.approx(1421.35)
    assert bar["prev_close"] == pytest.approx(1408.9)
    assert bar["volume"] == 651


def test_price_upsert_is_idempotent_but_updates_changed_close(conn):
    bars = prices.parse_bhavcopy(bhav_zip())
    assert db.upsert_prices(conn, bars, source=prices.SOURCE, fetched_at=FETCHED) == 1
    before = db.fingerprint(conn, "stock_price")
    db.upsert_prices(conn, bars, source=prices.SOURCE, fetched_at=dt(2026, 8, 25, 13, tzinfo=UTC))
    assert db.fingerprint(conn, "stock_price") == before
    corrected = [dict(bars[0], close=1430.0)]
    db.upsert_prices(conn, corrected, source=prices.SOURCE, fetched_at=FETCHED)
    (close,) = conn.execute("SELECT close FROM stock_price WHERE symbol='ZEAL'").fetchone()
    assert close == pytest.approx(1430.0)


def test_backfill_advances_through_interior_holiday_and_holds_tail(conn):
    # Fri Aug 21 real session; Sat/Sun weekend; Mon Aug 24 holiday (404);
    # Tue Aug 25 tail not yet published (404) -> must stay pending for retry.
    stamps = {
        "20260821": bhav_zip(),
        "20260824": 404,
        "20260825": 404,
    }
    total = prices.backfill(
        conn,
        date(2026, 8, 21),
        date(2026, 8, 25),
        fetched_at=FETCHED,
        opener=FakeOpener(stamps),
    )
    assert total == 1
    watermark, detail = conn.execute(
        "SELECT last_date, detail FROM ingest_watermark WHERE kind=?",
        [prices.WATERMARK_KIND],
    ).fetchone()
    assert watermark == date(2026, 8, 24)
    assert detail.startswith("non-trading")
    (count,) = conn.execute("SELECT COUNT(*) FROM stock_price").fetchone()
    assert count == 1


def test_zero_equity_rows_fail_loudly_instead_of_silent_skip(conn):
    bonds_only = "\n".join(
        line for line in BHAV_CSV.splitlines() if ",GB," in line or line.startswith("TradDt")
    )
    stamps = {"20260821": bhav_zip(), "20260824": bhav_zip(bonds_only)}
    with pytest.raises(prices.SourceError, match="zero equity-series rows"):
        prices.backfill(
            conn,
            date(2026, 8, 21),
            date(2026, 8, 24),
            fetched_at=FETCHED,
            opener=FakeOpener(stamps),
        )


def test_backfill_never_regresses_watermark_on_explicit_rerun(conn):
    opener = FakeOpener({"20260820": 404, "20260821": bhav_zip(), "20260824": bhav_zip()})
    prices.backfill(conn, date(2026, 8, 21), date(2026, 8, 24), fetched_at=FETCHED, opener=opener)
    first = db.get_watermark(conn, prices.WATERMARK_KIND)
    prices.backfill(conn, date(2026, 8, 20), date(2026, 8, 22), fetched_at=FETCHED, opener=opener)
    assert db.get_watermark(conn, prices.WATERMARK_KIND) >= first
