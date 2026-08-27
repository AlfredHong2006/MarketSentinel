"""What the Key Developments section shows, and where it sits in the page.

The section's job is to display a judgement made elsewhere. These tests pin the display side of
that contract -- the funnel caption, the honest empty state, one row per development with its
sources intact -- and that the dashboard makes none of the judgement itself.
"""

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from marketsentinel.domain import (
    ArticleEvidenceReference,
    ClaimAssessment,
    CompanyIntelligenceEvent,
    CompanyReference,
    EventDirection,
    EventExtraction,
    EventType,
    EvidenceStatus,
    SourceClass,
    TimeHorizon,
)
from marketsentinel.materiality import (
    EMPTY_KEY_DEVELOPMENTS_MESSAGE,
    MAX_KEY_DEVELOPMENTS,
    MaterialityDiagnostics,
    key_developments_caption,
    prepare_key_developments,
)

DASHBOARD_SOURCE = Path("src/marketsentinel/dashboard.py")
NOW = datetime(2026, 8, 20, 12, tzinfo=UTC)
ACME = CompanyReference(symbol="ACME", name="Acme Corporation")
GROUPED_TITLE = "Acme to provide up to $105 billion guarantee for the Ohio data center"


class FakeColumn:
    def __init__(self, sink: list[tuple[str, Any]]) -> None:
        self.sink = sink

    def metric(self, label: str, value: Any, help: str | None = None) -> None:
        self.sink.append(("metric", (label, value, help)))


class FakeBlock:
    """A container or expander: records that it opened, and what it was labelled."""

    def __init__(self, sink: list[tuple[str, Any]], kind: str, label: Any = None) -> None:
        self.sink = sink
        self.kind = kind
        self.label = label

    def __enter__(self) -> "FakeBlock":
        self.sink.append((self.kind, self.label))
        return self

    def __exit__(self, *_: object) -> bool:
        return False


class FakeStreamlit:
    """Records what a render function asked to be shown, in order."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def __getattr__(self, name: str):
        def record(value: Any = None, *_: object, **__: object) -> None:
            self.calls.append((name, value))

        return record

    def container(self, **_: object) -> FakeBlock:
        return FakeBlock(self.calls, "container")

    def expander(self, label: str, **_: object) -> FakeBlock:
        return FakeBlock(self.calls, "expander", label)

    def columns(self, count: int) -> list[FakeColumn]:
        self.calls.append(("columns", count))
        return [FakeColumn(self.calls) for _ in range(count)]

    def texts(self, *kinds: str) -> list[str]:
        return [str(value) for name, value in self.calls if name in kinds]


@pytest.fixture
def dashboard(monkeypatch):
    """The real dashboard module with a recording stand-in for Streamlit."""

    import marketsentinel.dashboard as module

    fake = FakeStreamlit()
    monkeypatch.setattr(module, "st", fake)
    monkeypatch.setattr(module, "recorded", fake, raising=False)
    return module


def event(
    article_id: str,
    *,
    title: str = "Acme acquires rival chipmaker Bolt for $4 billion",
    event_type: EventType = EventType.ACQUISITION,
    magnitude: float = 0.55,
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
                claim_id="c1",
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
                claim_id="c2",
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


def payload(events: list[CompanyIntelligenceEvent]) -> dict[str, Any]:
    return {"intelligence_events": [item.model_dump(mode="json") for item in events]}


def diagnostics(considered: int, material: int, developments: int, rendered: int):
    return MaterialityDiagnostics(
        considered=considered,
        material=material,
        developments=developments,
        rendered=rendered,
        rejected_by_condition={},
    )


def test_the_caption_names_both_narrowings_the_list_went_through() -> None:
    """Analysed to material is the gate; material to developments is grouping. Two claims."""

    assert (
        key_developments_caption(diagnostics(125, 54, 49, 8))
        == "125 analysed → 54 material → 49 developments · showing the strongest 8"
    )
    assert (
        key_developments_caption(diagnostics(12, 3, 3, 3))
        == "12 analysed → 3 material → 3 developments"
    )
    assert (
        key_developments_caption(diagnostics(40, 0, 0, 0))
        == "40 analysed → 0 material → 0 developments"
    )


def test_a_truncated_list_says_so_rather_than_reading_as_an_absence() -> None:
    headlines = (
        "Acme acquires rival chipmaker Bolt for $4 billion",
        "Acme sued by Jamendo over training data",
        "Acme to raise $25 billion in its first bond sale in five years",
        "China bans Acme gaming chips from import",
        "Acme takes a $5 billion stake in Cypher Systems",
        "Acme wins a datacentre order from Delta Optics",
        "Acme names a new chief financial officer",
        "Regulators open an antitrust review of the Bolt purchase",
        "Acme discloses a $21bn holding in Falcon Networks",
        "Acme unveils a workstation superchip",
        "Acme signs a supply agreement with Granite Storage",
    )
    events = [
        event(f"deal-{index}", title=headline, event_type=event_type)
        for index, (headline, event_type) in enumerate(
            zip(
                headlines,
                (
                    EventType.ACQUISITION,
                    EventType.LITIGATION,
                    EventType.FINANCING,
                    EventType.REGULATION,
                    EventType.INVESTMENT,
                    EventType.CONTRACT_AWARD,
                    EventType.MANAGEMENT_CHANGE,
                    EventType.REGULATION,
                    EventType.INVESTMENT,
                    EventType.PRODUCT_LAUNCH,
                    EventType.PARTNERSHIP,
                ),
                strict=True,
            )
        )
    ]

    assert len(headlines) == MAX_KEY_DEVELOPMENTS + 3

    prepared = prepare_key_developments(events)

    assert prepared.diagnostics.developments == len(headlines)
    assert len(prepared.rows) == MAX_KEY_DEVELOPMENTS
    assert "showing the strongest 8" in key_developments_caption(prepared.diagnostics)


def test_the_section_renders_the_funnel_caption_and_one_row_for_each_development(
    dashboard,
) -> None:
    grouped = [
        event("ohio-a", title=GROUPED_TITLE, event_type=EventType.INVESTMENT),
        event(
            "ohio-b",
            title=GROUPED_TITLE,
            event_type=EventType.INVESTMENT,
            publisher="Bloomberg",
            hours_old=2,
        ),
    ]
    solo = event("groq", title="Acme buying chip startup Groq for $20 billion", magnitude=0.80)
    commentary = event("why", title="Why Acme's next quarter could disappoint")

    dashboard.render_key_developments(payload([*grouped, solo, commentary]))
    calls = dashboard.recorded.calls

    assert ("subheader", "Key Developments") in calls
    captions = dashboard.recorded.texts("caption")
    assert captions[0].startswith("4 analysed → 3 material → 2 developments")
    # One container per development, not one per report.
    assert sum(1 for name, _ in calls if name == "container") == 2
    headlines = [text for text in dashboard.recorded.texts("markdown") if text.startswith("**[")]
    assert len(headlines) == 2
    assert headlines[0].startswith("**[Acme buying chip startup Groq")


def test_a_grouped_development_reports_its_breadth_and_keeps_every_source_link(
    dashboard,
) -> None:
    """Folding three reports into one row must not lose the other two."""

    events = [
        event(GROUPED_TITLE[:6] + name, title=GROUPED_TITLE, publisher=publisher, hours_old=hours)
        for name, publisher, hours in (
            ("a", "Reuters", 0),
            ("b", "Bloomberg", 2),
            ("c", "Financial Times", 5),
        )
    ]

    dashboard.render_key_developments(payload(events))
    markdown = dashboard.recorded.texts("markdown")
    captions = dashboard.recorded.texts("caption")

    assert any("3 reports · 3 publishers" in text for text in captions)
    assert ("expander", "All 3 reports · 3 publishers") in dashboard.recorded.calls
    for publisher, name in (("Reuters", "a"), ("Bloomberg", "b"), ("Financial Times", "c")):
        assert any(
            text.startswith(f"{publisher} — [") and f"https://example.com/Acme t{name}" in text
            for text in markdown
        ), publisher


def test_a_single_report_states_its_breadth_honestly_rather_than_implying_more(
    dashboard,
) -> None:
    dashboard.render_key_developments(payload([event("solo")]))

    assert any("1 report · 1 publisher" in text for text in dashboard.recorded.texts("caption"))
    assert not any(name == "expander" for name, _ in dashboard.recorded.calls)


def test_conflicting_evidence_is_shown_on_the_development_not_used_to_hide_it(
    dashboard,
) -> None:
    disputed = event("disputed", external_publishers=("Bloomberg",), contradicted=True)

    dashboard.render_key_developments(payload([disputed]))
    calls = dashboard.recorded.calls

    assert sum(1 for name, _ in calls if name == "container") == 1
    warnings = [value for name, value in calls if name == "warning"]
    assert warnings == ["Conflicting evidence on 1 of 2 claims"]


def test_the_empty_state_says_what_was_examined_rather_than_showing_nothing(dashboard) -> None:
    commentary = [
        event("why", title="Why Acme's next quarter could disappoint"),
        event("preview", title="Acme earnings preview: what to expect on Wednesday"),
    ]

    dashboard.render_key_developments(payload(commentary))
    calls = dashboard.recorded.calls

    assert ("info", EMPTY_KEY_DEVELOPMENTS_MESSAGE) in calls
    assert ("caption", "2 analysed → 0 material → 0 developments") in calls
    assert not any(name == "container" for name, _ in calls)


def test_an_absent_or_unusable_payload_renders_the_same_empty_state(dashboard) -> None:
    dashboard.render_key_developments({})

    assert ("info", EMPTY_KEY_DEVELOPMENTS_MESSAGE) in dashboard.recorded.calls
    assert ("caption", "0 analysed → 0 material → 0 developments") in dashboard.recorded.calls


def test_key_developments_sits_between_the_market_view_and_todays_intelligence() -> None:
    """The section answers 'what happened' before the surface that ranks today's extractions."""

    order = _rendered_sections()

    assert order.index("render_key_developments") == order.index("render_current_market_view") + 1
    assert order.index("render_todays_intelligence") == order.index("render_key_developments") + 1
    assert order.count("render_key_developments") == 1


def test_the_dashboard_displays_the_materiality_verdict_without_recomputing_it() -> None:
    """Every judgement stays in one module; the page may only ask for the prepared answer."""

    imported = _imported_names("marketsentinel.materiality")

    assert imported == {
        "EMPTY_KEY_DEVELOPMENTS_MESSAGE",
        "key_developments_caption",
        "prepare_key_developments",
    }
    source = DASHBOARD_SOURCE.read_text(encoding="utf-8")
    for name in ("assess_materiality", "is_material", "group_material_events", "TIER_LABELS"):
        assert name not in source, name


def _dashboard_tree() -> ast.Module:
    return ast.parse(DASHBOARD_SOURCE.read_text(encoding="utf-8"))


def _rendered_sections() -> list[str]:
    return [
        node.func.id
        for node in ast.walk(_dashboard_tree())
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id.startswith("render_")
        and [argument for argument in node.args if getattr(argument, "id", None) == "analysis"]
    ]


def _imported_names(module: str) -> set[str]:
    return {
        alias.name
        for node in ast.walk(_dashboard_tree())
        if isinstance(node, ast.ImportFrom) and node.module == module
        for alias in node.names
    }
