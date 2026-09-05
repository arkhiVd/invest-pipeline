"""Read-only broker snapshot reconciliation against deterministic stock research."""

from __future__ import annotations

import sys
from pathlib import Path

from invest import db, kite, research, watchlist


def latest_run_id(conn) -> str:
    row = conn.execute(
        "SELECT run_id FROM broker_snapshot_run ORDER BY fetched_at DESC, run_id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        raise ValueError("no broker snapshot is stored")
    if not kite.snapshot_integrity(conn, row[0]):
        raise ValueError("latest broker snapshot failed integrity verification")
    return row[0]


def _universe_maps(conn) -> tuple[set[str], dict[str, str | None]]:
    rows = conn.execute(
        "SELECT symbol, isin FROM stock_universe WHERE is_active ORDER BY symbol"
    ).fetchall()
    symbols = {row[0] for row in rows}
    isin_candidates: dict[str, list[str]] = {}
    for symbol, isin in rows:
        if isin:
            isin_candidates.setdefault(isin, []).append(symbol)
    isin_map = {
        isin: candidates[0] if len(candidates) == 1 else None
        for isin, candidates in isin_candidates.items()
    }
    return symbols, isin_map


def _resolve_symbol(
    exchange: str, tradingsymbol: str, isin: str | None, symbols: set[str], isin_map: dict
) -> str | None:
    if exchange == "NSE" and tradingsymbol in symbols:
        return tradingsymbol
    if isin and isin_map.get(isin):
        return isin_map[isin]
    if tradingsymbol in symbols:
        return tradingsymbol
    return None


def reconcile(conn) -> dict:
    run_id = latest_run_id(conn)
    symbols, isin_map = _universe_maps(conn)
    holdings = []
    owned_symbols: set[str] = set()
    for exchange, tradingsymbol, product, isin, quantity in conn.execute(
        "SELECT exchange, tradingsymbol, product, isin, quantity "
        "FROM broker_holding WHERE run_id=? AND quantity > 0 "
        "ORDER BY exchange, tradingsymbol, product",
        [run_id],
    ).fetchall():
        resolved = _resolve_symbol(exchange, tradingsymbol, isin, symbols, isin_map)
        if resolved:
            owned_symbols.add(resolved)
        holdings.append(
            {
                "exchange": exchange,
                "tradingsymbol": tradingsymbol,
                "resolved_symbol": resolved,
                "product": product,
                "quantity": quantity,
            }
        )
    scheme_rows = conn.execute(
        "SELECT scheme_code, COALESCE(display_name, name), isin, isin2 FROM mf_scheme"
    ).fetchall()
    scheme_by_isin: dict[str, set[tuple[int, str]]] = {}
    for scheme_code, name, isin, isin2 in scheme_rows:
        for value in {isin, isin2} - {None}:
            scheme_by_isin.setdefault(value, set()).add((scheme_code, name))
    mutual_funds = []
    for symbol, fund, quantity in conn.execute(
        "SELECT tradingsymbol, fund, SUM(quantity) FROM broker_mf_holding "
        "WHERE run_id=? AND quantity > 0 GROUP BY tradingsymbol, fund "
        "ORDER BY fund, tradingsymbol",
        [run_id],
    ).fetchall():
        matches = sorted(scheme_by_isin.get(symbol, set()))
        mutual_funds.append(
            {
                "tradingsymbol": symbol,
                "fund": fund,
                "quantity": quantity,
                "tracked_name": matches[0][1] if len(matches) == 1 else None,
                "ambiguous": len(matches) > 1,
            }
        )
    candidate_items = research.candidates(conn)
    screen_sets: dict[str, set[str]] = {}
    for item in candidate_items:
        screen_sets.setdefault(item["symbol"], set()).update(item["screens"])
    candidate_screens = {symbol: sorted(screens) for symbol, screens in sorted(screen_sets.items())}
    candidates = set(candidate_screens)
    return {
        "run_id": run_id,
        "holdings": holdings,
        "owned_research": sorted(owned_symbols & candidates),
        "unowned_research": sorted(candidates - owned_symbols),
        "owned_not_research": sorted(owned_symbols - candidates),
        "unmatched_holdings": [item for item in holdings if item["resolved_symbol"] is None],
        "candidate_screens": candidate_screens,
        "mutual_funds": mutual_funds,
        "tracked_mutual_funds": [item for item in mutual_funds if item["tracked_name"]],
        "untracked_mutual_funds": [
            item for item in mutual_funds if item["tracked_name"] is None and not item["ambiguous"]
        ],
        "ambiguous_mutual_funds": [item for item in mutual_funds if item["ambiguous"]],
    }


def render(report: dict) -> str:
    lines = [
        f"PORTFOLIO RECONCILIATION run={report['run_id']}",
        "Research comparison only. No trade instruction is produced.",
        f"holdings={len(report['holdings'])} "
        f"owned_research={len(report['owned_research'])} "
        f"unowned_research={len(report['unowned_research'])} "
        f"unmatched={len(report['unmatched_holdings'])} "
        f"mutual_funds={len(report['mutual_funds'])} "
        f"mf_untracked={len(report['untracked_mutual_funds'])} "
        f"mf_ambiguous={len(report['ambiguous_mutual_funds'])}",
        "",
        "OWNED RESEARCH",
    ]
    lines.extend(
        f"  {symbol} screens={','.join(report['candidate_screens'][symbol])}"
        for symbol in report["owned_research"]
    )
    if not report["owned_research"]:
        lines.append("  none")
    lines.extend(["", "UNOWNED RESEARCH"])
    lines.extend(
        f"  {symbol} screens={','.join(report['candidate_screens'][symbol])}"
        for symbol in report["unowned_research"]
    )
    if not report["unowned_research"]:
        lines.append("  none")
    lines.extend(["", "OWNED OUTSIDE CURRENT RESEARCH"])
    lines.extend(f"  {symbol}" for symbol in report["owned_not_research"])
    if not report["owned_not_research"]:
        lines.append("  none")
    lines.extend(["", "UNMATCHED BROKER HOLDINGS"])
    lines.extend(
        f"  {item['exchange']}:{item['tradingsymbol']} product={item['product']} "
        f"quantity={item['quantity']:g}"
        for item in report["unmatched_holdings"]
    )
    if not report["unmatched_holdings"]:
        lines.append("  none")
    lines.extend(["", "TRACKED MUTUAL FUNDS"])
    lines.extend(
        f"  {item['fund']} tracked_as={item['tracked_name']} quantity={item['quantity']:g}"
        for item in report["tracked_mutual_funds"]
    )
    if not report["tracked_mutual_funds"]:
        lines.append("  none")
    lines.extend(["", "UNTRACKED MUTUAL FUNDS"])
    lines.extend(
        f"  {item['fund']} isin={item['tradingsymbol']} quantity={item['quantity']:g}"
        for item in report["untracked_mutual_funds"]
    )
    if not report["untracked_mutual_funds"]:
        lines.append("  none")
    lines.extend(["", "AMBIGUOUS MUTUAL FUND MATCHES"])
    lines.extend(
        f"  {item['fund']} isin={item['tradingsymbol']} quantity={item['quantity']:g}"
        for item in report["ambiguous_mutual_funds"]
    )
    if not report["ambiguous_mutual_funds"]:
        lines.append("  none")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="invest-portfolio")
    parser.add_argument("--db", default="data/invest.duckdb")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    conn = db.connect(args.db)
    try:
        db.init_schema(conn)
        report = reconcile(conn)
        text = render(report)
        if args.out:
            watchlist.atomic_write(str(args.out), text)
        print(text)
        return 0
    except (OSError, ValueError) as exc:
        print(f"portfolio reconciliation failed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
