"""T3.6 idempotent research digest and vault-board delivery."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import UTC, date
from datetime import datetime as dt
from pathlib import Path

from invest import alerts, db, prices, research, watchlist

TELEGRAM_MAX = 4096
DEFAULT_BOARD = "data/invest-board.md"
SWING_HEADER_RE = re.compile(r"^SWING SIGNALS as_of=(\d{4}-\d{2}-\d{2})\b")


def rows(conn, *, min_score: int) -> list[dict]:
    result = []
    for row in conn.execute(
        """
        SELECT symbol, snapshot_date, screens_json, metrics_json,
               components_json, total_score, rationale, red_flags_json
        FROM stock_research_score
        WHERE methodology_version = ? AND total_score >= ?
        ORDER BY total_score DESC, symbol
        """,
        [research.METHODOLOGY, min_score],
    ).fetchall():
        result.append(
            {
                "symbol": row[0],
                "snapshot_date": row[1],
                "screens": json.loads(row[2]),
                "metrics": json.loads(row[3]),
                "components": json.loads(row[4]),
                "total_score": row[5],
                "rationale": row[6],
                "red_flags": json.loads(row[7]),
            }
        )
    return result


def failed_attempts(conn, *, max_attempts: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM stock_research_attempt a "
        "WHERE attempts >= ? AND NOT EXISTS ("
        "SELECT 1 FROM stock_research_score s WHERE s.symbol=a.symbol "
        "AND s.snapshot_date=a.snapshot_date "
        "AND s.methodology_version=a.methodology_version)",
        [max_attempts],
    ).fetchone()[0]


def pending_scores(conn, *, max_attempts: int) -> int:
    pending = 0
    for item in research.candidates(conn):
        key = [item["symbol"], item["snapshot_date"], research.METHODOLOGY]
        if conn.execute(
            "SELECT 1 FROM stock_research_score WHERE symbol=? "
            "AND snapshot_date=? AND methodology_version=?",
            key,
        ).fetchone():
            continue
        row = conn.execute(
            "SELECT attempts FROM stock_research_attempt WHERE symbol=? "
            "AND snapshot_date=? AND methodology_version=?",
            key,
        ).fetchone()
        if row is None or row[0] < max_attempts:
            pending += 1
    return pending


def render_digest(
    items: list[dict],
    *,
    as_of: date,
    swing_text: str | None = None,
    scoring_gaps: int = 0,
) -> str:
    lines = [f"INVEST RESEARCH {as_of} | survivors={len(items)} scoring_gaps={scoring_gaps}"]
    for item in items:
        component_text = " ".join(f"{key}={item['components'][key]}" for key in research.COMPONENTS)
        lines.extend(
            [
                f"{item['symbol']} score={item['total_score']}/25 "
                f"screens={','.join(item['screens'])}",
                f"  {component_text}",
                f"  {item['rationale']}",
                "  red_flags=" + ("; ".join(item["red_flags"]) or "none stated"),
            ]
        )
    if swing_text:
        lines.extend(["", swing_text.strip()])
    return "\n".join(lines)


def _board_item(item: dict) -> list[str]:
    metrics = ", ".join(f"{key}={value}" for key, value in sorted(item["metrics"].items()))
    return [
        f"- **{item['symbol']}** [{item['total_score']}/25] {', '.join(item['screens'])}",
        "  - components: "
        + ", ".join(f"{key}={item['components'][key]}" for key in research.COMPONENTS),
        f"  - metrics: {metrics}",
        f"  - rationale: {item['rationale']}",
        "  - red flags: " + ("; ".join(item["red_flags"]) or "none stated"),
    ]


def render_board(items: list[dict], *, as_of: date, min_score: int, scoring_gaps: int = 0) -> str:
    lines = [
        "# Invest board",
        "",
        f"> Generated from persisted research scores on {as_of}. Do not hand-edit scores. "
        f"Unscored retry-exhausted snapshots: {scoring_gaps}.",
        "",
    ]
    sections = (
        (f"Review ({min_score}+/25)", [i for i in items if i["total_score"] >= min_score]),
        (f"Below threshold (<{min_score}/25)", [i for i in items if i["total_score"] < min_score]),
    )
    for heading, section_items in sections:
        lines.append(f"## {heading}")
        if not section_items:
            lines.append("- None.")
        for item in section_items:
            lines.extend(_board_item(item))
        lines.append("")
    return "\n".join(lines)


def validate_swing_artifact(text: str, *, expected_as_of: date) -> None:
    match = SWING_HEADER_RE.match(text)
    if match is None:
        raise ValueError("swing artifact header is malformed")
    if date.fromisoformat(match.group(1)) != expected_as_of:
        raise ValueError("swing artifact does not match the current watchlist reference date")


def atomic_write(path: str | Path, content: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def deliver(conn, text: str, *, token: str, chat_id: str, poster=None, now=None) -> str:
    """Send a digest once per exact content fingerprint."""
    if len(text) > TELEGRAM_MAX:
        raise ValueError(f"Telegram digest exceeds {TELEGRAM_MAX} characters")
    fingerprint = hashlib.sha256(text.encode()).hexdigest()
    if conn.execute(
        "SELECT 1 FROM stock_research_delivery WHERE content_sha256=?", [fingerprint]
    ).fetchone():
        return "duplicate"
    alerts.send_message(token, chat_id, text, poster=poster)
    conn.execute(
        "INSERT INTO stock_research_delivery VALUES (?, 'telegram', ?)",
        [fingerprint, now or dt.now(UTC)],
    )
    return "sent"


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="invest-research-digest")
    parser.add_argument("--db", default=str(research.DEFAULT_DB))
    parser.add_argument("--config", default=str(research.DEFAULT_CONFIG))
    parser.add_argument("--out", default="data/research-latest.txt")
    parser.add_argument("--board", default=DEFAULT_BOARD)
    parser.add_argument("--swing", default="data/swing-latest.txt")
    parser.add_argument("--swing-config", default=watchlist.DEFAULT_CONFIG)
    parser.add_argument("--send", action="store_true")
    args = parser.parse_args(argv)
    conn = db.connect(args.db)
    try:
        db.init_schema(conn)
        config = research.load_config(args.config)
        all_items = rows(conn, min_score=0)
        selected = [i for i in all_items if i["total_score"] >= config["min_total_score"]]
        gaps = failed_attempts(conn, max_attempts=config["max_attempts_per_snapshot"])
        pending = pending_scores(conn, max_attempts=config["max_attempts_per_snapshot"])
        if args.send and pending:
            raise ValueError(f"{pending} research scores are still pending")
        today = dt.now(prices.IST).date()
        swing_path = Path(args.swing) if args.swing else None
        if args.send and (swing_path is None or not swing_path.is_file()):
            raise ValueError("current swing artifact is required for delivery")
        swing_text = swing_path.read_text(encoding="utf-8") if swing_path else None
        if args.send and swing_text is not None:
            swing_config = watchlist.load_config(args.swing_config)
            watched = watchlist.build_watchlist(conn, swing_config)
            if not watched["picks"]:
                raise ValueError("current swing watchlist has no eligible picks")
            expected_as_of = min(item["as_of"] for item in watched["picks"])
            validate_swing_artifact(swing_text, expected_as_of=expected_as_of)
        text = render_digest(
            selected,
            as_of=today,
            scoring_gaps=gaps,
            swing_text=swing_text,
        )
        board = render_board(
            all_items,
            as_of=today,
            min_score=config["min_total_score"],
            scoring_gaps=gaps,
        )
        atomic_write(args.out, text + "\n")
        atomic_write(args.board, board)
        outcome = "preview"
        if args.send:
            token, chat_id = alerts.load_credentials()
            if not token or not chat_id:
                raise ValueError("invest Telegram credentials are not configured")
            outcome = deliver(conn, text, token=token, chat_id=chat_id)
        print(f"research digest: {outcome} selected={len(selected)} all={len(all_items)}")
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"research digest failed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
