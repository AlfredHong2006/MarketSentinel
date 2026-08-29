"""Guarantees for the materiality gold set and the evaluator that scores against it.

Two different things are pinned here. The first is the labelled corpus itself: that it is complete,
internally consistent, and genuinely independent of the gate -- a gold set copied from the thing it
measures would report a perfect score and mean nothing. The second is the evaluator's honesty:
that it never reports an adjusted figure without the raw one beside it, that an undocumented
disagreement fails, and that a documented disagreement which no longer happens also fails.

Nothing here reads the real database. The fixture is self-contained by design, and the read-only
loading path is exercised against a repository built for the test.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from conftest import make_article

from marketsentinel.analysis_compatibility import ArticleAnalysisCompatibility
from marketsentinel.domain import (
    ArticleAnalysis,
    ArticleEvidenceReference,
    CompanyReference,
    EventDirection,
    EventExtraction,
    EventType,
    SourceClass,
    TimeHorizon,
)
from marketsentinel.event_analysis import (
    ARTICLE_ANALYSIS_SCHEMA_VERSION,
    STAGE_A_PROMPT_VERSION,
    STAGE_B_PROMPT_VERSION,
    STAGE_C_PROMPT_VERSION,
)
from marketsentinel.materiality import REJECTION_CONDITIONS, prepare_key_developments
from marketsentinel.storage.sqlite import SQLiteRepository
from scripts.evaluate_materiality import (
    DEFAULT_FIXTURE,
    DISPLAY_WINDOW_DAYS,
    GATE_FALSE_NEGATIVE,
    GATE_FALSE_POSITIVE,
    GROUPING_FALSE_NEGATIVE,
    GROUPING_RECALL_FLOOR,
    STORED_ANALYSES_LIMIT,
    analysis_record,
    detect_drift,
    evaluate,
    event_from_record,
    render_report,
    stored_records,
    worksheet,
)

# The corpus these numbers describe: 125 compatible NVDA analyses, 119 labelled on 2026-08-25 and
# six added by a later collection run and labelled on 2026-08-26. They are an in-sample regression
# pin, not a claim about the gate's accuracy anywhere else.
LABELLED_ANALYSES = 125
GOLD_MATERIAL = 52
GATE_MATERIAL = 54
GATE_DEVELOPMENTS = 49
TRUE_POSITIVES = 49
DOCUMENTED_FALSE_POSITIVES = 5
DOCUMENTED_FALSE_NEGATIVES = 3
GOLD_PAIRS = 9
RECOVERED_PAIRS = 7
REJECTION_HISTOGRAM = {
    "guard:commentary": 8,
    "guard:market_move": 0,
    "guard:price_move": 1,
    # No NVDA row is another organisation's appointment of an NVDA executive, so this guard
    # changes nothing on this corpus. It is pinned at zero precisely so that stays visible.
    "guard:third_party_appointment": 0,
    "driver:not_meaningful": 24,
    "driver:event_type": 19,
    "durability": 3,
    "evidence": 16,
}

NOW = datetime(2026, 8, 20, 12, tzinfo=UTC)


@pytest.fixture(scope="module")
def gold() -> dict[str, Any]:
    return json.loads(DEFAULT_FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def report():
    """One evaluation of the shipped fixture, without any database access."""

    return evaluate(DEFAULT_FIXTURE)


def record(
    article_id: str,
    *,
    title: str = "Acme acquires rival chipmaker Bolt for $4 billion",
    event_type: EventType = EventType.ACQUISITION,
    material: bool = True,
    group_id: str | None = None,
    reason: str = "A sized acquisition of a named company.",
    publisher: str = "Reuters",
    hours_old: int = 0,
) -> dict[str, Any]:
    """One fixture-shaped record, for exercising the evaluator's own failure modes."""

    published_at = (NOW - timedelta(hours=hours_old)).isoformat()
    return {
        "article_id": article_id,
        "title": title,
        "publisher": publisher,
        "url": f"https://example.com/{article_id}",
        "published_at": published_at,
        "source_class": SourceClass.MAJOR_FINANCIAL_NEWS.value,
        "subject_company": {"symbol": "ACME", "name": "Acme Corporation"},
        "event": {
            "event_type": event_type.value,
            "direction": EventDirection.POSITIVE.value,
            "magnitude": 0.55,
            "model_confidence": 0.85,
            "time_horizon": TimeHorizon.MONTHS.value,
        },
        "claims": [],
        "evidence_sources": [],
        "label": {
            "material": material,
            "reason": reason,
            "group_id": group_id or (article_id if material else None),
        },
    }


def fixture_file(
    path: Path,
    records: list[dict[str, Any]],
    disagreements: list[dict[str, Any]] | None = None,
) -> Path:
    target = path / "gold.json"
    target.write_text(
        json.dumps(
            {
                "description": "Synthetic gold set for evaluator tests.",
                "corpus": {"ticker": "ACME", "compatible_analyses": len(records)},
                "known_disagreements": disagreements or [],
                "records": records,
            }
        ),
        encoding="utf-8",
    )
    return target


def test_the_gold_set_is_completely_labelled_and_internally_consistent(gold) -> None:
    records = gold["records"]
    identifiers = [item["article_id"] for item in records]

    assert len(records) == LABELLED_ANALYSES
    assert len(set(identifiers)) == len(identifiers)
    # The header must not be able to drift away from the rows it describes.
    assert gold["corpus"]["compatible_analyses"] == LABELLED_ANALYSES
    assert gold["corpus"]["labelled_material"] == GOLD_MATERIAL
    for item in records:
        label = item["label"]
        assert isinstance(label["material"], bool), item["title"]
        assert label["reason"].strip(), item["title"]
        # A group id is what makes two rows one development, so it is meaningless on a row that
        # is not a development at all.
        assert bool(label["group_id"]) is label["material"], item["title"]
    assert sum(1 for item in records if item["label"]["material"]) == GOLD_MATERIAL


def test_the_gold_labels_are_not_a_copy_of_the_gate_they_measure(report) -> None:
    """A gold set that agrees everywhere would score perfectly and establish nothing."""

    assert report.gate_raw.false_positives == DOCUMENTED_FALSE_POSITIVES
    assert report.gate_raw.false_negatives == DOCUMENTED_FALSE_NEGATIVES
    assert report.grouping.missed


def test_the_gold_corpus_reproduces_the_pinned_gate_and_grouping_counts(gold) -> None:
    """The fixture stands in for the stored corpus, so it must reproduce the same verdicts."""

    events = [event_from_record(item) for item in gold["records"]]

    prepared = prepare_key_developments(events)

    assert prepared.diagnostics.considered == LABELLED_ANALYSES
    assert prepared.diagnostics.material == GATE_MATERIAL
    assert prepared.diagnostics.developments == GATE_DEVELOPMENTS
    assert dict(prepared.diagnostics.rejected_by_condition) == REJECTION_HISTOGRAM
    assert set(REJECTION_HISTOGRAM) == set(REJECTION_CONDITIONS)


def test_every_disagreement_is_explained_and_every_explanation_is_still_needed(report) -> None:
    """The primary acceptance criterion, in both directions."""

    assert report.unexplained == ()
    assert report.stale == ()
    assert report.failures == []


def test_gate_metrics_on_the_labelled_corpus_are_pinned(report) -> None:
    raw = report.gate_raw

    assert (raw.true_positives, raw.false_positives, raw.false_negatives) == (
        TRUE_POSITIVES,
        DOCUMENTED_FALSE_POSITIVES,
        DOCUMENTED_FALSE_NEGATIVES,
    )
    assert raw.true_negatives == LABELLED_ANALYSES - TRUE_POSITIVES - 8
    assert raw.precision == pytest.approx(TRUE_POSITIVES / GATE_MATERIAL)
    assert raw.recall == pytest.approx(TRUE_POSITIVES / GOLD_MATERIAL)
    # Excluding the documented rows leaves the corpus the gate is not knowingly wrong about.
    assert report.gate_adjusted.precision == pytest.approx(1.0)
    assert report.gate_adjusted.recall == pytest.approx(1.0)


def test_the_gate_beats_both_naive_baselines_on_the_same_labels(report) -> None:
    """Precision is the comparison that matters: both baselines admit the tie-flood."""

    baselines = dict(report.baselines)

    assert len(baselines) == 2
    for name, metrics in baselines.items():
        assert report.gate_raw.precision > metrics.precision, name
    assert baselines["meaningful-event floor"].recall > report.gate_raw.recall


def test_raw_grouping_recall_is_reported_even_though_it_sits_below_the_tripwire(
    report, capsys
) -> None:
    """The adjustment is an argument about accepted misses, never a reason to hide the miss."""

    raw = report.grouping.raw

    assert len(report.grouping.gold_pairs) == GOLD_PAIRS
    assert raw.true_positives == RECOVERED_PAIRS
    assert raw.recall == pytest.approx(RECOVERED_PAIRS / GOLD_PAIRS)
    assert raw.recall < GROUPING_RECALL_FLOOR
    assert raw.precision == pytest.approx(1.0)
    assert report.grouping_adjusted.recall == pytest.approx(1.0)
    # Nothing is scored outside the rows gold and gate both call developments, and here that
    # restriction drops no gold pair at all.
    assert report.grouping.excluded_gold_pairs == ()

    render_report(report)
    printed = capsys.readouterr().out

    assert "raw:      precision 1.000 | recall 0.778" in printed
    assert "adjusted: precision 1.000 | recall 1.000" in printed
    assert "below the 0.80 tripwire" in printed


def test_documented_disagreements_name_rows_that_exist_and_kinds_that_match(gold, report) -> None:
    identifiers = {item["article_id"] for item in gold["records"]}
    kinds = {item["kind"] for item in gold["known_disagreements"]}

    for entry in gold["known_disagreements"]:
        named = entry.get("article_ids", [entry.get("article_id")])
        assert set(named) <= identifiers, entry
        assert entry["reason"].strip(), entry
        # Provenance is recorded because one leak was found while labelling rather than in the
        # design that approved the others.
        assert entry["source"] in {"final-m5-plan", "census-labelling"}, entry
    assert kinds == {GATE_FALSE_POSITIVE, GATE_FALSE_NEGATIVE, GROUPING_FALSE_NEGATIVE}
    assert len(gold["known_disagreements"]) == len(report.documented)


def test_an_undocumented_disagreement_fails_the_evaluation(writable_tmp_path) -> None:
    leak = record(
        "leak",
        title="Acme unveils a new workstation",
        event_type=EventType.PRODUCT_LAUNCH,
        material=False,
    )

    result = evaluate(fixture_file(writable_tmp_path, [record("deal"), leak]))

    assert [line.split(":")[0] for line in result.unexplained] == [GATE_FALSE_POSITIVE]
    assert "leak" in result.unexplained[0]
    assert result.failures


def test_an_accepted_disagreement_that_no_longer_happens_fails_the_evaluation(
    writable_tmp_path,
) -> None:
    """Otherwise the adjusted figures quietly shrink the corpus the gate is scored on."""

    fixture = fixture_file(
        writable_tmp_path,
        [record("deal")],
        [
            {
                "kind": GATE_FALSE_POSITIVE,
                "article_id": "deal",
                "expected": "immaterial",
                "gate_says": "material",
                "reason": "Fixed since it was recorded.",
                "source": "final-m5-plan",
            }
        ],
    )

    result = evaluate(fixture)

    assert result.unexplained == ()
    assert len(result.stale) == 1
    assert "now agrees with the label" in result.stale[0]
    assert result.failures


def test_two_rows_sharing_an_article_id_are_refused_rather_than_silently_merged(
    writable_tmp_path,
) -> None:
    """Every structure the evaluator builds is keyed by article id, so a duplicate loses a row."""

    with pytest.raises(ValueError, match="share an article_id"):
        evaluate(fixture_file(writable_tmp_path, [record("deal"), record("deal")]))


def test_an_unlabelled_row_is_refused_rather_than_scored_as_immaterial(writable_tmp_path) -> None:
    unlabelled = record("blank")
    unlabelled["label"] = {"material": None, "reason": "", "group_id": None}

    with pytest.raises(ValueError, match="unlabelled"):
        evaluate(fixture_file(writable_tmp_path, [record("deal"), unlabelled]))


def test_grouping_is_scored_only_where_gold_and_gate_agree_a_row_is_a_development(
    writable_tmp_path,
) -> None:
    """A gold pair whose partner the gate rejected is reported, never silently dropped."""

    shared = "Acme wins the Ohio datacentre contract"
    rejected = record(
        "editorial",
        title=shared,
        event_type=EventType.PRODUCT_LAUNCH,
        group_id="ohio",
        reason="Same development, but the gate rejects this row at the evidence condition.",
    )
    rejected["source_class"] = SourceClass.OFFICIAL_COMPANY.value
    records = [
        record("wire", title=shared, event_type=EventType.CONTRACT_AWARD, group_id="ohio"),
        rejected,
    ]

    result = evaluate(fixture_file(writable_tmp_path, records))

    assert result.grouping.scope == 1
    assert result.grouping.gold_pairs == ()
    assert len(result.grouping.excluded_gold_pairs) == 1


def test_fixture_records_rebuild_into_typed_events_without_inventing_stored_values(gold) -> None:
    first = gold["records"][0]

    event = event_from_record(first)

    assert event.article_id == first["article_id"]
    assert event.source_reference.title == first["title"]
    assert event.source_reference.url == first["url"]
    assert event.event.event_type.value == first["event"]["event_type"]
    assert event.event.magnitude == first["event"]["magnitude"]
    assert len(event.evidence_sources) == len(first["evidence_sources"])
    # Fields the fixture deliberately does not carry are named as such rather than guessed.
    assert "no materiality rule reads" in event.event.summary
    assert event.evidence_strength == 0.0


def test_drift_names_changed_absent_and_unlabelled_rows() -> None:
    labelled = [record("kept"), record("moved"), record("gone")]
    stored = [
        dict(labelled[0]),
        {**labelled[1], "title": "Acme acquires rival chipmaker Bolt for $6 billion"},
        record("fresh"),
    ]

    drift = detect_drift(labelled, stored, now=NOW)

    assert drift.checked
    assert not drift.clean
    assert drift.changed == (("moved", ("title",)),)
    assert drift.absent == ("gone",)
    assert drift.added == ("fresh",)
    assert drift.outside_window == ()


def test_drift_notices_a_field_the_labelled_row_never_carried() -> None:
    """A projection that grows is drift too: the old rows are silent about the new field."""

    labelled = record("kept")
    stored = {**labelled, "sentiment_label": "positive"}
    del stored["label"]

    drift = detect_drift([labelled], [stored], now=NOW)

    assert drift.changed == (("kept", ("sentiment_label",)),)


def test_labels_made_against_a_superseded_analysis_version_fail_the_evaluation(
    writable_tmp_path,
) -> None:
    """A prompt bump re-analyses the corpus, so every label describes a row that no longer exists."""

    target = fixture_file(writable_tmp_path, [record("deal")])
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["corpus"]["stage_a_prompt_version"] = "event-extraction-v1"
    payload["corpus"]["schema_version"] = ARTICLE_ANALYSIS_SCHEMA_VERSION
    target.write_text(json.dumps(payload), encoding="utf-8")

    result = evaluate(target)

    assert len(result.version_mismatch) == 1
    assert "event-extraction-v1" in result.version_mismatch[0]
    assert result.failures


def test_the_shipped_labels_match_the_analysis_version_the_code_runs(report) -> None:
    assert report.version_mismatch == ()


def test_drift_separates_a_row_that_left_the_display_window_from_one_that_vanished() -> None:
    """The window sliding is not the corpus changing, and reading it as such would be alarming."""

    aged = record("aged", hours_old=24 * 400)

    drift = detect_drift([aged, record("gone")], [], now=NOW)

    assert drift.outside_window == ("aged",)
    assert drift.absent == ("gone",)


def test_a_clean_fixture_reports_no_drift(gold) -> None:
    stored = [
        {key: value for key, value in item.items() if key != "label"} for item in gold["records"]
    ]

    assert detect_drift(gold["records"], stored, now=NOW).clean


def test_the_read_only_loader_matches_the_product_loading_path(writable_tmp_path) -> None:
    """The evaluator restates the service's stored-analysis read, so it must agree with it."""

    repository = SQLiteRepository(writable_tmp_path / "market.db")
    repository.initialize()
    articles = [
        make_article(title="Acme acquires Bolt", url="https://example.com/a"),
        make_article(title="Acme lands a datacentre order", url="https://example.com/b"),
    ]
    repository.upsert_articles(articles)
    for article in articles:
        repository.store_article_analysis(stored_analysis(article.fingerprint, article.title), "v1")
    # A superseded prompt version must not reach either reader.
    repository.store_article_analysis(
        stored_analysis(articles[0].fingerprint, articles[0].title, stage_a="event-extraction-v1"),
        "v0",
    )

    loaded = stored_records(writable_tmp_path / "market.db", "ACME")
    expected = repository.list_article_analyses(
        "ACME", limit=STORED_ANALYSES_LIMIT, compatibility=compatibility()
    )

    assert len(expected) == len(articles)
    assert [item["article_id"] for item in loaded] == [item.article_id for item in expected]
    assert loaded == [analysis_record(item) for item in expected]


def test_the_loader_reads_the_same_corpus_bounds_the_service_does() -> None:
    """The bounds are restated in the evaluator, so nothing but a test keeps the two in step."""

    from marketsentinel import service

    assert STORED_ANALYSES_LIMIT == service._STORED_ANALYSES_LIMIT
    assert DISPLAY_WINDOW_DAYS == 366


def test_the_worksheet_leaves_labels_empty_and_marks_computed_columns_advisory(
    writable_tmp_path,
) -> None:
    repository = SQLiteRepository(writable_tmp_path / "market.db")
    repository.initialize()
    article = make_article(title="Acme acquires Bolt", url="https://example.com/a")
    repository.upsert_articles([article])
    repository.store_article_analysis(stored_analysis(article.fingerprint, article.title), "v1")

    skeleton = worksheet(writable_tmp_path / "market.db", "ACME")
    row = skeleton["records"][0]

    assert skeleton["corpus"]["compatible_analyses"] == 1
    assert skeleton["known_disagreements"] == []
    assert row["label"] == {"material": None, "reason": "", "group_id": None}
    assert set(row["advisory"]) == {
        "gate_material",
        "gate_failed_condition",
        "gate_tier",
        "gate_rescued_disclosure",
        "external_sources",
        "contradicted_claims",
    }
    assert "never the answer" in skeleton["description"]


def compatibility() -> ArticleAnalysisCompatibility:
    return ArticleAnalysisCompatibility(
        model_version="",
        stage_a_prompt_version=STAGE_A_PROMPT_VERSION,
        stage_b_prompt_version=STAGE_B_PROMPT_VERSION,
        stage_c_prompt_version=STAGE_C_PROMPT_VERSION,
        schema_version=ARTICLE_ANALYSIS_SCHEMA_VERSION,
    )


def stored_analysis(
    article_id: str,
    title: str,
    *,
    stage_a: str = STAGE_A_PROMPT_VERSION,
) -> ArticleAnalysis:
    return ArticleAnalysis(
        article_id=article_id,
        source_reference=ArticleEvidenceReference(
            article_id=article_id,
            title=title,
            publisher="Reuters",
            published_at=NOW,
            url=f"https://example.com/{article_id}",
        ),
        source_class=SourceClass.MAJOR_FINANCIAL_NEWS,
        subject_company=CompanyReference(symbol="ACME", name="Acme Corporation"),
        event=EventExtraction(
            event_type=EventType.ACQUISITION,
            summary="A deterministic test extraction.",
            direction=EventDirection.POSITIVE,
            magnitude=0.55,
            time_horizon=TimeHorizon.MONTHS,
            model_confidence=0.85,
        ),
        evidence_count=0,
        evidence_strength=0.5,
        evidence_fingerprint=f"evidence-{article_id[:16]}",
        model_version="test-model",
        stage_a_prompt_version=stage_a,
        stage_b_prompt_version=STAGE_B_PROMPT_VERSION,
        stage_c_prompt_version=STAGE_C_PROMPT_VERSION,
        schema_version=ARTICLE_ANALYSIS_SCHEMA_VERSION,
        analysis_created_at=NOW,
    )
