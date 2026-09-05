import hashlib
import json
import stat
from datetime import UTC
from datetime import datetime as dt
from urllib import error

import duckdb
import pytest

from invest import db, kite

NOW = dt(2026, 8, 27, 10, tzinfo=UTC)
HOLDING = {
    "exchange": "NSE",
    "tradingsymbol": "ACME",
    "product": "CNC",
    "instrument_token": 408065,
    "isin": "INE000A01001",
    "quantity": 10,
    "t1_quantity": 1,
    "used_quantity": 0,
    "average_price": 1400.0,
    "last_price": 1500.0,
    "close_price": 1490.0,
    "pnl": 1000.0,
    "day_change": 10.0,
    "day_change_percentage": 0.67,
}
MF_HOLDING = {
    "folio": "12345",
    "fund": "Example Direct Growth",
    "tradingsymbol": "INF123A01017",
    "average_price": 10.0,
    "last_price": 12.0,
    "last_price_date": "2026-08-26",
    "pledged_quantity": 0,
    "pnl": 20.0,
    "quantity": 10.0,
}
POSITION = {
    "exchange": "NSE",
    "tradingsymbol": "ACME",
    "product": "CNC",
    "instrument_token": 408065,
    "quantity": 10,
    "overnight_quantity": 10,
    "multiplier": 1,
    "average_price": 1400.0,
    "last_price": 1500.0,
    "close_price": 1490.0,
    "pnl": 1000.0,
    "m2m": 100.0,
    "unrealised": 1000.0,
    "realised": 0.0,
    "buy_quantity": 10,
    "buy_price": 1400.0,
    "buy_value": 14000.0,
    "sell_quantity": 0,
    "sell_price": 0.0,
    "sell_value": 0.0,
}


def response(data):
    return json.dumps({"status": "success", "data": data}).encode()


def test_login_exchange_uses_only_session_route_and_correct_checksum():
    seen = []

    def opener(req):
        seen.append(req)
        return response({"access_token": "access"})

    token = kite.exchange_token("key", "secret", "request", opener=opener)
    assert token == "access"
    req = seen[0]
    assert req.full_url == f"{kite.API_BASE}{kite.SESSION_PATH}"
    assert req.get_method() == "POST"
    body = dict(item.split("=") for item in req.data.decode().split("&"))
    assert body["checksum"] == hashlib.sha256(b"keyrequestsecret").hexdigest()
    assert "secret" not in req.data.decode()
    with pytest.raises(kite.KiteError, match="invalid access_token"):
        kite.exchange_token(
            "key",
            "secret",
            "request",
            opener=lambda _req: response({"access_token": " "}),
        )
    with pytest.raises(ValueError, match="valid request_token"):
        kite.exchange_token("key", "secret", "bad token", opener=opener)


def test_redirect_parser_accepts_token_or_complete_url():
    assert kite.request_token_from_redirect("abc") == "abc"
    assert (
        kite.request_token_from_redirect("https://local/callback?status=success&request_token=abc")
        == "abc"
    )
    with pytest.raises(ValueError, match="one request_token"):
        kite.request_token_from_redirect("https://local/callback?status=success")


def test_read_client_enforces_exact_get_allowlist_and_headers():
    seen = []
    payloads = {
        kite.PROFILE_PATH: {"user_id": "AB1234"},
        kite.HOLDINGS_PATH: [HOLDING],
        kite.POSITIONS_PATH: {"net": [POSITION], "day": []},
        kite.MF_HOLDINGS_PATH: [MF_HOLDING],
    }

    def opener(req):
        seen.append(req)
        path = req.full_url.removeprefix(kite.API_BASE)
        return response(payloads[path])

    client = kite.ReadClient("key", "access", opener=opener)
    assert client.profile()["user_id"] == "AB1234"
    assert client.holdings()[0]["tradingsymbol"] == "ACME"
    assert len(client.positions()["net"]) == 1
    assert client.mutual_funds()[0]["fund"] == "Example Direct Growth"
    assert all(req.get_method() == "GET" for req in seen)
    assert {req.full_url.removeprefix(kite.API_BASE) for req in seen} == kite.READ_PATHS
    assert all(req.headers["Authorization"] == "token key:access" for req in seen)
    with pytest.raises(ValueError, match="allowlist"):
        client.get("/portfolio/not-allowed")


def test_auth_and_malformed_responses_fail_closed():
    client = kite.ReadClient(
        "key",
        "access",
        opener=lambda _req: json.dumps(
            {"status": "error", "error_type": "TokenException", "message": "expired"}
        ).encode(),
    )
    with pytest.raises(kite.AuthExpired, match="expired"):
        client.profile()
    with pytest.raises(kite.KiteError, match="not JSON"):
        kite.ReadClient("key", "access", opener=lambda _req: b"bad").profile()
    bad = kite.ReadClient("key", "access", opener=lambda _req: response([]))
    with pytest.raises(kite.KiteError, match="profile"):
        bad.profile()


def test_http_auth_failure_and_source_failure_are_bounded():
    def fail_auth(req):
        raise error.HTTPError(req.full_url, 403, "forbidden", {}, None)

    with pytest.raises(kite.AuthExpired, match="403"):
        kite.ReadClient("key", "access", opener=fail_auth).profile()

    def fail_source(_req):
        raise error.URLError("private diagnostic")

    with pytest.raises(kite.KiteError, match="source unavailable: URLError") as caught:
        kite.ReadClient("key", "access", opener=fail_source).profile()
    assert "private diagnostic" not in str(caught.value)


def test_credentials_and_access_token_stay_in_separate_protected_files(tmp_path):
    env = tmp_path / "invest.env"
    env.write_text("KITE_API_KEY='key'\nKITE_API_SECRET=secret\nOTHER=x\n")
    assert kite.load_credentials(env) == ("key", "secret")
    token_file = tmp_path / "private" / "token"
    kite.save_access_token("access", token_file)
    assert kite.load_access_token(token_file) == "access"
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    kite.save_access_token("replacement", token_file)
    assert kite.load_access_token(token_file) == "replacement"
    assert not list(token_file.parent.glob("*.tmp.*"))
    token_file.chmod(0o644)
    with pytest.raises(kite.AuthExpired, match="mode-0600"):
        kite.load_access_token(token_file)
    with pytest.raises(ValueError, match="invalid"):
        kite.save_access_token("bad token", token_file)


def test_mutual_fund_folios_aggregate_without_storing_folio_identity():
    second = {
        **MF_HOLDING,
        "folio": "67890",
        "quantity": 30.0,
        "average_price": 20.0,
        "pnl": 60.0,
    }
    rows = kite.aggregate_mf_holdings([MF_HOLDING, second])
    assert len(rows) == 1
    assert rows[0]["quantity"] == 40.0
    assert rows[0]["average_price"] == pytest.approx(17.5)
    assert rows[0]["pnl"] == 80.0
    assert "folio" not in rows[0]


def test_snapshot_is_atomic_idempotent_and_keeps_account_private():
    conn = duckdb.connect()
    db.init_schema(conn)
    first = kite.store_snapshot(
        conn,
        {"user_id": "AB1234"},
        [HOLDING],
        {"net": [POSITION], "day": []},
        [MF_HOLDING],
        fetched_at=NOW,
    )
    replay = kite.store_snapshot(
        conn,
        {"user_id": "AB1234"},
        [HOLDING],
        {"net": [POSITION], "day": []},
        [MF_HOLDING],
        fetched_at=NOW,
    )
    assert first["status"] == "stored"
    assert replay == {**first, "status": "duplicate"}
    assert conn.execute("SELECT COUNT(*) FROM broker_snapshot_run").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM broker_holding").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM broker_position").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM broker_mf_holding").fetchone()[0] == 1
    account = conn.execute("SELECT account_sha256 FROM broker_snapshot_run").fetchone()[0]
    assert account == hashlib.sha256(b"AB1234").hexdigest()
    assert "AB1234" not in db.fingerprint(conn, "broker_snapshot_run")
    assert kite.snapshot_integrity(conn, first["run_id"])
    conn.execute(
        "UPDATE broker_holding SET last_price=last_price+1 WHERE run_id=?",
        [first["run_id"]],
    )
    assert not kite.snapshot_integrity(conn, first["run_id"])
    conn.close()


def test_invalid_input_and_database_child_failure_publish_nothing():
    conn = duckdb.connect()
    db.init_schema(conn)
    bad = {**HOLDING, "last_price": float("nan")}
    with pytest.raises(kite.KiteError, match="finite"):
        kite.store_snapshot(
            conn,
            {"user_id": "AB1234"},
            [bad],
            {"net": [], "day": []},
            [],
            fetched_at=NOW,
        )
    with pytest.raises(kite.KiteError, match="net and day"):
        kite.store_snapshot(conn, {"user_id": "AB1234"}, [], {"net": []}, [], fetched_at=NOW)
    with pytest.raises(kite.KiteError, match="isin"):
        kite.normalize_holding({**HOLDING, "isin": 123})
    with pytest.raises(kite.KiteError, match="isin"):
        kite.normalize_holding({**HOLDING, "isin": " "})
    # Duplicate child keys fail after the parent and first child INSERT. The
    # explicit transaction must roll back both rows.
    with pytest.raises(duckdb.ConstraintException):
        kite.store_snapshot(
            conn,
            {"user_id": "AB1234"},
            [HOLDING, HOLDING],
            {"net": [], "day": []},
            [],
            fetched_at=NOW,
        )
    assert conn.execute("SELECT COUNT(*) FROM broker_snapshot_run").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM broker_holding").fetchone()[0] == 0
    conn.close()
