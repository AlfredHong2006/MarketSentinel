from datetime import UTC, datetime

from conftest import make_article

from marketsentinel.domain import DailySentiment
from marketsentinel.sentiment.finbert import StaticSentimentAnalyzer
from marketsentinel.storage.sqlite import SQLiteRepository


def test_sqlite_round_trip_preserves_typed_article_and_aggregate(writable_tmp_path) -> None:
    repository = SQLiteRepository(writable_tmp_path / "market.db")
    repository.initialize()
    article = make_article()
    scored = StaticSentimentAnalyzer().score([article])[0]
    daily = DailySentiment(
        ticker="ACME",
        date=article.published_at.date(),
        score=scored.sentiment_score,
        moving_average_7d=scored.sentiment_score,
        article_count=1,
        computed_at=datetime.now(UTC),
    )

    repository.upsert_articles([article])
    repository.upsert_sentiments([scored])
    repository.upsert_daily_sentiment([daily])

    stored_articles = repository.list_scored_articles("ACME")
    stored_daily = repository.list_daily_sentiment("ACME")
    assert stored_articles[0].fingerprint == article.fingerprint
    assert stored_articles[0].sentiment_score == scored.sentiment_score
    assert stored_daily == [daily]
