"""Deterministic article source metadata shared by selection and event analysis."""

from urllib.parse import urlparse

from marketsentinel.domain import SourceClass


def classify_article_source(
    source: str,
    url: str | None = None,
    title: str | None = None,
) -> SourceClass:
    """Classify provenance using narrow, inspectable metadata rules."""

    value = source.casefold()
    hostname = urlparse(url).hostname.casefold() if url and urlparse(url).hostname else ""
    title_value = (title or "").casefold()
    if any(
        item in value
        for item in (
            "nvidia newsroom",
            "nvidia blog",
            "nvidia investor",
            "apple newsroom",
            "apple investor",
            "investor relations",
        )
    ) or hostname.endswith(("nvidia.com", "apple.com")):
        return SourceClass.OFFICIAL_COMPANY
    if any(
        item in value or item in hostname
        for item in ("sec", "sec.gov", "edgar", "fca", "fca.org", "companies house")
    ):
        return SourceClass.REGULATORY_OR_FILING
    if any(
        item in value or item in hostname
        for item in (
            "reuters",
            "bloomberg",
            "financial times",
            "ft.com",
            "wall street journal",
            "wsj.com",
            "cnbc",
        )
    ):
        return SourceClass.MAJOR_FINANCIAL_NEWS
    if any(item in value for item in ("the register", "tom's hardware", "semianalysis")):
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
