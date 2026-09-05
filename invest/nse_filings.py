"""Official NSE corporate-filing discovery and XBRL primitives (T3.2a).

Canonical fundamentals come from retained exchange filings, not from an
analytics site's derived values. Both regimes are required:
- legacy financial-results endpoint (history through roughly FY2024)
- integrated-filing endpoint (post-2024 financials)
- shareholding-pattern endpoint (including promoter pledge/encumbrance)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
from dataclasses import dataclass
from datetime import UTC, date
from datetime import datetime as dt
from http.cookiejar import CookieJar
from pathlib import Path
from urllib import parse, request
from xml.etree import ElementTree as ET

from invest import db

log = logging.getLogger("invest.nse_filings")

BASE = "https://www.nseindia.com"
SOURCE = "nse_corporate_xbrl"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = PROJECT_ROOT / "data/invest.duckdb"
DEFAULT_XBRL_ROOT = PROJECT_ROOT / "data/xbrl"
MAX_JSON_BYTES = 5 * 1024 * 1024
MAX_XBRL_BYTES = 10 * 1024 * 1024
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
)
FINANCIAL_REFERER = f"{BASE}/companies-listing/corporate-filings-financial-results"
ARCHIVE_RE = re.compile(r"https://nsearchives\.nseindia\.com/\S+\.xml", re.I)

PLEDGE_FACT = "WhetherAnySharesHeldByPromotersAreEncumberedUnderPledged"
SYMBOL_RE = re.compile(r"[A-Z0-9&._-]+")
FINANCIAL_FACTS = {
    "RevenueFromOperations",
    "ProfitLossForPeriod",
    "FinanceCosts",
    "Equity",
    "EquityShareCapital",
    "OtherEquity",
    "BorrowingsCurrent",
    "BorrowingsNoncurrent",
    "DebtEquityRatio",
    "InterestServiceCoverageRatio",
    "BasicEarningsLossPerShareFromContinuingOperations",
    # Non-bank P&L expense subtotal -> operating margin.
    "Expenses",
    # Banking-taxonomy equivalents (verified against HDFCBANK/SBIN/FEDERALBNK
    # filings); fundamentals.py normalizes these at query time:
    "InterestEarned",  # ~ RevenueFromOperations
    "InterestExpended",  # ~ FinanceCosts
    "ProfitLossForThePeriod",  # ~ ProfitLossForPeriod
    "Capital",  # ~ EquityShareCapital
    "ReservesAndSurplus",  # ~ OtherEquity
    "Borrowings",  # ~ BorrowingsCurrent + BorrowingsNoncurrent
    "BasicEarningsPerShareBeforeExtraordinaryItems",  # ~ Basic EPS
    # Audit-corrected deterministic ratios and cash flow. Annual Q4 filings
    # expose these in official Ind-AS/NBFC XBRLs.
    "ProfitBeforeTax",
    "ProfitBeforeExceptionalItemsAndTax",
    "ProfitBeforeExtraordinaryItemsAndTax",
    "TaxExpense",
    "DepreciationDepletionAndAmortisationExpense",
    "OperatingExpenses",
    "OperatingProfitBeforeProvisionAndContingencies",
    "TotalAssets",
    "Assets",
    "CurrentAssets",
    "CurrentLiabilities",
    "CurrentRatio",
    "CashFlowsFromUsedInOperatingActivities",
    "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
    "PurchaseOfTangibleAssetsClassifiedAsInvestingActivities",
    # Piotroski gross-margin inputs.
    "CostOfMaterialsConsumed",
    "PurchasesOfStockInTrade",
    "ChangesInInventoriesOfFinishedGoodsWorkInProgressAndStockInTrade",
    # SHP percentage facts are selected by their dimensional category in
    # fundamentals.py; values are decimal fractions in official NSE XBRL.
    "ShareholdingAsAPercentageOfTotalNumberOfShares",
    # Legacy results expose equity through these filing-level values, often
    # under synthetic FourD refs rather than a real xbrli instant context.
    "PaidUpValueOfEquityShareCapital",
    "ReserveExcludingRevaluationReserves",
}


class SourceError(RuntimeError):
    """NSE transport or contract failure."""


@dataclass(frozen=True)
class FilingRef:
    symbol: str
    filing_type: str
    period_end: date | None
    consolidation: str | None
    taxonomy: str | None
    xbrl_url: str


@dataclass(frozen=True)
class DiscoveryResult:
    refs: tuple[FilingRef, ...]
    legacy_ok: bool
    integrated_ok: bool
    shareholding_ok: bool
    legacy_refs: int
    integrated_refs: int
    shareholding_refs: int
    errors: tuple[str, ...]


@dataclass(frozen=True)
class XbrlFact:
    name: str
    value: str
    context_ref: str | None
    unit_ref: str | None
    decimals: str | None


@dataclass(frozen=True)
class XbrlContext:
    context_id: str
    start_date: str | None
    end_date: str | None
    instant: str | None
    dimensions: tuple[tuple[str, str], ...]


def valid_symbol(symbol: str) -> str:
    normalized = symbol.strip().upper()
    if normalized in {".", ".."} or not normalized or not SYMBOL_RE.fullmatch(normalized):
        raise ValueError("symbol contains unsupported characters")
    return normalized


def _opener():
    jar = CookieJar()
    opener = request.build_opener(request.HTTPCookieProcessor(jar))
    opener.addheaders = [
        ("User-Agent", USER_AGENT),
        ("Accept", "application/json, text/plain, */*"),
        ("Referer", FINANCIAL_REFERER),
    ]
    try:
        opener.open(BASE, timeout=30).close()
    except Exception as exc:  # noqa: BLE001 - warmup may be challenged yet set cookies
        log.warning("NSE warmup failed (%s); continuing with cookie jar", type(exc).__name__)
    return opener


def _get_json(opener, path: str, params: dict) -> object:
    url = f"{BASE}{path}?{parse.urlencode(params)}"
    try:
        with opener.open(url, timeout=30) as response:
            body = response.read(MAX_JSON_BYTES + 1)
            if len(body) > MAX_JSON_BYTES:
                raise SourceError(f"NSE response too large for {path}")
            return json.loads(body)
    except Exception as exc:  # noqa: BLE001 - normalized to a source-safe error
        raise SourceError(f"NSE request failed for {path}: {type(exc).__name__}") from exc


def _parse_date(value) -> date | None:
    if not value:
        return None
    clean = str(value).strip().title()
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return dt.strptime(clean, fmt).date()
        except ValueError:
            pass
    return None


def _taxonomy(url: str) -> str | None:
    name = Path(parse.urlparse(url).path).name.upper()
    for marker in ("BANKING", "NBFC", "INDAS", "GAAP", "SHP"):
        if marker in name:
            return marker.lower()
    return None


def discover_with_status(symbol: str, *, fetcher=None) -> DiscoveryResult:
    """Discover refs while preserving each endpoint's health/completeness."""
    symbol = valid_symbol(symbol)
    get = fetcher
    if get is None:
        opener = _opener()
        get = lambda path, params: _get_json(opener, path, params)  # noqa: E731

    common = {"index": "equities", "symbol": symbol}
    sections: dict[str, object] = {}
    errors: list[str] = []
    for name, path, params in (
        ("legacy", "/api/corporates-financial-results", {**common, "period": "Annual"}),
        ("integrated", "/api/integrated-filing-results", common),
        ("shareholding", "/api/corporate-share-holdings-master", common),
    ):
        try:
            sections[name] = get(path, params)
        except SourceError as exc:
            log.warning("%s section unavailable for %s (%s)", name, symbol, exc)
            errors.append(name)

    legacy = sections.get("legacy")
    integrated = sections.get("integrated")
    shareholding = sections.get("shareholding")
    legacy_ok = isinstance(legacy, list)
    shareholding_ok = isinstance(shareholding, list)
    integrated_rows = integrated.get("data") if isinstance(integrated, dict) else None
    integrated_ok = isinstance(integrated_rows, list)
    for ok, label in (
        (legacy_ok, "legacy"),
        (integrated_ok, "integrated"),
        (shareholding_ok, "shareholding"),
    ):
        if not ok:
            log.warning("%s section shape drifted for %s; continuing partial", label, symbol)

    # Normalize failed/shape-drift sections to empty only after preserving
    # their status. This makes partial discovery explicit rather than raising
    # an accidental TypeError while iterating None.
    legacy_rows = legacy if legacy_ok else []
    integrated_rows = integrated_rows if integrated_ok else []
    shareholding_rows = shareholding if shareholding_ok else []

    refs: list[FilingRef] = []
    for row in legacy_rows:
        url = row.get("xbrl")
        if url:
            refs.append(
                FilingRef(
                    symbol,
                    "financial_annual_legacy",
                    _parse_date(row.get("toDate")),
                    row.get("consolidated"),
                    _taxonomy(url),
                    url,
                )
            )
    for row in integrated_rows:
        if "financial" not in str(row.get("type", "")).lower():
            continue
        url = row.get("xbrl")
        if url:
            refs.append(
                FilingRef(
                    symbol,
                    "financial_integrated",
                    _parse_date(row.get("qe_Date")),
                    row.get("consolidated"),
                    _taxonomy(url),
                    url,
                )
            )
    for row in shareholding_rows:
        url = row.get("xbrl")
        if url:
            refs.append(
                FilingRef(
                    symbol,
                    "shareholding",
                    _parse_date(row.get("date")),
                    None,
                    _taxonomy(url),
                    url,
                )
            )

    # Revised/resubmitted records may duplicate an immutable archive URL.
    # NSE also emits literal placeholders like ".../xbrl/-" on some legacy
    # rows; those are not real files and must never reach selection.
    real = [r for r in refs if ARCHIVE_RE.fullmatch(r.xbrl_url)]
    dropped = len(refs) - len(real)
    if dropped:
        log.warning("%s: dropped %d placeholder XBRL ref(s)", symbol, dropped)
    deduped = tuple({ref.xbrl_url: ref for ref in real}.values())
    counts = {
        kind: sum(ref.filing_type == kind for ref in deduped)
        for kind in (
            "financial_annual_legacy",
            "financial_integrated",
            "shareholding",
        )
    }
    shape_errors = [
        name
        for name, ok in (
            ("legacy", legacy_ok),
            ("integrated", integrated_ok),
            ("shareholding", shareholding_ok),
        )
        if not ok and name not in errors
    ]
    return DiscoveryResult(
        refs=deduped,
        legacy_ok=legacy_ok,
        integrated_ok=integrated_ok,
        shareholding_ok=shareholding_ok,
        legacy_refs=counts["financial_annual_legacy"],
        integrated_refs=counts["financial_integrated"],
        shareholding_refs=counts["shareholding"],
        errors=tuple(sorted((*errors, *shape_errors))),
    )


def discover(symbol: str, *, fetcher=None) -> list[FilingRef]:
    """Compatibility wrapper returning refs only; total failure stays loud."""
    result = discover_with_status(symbol, fetcher=fetcher)
    if not (result.legacy_ok or result.integrated_ok or result.shareholding_ok):
        raise SourceError(
            f"NSE discovery unusable for {valid_symbol(symbol)} "
            f"(failed sections: {result.errors or 'shape drift'})"
        )
    return list(result.refs)


def fetch_xbrl(url: str, *, opener=None) -> bytes:
    if not ARCHIVE_RE.fullmatch(url):
        raise ValueError("refusing non-NSE archive XBRL URL")
    opener = opener or _opener()
    req = request.Request(url, headers={"User-Agent": USER_AGENT, "Referer": BASE})
    try:
        with opener.open(req, timeout=30) as response:
            body = response.read(MAX_XBRL_BYTES + 1)
    except Exception as exc:  # noqa: BLE001
        raise SourceError(f"NSE XBRL download failed: {type(exc).__name__}") from exc
    if len(body) > MAX_XBRL_BYTES:
        raise SourceError("NSE XBRL response exceeds 10 MiB limit")
    if not body.lstrip().startswith(b"<"):
        raise SourceError("NSE XBRL response is not XML")
    return body


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def xbrl_contexts(xml: bytes) -> dict[str, XbrlContext]:
    """Parse period and dimensional identity needed to select the correct fact."""
    root = ET.fromstring(xml)
    contexts: dict[str, XbrlContext] = {}
    for element in root.iter():
        if local_name(element.tag) != "context" or not element.get("id"):
            continue
        values: dict[str, str] = {}
        dimensions: list[tuple[str, str]] = []
        for child in element.iter():
            name = local_name(child.tag)
            text = (child.text or "").strip()
            if name in {"startDate", "endDate", "instant"} and text:
                values[name] = text
            if name in {"explicitMember", "typedMember"} and text:
                dimensions.append((child.get("dimension", ""), text))
        context_id = element.get("id")
        contexts[context_id] = XbrlContext(
            context_id=context_id,
            start_date=values.get("startDate"),
            end_date=values.get("endDate"),
            instant=values.get("instant"),
            dimensions=tuple(dimensions),
        )

    # Some BSE-generated legacy instances reference OneD/FourD but define
    # unrelated xbrli context IDs. Their own reporting-period facts provide
    # the authoritative dates; synthesize only missing referenced contexts.
    reporting: dict[str, dict[str, str]] = {}
    for element in root.iter():
        name = local_name(element.tag)
        context_ref = element.get("contextRef")
        text = (element.text or "").strip()
        if (
            context_ref
            and text
            and name
            in {
                "DateOfStartOfReportingPeriod",
                "DateOfEndOfReportingPeriod",
            }
        ):
            key = "startDate" if name.startswith("DateOfStart") else "endDate"
            slot = reporting.setdefault(context_ref, {})
            if key in slot and slot[key] != text:
                raise SourceError(f"conflicting {key} metadata for synthetic context {context_ref}")
            slot[key] = text
    for context_ref, dates in reporting.items():
        if context_ref not in contexts and dates.get("startDate") and dates.get("endDate"):
            contexts[context_ref] = XbrlContext(
                context_id=context_ref,
                start_date=dates["startDate"],
                end_date=dates["endDate"],
                instant=None,
                dimensions=(),
            )
    return contexts


def financial_facts(xml: bytes) -> dict[str, list[XbrlFact]]:
    """Extract facts with identity; callers MUST select via ``xbrl_contexts``."""
    root = ET.fromstring(xml)
    out: dict[str, list[XbrlFact]] = {}
    for element in root.iter():
        name = local_name(element.tag)
        if name in FINANCIAL_FACTS and element.text and element.text.strip():
            fact = XbrlFact(
                name=name,
                value=element.text.strip(),
                context_ref=element.get("contextRef"),
                unit_ref=element.get("unitRef"),
                decimals=element.get("decimals"),
            )
            out.setdefault(name, []).append(fact)
    return out


def promoter_pledged(xml: bytes) -> bool | None:
    """Return an unambiguous filing-level promoter pledge flag."""
    root = ET.fromstring(xml)
    values: set[bool] = set()
    for element in root.iter():
        if local_name(element.tag) != PLEDGE_FACT:
            continue
        value = (element.text or "").strip().lower()
        if value in {"true", "1", "yes"}:
            values.add(True)
        elif value in {"false", "0", "no"}:
            values.add(False)
    if len(values) > 1:
        raise SourceError("contradictory promoter pledge facts in one XBRL filing")
    return next(iter(values)) if values else None


def retain(
    conn,
    ref: FilingRef,
    xml: bytes,
    *,
    root: Path = DEFAULT_XBRL_ROOT,
    fetched_at: dt | None = None,
) -> Path:
    """Content-address raw XBRL and upsert its filing manifest idempotently."""
    symbol = valid_symbol(ref.symbol)
    digest = hashlib.sha256(xml).hexdigest()
    target = (root / symbol / f"{digest}.xml").resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_bytes(xml)
    db.upsert_stock_filing(
        conn,
        xbrl_url=ref.xbrl_url,
        symbol=ref.symbol,
        source=SOURCE,
        filing_type=ref.filing_type,
        period_end=ref.period_end,
        consolidation=ref.consolidation,
        taxonomy=ref.taxonomy,
        content_sha256=digest,
        raw_path=str(target),
        fetched_at=fetched_at or dt.now(UTC),
    )
    return target


def ingest_filing(
    conn,
    ref: FilingRef,
    xml: bytes,
    *,
    fetched_at: dt | None = None,
    keep_raw: bool = False,
    root: Path | None = None,
) -> dict:
    """Store one filing's manifest, parsed contexts, and filtered facts.

    Canonical storage is the compact fact/context tables; raw XML files are an
    opt-in audit extra (keep_raw=True). Shareholding filings additionally store
    their promoter-pledge boolean as a fact row.
    """
    fetched_at = fetched_at or dt.now(UTC)
    digest = hashlib.sha256(xml).hexdigest()
    if keep_raw:
        retain(conn, ref, xml, root=root or DEFAULT_XBRL_ROOT, fetched_at=fetched_at)
    else:
        db.upsert_stock_filing(
            conn,
            xbrl_url=ref.xbrl_url,
            symbol=ref.symbol,
            source=SOURCE,
            filing_type=ref.filing_type,
            period_end=ref.period_end,
            consolidation=ref.consolidation,
            taxonomy=ref.taxonomy,
            content_sha256=digest,
            raw_path=None,
            fetched_at=fetched_at,
        )

    facts = financial_facts(xml)
    if ref.filing_type == "shareholding":
        pledged = promoter_pledged(xml)
        if pledged is not None:
            facts[PLEDGE_FACT] = [
                XbrlFact(
                    name=PLEDGE_FACT,
                    value=str(pledged).lower(),
                    context_ref="",
                    unit_ref=None,
                    decimals=None,
                )
            ]
    contexts = xbrl_contexts(xml)
    n_ctx = db.upsert_filing_contexts(conn, ref.xbrl_url, contexts)
    n_facts = db.upsert_filing_facts(conn, ref.xbrl_url, facts)
    return {"digest": digest, "contexts": n_ctx, "facts": n_facts}


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(prog="invest-nse-filings")
    parser.add_argument("symbol")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--retain-latest", action="store_true")
    parser.add_argument(
        "--filing-type",
        choices=("financial_annual_legacy", "financial_integrated", "shareholding"),
        default="shareholding",
        help="filing class selected by --retain-latest (default: shareholding)",
    )
    args = parser.parse_args(argv)
    try:
        refs = discover(args.symbol)
        counts = {
            kind: sum(r.filing_type == kind for r in refs)
            for kind in sorted({r.filing_type for r in refs})
        }
        print(f"NSE XBRL {args.symbol.upper()}: {len(refs)} filings {counts}")
        if args.retain_latest:
            candidates = [r for r in refs if r.filing_type == args.filing_type]
            if not candidates:
                raise SourceError(f"no {args.filing_type} filings discovered")
            latest = max(candidates, key=lambda r: (r.period_end or date.min, r.xbrl_url))
            xml = fetch_xbrl(latest.xbrl_url)
            conn = db.connect(args.db)
            try:
                db.init_schema(conn)
                path = retain(conn, latest, xml)
            finally:
                conn.close()
            print(f"retained {latest.filing_type} {latest.period_end} -> {path}")
    except (SourceError, ValueError, ET.ParseError) as exc:
        log.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
