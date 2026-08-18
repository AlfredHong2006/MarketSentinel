"""Shared semantic policy for meaningful company events."""

from typing import SupportsFloat

from marketsentinel.domain import EventExtraction, EventType

MIN_MEANINGFUL_EVENT_MAGNITUDE = 0.30
MIN_MEANINGFUL_EVENT_CONFIDENCE = 0.65


def is_meaningful_event(
    event: EventExtraction,
    *,
    is_external_institutional_holding: bool = False,
) -> bool:
    """Whether an extracted event is concrete, material, and sufficiently reliable.

    This is deliberately a product-semantic rule rather than a price prediction.
    A separately detected third-party ownership change is not a subject-company event.
    """

    return is_meaningful_event_values(
        event.event_type,
        event.magnitude,
        event.model_confidence,
        is_external_institutional_holding=is_external_institutional_holding,
    )


def is_meaningful_event_values(
    event_type: EventType | str,
    magnitude: SupportsFloat,
    extraction_confidence: SupportsFloat,
    *,
    is_external_institutional_holding: bool = False,
) -> bool:
    """Value-oriented form for compact dashboard event projections."""

    return (
        str(event_type) != EventType.UNCERTAIN.value
        and not is_external_institutional_holding
        and float(magnitude) >= MIN_MEANINGFUL_EVENT_MAGNITUDE
        and float(extraction_confidence) >= MIN_MEANINGFUL_EVENT_CONFIDENCE
    )
