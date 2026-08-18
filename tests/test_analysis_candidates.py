from datetime import UTC, datetime, timedelta

import pytest
from conftest import make_article

from marketsentinel.analysis_candidates import (
    select_analysis_candidates,
    select_analysis_candidates_with_diagnostics,
)
from marketsentinel.domain import ArticleAnalysisResponse, CompanyReference
from marketsentinel.service import MarketAnalysisService

NOW = datetime(2026, 8, 18, 12, tzinfo=UTC)
APPLE = CompanyReference(symbol="AAPL", name="Apple Inc.")


def article(
    title: str,
    *,
    hours_old: int = 1,
    source: str = "Reuters",
    relevance: float = 0.9,
):
    value = make_article(
        title=title,
        published_at=NOW - timedelta(hours=hours_old),
        source=source,
        url=f"https://example.com/{abs(hash((title, source)))}",
    )
    return value.model_copy(update={"ticker": "AAPL", "relevance_score": relevance})


def test_candidate_ranking_is_deterministic_for_same_articles_and_now() -> None:
    articles = [
        article("Apple announces new product", source="Apple Newsroom"),
        article("Apple signs a supply agreement", source="Reuters", hours_old=2),
        article("Apple expands a services contract", source="General Daily", hours_old=3),
    ]

    first = select_analysis_candidates(articles, NOW, 3, subject_company=APPLE)
    second = select_analysis_candidates(list(reversed(articles)), NOW, 3, subject_company=APPLE)

    assert [item.fingerprint for item in first] == [item.fingerprint for item in second]


def test_demo_and_below_floor_articles_are_excluded() -> None:
    demo = article("Apple announces a product").model_copy(update={"is_demo": True})
    weak = article("Apple is mentioned", relevance=0.49)
    result = select_analysis_candidates_with_diagnostics(
        [demo, weak], NOW, 10, subject_company=APPLE
    )

    assert result.candidates == ()
    assert result.diagnostics.demo_rejected == 1
    assert result.diagnostics.low_relevance_rejected == 1


def test_zero_candidate_limit_selects_nothing() -> None:
    result = select_analysis_candidates_with_diagnostics(
        [article("Apple announces a product")], NOW, 0, subject_company=APPLE
    )

    assert result.candidates == ()
    assert result.diagnostics.considered == 1
    assert result.diagnostics.selected == 0


@pytest.mark.parametrize(
    "title",
    [
        "Fund X buys 5,301 shares of Apple",
        "Asset manager sells 8,000 Apple shares",
        "Hedge fund bought 1.2 million shares of Apple",
    ],
)
def test_obvious_title_only_holding_article_is_excluded_without_snippet(title: str) -> None:
    holding = article(title).model_copy(update={"snippet": None})
    result = select_analysis_candidates_with_diagnostics([holding], NOW, 10, subject_company=APPLE)

    assert result.candidates == ()
    assert result.diagnostics.obvious_holdings_rejected == 1


@pytest.mark.parametrize(
    "title",
    [
        "Apple acquires another company",
        "Apple invests in another business",
        "Apple partners with Broadcom",
        "Apple raises capital for manufacturing",
        "Apple shares rise after product launch",
        "Fund reports holding in Apple as Apple announces strategic investment",
    ],
)
def test_subject_company_actions_are_not_prefiltered_as_holding_reports(title: str) -> None:
    assert select_analysis_candidates([article(title)], NOW, 1, subject_company=APPLE)


@pytest.mark.parametrize(
    ("title", "subject"),
    [
        (
            "Apple Reports Record Q3 Revenue as Institutional Investors Increase Apple Holdings",
            APPLE,
        ),
        ("Apple Unveils Vision Pro 2 as Investors Take Apple Positions", APPLE),
        (
            "NVIDIA Wins $2bn Contract; Investors Increase NVIDIA Stake",
            CompanyReference(symbol="NVDA", name="NVIDIA"),
        ),
        (
            "NVIDIA Posts Record Data-Centre Growth; Institution Discloses NVIDIA Position",
            CompanyReference(symbol="NVDA", name="NVIDIA"),
        ),
    ],
)
def test_mixed_corporate_event_and_holdings_headline_survives_prefilter(
    title: str,
    subject: CompanyReference,
) -> None:
    assert select_analysis_candidates([article(title)], NOW, 1, subject_company=subject)


def test_commentary_is_deprioritized_but_not_blindly_excluded() -> None:
    commentary = article(
        "Apple may expand its payments partnership",
        source="The Motley Fool",
    )
    news = article(
        "Apple files routine supplier disclosure",
        source="General Daily",
        hours_old=24,
    )
    result = select_analysis_candidates_with_diagnostics(
        [commentary, news], NOW, 2, subject_company=APPLE
    )

    assert [item.fingerprint for item in result.candidates] == [
        news.fingerprint,
        commentary.fingerprint,
    ]
    assert result.diagnostics.commentary_deprioritized == 1


def test_narrow_stock_prediction_is_excluded() -> None:
    prediction = article("Prediction: Will Apple stock rise this year?", source="General Daily")
    result = select_analysis_candidates_with_diagnostics(
        [prediction], NOW, 1, subject_company=APPLE
    )

    assert result.candidates == ()
    assert result.diagnostics.excluded_prediction == 1


def test_publisher_cap_is_respected() -> None:
    articles = [
        article(f"Apple development uniqueitem{index}", hours_old=index) for index in range(5)
    ]
    result = select_analysis_candidates_with_diagnostics(
        articles, NOW, 10, subject_company=APPLE, publisher_cap=2
    )

    assert len(result.candidates) == 2
    assert result.diagnostics.publisher_cap_rejected == 3


def test_near_identical_headline_diversity_works_across_publishers() -> None:
    first = article(
        "Apple announces Broadcom wireless chip partnership",
        source="Reuters",
    )
    duplicate = article(
        "Apple and Broadcom announce wireless chips partnership",
        source="General Daily",
        hours_old=2,
    )
    distinct = article(
        "Apple appoints new operations executive",
        source="Industry Journal",
        hours_old=3,
    )
    result = select_analysis_candidates_with_diagnostics(
        [first, duplicate, distinct], NOW, 10, subject_company=APPLE
    )

    assert len(result.candidates) == 2
    assert result.diagnostics.near_title_rejected == 1


@pytest.mark.parametrize(
    ("first_title", "second_title"),
    [
        ("NVIDIA signs deal with Toyota", "NVIDIA signs deal with Honda"),
        ("Apple acquires AI startup Voysis", "Apple acquires AI startup Xnor"),
        ("NVIDIA opens Arizona plant", "NVIDIA opens Texas plant"),
        ("NVIDIA beats earnings estimates", "NVIDIA misses earnings estimates"),
    ],
)
def test_templated_titles_with_replaced_event_terms_are_both_retained(
    first_title: str,
    second_title: str,
) -> None:
    result = select_analysis_candidates_with_diagnostics(
        [
            article(first_title, source="Reuters"),
            article(second_title, source="General Daily", hours_old=2),
        ],
        NOW,
        10,
        subject_company=APPLE,
    )

    assert len(result.candidates) == 2
    assert result.diagnostics.near_title_rejected == 0


@pytest.mark.parametrize(
    ("first_title", "second_title"),
    [
        (
            "NVIDIA launches Blackwell Ultra platform",
            "NVIDIA launches Blackwell Ultra platform",
        ),
        (
            "NVIDIA launches Blackwell Ultra platform",
            "NVIDIA launches Blackwell Ultra platform for enterprise AI systems",
        ),
    ],
)
def test_syndicated_and_one_sided_expanded_titles_are_still_suppressed(
    first_title: str,
    second_title: str,
) -> None:
    result = select_analysis_candidates_with_diagnostics(
        [
            article(first_title, source="Reuters"),
            article(second_title, source="General Daily", hours_old=2),
        ],
        NOW,
        10,
        subject_company=APPLE,
    )

    assert len(result.candidates) == 1
    assert result.diagnostics.near_title_rejected == 1


class SequenceRunner:
    def __init__(self, statuses: list[str]) -> None:
        self.statuses = statuses
        self.calls: list[str] = []

    def analyze_article(self, article_id: str) -> ArticleAnalysisResponse:
        self.calls.append(article_id)
        status = self.statuses[len(self.calls) - 1]
        return ArticleAnalysisResponse(article_id=article_id, status=status)


def service_for_runner(runner, *, candidates: int = 40, max_new: int = 6):
    return MarketAnalysisService(
        constituents=None,
        news=None,
        historical_news=None,
        sentiment=None,
        prices=None,
        repository=None,
        forecaster=None,
        article_analysis_runner=runner,
        analysis_auto_candidates=candidates,
        analysis_auto_max_new_per_run=max_new,
    )


def runner_articles(count: int):
    return [
        article(
            f"Apple development uniqueevent{index}",
            hours_old=index,
            source=f"Publisher {index}",
        )
        for index in range(count)
    ]


def test_new_attempt_budget_never_exceeds_six_across_twenty_candidates() -> None:
    runner = SequenceRunner(["generated"] * 20)
    service = service_for_runner(runner)

    diagnostics = service._run_automatic_analysis(runner_articles(20), APPLE, NOW)

    assert len(runner.calls) == 6
    assert diagnostics.newly_generated == 6
    assert diagnostics.budget_skipped == 14


def test_cached_responses_do_not_consume_new_attempt_budget() -> None:
    runner = SequenceRunner(["cached"] * 5 + ["generated"] * 6 + ["generated"] * 9)
    service = service_for_runner(runner)

    diagnostics = service._run_automatic_analysis(runner_articles(20), APPLE, NOW)

    assert len(runner.calls) == 11
    assert diagnostics.cached == 5
    assert diagnostics.newly_generated == 6
    assert diagnostics.budget_skipped == 9


def test_two_consecutive_failures_or_unavailable_trip_circuit_breaker() -> None:
    runner = SequenceRunner(["failed", "unavailable"] + ["generated"] * 18)
    service = service_for_runner(runner)

    diagnostics = service._run_automatic_analysis(runner_articles(20), APPLE, NOW)

    assert len(runner.calls) == 2
    assert diagnostics.failed == 1
    assert diagnostics.unavailable == 1
    assert diagnostics.circuit_breaker_tripped is True
    assert diagnostics.budget_skipped == 18


def test_cached_or_generated_success_resets_circuit_breaker_streak() -> None:
    runner = SequenceRunner(["failed", "cached", "unavailable", "generated"])
    service = service_for_runner(runner, candidates=4)

    diagnostics = service._run_automatic_analysis(runner_articles(4), APPLE, NOW)

    assert len(runner.calls) == 4
    assert diagnostics.circuit_breaker_tripped is False
    assert diagnostics.budget_skipped == 0


@pytest.mark.parametrize("runner,max_new", [(None, 6), (SequenceRunner([]), 0)])
def test_no_runner_or_zero_cap_makes_no_calls(runner, max_new: int) -> None:
    service = service_for_runner(runner, max_new=max_new)

    diagnostics = service._run_automatic_analysis(runner_articles(3), APPLE, NOW)

    assert runner is None or runner.calls == []
    assert diagnostics.budget_skipped == 3
