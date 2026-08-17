"""Small, deterministic presentation helpers for Layer 2 Streamlit rendering."""

EMPTY_EVIDENCE_MESSAGE = "None identified from supplied evidence."
EMPTY_RELATED_COMPANIES_MESSAGE = "No related companies met the event-specific evidence standard."


def bullet_items(values: list[str]) -> list[str]:
    """Return clean visible bullets, never a Python-list representation."""

    return [f"• {value}" for value in values] or [EMPTY_EVIDENCE_MESSAGE]
