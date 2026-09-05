"""Fixed-path, read-only query layer for the local research dashboard."""

from __future__ import annotations

from pathlib import Path

import duckdb

UI_DB = Path("data/ui/invest-ui.duckdb")
PRODUCTION_DB = Path("data/invest.duckdb")

QUERIES = {
    "metadata": "SELECT projection_version,source_schema_version,published_at,projection_fingerprint,row_counts_json,integrity_json FROM projection_metadata LIMIT 1",  # noqa: E501
    "watermarks": "SELECT kind,last_date,detail,updated_at FROM ingest_watermark ORDER BY kind LIMIT 100",  # noqa: E501
    "symbols": "SELECT market,symbol FROM (SELECT 'IN' market,symbol FROM ui_screen_survivor UNION SELECT 'IN',symbol FROM ui_swing_watchlist UNION SELECT 'IN',tradingsymbol FROM broker_holding UNION SELECT 'US',ticker FROM vested_holding) ORDER BY market,symbol LIMIT 1000",  # noqa: E501
    "home_kpis": "SELECT (SELECT count(DISTINCT symbol) FROM ui_screen_survivor) survivor_count,(SELECT count(DISTINCT symbol) FROM ui_swing_watchlist WHERE gap_reason IS NULL) watchlist_count,(SELECT coalesce(sum(quantity*last_price),0) FROM broker_holding) india_value,(SELECT coalesce(sum(current_value_usd),0) FROM vested_holding) us_value,(SELECT count(*) FROM news_classification) headline_count LIMIT 1",  # noqa: E501
    "research": "SELECT s.symbol,s.screen_id,s.as_of,s.source,s.methodology_version screen_methodology,s.predicates_json,s.metrics_json screen_metrics_json,r.snapshot_date,r.methodology_version score_methodology,r.components_json,r.total_score,r.rationale,r.red_flags_json,r.model,r.scored_at,exists(SELECT 1 FROM broker_holding h WHERE h.tradingsymbol=s.symbol AND h.quantity>0) owned_in_latest_snapshot FROM ui_screen_survivor s LEFT JOIN stock_research_score r ON r.symbol=s.symbol ORDER BY s.symbol,s.screen_id LIMIT 250",  # noqa: E501
    "research_rank": "SELECT s.symbol,s.research_rank,s.score,s.evidence_completeness,s.status,s.missing_components_json,r.source_as_of,r.recorded_at,r.methodology_version FROM ranking_symbol s JOIN ranking_run r USING(run_id) WHERE s.run_id=(SELECT run_id FROM ranking_run ORDER BY source_as_of DESC,recorded_at DESC LIMIT 1) ORDER BY s.research_rank NULLS LAST,s.symbol LIMIT 5000",  # noqa: E501
    "rank_components": "SELECT c.symbol,c.component,c.normalized_value,c.component_weight,c.weighted_contribution,c.missing_status FROM ranking_component c WHERE c.run_id=(SELECT run_id FROM ranking_run ORDER BY source_as_of DESC,recorded_at DESC LIMIT 1) ORDER BY c.symbol,c.component LIMIT 25000",  # noqa: E501
    "rank_inputs": "SELECT i.symbol,i.component,i.field,i.raw_value,i.unit,i.source,i.source_as_of,i.normalization_cohort,i.cohort_size,i.transform,i.direction,i.normalized_value,i.input_weight,i.component_weight,i.weighted_contribution,i.missing_status FROM ranking_input i WHERE i.run_id=(SELECT run_id FROM ranking_run ORDER BY source_as_of DESC,recorded_at DESC LIMIT 1) ORDER BY i.symbol,i.component,i.field LIMIT 60000",  # noqa: E501
    "rank_history": "SELECT s.symbol,s.research_rank,s.score,s.evidence_completeness,s.status,r.source_as_of,r.recorded_at,r.methodology_version FROM ranking_symbol s JOIN ranking_run r USING(run_id) ORDER BY r.source_as_of DESC,r.recorded_at DESC,s.research_rank NULLS LAST,s.symbol LIMIT 5000",  # noqa: E501
    "swing": "SELECT rank,symbol,close,as_of,beta,observations,ema10,ema21,ema_state,freshness,gap_reason FROM ui_swing_watchlist ORDER BY rank LIMIT 100",  # noqa: E501
    "signal_history": "SELECT symbol,action,signal_date,source_as_of,close,ema10,ema21,quantity,sizing_stop,capital_to_deploy,maximum_loss_at_stop,sizing_gap_reason,methodology_version,recorded_at FROM signal_event ORDER BY signal_date DESC,symbol,event_id LIMIT 500",  # noqa: E501
    "swing_watermark": "SELECT kind,last_date,detail,updated_at FROM ingest_watermark WHERE kind='swing_signals' LIMIT 1",  # noqa: E501
    "research_positions": "SELECT market,symbol,current_state,state_source_at,methodology_version,entry_sizing_stop,created_at,updated_at FROM research_position ORDER BY updated_at DESC LIMIT 500",  # noqa: E501
    "position_history": "SELECT p.market,p.symbol,e.from_state,e.to_state,e.source_at,e.recorded_at,e.methodology_version,e.actor,e.evidence_type FROM position_state_event e JOIN research_position p USING(position_id) ORDER BY e.source_at DESC LIMIT 1000",  # noqa: E501
    "screen_history": "SELECT screen_id,symbol,source_as_of,methodology_version,event_type FROM screen_membership_event ORDER BY source_as_of DESC,screen_id,symbol LIMIT 2000",  # noqa: E501
    "watchlist_history": "SELECT w.index_name,r.symbol,r.source_as_of,r.methodology_version,r.result,r.rank,r.close,r.beta,r.observations,r.evidence_as_of FROM watchlist_symbol_result r JOIN watchlist_run w USING(run_id) ORDER BY r.source_as_of DESC,r.symbol LIMIT 5000",  # noqa: E501
    "mf": "SELECT s.display_name,s.name,r.lookback,r.fund_return,r.category_avg_return,r.result,k.sd,k.beta,k.sharpe,k.upside_cr,k.downside_cr,r.benchmark,r.frequency,r.methodology_version,r.sources,r.calculated_at,coalesce(r.note,k.note) note FROM mf_scheme s JOIN mf_return_metrics r USING(scheme_code) LEFT JOIN mf_risk_metrics k ON k.scheme_code=r.scheme_code AND k.lookback=r.lookback WHERE s.is_active ORDER BY s.display_name,r.lookback LIMIT 250",  # noqa: E501
    "pe": "SELECT nav_date,pe,pb,dy,close,source,fetched_at FROM nifty_pe ORDER BY nav_date DESC LIMIT 730",  # noqa: E501
    "broker_run": "SELECT run_id,broker,snapshot_date,holding_count,mf_holding_count,fetched_at FROM broker_snapshot_run LIMIT 1",  # noqa: E501
    "broker_holdings": "SELECT h.exchange,h.tradingsymbol,h.product,h.quantity,h.average_price,h.last_price,h.close_price,h.pnl,h.day_change,h.day_change_percentage,exists(SELECT 1 FROM stock_research_score r WHERE r.symbol=h.tradingsymbol) current_research_survivor FROM broker_holding h ORDER BY h.tradingsymbol LIMIT 500",  # noqa: E501
    "broker_mf": "SELECT fund,quantity,pledged_quantity,average_price,last_price,pnl,last_price_date,tracked_name,mapping_status FROM broker_mf_holding ORDER BY fund LIMIT 500",  # noqa: E501
    "vested_run": "SELECT run_id,provider,snapshot_date,holding_count,current_value_usd,invested_usd,imported_at FROM vested_snapshot_run LIMIT 1",  # noqa: E501
    "vested_holdings": "SELECT ticker,name,quantity,current_price_usd,current_value_usd,average_cost_usd,invested_usd,return_usd,return_pct FROM vested_holding ORDER BY current_value_usd DESC LIMIT 500",  # noqa: E501
    "portfolio_performance": "SELECT result_id,provider,account_scope,metric,status,value,currency,coverage_start,coverage_end,methodology_version,assumptions_json,exclusions_json,residuals_json,calculated_at FROM ui_portfolio_performance ORDER BY provider,account_scope,metric LIMIT 100",  # noqa: E501
    "portfolio_allocation": "SELECT result_id,dimension,bucket,native_value,base_value,weight,source_as_of FROM ui_portfolio_allocation ORDER BY result_id,dimension,weight DESC LIMIT 1000",  # noqa: E501
    "accounting_completeness": "SELECT provider,account_scope,coverage_start,coverage_end,transactions_status,cash_flows_status,income_status,valuations_status,corporate_actions_status,fx_status,assumptions_json,exclusions_json,residuals_json,methodology_version,assessed_at FROM ui_accounting_completeness ORDER BY provider,account_scope LIMIT 20",  # noqa: E501
    "news": "SELECT e.symbol,a.title,a.url,a.publisher,a.published_at,c.sentiment,c.event_type,c.materiality,c.rationale,c.evidence_scope,c.model,c.methodology_version,c.classified_at FROM news_classification c JOIN news_article a USING(article_id) JOIN news_article_entity e ON e.article_id=c.article_id AND e.symbol=c.symbol ORDER BY a.published_at DESC LIMIT 500",  # noqa: E501
}


def connect():
    selected = UI_DB.resolve()
    if selected == PRODUCTION_DB.resolve() or selected.name == "invest.duckdb":
        raise ValueError("the dashboard cannot open the production database")
    return duckdb.connect(str(selected), read_only=True)


def query(name: str) -> tuple[list[str], list[tuple]]:
    if name not in QUERIES:
        raise ValueError("query is outside the dashboard allowlist")
    conn = connect()
    try:
        cursor = conn.execute(QUERIES[name])
        columns = [item[0] for item in cursor.description]
        return columns, cursor.fetchall()
    finally:
        conn.close()


def snapshot_identity() -> str:
    _, rows = query("metadata")
    if not rows:
        raise RuntimeError("projection metadata is missing")
    return f"{rows[0][2]}:{rows[0][3]}"
