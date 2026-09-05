"""VBRS zone-change Telegram alerts (T2.4) — separate bot, per the operator.

Delivery: plain urllib POST to the Telegram Bot API (no new dependencies;
SPEC pins change only via a TASKS version-bump task).

Configuration (secrets live outside Git, .env* is gitignored):
  .env
    INVEST_BOT_TOKEN=<from @BotFather>
    INVEST_CHAT_ID=<your chat with the bot>

Zone detection recomputes from stored nifty_pe history each run: the latest
PE's zone vs the previous distinct day's zone. A crossing sends ONE message;
no crossing = silent success. First-ever run records the baseline silently
(use `--force` to send a test message anyway).

Setup once, from the repo:
  1. @BotFather -> /newbot -> copy token into invest.env
  2. message your new bot anything in Telegram
  3. .venv/bin/python -m invest.alerts chatid   # prints candidate chat ids
  4. put INVEST_CHAT_ID into invest.env
  5. .venv/bin/python -m invest.alerts --force  # live test send

Unconfigured (missing file/vars) = logged skip + exit 0, so the nightly
pipeline stays green before setup. Configured-but-failing send = nonzero exit
(fail-loud, journald evidence).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from urllib import request as urlreq

from invest import db, vbrs

log = logging.getLogger("invest.alerts")

ENV_PATH = Path(os.environ.get("INVEST_ENV", ".env"))
API_BASE = "https://api.telegram.org/bot"


def load_credentials(path: Path | None = None) -> tuple[str | None, str | None]:
    """Read INVEST_BOT_TOKEN / INVEST_CHAT_ID from the env file (or env vars)."""
    path = path or ENV_PATH
    token = os.environ.get("INVEST_BOT_TOKEN")
    chat_id = os.environ.get("INVEST_CHAT_ID")
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            val = val.strip().strip('"').strip("'")
            if key.strip() == "INVEST_BOT_TOKEN" and not token:
                token = val or None
            if key.strip() == "INVEST_CHAT_ID" and not chat_id:
                chat_id = val or None
    return token, chat_id


def send_message(token: str, chat_id: str, text: str, poster=None) -> None:
    """POST to the Bot API. Injectable poster keeps tests offline."""
    url = f"{API_BASE}{token}/sendMessage"
    body = json.dumps({"chat_id": chat_id, "text": text}).encode()
    req = urlreq.Request(url, data=body, headers={"Content-Type": "application/json"})
    if poster is not None:
        poster(req)
        return
    with urlreq.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read())
        if not payload.get("ok"):
            msg = f"telegram api error: {payload}"
            raise RuntimeError(msg)


def last_two_zones(conn) -> tuple[tuple[str, float] | None, tuple[str, float] | None]:
    """(previous_zone, pe), (latest_zone, pe) from stored history; None if <2 days."""
    rows = conn.execute(
        "SELECT DISTINCT nav_date, pe FROM nifty_pe WHERE pe IS NOT NULL "
        "ORDER BY nav_date DESC LIMIT 2"
    ).fetchall()
    cfg = vbrs.load_config()
    zones = [(vbrs.zone(p, cfg), p) for _d, p in reversed(rows)]
    if len(zones) < 2:
        return None, (zones[-1] if zones else None)
    return zones[0], zones[-1]


def build_message(prev: tuple[str, float], cur: tuple[str, float]) -> str:
    cfg = vbrs.load_config()
    arrow = {"Cheap": "↓", "Base": "→", "Expensive": "↑"}
    return (
        f"*VBRS zone change*: {prev[0]} {arrow.get(cur[0], '')} {cur[0]}\n"
        f"Nifty PE {prev[1]:.2f} → {cur[1]:.2f} (median {cfg['median_pe']})\n"
        f"Cash position now {vbrs.cash_position(cur[1], float(cfg['median_pe']), cfg):.2%}\n"
        f"(model output only — trades stay manual)"
    )


def run_check(conn, *, force: bool = False, poster=None) -> str:
    """Returns 'sent' | 'baseline' | 'quiet' | 'unconfigured'. Exits handled by CLI."""
    token, chat_id = load_credentials()
    if not token or not chat_id:
        log.info("alerts not configured (%s missing/empty); skipping", ENV_PATH)
        return "unconfigured"

    prev, cur = last_two_zones(conn)
    if cur is None:
        log.info("no PE data yet; nothing to alert")
        return "quiet"
    if prev is None and not force:
        log.info("baseline recorded: %s PE %.2f (%s)", cur[0], cur[1], cur[0])
        return "baseline"
    if prev is not None and prev[0] == cur[0] and not force:
        log.info("zone unchanged (%s); quiet", cur[0])
        return "quiet"

    text = build_message(prev or ("(baseline)", cur[1]), cur)
    send_message(token, chat_id, text, poster=poster)
    log.info("alert sent: %s -> %s", prev and prev[0], cur[0])
    return "sent"


def print_chat_candidates() -> int:
    """Helper: dump getUpdates so the user can read off their chat id."""
    token, _ = load_credentials()
    if not token:
        print(f"INVEST_BOT_TOKEN not set in {ENV_PATH}", file=sys.stderr)
        return 2
    req = urlreq.Request(
        f"{API_BASE}{token}/getUpdates", headers={"User-Agent": "invest-pipeline/0.1"}
    )
    with urlreq.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read())
    updates = payload.get("result", [])
    if not updates:
        print("no messages yet — open Telegram, send your bot any message, retry")
        return 1
    seen = {}
    for u in updates:
        msg = u.get("message") or u.get("edited_message") or {}
        chat = msg.get("chat") or {}
        if chat.get("id") is not None:
            seen[chat["id"]] = f"{chat.get('first_name', '')} {chat.get('title', '')}".strip()
    for cid, who in seen.items():
        print(f"chat_id={cid}  ({who})")
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser(prog="invest-alerts")
    parser.add_argument("--db", default="data/invest.duckdb")
    parser.add_argument("--env", default=None, help="override env-file path")
    parser.add_argument("--force", action="store_true", help="send even without crossing")
    parser.add_argument(
        "command", nargs="?", choices=["chatid"], help="chatid = list chats that messaged the bot"
    )
    args = parser.parse_args(argv)

    global ENV_PATH
    if args.env:
        ENV_PATH = Path(args.env)

    if args.command == "chatid":
        return print_chat_candidates()

    conn = db.connect(args.db)
    db.init_schema(conn)
    outcome = run_check(conn, force=args.force)
    print(f"alerts: {outcome}")
    return 0 if outcome in ("sent", "quiet", "baseline", "unconfigured") else 1


if __name__ == "__main__":
    sys.exit(main())
