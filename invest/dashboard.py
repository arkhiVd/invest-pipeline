"""Localhost-only, read-only Streamlit research dashboard."""

from __future__ import annotations

import json
import re
from datetime import UTC
from datetime import datetime as dt
from urllib.parse import urlencode

import pandas as pd
import streamlit as st

from invest import ui_data, vbrs

DISCLAIMER = "Research context only. No trade instruction is produced."
SYMBOL_RE = re.compile(r"[A-Z0-9][A-Z0-9&.\-]{0,31}\Z")


@st.cache_data(show_spinner=False)
def _cached_query(name, identity):
    del identity
    return ui_data.query(name)


@st.cache_data(show_spinner=False)
def _cached_identity(file_identity):
    del file_identity
    return ui_data.snapshot_identity()


def _load(name):
    try:
        stat_result = ui_data.UI_DB.stat()
        file_identity = (stat_result.st_ino, stat_result.st_size, stat_result.st_mtime_ns)
        identity = _cached_identity(file_identity)
        columns, rows = _cached_query(name, identity)
        return pd.DataFrame(rows, columns=columns), None
    except Exception as exc:
        return (
            pd.DataFrame(),
            f"Query unavailable: {type(exc).__name__}. The projection was not changed.",
        )


def _utc_display(frame):
    displayed = frame.copy()
    for column in displayed.columns:
        if column.endswith("_at"):
            displayed[column] = pd.to_datetime(displayed[column], utc=True, errors="coerce")
    return displayed.rename(
        columns={column: f"{column}_utc" for column in displayed if column.endswith("_at")}
    )


def _csv_bytes(frame):
    exported = frame.drop(columns=["detail_url"], errors="ignore").copy()
    for column in exported.select_dtypes(include=["object", "str"]):
        exported[column] = exported[column].map(
            lambda value: (
                f"'{value}"
                if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@"))
                else value
            )
        )
    return exported.to_csv(index=False).encode("utf-8")


def _download(frame, key):
    st.download_button(
        "Download displayed rows (CSV)",
        _csv_bytes(_utc_display(frame)),
        file_name=f"invest-{key}.csv",
        mime="text/csv",
        key=f"download-{key}",
    )


def _symbol_links(frame, column, market):
    linked = frame.copy()
    if column in linked:
        linked["detail_url"] = linked[column].map(
            lambda value: (
                f"/symbol?{urlencode({'market': market, 'ticker': value})}" if value else None
            )
        )
    return linked


def _table(name, empty="No rows are available in this snapshot."):
    frame, error = _load(name)
    if error:
        st.error(error)
    elif frame.empty:
        st.info(empty)
    else:
        st.dataframe(_utc_display(frame), width="stretch", hide_index=True)
        _download(frame, name)
    return frame


FRESH_DAYS = {
    "nav": 3,
    "prices": 3,
    "index_prices": 3,
    "pe": 3,
    "fundamentals": 120,
    "kite": 35,
    "vested": 35,
    "research": 7,
    "news": 3,
    "swing_signals": 3,
}


def _age_label(value, kind=None):
    if value is None or pd.isna(value):
        return "missing"
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    days = (pd.Timestamp(dt.now(UTC)) - stamp).days
    threshold = FRESH_DAYS.get(str(kind).lower(), 3)
    return "stale" if days > threshold else "fresh"


def _watermark_status(row):
    detail = str(row.get("detail") or "").lower()
    if "fail-open" in detail or "failed-open" in detail:
        return "failed-open"
    if any(token in detail for token in ("fail-loud", "failed-loud", "error", "failed")):
        return "failed-loud"
    return _age_label(row.get("last_date"), row.get("kind"))


def _page_header(title, kinds, fallback=None):
    st.header(title)
    marks, error = _load("watermarks")
    selected = pd.DataFrame() if error else marks[marks.kind.isin(kinds)]
    if not selected.empty:
        latest = selected.last_date.max()
        statuses = sorted({_watermark_status(row) for _, row in selected.iterrows()})
        st.caption(f"As of {latest} · {', '.join(statuses)} · source UTC dates")
        return
    if fallback:
        query_name, column, freshness_kind = fallback
        frame, fallback_error = _load(query_name)
        if not fallback_error and not frame.empty and frame[column].notna().any():
            latest = frame[column].max()
            st.caption(
                f"As of {latest} · {_age_label(latest, freshness_kind)} · projected source date"
            )
            return
    st.caption("As of unavailable · freshness unknown")


def _curated_table(frame, columns, key, detail_label="Show all columns"):
    available = [column for column in columns if column in frame]
    if "detail_url" in frame and "detail_url" not in available:
        available.append("detail_url")
    config = {"detail_url": st.column_config.LinkColumn("Detail", display_text="Open")}
    st.dataframe(
        _utc_display(frame[available]),
        width="stretch",
        hide_index=True,
        column_config=config,
    )
    hidden = [column for column in frame if column not in available]
    if hidden:
        with st.expander(detail_label):
            st.dataframe(
                _utc_display(frame),
                width="stretch",
                hide_index=True,
                column_config=config,
            )
    _download(frame[available], key)


def home():
    st.header("Home and pipeline freshness")
    meta, meta_error = _load("metadata")
    if meta_error:
        st.error(meta_error)
    elif meta.empty:
        st.info("Projection metadata is missing. Health is unavailable.")
    else:
        published = pd.Timestamp(meta.iloc[0]["published_at"]).tz_convert("UTC")
        st.caption(
            f"Projection readable · {published.isoformat()} · schema "
            f"{meta.iloc[0]['source_schema_version']} · "
            f"{meta.iloc[0]['projection_version']} · fingerprint "
            f"{str(meta.iloc[0]['projection_fingerprint'])[:12]}… · "
            "readability does not prove nightly health"
        )
    kpi_rows, kpi_error = _load("home_kpis")
    if kpi_error or kpi_rows.empty:
        survivor_count = watchlist_count = india_value = us_value = headline_count = None
    else:
        kpi_row = kpi_rows.iloc[0]
        survivor_count = kpi_row.survivor_count
        watchlist_count = kpi_row.watchlist_count
        india_value = kpi_row.india_value
        us_value = kpi_row.us_value
        headline_count = kpi_row.headline_count
    kpis = st.columns(5)
    kpis[0].metric(
        "Current survivors", "Unavailable" if survivor_count is None else f"{survivor_count:,}"
    )
    kpis[1].metric(
        "Eligible swing rows",
        "Unavailable" if watchlist_count is None else f"{watchlist_count:,}",
    )
    kpis[2].metric(
        "India equity/ETF", "Unavailable" if india_value is None else f"INR {india_value:,.0f}"
    )
    kpis[3].metric("US holdings", "Unavailable" if us_value is None else f"USD {us_value:,.2f}")
    kpis[4].metric(
        "Recent headlines", "Unavailable" if headline_count is None else f"{headline_count:,}"
    )
    if kpi_error:
        st.error("Home KPIs are unavailable because the bounded aggregate query failed.")
    st.caption(
        "KPIs use only bounded rows in the current verified projection; "
        "currencies are not combined."
    )
    marks, marks_error = _load("watermarks")
    if marks_error:
        st.error(marks_error)
    elif marks.empty:
        st.info("No source watermarks are available.")
    else:
        summary = " · ".join(
            f"{mark['kind']} {_watermark_status(mark)} as of {mark['last_date']}"
            for _, mark in marks.iterrows()
        )
        st.caption(f"Source watermarks · {summary}")
    st.warning(
        "Nightly service result is unavailable in the projection; readable does not "
        "mean successful."
    )


def research():
    _page_header(
        "Deterministic screens and model research",
        set(),
        ("research", "as_of", "research"),
    )
    st.warning(
        "Deterministic screen survivor, not a recommendation. Model rationale is untrusted narrative."  # noqa: E501
    )
    frame, error = _load("research")
    if error:
        st.error(error)
    elif frame.empty:
        st.info("No current research survivors are stored.")
    else:

        def safe_json(value):
            if not value:
                return "unavailable"
            try:
                return json.dumps(json.loads(value), indent=2)
            except (TypeError, json.JSONDecodeError):
                return "unavailable: malformed stored JSON"

        for col in (
            "predicates_json",
            "screen_metrics_json",
            "components_json",
            "red_flags_json",
        ):
            frame[col] = frame[col].map(safe_json)
        frame = _symbol_links(frame, "symbol", "IN")
        _curated_table(
            frame,
            [
                "symbol",
                "screen_id",
                "as_of",
                "total_score",
                "owned_in_latest_snapshot",
                "rationale",
                "source",
                "score_methodology",
            ],
            "research",
            "Show predicates, components, flags, and model details",
        )

    st.subheader("Deterministic research rank")
    st.caption(
        "Research review order only. Binary screen membership is unchanged. "
        "Codex scores and news are not rank inputs. No trade instruction is produced."
    )
    rank_frame, rank_error = _load("research_rank")
    if rank_error:
        st.error(rank_error)
    elif rank_frame.empty:
        st.info("No deterministic rank history is available in this snapshot.")
    else:
        rank_frame = _symbol_links(rank_frame, "symbol", "IN")
        _curated_table(
            rank_frame,
            [
                "research_rank",
                "symbol",
                "score",
                "evidence_completeness",
                "status",
                "source_as_of",
                "methodology_version",
            ],
            "deterministic-rank",
            "Show missing components and recorded time",
        )
    st.subheader("Rank components")
    _table("rank_components", "No deterministic rank components are available.")
    st.subheader("Rank inputs and provenance")
    _table("rank_inputs", "No deterministic rank inputs are available.")
    st.subheader("Rank history")
    _table("rank_history", "No deterministic rank history is available.")


def swing():
    _page_header(
        "Swing watchlist and generated signals",
        {"bhavcopy_daily", "index_close_daily", "swing_signals"},
        ("swing", "as_of", "prices"),
    )
    st.warning(
        "A generated signal is not a confirmed position or instruction. EMA21 is a sizing stop only when stored on the signal day."  # noqa: E501
    )
    frame, error = _load("swing")
    if error:
        st.error(error)
    elif frame.empty:
        st.info("No eligible NIFTY 100 watchlist rows are available. Check gap evidence.")
    else:
        frame = _symbol_links(frame, "symbol", "IN")
        _curated_table(
            frame,
            [
                "rank",
                "symbol",
                "close",
                "as_of",
                "beta",
                "observations",
                "ema_state",
                "freshness",
                "gap_reason",
            ],
            "swing-watchlist",
            "Show EMA details",
        )
    st.subheader("Signal watermark")
    _table("swing_watermark", "The swing signal watermark is unavailable.")
    st.subheader("Generated signals")
    _table("signal_history", "No generated signal events are stored in this snapshot.")
    st.subheader("Operator-confirmed research positions")
    _table("research_positions", "No research-position lifecycle has been confirmed.")
    st.subheader("Position state history")
    _table("position_history", "No position state events are stored.")
    st.subheader("Watchlist change history")
    _table("watchlist_history", "No persisted watchlist history is available.")
    st.subheader("Screen membership history")
    _table("screen_history", "No persisted screen membership history is available.")
    st.caption(
        "Position state changes require explicit operator input. Broker evidence and generated "
        "signals cannot open or close a research position."
    )


def mf():
    _page_header("Mutual funds and VBRS", set(), ("pe", "nav_date", "pe"))
    funds, funds_error = _load("mf")
    if funds_error:
        st.error(funds_error)
    elif funds.empty:
        st.info("No computed mutual-fund comparison is available.")
    else:
        for column in (
            "fund_return",
            "category_avg_return",
            "sd",
            "upside_cr",
            "downside_cr",
        ):
            funds[column] = funds[column] * 100
        funds = funds.rename(
            columns={
                "fund_return": "fund_return_percent",
                "category_avg_return": "category_avg_return_percent",
                "sd": "annualized_sd_percent",
                "upside_cr": "upside_capture_percent",
                "downside_cr": "downside_capture_percent",
            }
        )
        _curated_table(
            funds,
            [
                "display_name",
                "lookback",
                "fund_return_percent",
                "category_avg_return_percent",
                "result",
                "annualized_sd_percent",
                "beta",
                "sharpe",
            ],
            "mf-metrics",
            "Show capture ratios, sources, and methodology",
        )
    st.subheader("Nifty valuation history")
    pe = _table("pe", "No Nifty PE/PB/DY history is available.")
    if not pe.empty and pd.notna(pe.iloc[0].pe):
        latest = pe.iloc[0]
        config = vbrs.load_config()
        cash = vbrs.cash_position(float(latest.pe), float(config["median_pe"]), config)
        zone_col, cash_col = st.columns(2)
        zone_col.metric("Latest VBRS zone", vbrs.zone(float(latest.pe), config))
        cash_col.metric("Model cash allocation", f"{cash:.2%}")
        st.caption(
            f"Source: {latest.source}; as of {latest.nav_date}; methodology: config/vbrs.json."
        )
        pe["cash_allocation_pct"] = pe.pe.map(
            lambda value: (
                vbrs.cash_position(float(value), float(config["median_pe"]), config) * 100
                if pd.notna(value)
                else None
            )
        )
        history = pe.sort_values("nav_date").set_index("nav_date")
        st.caption("Valuation ratios: PE and PB are multiples; DY is percentage points.")
        st.line_chart(history[["pe", "pb", "dy"]])
        st.caption("Model cash allocation (%)")
        st.line_chart(history[["cash_allocation_pct"]])
        st.dataframe(
            pe[["nav_date", "pe", "cash_allocation_pct", "source", "fetched_at"]].rename(
                columns={"pe": "pe_multiple", "cash_allocation_pct": "cash_allocation_percent"}
            ),
            width="stretch",
            hide_index=True,
        )
    elif not pe.empty:
        st.warning("VBRS unavailable: the latest PE input is missing.")
    st.caption("Canonical workbook: data/exports/MFs_export_<data-end>.xlsx")


def _portfolio_accounting(provider):
    performance_rows, performance_error = _load("portfolio_performance")
    allocation, allocation_error = _load("portfolio_allocation")
    completeness, completeness_error = _load("accounting_completeness")
    if performance_error or allocation_error or completeness_error:
        st.error(performance_error or allocation_error or completeness_error)
        return
    performance_rows = performance_rows[performance_rows.provider == provider]
    completeness = completeness[completeness.provider == provider]
    st.subheader("Portfolio accounting")
    if completeness.empty:
        st.info("No accounting completeness assessment is published for this account.")
    else:
        _curated_table(
            completeness,
            [
                "account_scope",
                "coverage_start",
                "coverage_end",
                "transactions_status",
                "cash_flows_status",
                "income_status",
                "valuations_status",
                "fx_status",
            ],
            f"{provider.lower()}-accounting-completeness",
            "Show assumptions, exclusions, residuals, and methodology",
        )
    if performance_rows.empty:
        st.info("No source-gated performance result is published for this account.")
        return
    _curated_table(
        performance_rows,
        [
            "account_scope",
            "metric",
            "status",
            "value",
            "currency",
            "coverage_start",
            "coverage_end",
            "methodology_version",
        ],
        f"{provider.lower()}-performance",
        "Show assumptions, exclusions, residuals, and calculation time",
    )
    unavailable = performance_rows[performance_rows.status == "UNAVAILABLE"]
    if not unavailable.empty:
        st.warning(
            "Unavailable metrics have no numeric value. Their exclusions name the missing evidence."
        )
    result_ids = set(performance_rows.result_id)
    allocation = allocation[allocation.result_id.isin(result_ids)]
    if not allocation.empty:
        _curated_table(
            allocation,
            [
                "dimension",
                "bucket",
                "native_value",
                "base_value",
                "weight",
                "source_as_of",
            ],
            f"{provider.lower()}-performance-allocation",
            "Show allocation result identifiers",
        )


def india():
    _page_header("India portfolio", set(), ("broker_run", "snapshot_date", "kite"))
    st.warning("Latest verified snapshot only. The dashboard cannot refresh or log in to a broker.")
    _table("broker_run", "No verified Zerodha snapshot is available.")
    holdings, holdings_error = _load("broker_holdings")
    if holdings_error:
        st.error(holdings_error)
    elif holdings.empty:
        st.info("No equity or ETF holdings are available.")
    else:
        holdings = _symbol_links(holdings, "tradingsymbol", "IN")
        _curated_table(
            holdings,
            [
                "tradingsymbol",
                "quantity",
                "average_price",
                "last_price",
                "pnl",
                "current_research_survivor",
            ],
            "india-holdings",
            "Show position and day-change details",
        )
    funds, funds_error = _load("broker_mf")
    if funds_error:
        st.error(funds_error)
    elif funds.empty:
        st.info("No Coin mutual-fund holdings are available.")
    else:
        _curated_table(
            funds,
            ["fund", "quantity", "average_price", "last_price", "pnl", "mapping_status"],
            "coin-funds",
            "Show all Coin fields",
        )
    if not holdings.empty:
        current = (holdings.quantity * holdings.last_price).sum()
        basis = (holdings.quantity * holdings.average_price).sum()
        st.metric("Equity/ETF current value", f"INR {current:,.2f}")
        st.metric("Equity/ETF invested basis", f"INR {basis:,.2f}")
        st.metric("Source P&L", f"INR {holdings.pnl.sum():,.2f}")
        st.metric("Derived value minus basis", f"INR {current - basis:,.2f}")
        owned_research = holdings[holdings.current_research_survivor]
        outside = holdings[~holdings.current_research_survivor]
        st.write(f"Owned current research survivors: {len(owned_research)}")
        st.write(f"Owned outside current research: {len(outside)}")
        st.caption(
            "ETF/non-EQ rows without a current research symbol remain outside "
            "current research; no identity is inferred."
        )
    if not funds.empty:
        mf_current = (funds.quantity * funds.last_price).sum()
        mf_basis = (funds.quantity * funds.average_price).sum()
        st.metric("Coin MF current value", f"INR {mf_current:,.2f}")
        st.metric("Coin MF invested basis", f"INR {mf_basis:,.2f}")
        st.metric("Coin MF source P&L", f"INR {funds.pnl.sum():,.2f}")
        st.metric("Coin MF derived difference", f"INR {mf_current - mf_basis:,.2f}")
        counts = funds.mapping_status.value_counts()
        st.write(
            "MF mapping: "
            f"tracked={counts.get('tracked', 0)}, "
            f"untracked={counts.get('untracked', 0)}, "
            f"ambiguous={counts.get('ambiguous', 0)}."
        )
    st.caption(
        "Source P&L and derived differences are labels, not audited performance. Research reconciliation may be unavailable in this snapshot."  # noqa: E501
    )
    _portfolio_accounting("ZERODHA")


def us():
    _page_header("US portfolio", set(), ("vested_run", "snapshot_date", "vested"))
    st.warning(
        "The Vested XLSX flattens managed products. No managed-product attribution is inferred."
    )
    _portfolio_accounting("VESTED")
    _table("vested_run", "No verified Vested snapshot is available.")
    holdings, holdings_error = _load("vested_holdings")
    if holdings_error:
        st.error(holdings_error)
    elif holdings.empty:
        st.info("No US holdings are available.")
    else:
        holdings = _symbol_links(holdings, "ticker", "US")
        _curated_table(
            holdings,
            [
                "ticker",
                "name",
                "quantity",
                "current_value_usd",
                "invested_usd",
                "return_usd",
                "return_pct",
            ],
            "us-holdings",
            "Show price and cost details",
        )
    if not holdings.empty:
        st.metric("Current value", f"USD {holdings.current_value_usd.sum():,.2f}")
        current = holdings.current_value_usd.sum()
        invested = holdings.invested_usd.sum()
        holdings["weight_pct"] = holdings.current_value_usd / current * 100 if current else 0
        st.metric("Invested basis", f"USD {invested:,.2f}")
        st.metric("Derived difference", f"USD {current - invested:,.2f}")
        st.write(
            f"Largest holding concentration: {holdings.weight_pct.max():.2f}% "
            f"across {len(holdings)} holdings."
        )
        st.bar_chart(
            holdings.set_index("ticker")["current_value_usd"],
            height=350,
            horizontal=True,
            x_label="Current value (USD)",
            y_label="Ticker",
        )


def news():
    _page_header("Headline-only classifications", set(), ("news", "published_at", "news"))
    st.warning(
        "Headline-only classification. It is contextual research and cannot change a screen, score, or trade state."  # noqa: E501
    )
    frame, error = _load("news")
    if error:
        st.error(error)
    elif frame.empty:
        st.info("No classified headlines are available.")
    else:
        symbols = ["All", *sorted(frame.symbol.unique())]
        selected = st.selectbox("Symbol", symbols)
        filtered = frame if selected == "All" else frame[frame.symbol == selected]
        pages = max(1, (len(filtered) + 49) // 50)
        page = st.number_input("Page", min_value=1, max_value=pages, value=1, step=1)
        start = (int(page) - 1) * 50
        displayed = filtered.iloc[start : start + 50].copy()
        st.dataframe(
            _utc_display(displayed),
            width="stretch",
            hide_index=True,
            column_config={
                "detail_url": st.column_config.LinkColumn("Detail", display_text="Open")
            },
        )
        _download(displayed, f"news-page-{int(page)}-{selected}")
        st.caption(
            "Grouped by symbol with fixed pages of 50. Exact stored URLs, publisher, "
            "publication time UTC, model, and methodology are shown."
        )


def _symbol_catalog():
    catalog = {"IN": set(), "US": set()}
    frame, error = _load("symbols")
    if error:
        return catalog
    for market, symbol in frame[["market", "symbol"]].itertuples(index=False, name=None):
        normalized = str(symbol).upper()
        if market in catalog and SYMBOL_RE.fullmatch(normalized):
            catalog[market].add(normalized)
    return catalog


def _symbol_options(catalog):
    return [f"{market}:{symbol}" for market in ("IN", "US") for symbol in sorted(catalog[market])]


def _requested_symbol(value, market, catalog):
    requested = str(value or "").strip().upper()
    namespace = str(market or "").strip().upper()
    if not requested and not namespace:
        return "", None
    if namespace not in catalog or not SYMBOL_RE.fullmatch(requested):
        return f"{namespace}:{requested}", "malformed"
    key = f"{namespace}:{requested}"
    if requested not in catalog[namespace]:
        return key, "unknown"
    return key, None


def symbol_detail():
    st.header("Symbol detail")
    catalog = _symbol_catalog()
    symbols = _symbol_options(catalog)
    requested, issue = _requested_symbol(
        st.query_params.get("ticker", ""), st.query_params.get("market", ""), catalog
    )
    if issue == "malformed":
        st.error("Malformed market or ticker. Use an exact projected instrument.")
        return
    selected = st.selectbox(
        "Market and ticker",
        symbols,
        index=symbols.index(requested) if requested in symbols else None,
        placeholder="Select an exact projected instrument",
    )
    if issue == "unknown":
        st.warning("Unknown instrument in the current projection. No fuzzy match was attempted.")
    if not selected:
        st.info("Choose a projected instrument to reconcile its current context.")
        return
    market, ticker = selected.split(":", 1)
    st.query_params.from_dict({"market": market, "ticker": ticker})
    st.caption(
        f"{market} namespace · exact current-snapshot reconciliation only; "
        "cross-market identity is never inferred."
    )
    if market == "IN":
        sections = (
            (
                "Screen and research",
                "research",
                "symbol",
                ["symbol", "screen_id", "as_of", "total_score", "rationale", "source"],
            ),
            (
                "Swing",
                "swing",
                "symbol",
                ["rank", "symbol", "close", "as_of", "beta", "ema_state", "freshness"],
            ),
            (
                "India holding",
                "broker_holdings",
                "tradingsymbol",
                ["tradingsymbol", "quantity", "average_price", "last_price", "pnl"],
            ),
        )
    else:
        sections = (
            (
                "US holding",
                "vested_holdings",
                "ticker",
                ["ticker", "name", "quantity", "current_value_usd", "invested_usd", "return_pct"],
            ),
        )
    if market == "IN" and ticker not in catalog["US"]:
        sections += (
            (
                "Recent headlines",
                "news",
                "symbol",
                [
                    "symbol",
                    "title",
                    "publisher",
                    "published_at",
                    "sentiment",
                    "event_type",
                    "materiality",
                ],
            ),
        )
    elif market == "IN":
        st.warning(
            "Headline reconciliation is omitted because this ticker exists in both "
            "market namespaces."
        )
    else:
        st.caption("US headline reconciliation is unavailable without a market namespace.")
    for title, query_name, column, columns in sections:
        st.subheader(title)
        frame, error = _load(query_name)
        if error:
            st.error(error)
            continue
        exact = frame[frame[column].astype(str).str.upper() == ticker]
        if exact.empty:
            st.info(f"No exact {title.lower()} rows in this projection.")
        else:
            _curated_table(
                exact,
                columns,
                f"symbol-{market}-{ticker}-{query_name}",
                "Show all projected fields",
            )


def main():
    st.set_page_config(page_title="Invest research", layout="wide")
    pages = [
        st.Page("pages/home.py", title="Home", url_path="home", default=True),
        st.Page("pages/research.py", title="Screens and research", url_path="research"),
        st.Page("pages/swing.py", title="Swing", url_path="swing"),
        st.Page("pages/mf_vbrs.py", title="MF and VBRS", url_path="mf-vbrs"),
        st.Page("pages/india.py", title="India portfolio", url_path="india"),
        st.Page("pages/us.py", title="US portfolio", url_path="us"),
        st.Page("pages/news.py", title="News", url_path="news"),
        st.Page("pages/symbol.py", title="Symbol detail", url_path="symbol"),
    ]
    current = st.navigation(pages, expanded=True)
    st.title("Invest research dashboard")
    st.caption(DISCLAIMER)
    current.run()
    if current.url_path is not None:
        market = st.sidebar.selectbox("Lookup market", ("IN", "US"))
        lookup = st.sidebar.text_input("Global symbol lookup", placeholder="Exact ticker")
        ticker = lookup.strip().upper()
        if ticker and SYMBOL_RE.fullmatch(ticker):
            st.sidebar.page_link(
                pages[-1],
                label=f"Open {market}:{ticker}",
                query_params={"market": market, "ticker": ticker},
            )
        elif ticker:
            st.sidebar.error("Malformed ticker")


if __name__ == "__main__":
    main()
