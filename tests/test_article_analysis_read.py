"""Reading one stored article analysis: a read that can never become a generation.

This is the endpoint the Relevant News browser opens, so the property that matters is that it
returns only what is already stored. An article without a compatible analysis is reported absent;
it is never an invitation to produce one, and no provider is reached to try.
"""

from fastapi.testclient import TestClient
from test_overview import (
    COMPATIBILITY,
    CachedConstituents,
    FakePrices,
    build_test_app,
    read_only_service,
    stored_row_counts,
)
from test_relevant_news import seed_three_articles

from marketsentinel.analysis_compatibility import ArticleAnalysisCompatibility
from marketsentinel.dashboard_intelligence import (
    corroboration_label,
    prepare_intelligence_card,
    summarize_corroboration,
)
from marketsentinel.domain import StoredArticleAnalysisView
from marketsentinel.event_analysis import (
    ARTICLE_ANALYSIS_SCHEMA_VERSION,
    STAGE_B_PROMPT_VERSION,
    STAGE_C_PROMPT_VERSION,
)

ANALYSED = "Acme wins a multi-year supply contract"
STALE = "Acme opens a new distribution centre"
UNANALYSED = "Acme shares trade sideways in quiet session"


def article_id_for(view, title: str) -> str:
    return next(row.article_id for row in view.articles if row.title == title)


# --------------------------------------------------------------------------------------------
# Parity: the same labels and the same corroboration semantics as everywhere else
# --------------------------------------------------------------------------------------------


def test_article_analysis_repeats_the_shared_card_preparation_exactly(writable_tmp_path) -> None:
    """Opening an analysis by article must not describe it differently from a ranked card."""

    repository = seed_three_articles(writable_tmp_path)
    service = read_only_service(repository, FakePrices())
    listing = service.list_relevant_news("ACME")
    target = article_id_for(listing, ANALYSED)

    view = service.read_article_analysis("ACME", target)
    assert view is not None

    expected = prepare_intelligence_card(view.event)
    assert view.article_id == target
    assert view.impact_label == expected.impact_label
    assert view.impact_score == expected.impact_score
    assert view.primary_source_label == expected.primary_source_label
    assert view.corroboration.metric_label == expected.corroboration_metric
    assert view.corroboration.contradiction_label == expected.contradiction_label
    assert view.corroboration.summary_label == corroboration_label(expected.corroboration)
    summary = summarize_corroboration(view.event)
    assert view.corroboration.external_sources == summary.external_sources
    assert view.corroboration.corroborated_claims == summary.corroborated_claims
    assert view.corroboration.contradicted_claims == summary.contradicted_claims


def test_analysed_is_exactly_the_set_the_browser_marks_analysed(writable_tmp_path) -> None:
    """The chip and the openable detail must agree, or a row offers a detail that 404s."""

    repository = seed_three_articles(writable_tmp_path)
    service = read_only_service(repository, FakePrices())
    listing = service.list_relevant_news("ACME")

    for row in listing.articles:
        found = service.read_article_analysis("ACME", row.article_id)
        assert (found is not None) is row.has_compatible_analysis


def test_a_stale_version_analysis_is_not_readable(writable_tmp_path) -> None:
    """The exact-equality compatibility rule governs this read like every other."""

    repository = seed_three_articles(writable_tmp_path)
    service = read_only_service(repository, FakePrices())
    listing = service.list_relevant_news("ACME")

    assert service.read_article_analysis("ACME", article_id_for(listing, STALE)) is None
    assert service.read_article_analysis("ACME", article_id_for(listing, UNANALYSED)) is None


def test_a_bumped_prompt_version_retires_the_readable_analysis_too(writable_tmp_path) -> None:
    repository = seed_three_articles(writable_tmp_path)
    service = read_only_service(repository, FakePrices())
    target = article_id_for(service.list_relevant_news("ACME"), ANALYSED)
    assert service.read_article_analysis("ACME", target) is not None

    service.article_analysis_compatibility = ArticleAnalysisCompatibility(
        model_version=COMPATIBILITY.model_version,
        stage_a_prompt_version="event-extraction-from-a-future-version",
        stage_b_prompt_version=STAGE_B_PROMPT_VERSION,
        stage_c_prompt_version=STAGE_C_PROMPT_VERSION,
        schema_version=ARTICLE_ANALYSIS_SCHEMA_VERSION,
    )

    assert service.read_article_analysis("ACME", target) is None


def test_an_unknown_article_id_is_absent_rather_than_fabricated(writable_tmp_path) -> None:
    repository = seed_three_articles(writable_tmp_path)
    service = read_only_service(repository, FakePrices())

    assert service.read_article_analysis("ACME", "no-such-article") is None


# --------------------------------------------------------------------------------------------
# Read safety
# --------------------------------------------------------------------------------------------


def test_reading_an_analysis_writes_nothing_and_fetches_no_price(writable_tmp_path) -> None:
    repository = seed_three_articles(writable_tmp_path)
    before = stored_row_counts(repository)
    prices = FakePrices()
    service = read_only_service(repository, prices)
    target = article_id_for(service.list_relevant_news("ACME"), ANALYSED)

    service.read_article_analysis("ACME", target)

    assert stored_row_counts(repository) == before
    assert prices.calls == 0


def test_repeated_reads_are_identical_and_grow_nothing(writable_tmp_path) -> None:
    repository = seed_three_articles(writable_tmp_path)
    before = stored_row_counts(repository)
    service = read_only_service(repository, FakePrices())
    target = article_id_for(service.list_relevant_news("ACME"), ANALYSED)

    assert service.read_article_analysis("ACME", target) == service.read_article_analysis(
        "ACME", target
    )
    assert stored_row_counts(repository) == before


# --------------------------------------------------------------------------------------------
# The API boundary
# --------------------------------------------------------------------------------------------


def test_endpoint_serves_the_stored_analysis_over_a_get(writable_tmp_path) -> None:
    repository = seed_three_articles(writable_tmp_path)
    before = stored_row_counts(repository)
    service = read_only_service(repository, FakePrices())
    target = article_id_for(service.list_relevant_news("ACME"), ANALYSED)
    app = build_test_app(repository, service)

    with TestClient(app) as client:
        response = client.get(f"/api/v1/companies/ACME/articles/{target}/analysis")

    response.raise_for_status()
    assert stored_row_counts(repository) == before
    view = StoredArticleAnalysisView.model_validate_json(response.text)
    assert view.article_id == target
    assert view.event.event.summary


def test_endpoint_reports_an_unanalysed_article_as_not_found(writable_tmp_path) -> None:
    repository = seed_three_articles(writable_tmp_path)
    service = read_only_service(repository, FakePrices())
    target = article_id_for(service.list_relevant_news("ACME"), UNANALYSED)
    app = build_test_app(repository, service)

    with TestClient(app) as client:
        response = client.get(f"/api/v1/companies/ACME/articles/{target}/analysis")

    assert response.status_code == 404
    assert "No compatible stored analysis" in response.json()["detail"]


def test_endpoint_reports_an_unknown_company_as_not_found(writable_tmp_path) -> None:
    repository = seed_three_articles(writable_tmp_path)

    class UnknownConstituents(CachedConstituents):
        def resolve_cached(self, symbol: str):
            from marketsentinel.errors import ConstituentNotFoundError

            raise ConstituentNotFoundError(f"{symbol!r} is not in the universe")

    service = read_only_service(repository, FakePrices())
    service.constituents = UnknownConstituents()

    with TestClient(build_test_app(repository, service)) as client:
        response = client.get("/api/v1/companies/NOPE/articles/anything/analysis")

    assert response.status_code == 404
