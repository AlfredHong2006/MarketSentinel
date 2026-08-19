from datetime import UTC, datetime, timedelta

import pytest

from marketsentinel.domain import (
    ArticleAnalysis,
    ArticleEvidenceReference,
    CompanyReference,
    EventDirection,
    EventExtraction,
    EventType,
    RiskTheme,
    SourceClass,
    TimeHorizon,
)
from marketsentinel.risk_scoring import (
    MAX_TOP_RISKS,
    band_for,
    describes_same_event,
    rank_company_risks,
)
from marketsentinel.risk_signals import extract_risk_signals

NOW = datetime(2026, 8, 19, 12, tzinfo=UTC)


def analysis(
    article_id: str,
    *,
    title: str = "Acme hit by new export controls on advanced parts",
    publisher: str = "Reuters",
    direction: EventDirection = EventDirection.NEGATIVE,
    event_type: EventType = EventType.REGULATION,
    magnitude: float = 0.60,
    confidence: float = 0.85,
    horizon: TimeHorizon = TimeHorizon.MONTHS,
    negative_channels: list[str] | None = None,
    evidence_strength: float = 0.60,
    source_class: SourceClass = SourceClass.MAJOR_FINANCIAL_NEWS,
    published_at: datetime | None = None,
) -> ArticleAnalysis:
    return ArticleAnalysis(
        article_id=article_id,
        source_reference=ArticleEvidenceReference(
            article_id=article_id,
            title=title,
            publisher=publisher,
            published_at=published_at or NOW,
            url=f"https://example.com/{article_id}",
        ),
        source_class=source_class,
        subject_company=CompanyReference(symbol="ACME", name="Acme Corporation"),
        event=EventExtraction(
            event_type=event_type,
            summary="Regulators imposed new licence requirements on Acme.",
            direction=direction,
            magnitude=magnitude,
            time_horizon=horizon,
            model_confidence=confidence,
            important_claims=["Acme received a licence requirement."],
            negative_channels=negative_channels or [],
        ),
        evidence_count=0,
        evidence_strength=evidence_strength,
        evidence_fingerprint="fingerprint",
        model_version="test-model",
        stage_a_prompt_version="a",
        stage_b_prompt_version="b",
        stage_c_prompt_version="c",
        schema_version="s",
        analysis_created_at=NOW,
    )


def theme_score(analyses: list[ArticleAnalysis], theme: RiskTheme) -> int:
    ranking = rank_company_risks(analyses, now=NOW)
    return next(risk.concern_index for risk in ranking.top_risks if risk.theme is theme)


# ------------------------------------------------------------------------- bands


@pytest.mark.parametrize(
    ("concern_index", "band"),
    [
        (100, "Severe"),
        (70, "Severe"),
        (69, "Elevated"),
        (50, "Elevated"),
        (49, "Moderate"),
        (30, "Moderate"),
        (29, "Watch"),
        (1, "Watch"),
    ],
)
def test_band_boundaries(concern_index: int, band: str) -> None:
    assert band_for(concern_index) == band


# ------------------------------------------------------------- empty / sparse state


def test_no_signals_produces_no_risks_rather_than_padded_rows() -> None:
    ranking = rank_company_risks([], now=NOW)

    assert ranking.top_risks == ()
    assert ranking.diagnostics.themes_ranked == 0


def test_only_positive_events_without_channels_produce_no_risks() -> None:
    records = [
        analysis(
            f"a{index}", direction=EventDirection.POSITIVE, event_type=EventType.PRODUCT_LAUNCH
        )
        for index in range(3)
    ]

    assert rank_company_risks(records, now=NOW).top_risks == ()


def test_unmapped_signals_never_appear_as_a_ranked_theme() -> None:
    records = [
        analysis(
            "unmapped",
            direction=EventDirection.POSITIVE,
            event_type=EventType.INVESTMENT,
            negative_channels=["the company continues to operate normally"],
        )
    ]

    ranking = rank_company_risks(records, now=NOW)

    assert ranking.top_risks == ()
    assert RiskTheme.UNMAPPED not in {risk.theme for risk in ranking.top_risks}
    assert ranking.diagnostics.unmapped_signals == 1


# -------------------------------------------------------------------- duplication


def test_six_rewrites_from_one_publisher_cannot_linearly_inflate_a_theme() -> None:
    single = [analysis("only")]
    rewrites = [
        analysis(f"rewrite-{index}", published_at=NOW - timedelta(hours=index))
        for index in range(6)
    ]

    base = theme_score(single, RiskTheme.REGULATORY_ANTITRUST)
    inflated = theme_score(rewrites, RiskTheme.REGULATORY_ANTITRUST)

    assert inflated == base
    ranking = rank_company_risks(rewrites, now=NOW)
    assert ranking.top_risks[0].supporting_event_group_count == 1


def test_same_event_across_publishers_receives_only_bounded_corroboration() -> None:
    title = "Acme hit by new export controls on advanced parts"
    records = [
        analysis(f"pub-{index}", publisher=publisher, title=title)
        for index, publisher in enumerate(["Reuters", "Bloomberg", "CNBC", "Financial Times"])
    ]

    base = theme_score([records[0]], RiskTheme.REGULATORY_ANTITRUST)
    corroborated = theme_score(records, RiskTheme.REGULATORY_ANTITRUST)

    # Publisher bonus is capped at two steps of 0.04, so at most eight index points.
    assert base < corroborated <= base + 8
    assert rank_company_risks(records, now=NOW).top_risks[0].supporting_event_group_count == 1


def test_distinct_events_under_one_theme_add_bounded_event_corroboration() -> None:
    records = [
        analysis("suit-1", title="Acme sued in California over battery claims"),
        analysis("suit-2", title="Acme faces Ohio lawsuit about warranty terms"),
        analysis("suit-3", title="Acme defends Texas class action on pricing"),
    ]
    for record in records:
        assert record.event.event_type is EventType.REGULATION

    ranking = rank_company_risks(records, now=NOW)
    risk = ranking.top_risks[0]

    assert risk.supporting_event_group_count == 3
    base = theme_score([records[0]], RiskTheme.REGULATORY_ANTITRUST)
    # Event bonus is capped at two steps of 0.04 and the publisher is identical throughout.
    assert base < risk.concern_index <= base + 8


def test_total_corroboration_bonus_never_exceeds_sixteen_points() -> None:
    records = [
        analysis(
            f"wide-{index}",
            publisher=publisher,
            title=f"Acme faces {city} regulatory action over licensing",
        )
        for index, (publisher, city) in enumerate(
            [
                ("Reuters", "California"),
                ("Bloomberg", "Ohio"),
                ("CNBC", "Texas"),
                ("Financial Times", "Arizona"),
                ("WSJ", "Nevada"),
            ]
        )
    ]

    base = theme_score([records[0]], RiskTheme.REGULATORY_ANTITRUST)

    assert theme_score(records, RiskTheme.REGULATORY_ANTITRUST) <= base + 16


@pytest.mark.parametrize(
    ("first_title", "second_title"),
    [
        ("Acme signs supply deal with Toyota", "Acme signs supply deal with Honda"),
        ("Acme opens Arizona plant", "Acme opens Texas plant"),
        ("Acme sued in California over pricing", "Acme sued in Florida over pricing"),
    ],
)
def test_distinct_named_events_are_not_false_merged(first_title: str, second_title: str) -> None:
    first = analysis("first", title=first_title)
    second = analysis("second", title=second_title, published_at=NOW - timedelta(hours=2))
    signals = extract_risk_signals([first, second], now=NOW).signals

    assert not describes_same_event(signals[0], signals[1])


def test_identical_and_reworded_duplicates_are_grouped() -> None:
    first = analysis("a", title="Acme hit by new export controls on advanced parts")
    second = analysis(
        "b",
        title="Acme hit by new export control on advanced part",
        published_at=NOW - timedelta(hours=3),
    )
    signals = extract_risk_signals([first, second], now=NOW).signals

    assert describes_same_event(signals[0], signals[1])


def test_events_outside_the_forty_eight_hour_window_are_not_grouped() -> None:
    title = "Acme hit by new export controls on advanced parts"
    first = analysis("a", title=title)
    second = analysis("b", title=title, published_at=NOW - timedelta(hours=60))
    signals = extract_risk_signals([first, second], now=NOW).signals

    assert not describes_same_event(signals[0], signals[1])


def test_weak_signals_below_half_of_base_do_not_earn_corroboration() -> None:
    strong = analysis("strong", publisher="Reuters", magnitude=1.0)
    # A heavily decayed, low-magnitude mention from another publisher.
    weak = analysis(
        "weak",
        publisher="Bloomberg",
        magnitude=0.30,
        horizon=TimeHorizon.DAYS,
        published_at=NOW - timedelta(days=60),
        title="Acme mentioned briefly in a regulatory roundup",
    )

    assert theme_score([strong], RiskTheme.REGULATORY_ANTITRUST) == theme_score(
        [strong, weak], RiskTheme.REGULATORY_ANTITRUST
    )


# ------------------------------------------------------------------ severe-band gate


def test_single_general_news_publisher_is_capped_below_severe() -> None:
    record = analysis("solo", magnitude=1.0, confidence=1.0, evidence_strength=1.0)

    ranking = rank_company_risks([record], now=NOW)

    assert ranking.top_risks[0].concern_index == 69
    assert ranking.top_risks[0].band == "Elevated"
    assert ranking.diagnostics.severe_band_capped == 1


def test_two_distinct_publishers_may_exceed_the_severe_threshold() -> None:
    title = "Acme hit by new export controls on advanced parts"
    records = [
        analysis(
            "a",
            publisher="Reuters",
            title=title,
            magnitude=1.0,
            confidence=1.0,
            evidence_strength=1.0,
        ),
        analysis(
            "b",
            publisher="Bloomberg",
            title=title,
            magnitude=1.0,
            confidence=1.0,
            evidence_strength=1.0,
        ),
    ]

    ranking = rank_company_risks(records, now=NOW)

    assert ranking.top_risks[0].concern_index > 69
    assert ranking.top_risks[0].band == "Severe"
    assert ranking.diagnostics.severe_band_capped == 0


@pytest.mark.parametrize(
    "source_class", [SourceClass.OFFICIAL_COMPANY, SourceClass.REGULATORY_OR_FILING]
)
def test_official_or_regulatory_source_alone_may_exceed_the_severe_threshold(
    source_class: SourceClass,
) -> None:
    record = analysis(
        "filing", magnitude=1.0, confidence=1.0, evidence_strength=1.0, source_class=source_class
    )

    ranking = rank_company_risks([record], now=NOW)

    assert ranking.top_risks[0].concern_index > 69
    assert ranking.diagnostics.severe_band_capped == 0


# -------------------------------------------------------------- ranking / selection


def test_at_most_four_risks_are_returned_and_ordering_is_by_concern() -> None:
    records = [
        analysis("reg", magnitude=1.0, event_type=EventType.REGULATION),
        analysis("lit", magnitude=0.85, event_type=EventType.LITIGATION),
        analysis("sup", magnitude=0.70, event_type=EventType.SUPPLY_DISRUPTION),
        analysis("mac", magnitude=0.55, event_type=EventType.MACROECONOMIC_EXPOSURE),
        analysis("mgmt", magnitude=0.40, event_type=EventType.MANAGEMENT_CHANGE),
        analysis("guide", magnitude=0.35, event_type=EventType.ANALYST_OR_GUIDANCE_CHANGE),
    ]

    ranking = rank_company_risks(records, now=NOW)
    scores = [risk.concern_index for risk in ranking.top_risks]

    assert len(ranking.top_risks) == MAX_TOP_RISKS
    assert scores == sorted(scores, reverse=True)
    # themes_ranked counts what ranking produced, not what the display cap kept.
    assert ranking.diagnostics.themes_ranked == 6


def test_themes_ranked_counts_produced_themes_not_displayed_themes() -> None:
    """Seven produced themes must remain visible in diagnostics behind the top-four cap."""

    records = [
        analysis(f"t{index}", event_type=event_type, magnitude=magnitude)
        for index, (event_type, magnitude) in enumerate(
            [
                (EventType.REGULATION, 1.0),
                (EventType.LITIGATION, 0.95),
                (EventType.SUPPLY_DISRUPTION, 0.90),
                (EventType.MACROECONOMIC_EXPOSURE, 0.85),
                (EventType.MANAGEMENT_CHANGE, 0.80),
                (EventType.CONTRACT_LOSS, 0.75),
                (EventType.ANALYST_OR_GUIDANCE_CHANGE, 0.70),
            ]
        )
    ]

    ranking = rank_company_risks(records, now=NOW)

    assert len({risk.theme for risk in ranking.top_risks}) == MAX_TOP_RISKS
    assert len(ranking.top_risks) == MAX_TOP_RISKS
    assert ranking.diagnostics.themes_ranked == 7


def test_ranking_is_deterministic_and_ties_break_stably() -> None:
    records = [
        analysis("lit", event_type=EventType.LITIGATION, title="Acme faces a lawsuit in Ohio"),
        analysis("sup", event_type=EventType.SUPPLY_DISRUPTION, title="Acme supply line disrupted"),
        analysis("mac", event_type=EventType.MACROECONOMIC_EXPOSURE, title="Acme macro exposure"),
    ]

    first = rank_company_risks(records, now=NOW).top_risks
    reordered = rank_company_risks(list(reversed(records)), now=NOW).top_risks
    repeated = rank_company_risks(records, now=NOW).top_risks

    assert [risk.concern_index for risk in first] == [risk.concern_index for risk in reordered]
    assert [risk.theme for risk in first] == [risk.theme for risk in reordered]
    assert [risk.theme for risk in first] == [risk.theme for risk in repeated]
    # Equal-scoring themes must fall back to a stable alphabetical theme key.
    assert len({risk.concern_index for risk in first}) == 1
    assert [risk.theme.value for risk in first] == sorted(risk.theme.value for risk in first)


def test_payload_carries_provenance_for_every_ranked_risk() -> None:
    records = [
        analysis("a", publisher="Reuters"),
        analysis("b", publisher="Bloomberg", published_at=NOW - timedelta(hours=5)),
    ]

    risk = rank_company_risks(records, now=NOW).top_risks[0]

    assert risk.primary_article_id in {"a", "b"}
    assert risk.primary_article_url.startswith("https://example.com/")
    assert set(risk.supporting_article_ids) == {"a", "b"}
    assert set(risk.supporting_publishers) == {"Reuters", "Bloomberg"}
    assert risk.supporting_signal_count == 2
    assert risk.latest_published_at == NOW
    assert risk.summary


def test_diagnostics_report_deterministic_counters() -> None:
    records = [
        analysis("reg", event_type=EventType.REGULATION),
        analysis(
            "cap",
            direction=EventDirection.POSITIVE,
            event_type=EventType.INVESTMENT,
            negative_channels=["increases capital committed before utilisation is proven"],
        ),
        analysis("weak", magnitude=0.10),
    ]

    diagnostics = rank_company_risks(records, now=NOW).diagnostics

    assert diagnostics.considered_analyses == 3
    assert diagnostics.eligible_analyses == 2
    assert diagnostics.signals_extracted == 2
    assert diagnostics.realized_signals == 1
    assert diagnostics.prospective_signals == 1
    assert diagnostics.themes_ranked == 2
    assert diagnostics.event_groups == 2


def test_signals_from_different_themes_are_never_grouped() -> None:
    title = "Acme hit by new export controls on advanced parts"
    regulation = analysis("reg", title=title, event_type=EventType.REGULATION)
    litigation = analysis("lit", title=title, event_type=EventType.LITIGATION)
    signals = extract_risk_signals([regulation, litigation], now=NOW).signals

    assert not describes_same_event(signals[0], signals[1])


def test_titles_without_substantive_terms_are_not_grouped() -> None:
    first = analysis("a", title="A B")
    second = analysis("b", title="C D")
    signals = extract_risk_signals([first, second], now=NOW).signals

    assert not describes_same_event(signals[0], signals[1])


def test_a_theme_with_no_measurable_concern_is_dropped_rather_than_shown_as_zero() -> None:
    """Rounding to zero means no measurable concern remains, so the theme leaves the register."""

    stale = analysis(
        "stale",
        magnitude=0.30,
        horizon=TimeHorizon.IMMEDIATE,
        published_at=NOW - timedelta(days=30),
    )

    ranking = rank_company_risks([stale], now=NOW)

    assert ranking.top_risks == ()
    assert ranking.diagnostics.signals_extracted == 1
    assert ranking.diagnostics.themes_ranked == 0


def test_provenance_advertises_only_signals_that_qualified_for_scoring() -> None:
    """A signal too weak to earn corroboration must not be shown as supporting evidence."""

    strong = analysis("strong", publisher="Reuters", magnitude=1.0)
    weak = analysis(
        "weak",
        publisher="Bloomberg",
        magnitude=0.30,
        horizon=TimeHorizon.DAYS,
        published_at=NOW - timedelta(days=60),
        title="Acme mentioned briefly in a regulatory roundup",
    )

    solo = rank_company_risks([strong], now=NOW).top_risks[0]
    combined = rank_company_risks([strong, weak], now=NOW).top_risks[0]

    # No publisher bonus was earned, so the weak publisher must not be advertised either.
    assert combined.concern_index == solo.concern_index
    assert combined.supporting_publishers == ["Reuters"]
    assert combined.supporting_article_ids == ["strong"]
    assert combined.supporting_signal_count == 1
    # The strongest signal always qualifies, so the primary article is never hidden.
    assert combined.primary_article_id == "strong"
    assert combined.primary_publisher == "Reuters"


def test_qualifying_signals_from_several_publishers_are_all_advertised() -> None:
    title = "Acme hit by new export controls on advanced parts"
    records = [
        analysis("a", publisher="Reuters", title=title),
        analysis("b", publisher="Bloomberg", title=title, published_at=NOW - timedelta(hours=4)),
    ]

    risk = rank_company_risks(records, now=NOW).top_risks[0]

    assert set(risk.supporting_publishers) == {"Reuters", "Bloomberg"}
    assert risk.supporting_signal_count == 2
    assert risk.latest_published_at == NOW


def test_extra_signal_in_an_existing_theme_changes_provenance_but_not_the_score() -> None:
    """A recovered channel weaker than the theme maximum must not move the Concern Index.

    This is the property that makes a taxonomy recall addition safe: mapping a previously
    UNMAPPED mechanism into a theme that a stronger signal already owns adds evidence to the
    payload without inflating the score, because aggregation takes the maximum and the extra
    signal shares one publisher and one event group with it.
    """

    without_channel = analysis(
        "supply",
        event_type=EventType.SUPPLY_DISRUPTION,
        title="Acme warns of component shortage into the fourth quarter",
    )
    with_channel = without_channel.model_copy(
        update={
            "event": without_channel.event.model_copy(
                update={"negative_channels": ["Reduced product availability may cut shipments."]}
            )
        }
    )

    before = rank_company_risks([without_channel], now=NOW).top_risks[0]
    after = rank_company_risks([with_channel], now=NOW).top_risks[0]

    assert after.theme is RiskTheme.SUPPLY_CONSTRAINT
    assert after.concern_index == before.concern_index
    assert after.band == before.band
    assert after.primary_article_id == before.primary_article_id
    # Only the advertised evidence grows, and it stays one article and one event group.
    assert before.supporting_signal_count == 1
    assert after.supporting_signal_count == 2
    assert after.supporting_article_ids == ["supply"]
    assert after.supporting_event_group_count == 1
    assert rank_company_risks([with_channel], now=NOW).diagnostics.unmapped_signals == 0
