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


@pytest.mark.parametrize(
    "title",
    [
        "Apple Inc. $AAPL Shares Sold by Winning Points Advisors LLC",
        "Kozak & Associates Inc. Sells 6,908 Shares of Apple Inc.",
        "Apple Inc. $AAPL Shares Acquired by BSN CAPITAL PARTNERS Ltd",
        "Trust Co of the South Has $27.19 Million Stake in Apple Inc. $AAPL",
        "VectorGlobal IAG Inc. Acquires New Stake in Apple Inc. $AAPL",
        "Apple Inc. $AAPL is General Partner Inc.'s 2nd Largest Position",
        "Rathbones Group PLC Has $832.46 Million Holdings in Apple Inc. $AAPL",
        "Financial Solutions Advisory Group Inc. Invests $3.33 Million in Apple Inc. $AAPL",
        "Apple Inc. $AAPL Stock Position Lifted by Liontrust Investment Partners LLP",
        "Apple Inc. $AAPL Stock Holdings Decreased by Raab & Moskowitz Asset Management LLC",
        "Apple Inc. $AAPL Stake Decreased by Groupama Asset Managment",
        "Kentucky Retirement Systems Sells 840,815 Shares of Apple Inc. $AAPL",
        "Orographic Financial Advisors LLC Purchases Shares of 32,226 Apple Inc. $AAPL",
        "TrueWealth Financial Partners Purchases Shares of 15,158 Apple Inc. $AAPL",
        "Norris Financial Group LLC Acquires Shares of 31,633 Apple Inc. $AAPL",
        "Edgestream Partners L.P. Reduces Holdings in Apple Inc. $AAPL",
        "Apple Inc. $AAPL Shares Sold by Financial Avengers Inc.",
        "Bank of America Corp DE Sells 2,883,820 Shares of Apple Inc. $AAPL",
    ],
)
def test_reverse_external_institutional_holding_titles_are_rejected(title: str) -> None:
    result = select_analysis_candidates_with_diagnostics(
        [article(title)], NOW, 1, subject_company=APPLE
    )

    assert result.candidates == ()
    assert result.diagnostics.obvious_holdings_rejected == 1


@pytest.mark.parametrize(
    "title",
    [
        "Apple sells another company",
        "Apple acquires another company",
        "Apple raises capital for a new factory",
        "Apple invests in another company",
        "Apple shares fall after a product event",
    ],
)
def test_subject_company_actions_are_not_reverse_holding_false_positives(title: str) -> None:
    assert select_analysis_candidates([article(title)], NOW, 1, subject_company=APPLE)


@pytest.mark.parametrize(
    "title",
    [
        "Stocks making the biggest moves midday: NetApp, Intel, Apple and more",
        "Apple Inc Price Today | Live AAPL Price, Chart & Market Data",
        "Microsoft shares are surging. Here's how to still make money",
    ],
)
def test_narrow_market_reaction_only_titles_are_rejected(title: str) -> None:
    result = select_analysis_candidates_with_diagnostics(
        [article(title)], NOW, 1, subject_company=APPLE
    )

    assert result.candidates == ()
    assert result.diagnostics.market_reaction_rejected == 1


@pytest.mark.parametrize(
    "title",
    [
        "Apple Slides After Supply Shortages Hurt Sales Forecast",
        "Microsoft shares pop on revenue beat",
        # A record/biggest + market-capitalisation rule used to reject these genuine results and
        # buyback announcements. Accepting a pure market-value milestone is the accepted trade.
        "NVIDIA posts record data-centre revenue as market cap nears $5tn",
        "NVIDIA reports record quarterly revenue, market value tops $4 trillion",
        "Apple announces biggest buyback in company history as market cap holds $3tn",
    ],
)
def test_market_reaction_with_identifiable_company_event_survives(title: str) -> None:
    assert select_analysis_candidates([article(title)], NOW, 1, subject_company=APPLE)


@pytest.mark.parametrize(
    "title",
    [
        "Apple Inc. announces $500m US investment as Berkshire Hathaway Inc raises stake in Apple",
        "Apple reports record quarterly revenue as Vanguard Group Inc increases position in Apple",
        "Apple acquires an AI startup while Kentucky Retirement Systems sells 840,815 Apple shares",
        "SoftBank Group invests $2bn in Apple to expand an AI partnership",
        "Apple announces $500m investment in US manufacturing",
    ],
)
def test_subject_action_survives_alongside_an_external_ownership_clause(title: str) -> None:
    """Every ownership path shares one escape hatch, including the reverse-structure rules."""

    result = select_analysis_candidates_with_diagnostics(
        [article(title)], NOW, 1, subject_company=APPLE
    )

    assert [item.title for item in result.candidates] == [title]
    assert result.diagnostics.obvious_holdings_rejected == 0


@pytest.mark.parametrize(
    "title",
    [
        "Example Asset Management LLC invests $5m in Apple shares",
        "Financial Solutions Advisory Group Inc. Invests $3.33 Million in Apple Inc. $AAPL",
        "FinArc Investments Inc. Invests $2.32 Million in Apple Inc. $AAPL",
    ],
)
def test_portfolio_owner_cash_investments_are_still_rejected(title: str) -> None:
    result = select_analysis_candidates_with_diagnostics(
        [article(title)], NOW, 1, subject_company=APPLE
    )

    assert result.candidates == ()
    assert result.diagnostics.obvious_holdings_rejected == 1


@pytest.mark.parametrize(
    "title",
    [
        "Apple GC Jennifer Newstead sells 1,439 shares under 10b5-1 plan",
        "Executive sold Apple shares pursuant to Rule 10b5-1 trading plan",
        "Apple SVP disposes of 4,000 shares under a 10b5 1 plan",
    ],
)
def test_routine_scheduled_insider_sales_are_rejected(title: str) -> None:
    result = select_analysis_candidates_with_diagnostics(
        [article(title)], NOW, 1, subject_company=APPLE
    )

    assert result.candidates == ()
    assert result.diagnostics.scheduled_insider_sale_rejected == 1


@pytest.mark.parametrize(
    "title",
    [
        "Apple CEO cancels 10b5-1 plan following acquisition announcement",
        "Apple adopts a new 10b5-1 trading plan for its chief financial officer",
        "Apple insider sells 2,000 shares in an unscheduled disposal",
    ],
)
def test_non_routine_trading_plan_titles_survive(title: str) -> None:
    result = select_analysis_candidates_with_diagnostics(
        [article(title)], NOW, 1, subject_company=APPLE
    )

    assert [item.title for item in result.candidates] == [title]
    assert result.diagnostics.scheduled_insider_sale_rejected == 0


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


def test_official_company_sources_share_one_family_cap() -> None:
    articles = [
        article(
            f"NVIDIA official development uniqueitem{index}",
            hours_old=index,
            source="NVIDIA Blog" if index % 2 == 0 else "NVIDIA Newsroom",
        )
        for index in range(6)
    ]

    result = select_analysis_candidates_with_diagnostics(articles, NOW, 10, subject_company=APPLE)

    assert len(result.candidates) == 3
    assert result.diagnostics.official_family_cap_rejected == 3


def test_official_family_cap_leaves_room_for_major_financial_sources() -> None:
    officials = [
        article(
            f"NVIDIA official event uniqueitem{index}",
            hours_old=index,
            source="NVIDIA Blog" if index % 2 == 0 else "NVIDIA Newsroom",
        )
        for index in range(6)
    ]
    independent = [
        article(
            f"NVIDIA independent event uniqueitem{index}",
            hours_old=index + 6,
            source=("Reuters", "Bloomberg", "Financial Times")[index],
        )
        for index in range(3)
    ]

    result = select_analysis_candidates([*officials, *independent], NOW, 6, subject_company=APPLE)

    assert len(result) == 6
    assert sum(item.source.startswith("NVIDIA") for item in result) == 3
    assert {item.source for item in result[3:]} == {"Reuters", "Bloomberg", "Financial Times"}


def test_recency_precedes_exact_relevance_within_same_high_band_and_source_tier() -> None:
    old_exact_high = article(
        "Apple older high relevance event",
        hours_old=100,
        source="Reuters",
        relevance=0.95,
    )
    recent_band_high = article(
        "Apple recent high relevance event",
        hours_old=1,
        source="Reuters",
        relevance=0.85,
    )

    result = select_analysis_candidates(
        [old_exact_high, recent_band_high], NOW, 2, subject_company=APPLE
    )

    assert result == [recent_band_high, old_exact_high]


def test_higher_relevance_band_precedes_recency_within_same_source_tier() -> None:
    old_high_band = article(
        "Apple older high band event",
        hours_old=100,
        source="Reuters",
        relevance=0.85,
    )
    recent_medium_band = article(
        "Apple recent medium band event",
        hours_old=1,
        source="Reuters",
        relevance=0.84,
    )

    result = select_analysis_candidates(
        [old_high_band, recent_medium_band], NOW, 2, subject_company=APPLE
    )

    assert result == [old_high_band, recent_medium_band]


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
