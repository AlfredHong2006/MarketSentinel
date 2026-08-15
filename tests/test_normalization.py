import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from conftest import make_article, make_constituent

from marketsentinel.normalization import (
    article_fingerprint,
    deduplicate_articles,
    deduplicate_with_diagnostics,
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


def test_normalize_url_preserves_google_rss_article_identifier() -> None:
    first = "https://news.google.com/rss/articles/opaque-one?oc=5"
    second = "https://news.google.com/rss/articles/opaque-two?oc=5"

    assert normalize_url(first) != normalize_url(second)
    assert normalize_url(first).endswith("opaque-one?oc=5")


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


def test_deduplication_collapses_canonical_url_but_preserves_similar_distinct_titles() -> None:
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

    assert len(result) == 2
    assert {item.url for item in result} == {
        "https://example.com/acme",
        "https://mirror.example/acme",
    }


def test_deduplication_fixture_covers_provider_url_and_time_identity() -> None:
    published = datetime(2026, 8, 10, 12, tzinfo=UTC)
    exact_duplicate = make_article(
        title="NVIDIA announces new data-center platform",
        published_at=published,
        url="https://news.google.com/rss/articles/google-nvda-one?oc=5",
        provider_article_id="google-nvda-one",
    )
    same_alias_result = make_article(
        title="NVIDIA announces new data-center platform",
        published_at=published,
        url="https://news.google.com/rss/articles/google-nvda-one?oc=9",
        provider_article_id="google-nvda-one",
    )
    syndicated = make_article(
        title="NVIDIA announces new data-center platform",
        published_at=published,
        url="https://publisher.example/nvda-platform?utm_source=feed",
        source="Publisher Wire",
    )
    syndicated_copy = make_article(
        title="NVIDIA announces new data-center platform update",
        published_at=published,
        url="https://publisher.example/nvda-platform",
        source="Mirror Wire",
    )
    distinct_nvidia = make_article(
        title="NVIDIA releases separate networking product",
        published_at=published + timedelta(hours=1),
        url="https://publisher.example/nvda-networking",
        source="Publisher Wire",
    )
    distinct_apple = make_article(
        title="Apple launches a new developer tool",
        published_at=published + timedelta(hours=1),
        url="https://publisher.example/apple-tools",
        source="Publisher Wire",
    )
    same_publisher_next_day = make_article(
        title="Apple launches a new developer tool",
        published_at=published + timedelta(days=1),
        url="https://publisher.example/apple-tools-day-two",
        source="Publisher Wire",
    )

    result = deduplicate_with_diagnostics(
        [
            exact_duplicate,
            same_alias_result,
            syndicated,
            syndicated_copy,
            distinct_nvidia,
            distinct_apple,
            same_publisher_next_day,
        ]
    )

    assert len(result.articles) == 5
    assert result.exact_duplicates == 1
    assert result.canonical_url_duplicates == 1
    assert result.near_title_duplicates == 0
    assert {item.title for item in result.articles} >= {
        "NVIDIA releases separate networking product",
        "Apple launches a new developer tool",
    }


def test_time_bounded_title_duplicate_does_not_merge_different_days() -> None:
    published = datetime(2026, 8, 10, 12, tzinfo=UTC)
    same_story_retry = make_article(
        published_at=published,
        url="https://one.example/a",
        source="Same Publisher",
    )
    same_title_next_day = make_article(
        published_at=published + timedelta(days=1),
        url="https://one.example/b",
        source="Same Publisher",
    )

    assert len(deduplicate_articles([same_story_retry, same_title_next_day])) == 2


def test_historical_deduplication_regression_fixture() -> None:
    fixture_path = Path(__file__).parent / "fixtures" / "historical_deduplication.json"
    records = json.loads(fixture_path.read_text(encoding="utf-8"))
    articles = [
        make_article(
            title=item["title"],
            url=item["url"],
            source=item["source"],
            published_at=datetime.fromisoformat(item["published_at"]),
            provider_article_id=item.get("provider_article_id"),
        )
        for item in records
    ]

    result = deduplicate_with_diagnostics(articles)

    assert len(result.articles) == 5
    assert result.exact_duplicates == 2
    assert result.canonical_url_duplicates == 1
    assert result.near_title_duplicates == 0
