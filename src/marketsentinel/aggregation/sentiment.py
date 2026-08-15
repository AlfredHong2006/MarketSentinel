"""Confidence- and relevance-weighted daily financial-news sentiment."""

import math
from collections import defaultdict
from collections.abc import Sequence
from datetime import date, timedelta

from marketsentinel.domain import DailySentiment, ScoredArticle
from marketsentinel.timeutils import ensure_utc, utc_now

_LOG_THREE = math.log(3)


def sentiment_confidence(article: ScoredArticle) -> float:
    """Return one minus normalized entropy for FinBERT's three-class distribution."""

    probabilities = (article.positive, article.negative, article.neutral)
    entropy = -sum(
        probability * math.log(probability) for probability in probabilities if probability
    )
    return max(0.0, min(1.0, 1 - entropy / _LOG_THREE))


def aggregate_daily_sentiment(
    ticker: str,
    articles: Sequence[ScoredArticle],
    half_life_hours: float = 24.0,
) -> list[DailySentiment]:
    """Aggregate genuine scored articles per calendar date without imputing missing dates.

    For each article, the weight is relevance * confidence * exp(-lambda * age), where
    lambda is ln(2) / half_life_hours. The three-observation trend weights daily scores by their
    aggregate article weights.
    """

    if half_life_hours <= 0:
        raise ValueError("half_life_hours must be positive")
    if not articles:
        return []

    computed_at = utc_now()
    decay_lambda = math.log(2) / half_life_hours
    grouped: dict[date, list[ScoredArticle]] = defaultdict(list)
    for article in articles:
        grouped[ensure_utc(article.published_at).date()].append(article)

    daily_values: dict[date, dict[str, float | int]] = {}
    for day, values in grouped.items():
        weighted_rows = []
        for article in values:
            age_hours = max(
                0.0,
                (computed_at - ensure_utc(article.published_at)).total_seconds() / 3_600,
            )
            weight = (
                article.relevance_score
                * sentiment_confidence(article)
                * math.exp(-decay_lambda * age_hours)
            )
            weighted_rows.append((article, weight))

        total_weight = sum(weight for _, weight in weighted_rows)
        if total_weight <= 0:
            # A mathematically undefined daily value is omitted rather than fabricated as neutral.
            continue
        score = (
            sum(article.sentiment_score * weight for article, weight in weighted_rows)
            / total_weight
        )
        positive_share = (
            sum(article.positive * weight for article, weight in weighted_rows) / total_weight
        )
        negative_share = (
            sum(article.negative * weight for article, weight in weighted_rows) / total_weight
        )
        disagreement = math.sqrt(
            sum(
                weight * (article.sentiment_score - score) ** 2 for article, weight in weighted_rows
            )
            / total_weight
        )
        daily_values[day] = {
            "score": score,
            "article_count": len(values),
            "positive_share": positive_share,
            "negative_share": negative_share,
            "weighted_disagreement": disagreement,
            "aggregate_weight": total_weight,
        }

    results: list[DailySentiment] = []
    ordered_days = sorted(daily_values)
    for position, day in enumerate(ordered_days):
        values = daily_values[day]
        window_start = day - timedelta(days=6)
        calendar_window = [
            candidate["score"]
            for candidate_day, candidate in daily_values.items()
            if window_start <= candidate_day <= day
        ]
        trend_days = ordered_days[max(0, position - 2) : position + 1]
        trend_weight = sum(float(daily_values[item]["aggregate_weight"]) for item in trend_days)
        trend_3 = (
            sum(
                float(daily_values[item]["score"]) * float(daily_values[item]["aggregate_weight"])
                for item in trend_days
            )
            / trend_weight
        )
        results.append(
            DailySentiment(
                ticker=ticker,
                date=day,
                score=float(values["score"]),
                moving_average_7d=sum(float(score) for score in calendar_window)
                / len(calendar_window),
                trend_3=trend_3,
                article_count=int(values["article_count"]),
                positive_share=float(values["positive_share"]),
                negative_share=float(values["negative_share"]),
                weighted_disagreement=float(values["weighted_disagreement"]),
                aggregate_weight=float(values["aggregate_weight"]),
                computed_at=computed_at,
            )
        )
    return results
