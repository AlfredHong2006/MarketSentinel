from datetime import UTC, datetime

import httpx

from marketsentinel.domain import Constituent, IngestionFunnel, NewsFetchResult, SourceHealth
from marketsentinel.sources.historical import (
    GdeltHistoricalNewsProvider,
    GoogleNewsHistoricalProvider,
    HistoricalNewsService,
    _gdelt_failure_message,
    _is_transient_gdelt_error,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self.payload


def test_gdelt_parses_genuine_articles_and_builds_alias_query() -> None:
    requests = []

    def fake_get(url, **kwargs):
        requests.append((url, kwargs))
        return FakeResponse(
            {
                "articles": [
                    {
                        "title": "Nvidia shares rise after earnings",
                        "url": "https://publisher.example/nvda?utm_source=gdelt",
                        "seendate": "20260810T120000Z",
                        "domain": "publisher.example",
                    },
                    {
                        "title": "Nvidia shares rise after earnings update",
                        "url": "https://mirror.example/nvda",
                        "seendate": "20260810T130000Z",
                        "domain": "mirror.example",
                    },
                    {
                        "title": "Unrelated city news",
                        "url": "https://publisher.example/other",
                        "seendate": "20260810T130000Z",
                        "domain": "publisher.example",
                    },
                ]
            }
        )

    provider = GdeltHistoricalNewsProvider(
        window_days=30,
        request_interval_seconds=0,
        http_get=fake_get,
    )
    constituent = Constituent(
        symbol="NVDA",
        yahoo_symbol="NVDA",
        name="NVIDIA",
        market="S&P 500",
        aliases=("Nvidia Corporation",),
    )

    result = provider.fetch_history(
        constituent,
        since=datetime(2026, 7, 16, tzinfo=UTC),
        until=datetime(2026, 8, 15, tzinfo=UTC),
        max_articles=20,
    )

    assert result.health.status == "healthy"
    assert result.funnel.retrieved == 3
    assert result.funnel.relevant == 2
    assert result.funnel.unique == 2
    assert {item.source for item in result.articles} == {"publisher.example", "mirror.example"}
    assert requests[0][0].endswith("/api/v2/doc/doc")
    assert '"NVIDIA"' in requests[0][1]["params"]["query"]
    assert '"Nvidia Corporation"' in requests[0][1]["params"]["query"]
    assert '"NVDA"' in requests[0][1]["params"]["query"]


def test_gdelt_reports_failure_without_fabricating_articles() -> None:
    def failing_get(*args, **kwargs):
        raise httpx.ConnectError("offline")

    provider = GdeltHistoricalNewsProvider(
        window_days=30,
        request_interval_seconds=0,
        http_get=failing_get,
        sleeper=lambda _: None,
    )
    constituent = Constituent(
        symbol="AAPL",
        yahoo_symbol="AAPL",
        name="Apple Inc.",
        market="S&P 500",
    )

    result = provider.fetch_history(
        constituent,
        since=datetime(2026, 7, 16, tzinfo=UTC),
        until=datetime(2026, 8, 15, tzinfo=UTC),
        max_articles=20,
    )

    assert result.articles == []
    assert result.health.status == "unavailable"
    assert result.health.message is not None


def test_gdelt_failure_diagnostics_include_safe_http_and_timeout_categories() -> None:
    request = httpx.Request("GET", "https://api.gdeltproject.org/api/v2/doc/doc")
    response = httpx.Response(
        429,
        request=request,
        headers={"content-type": "text/plain; charset=utf-8"},
    )
    rate_limit = httpx.HTTPStatusError("too many", request=request, response=response)

    assert "rate limited" in _gdelt_failure_message(rate_limit, response)
    assert "HTTP 429" in _gdelt_failure_message(rate_limit, response)
    assert "content-type text/plain" in _gdelt_failure_message(rate_limit, response)
    assert "ConnectTimeout" in _gdelt_failure_message(httpx.ConnectTimeout("slow"), None)
    assert _is_transient_gdelt_error(rate_limit) is False
    assert _is_transient_gdelt_error(httpx.ConnectTimeout("slow")) is True


def test_gdelt_funnel_classifies_date_url_and_relevance_rejections() -> None:
    def fake_get(*args, **kwargs):
        return FakeResponse(
            {
                "articles": [
                    {
                        "title": "NVIDIA Corporation revenue rises",
                        "url": "https://publisher.example/valid",
                        "seendate": "20260810T120000Z",
                    },
                    {
                        "title": "NVIDIA Corporation revenue rises",
                        "url": "https://publisher.example/old",
                        "seendate": "20260701T120000Z",
                    },
                    {
                        "title": "NVIDIA Corporation revenue rises",
                        "seendate": "20260810T120000Z",
                    },
                    {
                        "title": "City council meeting",
                        "url": "https://publisher.example/irrelevant",
                        "seendate": "20260810T120000Z",
                    },
                ]
            }
        )

    provider = GdeltHistoricalNewsProvider(
        window_days=30, request_interval_seconds=0, http_get=fake_get
    )
    constituent = Constituent(symbol="NVDA", yahoo_symbol="NVDA", name="NVIDIA", market="S&P 500")
    result = provider.fetch_history(
        constituent,
        since=datetime(2026, 7, 16, tzinfo=UTC),
        until=datetime(2026, 8, 15, tzinfo=UTC),
        max_articles=20,
    )

    assert result.funnel.retrieved == 4
    assert result.funnel.invalid_dates == 1
    assert result.funnel.invalid_urls == 1
    assert result.funnel.irrelevant == 1
    assert result.funnel.relevant == result.funnel.unique == 1


def test_google_historical_fallback_parses_date_bounded_resolved_articles() -> None:
    xml = b"""<?xml version='1.0' encoding='UTF-8'?>
    <rss version='2.0'><channel><item>
      <title>Apple Inc. raises guidance - Example Finance</title>
      <link>https://news.google.com/rss/articles/redirect-one</link>
      <pubDate>Mon, 10 Aug 2026 12:00:00 GMT</pubDate>
      <source url='https://example-finance.test'>Example Finance</source>
    </item><item>
      <title>Apple orchard harvest - Local Paper</title>
      <link>https://news.google.com/rss/articles/redirect-two</link>
      <pubDate>Mon, 10 Aug 2026 13:00:00 GMT</pubDate>
      <source url='https://local.test'>Local Paper</source>
    </item></channel></rss>"""

    class RssResponse:
        content = xml

        def raise_for_status(self) -> None:
            return None

    queries = []

    def fake_get(url, **kwargs):
        queries.append((url, kwargs))
        return RssResponse()

    provider = GoogleNewsHistoricalProvider(
        http_get=fake_get,
        resolve_url=lambda url: (
            "https://example-finance.test/article" if url.endswith("one") else None
        ),
    )
    constituent = Constituent(
        symbol="AAPL", yahoo_symbol="AAPL", name="Apple Inc.", market="S&P 500"
    )

    result = provider.fetch_history(
        constituent,
        since=datetime(2026, 7, 16, tzinfo=UTC),
        until=datetime(2026, 8, 15, tzinfo=UTC),
        max_articles=20,
    )

    assert result.health.status == "degraded"
    assert result.funnel.retrieved == 2
    assert result.funnel.relevant == 1
    assert result.articles[0].url == "https://example-finance.test/article"
    assert result.articles[0].published_at == datetime(2026, 8, 10, 12, tzinfo=UTC)
    query = queries[0][1]["params"]["q"]
    assert '"Apple Inc."' in query
    assert "after:2026-07-16" in query
    assert "before:2026-08-16" in query


def test_historical_service_retains_primary_error_and_uses_real_fallback() -> None:
    constituent = Constituent(
        symbol="AAPL", yahoo_symbol="AAPL", name="Apple Inc.", market="S&P 500"
    )

    class Primary:
        name = "GDELT"

        def fetch_history(self, *args, **kwargs):
            return NewsFetchResult(
                articles=[],
                health=SourceHealth(provider=self.name, status="unavailable", message="HTTP 429"),
            )

    class Fallback:
        name = "RSS fallback"

        def fetch_history(self, *args, **kwargs):
            return NewsFetchResult(
                articles=[],
                health=SourceHealth(provider=self.name, status="degraded", message="No dated URLs"),
                funnel=IngestionFunnel(retrieved=4),
            )

    result, health = HistoricalNewsService(Primary(), Fallback()).fetch_result(
        constituent,
        datetime(2026, 7, 16, tzinfo=UTC),
        datetime(2026, 8, 15, tzinfo=UTC),
        20,
    )

    assert result.funnel.retrieved == 4
    assert [item.provider for item in health] == ["GDELT", "RSS fallback"]


def test_google_historical_fallback_keeps_real_rss_link_when_consent_blocks_resolution() -> None:
    xml = b"""<?xml version='1.0' encoding='UTF-8'?>
    <rss version='2.0'><channel><item>
      <title>NVIDIA Corporation reports higher revenue - Example Finance</title>
      <link>https://news.google.com/rss/articles/opaque-token</link>
      <pubDate>Mon, 10 Aug 2026 12:00:00 GMT</pubDate>
      <source url='https://example-finance.test'>Example Finance</source>
    </item></channel></rss>"""

    class RssResponse:
        content = xml

        def raise_for_status(self) -> None:
            return None

    provider = GoogleNewsHistoricalProvider(
        http_get=lambda *args, **kwargs: RssResponse(),
        resolve_url=lambda _: None,
    )
    constituent = Constituent(symbol="NVDA", yahoo_symbol="NVDA", name="NVIDIA", market="S&P 500")

    result = provider.fetch_history(
        constituent,
        since=datetime(2026, 7, 16, tzinfo=UTC),
        until=datetime(2026, 8, 15, tzinfo=UTC),
        max_articles=20,
    )

    assert result.articles[0].url == "https://news.google.com/rss/articles/opaque-token"
    assert "unresolved" in (result.health.message or "")
