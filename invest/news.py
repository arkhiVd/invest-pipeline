"""T6 deterministic RSS ingest and bounded headline-only classification."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import UTC, timedelta
from datetime import datetime as dt
from email.utils import parsedate_to_datetime
from pathlib import Path
from uuid import uuid4

from invest import db, portfolio, research, watchlist

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "config/news.json"
DEFAULT_DB = PROJECT_ROOT / "data/invest.duckdb"
PREFILTER_VERSION = "news-prefilter-2026.1"
METHODOLOGY = "headline-sentiment-2026.1"
GOOGLE_BASE = "https://news.google.com/rss/search"
ET_MARKETS = "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"
ALLOWED_FEED_HOSTS = {"news.google.com", "economictimes.indiatimes.com"}
SENTIMENTS = {"positive", "negative", "neutral"}
EVENT_TYPES = {
    "earnings",
    "contract",
    "corporate_action",
    "governance",
    "regulatory",
    "management",
    "analyst",
    "other",
}
_SPACE = re.compile(r"\s+")
_NONWORD = re.compile(r"[^A-Z0-9]+")
_COMPANY_SUFFIX = re.compile(
    r"\b(LIMITED|LTD|PRIVATE|PVT|INDIA|INDIAN|CORPORATION|COMPANY|CO)\b", re.I
)


class NewsError(RuntimeError):
    """Bounded source or classification failure."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def load_config(path: str | Path = DEFAULT_CONFIG) -> dict:
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    ints = (
        "max_calls_per_run",
        "max_attempts_per_article",
        "max_age_days",
        "max_future_minutes",
        "max_feed_bytes",
        "request_timeout_seconds",
        "google_max_items_per_entity",
    )
    for key in ints:
        if isinstance(cfg.get(key), bool) or not isinstance(cfg.get(key), int) or cfg[key] < 1:
            raise ValueError(f"news config {key} must be a positive integer")
    if cfg["max_calls_per_run"] > 20 or cfg["max_attempts_per_article"] > 5:
        raise ValueError("news model limits exceed safety bounds")
    for key in ("event_terms", "excluded_terms"):
        values = cfg.get(key)
        if (
            not isinstance(values, list)
            or not values
            or any(not isinstance(value, str) or not value.strip() for value in values)
        ):
            raise ValueError(f"news config {key} must be a non-empty string list")
    aliases = cfg.get("entity_aliases")
    if not isinstance(aliases, dict) or any(
        not isinstance(symbol, str)
        or not isinstance(values, list)
        or any(not isinstance(value, str) or len(_match_text(value)) < 3 for value in values)
        for symbol, values in aliases.items()
    ):
        raise ValueError("news config entity_aliases must map symbols to string lists")
    if cfg.get("backend") != "codex_exec":
        raise ValueError("news backend must be codex_exec")
    binary = cfg.get("codex_bin")
    if not isinstance(binary, str) or not Path(binary).is_absolute():
        raise ValueError("news codex_bin must be absolute")
    if not Path(binary).is_file() or not os.access(binary, os.X_OK):
        raise ValueError("news codex_bin must be executable")
    if not isinstance(cfg.get("model"), str) or not cfg["model"].strip():
        raise ValueError("news model must be non-empty")
    return cfg


def _plain(value: str) -> str:
    return _SPACE.sub(" ", value).strip()


def _match_text(value: str) -> str:
    return _plain(_NONWORD.sub(" ", value.upper()))


def _company_alias(name: str) -> str:
    return _plain(_COMPANY_SUFFIX.sub(" ", _match_text(name)))


def target_entities(conn, config: dict) -> list[dict]:
    """Return current deterministic research survivors plus verified owned equities."""
    candidate_symbols = {item["symbol"] for item in research.candidates(conn)}
    report = portfolio.reconcile(conn)
    owned = set(report["owned_research"]) | set(report["owned_not_research"])
    symbols = sorted(candidate_symbols | owned)
    if not symbols:
        return []
    rows = conn.execute(
        "SELECT symbol, company_name FROM stock_universe WHERE is_active AND symbol IN ("
        + ",".join("?" for _ in symbols)
        + ") ORDER BY symbol",
        symbols,
    ).fetchall()
    found = {row[0] for row in rows}
    missing = sorted(set(symbols) - found)
    if missing:
        raise ValueError(f"news targets missing from active stock universe: {missing}")
    output = []
    configured = config["entity_aliases"]
    for symbol, company_name in rows:
        symbol_alias = [_match_text(symbol)] if len(_match_text(symbol)) >= 5 else []
        aliases = tuple(
            value
            for value in dict.fromkeys(
                [
                    *(_match_text(alias) for alias in configured.get(symbol, [])),
                    *symbol_alias,
                    _company_alias(company_name or ""),
                ]
            )
            if len(value) >= 3
        )
        if not aliases:
            raise ValueError(f"news target {symbol} has no safe entity alias")
        output.append(
            {"symbol": symbol, "company_name": company_name or symbol, "aliases": aliases}
        )
    return output


def _google_url(entity: dict) -> str:
    query = f'"{entity["company_name"]}" OR {entity["symbol"]} when:7d'
    return (
        GOOGLE_BASE
        + "?"
        + urllib.parse.urlencode({"q": query, "hl": "en-IN", "gl": "IN", "ceid": "IN:en"})
    )


def fetch_xml(url: str, config: dict) -> bytes:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_FEED_HOSTS:
        raise NewsError("RSS URL is outside the fixed HTTPS allowlist")
    req = urllib.request.Request(url, headers={"User-Agent": "invest-research/1.0"})
    try:
        opener = urllib.request.build_opener(_NoRedirect)
        with opener.open(req, timeout=config["request_timeout_seconds"]) as response:
            final = urllib.parse.urlparse(response.geturl())
            if final.scheme != "https" or final.hostname not in ALLOWED_FEED_HOSTS:
                raise NewsError("RSS response URL is outside the fixed HTTPS allowlist")
            length = response.headers.get("Content-Length")
            if length and int(length) > config["max_feed_bytes"]:
                raise NewsError("RSS response exceeds byte limit")
            body = response.read(config["max_feed_bytes"] + 1)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise NewsError(f"RSS request failed: {type(exc).__name__}") from exc
    if len(body) > config["max_feed_bytes"]:
        raise NewsError("RSS response exceeds byte limit")
    return body


def parse_feed(
    raw: bytes,
    *,
    source_feed: str,
    now: dt,
    config: dict,
    limit: int | None = None,
) -> list[dict]:
    lowered = raw.lower()
    if b"\x00" in raw or b"<!doctype" in lowered or b"<!entity" in lowered:
        raise NewsError("RSS response contains unsupported encoding or XML declarations")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise NewsError("RSS response is not valid XML") from exc
    items = root.findall(".//item")
    if not items:
        raise NewsError("RSS response has no items")
    output = []
    for item in items[:limit]:
        title = _plain(item.findtext("title") or "")
        url = _plain(item.findtext("link") or "")
        publisher = _plain(item.findtext("source") or source_feed)
        published = _plain(item.findtext("pubDate") or "")
        parsed = urllib.parse.urlparse(url)
        if not title or not publisher or parsed.scheme != "https" or not parsed.hostname:
            continue
        try:
            published_at = parsedate_to_datetime(published)
        except (TypeError, ValueError, OverflowError):
            continue
        if published_at.tzinfo is None:
            continue
        published_at = published_at.astimezone(UTC)
        age = now.astimezone(UTC) - published_at
        if age > timedelta(days=config["max_age_days"]):
            continue
        if age < -timedelta(minutes=config["max_future_minutes"]):
            continue
        article_id = hashlib.sha256(f"{url}\n{title}".encode()).hexdigest()
        output.append(
            {
                "article_id": article_id,
                "title": title,
                "url": url,
                "publisher": publisher,
                "source_feed": source_feed,
                "published_at": published_at,
            }
        )
    return output


def match_entity(title: str, entity: dict) -> str | None:
    text = f" {_match_text(title)} "
    for alias in entity["aliases"]:
        if f" {alias} " in text:
            return "symbol" if alias == _match_text(entity["symbol"]) else "company_name"
    return None


def prefilter(title: str, config: dict) -> bool:
    text = _plain(title).casefold()
    if any(term.casefold() in text for term in config["excluded_terms"]):
        return False
    return any(term.casefold() in text for term in config["event_terms"])


def collect(entities: list[dict], config: dict, *, now: dt | None = None) -> list[dict]:
    now = now or dt.now(UTC)
    matched: dict[tuple[str, str], dict] = {}
    seen_articles: set[tuple[str, str]] = set()
    for entity in entities:
        articles = parse_feed(
            fetch_xml(_google_url(entity), config),
            source_feed="google-news-company",
            now=now,
            config=config,
            limit=config["google_max_items_per_entity"],
        )
        for article in articles:
            reason = match_entity(article["title"], entity)
            article_key = (article["article_id"], entity["symbol"])
            if reason and article_key not in seen_articles:
                seen_articles.add(article_key)
                matched[article_key] = {
                    **article,
                    "symbol": entity["symbol"],
                    "match_reason": reason,
                    "prefilter_pass": prefilter(article["title"], config),
                }
    et_articles = parse_feed(
        fetch_xml(ET_MARKETS, config), source_feed="et-markets", now=now, config=config
    )
    for article in et_articles:
        for entity in entities:
            reason = match_entity(article["title"], entity)
            article_key = (article["article_id"], entity["symbol"])
            if reason and article_key not in seen_articles:
                seen_articles.add(article_key)
                matched[article_key] = {
                    **article,
                    "symbol": entity["symbol"],
                    "match_reason": reason,
                    "prefilter_pass": prefilter(article["title"], config),
                }
    return sorted(
        matched.values(), key=lambda x: (x["published_at"], x["article_id"]), reverse=True
    )


def store_items(conn, items: list[dict], *, fetched_at: dt) -> int:
    inserted = 0
    conn.execute("BEGIN TRANSACTION")
    try:
        for item in items:
            exists = conn.execute(
                "SELECT 1 FROM news_article WHERE article_id=?", [item["article_id"]]
            ).fetchone()
            conn.execute(
                "INSERT INTO news_article VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                [
                    item["article_id"],
                    item["title"],
                    item["url"],
                    item["publisher"],
                    item["source_feed"],
                    item["published_at"],
                    fetched_at,
                ],
            )
            conn.execute(
                "INSERT INTO news_article_entity VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                [item["article_id"], item["symbol"], item["match_reason"], PREFILTER_VERSION],
            )
            inserted += int(exists is None)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return inserted


def _response_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "sentiment": {"type": "string", "enum": sorted(SENTIMENTS)},
            "event_type": {"type": "string", "enum": sorted(EVENT_TYPES)},
            "materiality": {"type": "integer", "minimum": 0, "maximum": 3},
            "rationale": {"type": "string", "minLength": 1, "maxLength": 300},
        },
        "required": ["sentiment", "event_type", "materiality", "rationale"],
        "additionalProperties": False,
    }


def build_prompt(item: dict) -> str:
    payload = {
        "symbol": item["symbol"],
        "title": item["title"],
        "publisher": item["publisher"],
        "published_at": item["published_at"].isoformat(),
        "url": item["url"],
    }
    return (
        "Classify only the supplied headline. Do not browse, infer article-body facts, give a "
        "recommendation, or calculate a stock score. Sentiment is the likely company-specific "
        "direction conveyed by the headline: positive, negative, or neutral. Materiality is 0 "
        "for no clear company impact, 1 low, 2 medium, 3 high. Explain uncertainty in at most "
        "45 words. Return JSON only. Input: "
        + json.dumps(payload, sort_keys=True, separators=(",", ":"))
    )


def codex_classify(prompt: str, config: dict, *, timeout: int = 300) -> str:
    with tempfile.TemporaryDirectory(prefix="invest-news-") as temporary:
        root = Path(temporary)
        schema_path = root / "schema.json"
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
                command, input=prompt, capture_output=True, text=True, timeout=timeout, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise NewsError(f"codex exec failed: {type(exc).__name__}") from exc
        if result.returncode != 0 or not output_path.exists():
            raise NewsError(f"codex exec failed with exit {result.returncode}")
        return output_path.read_text(encoding="utf-8").strip()


def parse_classification(reply: str, item: dict) -> dict:
    try:
        value = json.loads(reply)
    except json.JSONDecodeError as exc:
        raise ValueError("classification is not JSON") from exc
    expected = {"sentiment", "event_type", "materiality", "rationale"}
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("classification has wrong keys")
    if (
        not isinstance(value["sentiment"], str)
        or not isinstance(value["event_type"], str)
        or value["sentiment"] not in SENTIMENTS
        or value["event_type"] not in EVENT_TYPES
    ):
        raise ValueError("classification enum is invalid")
    materiality = value["materiality"]
    if (
        isinstance(materiality, bool)
        or not isinstance(materiality, int)
        or not 0 <= materiality <= 3
    ):
        raise ValueError("classification materiality is invalid")
    rationale = value["rationale"]
    if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > 300:
        raise ValueError("classification rationale is invalid")
    return {
        **value,
        "rationale": rationale.strip(),
        "cited_url": item["url"],
        "evidence_scope": "headline-only",
    }


def _record_attempt(conn, item: dict, *, now: dt, error: str | None) -> None:
    conn.execute(
        "INSERT INTO news_classification_attempt VALUES (?, ?, ?, 1, ?, ?) "
        "ON CONFLICT (article_id, symbol, methodology_version) DO UPDATE SET "
        "attempts=news_classification_attempt.attempts+1, last_error=excluded.last_error, "
        "last_attempt_at=excluded.last_attempt_at",
        [item["article_id"], item["symbol"], METHODOLOGY, error, now],
    )


def classify(
    conn, items: list[dict], config: dict, *, ask=codex_classify, now: dt | None = None
) -> dict:
    now = now or dt.now(UTC)
    attempted = stored = errors = 0
    for item in items:
        if not item["prefilter_pass"] or attempted >= config["max_calls_per_run"]:
            continue
        prior = conn.execute(
            "SELECT 1 FROM news_classification "
            "WHERE article_id=? AND symbol=? AND methodology_version=?",
            [item["article_id"], item["symbol"], METHODOLOGY],
        ).fetchone()
        attempts = conn.execute(
            "SELECT attempts FROM news_classification_attempt "
            "WHERE article_id=? AND symbol=? AND methodology_version=?",
            [item["article_id"], item["symbol"], METHODOLOGY],
        ).fetchone()
        if prior or (attempts and attempts[0] >= config["max_attempts_per_article"]):
            continue
        attempted += 1
        try:
            parsed = parse_classification(ask(build_prompt(item), config), item)
            conn.execute("BEGIN TRANSACTION")
            try:
                _record_attempt(conn, item, now=now, error=None)
                conn.execute(
                    "INSERT INTO news_classification VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        item["article_id"],
                        item["symbol"],
                        METHODOLOGY,
                        parsed["sentiment"],
                        parsed["event_type"],
                        parsed["materiality"],
                        parsed["rationale"],
                        parsed["cited_url"],
                        parsed["evidence_scope"],
                        config["model"],
                        now,
                    ],
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            stored += 1
        except (NewsError, ValueError) as exc:
            _record_attempt(conn, item, now=now, error=type(exc).__name__)
            errors += 1
    return {"attempted": attempted, "stored": stored, "errors": errors}


def render(conn) -> str:
    rows = conn.execute(
        "SELECT c.symbol, a.published_at, c.sentiment, c.event_type, c.materiality, "
        "a.title, a.publisher, c.rationale, c.cited_url FROM news_classification c "
        "JOIN news_article a USING (article_id) WHERE c.methodology_version=? "
        "ORDER BY c.symbol, a.published_at DESC, a.article_id",
        [METHODOLOGY],
    ).fetchall()
    lines = [
        "NEWS HEADLINE CLASSIFICATIONS",
        "Headline-only research context. No trade instruction is produced.",
        f"classified={len(rows)} methodology={METHODOLOGY}",
    ]
    current = None
    for symbol, published, sentiment, event, materiality, title, publisher, rationale, url in rows:
        if symbol != current:
            lines.extend(["", symbol])
            current = symbol
        lines.extend(
            [
                f"  {published.isoformat()} {sentiment} event={event} materiality={materiality}",
                f"  {title} [{publisher}]",
                f"  {rationale}",
                f"  {url}",
            ]
        )
    if not rows:
        lines.extend(["", "No classified headlines."])
    return "\n".join(lines)


def run(conn, config: dict, *, enable_llm: bool, now: dt | None = None) -> tuple[dict, str]:
    now = now or dt.now(UTC)
    entities = target_entities(conn, config)
    items = collect(entities, config, now=now)
    inserted = store_items(conn, items, fetched_at=now)
    result = (
        classify(conn, items, config, now=now)
        if enable_llm
        else {"attempted": 0, "stored": 0, "errors": 0}
    )
    run_id = uuid4().hex
    conn.execute(
        "INSERT INTO news_run VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            run_id,
            now,
            len(entities),
            len(items),
            inserted,
            sum(item["prefilter_pass"] for item in items),
            config["max_calls_per_run"],
            result["attempted"],
            result["stored"],
            json.dumps({"classification_errors": result["errors"]}, sort_keys=True),
        ],
    )
    return {
        "run_id": run_id,
        "targets": len(entities),
        "fetched": len(items),
        "inserted": inserted,
        "prefilter": sum(item["prefilter_pass"] for item in items),
        **result,
    }, render(conn)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="invest-news")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--out", type=Path, default=PROJECT_ROOT / "data/news-latest.txt")
    parser.add_argument("--enable-llm", action="store_true")
    args = parser.parse_args(argv)
    conn = db.connect(args.db)
    try:
        db.init_schema(conn)
        summary, text = run(conn, load_config(args.config), enable_llm=args.enable_llm)
        watchlist.atomic_write(str(args.out), text)
        print(json.dumps(summary, sort_keys=True))
        return 0
    except (NewsError, OSError, ValueError) as exc:
        print(f"news run failed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
