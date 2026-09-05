import json
from datetime import UTC, date, timedelta
from datetime import datetime as dt
from urllib import error

import duckdb
import pytest

from invest import db, watchlist

NOW = dt(2026, 8, 26, tzinfo=UTC)
CONFIG = {
    "universe_index": "NIFTY 100",
    "benchmark": "NIFTY 50",
    "window_days": 252,
    "min_observations": 3,
    "max_price": 3000.0,
    "top_n": 2,
    "max_price_age_days": 3,
    "constituent_min_count": 1,
}

CONSTITUENT_CSV = (
    b"Company Name,Industry,Series,Symbol,ISIN Code\n"
    b"Alpha Ltd.,IT,EQ,ALPHA,INE111A01017\n"
    b"Beta Ltd.,IT,EQ,BETA,INE222B01019\n"
)
INDEX_CSV = (
    b"Index Name,Index Date,Closing Index Value\n"
    b"NIFTY 100,2026-01-05,25000.0\n"
    b"NIFTY 50,2026-01-05,21737.0\n"
)


def connection():
    conn = duckdb.connect()
    db.init_schema(conn)
    return conn


def store_prices(conn, symbol, pairs):
    conn.executemany(
        "INSERT INTO stock_price VALUES (?, ?, NULL, NULL, NULL, ?, NULL, NULL, 'fx', ?)",
        [(symbol, day, close, NOW) for day, close in pairs],
    )


def store_index(conn, pairs):
    conn.executemany(
        "INSERT INTO index_close VALUES ('NIFTY 50', ?, ?, 'fx', ?)",
        [(day, close, NOW) for day, close in pairs],
    )


def weekdays(start, count):
    days = []
    day = start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def test_parse_and_store_constituents_is_no_churn_on_replay():
    rows = watchlist.parse_constituents(CONSTITUENT_CSV)
    assert [row["symbol"] for row in rows] == ["ALPHA", "BETA"]
    conn = connection()
    first = watchlist.store_constituents(conn, "NIFTY 100", rows, fetched_at=NOW)
    assert first["members"] == 2 and first["written"] == 2
    before = db.fingerprint(conn, "index_constituent")
    replay = watchlist.store_constituents(
        conn, "NIFTY 100", watchlist.parse_constituents(CONSTITUENT_CSV), fetched_at=NOW
    )
    assert replay["written"] == 0
    assert db.fingerprint(conn, "index_constituent") == before
    shrunk = [row for row in rows if row["symbol"] != "BETA"]
    removal = watchlist.store_constituents(conn, "NIFTY 100", shrunk, fetched_at=NOW)
    assert removal["removed"] == 1
    assert conn.execute("SELECT COUNT(*) FROM index_constituent").fetchone()[0] == 1
    conn.close()


def test_parse_constituents_rejects_contract_drift():
    with pytest.raises(watchlist.SourceError):
        watchlist.parse_constituents(b"Name,Ticker\nAlpha,ALPHA\n")
    with pytest.raises(watchlist.SourceError):
        watchlist.parse_constituents(b"Company Name,Industry,Series,Symbol,ISIN Code\n")


def test_store_constituents_rejects_partial_and_non_eq_snapshots():
    rows = watchlist.parse_constituents(CONSTITUENT_CSV)
    conn = connection()
    with pytest.raises(watchlist.SourceError, match="minimum is 3"):
        watchlist.store_constituents(conn, "NIFTY 100", rows, fetched_at=NOW, minimum_count=3)
    bad_series = [{**rows[0], "series": "BE"}, rows[1]]
    with pytest.raises(watchlist.SourceError, match="non-EQ"):
        watchlist.store_constituents(conn, "NIFTY 100", bad_series, fetched_at=NOW, minimum_count=2)
    assert conn.execute("SELECT COUNT(*) FROM index_constituent").fetchone()[0] == 0
    conn.close()


def test_initial_index_range_can_satisfy_minimum_observations(monkeypatch):
    class FixedDateTime:
        @staticmethod
        def now(_tz):
            return dt(2026, 8, 26, tzinfo=UTC)

    monkeypatch.setattr(watchlist, "dt", FixedDateTime)
    conn = connection()
    start, end = watchlist.index_refresh_range(conn, min_observations=200)
    assert end == date(2026, 8, 25)
    assert (end - start).days == 340
    # Even after 40 weekday holidays/publication gaps, at least 200 sessions fit.
    weekdays_in_range = sum(
        1
        for offset in range((end - start).days + 1)
        if (start + timedelta(days=offset)).weekday() < 5
    )
    assert weekdays_in_range - 40 >= 200
    conn.close()


def test_parse_index_close_matches_official_title_case_and_fails_when_missing():
    live_style = INDEX_CSV.replace(b"NIFTY 50", b"Nifty 50")
    assert watchlist.parse_index_close(live_style, "NIFTY 50") == 21737.0
    with pytest.raises(watchlist.SourceError, match="missing"):
        watchlist.parse_index_close(live_style, "NIFTY 500")


def test_ingest_index_day_idempotent_and_watermark_advances():
    conn = connection()
    day = date(2026, 1, 5)
    assert watchlist.ingest_index_day(
        conn,
        day,
        index_name="NIFTY 50",
        fetched_at=NOW,
        opener=_opener_for({watchlist.INDEX_URL_TEMPLATE.format(stamp="05012026"): INDEX_CSV}),
    )
    before = db.fingerprint(conn, "index_close")
    assert watchlist.ingest_index_day(
        conn,
        day,
        index_name="NIFTY 50",
        fetched_at=NOW,
        opener=_opener_for({watchlist.INDEX_URL_TEMPLATE.format(stamp="05012026"): INDEX_CSV}),
    )
    assert db.fingerprint(conn, "index_close") == before
    assert db.get_watermark(conn, watchlist.WATERMARK_KIND) == day
    conn.close()


def test_interior_404_stays_pending_until_later_session_confirms():
    conn = connection()
    days = weekdays(date(2026, 1, 5), 2)
    urls = {
        watchlist.INDEX_URL_TEMPLATE.format(stamp=day.strftime("%d%m%Y")): INDEX_CSV for day in days
    }
    opener = _FakeOpener(urls, not_found=[next(iter(urls))])
    assert (
        watchlist.ingest_index_day(
            conn, days[0], index_name="NIFTY 50", fetched_at=NOW, opener=opener
        )
        is False
    )
    assert db.get_watermark(conn, watchlist.WATERMARK_KIND) is None
    assert (
        watchlist.ingest_index_day(
            conn, days[1], index_name="NIFTY 50", fetched_at=NOW, opener=opener
        )
        is True
    )
    assert db.get_watermark(conn, watchlist.WATERMARK_KIND) == days[1]
    conn.close()


class _FakeResponse:
    def __init__(self, body):
        self._body = body

    def read(self, _limit):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _FakeOpener:
    def __init__(self, payloads, not_found=()):
        self._payloads = payloads
        self._not_found = set(not_found)

    def open(self, request, timeout=None):  # noqa: ARG002
        if request.full_url in self._not_found:
            raise error.HTTPError(request.full_url, 404, "missing", {}, None)
        return _FakeResponse(self._payloads[request.full_url])


def _opener_for(payloads):
    return _FakeOpener(payloads)


def test_backfill_skips_weekends_and_validates_range():
    conn = connection()
    csv_by_url = {
        watchlist.INDEX_URL_TEMPLATE.format(stamp=day.strftime("%d%m%Y")): INDEX_CSV
        for day in weekdays(date(2026, 1, 5), 5)
    }
    stored = watchlist.backfill_index(
        conn,
        date(2026, 1, 5),
        date(2026, 1, 11),
        index_name="NIFTY 50",
        fetched_at=NOW,
        opener=_opener_for(csv_by_url),
    )
    assert stored == 5
    assert db.get_watermark(conn, watchlist.WATERMARK_KIND) == date(2026, 1, 11)
    with pytest.raises(ValueError, match="after end"):
        watchlist.backfill_index(
            conn,
            date(2026, 1, 10),
            date(2026, 1, 9),
            index_name="NIFTY 50",
            opener=None,
        )
    conn.close()


def test_beta_matches_hand_computed_leverage_of_two():
    conn = connection()
    days = weekdays(date(2026, 1, 5), 4)
    store_index(conn, list(zip(days, [100.0, 110.0, 99.0, 108.9], strict=True)))
    store_prices(conn, "LEVER", list(zip(days, [50.0, 60.0, 48.0, 57.6], strict=True)))
    value, observations = watchlist.beta(
        conn, "LEVER", benchmark="NIFTY 50", window_days=252, min_observations=3
    )
    assert value == pytest.approx(2.0)
    assert observations == 3
    conn.close()


def test_beta_negative_and_fail_closed_cases():
    conn = connection()
    days = weekdays(date(2026, 1, 5), 4)
    store_index(conn, list(zip(days, [100.0, 110.0, 99.0, 108.9], strict=True)))
    store_prices(conn, "INVERSE", list(zip(days, [50.0, 45.0, 49.5, 44.55], strict=True)))
    value, _ = watchlist.beta(
        conn, "INVERSE", benchmark="NIFTY 50", window_days=252, min_observations=3
    )
    assert value == pytest.approx(-1.0)
    store_prices(conn, "THINDATA", list(zip(days[:2], [5.0, 5.1], strict=True)))
    thin, thin_obs = watchlist.beta(
        conn, "THINDATA", benchmark="NIFTY 50", window_days=252, min_observations=3
    )
    assert thin is None and thin_obs == 1
    flat_days = weekdays(date(2026, 2, 2), 4)
    store_index(conn, [(day, 100.0) for day in flat_days])
    store_prices(conn, "FLATMKT", [(day, 10.0 + i) for i, day in enumerate(flat_days)])
    degenerate, _ = watchlist.beta(
        conn, "FLATMKT", benchmark="NIFTY 50", window_days=252, min_observations=3
    )
    assert degenerate is None
    conn.close()


def test_beta_drops_mismatched_horizon_pairs_not_silent_distortion():
    conn = connection()
    days = weekdays(date(2026, 3, 2), 5)
    market = [100.0, 110.0, 100.0, 105.0, 102.9]
    store_index(conn, list(zip(days, market, strict=True)))
    # Stock has no close on days[2]: only d2 and d5 pairs share base dates.
    stock = {days[0]: 50.0, days[1]: 60.0, days[3]: 80.0, days[4]: 76.8}
    store_prices(conn, "HALT", sorted(stock.items()))
    value, observations = watchlist.beta(
        conn, "HALT", benchmark="NIFTY 50", window_days=252, min_observations=2
    )
    assert observations == 2
    assert value == pytest.approx(2.0)
    conn.close()


def build_fixture_db():
    conn = connection()
    conn.execute(
        """
        INSERT INTO index_constituent VALUES
            ('NIFTY 100', 'BETAHI', 'A', NULL, 'I1', 'EQ', 'fx', ?),
            ('NIFTY 100', 'BETAMID', 'B', NULL, 'I2', 'EQ', 'fx', ?),
            ('NIFTY 100', 'BETALO', 'C', NULL, 'I3', 'EQ', 'fx', ?),
            ('NIFTY 100', 'PRICEOUT', 'D', NULL, 'I4', 'EQ', 'fx', ?),
            ('NIFTY 100', 'NOCLOSE', 'E', NULL, 'I5', 'EQ', 'fx', ?),
            ('NIFTY 100', 'THINDATA', 'F', NULL, 'I6', 'EQ', 'fx', ?),
            ('NIFTY 100', 'STALEOLD', 'G', NULL, 'I7', 'EQ', 'fx', ?)
        """,
        [NOW] * 7,
    )
    days = weekdays(date(2026, 1, 5), 8)
    market = [100.0 + i * 0.5 for i in range(len(days))]
    store_index(conn, list(zip(days, market, strict=True)))

    def scaled(k, base=100.0):
        closes = [base]
        for previous, current in zip(market, market[1:], strict=False):
            closes.append(closes[-1] * (1.0 + k * (current / previous - 1.0)))
        return closes

    series = {
        # exact-leverage series pin beta ordering: 3x > 1.5x > 0.75x.
        "BETAHI": scaled(3.0),
        "BETAMID": scaled(1.5),
        "BETALO": scaled(0.75),
        "PRICEOUT": [3000.0] * len(days),
        "THINDATA": [5.0, 5.1],
    }
    for symbol, closes in series.items():
        store_prices(conn, symbol, list(zip(days[: len(closes)], closes, strict=True)))
    # THINDATA's two closes sit at the fresh window edge so it fails the
    # beta-observation gate, not the staleness gate.
    conn.execute("DELETE FROM stock_price WHERE symbol = 'THINDATA'")
    store_prices(
        conn,
        "THINDATA",
        list(zip(days[-2:], [5.0, 5.1], strict=True)),
    )
    # Last close predates the fresh window by more than max_price_age_days=3.
    stale_days = weekdays(date(2026, 1, 5), 4)
    store_prices(conn, "STALEOLD", [(day, 42.0) for day in stale_days])
    return conn


def test_build_watchlist_ranks_filters_and_counts_gaps():
    conn = build_fixture_db()
    report = watchlist.build_watchlist(conn, CONFIG)
    assert [item["symbol"] for item in report["picks"]] == ["BETAHI", "BETAMID"]
    assert report["picks"][0]["beta"] > report["picks"][1]["beta"] > 1
    assert [item["rank"] for item in report["picks"]] == [1, 2]
    assert [item["symbol"] for item in report["excluded_by_price"]] == ["PRICEOUT"]
    assert report["gaps"]["no_close"] == ["NOCLOSE"]
    assert report["gaps"]["insufficient_beta"] == ["THINDATA"]
    assert report["gaps"]["stale_close"] == ["STALEOLD"]
    text = watchlist.render(report)
    assert "SWING WATCHLIST" in text and "price_excluded=1" in text
    assert "stale_close=1" in text
    conn.close()


def test_build_watchlist_requires_stored_constituents():
    conn = connection()
    with pytest.raises(ValueError, match="constituents"):
        watchlist.build_watchlist(conn, CONFIG)
    conn.close()


def test_cutoff_sql_excludes_later_close_and_beta_observations():
    conn = connection()
    days = weekdays(date(2026, 1, 5), 4)
    store_index(conn, list(zip(days, [100.0, 101.0, 102.0, 103.0], strict=True)))
    store_prices(conn, "SAFE", list(zip(days, [50.0, 51.0, 52.0, 53.0], strict=True)))
    cutoff = days[2]
    assert watchlist.latest_closes(conn, cutoff=cutoff)["SAFE"] == (cutoff, 52.0)
    _value, observations = watchlist.beta(
        conn,
        "SAFE",
        benchmark="NIFTY 50",
        window_days=252,
        min_observations=1,
        cutoff=cutoff,
    )
    assert observations == 2
    conn.close()


def test_load_config_validation(tmp_path):
    path = tmp_path / "swing.json"
    good = dict(CONFIG)
    path.write_text(json.dumps(good))
    assert watchlist.load_config(str(path)) == good
    bad_cases = [
        {k: v for k, v in good.items() if k != "top_n"},
        {**good, "window_days": 2, "min_observations": 3},
        {**good, "max_price": 0},
        {**good, "universe_index": ""},
    ]
    for bad in bad_cases:
        path.write_text(json.dumps(bad))
        with pytest.raises(ValueError):
            watchlist.load_config(str(path))
