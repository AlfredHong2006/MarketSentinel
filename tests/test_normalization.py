from datetime import UTC, datetime, timedelta

from conftest import make_article, make_constituent

from marketsentinel.normalization import (
    article_fingerprint,
    deduplicate_articles,
    historical_query_terms,
    normalize_url,
    relevance_score,
)


def test_normalized_title_fingerprint_collapses_punctuation_and_case() -> None:
    assert article_fingerprint("ACME: Earnings Rise!") == article_fingerprint("acme earnings rise")


def test_normalize_url_removes_tracking_parameters() -> None:
    assert normalize_url("HTTPS://Example.com/story/?utm_source=rss&id=3#top") == (
        "https://example.com/story?id=3"
    )


def test_ticker_scoped_fingerprint_allows_one_story_to_belong_to_two_companies() -> None:
    assert article_fingerprint("Joint venture announced", "AAA") != article_fingerprint(
        "Joint venture announced", "BBB"
    )


def test_relevance_prefers_company_name_and_rejects_unrelated_title() -> None:
    constituent = make_constituent()

    assert relevance_score("Acme Corporation shares jump", constituent) >= 0.85
    assert relevance_score("Weather forecast for London", constituent) == 0


def test_relevance_rejects_clear_single_word_company_collision() -> None:
    apple = make_constituent().model_copy(update={"name": "Apple", "symbol": "AAPL"})

    assert relevance_score("Apple pie recipe for a summer picnic", apple) == 0
    assert relevance_score("Apple shares rise after earnings", apple) > 0


def test_historical_query_terms_include_controlled_aliases_and_ticker() -> None:
    constituent = make_constituent().model_copy(update={"aliases": ("Acme Holdings",)})

    assert historical_query_terms(constituent) == ("Acme Corporation", "Acme Holdings", "ACME")


def test_deduplication_keeps_newest_duplicate() -> None:
    now = datetime.now(UTC)
    older = make_article(published_at=now - timedelta(hours=3), url="https://one.example")
    newer = make_article(published_at=now - timedelta(hours=1), url="https://two.example")

    result = deduplicate_articles([older, newer])

    assert len(result) == 1
    assert result[0].url == "https://two.example"


def test_deduplication_collapses_canonical_url_and_near_duplicate_title() -> None:
    now = datetime.now(UTC)
    original = make_article(
        title="Acme Corporation shares rise after quarterly earnings",
        published_at=now - timedelta(hours=3),
        url="https://example.com/acme?utm_source=rss",
    )
    same_url = make_article(
        title="Acme Corporation shares rise after quarterly earnings update",
        published_at=now - timedelta(hours=2),
        url="https://example.com/acme",
    )
    near_title = make_article(
        title="Acme Corporation shares rise after quarterly earnings report",
        published_at=now - timedelta(hours=1),
        url="https://mirror.example/acme",
    )

    result = deduplicate_articles([original, same_url, near_title])

    assert len(result) == 1
    assert result[0].url == "https://mirror.example/acme"
