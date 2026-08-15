"""Recency-weighted daily sentiment and calendar-window moving averages."""

from collections import defaultdict
from collections.abc import Sequence
from datetime import timedelta

from marketsentinel.domain import DailySentiment, ScoredArticle
from marketsentinel.timeutils import ensure_utc, utc_now


def aggregate_daily_sentiment(
    ticker: str,
    articles: Sequence[ScoredArticle],
    half_life_hours: float = 24.0,
) -> list[DailySentiment]:
    """Aggregate score=(P(positive)-P(negative)) with exponential recency weights."""

    if half_life_hours <= 0:
        raise ValueError("half_life_hours must be positive")
    if not articles:
        return []

    computed_at = utc_now()
    grouped: dict[object, list[ScoredArticle]] = defaultdict(list)
    for article in articles:
        grouped[ensure_utc(article.published_at).date()].append(article)

    daily_scores: dict[object, tuple[float, int]] = {}
    for day, values in grouped.items():
        weights = [
            0.5
            ** (
                max(0.0, (computed_at - ensure_utc(article.published_at)).total_seconds())
                / 3600
                / half_life_hours
            )
            for article in values
        ]
        weight_sum = sum(weights)
        score = (
            sum(
                article.sentiment_score * weight
                for article, weight in zip(values, weights, strict=True)
            )
            / weight_sum
        )
        daily_scores[day] = (score, len(values))

    results: list[DailySentiment] = []
    for day in sorted(daily_scores):
        window_start = day - timedelta(days=6)
        window_values = [
            score
            for candidate_day, (score, _) in daily_scores.items()
            if window_start <= candidate_day <= day
        ]
        score, count = daily_scores[day]
        results.append(
            DailySentiment(
                ticker=ticker,
                date=day,
                score=score,
                moving_average_7d=sum(window_values) / len(window_values),
                article_count=count,
                computed_at=computed_at,
            )
        )
    return results
