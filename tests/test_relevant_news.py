"""Relevant News browser: a read-only, stored-data surface with no paid analysis action.

Mirrors the read-path safety and projection-parity style of ``test_overview.py``: every
persistence, network, sentiment, and article-analysis path is a tripwire, and the projection must
not be able to hold a second opinion about what "analysed" means.
"""

from datetime import UTC, datetime, timedelta

from conftest import make_article
from fastapi.testclient import TestClient
from test_overview import (
    ACME,
    COMPATIBILITY,
    CachedConstituents,
    FakePrices,
    build_test_app,
    read_only_service,
    stored_row_counts,
)

from marketsentinel.dashboard_articles import (
    EMPTY_RELEVANT_NEWS_MESSAGE,
    RELEVANT_NEWS_CAPTION,
    prepare_relevant_news,
)
from marketsentinel.domain import (
    ArticleAnalysis,
    ArticleEvidenceReference,
    EventDirection,
    EventExtraction,
    EventType,
    RelevantNewsView,
    SourceClass,
    TimeHorizon,
)
from marketsentinel.event_analysis import (
    ARTICLE_ANALYSIS_SCHEMA_VERSION,
    STAGE_A_PROMPT_VERSION,
    STAGE_B_PROMPT_VERSION,
    STAGE_C_PROMPT_VERSION,
)
from marketsentinel.overview import build_relevant_news
from marketsentinel.sentiment.finbert import StaticSentimentAnalyzer
from marketsentinel.storage.sqlite import SQLiteRepository

NOW = datetime(2026, 8, 31, 12, tzinfo=UTC)


def analysis_for(
    article,
    *,
    stage_a_prompt_version: str = STAGE_A_PROMPT_VERSION,
) -> ArticleAnalysis:
    return ArticleAnalysis(
        article_id=article.fingerprint,
        source_reference=ArticleEvidenceReference(
            article_id=article.fingerprint,
            title=article.title,
            publisher=article.source,
            published_at=article.published_at,
            url=article.url,
        ),
        source_class=SourceClass.MAJOR_FINANCIAL_NEWS,
        subject_company=ACME,
        event=EventExtraction(
            event_type=EventType.CONTRACT_AWARD,
            summary="A deterministic test extraction.",
            direction=EventDirection.POSITIVE,
            magnitude=0.7,
            time_horizon=TimeHorizon.MONTHS,
            model_confidence=0.85,
            important_claims=["A deterministic test claim."],
        ),
        evidence_count=0,
        evidence_strength=0.6,
        evidence_fingerprint="evidence-test",
        model_version=COMPATIBILITY.model_version,
        stage_a_prompt_version=stage_a_prompt_version,
        stage_b_prompt_version=STAGE_B_PROMPT_VERSION,
        stage_c_prompt_version=STAGE_C_PROMPT_VERSION,
        schema_version=ARTICLE_ANALYSIS_SCHEMA_VERSION,
        analysis_created_at=article.published_at,
    )


def seed_three_articles(path) -> SQLiteRepository:
    """One current-compatible analysis, one stale-version analysis, one never analysed."""

    repository = SQLiteRepository(path / "market.db")
    repository.initialize()
    analysed = make_article(
        title="Acme wins a multi-year supply contract",
        published_at=NOW - timedelta(days=1),
        url="https://example.com/analysed",
    )
    stale = make_article(
        title="Acme opens a new distribution centre",
        published_at=NOW - timedelta(days=2),
        url="https://example.com/stale",
    )
    unanalysed = make_article(
        title="Acme shares trade sideways in quiet session",
        published_at=NOW - timedelta(days=3),
        url="https://example.com/unanalysed",
    )
    articles = [analysed, stale, unanalysed]
    repository.upsert_articles(articles)
    repository.upsert_sentiments(StaticSentimentAnalyzer().score(articles))
    repository.store_article_analysis(analysis_for(analysed), "cache-v1")
    repository.store_article_analysis(
        analysis_for(stale, stage_a_prompt_version="event-extraction-from-a-past-version"),
        "cache-v1",
    )
    return repository


# --------------------------------------------------------------------------------------------
# Projection parity: "analysed" means current-compatible display, never a second opinion
# --------------------------------------------------------------------------------------------


def test_only_current_compatible_analyses_are_marked_analysed() -> None:
    analysed = make_article(title="Compatible", url="https://example.com/a")
    stale = make_article(title="Stale version", url="https://example.com/b")
    unanalysed = make_article(title="Never analysed", url="https://example.com/c")
    scored = StaticSentimentAnalyzer().score([analysed, stale, unanalysed])

    # Only the compatible analysis is passed in, exactly as the compatibility-filtering
    # repository read already guarantees -- this projection never re-checks version fields itself.
    compatible = [analysis_for(analysed)]
    rows = prepare_relevant_news(scored, compatible)

    flags = {row.article.fingerprint: row.has_compatible_analysis for row in rows}
    assert flags[analysed.fingerprint] is True
    assert flags[stale.fingerprint] is False
    assert flags[unanalysed.fingerprint] is False


def test_build_relevant_news_carries_every_stored_field_through_unchanged() -> None:
    article = make_article(title="Acme wins a contract", url="https://example.com/x")
    [scored] = StaticSentimentAnalyzer().score([article])

    view = build_relevant_news(articles=[scored], compatible_analyses=[], window_days=366)

    assert view.caption == RELEVANT_NEWS_CAPTION
    assert view.empty_message == EMPTY_RELEVANT_NEWS_MESSAGE
    assert view.window_days == 366
    [row] = view.articles
    assert row.article_id == scored.fingerprint
    assert row.title == scored.title
    assert row.url == scored.url
    assert row.source == scored.source
    assert row.published_at == scored.published_at
    assert row.label == scored.label
    assert row.sentiment_score == scored.sentiment_score
    assert row.is_demo == scored.is_demo
    assert row.has_compatible_analysis is False


def test_empty_relevant_news_reports_the_shared_empty_message() -> None:
    view = build_relevant_news(articles=[], compatible_analyses=[], window_days=366)

    assert view.articles == []
    assert view.empty_message == EMPTY_RELEVANT_NEWS_MESSAGE


def test_the_relevant_news_view_round_trips_through_json_unchanged() -> None:
    article = make_article(title="Acme wins a contract", url="https://example.com/x")
    [scored] = StaticSentimentAnalyzer().score([article])
    view = build_relevant_news(
        articles=[scored], compatible_analyses=[analysis_for(article)], window_days=366
    )

    assert RelevantNewsView.model_validate_json(view.model_dump_json()) == view


# --------------------------------------------------------------------------------------------
# The read path: no ingestion, no scoring, no analysis, no price fetch, no write
# --------------------------------------------------------------------------------------------


def test_list_relevant_news_reads_nothing_analyses_nothing_and_writes_nothing(
    writable_tmp_path,
) -> None:
    repository = seed_three_articles(writable_tmp_path)
    before = stored_row_counts(repository)
    prices = FakePrices()
    service = read_only_service(repository, prices)

    view = service.list_relevant_news("ACME")

    assert stored_row_counts(repository) == before
    # Unlike read_stored, the article browser needs no price series at all.
    assert prices.calls == 0
    assert len(view.articles) == 3


def test_list_relevant_news_marks_only_the_current_compatible_analysis(
    writable_tmp_path,
) -> None:
    repository = seed_three_articles(writable_tmp_path)
    service = read_only_service(repository, FakePrices())

    view = service.list_relevant_news("ACME")

    by_title = {row.title: row.has_compatible_analysis for row in view.articles}
    assert by_title["Acme wins a multi-year supply contract"] is True
    assert by_title["Acme opens a new distribution centre"] is False
    assert by_title["Acme shares trade sideways in quiet session"] is False


def test_list_relevant_news_excludes_articles_outside_its_window(writable_tmp_path) -> None:
    repository = seed_three_articles(writable_tmp_path)
    old_article = make_article(
        title="Ancient Acme news outside the window",
        published_at=NOW - timedelta(days=400),
        url="https://example.com/old",
    )
    repository.upsert_articles([old_article])
    repository.upsert_sentiments(StaticSentimentAnalyzer().score([old_article]))
    service = read_only_service(repository, FakePrices())

    view = service.list_relevant_news("ACME")

    assert "Ancient Acme news outside the window" not in {row.title for row in view.articles}


def test_repeated_reads_never_grow_the_stored_corpus(writable_tmp_path) -> None:
    repository = seed_three_articles(writable_tmp_path)
    before = stored_row_counts(repository)
    service = read_only_service(repository, FakePrices())

    first = service.list_relevant_news("ACME")
    second = service.list_relevant_news("ACME")

    assert stored_row_counts(repository) == before
    assert first == second


# --------------------------------------------------------------------------------------------
# No row cap: the window is the only bound
# --------------------------------------------------------------------------------------------


LARGE_CORPUS_SIZE = 1_200


def seed_large_corpus(path, count: int = LARGE_CORPUS_SIZE) -> SQLiteRepository:
    """A corpus larger than any previous row cap, entirely inside the 366-day window."""

    repository = SQLiteRepository(path / "market.db")
    repository.initialize()
    articles = [
        make_article(
            title=f"Acme corporate development number {index}",
            # Spread across ~300 days so every row stays inside the window while the ordering
            # has something real to sort by.
            published_at=NOW - timedelta(days=index % 300, minutes=index),
            url=f"https://example.com/bulk/{index}",
        )
        for index in range(count)
    ]
    repository.upsert_articles(articles)
    repository.upsert_sentiments(StaticSentimentAnalyzer().score(articles))
    return repository


def test_every_stored_article_in_the_window_is_returned(writable_tmp_path) -> None:
    """The product requirement: browse *all* stored news inside the window.

    Pins the absence of a row cap specifically above 1000, the cap this read path used to apply,
    so a reintroduced limit fails here instead of silently hiding stored articles behind a page
    that still reads as complete.
    """

    repository = seed_large_corpus(writable_tmp_path)
    service = read_only_service(repository, FakePrices())

    view = service.list_relevant_news("ACME")

    assert len(view.articles) == LARGE_CORPUS_SIZE
    # Distinct rows, not one row repeated to reach the count.
    assert len({row.article_id for row in view.articles}) == LARGE_CORPUS_SIZE


def test_the_returned_count_is_the_true_total_for_the_window(writable_tmp_path) -> None:
    """``len(articles)`` is the contract's total, so it must equal what the window really holds.

    The client prints this number as "N articles"; if the read path ever truncated, that caption
    would state a total lower than the stored reality while looking complete.
    """

    repository = seed_large_corpus(writable_tmp_path)
    # One more article outside the window: excluded by the window, not by any cap.
    outside = make_article(
        title="Acme news from before the window",
        published_at=NOW - timedelta(days=500),
        url="https://example.com/outside",
    )
    repository.upsert_articles([outside])
    repository.upsert_sentiments(StaticSentimentAnalyzer().score([outside]))
    service = read_only_service(repository, FakePrices())

    view = service.list_relevant_news("ACME")

    assert len(view.articles) == LARGE_CORPUS_SIZE
    assert "Acme news from before the window" not in {row.title for row in view.articles}


def test_a_large_corpus_is_served_whole_over_the_endpoint(writable_tmp_path) -> None:
    """The cap has to be absent at the boundary too, not merely inside the service."""

    repository = seed_large_corpus(writable_tmp_path)
    app = build_test_app(repository, read_only_service(repository, FakePrices()))

    with TestClient(app) as client:
        response = client.get("/api/v1/companies/ACME/articles")

    response.raise_for_status()
    assert len(response.json()["articles"]) == LARGE_CORPUS_SIZE


def test_the_repository_still_honours_an_explicit_limit(writable_tmp_path) -> None:
    """Removing the cap from this read path must not remove capping from the repository.

    Other callers (the live funnel, the backfill planner) pass deliberate caps of their own.
    """

    repository = seed_large_corpus(writable_tmp_path)

    assert len(repository.list_scored_articles("ACME", limit=10)) == 10
    assert len(repository.list_scored_articles("ACME", limit=None)) == LARGE_CORPUS_SIZE
    # The documented default stays a cap, so no existing caller silently changes behaviour.
    assert len(repository.list_scored_articles("ACME")) == 500


# --------------------------------------------------------------------------------------------
# The API boundary
# --------------------------------------------------------------------------------------------


def test_relevant_news_endpoint_serves_the_projection_over_a_get(writable_tmp_path) -> None:
    repository = seed_three_articles(writable_tmp_path)
    before = stored_row_counts(repository)
    app = build_test_app(repository, read_only_service(repository, FakePrices()))

    with TestClient(app) as client:
        response = client.get("/api/v1/companies/acme/articles")

    response.raise_for_status()
    payload = response.json()
    assert stored_row_counts(repository) == before
    assert len(payload["articles"]) == 3
    assert payload["caption"] == RELEVANT_NEWS_CAPTION
    # No path to the paid per-article analysis action is exposed on this surface.
    assert "analyze" not in payload
    assert "forecast" not in payload


def test_relevant_news_endpoint_reports_an_unknown_symbol_as_not_found(writable_tmp_path) -> None:
    repository = seed_three_articles(writable_tmp_path)

    class UnknownConstituents(CachedConstituents):
        def resolve_cached(self, symbol: str):
            from marketsentinel.errors import ConstituentNotFoundError

            raise ConstituentNotFoundError(f"{symbol!r} is not in the universe")

    service = read_only_service(repository, FakePrices())
    service.constituents = UnknownConstituents()
    app = build_test_app(repository, service)

    with TestClient(app) as client:
        response = client.get("/api/v1/companies/NOPE/articles")

    assert response.status_code == 404
