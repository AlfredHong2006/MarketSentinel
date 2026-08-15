"""News provider interface, Google News RSS adapter, and explicit demo fallback."""

import json
import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

import feedparser
import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from marketsentinel.domain import (
    Article,
    Constituent,
    IngestionFunnel,
    NewsFetchResult,
    SourceHealth,
)
from marketsentinel.errors import ProviderError
from marketsentinel.normalization import article_fingerprint, deduplicate_articles, relevance_score
from marketsentinel.timeutils import ensure_utc, utc_now

LOGGER = logging.getLogger(__name__)


class NewsProvider(Protocol):
    name: str

    def fetch(
        self,
        constituent: Constituent,
        since: datetime,
        max_articles: int,
    ) -> NewsFetchResult: ...


class GoogleNewsRssProvider:
    """Free recent-news adapter. RSS is not treated as a historical-news archive."""

    name = "Google News RSS"

    def __init__(self, timeout_seconds: float = 10.0, user_agent: str = "MarketSentinel/0.1"):
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent

    @retry(
        retry=retry_if_exception_type(httpx.HTTPError),
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=0.5, max=4),
        reraise=True,
    )
    def _request(self, query: str) -> httpx.Response:
        response = httpx.get(
            "https://news.google.com/rss/search",
            params={"q": query, "hl": "en-GB", "gl": "GB", "ceid": "GB:en"},
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/rss+xml, application/xml",
            },
            timeout=self.timeout_seconds,
            follow_redirects=True,
        )
        response.raise_for_status()
        return response

    def fetch(
        self,
        constituent: Constituent,
        since: datetime,
        max_articles: int,
    ) -> NewsFetchResult:
        started = time.perf_counter()
        lookback_days = max(1, (utc_now() - ensure_utc(since)).days + 1)
        query = f'"{constituent.name}" OR "{constituent.symbol} stock" when:{lookback_days}d'
        try:
            response = self._request(query)
        except httpx.HTTPError as exc:
            latency = round((time.perf_counter() - started) * 1000)
            return NewsFetchResult(
                articles=[],
                health=SourceHealth(
                    provider=self.name,
                    status="unavailable",
                    latency_ms=latency,
                    message=f"RSS request failed after retries: {type(exc).__name__}",
                ),
            )

        parsed = feedparser.parse(response.content)
        entries = list(parsed.entries)
        fetched_at = utc_now()
        articles: list[Article] = []
        for entry in entries:
            published = _entry_datetime(entry)
            if published is None or published < ensure_utc(since):
                continue
            title = str(entry.get("title", "")).strip()
            url = str(entry.get("link", "")).strip()
            source = str(entry.get("source", {}).get("title", "Unknown source")).strip()
            if source != "Unknown source" and title.endswith(f" - {source}"):
                title = title[: -(len(source) + 3)].strip()
            relevance = relevance_score(title, constituent)
            if not title or not url or relevance < 0.35:
                continue
            articles.append(
                Article(
                    fingerprint=article_fingerprint(title, constituent.symbol),
                    ticker=constituent.symbol,
                    title=title,
                    url=url,
                    source=source,
                    published_at=published,
                    fetched_at=fetched_at,
                    provider=self.name,
                    relevance_score=relevance,
                )
            )

        valid = deduplicate_articles(articles)[:max_articles]
        latency = round((time.perf_counter() - started) * 1000)
        if not entries:
            message = "The RSS response contained no entries; HTTP success alone is not healthy."
            status = "degraded"
        elif not valid:
            message = "RSS returned entries, but none passed date and relevance validation."
            status = "degraded"
        else:
            message = None
            status = "healthy"
        return NewsFetchResult(
            articles=valid,
            health=SourceHealth(
                provider=self.name,
                status=status,
                records_received=len(entries),
                valid_records=len(valid),
                latency_ms=latency,
                message=message,
            ),
            funnel=IngestionFunnel(
                retrieved=len(entries),
                relevant=len(articles),
                unique=len(valid),
            ),
        )


def _entry_datetime(entry: object) -> datetime | None:
    parsed = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if parsed is None:
        return None
    return datetime(*parsed[:6], tzinfo=UTC)


class DemoNewsProvider:
    """Deterministic synthetic records used only when clearly marked as demo data."""

    name = "MarketSentinel demo fixture"

    def __init__(self, fixture_path: Path):
        self.fixture_path = fixture_path

    def fetch(
        self,
        constituent: Constituent,
        since: datetime,
        max_articles: int,
    ) -> NewsFetchResult:
        try:
            templates = json.loads(self.fixture_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ProviderError(f"Unable to load demo news fixture: {self.fixture_path}") from exc
        now = utc_now()
        articles: list[Article] = []
        for template in templates:
            published = now - timedelta(hours=float(template["published_hours_ago"]))
            if published < ensure_utc(since):
                continue
            values = {"company": constituent.name, "symbol": constituent.symbol.casefold()}
            title = str(template["title"]).format(**values)
            articles.append(
                Article(
                    fingerprint=article_fingerprint(title, constituent.symbol),
                    ticker=constituent.symbol,
                    title=title,
                    url=str(template["url"]).format(**values),
                    source=str(template["source"]),
                    published_at=published,
                    fetched_at=now,
                    provider=self.name,
                    relevance_score=relevance_score(title, constituent),
                    is_demo=True,
                )
            )
        articles = deduplicate_articles(articles)[:max_articles]
        return NewsFetchResult(
            articles=articles,
            health=SourceHealth(
                provider=self.name,
                status="degraded",
                records_received=len(templates),
                valid_records=len(articles),
                message="Synthetic demo headlines are in use because live RSS returned no valid data.",
            ),
            funnel=IngestionFunnel(
                retrieved=len(templates),
                relevant=len(articles),
                unique=len(articles),
            ),
        )


class NewsService:
    def __init__(
        self,
        primary: NewsProvider,
        demo_fallback: NewsProvider | None = None,
    ) -> None:
        self.primary = primary
        self.demo_fallback = demo_fallback

    def fetch_result(
        self,
        constituent: Constituent,
        since: datetime,
        max_articles: int,
    ) -> tuple[NewsFetchResult, list[SourceHealth]]:
        primary_result = self.primary.fetch(constituent, since, max_articles)
        health = [primary_result.health]
        if primary_result.articles or self.demo_fallback is None:
            return primary_result, health
        try:
            fallback_result = self.demo_fallback.fetch(constituent, since, max_articles)
        except ProviderError as exc:
            LOGGER.exception("Demo news fallback failed")
            health.append(
                SourceHealth(
                    provider=self.demo_fallback.name,
                    status="unavailable",
                    message=str(exc),
                )
            )
            return NewsFetchResult(
                articles=[],
                health=primary_result.health,
                funnel=primary_result.funnel,
            ), health
        health.append(fallback_result.health)
        return fallback_result, health

    def fetch(
        self,
        constituent: Constituent,
        since: datetime,
        max_articles: int,
    ) -> tuple[list[Article], list[SourceHealth]]:
        """Compatibility wrapper for callers that only need articles and health."""

        result, health = self.fetch_result(constituent, since, max_articles)
        return result.articles, health
