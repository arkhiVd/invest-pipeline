from __future__ import annotations

from io import BytesIO
from urllib import request

import pytest

from invest import kite


def test_demo_mode_refuses_real_broker_network(monkeypatch):
    monkeypatch.setenv("INVEST_MODE", "demo")
    monkeypatch.delenv(kite.LIVE_READ_OPT_IN, raising=False)
    req = request.Request(f"{kite.API_BASE}{kite.HOLDINGS_PATH}")
    with pytest.raises(kite.KiteError, match="disabled in demo"):
        kite._open(req)


def test_live_mode_still_requires_separate_read_acknowledgement(monkeypatch):
    monkeypatch.setenv("INVEST_MODE", "live")
    monkeypatch.delenv(kite.LIVE_READ_OPT_IN, raising=False)
    req = request.Request(f"{kite.API_BASE}{kite.HOLDINGS_PATH}")
    with pytest.raises(kite.KiteError, match="explicit"):
        kite._open(req)


def test_injected_client_remains_offline(monkeypatch):
    monkeypatch.setenv("INVEST_MODE", "test")
    response = BytesIO(b'{"status":"success","data":[]}')
    assert kite._open(request.Request("https://example.invalid"), opener=lambda _: response)
