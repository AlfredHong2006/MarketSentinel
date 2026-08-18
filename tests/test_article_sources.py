import pytest

from marketsentinel.article_sources import classify_article_source
from marketsentinel.domain import SourceClass


@pytest.mark.parametrize(
    "source",
    [
        "Cybersecurity Dive",
        "SecurityWeek",
        "Second Measure",
        "Insecure Systems Journal",
        "FCArena",
    ],
)
def test_regulatory_tokens_do_not_match_inside_ordinary_source_names(source: str) -> None:
    assert classify_article_source(source) is SourceClass.GENERAL_NEWS


@pytest.mark.parametrize(
    "source",
    [
        "SEC",
        "SEC filing",
        "SEC EDGAR filing",
        "EDGAR",
        "FCA notice",
        "Companies House",
        "U.S. Securities and Exchange Commission",
    ],
)
def test_legitimate_regulatory_source_names_remain_recognized(source: str) -> None:
    assert classify_article_source(source) is SourceClass.REGULATORY_OR_FILING


@pytest.mark.parametrize(
    "url",
    [
        "https://sec.gov/Archives/example",
        "https://www.sec.gov/newsroom",
        "https://www.fca.org.uk/news",
    ],
)
def test_legitimate_regulatory_domains_remain_recognized(url: str) -> None:
    assert classify_article_source("Unknown", url) is SourceClass.REGULATORY_OR_FILING


@pytest.mark.parametrize(
    "url",
    [
        "https://sec.gov.example.com/story",
        "https://notsec.gov/story",
        "https://fca.org.uk.example.com/story",
    ],
)
def test_lookalike_regulatory_domains_are_not_recognized(url: str) -> None:
    assert classify_article_source("Unknown source", url) is SourceClass.UNKNOWN


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://apple.com/newsroom", SourceClass.OFFICIAL_COMPANY),
        ("https://newsroom.apple.com/releases", SourceClass.OFFICIAL_COMPANY),
        ("https://nvidia.com/news", SourceClass.OFFICIAL_COMPANY),
        ("https://blogs.nvidia.com/post", SourceClass.OFFICIAL_COMPANY),
        ("https://ft.com/content/example", SourceClass.MAJOR_FINANCIAL_NEWS),
        ("https://www.ft.com/content/example", SourceClass.MAJOR_FINANCIAL_NEWS),
        ("https://theregister.com/story", SourceClass.INDUSTRY_SPECIALIST),
    ],
)
def test_legitimate_source_domains_use_exact_or_true_subdomain_matching(
    url: str, expected: SourceClass
) -> None:
    assert classify_article_source("Unknown source", url) is expected


@pytest.mark.parametrize(
    "url",
    [
        "https://pineapple.com/story",
        "https://notnvidia.com/story",
        "https://craft.com/story",
    ],
)
def test_source_domain_suffix_collisions_are_not_promoted(url: str) -> None:
    assert classify_article_source("Unknown source", url) is SourceClass.UNKNOWN


def test_register_publisher_name_is_exact_not_substring_based() -> None:
    assert classify_article_source("The Register") is SourceClass.INDUSTRY_SPECIALIST
    assert classify_article_source("The Register-Guard") is SourceClass.GENERAL_NEWS


# Google News RSS reports imprint and edition variants, and its item URLs are google.com
# redirects, so the publisher name is usually the only available signal.
@pytest.mark.parametrize(
    "source",
    [
        "Reuters",
        "Reuters UK",
        "Thomson Reuters",
        "Bloomberg",
        "Bloomberg Businessweek",
        "CNBC",
        "CNBC TV18",
        "Financial Times",
        "The Financial Times",
        "Wall Street Journal",
        "The Wall Street Journal",
        "WSJ",
    ],
)
def test_major_financial_brand_variants_are_recognized_without_a_publisher_url(
    source: str,
) -> None:
    google_redirect = "https://news.google.com/rss/articles/CBMiExample"
    assert classify_article_source(source, google_redirect) is SourceClass.MAJOR_FINANCIAL_NEWS


@pytest.mark.parametrize(
    "source",
    [
        "Reutersville Gazette",
        "Bloombergia Daily",
        "CNBCX",
        "WSJournal Weekly",
        "Financial Timescale Review",
        "Wall Street Journalism Review",
        "Cybersecurity Dive",
        "SecurityWeek",
    ],
)
def test_brand_lookalike_source_names_are_not_promoted(source: str) -> None:
    assert classify_article_source(source) is SourceClass.GENERAL_NEWS


@pytest.mark.parametrize(
    "url",
    [
        "https://notreuters.com/story",
        "https://reuters.com.example.net/story",
        "https://bloombergia.com/story",
    ],
)
def test_brand_lookalike_domains_are_not_promoted(url: str) -> None:
    assert classify_article_source("Unknown source", url) is SourceClass.UNKNOWN


def test_official_company_and_industry_names_stay_exact() -> None:
    """Only major-financial names accept bounded variants; the other tiers stay exact."""

    assert classify_article_source("NVIDIA Newsroom") is SourceClass.OFFICIAL_COMPANY
    assert classify_article_source("NVIDIA Newsroom Digest") is SourceClass.GENERAL_NEWS
    assert classify_article_source("SemiAnalysis") is SourceClass.INDUSTRY_SPECIALIST
    assert classify_article_source("SemiAnalysis Weekly") is SourceClass.GENERAL_NEWS
