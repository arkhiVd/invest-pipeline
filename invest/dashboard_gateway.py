"""Fail-closed authentication gateway for the Tailnet dashboard."""

from __future__ import annotations

import argparse
import asyncio
import base64
import getpass
import hashlib
import hmac
import html
import json
import os
import secrets
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web

COOKIE_NAME = "invest_session"
COOKIE_PATH = "/invest"
IDLE_SECONDS = 30 * 60
ABSOLUTE_SECONDS = 8 * 60 * 60
MAX_LOGIN_BYTES = 4096
SCRYPT_N = 1 << 15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_MAXMEM = 64 * 1024 * 1024
UPSTREAM = "http://127.0.0.1:8501"
DENIED = "Authentication required."
UNAVAILABLE = "Dashboard temporarily unavailable."
HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


class ConfigError(ValueError):
    """Protected authentication configuration is invalid."""


@dataclass(frozen=True)
class AuthConfig:
    username: str
    password_hash: str
    session_key: bytes


AUTH_KEY = web.AppKey("auth", AuthConfig)
CLIENT_KEY = web.AppKey("client", ClientSession)
REVOCATIONS_KEY = web.AppKey("revocations", dict[str, int])


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str, salt: bytes | None = None) -> str:
    if len(password) < 12:
        raise ConfigError("password must contain at least 12 characters")
    salt = salt or secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=32,
        maxmem=SCRYPT_MAXMEM,
    )
    return f"scrypt$v1${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(derived)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, version, n, r, p, salt, expected = encoded.split("$")
        if (algorithm, version, int(n), int(r), int(p)) != (
            "scrypt",
            "v1",
            SCRYPT_N,
            SCRYPT_R,
            SCRYPT_P,
        ):
            return False
        actual = hashlib.scrypt(
            password.encode(),
            salt=_unb64(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=32,
            maxmem=SCRYPT_MAXMEM,
        )
        return hmac.compare_digest(actual, _unb64(expected))
    except (ValueError, TypeError):
        return False


def load_config(path: Path) -> AuthConfig:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise ConfigError("credential file must be a regular mode-0600 file")
        if info.st_uid != os.geteuid():
            raise ConfigError("credential file must be owned by the service account")
        raw = json.loads(path.read_text())
        if set(raw) != {"username", "password_hash", "session_key"}:
            raise ConfigError("credential file has unexpected keys")
        username = raw["username"]
        password_hash = raw["password_hash"]
        session_key = _unb64(raw["session_key"])
        if not isinstance(username, str) or not username or len(username) > 128:
            raise ConfigError("invalid username")
        parts = password_hash.split("$") if isinstance(password_hash, str) else []
        if len(parts) != 7 or parts[:5] != [
            "scrypt",
            "v1",
            str(SCRYPT_N),
            str(SCRYPT_R),
            str(SCRYPT_P),
        ]:
            raise ConfigError("invalid password hash")
        if len(_unb64(parts[5])) != 16 or len(_unb64(parts[6])) != 32:
            raise ConfigError("invalid password hash")
        if len(session_key) != 32:
            raise ConfigError("invalid session key")
        return AuthConfig(username, password_hash, session_key)
    except ConfigError:
        raise
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ConfigError("credential file is unavailable or malformed") from exc


def write_config(path: Path) -> None:
    username = input("Dashboard username: ").strip()
    if not username or len(username) > 128:
        raise ConfigError("username must contain 1-128 characters")
    password = getpass.getpass("Dashboard password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if not hmac.compare_digest(password, confirmation):
        raise ConfigError("passwords do not match")
    payload = {
        "username": username,
        "password_hash": hash_password(password),
        "session_key": _b64(secrets.token_bytes(32)),
    }
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _sign(config: AuthConfig, issued: int, last: int, nonce: str) -> str:
    payload = f"v1.{issued}.{last}.{nonce}"
    signature = _b64(hmac.digest(config.session_key, payload.encode(), "sha256"))
    return f"{payload}.{signature}"


def valid_session(
    config: AuthConfig, token: str | None, now: int | None = None
) -> tuple[int, int, str] | None:
    try:
        version, issued_raw, last_raw, nonce, signature = (token or "").split(".")
        issued, last = int(issued_raw), int(last_raw)
        payload = f"{version}.{issued}.{last}.{nonce}"
        expected = _b64(hmac.digest(config.session_key, payload.encode(), "sha256"))
        current = int(time.time()) if now is None else now
        if version != "v1" or not hmac.compare_digest(signature, expected):
            return None
        if issued > current + 60 or last < issued or current - last > IDLE_SECONDS:
            return None
        if current - issued > ABSOLUTE_SECONDS or len(nonce) < 16:
            return None
        return issued, last, nonce
    except (ValueError, TypeError):
        return None


def _login_page(message: str = "", status: int = 200) -> web.Response:
    notice = f"<p>{html.escape(message)}</p>" if message else ""
    body = f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content=\"width=device-width,initial-scale=1\"><title>Invest login</title>
<style>body{{font:16px system-ui;max-width:24rem;margin:10vh auto;padding:1rem}}
label,input,button{{display:block;width:100%;margin:.7rem 0}}
input,button{{box-sizing:border-box;padding:.7rem}}</style></head>
<body><h1>Invest dashboard</h1>{notice}<form method=post action=\"{COOKIE_PATH}/login\">
<label>Username<input name=username autocomplete=username required maxlength=128></label>
<label>Password<input name=password type=password autocomplete=current-password required></label>
<button type=submit>Sign in</button></form></body></html>"""
    return web.Response(
        text=body,
        status=status,
        content_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


def _set_cookie(response: web.StreamResponse, token: str, max_age: int = IDLE_SECONDS) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=max_age,
        secure=True,
        httponly=True,
        samesite="Strict",
        path=COOKIE_PATH,
    )


def _external_path(request: web.Request) -> str:
    path = request.path
    return path if path.startswith(COOKIE_PATH) else f"{COOKIE_PATH}{path if path != '/' else '/'}"


async def login(request: web.Request) -> web.Response:
    if request.content_length is not None and request.content_length > MAX_LOGIN_BYTES:
        return web.Response(text=DENIED, status=413)
    try:
        form = await request.post()
        username, password = str(form.get("username", "")), str(form.get("password", ""))
    except (ValueError, OSError):
        return web.Response(text=DENIED, status=400)
    config = request.app[AUTH_KEY]
    user_ok = hmac.compare_digest(username.encode(), config.username.encode())
    password_ok = verify_password(password, config.password_hash)
    if not (user_ok and password_ok):
        return _login_page(DENIED, status=401)
    now = int(time.time())
    response = web.HTTPSeeOther(location=f"{COOKIE_PATH}/")
    _set_cookie(response, _sign(config, now, now, secrets.token_urlsafe(18)))
    raise response


async def logout(request: web.Request) -> web.Response:
    config = request.app[AUTH_KEY]
    session = valid_session(config, request.cookies.get(COOKIE_NAME))
    if session is not None:
        issued, _, nonce = session
        request.app[REVOCATIONS_KEY][nonce] = issued + ABSOLUTE_SECONDS
    response = _login_page("Signed out.")
    response.del_cookie(
        COOKIE_NAME, path=COOKIE_PATH, secure=True, httponly=True, samesite="Strict"
    )
    return response


def _filtered_headers(headers) -> dict[str, str]:
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in HOP_HEADERS and key.lower() != "host"
    }


def _upstream_headers(request: web.Request) -> dict[str, str]:
    headers = {
        key: value
        for key, value in _filtered_headers(request.headers).items()
        if key.lower() not in {"authorization", "cookie"}
    }
    upstream_cookies = [
        f"{name}={value}" for name, value in request.cookies.items() if name != COOKIE_NAME
    ]
    if upstream_cookies:
        headers["Cookie"] = "; ".join(upstream_cookies)
    return headers


async def _proxy_websocket(
    request: web.Request, target: str, expires_at: int
) -> web.StreamResponse:
    protocols = tuple(
        value.strip()
        for value in request.headers.get("Sec-WebSocket-Protocol", "").split(",")
        if value.strip()
    )
    headers = {
        key: value
        for key, value in _upstream_headers(request).items()
        if not key.lower().startswith("sec-websocket-") and key.lower() != "origin"
    }
    try:
        async with request.app[CLIENT_KEY].ws_connect(
            target,
            headers=headers,
            protocols=protocols,
            origin=UPSTREAM,
            compress=0,
            max_msg_size=16 * 1024 * 1024,
        ) as upstream:
            selected = (upstream.protocol,) if upstream.protocol else ()
            downstream = web.WebSocketResponse(
                protocols=selected, max_msg_size=16 * 1024 * 1024, compress=False
            )
            await downstream.prepare(request)

            async def relay(source, destination):
                async for message in source:
                    if message.type == WSMsgType.TEXT:
                        await destination.send_str(message.data)
                    elif message.type == WSMsgType.BINARY:
                        await destination.send_bytes(message.data)
                    elif message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                        break

            async def expire():
                await asyncio.sleep(max(0, expires_at - time.time()))

            tasks = {
                asyncio.create_task(relay(downstream, upstream)),
                asyncio.create_task(relay(upstream, downstream)),
                asyncio.create_task(expire()),
            }
            _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            await downstream.close(code=1000, message=b"session ended")
            return downstream
    except Exception:
        return web.Response(text=UNAVAILABLE, status=502, headers={"Cache-Control": "no-store"})


async def proxy(request: web.Request) -> web.StreamResponse:
    config = request.app[AUTH_KEY]
    now = int(time.time())
    revocations = request.app[REVOCATIONS_KEY]
    revocations.update({nonce: expiry for nonce, expiry in revocations.items() if expiry > now})
    session = valid_session(config, request.cookies.get(COOKIE_NAME), now)
    if session is not None and session[2] in revocations:
        session = None
    if session is None:
        if request.method == "GET" and request.path.rstrip("/") in {"", COOKIE_PATH}:
            return _login_page()
        return web.Response(text=DENIED, status=401, headers={"Cache-Control": "no-store"})
    issued, last, nonce = session
    path = _external_path(request)
    target = f"{UPSTREAM}{path}"
    if request.query_string:
        target += f"?{request.query_string}"
    if request.headers.get("Upgrade", "").lower() == "websocket":
        expires_at = min(issued + ABSOLUTE_SECONDS, last + IDLE_SECONDS)
        return await _proxy_websocket(request, target, expires_at)
    try:
        async with request.app[CLIENT_KEY].request(
            request.method,
            target,
            headers=_upstream_headers(request),
            data=request.content,
            allow_redirects=False,
        ) as upstream:
            response = web.StreamResponse(
                status=upstream.status,
                headers=_filtered_headers(upstream.headers),
            )
            now = int(time.time())
            _set_cookie(response, _sign(config, issued, now, nonce))
            await response.prepare(request)
            async for chunk in upstream.content.iter_chunked(64 * 1024):
                await response.write(chunk)
            await response.write_eof()
            return response
    except Exception:
        return web.Response(text=UNAVAILABLE, status=502, headers={"Cache-Control": "no-store"})


async def _client_context(app: web.Application):
    timeout = ClientTimeout(total=None, connect=5, sock_read=300)
    app[CLIENT_KEY] = ClientSession(timeout=timeout, auto_decompress=False)
    yield
    await app[CLIENT_KEY].close()


async def login_page(_: web.Request) -> web.Response:
    return _login_page()


def runtime_config(config: AuthConfig) -> AuthConfig:
    boot_key = hmac.digest(config.session_key, secrets.token_bytes(32), "sha256")
    return AuthConfig(config.username, config.password_hash, boot_key)


def create_app(config: AuthConfig) -> web.Application:
    app = web.Application(client_max_size=16 * 1024 * 1024, handler_args={"access_log": None})
    app[AUTH_KEY] = config
    app[REVOCATIONS_KEY] = {}
    app.cleanup_ctx.append(_client_context)
    app.router.add_get(f"{COOKIE_PATH}/login", login_page)
    app.router.add_post(f"{COOKIE_PATH}/login", login)
    app.router.add_post(f"{COOKIE_PATH}/logout", logout)
    app.router.add_get("/login", login_page)
    app.router.add_post("/login", login)
    app.router.add_post("/logout", logout)
    app.router.add_route("*", "/{path:.*}", proxy)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--init-credentials", action="store_true")
    parser.add_argument("--port", type=int, default=8502)
    args = parser.parse_args()
    if args.init_credentials:
        write_config(args.config)
        return
    config = runtime_config(load_config(args.config))
    web.run_app(create_app(config), host="127.0.0.1", port=args.port, access_log=None, print=None)


if __name__ == "__main__":
    main()
