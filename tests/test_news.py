import json
from datetime import UTC
from datetime import datetime as dt

import duckdb
import pytest

from invest import db, news

NOW = dt(2026, 8, 28, 12, tzinfo=UTC)
URL = "https://news.example/article/1"


def config(**updates):
    value = {
        "max_calls_per_run": 2,
        "max_attempts_per_article": 2,
        "max_age_days": 7,
        "max_future_minutes": 30,
        "max_feed_bytes": 100000,
        "request_timeout_seconds": 2,
        "google_max_items_per_entity": 20,
        "entity_aliases": {},
        "event_terms": ["results", "contract"],
        "excluded_terms": ["stocks to buy", "price target"],
        "model": "test-model",
    }
    value.update(updates)
    return value


def rss(*items):
    body = "".join(
        f"<item><title>{title}</title><link>{url}</link><source>{source}</source>"
        f"<pubDate>{published}</pubDate></item>"
        for title, url, source, published in items
    )
    return f"<?xml version='1.0'?><rss><channel>{body}</channel></rss>".encode()


def entity():
    return {
        "symbol": "ACME",
        "company_name": "Acme Limited",
        "aliases": ("ACME",),
    }


def item(article_id="a1", title="ACME reports quarterly results"):
    return {
        "article_id": article_id,
        "title": title,
        "url": URL + article_id,
        "publisher": "Publisher",
        "source_feed": "test",
        "published_at": NOW,
        "symbol": "ACME",
        "match_reason": "symbol",
        "prefilter_pass": True,
    }


def connection():
    conn = duckdb.connect()
    db.init_schema(conn)
    conn.execute(
        "INSERT INTO stock_universe "
        "(symbol, company_name, series, is_active, source, fetched_at) "
        "VALUES ('ACME', 'Acme Limited', 'EQ', true, 'test', ?)",
        [NOW],
    )
    return conn


def reply(value, *, url):
    return json.dumps(
        {
            "sentiment": value,
            "event_type": "earnings",
            "materiality": 2,
            "rationale": "Headline reports a company result; article body was not inspected.",
        }
    )


def test_target_entities_union_research_and_owned_with_safe_short_aliases(monkeypatch):
    conn = connection()
    conn.execute(
        "INSERT INTO stock_universe "
        "(symbol, company_name, series, is_active, source, fetched_at) "
        "VALUES ('ZEAL', 'Zeal Synthetic Beverages', 'EQ', true, 'test', ?)",
        [NOW],
    )
    monkeypatch.setattr(
        news.research,
        "candidates",
        lambda conn: [{"symbol": "ACME", "screens": ["quality"]}],
    )
    monkeypatch.setattr(
        news.portfolio,
        "reconcile",
        lambda conn: {
            "owned_research": [],
            "owned_not_research": ["ZEAL"],
        },
    )
    cfg = config(entity_aliases={"ZEAL": ["ZEAL SYNTHETIC BEVERAGES"]})
    targets = news.target_entities(conn, cfg)
    assert [target["symbol"] for target in targets] == ["ACME", "ZEAL"]
    assert targets[0]["aliases"] == ("ACME",)
    assert "ZEAL" not in targets[1]["aliases"]
    assert "ZEAL SYNTHETIC BEVERAGES" in targets[1]["aliases"]
    conn.close()


def test_parse_feed_requires_current_valid_https_items():
    raw = rss(
        (
            "ACME reports results",
            "https://example.com/current",
            "Wire",
            "Fri, 28 Aug 2026 10:00:00 GMT",
        ),
        (
            "Old ACME results",
            "https://example.com/old",
            "Wire",
            "Tue, 01 Jan 2024 10:00:00 GMT",
        ),
        (
            "Broken URL",
            "http://example.com/plain",
            "Wire",
            "Fri, 28 Aug 2026 10:00:00 GMT",
        ),
        ("Bad date", "https://example.com/date", "Wire", "yesterday"),
    )
    rows = news.parse_feed(raw, source_feed="test", now=NOW, config=config())
    assert [row["title"] for row in rows] == ["ACME reports results"]
    assert rows[0]["published_at"].tzinfo == UTC


def test_parse_feed_rejects_malformed_empty_or_declared_entity_xml():
    with pytest.raises(news.NewsError, match="valid XML"):
        news.parse_feed(b"<rss>", source_feed="x", now=NOW, config=config())
    with pytest.raises(news.NewsError, match="no items"):
        news.parse_feed(b"<rss><channel/></rss>", source_feed="x", now=NOW, config=config())
    declared = b'<!DOCTYPE rss [<!ENTITY x "expanded">]><rss><channel/></rss>'
    with pytest.raises(news.NewsError, match="XML declarations"):
        news.parse_feed(declared, source_feed="x", now=NOW, config=config())
    utf16 = '<!DOCTYPE rss [<!ENTITY x "expanded">]><rss><channel/></rss>'.encode("utf-16")
    with pytest.raises(news.NewsError, match="unsupported encoding"):
        news.parse_feed(utf16, source_feed="x", now=NOW, config=config())


def test_fetch_uses_no_redirect_handler_and_rejects_changed_final_host(monkeypatch):
    class Response:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def geturl(self):
            return "https://internal.example/feed"

        def read(self, size):
            return b"<rss/>"

    class Opener:
        def open(self, request, timeout):
            return Response()

    handlers = []

    def build_opener(handler):
        handlers.append(handler)
        return Opener()

    monkeypatch.setattr(news.urllib.request, "build_opener", build_opener)
    with pytest.raises(news.NewsError, match="response URL"):
        news.fetch_xml("https://news.google.com/rss/search?q=x", config())
    assert handlers[0] is news._NoRedirect


def test_collect_keeps_distinct_urls_with_the_same_title(monkeypatch):
    entity_row = entity()
    same_title = "ACME reports results"
    google = rss(
        (same_title, "https://publisher.example/one", "Wire", "Fri, 28 Aug 2026 10:00:00 GMT"),
        (same_title, "https://publisher.example/two", "Wire", "Fri, 28 Aug 2026 10:01:00 GMT"),
    )
    et = rss(
        ("Generic market news", "https://et.example/one", "ET", "Fri, 28 Aug 2026 10:00:00 GMT")
    )
    responses = iter((google, et))
    monkeypatch.setattr(news, "fetch_xml", lambda url, cfg: next(responses))
    rows = news.collect([entity_row], config(), now=NOW)
    assert {row["url"] for row in rows} == {
        "https://publisher.example/one",
        "https://publisher.example/two",
    }


def test_entity_match_is_token_bounded_and_prefilter_excludes_recommendations():
    assert news.match_entity("ACME wins a contract", entity()) == "symbol"
    assert news.match_entity("ACMEBANK wins a contract", entity()) is None
    assert news.prefilter("ACME wins a contract", config())
    assert not news.prefilter("Stocks to buy: ACME price target after results", config())
    assert not news.prefilter("ACME opens a new office", config())


def test_store_replay_does_not_churn_fetched_at_and_requires_universe_symbol():
    conn = connection()
    first = item()
    assert news.store_items(conn, [first], fetched_at=NOW) == 1
    stored_at = conn.execute("SELECT fetched_at FROM news_article").fetchone()[0]
    later = NOW.replace(hour=13)
    assert news.store_items(conn, [first], fetched_at=later) == 0
    assert conn.execute("SELECT fetched_at FROM news_article").fetchone()[0] == stored_at
    bad = {**item("bad"), "symbol": "MISSING"}
    with pytest.raises(duckdb.ConstraintException):
        news.store_items(conn, [bad], fetched_at=NOW)
    assert (
        conn.execute("SELECT COUNT(*) FROM news_article WHERE article_id='bad'").fetchone()[0] == 0
    )
    conn.close()


def test_classification_adds_deterministic_citation_and_headline_scope():
    current = item()
    valid = news.parse_classification(reply("positive", url=current["url"]), current)
    assert valid["materiality"] == 2
    assert valid["cited_url"] == current["url"]
    assert valid["evidence_scope"] == "headline-only"
    invented = json.loads(reply("positive", url=current["url"]))
    invented["cited_url"] = "https://example.com/invented"
    with pytest.raises(ValueError, match="wrong keys"):
        news.parse_classification(json.dumps(invented), current)
    malformed_enum = json.loads(reply("positive", url=current["url"]))
    malformed_enum["sentiment"] = ["positive"]
    with pytest.raises(ValueError, match="enum"):
        news.parse_classification(json.dumps(malformed_enum), current)


def test_classification_budget_replay_and_malformed_attempt_accounting():
    conn = connection()
    items = [item("a1"), item("a2"), item("a3")]
    news.store_items(conn, items, fetched_at=NOW)
    calls = []

    def ask(prompt, cfg):
        calls.append(prompt)
        url = items[len(calls) - 1]["url"]
        return reply("neutral", url=url)

    result = news.classify(conn, items, config(), ask=ask, now=NOW)
    assert result == {"attempted": 2, "stored": 2, "errors": 0}
    assert len(calls) == 2
    replay = news.classify(conn, items, config(), ask=ask, now=NOW)
    assert replay == {"attempted": 1, "stored": 1, "errors": 0}
    assert conn.execute("SELECT COUNT(*) FROM news_classification").fetchone()[0] == 3
    assert news.classify(conn, items, config(), ask=ask, now=NOW)["attempted"] == 0
    conn.close()


def test_failed_output_never_becomes_classification_and_stops_at_attempt_cap():
    conn = connection()
    current = item()
    news.store_items(conn, [current], fetched_at=NOW)

    def malformed(prompt, cfg):
        return "not json"

    one = news.classify(conn, [current], config(), ask=malformed, now=NOW)
    two = news.classify(conn, [current], config(), ask=malformed, now=NOW)
    three = news.classify(conn, [current], config(), ask=malformed, now=NOW)
    assert one["errors"] == two["errors"] == 1
    assert three["attempted"] == 0
    assert conn.execute("SELECT COUNT(*) FROM news_classification").fetchone()[0] == 0
    conn.close()


def test_render_has_citation_and_no_trade_instruction():
    conn = connection()
    current = item()
    news.store_items(conn, [current], fetched_at=NOW)
    news.classify(
        conn,
        [current],
        config(),
        ask=lambda prompt, cfg: reply("negative", url=current["url"]),
        now=NOW,
    )
    text = news.render(conn)
    assert "Headline-only" in text
    assert "No trade instruction" in text
    assert current["url"] in text
    assert "negative" in text
    conn.close()
