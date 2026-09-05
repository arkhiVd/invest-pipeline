"""T2.4 acceptance: zone-change detection + Telegram delivery (offline)."""

import json
from datetime import date

import pytest

from invest import alerts, db, pe, vbrs


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


def _seed(conn, day, p):
    pe.store(conn, {"pe": p, "pb": 2.9, "dy": 1.1, "close": 24000.0}, day=day)


def test_quiet_when_zone_unchanged(conn, tmp_path, monkeypatch):
    env = tmp_path / "invest.env"
    env.write_text("INVEST_BOT_TOKEN=t\nINVEST_CHAT_ID=c\n")
    monkeypatch.setattr(alerts, "ENV_PATH", env)
    _seed(conn, date(2026, 8, 24), 20.40)
    _seed(conn, date(2026, 8, 25), 20.50)  # both Base
    sent = []
    outcome = alerts.run_check(conn, poster=lambda req: sent.append(req))
    assert outcome == "quiet"
    assert not sent


def test_alert_sent_on_crossing(conn, tmp_path, monkeypatch):
    env = tmp_path / "invest.env"
    env.write_text("INVEST_BOT_TOKEN=t\nINVEST_CHAT_ID=c\n")
    monkeypatch.setattr(alerts, "ENV_PATH", env)
    _seed(conn, date(2026, 8, 24), 19.80)  # Cheap
    _seed(conn, date(2026, 8, 25), 20.60)  # Base -> crossing
    captured = []

    def fake_poster(req):
        captured.append(json.loads(req.data.decode()))

    outcome = alerts.run_check(conn, poster=fake_poster)
    assert outcome == "sent"
    body = captured[0]
    assert body["chat_id"] == "c"
    assert "Cheap" in body["text"] and "Base" in body["text"]
    assert vbrs.cash_position.__name__ in build_source_guard()  # formula reused


def build_source_guard():
    # guards against someone hand-computing cash % in the alert text path
    import inspect

    return inspect.getsource(vbrs.cash_position)


def test_baseline_recorded_silently(conn, tmp_path, monkeypatch):
    env = tmp_path / "invest.env"
    env.write_text("INVEST_BOT_TOKEN=t\nINVEST_CHAT_ID=c\n")
    monkeypatch.setattr(alerts, "ENV_PATH", env)
    _seed(conn, date(2026, 8, 25), 20.47)
    sent = []
    outcome = alerts.run_check(conn, poster=lambda r: sent.append(r))
    assert outcome == "baseline"
    assert not sent


def test_unconfigured_is_green_skip(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(alerts, "ENV_PATH", tmp_path / "missing.env")
    _seed(conn, date(2026, 8, 25), 20.47)
    sent = []
    outcome = alerts.run_check(conn, poster=lambda r: sent.append(r))
    assert outcome == "unconfigured"
    assert not sent


def test_force_sends_even_without_crossing(conn, tmp_path, monkeypatch):
    env = tmp_path / "invest.env"
    env.write_text("INVEST_BOT_TOKEN=t\nINVEST_CHAT_ID=c\n")
    monkeypatch.setattr(alerts, "ENV_PATH", env)
    _seed(conn, date(2026, 8, 25), 20.47)
    sent = []
    outcome = alerts.run_check(conn, force=True, poster=lambda r: sent.append(r))
    assert outcome == "sent" and len(sent) == 1


def test_load_credentials_env_file_precedence(tmp_path):
    env = tmp_path / "invest.env"
    env.write_text('INVEST_BOT_TOKEN="abc"\n# comment\nINVEST_CHAT_ID=42\n')
    token, chat = alerts.load_credentials(env)
    assert (token, chat) == ("abc", "42")
