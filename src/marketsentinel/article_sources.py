"""Deterministic article source metadata shared by selection and event analysis."""

import re
from urllib.parse import urlparse

from marketsentinel.domain import SourceClass

_REGULATORY_SOURCE_PATTERN = re.compile(
    r"(?<![a-z0-9])(?:sec|edgar|fca)(?![a-z0-9])"
    r"|\bcompanies\s+house\b"
    r"|\bsecurities\s+and\s+exchange\s+commission\b"
)
_OFFICIAL_COMPANY_SOURCES = {
    "apple investor",
    "apple newsroom",
    "investor relations",
    "nvidia blog",
    "nvidia developer",
    "nvidia investor",
    "nvidia newsroom",
}
_MAJOR_FINANCIAL_SOURCES = {
    "bloomberg",
    "bloomberg.com",
    "cnbc",
    "cnbc.com",
    "financial times",
    "ft.com",
    "reuters",
    "wall street journal",
    "wsj.com",
}
_INDUSTRY_SPECIALIST_SOURCES = {
    "semianalysis",
    "the register",
    "tom's hardware",
}
# Whole-word brand phrases for major financial publishers only. Google News RSS reports edition
# and imprint variants ("Thomson Reuters", "The Wall Street Journal", "CNBC TV18") and its item
# URLs are news.google.com redirects, so hostname matching is usually unavailable. These tokens
# are distinctive multi-character brands, unlike the "sec" fragment that caused earlier
# substring collisions. Official-company and industry-specialist names stay exact.
_MAJOR_FINANCIAL_BRAND_PATTERN = re.compile(
    r"(?<![a-z0-9])(?:"
    r"reuters|bloomberg|cnbc|wsj"
    r"|financial\s+times"
    r"|wall\s+street\s+journal"
    r")(?![a-z0-9])"
)


def _is_domain(hostname: str, *domains: str) -> bool:
    return any(hostname == domain or hostname.endswith(f".{domain}") for domain in domains)


def classify_article_source(
    source: str,
    url: str | None = None,
    title: str | None = None,
) -> SourceClass:
    """Classify provenance using narrow, inspectable metadata rules."""

    value = source.casefold().strip()
    hostname = urlparse(url).hostname.casefold() if url and urlparse(url).hostname else ""
    title_value = (title or "").casefold()
    if value in _OFFICIAL_COMPANY_SOURCES or _is_domain(hostname, "nvidia.com", "apple.com"):
        return SourceClass.OFFICIAL_COMPANY
    if _REGULATORY_SOURCE_PATTERN.search(value) or _is_domain(
        hostname, "sec.gov", "fca.org", "fca.org.uk"
    ):
        return SourceClass.REGULATORY_OR_FILING
    if (
        value in _MAJOR_FINANCIAL_SOURCES
        or _MAJOR_FINANCIAL_BRAND_PATTERN.search(value) is not None
        or _is_domain(hostname, "reuters.com", "bloomberg.com", "ft.com", "wsj.com", "cnbc.com")
    ):
        return SourceClass.MAJOR_FINANCIAL_NEWS
    if value in _INDUSTRY_SPECIALIST_SOURCES or _is_domain(
        hostname, "theregister.com", "tomshardware.com", "semianalysis.com"
    ):
        return SourceClass.INDUSTRY_SPECIALIST
    if any(
        item in value or item in title_value
        for item in (
            "opinion",
            "motley fool",
            "seeking alpha",
            "investorplace",
            "prediction:",
            "price prediction",
        )
    ):
        return SourceClass.COMMENTARY_OR_OPINION
    if value and value != "unknown source":
        return SourceClass.GENERAL_NEWS
    return SourceClass.UNKNOWN
