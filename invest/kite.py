"""Read-only Zerodha Personal API adapter and atomic portfolio snapshots."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from datetime import UTC, date
from datetime import datetime as dt
from pathlib import Path
from urllib import error, parse, request

from invest import db, prices

API_BASE = "https://api.kite.trade"
LOGIN_BASE = "https://kite.zerodha.com/connect/login"
PROFILE_PATH = "/user/profile"
HOLDINGS_PATH = "/portfolio/holdings"
POSITIONS_PATH = "/portfolio/positions"
MF_HOLDINGS_PATH = "/mf/holdings"
SESSION_PATH = "/session/token"
READ_PATHS = frozenset((PROFILE_PATH, HOLDINGS_PATH, POSITIONS_PATH, MF_HOLDINGS_PATH))
HOLDING_FIELDS = (
    "exchange",
    "tradingsymbol",
    "product",
    "instrument_token",
    "isin",
    "quantity",
    "t1_quantity",
    "used_quantity",
    "average_price",
    "last_price",
    "close_price",
    "pnl",
    "day_change",
    "day_change_percentage",
)
MF_HOLDING_FIELDS = (
    "tradingsymbol",
    "fund",
    "quantity",
    "pledged_quantity",
    "average_price",
    "last_price",
    "pnl",
    "last_price_date",
)
POSITION_FIELDS = (
    "scope",
    "exchange",
    "tradingsymbol",
    "product",
    "instrument_token",
    "quantity",
    "overnight_quantity",
    "multiplier",
    "average_price",
    "last_price",
    "close_price",
    "pnl",
    "m2m",
    "unrealised",
    "realised",
    "buy_quantity",
    "buy_price",
    "buy_value",
    "sell_quantity",
    "sell_price",
    "sell_value",
)
ENV_PATH = Path(os.environ.get("INVEST_ENV", ".env"))
TOKEN_PATH = Path(os.environ.get("KITE_TOKEN_FILE", "data/kite-access-token"))
LIVE_READ_OPT_IN = "INVEST_ALLOW_LIVE_BROKER_READS"


class KiteError(RuntimeError):
    """Base error for source, auth, and response-contract failures."""


class AuthExpired(KiteError):
    """The Personal API access token is absent, expired, or invalid."""


def _decode_response(raw: bytes) -> object:
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise KiteError(f"Kite response is not JSON: {type(exc).__name__}") from exc
    if not isinstance(payload, dict):
        raise KiteError("Kite response must be an object")
    if payload.get("status") != "success":
        kind = payload.get("error_type")
        message = payload.get("message") or "unspecified API error"
        if kind == "TokenException":
            raise AuthExpired(message)
        raise KiteError(f"Kite API error {kind or 'unknown'}: {message}")
    if "data" not in payload:
        raise KiteError("Kite response has no data field")
    return payload["data"]


def login_url(api_key: str) -> str:
    if not api_key:
        raise ValueError("api_key is required")
    return f"{LOGIN_BASE}?{parse.urlencode({'v': 3, 'api_key': api_key})}"


def request_token_from_redirect(value: str) -> str:
    """Accept either the token itself or the complete registered redirect URL."""
    if not value:
        raise ValueError("request token is required")
    if "://" not in value:
        return value
    tokens = parse.parse_qs(parse.urlsplit(value).query).get("request_token", [])
    if len(tokens) != 1 or not tokens[0]:
        raise ValueError("redirect URL must contain one request_token")
    return tokens[0]


def exchange_token(api_key: str, api_secret: str, request_token: str, *, opener=None) -> str:
    """Exchange one operator-supplied login token at the sole allowed POST route."""
    if not api_key or not api_secret or not _valid_token(request_token):
        raise ValueError("api_key, api_secret, and a valid request_token are required")
    checksum = hashlib.sha256(f"{api_key}{request_token}{api_secret}".encode()).hexdigest()
    body = parse.urlencode(
        {"api_key": api_key, "request_token": request_token, "checksum": checksum}
    ).encode()
    req = request.Request(
        f"{API_BASE}{SESSION_PATH}",
        data=body,
        headers={"X-Kite-Version": "3", "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        raw = _open(req, opener=opener)
    except error.HTTPError as exc:
        if exc.code in (401, 403):
            raise AuthExpired(f"token exchange HTTP {exc.code}") from exc
        raise KiteError(f"token exchange HTTP {exc.code}") from exc
    data = _decode_response(raw)
    if not isinstance(data, dict):
        raise KiteError("token exchange data must be an object")
    token = data.get("access_token")
    if not _valid_token(token):
        raise KiteError("token exchange has an invalid access_token")
    return token


def _open(req: request.Request, *, opener=None) -> bytes:
    try:
        if opener is not None:
            response = opener(req)
            raw = response.read() if hasattr(response, "read") else response
        else:
            if os.environ.get("INVEST_MODE", "demo").lower() in {"demo", "test"}:
                raise KiteError("broker network access is disabled in demo and test modes")
            if os.environ.get(LIVE_READ_OPT_IN) != "READ_ONLY_ACKNOWLEDGED":
                raise KiteError(f"broker reads require explicit {LIVE_READ_OPT_IN} opt-in")
            with request.urlopen(req, timeout=30) as response:
                raw = response.read()
    except error.HTTPError:
        raise
    except (error.URLError, TimeoutError, OSError) as exc:
        raise KiteError(f"Kite source unavailable: {type(exc).__name__}") from exc
    if not isinstance(raw, bytes):
        raise KiteError("Kite source returned a non-bytes response")
    return raw


class ReadClient:
    def __init__(self, api_key: str, access_token: str, *, opener=None):
        if not api_key or not access_token:
            raise AuthExpired("Kite API key and access token are required")
        self.api_key = api_key
        self.access_token = access_token
        self.opener = opener

    def get(self, path: str) -> object:
        if path not in READ_PATHS:
            raise ValueError("Kite path is outside the read allowlist")
        req = request.Request(
            f"{API_BASE}{path}",
            headers={
                "Authorization": f"token {self.api_key}:{self.access_token}",
                "X-Kite-Version": "3",
            },
            method="GET",
        )
        try:
            raw = _open(req, opener=self.opener)
        except error.HTTPError as exc:
            if exc.code in (401, 403):
                raise AuthExpired(f"Kite read HTTP {exc.code}") from exc
            raise KiteError(f"Kite read HTTP {exc.code}") from exc
        return _decode_response(raw)

    def profile(self) -> dict:
        data = self.get(PROFILE_PATH)
        if not isinstance(data, dict) or not isinstance(data.get("user_id"), str):
            raise KiteError("profile must contain user_id")
        return data

    def holdings(self) -> list[dict]:
        data = self.get(HOLDINGS_PATH)
        if not isinstance(data, list):
            raise KiteError("holdings data must be a list")
        return data

    def mutual_funds(self) -> list[dict]:
        data = self.get(MF_HOLDINGS_PATH)
        if not isinstance(data, list):
            raise KiteError("mutual-fund holdings data must be a list")
        return data

    def positions(self) -> dict[str, list[dict]]:
        data = self.get(POSITIONS_PATH)
        if not isinstance(data, dict) or set(data) != {"net", "day"}:
            raise KiteError("positions data must contain exactly net and day lists")
        if not all(isinstance(data[key], list) for key in ("net", "day")):
            raise KiteError("position scopes must be lists")
        return data


def _text(row: dict, key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise KiteError(f"{key} must be a non-empty string")
    return value.strip()


def _optional_isin(row: dict) -> str | None:
    value = row.get("isin")
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", value):
        raise KiteError("isin must be a valid 12-character ISIN or null")
    return value


def _number(row: dict, key: str, *, default=None) -> float:
    value = row.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise KiteError(f"{key} must be numeric")
    value = float(value)
    if not math.isfinite(value):
        raise KiteError(f"{key} must be finite")
    return value


def _optional_int(row: dict, key: str) -> int | None:
    value = row.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise KiteError(f"{key} must be an integer or null")
    return value


def normalize_holding(row: object) -> dict:
    if not isinstance(row, dict):
        raise KiteError("holding row must be an object")
    return {
        "exchange": _text(row, "exchange"),
        "tradingsymbol": _text(row, "tradingsymbol"),
        "product": _text(row, "product"),
        "instrument_token": _optional_int(row, "instrument_token"),
        "isin": _optional_isin(row),
        "quantity": _number(row, "quantity"),
        "t1_quantity": _number(row, "t1_quantity", default=0),
        "used_quantity": _number(row, "used_quantity", default=0),
        "average_price": _number(row, "average_price"),
        "last_price": _number(row, "last_price"),
        "close_price": _number(row, "close_price"),
        "pnl": _number(row, "pnl"),
        "day_change": _number(row, "day_change"),
        "day_change_percentage": _number(row, "day_change_percentage"),
    }


def normalize_mf_holding(row: object) -> dict:
    if not isinstance(row, dict):
        raise KiteError("mutual-fund holding row must be an object")
    symbol = _text(row, "tradingsymbol")
    if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", symbol):
        raise KiteError("mutual-fund tradingsymbol must be an ISIN")
    raw_date = row.get("last_price_date")
    try:
        last_price_date = date.fromisoformat(raw_date) if raw_date else None
    except (TypeError, ValueError) as exc:
        raise KiteError("last_price_date must be ISO date or empty") from exc
    return {
        "tradingsymbol": symbol,
        "fund": _text(row, "fund"),
        "quantity": _number(row, "quantity"),
        "pledged_quantity": _number(row, "pledged_quantity", default=0),
        "average_price": _number(row, "average_price"),
        "last_price": _number(row, "last_price"),
        "pnl": _number(row, "pnl"),
        "last_price_date": last_price_date,
    }


def aggregate_mf_holdings(rows: list) -> list[dict]:
    grouped: dict[tuple[str, str], list[dict]] = {}
    for raw in rows:
        row = normalize_mf_holding(raw)
        if row["quantity"] < 0 or row["pledged_quantity"] < 0:
            raise KiteError("mutual-fund quantities must be non-negative")
        grouped.setdefault((row["tradingsymbol"], row["fund"]), []).append(row)
    result = []
    for key, items in sorted(grouped.items()):
        first = items[0]
        if any(
            item["last_price"] != first["last_price"]
            or item["last_price_date"] != first["last_price_date"]
            for item in items[1:]
        ):
            raise KiteError("mutual-fund folios disagree on last price or date")
        quantity = sum(item["quantity"] for item in items)
        if quantity:
            average_price = (
                sum(item["average_price"] * item["quantity"] for item in items) / quantity
            )
        else:
            prices_seen = {item["average_price"] for item in items}
            if len(prices_seen) != 1:
                raise KiteError("zero-quantity mutual-fund folios disagree on average price")
            average_price = first["average_price"]
        result.append(
            {
                "tradingsymbol": key[0],
                "fund": key[1],
                "quantity": quantity,
                "pledged_quantity": sum(item["pledged_quantity"] for item in items),
                "average_price": average_price,
                "last_price": first["last_price"],
                "pnl": sum(item["pnl"] for item in items),
                "last_price_date": first["last_price_date"],
            }
        )
    return result


def normalize_position(row: object, *, scope: str) -> dict:
    if not isinstance(row, dict):
        raise KiteError("position row must be an object")
    if scope not in ("net", "day"):
        raise KiteError("position scope must be net or day")
    keys = (
        "quantity",
        "overnight_quantity",
        "multiplier",
        "average_price",
        "last_price",
        "close_price",
        "pnl",
        "m2m",
        "unrealised",
        "realised",
        "buy_quantity",
        "buy_price",
        "buy_value",
        "sell_quantity",
        "sell_price",
        "sell_value",
    )
    result = {
        "scope": scope,
        "exchange": _text(row, "exchange"),
        "tradingsymbol": _text(row, "tradingsymbol"),
        "product": _text(row, "product"),
        "instrument_token": _optional_int(row, "instrument_token"),
    }
    result.update({key: _number(row, key) for key in keys})
    return result


def store_snapshot(
    conn, profile: dict, holdings: list, positions: dict, mutual_funds: list, *, fetched_at: dt
) -> dict:
    if fetched_at.tzinfo is None:
        raise ValueError("fetched_at must be timezone-aware")
    if not isinstance(profile, dict):
        raise KiteError("profile must be an object")
    if not isinstance(holdings, list):
        raise KiteError("holdings data must be a list")
    if not isinstance(mutual_funds, list):
        raise KiteError("mutual-fund holdings data must be a list")
    if not isinstance(positions, dict) or set(positions) != {"net", "day"}:
        raise KiteError("positions data must contain exactly net and day lists")
    if not all(isinstance(positions[key], list) for key in ("net", "day")):
        raise KiteError("position scopes must be lists")
    user_id = profile.get("user_id")
    if not isinstance(user_id, str) or not user_id:
        raise KiteError("profile must contain user_id")
    normalized_holdings = sorted(
        (normalize_holding(row) for row in holdings),
        key=lambda row: (row["exchange"], row["tradingsymbol"], row["product"]),
    )
    normalized_mutual_funds = aggregate_mf_holdings(mutual_funds)
    normalized_positions = sorted(
        (
            normalize_position(row, scope=scope)
            for scope in ("net", "day")
            for row in positions.get(scope, [])
        ),
        key=lambda row: (row["scope"], row["exchange"], row["tradingsymbol"], row["product"]),
    )
    canonical = json.dumps(
        {
            "holdings": normalized_holdings,
            "mutual_funds": normalized_mutual_funds,
            "positions": normalized_positions,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    content_sha256 = hashlib.sha256(canonical.encode()).hexdigest()
    account_sha256 = hashlib.sha256(user_id.encode()).hexdigest()
    snapshot_date = fetched_at.astimezone(prices.IST).date()
    run_id = hashlib.sha256(
        f"zerodha:{account_sha256}:{snapshot_date}:{content_sha256}".encode()
    ).hexdigest()[:24]
    if conn.execute("SELECT 1 FROM broker_snapshot_run WHERE run_id=?", [run_id]).fetchone():
        return {"run_id": run_id, "status": "duplicate", "content_sha256": content_sha256}
    conn.execute("BEGIN TRANSACTION")
    try:
        conn.execute(
            "INSERT INTO broker_snapshot_run "
            "(run_id, broker, account_sha256, snapshot_date, content_sha256, "
            "holding_count, position_count, mf_holding_count, fetched_at) "
            "VALUES (?, 'zerodha', ?, ?, ?, ?, ?, ?, ?)",
            [
                run_id,
                account_sha256,
                snapshot_date,
                content_sha256,
                len(normalized_holdings),
                len(normalized_positions),
                len(normalized_mutual_funds),
                fetched_at.astimezone(UTC),
            ],
        )
        for row in normalized_holdings:
            conn.execute(
                "INSERT INTO broker_holding VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [run_id, *(row[key] for key in HOLDING_FIELDS)],
            )
        for row in normalized_mutual_funds:
            conn.execute(
                "INSERT INTO broker_mf_holding VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [run_id, *(row[key] for key in MF_HOLDING_FIELDS)],
            )
        for row in normalized_positions:
            conn.execute(
                "INSERT INTO broker_position VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [run_id, *(row[key] for key in POSITION_FIELDS)],
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"run_id": run_id, "status": "stored", "content_sha256": content_sha256}


def snapshot_integrity(conn, run_id: str) -> bool:
    parent = conn.execute(
        "SELECT content_sha256, holding_count, position_count, mf_holding_count "
        "FROM broker_snapshot_run WHERE run_id=?",
        [run_id],
    ).fetchone()
    if parent is None:
        return False
    holdings = [
        dict(zip(HOLDING_FIELDS, row, strict=True))
        for row in conn.execute(
            "SELECT " + ", ".join(HOLDING_FIELDS) + " FROM broker_holding "
            "WHERE run_id=? ORDER BY exchange, tradingsymbol, product",
            [run_id],
        ).fetchall()
    ]
    mutual_funds = [
        dict(zip(MF_HOLDING_FIELDS, row, strict=True))
        for row in conn.execute(
            "SELECT " + ", ".join(MF_HOLDING_FIELDS) + " FROM broker_mf_holding "
            "WHERE run_id=? ORDER BY tradingsymbol, fund",
            [run_id],
        ).fetchall()
    ]
    positions = [
        dict(zip(POSITION_FIELDS, row, strict=True))
        for row in conn.execute(
            "SELECT " + ", ".join(POSITION_FIELDS) + " FROM broker_position "
            "WHERE run_id=? ORDER BY scope, exchange, tradingsymbol, product",
            [run_id],
        ).fetchall()
    ]
    canonical = json.dumps(
        {"holdings": holdings, "mutual_funds": mutual_funds, "positions": positions},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    fingerprint = hashlib.sha256(canonical.encode()).hexdigest()
    counts_match = (
        len(holdings) == parent[1]
        and len(positions) == parent[2]
        and len(mutual_funds) == parent[3]
    )
    if fingerprint == parent[0] and counts_match:
        return True
    # v14 snapshots predate the Coin child table and hashed only equity and
    # position rows. Preserve verification for those migrated rows.
    if parent[3] == 0 and not mutual_funds:
        legacy = json.dumps(
            {"holdings": holdings, "positions": positions},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(legacy.encode()).hexdigest() == parent[0] and counts_match
    return False


def sync(conn, client: ReadClient, *, fetched_at: dt | None = None) -> dict:
    fetched_at = fetched_at or dt.now(UTC)
    profile = client.profile()
    holdings = client.holdings()
    positions = client.positions()
    mutual_funds = client.mutual_funds()
    return store_snapshot(conn, profile, holdings, positions, mutual_funds, fetched_at=fetched_at)


def load_credentials(path: Path | None = None) -> tuple[str | None, str | None]:
    """Load the Personal API key and secret without reading an access token."""
    path = path or ENV_PATH
    values = {
        "KITE_API_KEY": os.environ.get("KITE_API_KEY"),
        "KITE_API_SECRET": os.environ.get("KITE_API_SECRET"),
    }
    if path.is_file():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key in values and not values[key]:
                values[key] = value.strip().strip('"').strip("'") or None
    return values["KITE_API_KEY"], values["KITE_API_SECRET"]


def _valid_token(token: object) -> bool:
    return (
        isinstance(token, str)
        and bool(token)
        and token == token.strip()
        and all(character.isprintable() and not character.isspace() for character in token)
    )


def save_access_token(token: str, path: Path | None = None) -> None:
    if not _valid_token(token):
        raise ValueError("access token is invalid")
    path = path or TOKEN_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def load_access_token(path: Path | None = None) -> str:
    path = path or TOKEN_PATH
    try:
        metadata = path.stat()
        if not path.is_file() or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise AuthExpired("Kite access token file must be a regular mode-0600 file")
        raw = path.read_text(encoding="utf-8")
        token = raw.strip()
    except FileNotFoundError as exc:
        raise AuthExpired("Kite access token file is missing; complete login first") from exc
    if raw not in (token, token + "\n") or not _valid_token(token):
        raise AuthExpired("Kite access token file contains an invalid token")
    return token


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="invest-kite")
    parser.add_argument("command", choices=("login-url", "exchange", "sync"))
    parser.add_argument("--db", default="data/invest.duckdb")
    parser.add_argument("--env", type=Path, default=ENV_PATH)
    parser.add_argument("--token-file", type=Path, default=TOKEN_PATH)
    parser.add_argument("--request-token")
    args = parser.parse_args(argv)
    try:
        api_key, api_secret = load_credentials(args.env)
        if not api_key:
            raise ValueError("KITE_API_KEY is not configured")
        if args.command == "login-url":
            print(login_url(api_key))
            return 0
        if args.command == "exchange":
            if not api_secret:
                raise ValueError("KITE_API_SECRET is not configured")
            supplied = args.request_token or input("Paste redirect URL or request token: ").strip()
            token = exchange_token(api_key, api_secret, request_token_from_redirect(supplied))
            save_access_token(token, args.token_file)
            print(f"Kite access token stored in {args.token_file}")
            return 0
        conn = db.connect(args.db)
        try:
            db.init_schema(conn)
            result = sync(conn, ReadClient(api_key, load_access_token(args.token_file)))
            counts = conn.execute(
                "SELECT holding_count, position_count, mf_holding_count "
                "FROM broker_snapshot_run WHERE run_id=?",
                [result["run_id"]],
            ).fetchone()
        finally:
            conn.close()
        print(
            f"Kite snapshot {result['status']}: run={result['run_id']} "
            f"holdings={counts[0]} positions={counts[1]} mutual_funds={counts[2]}"
        )
        return 0
    except (AuthExpired, KiteError, OSError, ValueError) as exc:
        print(f"Kite sync failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
