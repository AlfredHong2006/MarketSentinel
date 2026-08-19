"""Pure presentation preparation for the Top Risks section."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from marketsentinel.domain import RankedRisk
from marketsentinel.risk_taxonomy import theme_label

MAX_SUMMARY_CHARACTERS = 180
EMPTY_TOP_RISKS_MESSAGE = "No material downside risks identified from currently analysed evidence."
CONCERN_INDEX_CAPTION = (
    "Concern Index ranks the downside evidence currently available for this company. "
    "It is not a probability, an expected loss, or a price prediction, and it is not "
    "comparable across companies."
)


@dataclass(frozen=True)
class RiskRow:
    """A display-ready row backed only by a compatible ranked risk."""

    rank: int
    label: str
    concern_index: int
    band: str
    summary: str
    risk: RankedRisk


def compatible_top_risks(payload: Sequence[Mapping[str, Any]]) -> list[RankedRisk]:
    """Reject incompatible stored/API records instead of inventing missing fields."""

    compatible: list[RankedRisk] = []
    for item in payload:
        try:
            compatible.append(RankedRisk.model_validate(item))
        except ValidationError:
            continue
    return compatible


def display_summary(summary: str, limit: int = MAX_SUMMARY_CHARACTERS) -> str:
    """Shorten a mechanism for the risk row. Presentation only; stored text is untouched."""

    if len(summary) <= limit:
        return summary
    return summary[:limit].rstrip() + "…"


def prepare_top_risk_rows(risks: Sequence[RankedRisk]) -> list[RiskRow]:
    """Number the already-ranked risks for display without re-ordering them."""

    return [
        RiskRow(
            rank=index,
            label=theme_label(risk.theme),
            concern_index=risk.concern_index,
            band=risk.band,
            summary=display_summary(risk.summary),
            risk=risk,
        )
        for index, risk in enumerate(risks, start=1)
    ]
