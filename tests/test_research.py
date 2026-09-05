import json
from datetime import UTC, date
from datetime import datetime as dt

import duckdb
import pytest

from invest import db, research, research_digest

NOW = dt(2026, 8, 26, tzinfo=UTC)
CONFIG = {
    "backend": "codex_exec",
    "codex_bin": "/bin/echo",
    "model": "test-model",
    "max_calls_per_run": 2,
    "max_attempts_per_snapshot": 3,
    "min_total_score": 18,
}


def connection():
    conn = duckdb.connect()
    db.init_schema(conn)
    return conn


def candidate(symbol, day=date(2026, 8, 25)):
    return {
        "symbol": symbol,
        "snapshot_date": day,
        "screens": ["garp"],
        "metrics": {"pe_ratio": 12.0, "roe": 0.3},
    }


def reply(score=4):
    return json.dumps(
        {
            "components": {key: score for key in research.COMPONENTS},
            "rationale": "Strong supplied metrics, subject to filing evidence.",
            "red_flags": ["Check concentration risk"],
        }
    )


def test_candidates_are_union_of_deterministic_survivors_only(monkeypatch):
    universe = {
        "AAA": {"as_of": date(2026, 8, 25), "roe": 0.3, "pe_ratio": 12.0},
        "BBB": {"as_of": date(2026, 8, 25), "roe": 0.1, "pe_ratio": 40.0},
    }
    cfg = {
        "screens": {
            "quality": {"conditions": {"roe": {"gt": 0.2}}},
            "value": {"conditions": {"pe_ratio": {"lt": 20.0}}},
        }
    }
    monkeypatch.setattr(research.screens, "build_universe", lambda _conn: universe)
    found = research.candidates(object(), cfg)
    assert [item["symbol"] for item in found] == ["AAA"]
    assert found[0]["screens"] == ["quality", "value"]
    assert found[0]["metrics"]["pe_ratio"] == 12.0
    assert found[0]["metrics"]["roe"] == 0.3
    assert found[0]["metrics"]["free_cash_flow_3y"] is None
    assert set(research.RESEARCH_FIELDS) <= set(found[0]["metrics"])


def test_parse_reply_components_are_strict_and_total_is_python_computed():
    parsed = research.parse_reply(reply())
    assert parsed["total_score"] == 20
    bad = json.loads(reply())
    bad["components"]["valuation"] = 6
    with pytest.raises(ValueError, match="valuation"):
        research.parse_reply(json.dumps(bad))
    bad = json.loads(reply())
    bad["total_score"] = 25
    with pytest.raises(ValueError, match="top-level"):
        research.parse_reply(json.dumps(bad))


def test_budget_caps_actual_calls_and_records_run_count():
    conn = connection()
    calls = []

    def ask(prompt, config):
        calls.append((prompt, config["model"]))
        return reply()

    result = research.score_candidates(
        conn,
        [candidate("AAA"), candidate("BBB"), candidate("CCC")],
        CONFIG,
        ask=ask,
        now=NOW,
    )
    assert result["attempted_calls"] == 2
    assert result["stored_scores"] == 2
    assert len(calls) == CONFIG["max_calls_per_run"]
    run = conn.execute(
        "SELECT candidate_count, budget_calls, attempted_calls, stored_scores "
        "FROM stock_research_run"
    ).fetchone()
    assert run == (3, 2, 2, 2)
    assert conn.execute("SELECT COUNT(*) FROM stock_research_score").fetchone()[0] == 2
    conn.close()


def test_replay_does_not_rescore_immutable_snapshot():
    conn = connection()
    calls = 0

    def ask(_prompt, _config):
        nonlocal calls
        calls += 1
        return reply()

    items = [candidate("AAA")]
    research.score_candidates(conn, items, CONFIG, ask=ask, now=NOW)
    second = research.score_candidates(conn, items, CONFIG, ask=ask, now=NOW)
    assert calls == 1
    assert second["attempted_calls"] == 0
    assert conn.execute("SELECT COUNT(*) FROM stock_research_score").fetchone()[0] == 1
    conn.close()


def test_invalid_bridge_reply_is_not_stored_or_turned_into_zero():
    conn = connection()
    result = research.score_candidates(
        conn, [candidate("AAA")], CONFIG, ask=lambda *_: "gateway failed", now=NOW
    )
    assert result["attempted_calls"] == 1
    assert result["stored_scores"] == 0
    assert result["errors"] == ["AAA:ValueError"]
    assert conn.execute("SELECT COUNT(*) FROM stock_research_score").fetchone()[0] == 0
    conn.close()


def test_persistent_failure_stops_after_snapshot_retry_cap():
    conn = connection()
    calls = 0

    def fail(*_args):
        nonlocal calls
        calls += 1
        return "not json"

    item = [candidate("AAA")]
    for _ in range(CONFIG["max_attempts_per_snapshot"] + 2):
        last = research.score_candidates(conn, item, CONFIG, ask=fail, now=NOW)
    assert calls == CONFIG["max_attempts_per_snapshot"]
    assert last["attempted_calls"] == 0
    assert last["retry_exhausted"] == 1
    attempt = conn.execute("SELECT attempts, last_error FROM stock_research_attempt").fetchone()
    assert attempt == (3, "ValueError")
    assert research_digest.failed_attempts(conn, max_attempts=3) == 1
    conn.close()


def test_prompt_contains_only_supplied_candidate_and_metric_instruction():
    prompt = research.build_prompt(candidate("AAA"))
    assert '"symbol":"AAA"' in prompt
    assert "Do not recalculate" in prompt
    assert "CCC" not in prompt


def test_config_rejects_unsafe_backend_and_excess_budget(tmp_path):
    path = tmp_path / "research.json"
    path.write_text(json.dumps(CONFIG))
    assert research.load_config(path) == CONFIG
    path.write_text(json.dumps({**CONFIG, "codex_bin": "relative/codex"}))
    with pytest.raises(ValueError, match="absolute"):
        research.load_config(path)
    path.write_text(json.dumps({**CONFIG, "codex_bin": str(tmp_path / "missing")}))
    with pytest.raises(ValueError, match="executable"):
        research.load_config(path)
    bridge = {
        **CONFIG,
        "backend": "claude_bridge",
        "bridge_url": "https://remote.example",
    }
    path.write_text(json.dumps(bridge))
    with pytest.raises(ValueError, match="loopback"):
        research.load_config(path)
    path.write_text(json.dumps({**CONFIG, "max_calls_per_run": 21}))
    with pytest.raises(ValueError, match="max_calls"):
        research.load_config(path)
    path.write_text(json.dumps({**CONFIG, "max_attempts_per_snapshot": 6}))
    with pytest.raises(ValueError, match="max_attempts"):
        research.load_config(path)


def test_codex_backend_is_ephemeral_read_only_and_schema_constrained(monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        output = command[command.index("--output-last-message") + 1]
        with open(output, "w", encoding="utf-8") as handle:
            handle.write(reply())

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr(research.subprocess, "run", fake_run)
    assert research.parse_reply(research.codex_ask("prompt", CONFIG))["total_score"] == 20
    command = captured["command"]
    assert "--ephemeral" in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[command.index("-m") + 1] == "test-model"
    schema_path = command[command.index("--output-schema") + 1]
    # Temporary files are removed after the call, but the command proves a
    # schema path was mandatory and stdin carried only the prompt.
    assert schema_path.endswith("response-schema.json")
    assert captured["kwargs"]["input"] == "prompt"
    assert captured["kwargs"]["check"] is False


def test_response_schema_requires_exact_component_keys():
    schema = research._response_schema()
    components = schema["properties"]["components"]
    assert components["required"] == list(research.COMPONENTS)
    assert components["additionalProperties"] is False
    assert all(spec["maximum"] == 5 for spec in components["properties"].values())


def test_pending_scores_requires_score_or_exhausted_attempts(monkeypatch):
    conn = connection()
    item = candidate("AAA")
    monkeypatch.setattr(research, "candidates", lambda _conn: [item])
    assert research_digest.pending_scores(conn, max_attempts=3) == 1
    research.score_candidates(conn, [item], CONFIG, ask=lambda *_: reply(), now=NOW)
    assert research_digest.pending_scores(conn, max_attempts=3) == 0
    conn.close()


def test_digest_and_board_show_components_metrics_and_swing_text():
    conn = connection()
    research.score_candidates(conn, [candidate("AAA")], CONFIG, ask=lambda *_: reply(), now=NOW)
    items = research_digest.rows(conn, min_score=18)
    gaps = research_digest.failed_attempts(conn, max_attempts=3)
    digest = research_digest.render_digest(
        items,
        as_of=date(2026, 8, 26),
        swing_text="SWING SIGNALS new=0",
        scoring_gaps=gaps,
    )
    board = research_digest.render_board(
        items, as_of=date(2026, 8, 26), min_score=18, scoring_gaps=gaps
    )
    for text in (digest, board):
        assert "AAA" in text
        assert "business_quality=4" in text
        assert "Check concentration risk" in text
    assert "SWING SIGNALS new=0" in digest
    assert "pe_ratio=12.0" in board
    conn.close()


def test_board_keeps_below_threshold_scores_visible():
    high = {
        "symbol": "HIGH",
        "total_score": 20,
        "screens": ["garp"],
        "metrics": {},
        "components": {key: 4 for key in research.COMPONENTS},
        "rationale": "high",
        "red_flags": [],
    }
    low = {**high, "symbol": "LOW", "total_score": 10}
    board = research_digest.render_board([high, low], as_of=date(2026, 8, 26), min_score=18)
    assert "## Review (18+/25)" in board
    assert "**HIGH** [20/25]" in board
    assert "## Below threshold (<18/25)" in board
    assert "**LOW** [10/25]" in board


def test_swing_artifact_must_match_current_watchlist_reference_date():
    research_digest.validate_swing_artifact(
        "SWING SIGNALS as_of=2026-08-25 scanned=20 new=0",
        expected_as_of=date(2026, 8, 25),
    )
    with pytest.raises(ValueError, match="current watchlist"):
        research_digest.validate_swing_artifact(
            "SWING SIGNALS as_of=2026-08-24 scanned=20 new=0",
            expected_as_of=date(2026, 8, 25),
        )
    with pytest.raises(ValueError, match="malformed"):
        research_digest.validate_swing_artifact(
            "not a swing artifact", expected_as_of=date(2026, 8, 25)
        )


def test_delivery_is_fingerprint_idempotent_and_marks_only_after_send():
    conn = connection()
    sent = []
    assert (
        research_digest.deliver(
            conn,
            "digest",
            token="t",
            chat_id="c",
            poster=lambda req: sent.append(json.loads(req.data)),
            now=NOW,
        )
        == "sent"
    )
    assert len(sent) == 1
    assert research_digest.deliver(conn, "digest", token="t", chat_id="c") == "duplicate"
    assert len(sent) == 1
    assert conn.execute("SELECT COUNT(*) FROM stock_research_delivery").fetchone()[0] == 1
    conn.close()


def test_delivery_failure_does_not_mark_fingerprint():
    conn = connection()

    def fail(_req):
        raise RuntimeError("telegram down")

    with pytest.raises(RuntimeError, match="telegram down"):
        research_digest.deliver(conn, "digest", token="t", chat_id="c", poster=fail)
    assert conn.execute("SELECT COUNT(*) FROM stock_research_delivery").fetchone()[0] == 0
    with pytest.raises(ValueError, match="4096"):
        research_digest.deliver(conn, "x" * 4097, token="t", chat_id="c")
    conn.close()


def test_atomic_write_replaces_complete_file(tmp_path):
    path = tmp_path / "board.md"
    research_digest.atomic_write(path, "first")
    research_digest.atomic_write(path, "second")
    assert path.read_text() == "second"
