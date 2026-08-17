from marketsentinel.article_presentation import (
    EMPTY_EVIDENCE_MESSAGE,
    EMPTY_RELATED_COMPANIES_MESSAGE,
    bullet_items,
)


def test_empty_channels_have_clean_evidence_message() -> None:
    assert bullet_items([]) == [EMPTY_EVIDENCE_MESSAGE]


def test_channels_render_as_clean_bullets() -> None:
    assert bullet_items(["Possible demand", "Possible execution cost"]) == [
        "• Possible demand",
        "• Possible execution cost",
    ]


def test_empty_related_company_message_is_clean() -> None:
    assert (
        EMPTY_RELATED_COMPANIES_MESSAGE
        == "No related companies met the event-specific evidence standard."
    )
