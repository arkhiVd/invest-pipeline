from __future__ import annotations

import asyncio
import json
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from invest import dashboard_gateway as gateway


@pytest.fixture
def password():
    return secrets.token_urlsafe(24)


@pytest.fixture
def auth(password):
    return gateway.AuthConfig("operator", gateway.hash_password(password), b"k" * 32)


def test_password_hash_is_salted_and_verifiable(password):
    first = gateway.hash_password(password)
    second = gateway.hash_password(password)
    assert first != second
    assert gateway.verify_password(password, first)
    assert not gateway.verify_password(secrets.token_urlsafe(24), first)


def test_load_config_requires_exact_keys_and_mode_0600(tmp_path: Path, password):
    path = tmp_path / "credentials.json"
    path.write_text(
        json.dumps(
            {
                "username": "operator",
                "password_hash": gateway.hash_password(password),
                "session_key": gateway._b64(b"k" * 32),
            }
        )
    )
    path.chmod(0o600)
    assert gateway.load_config(path).username == "operator"
    path.chmod(0o640)
    with pytest.raises(gateway.ConfigError, match="mode-0600"):
        gateway.load_config(path)


def test_credential_writer_is_atomic_mode_0600_and_silent(tmp_path, monkeypatch, capsys, password):
    path = tmp_path / "protected" / "auth.json"
    monkeypatch.setattr("builtins.input", lambda _: "operator")
    monkeypatch.setattr(gateway.getpass, "getpass", lambda _: password)
    gateway.write_config(path)
    assert path.stat().st_mode & 0o777 == 0o600
    assert capsys.readouterr().out == ""
    loaded = gateway.load_config(path)
    assert loaded.username == "operator"
    assert gateway.verify_password(password, loaded.password_hash)


def test_session_enforces_signature_idle_absolute_and_rotation(auth):
    now = int(time.time())
    token = gateway._sign(auth, now, now, "long-enough-random-nonce")
    assert gateway.valid_session(auth, token, now)
    assert gateway.valid_session(auth, token + "x", now) is None
    assert gateway.valid_session(auth, token, now + gateway.IDLE_SECONDS + 1) is None
    old = gateway._sign(auth, now - gateway.ABSOLUTE_SECONDS - 1, now, "long-enough-random-nonce")
    assert gateway.valid_session(auth, old, now) is None
    rotated = gateway.AuthConfig(auth.username, auth.password_hash, b"z" * 32)
    assert gateway.valid_session(rotated, token, now) is None
    restarted = gateway.runtime_config(auth)
    assert gateway.valid_session(restarted, token, now) is None


def test_every_non_login_route_denies_without_session(auth):
    async def check():
        async with TestClient(TestServer(gateway.create_app(auth))) as client:
            root = await client.get("/invest/", allow_redirects=False)
            assert root.status == 200
            assert "Invest dashboard" in await root.text()
            for path in ("/invest/deep-link", "/invest/static/app.js", "/invest/_stcore/stream"):
                response = await client.get(path)
                assert response.status == 401
                assert await response.text() == gateway.DENIED

    asyncio.run(check())


def test_login_cookie_logout_and_wrong_credentials(auth, password):
    async def check():
        async with TestClient(TestServer(gateway.create_app(auth))) as client:
            wrong = await client.post(
                "/invest/login",
                data={"username": "operator", "password": secrets.token_urlsafe(24)},
            )
            assert wrong.status == 401
            assert gateway.COOKIE_NAME not in wrong.cookies
            response = await client.post(
                "/invest/login",
                data={"username": "operator", "password": password},
                allow_redirects=False,
            )
            cookie = response.cookies[gateway.COOKIE_NAME]
            assert response.status == 303
            assert cookie["secure"] and cookie["httponly"]
            assert cookie["samesite"] == "Strict"
            assert cookie["path"] == "/invest"
            token = cookie.value
            session_headers = {"Cookie": f"{gateway.COOKIE_NAME}={token}"}
            logged_out = await client.post("/invest/logout", headers=session_headers)
            assert logged_out.cookies[gateway.COOKIE_NAME]["max-age"] == "0"
            replay = await client.get("/invest/deep-link", headers=session_headers)
            assert replay.status == 401

    asyncio.run(check())


def test_authenticated_http_and_websocket_proxy(monkeypatch, auth):
    async def http_handler(request):
        return web.json_response(
            {
                "path": request.path,
                "cookie": request.headers.get("Cookie"),
                "authorization": request.headers.get("Authorization"),
            }
        )

    async def websocket_handler(request):
        socket = web.WebSocketResponse(protocols=("streamlit",))
        await socket.prepare(request)
        async for message in socket:
            if message.type == WSMsgType.TEXT:
                await socket.send_str(f"echo:{message.data}")
        return socket

    async def check():
        upstream_app = web.Application()
        upstream_app.router.add_get("/invest/value", http_handler)
        upstream_app.router.add_get("/invest/_stcore/stream", websocket_handler)
        async with TestServer(upstream_app) as upstream:
            monkeypatch.setattr(gateway, "UPSTREAM", str(upstream.make_url("/")).rstrip("/"))
            async with TestClient(TestServer(gateway.create_app(auth))) as client:
                now = int(time.time())
                token = gateway._sign(auth, now, now, "long-enough-random-nonce")
                headers = {
                    "Cookie": f"{gateway.COOKIE_NAME}={token}; streamlit_xsrf=kept",
                    "Authorization": "Bearer must-not-reach-upstream",
                }
                response = await client.get("/invest/value", headers=headers)
                assert response.status == 200
                payload = await response.json()
                assert payload == {
                    "path": "/invest/value",
                    "cookie": "streamlit_xsrf=kept",
                    "authorization": None,
                }
                async with client.ws_connect(
                    "/invest/_stcore/stream", headers=headers, protocols=("streamlit", "xsrf")
                ) as socket:
                    assert socket.protocol == "streamlit"
                    await socket.send_str("safe")
                    message = await socket.receive()
                    assert message.data == "echo:safe"

    asyncio.run(check())


def test_proxy_negotiates_with_real_streamlit(monkeypatch, auth):
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            "invest/dashboard.py",
            "--server.address=127.0.0.1",
            f"--server.port={port}",
            "--server.baseUrlPath=invest",
            "--server.headless=true",
            "--browser.gatherUsageStats=false",
            "--server.fileWatcherType=none",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    async def check():
        monkeypatch.setattr(gateway, "UPSTREAM", f"http://127.0.0.1:{port}")
        async with TestClient(TestServer(gateway.create_app(auth))) as client:
            now = int(time.time())
            token = gateway._sign(auth, now, now, "long-enough-random-nonce")
            headers = {"Cookie": f"{gateway.COOKIE_NAME}={token}"}
            for _ in range(50):
                response = await client.get("/invest/_stcore/health", headers=headers)
                if response.status == 200:
                    break
                await asyncio.sleep(0.1)
            else:
                pytest.fail("Streamlit did not become ready")
            async with client.ws_connect(
                "/invest/_stcore/stream", headers=headers, protocols=("streamlit",)
            ) as websocket:
                assert websocket.protocol == "streamlit"

    try:
        asyncio.run(check())
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_established_websocket_closes_at_session_expiry(monkeypatch, auth):
    async def websocket_handler(request):
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        async for _ in socket:
            pass
        return socket

    async def check():
        upstream_app = web.Application()
        upstream_app.router.add_get("/invest/_stcore/stream", websocket_handler)
        async with TestServer(upstream_app) as upstream:
            monkeypatch.setattr(gateway, "UPSTREAM", str(upstream.make_url("/")).rstrip("/"))
            monkeypatch.setattr(gateway, "IDLE_SECONDS", 1)
            async with TestClient(TestServer(gateway.create_app(auth))) as client:
                now = int(time.time())
                token = gateway._sign(auth, now, now, "long-enough-random-nonce")
                async with client.ws_connect(
                    "/invest/_stcore/stream",
                    headers={"Cookie": f"{gateway.COOKIE_NAME}={token}"},
                ) as socket:
                    message = await asyncio.wait_for(socket.receive(), timeout=2)
                    assert message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED}

    asyncio.run(check())


def test_upstream_failure_response_is_non_sensitive(monkeypatch, auth):
    async def check():
        monkeypatch.setattr(gateway, "UPSTREAM", "http://127.0.0.1:1")
        async with TestClient(TestServer(gateway.create_app(auth))) as client:
            now = int(time.time())
            token = gateway._sign(auth, now, now, "long-enough-random-nonce")
            response = await client.get(
                "/invest/value", headers={"Cookie": f"{gateway.COOKIE_NAME}={token}"}
            )
            assert response.status == 502
            assert await response.text() == gateway.UNAVAILABLE

    asyncio.run(check())
    assert "127.0.0.1" not in gateway.UNAVAILABLE
