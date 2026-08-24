"""Semantics of the historical priority signal and the bounded priority bonus.

The signal exists to raise recall of reports of disclosures and corporate actions under a bounded
analysis budget. It is deliberately not a materiality judgement: these tests pin what the
deterministic rule treats as the report of an event, what it treats as commentary or price
reaction, and that neither ever overrides source quality or a diversity cap.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import make_article

from marketsentinel.analysis_candidates import (
    describes_market_move,
    has_financial_disclosure_signal,
    has_priority_signal,
    reads_as_commentary,
    select_analysis_candidates,
    select_analysis_candidates_with_diagnostics,
)
from marketsentinel.domain import CompanyReference

NOW = datetime(2026, 8, 18, 12, tzinfo=UTC)
ACME = CompanyReference(symbol="ACME", name="Acme Corporation")
NVDA = CompanyReference(symbol="NVDA", name="Nvidia")
GOLD = json.loads(
    (Path(__file__).parent / "fixtures" / "nvda_selector_gold.json").read_text(encoding="utf-8")
)


def article(title, *, hours_old=1, source="Reuters", relevance=0.9):
    value = make_article(
        title=title,
        published_at=NOW - timedelta(hours=hours_old),
        source=source,
        url=f"https://example.com/{abs(hash((title, source)))}",
    )
    return value.model_copy(update={"ticker": "ACME", "relevance_score": relevance})


@pytest.mark.parametrize("case", GOLD["positives"], ids=lambda case: case["family"])
def test_gold_reports_of_disclosures_and_actions_are_prioritised(case) -> None:
    assert has_priority_signal(case["title"]), case["title"]


@pytest.mark.parametrize("case", GOLD["negatives"], ids=lambda case: case["reason"])
def test_gold_editorial_previews_and_price_reactions_are_not_prioritised(case) -> None:
    assert not has_priority_signal(case["title"]), case["title"]


@pytest.mark.parametrize("case", GOLD["known_false_positives"], ids=lambda case: case["reason"])
def test_gold_known_false_positives_stay_documented(case) -> None:
    """These match today. The fixture records them so a change of behaviour is visible."""

    assert has_priority_signal(case["title"]), case["title"]


@pytest.mark.parametrize(
    "case", GOLD["subject_disposal_positives"], ids=lambda case: case["title"][:40]
)
def test_subject_disposal_titles_are_excluded_without_a_known_subject(case) -> None:
    """The base signal cannot tell a subject's own disposal from an outside holder's exit."""

    assert not has_priority_signal(case["title"]), case["title"]


def test_the_selector_restores_priority_when_the_subject_itself_is_the_seller() -> None:
    subject_sale = article(
        GOLD["subject_disposal_positives"][0]["title"],
        hours_old=5,
        source="Financial Times",
    )
    outside_sale = article(
        "SoftBank sells its entire stake in Nvidia for $5.83 billion",
        hours_old=1,
        source="CNBC",
    )
    ordinary = article("Nvidia expands a services agreement", hours_old=2, source="Reuters")

    result = select_analysis_candidates(
        [outside_sale, ordinary, subject_sale],
        NOW,
        3,
        subject_company=NVDA,
        prioritize_disclosures=True,
    )

    assert result == [subject_sale, outside_sale, ordinary], (
        "the subject's own disposal keeps priority despite being the oldest article; "
        "an outside holder's exit of the same shape never does"
    )


@pytest.mark.parametrize(
    ("title", "family"),
    [
        ("Globex PLC Announces Financial Results for Fourth Quarter and Fiscal 2026", "disclosure"),
        ("Initech to Invest $2 Billion in Umbrella Corporation", "investment"),
        ("Wonka Industries takes $400 million stake in Soylent Corp", "investment"),
        ("Stark Industries to acquire robotics maker Cyberdyne", "acquisition"),
        ("Globex buying analytics startup Massive Dynamic for $1.2 billion", "acquisition"),
        ("Initech to raise $3 billion in first corporate bond sale since 2019", "financing"),
        ("Regulators ban Globex chip exports to two markets", "regulatory_action"),
        ("Authorities clear Initech sales of advanced processors", "regulatory_action"),
        ("Stark Industries sued by Hammer Industries over patent licensing", "litigation"),
        ("Two executives charged with smuggling Globex components", "litigation"),
    ],
)
def test_action_families_are_company_agnostic(title: str, family: str) -> None:
    assert has_priority_signal(title), f"{family}: {title}"


@pytest.mark.parametrize(
    "title",
    [
        "Why Globex took a stake in Initech",
        "How Initech's $2 billion acquisition could reshape the market",
        "Opinion: regulators should ban these chip exports",
        "Analysis: what Globex's bond sale means for investors",
        "Globex acquisition explained",
        "Umbrella Corporation earnings preview: what to expect",
    ],
)
def test_commentary_about_an_action_is_not_a_report_of_one(title: str) -> None:
    assert reads_as_commentary(title)
    assert not has_priority_signal(title)


@pytest.mark.parametrize(
    "title",
    [
        "Globex shares surge 8% after it acquires Initech",
        "Initech stock climbs 4% as regulators clear its chip exports",
        "Soylent stock jumps 6% as Globex invests $2 billion",
        "Stark shares fall after the company raises $1 billion",
        "Wonka stock closes at record, pushing market cap past $1 trillion",
    ],
)
def test_price_reaction_framing_is_not_a_report_of_an_action(title: str) -> None:
    assert describes_market_move(title)
    assert not has_priority_signal(title)


def test_scheduling_notices_remain_outside_the_signal() -> None:
    assert not has_priority_signal(
        "Globex Sets Conference Call for Third-Quarter Financial Results"
    )
    assert not has_financial_disclosure_signal(
        "Globex Sets Conference Call for Third-Quarter Financial Results"
    )


def test_priority_never_lifts_a_lower_source_tier_above_a_higher_one() -> None:
    commentary = article(
        "Globex to acquire Initech in $3 billion deal",
        source="The Motley Fool",
    )
    wire = article("Globex expands a services agreement", hours_old=3, source="Reuters")
    official = article("Globex technical deep dive", hours_old=4, source="NVIDIA Blog")

    result = select_analysis_candidates(
        [commentary, wire, official],
        NOW,
        3,
        subject_company=ACME,
        prioritize_disclosures=True,
        priority_bonus_limit=2,
    )

    assert has_priority_signal(commentary.title), "fixture must carry the signal"
    assert result.index(official) < result.index(commentary)
    assert result.index(wire) < result.index(commentary)


def _priority_dense_pool(count: int = 8) -> list:
    return [
        article(
            f"Globex to acquire uniquetarget{index} in a multi-billion deal",
            hours_old=index + 1,
            source=("Reuters", "Bloomberg", "Financial Times", "CNBC")[index % 4],
        )
        for index in range(count)
    ]


def test_priority_bonus_defaults_to_off_so_existing_budgets_are_unchanged() -> None:
    pool = _priority_dense_pool()

    baseline = select_analysis_candidates(pool, NOW, 4, subject_company=ACME)
    flag_only = select_analysis_candidates(
        pool, NOW, 4, subject_company=ACME, prioritize_disclosures=True
    )
    explicit_zero = select_analysis_candidates(
        pool, NOW, 4, subject_company=ACME, prioritize_disclosures=True, priority_bonus_limit=0
    )

    assert len(baseline) == 4
    assert flag_only == explicit_zero
    assert len(flag_only) == 4, "the flag alone never grants an extra slot"


def test_a_priority_dense_period_gains_at_most_the_bonus() -> None:
    result = select_analysis_candidates_with_diagnostics(
        _priority_dense_pool(),
        NOW,
        4,
        subject_company=ACME,
        prioritize_disclosures=True,
        priority_bonus_limit=2,
    )

    assert len(result.candidates) == 6
    assert result.diagnostics.selected == 6


def test_a_quiet_period_stays_at_the_base_budget() -> None:
    quiet = [
        article(
            f"Globex publishes an engineering note uniqueitem{index}",
            hours_old=index + 1,
            source=("Reuters", "Bloomberg", "General Daily")[index % 3],
        )
        for index in range(8)
    ]

    result = select_analysis_candidates(
        quiet, NOW, 4, subject_company=ACME, prioritize_disclosures=True, priority_bonus_limit=2
    )

    assert not any(has_priority_signal(item.title) for item in quiet)
    assert len(result) == 4, "no priority article means no additional spend"


def test_the_bonus_pass_still_honours_the_publisher_cap() -> None:
    pool = [
        article(
            f"Globex to acquire uniquetarget{index} in a multi-billion deal",
            hours_old=index + 1,
            source="Reuters",
        )
        for index in range(8)
    ]

    result = select_analysis_candidates(
        pool, NOW, 4, subject_company=ACME, prioritize_disclosures=True, priority_bonus_limit=2
    )

    assert len(result) == 3, "one publisher may not fill base and bonus slots alike"


def test_the_bonus_pass_still_honours_the_official_company_cap() -> None:
    pool = [
        article(
            f"Globex to acquire uniquetarget{index} in a multi-billion deal",
            hours_old=index + 1,
            source=("NVIDIA Blog", "NVIDIA Newsroom", "NVIDIA Developer")[index % 3],
        )
        for index in range(8)
    ]

    result = select_analysis_candidates(
        pool, NOW, 4, subject_company=ACME, prioritize_disclosures=True, priority_bonus_limit=2
    )

    assert len(result) == 3, "the official-company family cap binds across both passes"


def test_the_bonus_pass_only_admits_priority_articles() -> None:
    priority = article("Globex to acquire Initech in a $3 billion deal", source="Reuters")
    ordinary = [
        article(
            f"Globex publishes an engineering note uniqueitem{index}",
            hours_old=index + 2,
            source=("Bloomberg", "Financial Times", "CNBC", "General Daily")[index % 4],
        )
        for index in range(6)
    ]

    result = select_analysis_candidates(
        [priority, *ordinary],
        NOW,
        4,
        subject_company=ACME,
        prioritize_disclosures=True,
        priority_bonus_limit=2,
    )

    assert priority in result
    assert len(result) == 4, "ordinary articles never fill a bonus slot"


def test_a_negative_priority_bonus_is_rejected() -> None:
    with pytest.raises(ValueError, match="priority bonus"):
        select_analysis_candidates_with_diagnostics(
            [], NOW, 4, subject_company=ACME, priority_bonus_limit=-1
        )


def test_the_combined_budget_stays_inside_the_selector_ceiling() -> None:
    with pytest.raises(ValueError, match="must not exceed 40"):
        select_analysis_candidates_with_diagnostics(
            [], NOW, 39, subject_company=ACME, priority_bonus_limit=2
        )
