"""Typed state updates for dashboard event markers."""

import json
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import ValidationError

from marketsentinel.domain import ArticleAnalysisResponse


def compatible_article_analysis_response(
    response: Mapping[str, Any],
) -> ArticleAnalysisResponse | None:
    """Parse a FastAPI JSON response while rejecting incompatible legacy analyses."""

    try:
        return ArticleAnalysisResponse.model_validate_json(json.dumps(response))
    except ValidationError:
        return None


def updated_event_markers(
    response: Mapping[str, Any],
    selected_symbol: str,
    existing_markers: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]] | None:
    """Return updated markers only for a compatible successful analysis of the selected company."""

    typed_response = compatible_article_analysis_response(response)
    if typed_response is None:
        return None
    analysis = typed_response.analysis
    if analysis is None or analysis.subject_company.symbol != selected_symbol:
        return None
    marker = analysis.to_analyzed_event().model_dump(mode="json")
    return [
        marker,
        *(item for item in existing_markers if item.get("article_id") != marker["article_id"]),
    ]


def updated_intelligence_events(
    response: Mapping[str, Any],
    selected_symbol: str,
    existing_events: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]] | None:
    """Return compatible detail cards for a fresh selected-company analysis."""

    typed_response = compatible_article_analysis_response(response)
    if typed_response is None:
        return None
    analysis = typed_response.analysis
    if analysis is None or analysis.subject_company.symbol != selected_symbol:
        return None
    event = analysis.to_company_intelligence_event().model_dump(mode="json")
    return [
        event,
        *(item for item in existing_events if item.get("article_id") != event["article_id"]),
    ]
