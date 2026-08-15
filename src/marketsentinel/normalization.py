"""Article normalization, relevance scoring, and deterministic deduplication."""

import hashlib
import re
from collections.abc import Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from marketsentinel.domain import Article, Constituent

_NON_WORD = re.compile(r"[^a-z0-9$]+")
_SPACE = re.compile(r"\s+")
_COMPANY_SUFFIXES = re.compile(
    r"\b(?:plc|incorporated|inc|corp(?:oration)?|company|co|limited|ltd|group|holdings?)\b",
    re.IGNORECASE,
)
_TRACKING_PARAMETERS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
}


def normalize_text(value: str) -> str:
    normalized = _NON_WORD.sub(" ", value.casefold())
    return _SPACE.sub(" ", normalized).strip()


def normalize_url(value: str) -> str:
    parts = urlsplit(value.strip())
    query = [
        (key, item)
        for key, item in parse_qsl(parts.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_") and key.casefold() not in _TRACKING_PARAMETERS
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(
        (parts.scheme.casefold(), parts.netloc.casefold(), path, urlencode(query), "")
    )


def article_fingerprint(title: str, ticker: str | None = None) -> str:
    """Fingerprint a normalized story within an optional ticker context."""

    normalized = normalize_text(title)
    identity = f"{ticker.casefold()}|{normalized}" if ticker else normalized
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def company_aliases(constituent: Constituent) -> set[str]:
    names = {normalize_text(constituent.name)}
    names.update(normalize_text(alias) for alias in constituent.aliases)
    stripped = normalize_text(_COMPANY_SUFFIXES.sub(" ", constituent.name))
    if len(stripped) >= 4:
        names.add(stripped)
    return {name for name in names if name}


def relevance_score(title: str, constituent: Constituent) -> float:
    """Return an explainable title-only relevance score, not a learned probability."""

    normalized_title = normalize_text(title)
    padded_title = f" {normalized_title} "
    score = 0.0

    for alias in company_aliases(constituent):
        if len(alias) >= 4 and f" {alias} " in padded_title:
            score = max(score, 0.85 if alias == normalize_text(constituent.name) else 0.7)

    ticker = normalize_text(constituent.symbol).replace(" ", "")
    raw_lower = title.casefold()
    if f"${ticker}" in raw_lower:
        score = max(score, 0.65)
    if len(ticker) >= 3 and re.search(rf"(?<![a-z0-9]){re.escape(ticker)}(?![a-z0-9])", raw_lower):
        score = max(score, 0.45)

    finance_terms = ("shares", "stock", "earnings", "revenue", "profit", "market")
    if score > 0 and any(term in normalized_title for term in finance_terms):
        score = min(1.0, score + 0.1)
    return score


def deduplicate_articles(articles: Iterable[Article]) -> list[Article]:
    """Keep the newest, then most relevant, record for each normalized title."""

    ordered = sorted(
        articles,
        key=lambda article: (article.published_at, article.relevance_score),
        reverse=True,
    )
    unique: dict[str, Article] = {}
    for article in ordered:
        unique.setdefault(article.fingerprint, article)
    return list(unique.values())
