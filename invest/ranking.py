"""Versioned deterministic survivor ranking and immutable persistence (T10.3).

Schema v19 is disposable-only until the Phase 10 production migration gate.
The engine ranks current deterministic screen survivors against the active NSE
EQ cohort. It never changes screen membership and uses no model or news input.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import UTC, date
from datetime import datetime as dt
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import duckdb

from invest import screens

SCHEMA_VERSION = 19
METHODOLOGY = "survivor-rank-2026.1"
PRODUCTION_DB = Path("data/invest.duckdb")


class RankingConflict(RuntimeError):
    """An immutable ranking identity was replayed with different content."""


@dataclass(frozen=True)
class InputSpec:
    field: str
    component: str
    unit: str
    direction: str
    input_weight: float
    source_kind: str


@dataclass(frozen=True)
class ComponentSpec:
    name: str
    weight: float


COMPONENTS = (
    ComponentSpec("valuation", 0.25),
    ComponentSpec("quality", 0.30),
    ComponentSpec("financial_strength", 0.20),
    ComponentSpec("momentum", 0.25),
    ComponentSpec("evidence_completeness", 0.0),
)

INPUTS = (
    InputSpec("pe_ratio", "valuation", "multiple", "lower", 0.60, "market"),
    InputSpec("pb_ratio", "valuation", "multiple", "lower", 0.40, "market"),
    InputSpec("roce", "quality", "fraction", "higher", 0.60, "fundamental"),
    InputSpec("operating_margin", "quality", "fraction", "higher", 0.40, "fundamental"),
    InputSpec("debt_to_equity", "financial_strength", "ratio", "lower", 0.40, "fundamental"),
    InputSpec("interest_coverage", "financial_strength", "ratio", "higher", 0.20, "fundamental"),
    InputSpec("current_ratio", "financial_strength", "ratio", "higher", 0.15, "fundamental"),
    InputSpec(
        "piotroski_score", "financial_strength", "integer 0-9", "higher", 0.25, "fundamental"
    ),
    InputSpec("price_to_52w_high", "momentum", "ratio", "higher", 0.40, "price"),
    InputSpec("price_above_50dma", "momentum", "boolean", "higher", 0.10, "price"),
    InputSpec("revenue_growth_yoy", "momentum", "fraction", "higher", 0.25, "fundamental"),
    InputSpec("profit_growth_yoy", "momentum", "fraction", "higher", 0.25, "fundamental"),
)

TRANSFORM = "empirical_midrank_percentile_v1"
COHORT = "active_eq"
SECTOR_MIN_VALID = 30
ROUND_DIGITS = 12
ROUND_QUANTUM = Decimal("0.000000000001")

RECONSTRUCTION_SQL = """
WITH rebuilt_component AS (
    SELECT run_id,symbol,component,
           cast(round(sum(cast(normalized_value AS DECIMAL(30,12)) *
                              cast(input_weight AS DECIMAL(30,12))),12) AS DOUBLE)
             AS normalized_value,
           min(component_weight) AS component_weight,
           cast(round(
             round(sum(cast(normalized_value AS DECIMAL(30,12)) *
                       cast(input_weight AS DECIMAL(30,12))),12) *
             min(cast(component_weight AS DECIMAL(30,12))),12
           ) AS DOUBLE) AS contribution,
           count(*) FILTER (WHERE missing_status='MISSING') AS missing_inputs,
           count(*) AS input_count
    FROM ranking_input
    GROUP BY run_id,symbol,component
), component_check AS (
    SELECT b.run_id,b.symbol,
           count(*) FILTER (WHERE
             c.normalized_value IS NOT DISTINCT FROM b.normalized_value
             AND c.component_weight=b.component_weight
             AND c.weighted_contribution IS NOT DISTINCT FROM b.contribution
             AND c.missing_status=CASE WHEN b.missing_inputs>0 THEN 'MISSING' ELSE 'AVAILABLE' END
           ) AS matching_components
    FROM rebuilt_component b
    LEFT JOIN ranking_component c USING(run_id,symbol,component)
    GROUP BY b.run_id,b.symbol
), stored_component_check AS (
    SELECT run_id,symbol,count(*) AS stored_components,
           count(*) FILTER (WHERE component IN (
             'valuation','quality','financial_strength','momentum'
           )) AS stored_score_components
    FROM ranking_component
    GROUP BY run_id,symbol
), rebuilt_symbol AS (
    SELECT run_id,symbol,
           CASE WHEN sum(missing_inputs)>0 THEN NULL ELSE cast(round(
             sum(cast(contribution AS DECIMAL(30,12))),12
           ) AS DOUBLE) END AS score,
           sum(input_count) AS input_count
    FROM rebuilt_component
    GROUP BY run_id,symbol
)
SELECT s.run_id,s.symbol,s.score AS stored_score,r.score AS rebuilt_score,
       coalesce(r.input_count,0)=12
       AND coalesce(c.matching_components,0)=4
       AND coalesce(sc.stored_components,0)=5
       AND coalesce(sc.stored_score_components,0)=4
       AND s.score IS NOT DISTINCT FROM r.score AS exact_match
FROM ranking_symbol s
LEFT JOIN rebuilt_symbol r USING(run_id,symbol)
LEFT JOIN component_check c USING(run_id,symbol)
LEFT JOIN stored_component_check sc USING(run_id,symbol)
ORDER BY s.run_id,s.symbol
"""

_DDL = (
    """
    CREATE TABLE IF NOT EXISTS ranking_methodology (
        methodology_version TEXT PRIMARY KEY,
        semantic_config_fingerprint TEXT NOT NULL,
        canonical_config_json TEXT NOT NULL,
        registered_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ranking_run (
        run_id TEXT PRIMARY KEY,
        source_as_of DATE NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL,
        methodology_version TEXT NOT NULL REFERENCES ranking_methodology(methodology_version),
        input_fingerprint TEXT NOT NULL,
        content_fingerprint TEXT NOT NULL,
        survivor_count INTEGER NOT NULL,
        available_count INTEGER NOT NULL,
        UNIQUE(methodology_version, source_as_of)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ranking_symbol (
        run_id TEXT NOT NULL REFERENCES ranking_run(run_id),
        symbol TEXT NOT NULL,
        score DOUBLE,
        research_rank INTEGER,
        evidence_completeness DOUBLE NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('AVAILABLE','MISSING')),
        missing_components_json TEXT NOT NULL,
        PRIMARY KEY(run_id, symbol)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ranking_component (
        run_id TEXT NOT NULL,
        symbol TEXT NOT NULL,
        component TEXT NOT NULL,
        normalized_value DOUBLE,
        component_weight DOUBLE NOT NULL,
        weighted_contribution DOUBLE,
        missing_status TEXT NOT NULL CHECK(missing_status IN ('AVAILABLE','MISSING')),
        PRIMARY KEY(run_id, symbol, component),
        FOREIGN KEY(run_id, symbol) REFERENCES ranking_symbol(run_id, symbol)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ranking_input (
        run_id TEXT NOT NULL,
        symbol TEXT NOT NULL,
        field TEXT NOT NULL,
        raw_value DOUBLE,
        unit TEXT NOT NULL,
        source TEXT NOT NULL,
        source_as_of DATE,
        normalization_cohort TEXT NOT NULL,
        cohort_size INTEGER NOT NULL,
        transform TEXT NOT NULL,
        direction TEXT NOT NULL CHECK(direction IN ('higher','lower')),
        normalized_value DOUBLE,
        component TEXT NOT NULL,
        input_weight DOUBLE NOT NULL,
        component_weight DOUBLE NOT NULL,
        weighted_contribution DOUBLE,
        missing_status TEXT NOT NULL CHECK(missing_status IN ('AVAILABLE','MISSING')),
        PRIMARY KEY(run_id, symbol, field),
        FOREIGN KEY(run_id, symbol) REFERENCES ranking_symbol(run_id, symbol)
    )
    """,
)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def semantic_config() -> dict:
    return {
        "methodology_version": METHODOLOGY,
        "cohort": COHORT,
        "transform": TRANSFORM,
        "missing_policy": "required_input_makes_composite_unavailable",
        "round_digits": ROUND_DIGITS,
        "rank_order": "score_desc,evidence_completeness_desc,symbol_asc",
        "components": [asdict(item) for item in COMPONENTS],
        "inputs": [asdict(item) for item in INPUTS],
        "excluded": [
            "dividend_yield",
            "free_cash_flow_3y",
            "peg",
            "rsi",
            "price_to_all_time_high",
            "roe",
            "eps_growth_yoy",
        ],
    }


def install_schema(conn: duckdb.DuckDBPyConnection) -> None:
    for statement in _DDL:
        conn.execute(statement)
    conn.execute(
        "INSERT INTO schema_migrations VALUES (?,?) ON CONFLICT DO NOTHING",
        [SCHEMA_VERSION, dt.now(UTC)],
    )


def register_methodology(conn: duckdb.DuckDBPyConnection, *, registered_at: dt) -> str:
    if registered_at.tzinfo is None:
        raise ValueError("registered_at must be timezone-aware")
    config_json = _canonical(semantic_config())
    config_fp = _fingerprint(semantic_config())
    existing = conn.execute(
        "SELECT semantic_config_fingerprint FROM ranking_methodology WHERE methodology_version=?",
        [METHODOLOGY],
    ).fetchone()
    if existing and existing[0] != config_fp:
        raise RankingConflict("methodology version already has different semantic config")
    conn.execute(
        "INSERT INTO ranking_methodology VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
        [METHODOLOGY, config_fp, config_json, registered_at],
    )
    return config_fp


def select_normalization_cohort(
    *, sector: str | None, sector_valid_count: int, broad_cohort: str = COHORT
) -> tuple[str, bool]:
    """Return the declared cohort and whether sector fallback was required."""
    if sector and sector != "UNKNOWN" and sector_valid_count >= SECTOR_MIN_VALID:
        return f"sector:{sector}", False
    return broad_cohort, bool(sector and sector != "UNKNOWN")


def midrank_percentiles(values: dict[str, float], *, higher: bool) -> dict[str, float]:
    """Return [0,1] empirical percentiles with equal values receiving equal midranks."""
    if not values:
        return {}
    if len(values) == 1:
        return {next(iter(values)): 0.5}
    ordered = sorted(values.items(), key=lambda item: item[1], reverse=higher)
    result: dict[str, float] = {}
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][1] == ordered[index][1]:
            end += 1
        average_position = (index + end - 1) / 2
        percentile = 1.0 - average_position / (len(ordered) - 1)
        for symbol, _value in ordered[index:end]:
            result[symbol] = percentile
        index = end
    return result


def _provenance(conn, universe: dict[str, dict]) -> dict[tuple[str, str], tuple[str, date | None]]:
    """Resolve the actual source selected by build_universe for every input."""
    result: dict[tuple[str, str], tuple[str, date | None]] = {}
    source_fields = {
        screens.COMPUTED_SOURCE: {
            spec.field for spec in INPUTS if spec.source_kind == "fundamental"
        },
        screens.MARKET_SOURCE: {spec.field for spec in INPUTS if spec.source_kind == "market"}
        | {"current_ratio", "interest_coverage"},
    }
    available: dict[tuple[str, str, str], tuple[float, date]] = {}
    for source, fields in source_fields.items():
        ordered_fields = sorted(fields)
        rows = conn.execute(
            f"""
            SELECT symbol,as_of,{",".join(ordered_fields)} FROM stock_fundamentals f
            WHERE source=? AND as_of=(SELECT max(as_of) FROM stock_fundamentals g
              WHERE g.symbol=f.symbol AND g.source=f.source)
            """,
            [source],
        ).fetchall()
        for row in rows:
            symbol, as_of, *values = row
            for field, value in zip(ordered_fields, values, strict=True):
                if value is not None:
                    available[(symbol, source, field)] = (value, as_of)
    for symbol in universe:
        for spec in INPUTS:
            if spec.source_kind == "price":
                continue
            candidates = (
                (screens.COMPUTED_SOURCE, screens.MARKET_SOURCE)
                if spec.field in {"current_ratio", "interest_coverage"}
                else (
                    screens.COMPUTED_SOURCE
                    if spec.source_kind == "fundamental"
                    else screens.MARKET_SOURCE,
                )
            )
            for source in candidates:
                found = available.get((symbol, source, spec.field))
                if found and universe[symbol].get(spec.field) == found[0]:
                    result[(symbol, spec.field)] = (source, found[1])
                    break
            result.setdefault((symbol, spec.field), ("unavailable", None))
    price_rows = conn.execute(
        """
        SELECT symbol,trade_date,source FROM stock_price
        QUALIFY row_number() OVER (
          PARTITION BY symbol ORDER BY trade_date DESC,source
        )=1
        """
    ).fetchall()
    price_provenance = {symbol: (source, as_of) for symbol, as_of, source in price_rows}
    for symbol in universe:
        for spec in INPUTS:
            if spec.source_kind == "price":
                result[(symbol, spec.field)] = price_provenance.get(symbol, ("unavailable", None))
    return result


def _rounded(value) -> float:
    return float(Decimal(str(value)).quantize(ROUND_QUANTUM, rounding=ROUND_HALF_UP))


def _weighted_sum(items: list[tuple[float, float]]) -> float:
    total = sum((Decimal(str(value)) * Decimal(str(weight)) for value, weight in items), Decimal(0))
    return _rounded(total)


def _finite(value) -> float | None:
    if value is None:
        return None
    converted = float(value)
    return converted if math.isfinite(converted) else None


def calculate(conn: duckdb.DuckDBPyConnection) -> dict:
    universe = screens.build_universe(conn)
    config = screens.load_config()
    survivors_by_screen = {
        screen_id: {
            row["symbol"]
            for row in screens.evaluate_screen(universe, definition["conditions"])["survivors"]
        }
        for screen_id, definition in config["screens"].items()
    }
    survivors = sorted(set().union(*survivors_by_screen.values())) if survivors_by_screen else []
    provenance = _provenance(conn, universe)
    component_weights = {item.name: item.weight for item in COMPONENTS}
    percentiles: dict[str, dict[str, float]] = {}
    cohort_sizes: dict[str, int] = {}
    for spec in INPUTS:
        values = {
            symbol: value
            for symbol, row in universe.items()
            if (value := _finite(row.get(spec.field))) is not None
        }
        percentiles[spec.field] = midrank_percentiles(values, higher=spec.direction == "higher")
        cohort_sizes[spec.field] = len(values)

    rows = []
    for symbol in survivors:
        inputs = []
        by_component: dict[str, list[dict]] = {}
        for spec in INPUTS:
            raw = _finite(universe[symbol].get(spec.field))
            normalized = percentiles[spec.field].get(symbol)
            normalized = _rounded(normalized) if normalized is not None else None
            source, source_as_of = provenance[(symbol, spec.field)]
            missing = raw is None or normalized is None
            contribution = (
                _rounded(
                    Decimal(str(normalized))
                    * Decimal(str(spec.input_weight))
                    * Decimal(str(component_weights[spec.component]))
                )
                if not missing
                else None
            )
            item = {
                "field": spec.field,
                "raw_value": raw,
                "unit": spec.unit,
                "source": source,
                "source_as_of": source_as_of,
                "normalization_cohort": COHORT,
                "cohort_size": cohort_sizes[spec.field],
                "transform": TRANSFORM,
                "direction": spec.direction,
                "normalized_value": normalized,
                "component": spec.component,
                "input_weight": spec.input_weight,
                "component_weight": component_weights[spec.component],
                "weighted_contribution": contribution,
                "missing_status": "MISSING" if missing else "AVAILABLE",
            }
            inputs.append(item)
            by_component.setdefault(spec.component, []).append(item)

        components = []
        missing_components = []
        for component in (item.name for item in COMPONENTS if item.weight > 0):
            items = by_component[component]
            missing = any(item["missing_status"] == "MISSING" for item in items)
            if missing:
                missing_components.append(component)
            normalized = (
                _weighted_sum([(item["normalized_value"], item["input_weight"]) for item in items])
                if not missing
                else None
            )
            components.append(
                {
                    "component": component,
                    "normalized_value": normalized,
                    "component_weight": component_weights[component],
                    "weighted_contribution": (
                        _rounded(
                            Decimal(str(normalized)) * Decimal(str(component_weights[component]))
                        )
                        if normalized is not None
                        else None
                    ),
                    "missing_status": "MISSING" if missing else "AVAILABLE",
                }
            )
        completeness = sum(i["missing_status"] == "AVAILABLE" for i in inputs) / len(inputs)
        components.append(
            {
                "component": "evidence_completeness",
                "normalized_value": completeness,
                "component_weight": 0.0,
                "weighted_contribution": 0.0,
                "missing_status": "AVAILABLE",
            }
        )
        score = (
            None
            if missing_components
            else _rounded(
                sum(
                    (
                        Decimal(str(item["weighted_contribution"]))
                        for item in components
                        if item["component"] != "evidence_completeness"
                    ),
                    Decimal(0),
                )
            )
        )
        rows.append(
            {
                "symbol": symbol,
                "score": score,
                "evidence_completeness": completeness,
                "status": "MISSING" if missing_components else "AVAILABLE",
                "missing_components": missing_components,
                "inputs": inputs,
                "components": components,
            }
        )

    available = sorted(
        (row for row in rows if row["score"] is not None),
        key=lambda row: (-row["score"], -row["evidence_completeness"], row["symbol"]),
    )
    for rank, row in enumerate(available, 1):
        row["research_rank"] = rank
    for row in rows:
        row.setdefault("research_rank", None)
    source_dates = [
        item["source_as_of"]
        for row in rows
        for item in row["inputs"]
        if item["source_as_of"] is not None
    ]
    return {
        "methodology_version": METHODOLOGY,
        "source_as_of": max(source_dates) if source_dates else None,
        "survivors": rows,
    }


def _close(left, right) -> bool:
    return left is not None and right is not None and math.isclose(left, right, abs_tol=1e-12)


def _validate_result(result: dict) -> None:
    expected_fields = {spec.field: spec for spec in INPUTS}
    component_specs = {spec.name: spec for spec in COMPONENTS}
    symbols = [row.get("symbol") for row in result.get("survivors", [])]
    if len(symbols) != len(set(symbols)) or any(not symbol for symbol in symbols):
        raise ValueError("ranking result has duplicate or empty symbols")
    available_rows = []
    for row in result.get("survivors", []):
        inputs = row.get("inputs", [])
        if {item.get("field") for item in inputs} != set(expected_fields) or len(inputs) != len(
            expected_fields
        ):
            raise ValueError("ranking result input set does not match methodology")
        for item in inputs:
            spec = expected_fields[item["field"]]
            missing = item.get("missing_status") == "MISSING"
            normalized = item.get("normalized_value")
            if (
                item.get("unit") != spec.unit
                or item.get("direction") != spec.direction
                or item.get("component") != spec.component
                or item.get("normalization_cohort") != COHORT
                or item.get("transform") != TRANSFORM
                or item.get("input_weight") != spec.input_weight
                or item.get("component_weight") != component_specs[spec.component].weight
                or not isinstance(item.get("cohort_size"), int)
                or item["cohort_size"] < 0
                or missing != (item.get("raw_value") is None or normalized is None)
                or (normalized is not None and not 0.0 <= normalized <= 1.0)
                or (not missing and (not item.get("source") or item.get("source_as_of") is None))
            ):
                raise ValueError("ranking input metadata or missing status is invalid")
        rebuilt_components = {}
        missing_components = []
        for name, component_spec in component_specs.items():
            if name == "evidence_completeness":
                continue
            items = [item for item in inputs if item.get("component") == name]
            specs = [spec for spec in INPUTS if spec.component == name]
            if {item["field"] for item in items} != {spec.field for spec in specs}:
                raise ValueError("ranking result component input set is invalid")
            missing = any(item.get("missing_status") == "MISSING" for item in items)
            if missing:
                missing_components.append(name)
                rebuilt_components[name] = None
            else:
                value = 0.0
                for item in items:
                    spec = expected_fields[item["field"]]
                    normalized = item.get("normalized_value")
                    if (
                        item.get("unit") != spec.unit
                        or item.get("direction") != spec.direction
                        or item.get("component") != spec.component
                        or item.get("normalization_cohort") != COHORT
                        or item.get("transform") != TRANSFORM
                        or not isinstance(item.get("cohort_size"), int)
                        or item["cohort_size"] < 1
                        or item.get("raw_value") is None
                        or not isinstance(normalized, (int, float))
                        or not 0.0 <= normalized <= 1.0
                        or not item.get("source")
                        or item.get("source") == "unavailable"
                        or item.get("source_as_of") is None
                    ):
                        raise ValueError("ranking input metadata or normalization is invalid")
                    expected = normalized * spec.input_weight
                    if (
                        item.get("input_weight") != spec.input_weight
                        or item.get("component_weight") != component_spec.weight
                        or not _close(
                            item.get("weighted_contribution"),
                            expected * component_spec.weight,
                        )
                    ):
                        raise ValueError("ranking input weight or contribution is invalid")
                    value += expected
                rebuilt_components[name] = value
        components = {item.get("component"): item for item in row.get("components", [])}
        if set(components) != set(component_specs) or len(row.get("components", [])) != len(
            component_specs
        ):
            raise ValueError("ranking component set does not match methodology")
        for name, spec in component_specs.items():
            item = components[name]
            expected_value = (
                sum(i.get("missing_status") == "AVAILABLE" for i in inputs) / len(inputs)
                if name == "evidence_completeness"
                else rebuilt_components[name]
            )
            expected_contribution = (
                0.0
                if name == "evidence_completeness"
                else None
                if expected_value is None
                else expected_value * spec.weight
            )
            if (
                item.get("component_weight") != spec.weight
                or (expected_value is None) != (item.get("normalized_value") is None)
                or (
                    expected_value is not None
                    and not _close(item.get("normalized_value"), expected_value)
                )
                or (expected_contribution is None) != (item.get("weighted_contribution") is None)
                or (
                    expected_contribution is not None
                    and not _close(item.get("weighted_contribution"), expected_contribution)
                )
            ):
                raise ValueError("ranking component arithmetic is invalid")
        completeness = sum(i.get("missing_status") == "AVAILABLE" for i in inputs) / len(inputs)
        expected_score = (
            None
            if missing_components
            else sum(components[name]["weighted_contribution"] for name in rebuilt_components)
        )
        expected_status = "MISSING" if missing_components else "AVAILABLE"
        if (
            not _close(row.get("evidence_completeness"), completeness)
            or row.get("missing_components") != missing_components
            or row.get("status") != expected_status
            or (expected_score is None) != (row.get("score") is None)
            or (expected_score is not None and not _close(row.get("score"), expected_score))
        ):
            raise ValueError("ranking symbol status or score is invalid")
        if expected_score is not None:
            available_rows.append(row)
    ordered = sorted(
        available_rows,
        key=lambda row: (-row["score"], -row["evidence_completeness"], row["symbol"]),
    )
    expected_ranks = {row["symbol"]: rank for rank, row in enumerate(ordered, 1)}
    if any(
        row.get("research_rank") != expected_ranks.get(row["symbol"]) for row in result["survivors"]
    ):
        raise ValueError("ranking order is invalid")


def _stored_content(conn, run_id: str) -> str:
    tables = ("ranking_symbol", "ranking_component", "ranking_input")
    content = {
        "ranking_run": conn.execute(
            """
            SELECT run_id,source_as_of,recorded_at,methodology_version,input_fingerprint,
                   survivor_count,available_count
            FROM ranking_run WHERE run_id=?
            """,
            [run_id],
        ).fetchone(),
        **{
            table: conn.execute(
                f"SELECT * FROM {table} WHERE run_id=? ORDER BY ALL", [run_id]
            ).fetchall()
            for table in tables
        },
    }
    return _fingerprint(content)


def verify_run(conn: duckdb.DuckDBPyConnection, run_id: str) -> bool:
    row = conn.execute(
        """
        SELECT r.content_fingerprint,m.semantic_config_fingerprint,m.canonical_config_json
        FROM ranking_run r JOIN ranking_methodology m USING(methodology_version)
        WHERE r.run_id=?
        """,
        [run_id],
    ).fetchone()
    if not row:
        return False
    content_fp, config_fp, config_json = row
    try:
        methodology_valid = config_json == _canonical(
            semantic_config()
        ) and config_fp == _fingerprint(json.loads(config_json))
    except (TypeError, json.JSONDecodeError):
        methodology_valid = False
    return methodology_valid and content_fp == _stored_content(conn, run_id)


def persist(conn: duckdb.DuckDBPyConnection, result: dict, *, recorded_at: dt) -> dict:
    if recorded_at.tzinfo is None:
        raise ValueError("recorded_at must be timezone-aware")
    if result.get("methodology_version") != METHODOLOGY or result.get("source_as_of") is None:
        raise ValueError("ranking result has invalid methodology or source date")
    _validate_result(result)
    payload = {
        "methodology_version": METHODOLOGY,
        "source_as_of": result["source_as_of"],
        "survivors": result["survivors"],
    }
    input_fp = _fingerprint(payload)
    run_id = _fingerprint([METHODOLOGY, result["source_as_of"]])[:24]
    existing = conn.execute(
        "SELECT input_fingerprint FROM ranking_run WHERE methodology_version=? AND source_as_of=?",
        [METHODOLOGY, result["source_as_of"]],
    ).fetchone()
    if existing:
        if existing[0] != input_fp:
            raise RankingConflict("ranking run identity already has different content")
        if not verify_run(conn, run_id):
            raise RankingConflict("stored ranking run failed integrity verification")
        return {"status": "REPLAY", "run_id": run_id}
    try:
        conn.execute("BEGIN")
        register_methodology(conn, registered_at=recorded_at)
        conn.execute(
            "INSERT INTO ranking_run VALUES (?,?,?,?,?,?,?,?)",
            [
                run_id,
                result["source_as_of"],
                recorded_at,
                METHODOLOGY,
                input_fp,
                "PENDING",
                len(result["survivors"]),
                sum(r["status"] == "AVAILABLE" for r in result["survivors"]),
            ],
        )
        for row in result["survivors"]:
            conn.execute(
                "INSERT INTO ranking_symbol VALUES (?,?,?,?,?,?,?)",
                [
                    run_id,
                    row["symbol"],
                    row["score"],
                    row["research_rank"],
                    row["evidence_completeness"],
                    row["status"],
                    _canonical(row["missing_components"]),
                ],
            )
            for component in row["components"]:
                conn.execute(
                    "INSERT INTO ranking_component VALUES (?,?,?,?,?,?,?)",
                    [
                        run_id,
                        row["symbol"],
                        component["component"],
                        component["normalized_value"],
                        component["component_weight"],
                        component["weighted_contribution"],
                        component["missing_status"],
                    ],
                )
            for item in row["inputs"]:
                conn.execute(
                    "INSERT INTO ranking_input VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        run_id,
                        row["symbol"],
                        item["field"],
                        item["raw_value"],
                        item["unit"],
                        item["source"],
                        item["source_as_of"],
                        item["normalization_cohort"],
                        item["cohort_size"],
                        item["transform"],
                        item["direction"],
                        item["normalized_value"],
                        item["component"],
                        item["input_weight"],
                        item["component_weight"],
                        item["weighted_contribution"],
                        item["missing_status"],
                    ],
                )
        content_fp = _stored_content(conn, run_id)
        conn.execute(
            "UPDATE ranking_run SET content_fingerprint=? WHERE run_id=?",
            [content_fp, run_id],
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"status": "ACCEPTED", "run_id": run_id}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="invest-ranking")
    parser.add_argument("--db", type=Path, default=PRODUCTION_DB)
    args = parser.parse_args(argv)
    conn = duckdb.connect(str(args.db))
    try:
        version = conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
        if version < SCHEMA_VERSION:
            raise RuntimeError(f"ranking requires schema v{SCHEMA_VERSION}, found v{version}")
        result = calculate(conn)
        stored = persist(conn, result, recorded_at=dt.now(UTC))
        print(
            json.dumps(
                {
                    **stored,
                    "methodology_version": METHODOLOGY,
                    "source_as_of": result["source_as_of"],
                    "survivor_count": len(result["survivors"]),
                    "available_count": sum(
                        row["status"] == "AVAILABLE" for row in result["survivors"]
                    ),
                },
                default=str,
                sort_keys=True,
            )
        )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
