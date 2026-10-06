"""Article polarity, dedup, session signals, regimes, and outcome-blind threshold selection."""

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from marketsentinel.article_sources import source_organization
from marketsentinel.domain import ScoredArticle
from marketsentinel.market_reaction.calendars import SessionCalendar
from marketsentinel.market_reaction.models import (
    MIN_DISTINCT_SOURCES,
    THRESHOLD_DECIMALS,
    THRESHOLD_FLOOR,
    THRESHOLD_NEGATIVE_QUANTILE,
    THRESHOLD_POSITIVE_QUANTILE,
    ReactionArticle,
    Regime,
    RegimeThresholds,
    SessionSignal,
    ThresholdSelection,
    TimestampQuality,
    TimingClass,
)
from marketsentinel.normalization import normalize_text, normalize_url

# Stored FinBERT probabilities are rounded, so an exact unit sum is not required.
_PROBABILITY_SUM_TOLERANCE = 0.02
# Google News RSS titles end "Headline - Publisher"; other feeds use a pipe or a dash variant.
_PUBLISHER_SEPARATORS = (" - ", " | ", " – ", " — ")
_MIDNIGHT = time(0, 0)
_GOOGLE_NEWS_STAMP_ZONE = ZoneInfo("America/Los_Angeles")


def article_polarity(p_positive: float, p_negative: float) -> float:
    return p_positive - p_negative


def has_valid_sentiment(article: ReactionArticle) -> bool:
    values = [article.p_positive, article.p_negative]
    if article.p_neutral is not None:
        values.append(article.p_neutral)
    if any(value is None or not math.isfinite(value) or not 0 <= value <= 1 for value in values):
        return False
    total = sum(values)
    if article.p_neutral is not None:
        return abs(total - 1) <= _PROBABILITY_SUM_TOLERANCE
    return total <= 1 + _PROBABILITY_SUM_TOLERANCE


def timestamp_quality(article: ReactionArticle) -> TimestampQuality:
    if article.published_at is not None and article.published_at.tzinfo is not None:
        return TimestampQuality.FULL
    if article.published_date is not None:
        return TimestampQuality.DATE_ONLY
    if article.published_at is not None:
        # A naive instant has no trustworthy time-of-day; only its date is usable.
        return TimestampQuality.DATE_ONLY
    return TimestampQuality.UNUSABLE


def normalize_title(title: str, source: str | None = None) -> str:
    """Versioned (`mr-title-v1`) deterministic title key.

    A trailing publisher suffix is stripped only when it equals the article's own source, which is
    the one case where it is safely identifiable; then case, punctuation, and whitespace collapse.
    """

    stripped = title.strip()
    source_key = normalize_text(source) if source else ""
    if source_key:
        for separator in _PUBLISHER_SEPARATORS:
            head, found, tail = stripped.rpartition(separator)
            if found and head.strip() and normalize_text(tail) == source_key:
                stripped = head
                break
    return normalize_text(stripped)


def split_publication_stamp(published_at: datetime) -> tuple[datetime | None, date | None]:
    """Separate a trustworthy instant from a provider's date-only placeholder.

    Two stored stamps mean "a date, not a time": exactly 00:00:00 UTC, and exactly 00:00:00
    America/Los_Angeles, which the Google News historical-range feed writes (07:00 UTC in summer,
    08:00 UTC in winter, hence the zone-aware check). The error is one-sided by construction: a
    genuine article published at one of those instants is delayed by at most a session and can
    never move into an earlier one.
    """

    if published_at.tzinfo is None:
        return None, published_at.date()
    instant = published_at.astimezone(UTC)
    if instant.time() == _MIDNIGHT:
        return None, instant.date()
    pacific = instant.astimezone(_GOOGLE_NEWS_STAMP_ZONE)
    if pacific.time() == _MIDNIGHT:
        return None, pacific.date()
    return instant, None


def from_scored_article(article: ScoredArticle) -> ReactionArticle:
    """Adapt a stored scored article, downgrading placeholder timestamps to date-only."""

    published_at, published_date = split_publication_stamp(article.published_at)
    return ReactionArticle(
        article_id=article.fingerprint,
        ticker=article.ticker,
        title=article.title,
        url=article.url,
        source=article.source,
        published_at=published_at,
        published_date=published_date,
        p_positive=article.positive,
        p_negative=article.negative,
        p_neutral=article.neutral,
        is_demo=article.is_demo,
    )


@dataclass
class SignalDiagnostics:
    input_article_count: int = 0
    wrong_ticker: int = 0
    demo: int = 0
    invalid_sentiment: int = 0
    unusable_timestamp: int = 0
    outside_calendar: int = 0
    full_timestamp: int = 0
    date_only_timestamp: int = 0
    canonical_duplicates: int = 0
    title_duplicates: int = 0
    lagged_sessions: int = 0
    eligible: int = 0


@dataclass(frozen=True)
class SessionSignals:
    signals: tuple[SessionSignal, ...]
    diagnostics: SignalDiagnostics = field(default_factory=SignalDiagnostics)


def _order_key(article: ReactionArticle) -> tuple[str, str]:
    moment = (
        article.published_at.astimezone(UTC).isoformat()
        if article.published_at is not None and article.published_at.tzinfo is not None
        else f"{article.published_date or article.published_at.date()}T99"
    )
    return (moment, article.article_id)


def build_session_signals(
    articles: Iterable[ReactionArticle], ticker: str, calendar: SessionCalendar
) -> SessionSignals:
    """Eligibility -> canonical dedup -> session assignment -> in-session title dedup -> mean.

    The kept copy of any duplicate is the earliest-published one (ties by article ID), so the
    result does not depend on input order. A session with no article yields no entry at all.
    """

    diagnostics = SignalDiagnostics()
    eligible: list[tuple[ReactionArticle, TimestampQuality]] = []
    for article in articles:
        diagnostics.input_article_count += 1
        if article.ticker.casefold() != ticker.casefold():
            diagnostics.wrong_ticker += 1
        elif article.is_demo:
            diagnostics.demo += 1
        elif not has_valid_sentiment(article):
            diagnostics.invalid_sentiment += 1
        elif (quality := timestamp_quality(article)) is TimestampQuality.UNUSABLE:
            diagnostics.unusable_timestamp += 1
        else:
            eligible.append((article, quality))
    eligible.sort(key=lambda item: _order_key(item[0]))

    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    by_session: dict[int, list[tuple[ReactionArticle, TimestampQuality]]] = {}
    for article, quality in eligible:
        canonical_url = normalize_url(article.url) if article.url else None
        if article.article_id in seen_ids or (canonical_url and canonical_url in seen_urls):
            diagnostics.canonical_duplicates += 1
            continue
        if quality is TimestampQuality.FULL:
            position = calendar.assign_instant(article.published_at)
        else:
            position = calendar.assign_date_only(
                article.published_date or article.published_at.date()
            )
        if position is None:
            diagnostics.outside_calendar += 1
            continue
        seen_ids.add(article.article_id)
        if canonical_url:
            seen_urls.add(canonical_url)
        if quality is TimestampQuality.FULL:
            diagnostics.full_timestamp += 1
        else:
            diagnostics.date_only_timestamp += 1
        by_session.setdefault(position, []).append((article, quality))

    signals: list[SessionSignal] = []
    for position in sorted(by_session):
        assigned = by_session[position]
        kept: list[tuple[ReactionArticle, TimestampQuality]] = []
        seen_titles: set[str] = set()
        for article, quality in assigned:
            title_key = normalize_title(article.title, article.source)
            if title_key and title_key in seen_titles:
                diagnostics.title_duplicates += 1
                continue
            seen_titles.add(title_key)
            kept.append((article, quality))
        diagnostics.eligible += len(kept)
        articles_kept = [item for item, _ in kept]
        # Distinct sources are counted over the deduplicated articles, so one syndicated headline
        # carried by several publishers is one voice, not several.
        sources = sorted(
            {source_organization(item.source, item.url, item.title) for item in articles_kept}
        )
        polarities = [article_polarity(item.p_positive, item.p_negative) for item in articles_kept]
        date_only = sum(quality is TimestampQuality.DATE_ONLY for _, quality in kept)
        date_only_share = date_only / len(kept)
        if date_only:
            diagnostics.lagged_sessions += 1
        signals.append(
            SessionSignal(
                session=calendar.sessions[position],
                signal=max(-1.0, min(1.0, math.fsum(polarities) / len(polarities))),
                article_count=len(kept),
                distinct_source_count=len(sources),
                assigned_article_count=len(assigned),
                date_only_share=date_only_share,
                # Exact only when every kept article carried a real publication time; one
                # date-only article is enough to make the whole session's timing untrustworthy.
                timing_class=TimingClass.LAGGED if date_only else TimingClass.EXACT,
                article_ids=tuple(item.article_id for item in articles_kept),
                sources=tuple(sources),
            )
        )
    return SessionSignals(signals=tuple(signals), diagnostics=diagnostics)


def classify_regime(
    signal: SessionSignal,
    thresholds: RegimeThresholds,
    min_distinct_sources: int = MIN_DISTINCT_SOURCES,
) -> Regime | None:
    """Apply the two asymmetric thresholds; both are stored as positive magnitudes."""

    if signal.distinct_source_count < min_distinct_sources:
        return None
    if signal.signal >= thresholds.positive:
        return Regime.CLEARLY_POSITIVE
    if signal.signal <= -thresholds.negative:
        return Regime.CLEARLY_NEGATIVE
    return None


def selection_population(
    signals: Iterable[SessionSignal], min_distinct_sources: int = MIN_DISTINCT_SOURCES
) -> list[float]:
    """Population E: pooled session signals that meet the distinct-source floor."""

    return [
        signal.signal for signal in signals if signal.distinct_source_count >= min_distinct_sources
    ]


def select_thresholds(
    signals: Sequence[SessionSignal], min_distinct_sources: int = MIN_DISTINCT_SOURCES
) -> ThresholdSelection:
    """Choose both regime thresholds from pooled session-signal marginals only.

    The signature is the leakage guard: it accepts session signals and nothing else, so no return
    outcome can reach it. Each tail is cut on its own side of the pooled distribution:

        tau_positive = max(0.20, round(Q0.85(S | E), 2))
        tau_negative = max(0.20, round(-Q0.15(S | E), 2))

    where E is the pooled signal-defined sessions meeting the distinct-source floor. Quantiles are
    linear-interpolated. A tail whose raw quantile sits inside the floor keeps the floor, which
    makes that regime rarer; it is never loosened to recover events.
    """

    pooled = list(signals)
    values = selection_population(pooled, min_distinct_sources)
    if any(not math.isfinite(value) for value in values):
        raise ValueError("session signals must be finite")
    if not values:
        return ThresholdSelection(
            population_count=0,
            pooled_session_count=len(pooled),
            positive_quantile=THRESHOLD_POSITIVE_QUANTILE,
            negative_quantile=THRESHOLD_NEGATIVE_QUANTILE,
            floor=THRESHOLD_FLOOR,
            raw_positive=None,
            raw_negative=None,
            thresholds=RegimeThresholds(negative=THRESHOLD_FLOOR, positive=THRESHOLD_FLOOR),
            positive_floor_applied=True,
            negative_floor_applied=True,
            positive_tail_share=0.0,
            negative_tail_share=0.0,
        )

    ordered = sorted(values)
    raw_positive = round(_quantile(ordered, THRESHOLD_POSITIVE_QUANTILE), THRESHOLD_DECIMALS)
    raw_negative = round(-_quantile(ordered, THRESHOLD_NEGATIVE_QUANTILE), THRESHOLD_DECIMALS)
    thresholds = RegimeThresholds(
        negative=max(THRESHOLD_FLOOR, raw_negative),
        positive=max(THRESHOLD_FLOOR, raw_positive),
    )
    return ThresholdSelection(
        population_count=len(values),
        pooled_session_count=len(pooled),
        positive_quantile=THRESHOLD_POSITIVE_QUANTILE,
        negative_quantile=THRESHOLD_NEGATIVE_QUANTILE,
        floor=THRESHOLD_FLOOR,
        raw_positive=raw_positive,
        raw_negative=raw_negative,
        thresholds=thresholds,
        positive_floor_applied=raw_positive < THRESHOLD_FLOOR,
        negative_floor_applied=raw_negative < THRESHOLD_FLOOR,
        positive_tail_share=sum(value >= thresholds.positive for value in values) / len(values),
        negative_tail_share=sum(value <= -thresholds.negative for value in values) / len(values),
    )


def _quantile(ordered: Sequence[float], q: float) -> float:
    """Linear-interpolation quantile (numpy's default), kept dependency-free and explicit."""

    position = q * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight
