from datetime import UTC, datetime

from marketsentinel.dashboard_risks import (
    band_color,
    compatible_top_risks,
    display_summary,
    prepare_top_risk_rows,
)
from marketsentinel.domain import RankedRisk, RiskTheme


def ranked_risk(
    theme: RiskTheme = RiskTheme.EXPORT_TRADE,
    *,
    concern_index: int = 42,
    band: str = "Moderate",
    summary: str = "Government restrictions remain the strongest currently evidenced concern.",
) -> RankedRisk:
    return RankedRisk(
        theme=theme,
        concern_index=concern_index,
        band=band,
        summary=summary,
        primary_article_id="article-1",
        primary_article_url="https://example.com/article-1",
        primary_publisher="Test Wire",
        first_evidenced_at=datetime(2026, 8, 10, tzinfo=UTC),
        latest_published_at=datetime(2026, 8, 15, tzinfo=UTC),
        supporting_article_ids=["article-1", "article-2"],
        supporting_publishers=["Test Wire", "Second Wire"],
        supporting_signal_count=2,
        supporting_event_group_count=1,
    )


def test_prepare_top_risk_rows_numbers_without_reordering() -> None:
    risks = [
        ranked_risk(RiskTheme.EXPORT_TRADE, concern_index=80, band="Severe"),
        ranked_risk(RiskTheme.CYBERSECURITY, concern_index=42, band="Moderate"),
    ]

    rows = prepare_top_risk_rows(risks)

    assert [row.rank for row in rows] == [1, 2]
    assert [row.concern_index for row in rows] == [80, 42]
    assert rows[0].label != rows[1].label


def test_display_summary_truncates_long_mechanism_text() -> None:
    long_summary = "x" * 250

    truncated = display_summary(long_summary)

    assert len(truncated) == 181
    assert truncated.endswith("…")


def test_display_summary_leaves_short_text_untouched() -> None:
    assert display_summary("short mechanism") == "short mechanism"


def test_band_color_maps_each_known_band_distinctly() -> None:
    colors = {band: band_color(band) for band in ("Severe", "Elevated", "Moderate", "Watch")}

    assert len(set(colors.values())) == 4


def test_band_color_falls_back_for_an_unknown_band() -> None:
    assert band_color("Unrecognized") == band_color("Watch")


def test_prepare_top_risk_rows_carries_band_color_matching_band() -> None:
    rows = prepare_top_risk_rows([ranked_risk(band="Severe")])

    assert rows[0].band == "Severe"
    assert rows[0].band_color == band_color("Severe")


def test_incompatible_record_missing_concern_index_is_excluded_not_fabricated() -> None:
    record = ranked_risk().model_dump(mode="json")
    record.pop("concern_index")

    assert compatible_top_risks([record]) == []
