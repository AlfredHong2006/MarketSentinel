from datetime import UTC, datetime, timedelta

import pytest

from marketsentinel.domain import (
    ArticleAnalysis,
    ArticleEvidenceReference,
    ClaimAssessment,
    CompanyReference,
    EventDirection,
    EventExtraction,
    EventType,
    EvidenceStatus,
    RiskTheme,
    SourceClass,
    TimeHorizon,
)
from marketsentinel.risk_signals import (
    EMBEDDED_CORROBORATION_BONUS,
    HALF_LIFE_DAYS,
    RiskSignalBasis,
    claim_state,
    context_strength,
    decay_for,
    extract_risk_signals,
    support_for,
)

NOW = datetime(2026, 8, 19, 12, tzinfo=UTC)


def analysis(
    *,
    article_id: str = "article-1",
    direction: EventDirection = EventDirection.NEGATIVE,
    event_type: EventType = EventType.REGULATION,
    magnitude: float = 0.60,
    confidence: float = 0.85,
    horizon: TimeHorizon = TimeHorizon.MONTHS,
    negative_channels: list[str] | None = None,
    claims: list[ClaimAssessment] | None = None,
    evidence_strength: float = 0.60,
    source_class: SourceClass = SourceClass.MAJOR_FINANCIAL_NEWS,
    publisher: str = "Reuters",
    title: str = "Acme faces new export controls on advanced parts",
    summary: str = "Regulators imposed new licence requirements on Acme exports.",
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
            summary=summary,
            direction=direction,
            magnitude=magnitude,
            time_horizon=horizon,
            model_confidence=confidence,
            important_claims=["Acme received a new licence requirement."],
            negative_channels=negative_channels or [],
        ),
        claims=claims or [],
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


def claim(
    status: EvidenceStatus, *, cited: bool = True, confidence: float = 0.8
) -> ClaimAssessment:
    return ClaimAssessment(
        claim_id="claim_1",
        status=status,
        reasoning="Assessed against supplied evidence.",
        evidence_article_ids=["evidence-1"] if cited else [],
        confidence=confidence,
    )


# --------------------------------------------------------------------------- gating


def test_shared_meaningful_event_thresholds_are_reused_not_reinvented() -> None:
    below_magnitude = analysis(article_id="a", magnitude=0.25)
    below_confidence = analysis(article_id="b", confidence=0.60)
    uncertain_type = analysis(article_id="c", event_type=EventType.UNCERTAIN)

    result = extract_risk_signals([below_magnitude, below_confidence, uncertain_type], now=NOW)

    assert result.signals == ()
    assert result.diagnostics.eligible_analyses == 0
    assert result.diagnostics.considered_analyses == 3


def test_uncertain_direction_contributes_nothing() -> None:
    record = analysis(
        direction=EventDirection.UNCERTAIN,
        negative_channels=["softening demand reduces expected unit shipments"],
    )

    result = extract_risk_signals([record], now=NOW)

    assert result.signals == ()
    assert result.diagnostics.eligible_analyses == 1


# ------------------------------------------------------------------- direction rules


def test_positive_event_without_negative_channels_yields_zero_signals() -> None:
    record = analysis(
        direction=EventDirection.POSITIVE, event_type=EventType.INVESTMENT, magnitude=1.0
    )

    result = extract_risk_signals([record], now=NOW)

    assert result.signals == ()
    assert result.diagnostics.eligible_analyses == 1


def test_positive_event_with_explicit_downside_channel_yields_prospective_signal() -> None:
    record = analysis(
        direction=EventDirection.POSITIVE,
        event_type=EventType.INVESTMENT,
        negative_channels=["increases capital committed before utilisation is proven"],
    )

    result = extract_risk_signals([record], now=NOW)

    assert len(result.signals) == 1
    signal = result.signals[0]
    assert signal.theme is RiskTheme.CAPITAL_ALLOCATION
    assert signal.basis is RiskSignalBasis.NEGATIVE_CHANNEL
    assert signal.severity == pytest.approx(0.60 * 0.60)
    assert result.diagnostics.prospective_signals == 1
    assert result.diagnostics.realized_signals == 0


def test_realized_negative_event_maps_directly_and_channels_also_contribute() -> None:
    record = analysis(
        direction=EventDirection.NEGATIVE,
        event_type=EventType.REGULATION,
        negative_channels=["softening demand reduces expected unit shipments"],
    )

    result = extract_risk_signals([record], now=NOW)

    bases = {signal.basis for signal in result.signals}
    assert bases == {RiskSignalBasis.REALIZED_EVENT, RiskSignalBasis.NEGATIVE_CHANNEL}
    realized = next(s for s in result.signals if s.basis is RiskSignalBasis.REALIZED_EVENT)
    channel = next(s for s in result.signals if s.basis is RiskSignalBasis.NEGATIVE_CHANNEL)
    assert realized.theme is RiskTheme.REGULATORY_ANTITRUST
    assert channel.theme is RiskTheme.DEMAND_SLOWDOWN
    assert realized.severity == pytest.approx(0.60)
    assert channel.severity == pytest.approx(0.60 * 0.85)


def test_negative_event_without_a_safe_mapping_contributes_only_channels() -> None:
    record = analysis(
        direction=EventDirection.NEGATIVE,
        event_type=EventType.OTHER,
        negative_channels=["a class action lawsuit seeks damages"],
    )

    result = extract_risk_signals([record], now=NOW)

    assert [signal.basis for signal in result.signals] == [RiskSignalBasis.NEGATIVE_CHANNEL]
    assert result.signals[0].theme is RiskTheme.LEGAL_LITIGATION


def test_mixed_event_uses_only_negative_channels() -> None:
    record = analysis(
        direction=EventDirection.MIXED,
        event_type=EventType.REGULATION,
        negative_channels=["a class action lawsuit seeks damages"],
    )

    result = extract_risk_signals([record], now=NOW)

    assert [signal.basis for signal in result.signals] == [RiskSignalBasis.NEGATIVE_CHANNEL]
    assert result.signals[0].severity == pytest.approx(0.60 * 0.70)


def test_neutral_event_channel_uses_the_prospective_factor() -> None:
    record = analysis(
        direction=EventDirection.NEUTRAL,
        event_type=EventType.OTHER,
        negative_channels=["a class action lawsuit seeks damages"],
    )

    result = extract_risk_signals([record], now=NOW)

    assert result.signals[0].severity == pytest.approx(0.60 * 0.60)


def test_realized_signal_outscores_an_otherwise_identical_prospective_signal() -> None:
    realized = analysis(
        article_id="realized",
        direction=EventDirection.NEGATIVE,
        event_type=EventType.LITIGATION,
    )
    prospective = analysis(
        article_id="prospective",
        direction=EventDirection.POSITIVE,
        event_type=EventType.PARTNERSHIP,
        negative_channels=["a class action lawsuit seeks damages"],
    )

    result = extract_risk_signals([realized, prospective], now=NOW)
    scores = {signal.article_id: signal.score for signal in result.signals}

    assert scores["realized"] > scores["prospective"]


def test_unmapped_channels_are_counted_but_produce_no_signal() -> None:
    record = analysis(
        direction=EventDirection.POSITIVE,
        event_type=EventType.INVESTMENT,
        negative_channels=["the company continues to operate normally"],
    )

    result = extract_risk_signals([record], now=NOW)

    assert result.signals == ()
    assert result.diagnostics.unmapped_signals == 1


# ------------------------------------------------------------------ claims / support


@pytest.mark.parametrize(
    ("status", "cited", "expected"),
    [
        (EvidenceStatus.UNSUPPORTED, True, "neutral"),
        (EvidenceStatus.UNCERTAIN, True, "neutral"),
        (EvidenceStatus.CORROBORATED, True, "corroborated"),
        (EvidenceStatus.CONTRADICTED, True, "contradicted"),
        (EvidenceStatus.CORROBORATED, False, "neutral"),
        (EvidenceStatus.CONTRADICTED, False, "neutral"),
    ],
)
def test_claim_state_precedence_and_citation_requirement(
    status: EvidenceStatus, cited: bool, expected: str
) -> None:
    assert claim_state(analysis(claims=[claim(status, cited=cited)])) == expected


def test_contradicted_takes_precedence_over_corroborated() -> None:
    record = analysis(
        claims=[
            claim(EvidenceStatus.CORROBORATED),
            ClaimAssessment(
                claim_id="claim_2",
                status=EvidenceStatus.CONTRADICTED,
                reasoning="Conflicts with supplied evidence.",
                evidence_article_ids=["evidence-2"],
                confidence=0.5,
            ),
        ]
    )

    assert claim_state(record) == "contradicted"


def test_corroborated_claim_strengthens_and_contradicted_weakens_support() -> None:
    neutral = support_for(analysis(claims=[claim(EvidenceStatus.UNSUPPORTED)]))
    corroborated = support_for(
        analysis(
            claims=[claim(EvidenceStatus.CORROBORATED)],
            # A corroborated cited claim already added the embedded bonus upstream.
            evidence_strength=0.60 + EMBEDDED_CORROBORATION_BONUS,
        )
    )
    contradicted = support_for(analysis(claims=[claim(EvidenceStatus.CONTRADICTED)]))

    assert corroborated > neutral > contradicted


def test_embedded_corroboration_bonus_is_not_counted_twice() -> None:
    """A corroborated analysis nets only the claim adjustment, not the embedded 0.15 again."""

    without = analysis(claims=[claim(EvidenceStatus.UNSUPPORTED)], evidence_strength=0.60)
    with_corroboration = analysis(
        claims=[claim(EvidenceStatus.CORROBORATED)],
        evidence_strength=0.60 + EMBEDDED_CORROBORATION_BONUS,
    )

    assert context_strength(with_corroboration) == pytest.approx(context_strength(without))
    assert support_for(with_corroboration) == pytest.approx(support_for(without) + 0.10)


def test_claim_confidence_does_not_alter_the_headline_score() -> None:
    low = analysis(claims=[claim(EvidenceStatus.CORROBORATED, confidence=0.05)])
    high = analysis(claims=[claim(EvidenceStatus.CORROBORATED, confidence=1.0)])

    assert support_for(low) == pytest.approx(support_for(high))


def test_support_uses_confidence_and_context_but_severity_does_not_use_confidence() -> None:
    low_confidence = analysis(article_id="low", confidence=0.65)
    high_confidence = analysis(article_id="high", confidence=1.0)

    result = extract_risk_signals([low_confidence, high_confidence], now=NOW)
    signals = {signal.article_id: signal for signal in result.signals}

    assert signals["high"].support > signals["low"].support
    assert signals["high"].severity == pytest.approx(signals["low"].severity)


def test_support_formula_is_the_specified_weighted_sum() -> None:
    record = analysis(confidence=0.80, evidence_strength=0.50)

    assert support_for(record) == pytest.approx(0.55 * 0.80 + 0.45 * 0.50)


# --------------------------------------------------------------------------- decay


@pytest.mark.parametrize("horizon", list(TimeHorizon))
def test_each_horizon_halves_the_signal_after_its_own_half_life(horizon: TimeHorizon) -> None:
    half_life = HALF_LIFE_DAYS[horizon]
    record = analysis(horizon=horizon, published_at=NOW - timedelta(days=half_life))

    assert decay_for(record, NOW) == pytest.approx(0.5)


def test_a_fresh_analysis_has_no_decay() -> None:
    assert decay_for(analysis(published_at=NOW), NOW) == pytest.approx(1.0)


def test_future_publication_timestamp_clamps_age_to_zero() -> None:
    future = analysis(published_at=NOW + timedelta(days=30))

    assert decay_for(future, NOW) == pytest.approx(1.0)


def test_durable_horizon_decays_more_slowly_than_a_transient_one() -> None:
    age = timedelta(days=30)
    transient = analysis(horizon=TimeHorizon.DAYS, published_at=NOW - age)
    durable = analysis(horizon=TimeHorizon.LONG_TERM, published_at=NOW - age)

    assert decay_for(durable, NOW) > decay_for(transient, NOW)


def test_decay_ignores_analysis_created_at() -> None:
    record = analysis(published_at=NOW - timedelta(days=120), horizon=TimeHorizon.MONTHS)
    restamped = record.model_copy(update={"analysis_created_at": NOW})

    assert decay_for(restamped, NOW) == pytest.approx(decay_for(record, NOW))


def test_signal_score_is_severity_times_support_times_decay() -> None:
    record = analysis(
        direction=EventDirection.NEGATIVE,
        event_type=EventType.LITIGATION,
        horizon=TimeHorizon.MONTHS,
        published_at=NOW - timedelta(days=120),
    )

    signal = extract_risk_signals([record], now=NOW).signals[0]

    assert signal.score == pytest.approx(signal.severity * signal.support * signal.decay)
    assert 0.0 <= signal.score <= 1.0


def test_incompatible_ranked_risk_payload_is_rejected_rather_than_repaired() -> None:
    """The dashboard boundary drops records it cannot interpret instead of inventing fields."""

    from marketsentinel.dashboard_risks import compatible_top_risks, prepare_top_risk_rows

    valid = {
        "theme": "capital_allocation",
        "concern_index": 41,
        "band": "Moderate",
        "summary": "increases capital committed before utilisation is proven",
        "primary_article_id": "article-1",
        "primary_article_url": "https://example.com/article-1",
        "primary_publisher": "Reuters",
        "first_evidenced_at": "2026-08-15T09:00:00Z",
        "latest_published_at": "2026-08-19T12:00:00Z",
    }

    assert len(compatible_top_risks([valid])) == 1
    assert compatible_top_risks([{**valid, "band": "Catastrophic"}]) == []
    assert compatible_top_risks([{**valid, "concern_index": 140}]) == []
    assert compatible_top_risks([{"theme": "capital_allocation"}]) == []
    assert prepare_top_risk_rows(compatible_top_risks([valid]))[0].rank == 1


def test_realized_regulation_event_describing_a_cyber_incident_is_rethemed_end_to_end() -> None:
    """The observed upstream misclassification must not survive into the extracted signal."""

    record = analysis(
        direction=EventDirection.NEGATIVE,
        event_type=EventType.REGULATION,
        summary=(
            "Hackers are exploiting Microsoft Teams in a ransomware campaign, "
            "posing as fake IT support."
        ),
        negative_channels=["Increased regulatory scrutiny regarding cybersecurity measures."],
    )

    signals = extract_risk_signals([record], now=NOW).signals
    by_basis = {signal.basis: signal for signal in signals}

    assert by_basis[RiskSignalBasis.REALIZED_EVENT].theme is RiskTheme.CYBERSECURITY
    assert by_basis[RiskSignalBasis.NEGATIVE_CHANNEL].theme is RiskTheme.REGULATORY_ANTITRUST


def test_top_risk_summary_is_truncated_for_display_only() -> None:
    from marketsentinel.dashboard_risks import MAX_SUMMARY_CHARACTERS, display_summary

    short = "increases capital committed before utilisation is proven"
    long_summary = "a" * (MAX_SUMMARY_CHARACTERS + 40)

    assert display_summary(short) == short
    assert display_summary("b" * MAX_SUMMARY_CHARACTERS) == "b" * MAX_SUMMARY_CHARACTERS
    truncated = display_summary(long_summary)
    assert truncated.endswith("…")
    assert len(truncated) == MAX_SUMMARY_CHARACTERS + 1
