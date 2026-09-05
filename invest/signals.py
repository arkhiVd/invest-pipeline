"""T4.4 close-confirmed 10/21-EMA crossover surfacing for the swing watchlist.

Scans are read-only over market tables; the module's own writes are limited
to the ``swing_signals`` watermark and its digest artifact. It reads
watchlist picks from invest.watchlist, surfaces only NEW crossovers since
the persisted watermark, and attaches 2% position sizing to entries via
invest.swing.position_size. Delivery into Telegram happens in T3.6; this
module produces the artifact (``data/swing-latest.txt`` by default) that
digest will embed.

Entry assumption: signal confirmed on the daily close, sizing uses that
close as entry and the configured stop reference (default: slow EMA value
on the signal day, matching the system's cross-under exit logic).
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
from datetime import UTC, date, timedelta
from datetime import datetime as dt
from decimal import Decimal

from invest import db, swing, watchlist

log = logging.getLogger("invest.signals")

DEFAULT_DB = watchlist.DEFAULT_DB
WATERMARK_KIND = "swing_signals"
STOP_MODES = ("ema21",)


def validate_signal_config(config: dict) -> None:
    if isinstance(config.get("capital"), bool) or not isinstance(
        config.get("capital"), (int, float)
    ):
        raise ValueError("capital must be a positive number")
    if not math.isfinite(config["capital"]) or config["capital"] <= 0:
        raise ValueError("capital must be positive and finite")
    if config.get("stop_mode") not in STOP_MODES:
        raise ValueError(f"stop_mode must be one of {STOP_MODES}")
    if isinstance(config.get("risk_fraction"), bool) or not isinstance(
        config.get("risk_fraction"), (int, float)
    ):
        raise ValueError("risk_fraction must be a fraction in (0, 1]")
    if not math.isfinite(config["risk_fraction"]):
        raise ValueError("risk_fraction must be a fraction in (0, 1]")
    risk = Decimal(str(config["risk_fraction"]))
    if not 0 < risk <= 1:
        raise ValueError("risk_fraction must be a fraction in (0, 1]")
    lookback = config.get("initial_lookback_days")
    if isinstance(lookback, bool) or not isinstance(lookback, int) or lookback < 1:
        raise ValueError("initial_lookback_days must be a positive integer")


def _size_entry(config: dict, close: float, slow_ema: float | None) -> dict | None:
    """Position-size one entry; returns an explicit gap when impossible."""
    if config["stop_mode"] == "ema21" and slow_ema is None:
        return {"quantity": 0, "reason": "ema_unavailable"}
    try:
        sized = swing.position_size(
            config["capital"],
            str(close),
            str(slow_ema),
            risk_fraction=Decimal(str(config["risk_fraction"])),
        )
    except ValueError as exc:
        return {"quantity": 0, "reason": f"invalid_stop:{exc}"}
    if sized.quantity <= 0:
        return {"quantity": 0, "reason": "zero_shares_within_risk"}
    return {
        "quantity": sized.quantity,
        "stop": float(sized.stop_price),
        "capital_to_deploy": float(sized.capital_to_deploy),
        "maximum_loss_at_stop": float(sized.maximum_loss_at_stop),
    }


def scan_symbol(
    conn,
    symbol: str,
    *,
    since: date,
    config: dict,
    through: date | None = None,
    fast_period: int = 10,
    slow_period: int = 21,
) -> list[dict]:
    """Return crossovers after ``since`` and no later than ``through``."""
    rows = conn.execute(
        "SELECT trade_date, close FROM stock_price "
        "WHERE symbol = ? AND close IS NOT NULL "
        "AND (?::DATE IS NULL OR trade_date <= ?::DATE) ORDER BY trade_date",
        [symbol, through, through],
    ).fetchall()
    dates = [row[0] for row in rows]
    closes = [float(row[1]) for row in rows]
    signals: list[dict] = []
    for point in swing.ema_crossover(closes, fast_period=fast_period, slow_period=slow_period):
        day = dates[point.index]
        if point.signal is swing.CrossoverSignal.NONE or day <= since:
            continue
        record = {
            "symbol": symbol,
            "action": point.signal.value,
            "date": day,
            "close": point.close,
            "ema_fast": point.fast_ema,
            "ema_slow": point.slow_ema,
        }
        if point.signal is swing.CrossoverSignal.ENTER:
            record["sizing"] = _size_entry(config, point.close, point.slow_ema)
        signals.append(record)
    return signals


def latest_session(conn) -> date | None:
    row = conn.execute("SELECT MAX(trade_date) FROM stock_price").fetchone()
    return row[0]


def run_scan(conn, config: dict, *, canonical_cutoff: date | None = None) -> dict:
    """Scan watchlist picks for new crossovers; no network access."""
    validate_signal_config(config)
    watched = watchlist.build_watchlist(conn, config, cutoff=canonical_cutoff)
    if not watched["picks"]:
        raise ValueError("no eligible swing watchlist picks; watermark not advanced")
    # Advance only through the oldest selected symbol's latest close. This
    # prevents one forward-dated or faster-arriving series from suppressing a
    # late crossover on another selected symbol.
    reference = canonical_cutoff or min(item["as_of"] for item in watched["picks"])
    watermark = db.get_watermark(conn, WATERMARK_KIND)
    if canonical_cutoff is not None:
        # A persisted cutoff must replay from the same bounded input window even
        # after the first run advances the operational watermark.
        since = reference - timedelta(days=config["initial_lookback_days"])
    elif watermark is None:
        since = reference - timedelta(days=config["initial_lookback_days"])
    else:
        since = min(watermark, reference - timedelta(days=1))
    signals: list[dict] = []
    for pick in watched["picks"]:
        signals.extend(
            scan_symbol(
                conn,
                pick["symbol"],
                since=since,
                through=reference,
                config=config,
            )
        )
    signals.sort(key=lambda item: (item["date"], item["symbol"], item["action"]))
    sizing_gaps = sum(
        1 for item in signals if item["action"] == "enter" and item["sizing"]["quantity"] == 0
    )
    return {
        "as_of": reference,
        "since": since,
        "first_run": watermark is None and canonical_cutoff is None,
        "scanned": len(watched["picks"]),
        "signals": signals,
        "sizing_gaps": sizing_gaps,
    }


def render(report: dict) -> str:
    lines = [
        f"SWING SIGNALS as_of={report['as_of']} scanned={report['scanned']} "
        f"new={len(report['signals'])}",
    ]
    for item in report["signals"]:
        base = (
            f"{item['action'].upper():<5} {item['symbol']:<16} {item['date']} "
            f"close={item['close']:.2f}"
        )
        if item["action"] == "enter":
            sizing = item["sizing"]
            if sizing["quantity"] > 0:
                base += (
                    f" qty={sizing['quantity']} stop={sizing['stop']:.2f} "
                    f"deploy={sizing['capital_to_deploy']:.0f} "
                    f"maxloss={sizing['maximum_loss_at_stop']:.0f}"
                )
            else:
                base += f" sizing=UNAVAILABLE({sizing['reason']})"
        lines.append(base)
    if report["sizing_gaps"]:
        lines.append(f"sizing_gaps={report['sizing_gaps']}")
    return "\n".join(lines)


def advance_watermark(conn, report: dict, *, updated_at: dt) -> None:
    current = db.get_watermark(conn, WATERMARK_KIND)
    if current is None or report["as_of"] >= current:
        db.set_watermark(
            conn,
            WATERMARK_KIND,
            report["as_of"],
            detail=f"signals={len(report['signals'])}",
            updated_at=updated_at,
        )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(prog="invest-signals")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--config", default=watchlist.DEFAULT_CONFIG)
    parser.add_argument("--out", default="data/swing-latest.txt")
    parser.add_argument(
        "--report-only", action="store_true", help="do not advance the signal watermark"
    )
    args = parser.parse_args(argv)
    if args.out == "":
        parser.error("--out must be a path; state must never advance silently")
    conn = db.connect(args.db)
    try:
        db.init_schema(conn)
        config = watchlist.load_config(args.config)
        report = run_scan(conn, config)
        text = render(report)
        print(text)
        if args.out and not args.report_only:
            watchlist.atomic_write(args.out, text)
            advance_watermark(conn, report=report, updated_at=dt.now(UTC))
    except (ValueError, OSError) as exc:
        log.error("%s", exc)
        return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
