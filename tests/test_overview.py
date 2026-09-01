"""What the Company Overview projection is allowed to be.

Two contracts are pinned here, and they are the whole reason the projection exists.

*Parity.* The Overview must be exactly what the deterministic layers already decided. Which rows
are developments, what order they are in, which reports are one development, what may be called
external support, and which stored analyses clear the shared meaningful-event floor on a chart are
questions answered in ``materiality``, ``dashboard_intelligence`` and ``dashboard_charts``. If the
projection could disagree with them, a non-Python client would be reading a second opinion, which
is the exact failure moving these call sites to the API boundary is meant to prevent.

*A read is a read.* ``read_stored`` exists so that opening the product costs nothing. The tests
below make every ingestion, scoring, analysis and write path a tripwire rather than asserting after
the fact, so a future change that quietly reintroduces one fails here instead of on a bill.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import make_article, make_constituent, make_price_history
from fastapi.testclient import TestClient

from marketsentinel.analysis_compatibility import ArticleAnalysisCompatibility
from marketsentinel.api.app import Services, create_app
from marketsentinel.config import Settings
from marketsentinel.dashboard_charts import (
    price_frame_for_timeframe,
    select_meaningful_events,
)
from marketsentinel.dashboard_intelligence import (
    EMPTY_TODAYS_INTELLIGENCE_MESSAGE,
    TODAYS_INTELLIGENCE_CAPTION,
    corroboration_label,
    evidence_breakdown_label,
    prepare_todays_intelligence,
)
from marketsentinel.dashboard_market_view import NO_PRICE_MESSAGE, build_market_view
from marketsentinel.dashboard_risks import (
    CONCERN_INDEX_CAPTION,
    EMPTY_TOP_RISKS_MESSAGE,
    prepare_top_risk_rows,
)
from marketsentinel.domain import (
    AnalyzedEvent,
    ArticleAnalysis,
    ArticleEvidenceReference,
    ClaimAssessment,
    CompanyIntelligenceEvent,
    CompanyOverview,
    CompanyReference,
    DailySentiment,
    EventDirection,
    EventExtraction,
    EventType,
    EvidenceStatus,
    PriceHistory,
    RankedRisk,
    RiskDiagnostics,
    RiskTheme,
    SourceClass,
    TimeHorizon,
)
from marketsentinel.event_analysis import (
    ARTICLE_ANALYSIS_SCHEMA_VERSION,
    STAGE_A_PROMPT_VERSION,
    STAGE_B_PROMPT_VERSION,
    STAGE_C_PROMPT_VERSION,
)
from marketsentinel.forecasting.baseline import BaselineForecaster
from marketsentinel.materiality import (
    EMPTY_KEY_DEVELOPMENTS_MESSAGE,
    key_developments_caption,
    prepare_key_developments,
)
from marketsentinel.overview import NO_PRICE_OBSERVATIONS_MESSAGE, build_company_overview
from marketsentinel.risk_scoring import RiskRanking
from marketsentinel.sentiment.finbert import StaticSentimentAnalyzer
from marketsentinel.service import MarketAnalysisService
from marketsentinel.storage.sqlite import SQLiteRepository

NOW = datetime(2026, 8, 20, 12, tzinfo=UTC)
ACME = CompanyReference(symbol="ACME", name="Acme Corporation")
DISCLAIMER = "Educational research only."
CONTRACT_TITLE = "Acme wins $4 billion supply contract from Bolt for the Ohio plant"


# --------------------------------------------------------------------------------------------
# Factories
# --------------------------------------------------------------------------------------------


def reference(
    article_id: str,
    title: str,
    publisher: str,
    *,
    published_at: datetime | None = None,
) -> ArticleEvidenceReference:
    return ArticleEvidenceReference(
        article_id=article_id,
        title=title,
        publisher=publisher,
        published_at=published_at or NOW - timedelta(hours=1),
        url=f"https://example.com/{article_id}",
    )


def event(
    article_id: str,
    *,
    title: str = CONTRACT_TITLE,
    event_type: EventType = EventType.CONTRACT_AWARD,
    magnitude: float = 0.7,
    publisher: str = "Reuters",
    hours_old: int = 0,
    external_publishers: tuple[str, ...] = (),
    contradicted: bool = False,
) -> CompanyIntelligenceEvent:
    published_at = NOW - timedelta(hours=hours_old)
    sources = [
        reference(f"{article_id}-e{index}", f"Independent desk {index} filed on it", name)
        for index, name in enumerate(external_publishers)
    ]
    claims = []
    if sources:
        claims.append(
            ClaimAssessment(
                claim_id="claim_1",
                status=EvidenceStatus.CORROBORATED,
                reasoning="Deterministic test verdict.",
                evidence_article_ids=[item.article_id for item in sources],
                confidence=0.8,
            )
        )
    if contradicted:
        disputed = reference(f"{article_id}-d", "A rival desk disputes the size", "CNBC")
        sources.append(disputed)
        claims.append(
            ClaimAssessment(
                claim_id="claim_2",
                status=EvidenceStatus.CONTRADICTED,
                reasoning="Deterministic test verdict.",
                evidence_article_ids=[disputed.article_id],
                confidence=0.8,
            )
        )
    return CompanyIntelligenceEvent(
        article_id=article_id,
        source_reference=reference(article_id, title, publisher, published_at=published_at),
        source_class=SourceClass.MAJOR_FINANCIAL_NEWS,
        subject_company=ACME,
        event=EventExtraction(
            event_type=event_type,
            summary="A deterministic test extraction.",
            direction=EventDirection.POSITIVE,
            magnitude=magnitude,
            time_horizon=TimeHorizon.MONTHS,
            model_confidence=0.85,
            important_claims=["Acme did the thing the title reports."],
        ),
        claims=claims,
        evidence_strength=0.5,
        evidence_sources=sources,
    )


def sample_events() -> list[CompanyIntelligenceEvent]:
    """A corpus with a grouped pair, a contradiction, and two rows the gate must reject."""

    return [
        event("a1", external_publishers=("Bloomberg", "Financial Times")),
        # Same wording from a second desk within the window: one development, two publishers.
        event("a2", publisher="Associated Press", hours_old=6),
        event(
            "a3",
            title="Acme faces an EU antitrust investigation into its licensing terms",
            event_type=EventType.REGULATION,
            magnitude=0.6,
            publisher="Financial Times",
            contradicted=True,
        ),
        event("a4", title="Acme shares jump 12% after the contract announcement"),
        event("a5", title="Why Acme could keep winning data-centre deals"),
    ]


def ranked_risk(theme: RiskTheme, concern_index: int, band: str) -> RankedRisk:
    return RankedRisk(
        theme=theme,
        concern_index=concern_index,
        band=band,
        summary=(
            "A deterministic downside mechanism written out at some length so the display "
            "truncation rule has something genuinely longer than its own character limit to "
            "actually shorten, rather than a short string it would pass through untouched."
        ),
        primary_article_id="a3",
        primary_article_url="https://example.com/a3",
        primary_publisher="Financial Times",
        first_evidenced_at=NOW - timedelta(days=20),
        latest_published_at=NOW - timedelta(days=1),
        supporting_publishers=["Reuters"],
        supporting_signal_count=2,
        supporting_event_group_count=1,
    )


def sample_risks() -> RiskRanking:
    return RiskRanking(
        top_risks=(
            ranked_risk(RiskTheme.REGULATORY_ANTITRUST, 72, "Severe"),
            ranked_risk(RiskTheme.CUSTOMER_CONCENTRATION, 41, "Watch"),
        ),
        diagnostics=RiskDiagnostics(considered_analyses=5, eligible_analyses=3, themes_ranked=2),
    )


def marker(article_id: str, event_date, magnitude: float = 0.7) -> AnalyzedEvent:
    return AnalyzedEvent(
        article_id=article_id,
        event_date=event_date,
        article_url=f"https://example.com/{article_id}",
        event_type=EventType.CONTRACT_AWARD.value,
        summary="A deterministic test extraction.",
        direction=EventDirection.POSITIVE.value,
        magnitude=magnitude,
        extraction_confidence=0.85,
        evidence_strength=0.5,
    )


def build_overview(
    events: list[CompanyIntelligenceEvent],
    *,
    price_history: PriceHistory | None = None,
    price_message: str | None = None,
    analyzed_events: list[AnalyzedEvent] | None = None,
    daily_sentiment: list[DailySentiment] | None = None,
    risks: RiskRanking | None = None,
) -> CompanyOverview:
    return build_company_overview(
        constituent=make_constituent(),
        price_history=price_history,
        price_message=price_message,
        articles=[],
        daily_sentiment=daily_sentiment or [],
        analyzed_events=analyzed_events or [],
        intelligence_events=events,
        risks=risks or RiskRanking(top_risks=(), diagnostics=RiskDiagnostics()),
        coverage_window_days=30,
        generated_at=NOW,
        disclaimer=DISCLAIMER,
    )


# --------------------------------------------------------------------------------------------
# Projection parity
# --------------------------------------------------------------------------------------------


def test_key_developments_projection_repeats_the_materiality_layer_exactly() -> None:
    """The API must not be able to hold a second opinion about what a development is."""

    events = sample_events()
    expected = prepare_key_developments(events)
    section = build_overview(events).key_developments

    assert [row.article_id for row in section.rows] == [
        row.event.article_id for row in expected.rows
    ]
    assert section.caption == key_developments_caption(expected.diagnostics)
    assert section.empty_message == EMPTY_KEY_DEVELOPMENTS_MESSAGE

    for actual, want in zip(section.rows, expected.rows, strict=True):
        assert actual.event == want.event
        assert actual.members == list(want.group.members)
        assert actual.publisher_count == want.group.publisher_count
        assert actual.impact_label == want.impact_label
        assert actual.impact_score == round(want.event.event.magnitude * 100)
        assert actual.tier_label == want.tier_label
        assert actual.primary_source_label == want.primary_source_label
        assert actual.provenance_note == want.provenance_note
        assert actual.corroboration.metric_label == want.corroboration_metric
        assert actual.corroboration.contradiction_label == want.contradiction_label
        assert actual.corroboration.summary_label == corroboration_label(want.corroboration)
        assert actual.corroboration.breakdown_label == evidence_breakdown_label(want.corroboration)
        assert actual.corroboration.external_sources == want.corroboration.external_sources
        assert actual.corroboration.comparison_articles == want.corroboration.comparison_articles
        assert actual.corroboration.corroborated_claims == want.corroboration.corroborated_claims
        assert actual.corroboration.contradicted_claims == want.corroboration.contradicted_claims
        assert actual.corroboration.unresolved_claims == want.corroboration.unresolved_claims
        assert actual.corroboration.supporting_articles == want.corroboration.supporting_articles
        assert actual.corroboration.primary_is_official == want.corroboration.primary_is_official


def test_key_development_diagnostics_still_reconcile_after_projection() -> None:
    """``considered`` equals ``material`` plus every rejection, or the funnel caption lies."""

    events = sample_events()
    expected = prepare_key_developments(events)
    diagnostics = build_overview(events).key_developments.diagnostics

    assert diagnostics.considered == len(events)
    assert diagnostics.considered == diagnostics.material + diagnostics.rejected
    assert diagnostics.rejected > 0
    assert diagnostics.material == expected.diagnostics.material
    assert diagnostics.developments == expected.diagnostics.developments
    assert diagnostics.rendered == expected.diagnostics.rendered
    assert diagnostics.rejected_by_condition == dict(expected.diagnostics.rejected_by_condition)


def test_duplicate_reporting_stays_one_development_with_both_publishers() -> None:
    """Grouping must survive projection: republication is breadth, never a second development."""

    section = build_overview(sample_events()).key_developments
    grouped = next(row for row in section.rows if len(row.members) > 1)

    assert grouped.publisher_count == 2
    assert {member.source_reference.publisher for member in grouped.members} == {
        "Reuters",
        "Associated Press",
    }
    assert len(section.rows) < section.diagnostics.material


def test_a_contradicted_claim_is_carried_forward_rather_than_deleted() -> None:
    section = build_overview(sample_events()).key_developments
    disputed = next(row for row in section.rows if row.article_id == "a3")

    assert disputed.corroboration.contradiction_label is not None
    assert disputed.corroboration.contradicted_claims == 1


def test_todays_intelligence_projection_repeats_the_prepared_cards_exactly() -> None:
    """The API must not be able to hold a second opinion about Today's Intelligence either."""

    events = sample_events()
    expected = prepare_todays_intelligence(events)
    section = build_overview(events).todays_intelligence

    assert [card.article_id for card in section.cards] == [
        card.event.article_id for card in expected
    ]
    assert section.caption == TODAYS_INTELLIGENCE_CAPTION
    assert section.empty_message == EMPTY_TODAYS_INTELLIGENCE_MESSAGE
    assert len(section.cards) <= 4

    for actual, want in zip(section.cards, expected, strict=True):
        assert actual.event == want.event
        assert actual.impact_label == want.impact_label
        assert actual.impact_score == want.impact_score
        assert actual.primary_source_label == want.primary_source_label
        assert actual.corroboration.metric_label == want.corroboration_metric
        assert actual.corroboration.contradiction_label == want.contradiction_label
        assert actual.corroboration.summary_label == corroboration_label(want.corroboration)
        assert actual.corroboration.breakdown_label == evidence_breakdown_label(want.corroboration)
        assert actual.corroboration.external_sources == want.corroboration.external_sources
        assert actual.corroboration.comparison_articles == want.corroboration.comparison_articles
        assert actual.corroboration.corroborated_claims == want.corroboration.corroborated_claims
        assert actual.corroboration.contradicted_claims == want.corroboration.contradicted_claims
        assert actual.corroboration.unresolved_claims == want.corroboration.unresolved_claims
        assert actual.corroboration.supporting_articles == want.corroboration.supporting_articles
        assert actual.corroboration.primary_is_official == want.corroboration.primary_is_official


def test_todays_intelligence_reports_the_shared_empty_message_when_nothing_qualifies() -> None:
    section = build_overview([]).todays_intelligence

    assert section.cards == []
    assert section.empty_message == EMPTY_TODAYS_INTELLIGENCE_MESSAGE


def test_top_risks_projection_repeats_the_prepared_rows_without_reordering() -> None:
    risks = sample_risks()
    expected = prepare_top_risk_rows(risks.top_risks)
    section = build_overview([], risks=risks).top_risks

    assert [row.rank for row in section.rows] == [1, 2]
    assert section.caption == CONCERN_INDEX_CAPTION
    assert section.empty_message == EMPTY_TOP_RISKS_MESSAGE
    assert section.diagnostics == risks.diagnostics
    for actual, want in zip(section.rows, expected, strict=True):
        assert actual.label == want.label
        assert actual.concern_index == want.concern_index
        assert actual.band == want.band
        assert actual.band_color == want.band_color
        assert actual.summary == want.summary
        assert actual.risk == want.risk
    assert section.rows[0].summary.endswith("…")


def test_market_view_projection_repeats_the_four_independent_observations() -> None:
    """Four notes, never fused. The projection copies them; it does not compose a verdict."""

    events = sample_events()
    risks = sample_risks()
    history = make_price_history(days=60)
    overview = build_overview(events, price_history=history, risks=risks)
    expected = build_market_view(
        price_points=[point.model_dump(mode="json") for point in history.points],
        daily_sentiment=[],
        risk_rows=prepare_top_risk_rows(risks.top_risks),
        intelligence_cards=prepare_todays_intelligence(events),
    )

    assert overview.market_view.price_note == expected.price_note
    assert overview.market_view.sentiment_note == expected.sentiment_note
    assert overview.market_view.risk_note == expected.risk_note
    assert overview.market_view.intelligence_note == expected.intelligence_note
    assert not hasattr(overview.market_view, "overall_score")


def test_chart_markers_repeat_the_shared_meaningful_event_floor_per_timeframe() -> None:
    """Marker selection stays server-side, so no client can put a sub-floor event on a chart."""

    history = make_price_history()
    dates = [point.date for point in history.points]
    markers = [
        marker("m-recent", dates[-3]),
        marker("m-mid", dates[-40]),
        marker("m-old", dates[-250]),
        # Below the shared magnitude floor: never a marker, in any timeframe.
        marker("m-trivial", dates[-5], magnitude=0.1),
    ]
    overview = build_overview([], price_history=history, analyzed_events=markers)
    price_payload = [point.model_dump(mode="json") for point in history.points]
    marker_payload = [item.model_dump(mode="json") for item in markers]

    assert overview.chart.status == "available"
    assert [view.timeframe for view in overview.chart.timeframes] == ["1M", "3M", "6M", "1Y", "MAX"]
    assert overview.chart.default_timeframe == "6M"
    for view in overview.chart.timeframes:
        frame = price_frame_for_timeframe(price_payload, view.timeframe)
        start, end = frame["date"].min(), frame["date"].max()
        expected = select_meaningful_events(marker_payload, start, end)
        assert [item.article_id for item in view.markers] == [
            str(item["article_id"]) for item in expected
        ]
        assert view.price_observations == len(frame)
        assert "m-trivial" not in {item.article_id for item in view.markers}
    assert {item.article_id for item in overview.chart.timeframes[0].markers} == {"m-recent"}
    assert "m-old" in {item.article_id for item in overview.chart.timeframes[-1].markers}


def test_the_max_timeframe_covers_every_observation_without_a_calendar_cutoff() -> None:
    """MAX is a window, not a new selection rule: the widest one, still server-selected.

    A client must never widen a chart by choosing its own markers, so MAX exists server-side and
    is subject to the same shared meaningful-event floor as every other timeframe.
    """

    history = make_price_history(days=400)
    overview = build_overview([], price_history=history)
    frames = {view.timeframe: view for view in overview.chart.timeframes}

    assert frames["MAX"].price_observations == len(history.points)
    assert frames["MAX"].start_date == history.points[0].date
    assert frames["MAX"].end_date == history.points[-1].date
    # Widest of them all, and never narrower than the longest calendar preset.
    assert frames["MAX"].price_observations >= frames["1Y"].price_observations


def test_a_failed_price_fetch_reports_an_unavailable_chart_and_invents_nothing() -> None:
    overview = build_overview([], price_history=None, price_message="Price history request failed")

    assert overview.chart.status == "unavailable"
    assert overview.chart.message == "Price history request failed"
    assert overview.chart.points == []
    assert overview.chart.timeframes == []
    assert overview.market_view.price_note == NO_PRICE_MESSAGE


def test_an_empty_price_series_is_reported_as_absent_rather_than_flattened() -> None:
    empty = PriceHistory(symbol="ACME", points=[], fetched_at=NOW)
    overview = build_overview([], price_history=empty)

    assert overview.chart.status == "unavailable"
    assert overview.chart.message == NO_PRICE_OBSERVATIONS_MESSAGE
    assert overview.chart.points == []


def test_coverage_reports_what_was_read_including_the_latest_observed_sentiment() -> None:
    sentiment = [
        DailySentiment(
            ticker="ACME",
            date=(NOW - timedelta(days=offset)).date(),
            score=0.2,
            moving_average_7d=0.18,
            trend_3=0.03,
            article_count=3,
            computed_at=NOW,
        )
        for offset in (2, 1)
    ]
    overview = build_overview(sample_events(), daily_sentiment=sentiment)

    assert overview.coverage.analysed_articles == 5
    assert overview.coverage.window_days == 30
    assert overview.coverage.latest_sentiment is not None
    assert overview.coverage.latest_sentiment.date == sentiment[-1].date


def test_the_overview_round_trips_through_json_unchanged() -> None:
    """It is served as a FastAPI response model, so the contract has to survive serialisation."""

    overview = build_overview(
        sample_events(),
        price_history=make_price_history(days=40),
        risks=sample_risks(),
    )

    assert CompanyOverview.model_validate_json(overview.model_dump_json()) == overview


# --------------------------------------------------------------------------------------------
# The read path
# --------------------------------------------------------------------------------------------


class CachedConstituents:
    """Resolution must come from the local cache. Reaching the network is the failure."""

    def resolve(self, symbol: str):
        raise AssertionError(f"the read path resolved {symbol!r} over the network")

    def resolve_cached(self, symbol: str):
        assert symbol == "ACME"
        return make_constituent()


class ExplodingNews:
    def fetch_result(self, *args: Any, **kwargs: Any):
        raise AssertionError("the read path fetched news")

    def fetch_history(self, *args: Any, **kwargs: Any):
        raise AssertionError("the read path fetched historical news")


class ExplodingSentiment:
    def score(self, *args: Any, **kwargs: Any):
        raise AssertionError("the read path scored sentiment")


class ExplodingRunner:
    def analyze_article(self, article_id: str):
        raise AssertionError(f"the read path analysed article {article_id!r}")


class FakePrices:
    def __init__(self, history: PriceHistory | None = None, failure: Exception | None = None):
        self.history = history
        self.failure = failure
        self.calls = 0

    def fetch(self, constituent):
        del constituent
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return self.history or make_price_history()


class ReadOnlyRepositoryGuard:
    """A tripwire, not an assertion: every persistence method fails loudly if a read reaches it."""

    def __init__(self, repository: SQLiteRepository) -> None:
        self._repository = repository

    def __getattr__(self, name: str):
        if name.startswith(("upsert_", "store_", "delete_")):
            raise AssertionError(f"the read path called the write method {name!r}")
        return getattr(self._repository, name)


COMPATIBILITY = ArticleAnalysisCompatibility(
    model_version="test-event-model",
    stage_a_prompt_version=STAGE_A_PROMPT_VERSION,
    stage_b_prompt_version=STAGE_B_PROMPT_VERSION,
    stage_c_prompt_version=STAGE_C_PROMPT_VERSION,
    schema_version=ARTICLE_ANALYSIS_SCHEMA_VERSION,
)


def seed_repository(path) -> SQLiteRepository:
    """One genuine stored article, its sentiment, its analysis, and one sentiment day."""

    repository = SQLiteRepository(path / "market.db")
    repository.initialize()
    now = datetime.now(UTC)
    article = make_article(title=CONTRACT_TITLE, published_at=now - timedelta(days=3))
    repository.upsert_articles([article])
    repository.upsert_sentiments(StaticSentimentAnalyzer().score([article]))
    repository.upsert_daily_sentiment(
        [
            DailySentiment(
                ticker="ACME",
                date=(now - timedelta(days=3)).date(),
                score=0.2,
                moving_average_7d=0.18,
                trend_3=0.02,
                article_count=1,
                computed_at=now,
            )
        ]
    )
    repository.store_article_analysis(
        ArticleAnalysis(
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
                summary="Acme won a multi-year supply contract.",
                direction=EventDirection.POSITIVE,
                magnitude=0.7,
                time_horizon=TimeHorizon.MONTHS,
                model_confidence=0.85,
                important_claims=["Acme won a multi-year supply contract."],
            ),
            evidence_count=0,
            evidence_strength=0.6,
            evidence_fingerprint="evidence-test",
            model_version=COMPATIBILITY.model_version,
            stage_a_prompt_version=STAGE_A_PROMPT_VERSION,
            stage_b_prompt_version=STAGE_B_PROMPT_VERSION,
            stage_c_prompt_version=STAGE_C_PROMPT_VERSION,
            schema_version=ARTICLE_ANALYSIS_SCHEMA_VERSION,
            analysis_created_at=now,
        ),
        "test-cache-version",
    )
    return repository


def stored_row_counts(repository: SQLiteRepository) -> dict[str, int]:
    tables = ("articles", "sentiments", "daily_sentiment", "article_intelligence_analyses")
    connection = repository._connect()  # noqa: SLF001 - a direct census, deliberately unmediated
    try:
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        }
    finally:
        connection.close()


def read_only_service(repository: SQLiteRepository, prices: FakePrices) -> MarketAnalysisService:
    return MarketAnalysisService(
        constituents=CachedConstituents(),
        news=ExplodingNews(),
        historical_news=ExplodingNews(),
        sentiment=ExplodingSentiment(),
        prices=prices,
        repository=ReadOnlyRepositoryGuard(repository),
        forecaster=BaselineForecaster(),
        article_analysis_compatibility=COMPATIBILITY,
        article_analysis_runner=ExplodingRunner(),
    )


def test_read_stored_ingests_nothing_analyses_nothing_and_writes_nothing(
    writable_tmp_path,
) -> None:
    repository = seed_repository(writable_tmp_path)
    before = stored_row_counts(repository)
    prices = FakePrices()
    service = read_only_service(repository, prices)

    overview = service.read_stored("ACME")

    assert stored_row_counts(repository) == before
    assert prices.calls == 1
    assert overview.data_source == "stored"
    assert overview.constituent.symbol == "ACME"
    assert overview.coverage.analysed_articles == 1
    assert overview.coverage.articles == 1
    assert overview.coverage.latest_sentiment is not None
    assert overview.chart.status == "available"
    assert overview.key_developments.diagnostics.considered == 1


def test_repeated_reads_never_grow_the_stored_corpus(writable_tmp_path) -> None:
    """The regression that matters for a recruiter-facing page: a page load is not an ingest."""

    repository = seed_repository(writable_tmp_path)
    before = stored_row_counts(repository)
    service = read_only_service(repository, FakePrices())

    first = service.read_stored("ACME")
    second = service.read_stored("ACME")

    assert stored_row_counts(repository) == before
    assert first.key_developments.rows == second.key_developments.rows
    assert first.top_risks.rows == second.top_risks.rows


def test_read_stored_survives_a_failed_price_fetch_without_failing_the_request(
    writable_tmp_path,
) -> None:
    from marketsentinel.errors import ProviderError

    repository = seed_repository(writable_tmp_path)
    service = read_only_service(
        repository, FakePrices(failure=ProviderError("Price history request failed for ACME"))
    )

    overview = service.read_stored("ACME")

    assert overview.chart.status == "unavailable"
    assert overview.chart.message == "Price history request failed for ACME"
    assert overview.chart.points == []
    assert overview.coverage.analysed_articles == 1


def test_read_stored_skips_the_price_fetch_when_nothing_is_stored(writable_tmp_path) -> None:
    """A zero-coverage company renders an intentional empty state, so its read must make no
    external call at all -- the price series would be this read's only third-party request,
    fetched purely to be discarded behind a page that never shows a chart."""

    from marketsentinel.service import NO_STORED_COVERAGE_PRICE_MESSAGE

    repository = SQLiteRepository(writable_tmp_path / "market.db")
    repository.initialize()
    prices = FakePrices()
    service = read_only_service(repository, prices)

    overview = service.read_stored("ACME")

    assert prices.calls == 0
    assert overview.coverage.articles == 0
    assert overview.coverage.analysed_articles == 0
    assert overview.key_developments.rows == []
    assert overview.top_risks.rows == []
    assert overview.chart.status == "unavailable"
    assert overview.chart.message == NO_STORED_COVERAGE_PRICE_MESSAGE
    assert overview.chart.points == []


def test_stale_stored_analyses_are_excluded_by_the_compatibility_rule(writable_tmp_path) -> None:
    """The read path applies the same exact-equality contract the refresh path applies.

    A bumped prompt version is the case that matters: ``accepts_for_display`` compares the four
    prompt and schema versions, so a stored analysis the running application can no longer
    interpret is skipped entirely rather than rendered under the new version's meaning.
    """

    repository = seed_repository(writable_tmp_path)
    service = read_only_service(repository, FakePrices())
    service.article_analysis_compatibility = ArticleAnalysisCompatibility(
        model_version=COMPATIBILITY.model_version,
        stage_a_prompt_version="event-extraction-from-a-future-version",
        stage_b_prompt_version=STAGE_B_PROMPT_VERSION,
        stage_c_prompt_version=STAGE_C_PROMPT_VERSION,
        schema_version=ARTICLE_ANALYSIS_SCHEMA_VERSION,
    )

    overview = service.read_stored("ACME")

    assert overview.coverage.analysed_articles == 0
    assert overview.key_developments.rows == []
    assert overview.key_developments.diagnostics.considered == 0


# --------------------------------------------------------------------------------------------
# The API boundary
# --------------------------------------------------------------------------------------------


def build_test_app(repository: SQLiteRepository, service: MarketAnalysisService, settings=None):
    return create_app(
        settings=settings,
        services=Services(
            repository=repository,
            constituents=CachedConstituents(),
            analysis=service,
            article_events=SimpleNamespace(),
        ),
    )


def test_overview_endpoint_serves_the_projection_over_a_get(writable_tmp_path) -> None:
    repository = seed_repository(writable_tmp_path)
    before = stored_row_counts(repository)
    app = build_test_app(repository, read_only_service(repository, FakePrices()))

    with TestClient(app) as client:
        response = client.get("/api/v1/companies/acme/overview")

    response.raise_for_status()
    payload = response.json()
    assert stored_row_counts(repository) == before
    # ``model_validate_json`` rather than ``model_validate``: the stored-event contract is
    # strict, so it parses JSON rather than coercing already-decoded Python strings.
    assert CompanyOverview.model_validate_json(response.text).constituent.symbol == "ACME"
    assert payload["data_source"] == "stored"
    assert payload["key_developments"]["caption"]
    assert payload["todays_intelligence"]["caption"]
    assert "forecast" not in payload
    assert "automatic_analysis" not in payload


def test_overview_endpoint_reports_an_unknown_symbol_as_not_found(writable_tmp_path) -> None:
    repository = seed_repository(writable_tmp_path)

    class UnknownConstituents(CachedConstituents):
        def resolve_cached(self, symbol: str):
            from marketsentinel.errors import ConstituentNotFoundError

            raise ConstituentNotFoundError(f"{symbol!r} is not in the universe")

    service = read_only_service(repository, FakePrices())
    service.constituents = UnknownConstituents()
    app = build_test_app(repository, service)

    with TestClient(app) as client:
        response = client.get("/api/v1/companies/NOPE/overview")

    assert response.status_code == 404


@pytest.mark.parametrize(
    ("origin", "allowed"),
    [("http://localhost:5173", True), ("https://not-configured.example", False)],
)
def test_cors_origins_come_from_settings(writable_tmp_path, origin: str, allowed: bool) -> None:
    """A future browser client is a configuration change, not a code change."""

    repository = seed_repository(writable_tmp_path)
    settings = Settings(cors_allow_origins=("http://localhost:5173",))
    app = build_test_app(repository, read_only_service(repository, FakePrices()), settings)

    with TestClient(app) as client:
        response = client.get("/health", headers={"Origin": origin})

    assert response.status_code == 200
    assert ("access-control-allow-origin" in response.headers) is allowed
