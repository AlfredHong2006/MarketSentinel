"""Historical-news provider contracts and a free GDELT DOC 2.0 implementation."""

import logging
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import urlsplit

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
from marketsentinel.normalization import (
    article_fingerprint,
    deduplicate_articles,
    historical_query_terms,
    relevance_score,
)
from marketsentinel.timeutils import ensure_utc, utc_now

LOGGER = logging.getLogger(__name__)
_GDELT_DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"


class HistoricalNewsProvider(Protocol):
    """Typed boundary for a provider capable of a bounded historical backfill."""

    name: str

    def fetch_history(
        self,
        constituent: Constituent,
        since: datetime,
        until: datetime,
        max_articles: int,
    ) -> NewsFetchResult: ...


class GdeltHistoricalNewsProvider:
    """GDELT DOC 2.0 article-list adapter for genuine recent historical coverage.

    GDELT's ``seendate`` is stored as ``published_at`` because it is the provider's only article
    timestamp. It represents when GDELT observed the article, not a verified publisher timestamp.
    """

    name = "GDELT DOC 2.0"

    def __init__(
        self,
        timeout_seconds: float = 10.0,
        user_agent: str = "MarketSentinel/0.1",
        window_days: int = 5,
        request_interval_seconds: float = 5.25,
        relevance_threshold: float = 0.5,
        http_get: Callable[..., httpx.Response] = httpx.get,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent
        self.window_days = window_days
        self.request_interval_seconds = request_interval_seconds
        self.relevance_threshold = relevance_threshold
        self._http_get = http_get
        self._sleeper = sleeper
        self._monotonic = monotonic
        self._last_request_at: float | None = None

    @retry(
        retry=retry_if_exception_type(httpx.HTTPError),
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=0.5, max=4),
        reraise=True,
    )
    def _request(
        self, query: str, start: datetime, end: datetime, max_records: int
    ) -> httpx.Response:
        if self._last_request_at is not None:
            remaining = self.request_interval_seconds - (self._monotonic() - self._last_request_at)
            if remaining > 0:
                self._sleeper(remaining)
        response = self._http_get(
            _GDELT_DOC_URL,
            params={
                "query": query,
                "mode": "artlist",
                "format": "json",
                "maxrecords": min(max_records, 250),
                "sort": "datedesc",
                "startdatetime": ensure_utc(start).strftime("%Y%m%d%H%M%S"),
                "enddatetime": ensure_utc(end).strftime("%Y%m%d%H%M%S"),
            },
            headers={"User-Agent": self.user_agent, "Accept": "application/json"},
            timeout=self.timeout_seconds,
            follow_redirects=True,
        )
        self._last_request_at = self._monotonic()
        response.raise_for_status()
        return response

    def fetch_history(
        self,
        constituent: Constituent,
        since: datetime,
        until: datetime,
        max_articles: int,
    ) -> NewsFetchResult:
        started = self._monotonic()
        normalized_since = ensure_utc(since)
        normalized_until = ensure_utc(until)
        if normalized_since >= normalized_until:
            return NewsFetchResult(
                articles=[],
                health=SourceHealth(
                    provider=self.name,
                    status="degraded",
                    message="Historical range is empty.",
                ),
            )

        query = _gdelt_query(constituent)
        windows = list(_time_windows(normalized_since, normalized_until, self.window_days))
        records_per_window = max(25, min(250, (max_articles + len(windows) - 1) // len(windows)))
        fetched_at = utc_now()
        retrieved = 0
        relevant_articles: list[Article] = []
        failures: list[str] = []

        for start, end in windows:
            try:
                response = self._request(query, start, end, records_per_window)
                payload = response.json()
                raw_articles = payload.get("articles", [])
                if not isinstance(raw_articles, list):
                    raise ValueError("GDELT JSON did not contain an articles list")
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                failures.append(
                    f"{start.date().isoformat()}–{end.date().isoformat()}: {_error_name(exc)}"
                )
                LOGGER.warning("GDELT window failed for %s to %s: %s", start, end, exc)
                continue

            retrieved += len(raw_articles)
            for raw_article in raw_articles:
                article = _parse_gdelt_article(
                    raw_article,
                    constituent,
                    fetched_at,
                    self.relevance_threshold,
                )
                if (
                    article is not None
                    and normalized_since <= ensure_utc(article.published_at) <= normalized_until
                ):
                    relevant_articles.append(article)

        unique = _cap_articles_by_date(deduplicate_articles(relevant_articles), max_articles)
        latency = round((self._monotonic() - started) * 1_000)
        if not unique and failures:
            status = "unavailable" if len(failures) == len(windows) else "degraded"
            message = (
                f"{len(failures)}/{len(windows)} GDELT windows failed: {'; '.join(failures[:3])}"
            )
        elif not unique:
            status = "degraded"
            message = "GDELT returned no articles that passed date, URL, and relevance validation."
        elif failures:
            status = "degraded"
            message = f"Backfill is partial: {len(failures)}/{len(windows)} GDELT windows failed."
        else:
            status = "healthy"
            message = None
        return NewsFetchResult(
            articles=unique,
            health=SourceHealth(
                provider=self.name,
                status=status,
                records_received=retrieved,
                valid_records=len(unique),
                latency_ms=latency,
                message=message,
            ),
            funnel=IngestionFunnel(
                retrieved=retrieved,
                relevant=len(relevant_articles),
                unique=len(unique),
            ),
        )


class GoogleNewsHistoricalProvider:
    """Date-bounded Google News RSS discovery fallback.

    Google News RSS can surface genuinely dated articles, but it is not an archive and may omit
    material. A resolved publisher URL is preferred. When Google consent prevents resolution, the
    genuine RSS item URL is retained and explicitly reported as an unresolved Google redirect.
    """

    name = "Google News RSS historical-range fallback"

    def __init__(
        self,
        timeout_seconds: float = 10.0,
        user_agent: str = "MarketSentinel/0.1",
        relevance_threshold: float = 0.5,
        http_get: Callable[..., httpx.Response] = httpx.get,
        resolve_url: Callable[[str], str | None] | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent
        self.relevance_threshold = relevance_threshold
        self._http_get = http_get
        self._resolve_url = resolve_url or self._resolve_publisher_url

    @retry(
        retry=retry_if_exception_type(httpx.HTTPError),
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=0.5, max=4),
        reraise=True,
    )
    def _request(self, query: str) -> httpx.Response:
        response = self._http_get(
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

    def _resolve_publisher_url(self, url: str) -> str | None:
        """Follow a Google RSS redirect and reject it if it does not reach a publisher."""

        try:
            response = self._http_get(
                url,
                headers={"User-Agent": self.user_agent},
                timeout=self.timeout_seconds,
                follow_redirects=True,
            )
            response.raise_for_status()
        except httpx.HTTPError:
            return None
        resolved = str(response.url)
        host = urlsplit(resolved).netloc.casefold()
        if not host or host.endswith("google.com") or host.endswith("googleusercontent.com"):
            return None
        return resolved

    def fetch_history(
        self,
        constituent: Constituent,
        since: datetime,
        until: datetime,
        max_articles: int,
    ) -> NewsFetchResult:
        started = time.perf_counter()
        normalized_since = ensure_utc(since)
        normalized_until = ensure_utc(until)
        query = _google_historical_query(constituent, normalized_since, normalized_until)
        try:
            response = self._request(query)
        except httpx.HTTPError as exc:
            return NewsFetchResult(
                articles=[],
                health=SourceHealth(
                    provider=self.name,
                    status="unavailable",
                    latency_ms=round((time.perf_counter() - started) * 1_000),
                    message=f"Historical RSS request failed after retries: {_error_name(exc)}",
                ),
            )

        parsed = feedparser.parse(response.content)
        entries = list(parsed.entries)
        fetched_at = utc_now()
        relevant_articles: list[Article] = []
        # Bound publisher redirect checks. The feed itself can contain many more entries than the
        # caller requested, and every retained record still has to pass local validation.
        candidate_entries = entries[: min(len(entries), max_articles * 3)]
        for entry in candidate_entries:
            published_at = _rss_entry_datetime(entry)
            title = str(entry.get("title", "")).strip()
            redirect_url = str(entry.get("link", "")).strip()
            source = str(entry.get("source", {}).get("title", "Unknown source")).strip()
            if source != "Unknown source" and title.endswith(f" - {source}"):
                title = title[: -(len(source) + 3)].strip()
            relevance = relevance_score(title, constituent)
            if (
                not title
                or not redirect_url
                or published_at is None
                or not normalized_since <= published_at <= normalized_until
                or relevance < self.relevance_threshold
            ):
                continue
            # Resolve only eligible entries. This prevents a page of noisy RSS results from
            # creating requests to arbitrary publishers. If consent blocks the redirect, retain
            # the genuine provider item URL rather than synthesizing a publisher URL.
            publisher_url = self._resolve_url(redirect_url) or redirect_url
            relevant_articles.append(
                Article(
                    fingerprint=article_fingerprint(title, constituent.symbol),
                    ticker=constituent.symbol,
                    title=title,
                    url=publisher_url,
                    source=source or urlsplit(publisher_url).netloc,
                    published_at=published_at,
                    fetched_at=fetched_at,
                    provider=self.name,
                    relevance_score=relevance,
                )
            )

        unique = _cap_articles_by_date(deduplicate_articles(relevant_articles), max_articles)
        latency = round((time.perf_counter() - started) * 1_000)
        unresolved_redirects = sum(
            urlsplit(article.url).netloc.casefold().endswith("google.com") for article in unique
        )
        if not entries:
            status = "degraded"
            message = "Historical RSS response contained no entries; RSS is not a reliable archive."
        elif not unique:
            status = "degraded"
            message = (
                "RSS returned entries, but none passed date and relevance validation. Historical "
                "RSS coverage is partial/non-archival."
            )
        else:
            status = "degraded"
            message = (
                "Genuine date-bounded RSS fallback in use; coverage is partial/non-archival and "
                "not a complete historical-news record."
            )
            if unresolved_redirects:
                message += (
                    f" {unresolved_redirects} retained link(s) are unresolved Google RSS redirects because "
                    "publisher resolution was blocked."
                )
        return NewsFetchResult(
            articles=unique,
            health=SourceHealth(
                provider=self.name,
                status=status,
                records_received=len(entries),
                valid_records=len(unique),
                latency_ms=latency,
                message=message,
            ),
            funnel=IngestionFunnel(
                retrieved=len(entries),
                relevant=len(relevant_articles),
                unique=len(unique),
            ),
        )


class HistoricalNewsService:
    """Use GDELT first while retaining a transparent no-key fallback on failure."""

    def __init__(
        self,
        primary: HistoricalNewsProvider,
        rss_fallback: HistoricalNewsProvider | None = None,
    ) -> None:
        self.primary = primary
        self.rss_fallback = rss_fallback

    def fetch_result(
        self,
        constituent: Constituent,
        since: datetime,
        until: datetime,
        max_articles: int,
    ) -> tuple[NewsFetchResult, list[SourceHealth]]:
        primary_result = self.primary.fetch_history(constituent, since, until, max_articles)
        health = [primary_result.health]
        if primary_result.articles or self.rss_fallback is None:
            return primary_result, health
        fallback_result = self.rss_fallback.fetch_history(constituent, since, until, max_articles)
        health.append(fallback_result.health)
        return (
            NewsFetchResult(
                articles=fallback_result.articles,
                health=fallback_result.health,
                funnel=IngestionFunnel(
                    retrieved=primary_result.funnel.retrieved + fallback_result.funnel.retrieved,
                    relevant=primary_result.funnel.relevant + fallback_result.funnel.relevant,
                    unique=fallback_result.funnel.unique,
                ),
            ),
            health,
        )


def _gdelt_query(constituent: Constituent) -> str:
    quoted = [f'"{term.replace(chr(34), "")}"' for term in historical_query_terms(constituent)]
    return f"({' OR '.join(quoted)})"


def _google_historical_query(
    constituent: Constituent,
    since: datetime,
    until: datetime,
) -> str:
    """Use controlled aliases with Google News' explicit calendar date operators."""

    terms = historical_query_terms(constituent)
    aliases = " OR ".join(f'"{term}"' for term in terms)
    # Google's date operators use an inclusive-looking, date-only range. We still enforce precise
    # RSS timestamps locally, so provider-side boundary behaviour cannot create invalid records.
    return (
        f"({aliases}) after:{since.date().isoformat()} "
        f"before:{(until + timedelta(days=1)).date().isoformat()}"
    )


def _time_windows(
    since: datetime,
    until: datetime,
    window_days: int,
) -> Sequence[tuple[datetime, datetime]]:
    windows: list[tuple[datetime, datetime]] = []
    cursor = until
    while cursor > since:
        start = max(since, cursor - timedelta(days=window_days))
        windows.append((start, cursor))
        cursor = start
    return tuple(reversed(windows))


def _parse_gdelt_article(
    raw_article: object,
    constituent: Constituent,
    fetched_at: datetime,
    relevance_threshold: float,
) -> Article | None:
    if not isinstance(raw_article, dict):
        return None
    title = str(raw_article.get("title", "")).strip()
    url = str(raw_article.get("url", "")).strip()
    published_at = _parse_gdelt_datetime(raw_article.get("seendate"))
    if not title or not url or published_at is None:
        return None
    relevance = relevance_score(title, constituent)
    if relevance < relevance_threshold:
        return None
    source = str(raw_article.get("domain") or urlsplit(url).netloc or "Unknown source").strip()
    return Article(
        fingerprint=article_fingerprint(title, constituent.symbol),
        ticker=constituent.symbol,
        title=title,
        url=url,
        source=source,
        published_at=published_at,
        fetched_at=fetched_at,
        provider="GDELT DOC 2.0",
        relevance_score=relevance,
    )


def _parse_gdelt_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    for pattern in ("%Y%m%dT%H%M%SZ", "%Y%m%d%H%M%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(value, pattern).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _rss_entry_datetime(entry: object) -> datetime | None:
    parsed = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if parsed is None:
        return None
    return datetime(*parsed[:6], tzinfo=UTC)


def _cap_articles_by_date(articles: Sequence[Article], maximum: int) -> list[Article]:
    """Cap results fairly so popular dates do not erase the 30-day timeline."""

    by_date: dict[object, list[Article]] = defaultdict(list)
    for article in articles:
        by_date[ensure_utc(article.published_at).date()].append(article)
    for values in by_date.values():
        values.sort(key=lambda item: (item.relevance_score, item.published_at), reverse=True)

    selected: list[Article] = []
    dates = sorted(by_date, reverse=True)
    while dates and len(selected) < maximum:
        remaining_dates: list[object] = []
        for day in dates:
            values = by_date[day]
            if values and len(selected) < maximum:
                selected.append(values.pop(0))
            if values:
                remaining_dates.append(day)
        dates = remaining_dates
    return selected


def _error_name(error: Exception) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        return f"HTTP {error.response.status_code}"
    return type(error).__name__
