"""T3.5 survivor-only LLM research with deterministic budget accounting."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import UTC
from datetime import datetime as dt
from pathlib import Path
from uuid import uuid4

from invest import db, screens

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "config/research.json"
DEFAULT_DB = screens.DEFAULT_DB
METHODOLOGY = "stock-research-2026.1"
COMPONENTS = (
    "business_quality",
    "valuation",
    "financial_strength",
    "governance",
    "evidence_quality",
)
RESEARCH_FIELDS = {
    "market_cap_cr",
    "close",
    "price_to_52w_high",
    "price_above_50dma",
    "pe_ratio",
    "pb_ratio",
    "peg",
    "dividend_yield",
    "roe",
    "roce",
    "avg_roe_3y",
    "avg_roe_5y",
    "avg_roce_3y",
    "avg_roce_5y",
    "operating_margin",
    "revenue_growth_yoy",
    "profit_growth_yoy",
    "revenue_cagr_3y",
    "profit_cagr_3y",
    "eps_cagr_3y",
    "debt_to_equity",
    "current_ratio",
    "free_cash_flow_3y",
    "interest_coverage",
    "piotroski_score",
    "promoter_holding",
    "promoter_pledged",
    "fii_holding",
}


def load_config(path: str | Path = DEFAULT_CONFIG) -> dict:
    cfg = json.loads(Path(path).read_text())
    required = {
        "backend": str,
        "model": str,
        "max_calls_per_run": int,
        "max_attempts_per_snapshot": int,
        "min_total_score": int,
    }
    for key, kind in required.items():
        value = cfg.get(key)
        if isinstance(value, bool) or not isinstance(value, kind):
            raise ValueError(f"research config {key} has wrong type")
    if cfg["backend"] not in {"codex_exec", "claude_bridge"}:
        raise ValueError("backend must be codex_exec or claude_bridge")
    if cfg["backend"] == "codex_exec":
        binary = cfg.get("codex_bin")
        if not isinstance(binary, str) or not Path(binary).is_absolute():
            raise ValueError("codex_bin must be an absolute path")
        if not Path(binary).is_file() or not os.access(binary, os.X_OK):
            raise ValueError("codex_bin must be an executable file")
    else:
        bridge_url = cfg.get("bridge_url")
        if not isinstance(bridge_url, str) or not bridge_url.startswith("http://127.0.0.1:"):
            raise ValueError("bridge_url must be loopback HTTP")
    if not cfg["model"]:
        raise ValueError("model must be non-empty")
    if not 1 <= cfg["max_calls_per_run"] <= 20:
        raise ValueError("max_calls_per_run must be within [1, 20]")
    if not 1 <= cfg["max_attempts_per_snapshot"] <= 5:
        raise ValueError("max_attempts_per_snapshot must be within [1, 5]")
    if not 0 <= cfg["min_total_score"] <= 25:
        raise ValueError("min_total_score must be within [0, 25]")
    return cfg


def candidates(conn, screen_config: dict | None = None) -> list[dict]:
    """Union deterministic screen survivors; never admit a non-survivor."""
    cfg = screen_config or screens.load_config()
    universe = screens.build_universe(conn)
    matched: dict[str, list[str]] = {}
    for screen_id, spec in cfg["screens"].items():
        result = screens.evaluate_screen(universe, spec["conditions"])
        for item in result["survivors"]:
            matched.setdefault(item["symbol"], []).append(screen_id)
    output = []
    for symbol in sorted(matched):
        row = universe[symbol]
        fields = sorted(
            RESEARCH_FIELDS
            | {
                field
                for screen_id in matched[symbol]
                for field in cfg["screens"][screen_id]["conditions"]
            }
        )
        metrics = {field: row.get(field) for field in fields}
        snapshot_date = row.get("as_of")
        if snapshot_date is None:
            raise ValueError(f"survivor {symbol} has no fundamentals as_of date")
        output.append(
            {
                "symbol": symbol,
                "snapshot_date": snapshot_date,
                "screens": sorted(matched[symbol]),
                "metrics": metrics,
            }
        )
    return output


def build_prompt(candidate: dict) -> str:
    payload = json.dumps(candidate, sort_keys=True, default=str, separators=(",", ":"))
    return (
        "Assess this deterministic NSE screen survivor. Do not recalculate or replace supplied "
        "metrics. Score 0 when evidence is absent or clearly adverse, 3 when mixed, and 5 only "
        "when supplied evidence is clearly strong. business_quality uses returns, margins and "
        "growth; valuation uses PE, PB, PEG and yield; financial_strength uses debt, liquidity, "
        "cash flow, coverage and Piotroski; governance uses promoter pledge and holdings; "
        "evidence_quality uses completeness and corroboration across matched screens. Assign "
        "each named component an integer 0-5, explain the component judgments in at most 60 "
        "words, and list concrete red flags. Missing evidence must lower evidence_quality and "
        "must not be invented. Return JSON only with keys components, rationale, red_flags. "
        f"components must contain exactly {list(COMPONENTS)}. Candidate: {payload}"
    )


def parse_reply(reply: str) -> dict:
    try:
        value = json.loads(reply)
    except json.JSONDecodeError as exc:
        raise ValueError("LLM reply is not JSON") from exc
    if not isinstance(value, dict) or set(value) != {"components", "rationale", "red_flags"}:
        raise ValueError("LLM reply has wrong top-level keys")
    components = value["components"]
    if not isinstance(components, dict) or set(components) != set(COMPONENTS):
        raise ValueError("LLM reply has wrong component keys")
    clean = {}
    for key in COMPONENTS:
        score = components[key]
        if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 5:
            raise ValueError(f"invalid component score: {key}")
        clean[key] = score
    rationale = value["rationale"]
    flags = value["red_flags"]
    if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > 500:
        raise ValueError("invalid rationale")
    if (
        not isinstance(flags, list)
        or len(flags) > 10
        or any(not isinstance(flag, str) or not flag.strip() or len(flag) > 200 for flag in flags)
    ):
        raise ValueError("invalid red_flags")
    return {
        "components": clean,
        "total_score": sum(clean.values()),
        "rationale": rationale.strip(),
        "red_flags": [flag.strip() for flag in flags],
    }


def bridge_ask(prompt: str, config: dict, *, timeout: int = 240) -> str:
    body = json.dumps(
        {
            "model": config["model"],
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        }
    ).encode()
    req = urllib.request.Request(
        config["bridge_url"].rstrip("/") + "/v1/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            result = json.loads(response.read().decode())
        return result["choices"][0]["message"]["content"]
    except (urllib.error.URLError, TimeoutError, KeyError, IndexError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"claude-bridge call failed: {type(exc).__name__}") from exc


def _response_schema() -> dict:
    component_schema = {
        "type": "object",
        "properties": {key: {"type": "integer", "minimum": 0, "maximum": 5} for key in COMPONENTS},
        "required": list(COMPONENTS),
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "components": component_schema,
            "rationale": {"type": "string", "minLength": 1, "maxLength": 500},
            "red_flags": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 200},
                "maxItems": 10,
            },
        },
        "required": ["components", "rationale", "red_flags"],
        "additionalProperties": False,
    }


def codex_ask(prompt: str, config: dict, *, timeout: int = 300) -> str:
    """Run one ephemeral, read-only Codex turn with schema-constrained output."""
    with tempfile.TemporaryDirectory(prefix="invest-research-") as temporary:
        root = Path(temporary)
        schema_path = root / "response-schema.json"
        output_path = root / "response.json"
        schema_path.write_text(json.dumps(_response_schema()), encoding="utf-8")
        command = [
            config["codex_bin"],
            "exec",
            "--ephemeral",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--color",
            "never",
            "-C",
            temporary,
            "-m",
            config["model"],
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
            "-",
        ]
        try:
            result = subprocess.run(
                command,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"codex exec failed: {type(exc).__name__}") from exc
        if result.returncode != 0 or not output_path.exists():
            detail = (result.stderr or result.stdout).strip()
            tail = detail[-1000:] if detail else "no output"
            raise RuntimeError(f"codex exec exit={result.returncode}: {tail}")
        return output_path.read_text(encoding="utf-8").strip()


def configured_ask(prompt: str, config: dict) -> str:
    if config["backend"] == "codex_exec":
        return codex_ask(prompt, config)
    return bridge_ask(prompt, config)


def _already_scored(conn, candidate: dict) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM stock_research_score WHERE symbol=? AND snapshot_date=? "
            "AND methodology_version=?",
            [candidate["symbol"], candidate["snapshot_date"], METHODOLOGY],
        ).fetchone()
        is not None
    )


def _attempts(conn, candidate: dict) -> int:
    row = conn.execute(
        "SELECT attempts FROM stock_research_attempt WHERE symbol=? AND snapshot_date=? "
        "AND methodology_version=?",
        [candidate["symbol"], candidate["snapshot_date"], METHODOLOGY],
    ).fetchone()
    return row[0] if row else 0


def _record_attempt(conn, candidate: dict, *, now: dt, error: str | None) -> None:
    conn.execute(
        """
        INSERT INTO stock_research_attempt VALUES (?, ?, ?, 1, ?, ?)
        ON CONFLICT (symbol, snapshot_date, methodology_version) DO UPDATE SET
            attempts = stock_research_attempt.attempts + 1,
            last_error = excluded.last_error,
            last_attempt_at = excluded.last_attempt_at
        """,
        [candidate["symbol"], candidate["snapshot_date"], METHODOLOGY, error, now],
    )


def score_candidates(conn, items: list[dict], config: dict, *, ask=None, now=None) -> dict:
    """Score at most the configured number of candidates, one call each."""
    ask = ask or configured_ask
    now = now or dt.now(UTC)
    run_id = f"{now:%Y%m%dT%H%M%S}-{uuid4().hex[:8]}"
    unscored = [item for item in items if not _already_scored(conn, item)]
    exhausted = [
        item for item in unscored if _attempts(conn, item) >= config["max_attempts_per_snapshot"]
    ]
    pending = [item for item in unscored if item not in exhausted]
    selected = pending[: config["max_calls_per_run"]]
    attempted = stored = 0
    errors = []
    for item in selected:
        prompt = build_prompt(item)
        attempted += 1
        try:
            parsed = parse_reply(ask(prompt, config))
        except (RuntimeError, ValueError) as exc:
            error = type(exc).__name__
            _record_attempt(conn, item, now=now, error=error)
            errors.append(f"{item['symbol']}:{error}")
            continue
        _record_attempt(conn, item, now=now, error=None)
        conn.execute(
            """
            INSERT INTO stock_research_score VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            [
                item["symbol"],
                item["snapshot_date"],
                METHODOLOGY,
                json.dumps(item["screens"], sort_keys=True),
                json.dumps(item["metrics"], sort_keys=True, default=str),
                json.dumps(parsed["components"], sort_keys=True),
                parsed["total_score"],
                parsed["rationale"],
                json.dumps(parsed["red_flags"], sort_keys=True),
                config["model"],
                hashlib.sha256(prompt.encode()).hexdigest(),
                now,
            ],
        )
        stored += 1
    conn.execute(
        "INSERT INTO stock_research_run VALUES (?, ?, ?, ?, ?, ?, ?)",
        [run_id, now, len(items), config["max_calls_per_run"], attempted, stored, ";".join(errors)],
    )
    return {
        "run_id": run_id,
        "candidates": len(items),
        "pending": len(pending),
        "retry_exhausted": len(exhausted),
        "attempted_calls": attempted,
        "stored_scores": stored,
        "errors": errors,
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="invest-research")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--enable-llm", action="store_true")
    args = parser.parse_args(argv)
    conn = db.connect(args.db)
    try:
        db.init_schema(conn)
        config = load_config(args.config)
        items = candidates(conn)
        if not args.enable_llm:
            print(f"research dry-run candidates={len(items)} calls=0")
            return 0
        result = score_candidates(conn, items, config)
        print(json.dumps(result, sort_keys=True))
        return int(bool(result["errors"]))
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
