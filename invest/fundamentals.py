"""Deterministic historical fundamentals from stored XBRL facts (T3.2c).

Reads stock_filing_fact/context/filing rows, builds fiscal-year (Apr-Mar)
series per symbol with consolidated-preferred dedup, normalizes banking
taxonomy element names at query time (no synthetic rows in storage), and
writes one stock_fundamentals snapshot per symbol (source
'nse_xbrl_computed'). Idempotent: replaying identical filings produces
identical rows via the upsert change-guard.

Metric conventions:
- ROCE(FY) = EBIT / capital employed, where EBIT=PBT+finance cost and
  capital employed=assets-current liabilities (equity+debt fallback).
- ROE(FY)  = net profit / equity at FY end.
- OPM      = operating profit / revenue: Ind-AS adds finance cost and
  depreciation back to revenue-total expenses; banking uses its direct
  operating-profit-before-provisions fact.
- CAGR(n)  = (v[t] / v[t-n]) ** (1/n) - 1 over fiscal-year ends exactly n
  years apart; any missing/negative base -> NULL.
- Averages require the FULL window (3 or 5 fiscal years); partial windows
  stay NULL rather than quietly meaning something else.

Current ratio, three-year aggregate FCF, direct EPS comparisons, and latest
promoter/FII holdings are derived only when their official facts are complete.
Price-derived fields remain in the price/screen layer.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Iterable
from datetime import UTC, date
from datetime import datetime as dt

from invest import db
from invest.nse_filings import DEFAULT_DB

log = logging.getLogger("invest.fundamentals")

SOURCE = "nse_xbrl_computed"
METHODOLOGY = "stock-source-2026.2-audit"
DEFAULT = DEFAULT_DB

# Canonical metric -> candidate fact names, first group with ALL members
# present wins. Bank equivalents normalize here, not in storage.
_REVENUE = ("RevenueFromOperations", "InterestEarned")
_PROFIT = ("ProfitLossForPeriod", "ProfitLossForThePeriod")
_FINANCE_COST = ("FinanceCosts", "InterestExpended")
_EPS = (
    "BasicEarningsLossPerShareFromContinuingOperations",
    "BasicEarningsPerShareBeforeExtraordinaryItems",
)
_EXPENSES = ("Expenses",)
_PBT = (
    "ProfitBeforeTax",
    "ProfitBeforeExceptionalItemsAndTax",
    "ProfitBeforeExtraordinaryItemsAndTax",
)
_TAX = ("TaxExpense",)
_DEPRECIATION = ("DepreciationDepletionAndAmortisationExpense",)
_ASSETS = ("Assets", "TotalAssets")
_CURRENT_ASSETS = ("CurrentAssets",)
_CURRENT_LIABILITIES = ("CurrentLiabilities",)
_CFO = ("CashFlowsFromUsedInOperatingActivities",)
_CAPEX = (
    "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
    "PurchaseOfTangibleAssetsClassifiedAsInvestingActivities",
)
_BANK_OPERATING_PROFIT = ("OperatingProfitBeforeProvisionAndContingencies",)
_GROSS_COSTS = (
    "CostOfMaterialsConsumed",
    "PurchasesOfStockInTrade",
    "ChangesInInventoriesOfFinishedGoodsWorkInProgressAndStockInTrade",
)
_EQUITY_GROUPS = (
    ("Equity",),
    ("EquityShareCapital", "OtherEquity"),
    ("Capital", "ReservesAndSurplus"),
    ("PaidUpValueOfEquityShareCapital", "ReserveExcludingRevaluationReserves"),
)
_LEGACY_INSTANT_FACTS = {
    "PaidUpValueOfEquityShareCapital",
    "ReserveExcludingRevaluationReserves",
}
_DEBT_GROUPS = (
    ("BorrowingsCurrent", "BorrowingsNoncurrent"),
    ("Borrowings",),
)

_PLEDGE_FACT = "WhetherAnySharesHeldByPromotersAreEncumberedUnderPledged"

_ROWS_SQL = """
SELECT f.period_end, f.consolidation, f.xbrl_url, fa.fact_name, fa.value,
       ctx.context_id, ctx.start_date, ctx.end_date, ctx.instant
FROM stock_filing_fact fa
JOIN stock_filing f ON f.xbrl_url = fa.xbrl_url
LEFT JOIN stock_filing_context ctx
  ON ctx.xbrl_url = fa.xbrl_url AND ctx.context_id = fa.context_ref
WHERE f.symbol = ?
  AND f.filing_type IN ('financial_annual_legacy', 'financial_integrated')
  AND (ctx.dimensions IS NULL OR ctx.dimensions = '')
"""


def _num(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _is_consolidated(consolidation: str | None) -> bool:
    return (consolidation or "").strip().lower() == "consolidated"


def _fy_span(start: date | None, end: date | None, fy_end: date) -> bool:
    """True when the context covers the full Apr 1..Mar 31 fiscal year."""
    return (
        start is not None
        and end == fy_end
        and start.month == 4
        and start.day == 1
        and start.year == fy_end.year - 1
    )


def _first_present(values: dict[str, float], groups: Iterable[tuple[str, ...]]) -> float | None:
    for group in groups:
        if all(name in values for name in group):
            return sum(values[name] for name in group)
    return None


def _pick_filings(rows: list[tuple]) -> dict[date, dict[str, dict[str, float]]]:
    """Build {fy_end: {'duration': {...}, 'instant': {...}}} per symbol.

    Ranking within a (fy_end, consolidation) bucket: consolidated first,
    then the RICHEST filing (most stored facts) so a later revision
    supersedes the original it amends, then URL for determinism.

    Duration recovery (evidence: legacy BSE-generated XBRLs label YTD
    values with quarter-length spans — AARTIIND FY24 carries FY profit
    4164.8M under context FourD claiming Jan..Mar). Layered rules:
    1. exact Apr..Mar span wins;
    2. else contexts ending at fy_end whose id starts with 'Four' — the
       BSE generator's cumulative-column convention — when they agree on
       one value;
    3. else a unique non-extractable candidate.
    Ambiguous candidates are dropped, never averaged.
    """
    seen: dict[tuple[date, bool, str], dict] = {}
    for period_end, consolidation, url, fact_name, value, cid, start, end, instant in rows:
        if period_end is None or period_end.month != 3 or period_end.day != 31:
            continue
        key = (period_end, _is_consolidated(consolidation), url)
        entry = seen.setdefault(key, {"dx": {}, "df": {}, "inst": {}})
        numeric = _num(value)
        if numeric is None:
            continue
        if instant == period_end:
            entry["inst"][fact_name] = numeric
        elif start is not None and end == period_end:
            if fact_name in _LEGACY_INSTANT_FACTS and (cid or "").lower() == "fourd":
                # Synthetic legacy fallback only; a real instant fact wins
                # regardless of document row order.
                entry["inst"].setdefault(fact_name, numeric)
            elif _fy_span(start, end, period_end):
                entry["dx"][fact_name] = numeric
            else:
                entry["df"].setdefault(fact_name, []).append(((cid or "").lower(), start, numeric))
    best: dict[tuple[date, bool], tuple[tuple, dict[str, dict[str, float]]]] = {}
    for (fy_end, cons, url), entry in seen.items():
        duration = dict(entry["dx"])
        for fact_name, candidates in entry["df"].items():
            if fact_name in duration:
                continue
            four = sorted({v for cid, _start, v in candidates if cid.startswith("four")})
            # Rule 3 has no naming-convention evidence, so demand a
            # near-full-year span before trusting a lone candidate.
            fullspan = sorted({v for _cid, start, v in candidates if (fy_end - start).days >= 300})
            if len(four) == 1:
                duration[fact_name] = four[0]
            elif not four and len(fullspan) == 1:
                duration[fact_name] = fullspan[0]
        facts = {"duration": duration, "instant": entry["inst"]}
        richness = len(duration) + len(entry["inst"])
        rank = (not cons, -richness, url)
        current = best.get((fy_end, cons))
        if current is None or rank < current[0]:
            best[(fy_end, cons)] = (rank, facts)
    merged: dict[date, dict[str, dict[str, float]]] = {}
    for (fy_end, cons), (_rank, facts) in best.items():
        slot = merged.setdefault(fy_end, {})
        # Consolidated wins; standalone fills a gap only when absent.
        if cons or not slot:
            slot.update(facts)
    return dict(sorted(merged.items()))


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator


def _cagr(series: dict[int, float], span: int, latest_year: int) -> float | None:
    base_year = latest_year - span
    if base_year not in series or latest_year not in series:
        return None
    base, final = series[base_year], series[latest_year]
    if base <= 0 or final <= 0:
        return None
    return (final / base) ** (1.0 / span) - 1.0


def fy_metrics(fy_map: dict[date, dict[str, dict[str, float]]]) -> dict[str, object]:
    """Compute all derived metrics from a symbol's fiscal-year map."""
    out: dict[str, object] = {
        "as_of": None,
        "operating_margin": None,
        "revenue_growth_yoy": None,
        "profit_growth_yoy": None,
        "eps_growth_yoy": None,
        "debt_to_equity": None,
        "interest_coverage": None,
        "promoter_pledged": None,
        "avg_roe_3y": None,
        "avg_roe_5y": None,
        "avg_roce_3y": None,
        "avg_roce_5y": None,
        "revenue_cagr_3y": None,
        "profit_cagr_3y": None,
        "eps_cagr_3y": None,
        "free_cash_flow": None,
        "free_cash_flow_3y": None,
        "current_ratio": None,
        "eps": None,
        "eps_previous": None,
        "piotroski_score": None,
        "promoter_holding": None,
        "fii_holding": None,
        "roe": None,
        "roce": None,
    }
    if not fy_map:
        return out
    latest_end = max(fy_map)
    out["as_of"] = latest_end
    latest = fy_map[latest_end]
    dur, inst = latest["duration"], latest["instant"]

    revenue = next((dur[n] for n in _REVENUE if n in dur), None)
    profit = next((dur[n] for n in _PROFIT if n in dur), None)
    finance_cost = next((dur[n] for n in _FINANCE_COST if n in dur), None)
    expenses = next((dur[n] for n in _EXPENSES if n in dur), None)
    pbt = next((dur[n] for n in _PBT if n in dur), None)
    tax = next((dur[n] for n in _TAX if n in dur), None)
    depreciation = next((dur[n] for n in _DEPRECIATION if n in dur), None)
    equity = _first_present(inst, _EQUITY_GROUPS)
    debt = _first_present(inst, _DEBT_GROUPS)
    assets = next((inst[n] for n in _ASSETS if n in inst), None)
    current_liabilities = next((inst[n] for n in _CURRENT_LIABILITIES if n in inst), None)

    # EBIT uses PBT + finance cost. If PBT is absent, reconstruct it from
    # PAT + tax; never silently call PAT+interest "EBIT".
    ebit_base = pbt
    if ebit_base is None and profit is not None and tax is not None:
        ebit_base = profit + tax
    ebit = ebit_base + finance_cost if ebit_base is not None and finance_cost is not None else None
    capital_employed = (
        assets - current_liabilities
        if assets is not None and current_liabilities is not None
        else (equity + debt if equity is not None and debt is not None else None)
    )
    out["roce"] = _ratio(ebit, capital_employed)
    out["roe"] = _ratio(profit, equity)

    bank_operating_profit = next((dur[n] for n in _BANK_OPERATING_PROFIT if n in dur), None)
    if revenue and revenue > 0:
        if bank_operating_profit is not None:
            out["operating_margin"] = bank_operating_profit / revenue
        elif expenses is not None and finance_cost is not None and depreciation is not None:
            # Ind-AS Expenses includes finance cost and depreciation. Add
            # those back to obtain operating profit before D&A/interest.
            out["operating_margin"] = (revenue - expenses + finance_cost + depreciation) / revenue

    series: dict[str, dict[int, float]] = {
        "rev": {},
        "pft": {},
        "eps": {},
        "fcf": {},
        "cfo": {},
        "assets": {},
        "debt_assets": {},
        "current": {},
        "share_capital": {},
        "gross_margin": {},
        "asset_turnover": {},
    }
    roe_by_year: dict[int, float] = {}
    roce_by_year: dict[int, float] = {}
    gross_cost_tags: dict[int, frozenset[str]] = {}
    for fy_end, entry in fy_map.items():
        d, i = entry["duration"], entry["instant"]
        rev = next((d[n] for n in _REVENUE if n in d), None)
        pft = next((d[n] for n in _PROFIT if n in d), None)
        eps = next((d[n] for n in _EPS if n in d), None)
        eq = _first_present(i, _EQUITY_GROUPS)
        dbt = _first_present(i, _DEBT_GROUPS)
        fc = next((d[n] for n in _FINANCE_COST if n in d), None)
        pbt_y = next((d[n] for n in _PBT if n in d), None)
        tax_y = next((d[n] for n in _TAX if n in d), None)
        assets_y = next((i[n] for n in _ASSETS if n in i), None)
        current_liab_y = next((i[n] for n in _CURRENT_LIABILITIES if n in i), None)
        current_assets_y = next((i[n] for n in _CURRENT_ASSETS if n in i), None)
        share_capital_y = i.get("EquityShareCapital")
        cfo = next((d[n] for n in _CFO if n in d), None)
        capex = next((d[n] for n in _CAPEX if n in d), None)
        gross_cost_names = [n for n in _GROSS_COSTS if n in d]
        gross_cost_values = [d[n] for n in gross_cost_names]
        year = fy_end.year
        if rev is not None:
            series["rev"][year] = rev
        if pft is not None:
            series["pft"][year] = pft
        if eps is not None:
            series["eps"][year] = eps
        if cfo is not None:
            series["cfo"][year] = cfo
        if cfo is not None and capex is not None:
            series["fcf"][year] = cfo - abs(capex)
        if assets_y is not None and assets_y > 0:
            series["assets"][year] = assets_y
            if dbt is not None:
                series["debt_assets"][year] = dbt / assets_y
            if rev is not None:
                series["asset_turnover"][year] = rev / assets_y
        current_y = _ratio(current_assets_y, current_liab_y)
        if current_y is not None:
            series["current"][year] = current_y
        if share_capital_y is not None:
            series["share_capital"][year] = share_capital_y
        if rev is not None and rev > 0 and gross_cost_values:
            series["gross_margin"][year] = (rev - sum(gross_cost_values)) / rev
            gross_cost_tags[year] = frozenset(gross_cost_names)
        roe = _ratio(pft, eq)
        if roe is not None:
            roe_by_year[year] = roe
        ebit_base_y = pbt_y
        if ebit_base_y is None and pft is not None and tax_y is not None:
            ebit_base_y = pft + tax_y
        ebit_y = ebit_base_y + fc if ebit_base_y is not None and fc is not None else None
        capital_y = (
            assets_y - current_liab_y
            if assets_y is not None and current_liab_y is not None
            else (eq + dbt if eq is not None and dbt is not None else None)
        )
        roce = _ratio(ebit_y, capital_y)
        if roce is not None:
            roce_by_year[year] = roce

    ly = latest_end.year
    prev = ly - 1

    def _yoy(current: float | None, base: float | None) -> float | None:
        # Same discipline as CAGR: missing or non-positive base -> NULL.
        if current is None or base is None or base <= 0:
            return None
        return current / base - 1.0

    out["revenue_growth_yoy"] = _yoy(revenue, series["rev"].get(prev))
    out["profit_growth_yoy"] = _yoy(profit, series["pft"].get(prev))
    eps_now = next((dur[n] for n in _EPS if n in dur), None)
    out["eps_growth_yoy"] = _yoy(eps_now, series["eps"].get(prev))

    for window, key_prefix in ((3, "3y"), (5, "5y")):
        window_years = [ly - offset for offset in range(window)]
        if all(y in roe_by_year for y in window_years):
            out[f"avg_roe_{key_prefix}"] = sum(roe_by_year[y] for y in window_years) / window
        if all(y in roce_by_year for y in window_years):
            out[f"avg_roce_{key_prefix}"] = sum(roce_by_year[y] for y in window_years) / window

    out["revenue_cagr_3y"] = _cagr(series["rev"], 3, ly)
    out["profit_cagr_3y"] = _cagr(series["pft"], 3, ly)
    out["eps_cagr_3y"] = _cagr(series["eps"], 3, ly)
    out["eps"] = series["eps"].get(ly)
    out["eps_previous"] = series["eps"].get(prev)
    out["free_cash_flow"] = series["fcf"].get(ly)
    fcf_years = [ly - offset for offset in range(3)]
    if all(y in series["fcf"] for y in fcf_years):
        out["free_cash_flow_3y"] = sum(series["fcf"][y] for y in fcf_years)

    # Standard nine-signal Piotroski F-score. Require every official input;
    # an incomplete score is more dangerous than a NULL that fails closed.
    required = (
        ("pft", ly),
        ("pft", prev),
        ("assets", ly),
        ("assets", prev),
        ("cfo", ly),
        ("debt_assets", ly),
        ("debt_assets", prev),
        ("current", ly),
        ("current", prev),
        ("share_capital", ly),
        ("share_capital", prev),
        ("gross_margin", ly),
        ("gross_margin", prev),
        ("asset_turnover", ly),
        ("asset_turnover", prev),
    )
    comparable_gross_costs = gross_cost_tags.get(ly) is not None and gross_cost_tags.get(
        ly
    ) == gross_cost_tags.get(prev)
    if comparable_gross_costs and all(year in series[name] for name, year in required):
        roa = series["pft"][ly] / series["assets"][ly]
        previous_roa = series["pft"][prev] / series["assets"][prev]
        signals = (
            roa > 0,
            series["cfo"][ly] > 0,
            roa > previous_roa,
            series["cfo"][ly] > series["pft"][ly],
            series["debt_assets"][ly] < series["debt_assets"][prev],
            series["current"][ly] > series["current"][prev],
            series["share_capital"][ly] <= series["share_capital"][prev],
            series["gross_margin"][ly] > series["gross_margin"][prev],
            series["asset_turnover"][ly] > series["asset_turnover"][prev],
        )
        out["piotroski_score"] = sum(signals)

    direct_de = inst.get("DebtEquityRatio")
    out["debt_to_equity"] = direct_de if direct_de is not None else _ratio(debt, equity)
    direct_current = dur.get("CurrentRatio")
    if direct_current is None:
        direct_current = inst.get("CurrentRatio")
    current_assets = next((inst[n] for n in _CURRENT_ASSETS if n in inst), None)
    computed_current = _ratio(current_assets, current_liabilities)
    out["current_ratio"] = computed_current if computed_current is not None else direct_current
    # Official direct ISCR tags are inconsistently scaled (e.g. 0.0555 for
    # a reported 5.55). Use the deterministic EBIT/finance-cost ratio.
    out["interest_coverage"] = _ratio(ebit, finance_cost)
    return out


def promoter_pledged(conn, symbol: str) -> bool | None:
    row = conn.execute(
        """
        SELECT fa.value FROM stock_filing_fact fa
        JOIN stock_filing f ON f.xbrl_url = fa.xbrl_url
        WHERE f.symbol = ? AND fa.fact_name = ?
        ORDER BY f.period_end DESC NULLS LAST, f.xbrl_url DESC
        LIMIT 1
        """,
        [symbol, _PLEDGE_FACT],
    ).fetchone()
    if row is None:
        return None
    return row[0].strip().lower() == "true" if row[0] else None


def shareholding_percentages(conn, symbol: str) -> tuple[float | None, float | None]:
    """Latest official SHP promoter/FII fractions selected by XBRL dimension."""
    filing = conn.execute(
        """
        SELECT xbrl_url FROM stock_filing
        WHERE symbol = ? AND filing_type = 'shareholding'
        ORDER BY period_end DESC NULLS LAST, xbrl_url DESC
        LIMIT 1
        """,
        [symbol],
    ).fetchone()
    if filing is None:
        return None, None
    rows = conn.execute(
        """
        SELECT fa.value, ctx.dimensions
        FROM stock_filing_fact fa
        JOIN stock_filing_context ctx
          ON ctx.xbrl_url = fa.xbrl_url AND ctx.context_id = fa.context_ref
        WHERE fa.xbrl_url = ?
          AND fa.fact_name = 'ShareholdingAsAPercentageOfTotalNumberOfShares'
          AND ctx.dimensions IS NOT NULL
        """,
        [filing[0]],
    ).fetchall()
    by_member: dict[str, set[float]] = {}
    for raw, dimensions in rows:
        value = _num(raw)
        if value is None or not 0 <= value <= 1:
            continue
        member = dimensions.rsplit("=", 1)[-1].rsplit(":", 1)[-1]
        by_member.setdefault(member, set()).add(value)

    def unique(member: str) -> float | None:
        values = by_member.get(member, set())
        return next(iter(values)) if len(values) == 1 else None

    promoter = unique("ShareholdingOfPromoterAndPromoterGroupMember")
    fii = unique("InstitutionsForeignMember")
    if fii is None:
        category_values = [
            next(iter(values))
            for member, values in by_member.items()
            if "ForeignPortfolioInvestorCategory" in member and len(values) == 1
        ]
        fii = sum(category_values) if category_values else None
    return promoter, fii


def compute_symbol(conn, symbol: str) -> dict[str, object] | None:
    rows = conn.execute(_ROWS_SQL, [symbol]).fetchall()
    if not rows:
        return None
    fy_map = _pick_filings(rows)
    metrics = fy_metrics(fy_map)
    metrics["promoter_pledged"] = promoter_pledged(conn, symbol)
    metrics["promoter_holding"], metrics["fii_holding"] = shareholding_percentages(conn, symbol)
    audit = {
        str(fy_end): {
            "duration": entry["duration"],
            "instant": entry["instant"],
        }
        for fy_end, entry in sorted(fy_map.items())
    }
    metrics["raw_json"] = json.dumps(audit, sort_keys=True, separators=(",", ":"))
    return metrics


def run(conn, symbols: list[str] | None = None) -> dict:
    if symbols is None:
        symbols = [
            row[0]
            for row in conn.execute(
                """
                SELECT DISTINCT f.symbol FROM stock_filing f
                JOIN stock_universe u ON u.symbol = f.symbol
                WHERE f.filing_type IN ('financial_annual_legacy', 'financial_integrated')
                ORDER BY f.symbol
                """
            ).fetchall()
        ]
    now = dt.now(UTC)
    written = skipped = errors = 0
    for symbol in symbols:
        try:
            metrics = compute_symbol(conn, symbol)
            if metrics is None or metrics.get("as_of") is None:
                skipped += 1
                continue
            as_of = metrics.pop("as_of")
            db.upsert_stock_fundamental(
                conn,
                symbol=symbol,
                as_of=as_of,
                source=SOURCE,
                methodology_version=METHODOLOGY,
                fetched_at=now,
                **metrics,
            )
        except Exception as exc:  # noqa: BLE001 - one bad symbol must not stop the pass
            log.warning("fundamentals %s failed (%s: %s)", symbol, type(exc).__name__, exc)
            errors += 1
            continue
        written += 1
    return {"written": written, "skipped": skipped, "errors": errors, "requested": len(symbols)}


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(prog="invest-fundamentals")
    parser.add_argument("--db", default=str(DEFAULT))
    parser.add_argument("--symbols", help="comma-separated subset (default: all crawlable)")
    args = parser.parse_args(argv)

    conn = db.connect(args.db)
    try:
        db.init_schema(conn)
        symbols = None
        if args.symbols:
            symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        stats = run(conn, symbols)
    finally:
        conn.close()
    print(
        f"fundamentals: requested={stats['requested']} written={stats['written']} "
        f"skipped={stats['skipped']} errors={stats['errors']} source={SOURCE}"
    )
    error_limit = max(10, stats["requested"] // 100)
    return 1 if stats["errors"] > error_limit else 0


if __name__ == "__main__":
    sys.exit(main())
