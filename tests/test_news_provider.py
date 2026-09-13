"""GoogleNewsRssProvider's optional, date-bounded fetch.

``until`` is what lets continuous coverage window-split a wide span into narrower requests (see
coverage_cycle.RecentProviderSource) instead of hitting Google's own per-request result cap once
for the whole span. Every existing caller omits it and must see no behaviour change.

Fully offline: ``_request`` is monkeypatched to a canned RSS payload, never a real HTTP call.
"""

from datetime import UTC, datetime

from marketsentinel.domain import Constituent
from marketsentinel.sources.news import GoogleNewsRssProvider

_XML = b"""<?xml version='1.0' encoding='UTF-8'?>
<rss version='2.0'><channel>
<item>
  <title>Acme Corp signs supply deal - Example Finance</title>
  <link>https://news.google.com/rss/articles/inside-window</link>
  <pubDate>Wed, 05 Aug 2026 12:00:00 GMT</pubDate>
  <source url='https://example-finance.test'>Example Finance</source>
</item>
<item>
  <title>Acme Corp announces new results - Example Finance</title>
  <link>https://news.google.com/rss/articles/after-window</link>
  <pubDate>Fri, 07 Aug 2026 12:00:00 GMT</pubDate>
  <source url='https://example-finance.test'>Example Finance</source>
</item>
</channel></rss>"""


class _RssResponse:
    content = _XML

    def raise_for_status(self) -> None:
        return None


def _constituent() -> Constituent:
    return Constituent(symbol="ACME", yahoo_symbol="ACME", name="Acme Corp", market="S&P 500")


def test_an_unbounded_fetch_keeps_the_original_relative_query_and_takes_every_valid_entry(
    monkeypatch,
) -> None:
    provider = GoogleNewsRssProvider()
    queries: list[str] = []
    monkeypatch.setattr(
        provider, "_request", lambda query: (queries.append(query), _RssResponse())[1]
    )

    result = provider.fetch(_constituent(), since=datetime(2026, 8, 1, tzinfo=UTC), max_articles=10)

    assert "when:" in queries[0]
    assert "after:" not in queries[0] and "before:" not in queries[0]
    assert len(result.articles) == 2  # no upper bound: both entries pass
    assert result.funnel.invalid_dates == 0


def test_a_bounded_fetch_uses_absolute_dates_and_excludes_entries_after_until(monkeypatch) -> None:
    provider = GoogleNewsRssProvider()
    queries: list[str] = []
    monkeypatch.setattr(
        provider, "_request", lambda query: (queries.append(query), _RssResponse())[1]
    )

    result = provider.fetch(
        _constituent(),
        since=datetime(2026, 8, 1, tzinfo=UTC),
        max_articles=10,
        until=datetime(2026, 8, 5, 23, 59, tzinfo=UTC),
    )

    assert "after:2026-08-01" in queries[0]
    assert "before:2026-08-06" in queries[0]  # the day after `until`, so `until`'s own day counts
    assert [item.url for item in result.articles] == [
        "https://news.google.com/rss/articles/inside-window"
    ]
    # The entry after `until` is excluded and explicitly counted, not silently dropped.
    assert result.funnel.invalid_dates == 1
    assert result.funnel.retrieved == 2


def test_a_bounded_fetch_still_reports_its_own_cap_when_exceeded(monkeypatch) -> None:
    provider = GoogleNewsRssProvider()
    monkeypatch.setattr(provider, "_request", lambda query: _RssResponse())

    result = provider.fetch(
        _constituent(),
        since=datetime(2026, 8, 1, tzinfo=UTC),
        max_articles=1,
        until=datetime(2026, 8, 8, tzinfo=UTC),  # both entries fall inside this window
    )

    assert len(result.articles) == 1
    assert result.funnel.request_limited == 1
