"""Article normalization, relevance scoring, and deterministic deduplication."""

import hashlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
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
_COLLISION_TERMS = {
    "apple": ("pie", "recipe", "orchard", "fruit", "cider"),
    "shell": ("beach", "seashell", "shellfish", "oyster"),
}
_COMPANY_CONTEXT_TERMS = (
    "shares",
    "stock",
    "earnings",
    "revenue",
    "profit",
    "market",
    "investor",
    "company",
    "chip",
    "gpu",
    "software",
)


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


_TITLE_DUPLICATE_WINDOW_SECONDS = 6 * 60 * 60


def article_fingerprint(
    title: str,
    ticker: str | None = None,
    source: str | None = None,
    published_at: datetime | None = None,
) -> str:
    """Fingerprint a stored article without conflating same-title stories on different dates."""

    normalized = normalize_text(title)
    identity = f"{ticker.casefold()}|{normalized}" if ticker else normalized
    if source is not None and published_at is not None:
        timestamp = published_at.replace(microsecond=0).isoformat()
        identity = f"{identity}|{normalize_text(source)}|{timestamp}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def company_aliases(constituent: Constituent) -> set[str]:
    names = {normalize_text(constituent.name)}
    names.update(normalize_text(alias) for alias in constituent.aliases)
    stripped = normalize_text(_COMPANY_SUFFIXES.sub(" ", constituent.name))
    if len(stripped) >= 4:
        names.add(stripped)
    return {name for name in names if name}


def historical_query_terms(constituent: Constituent) -> tuple[str, ...]:
    """Return controlled, human-reviewable terms for external historical search."""

    terms = [constituent.name, *constituent.aliases]
    stripped = _COMPANY_SUFFIXES.sub(" ", constituent.name)
    if len(normalize_text(stripped).split()) >= 2 and normalize_text(stripped) != normalize_text(
        constituent.name
    ):
        terms.append(stripped)
    if len(normalize_text(constituent.symbol)) >= 3:
        terms.append(constituent.symbol)

    deduplicated: dict[str, str] = {}
    for term in terms:
        cleaned = _SPACE.sub(" ", term).strip()
        if cleaned:
            deduplicated.setdefault(cleaned.casefold(), cleaned)
    return tuple(deduplicated.values())


def relevance_score(title: str, constituent: Constituent) -> float:
    """Return an explainable title-only relevance score, not a learned probability."""

    normalized_title = normalize_text(title)
    padded_title = f" {normalized_title} "
    score = 0.0

    collision_candidates = {
        alias
        for alias in company_aliases(constituent)
        if len(alias.split()) == 1 and alias in _COLLISION_TERMS
    }
    for candidate in collision_candidates:
        collision_terms = _COLLISION_TERMS[candidate]
        if any(term in normalized_title.split() for term in collision_terms) and not any(
            term in normalized_title for term in _COMPANY_CONTEXT_TERMS
        ):
            return 0.0

    for alias in company_aliases(constituent):
        if len(alias) >= 4 and f" {alias} " in padded_title:
            score = max(score, 0.85 if alias == normalize_text(constituent.name) else 0.7)

    ticker = normalize_text(constituent.symbol).replace(" ", "")
    raw_lower = title.casefold()
    finance_terms = _COMPANY_CONTEXT_TERMS
    has_finance_context = any(term in normalized_title for term in finance_terms)
    if f"${ticker}" in raw_lower:
        score = max(score, 0.65)
    elif (
        len(ticker) >= 3
        and has_finance_context
        and re.search(rf"(?<![a-z0-9]){re.escape(ticker)}(?![a-z0-9])", raw_lower)
    ):
        score = max(score, 0.5)

    if score > 0 and has_finance_context:
        score = min(1.0, score + 0.1)
    return score


def near_duplicate_titles(first: Article, second: Article) -> bool:
    """Match only identical normalized titles from one publisher in a six-hour window.

    This intentionally is not fuzzy text matching. Similar financial headlines can describe
    separate events, so title similarity alone is never a deduplication key.
    """

    return (
        normalize_text(first.title) == normalize_text(second.title)
        and normalize_text(first.source) == normalize_text(second.source)
        and abs((first.published_at - second.published_at).total_seconds())
        <= _TITLE_DUPLICATE_WINDOW_SECONDS
    )


@dataclass(frozen=True)
class DeduplicationResult:
    articles: list[Article]
    exact_duplicates: int = 0
    canonical_url_duplicates: int = 0
    near_title_duplicates: int = 0


def deduplicate_with_diagnostics(articles: Sequence[Article]) -> DeduplicationResult:
    """Deduplicate with stable provider ID, canonical URL, then constrained title identity."""

    ordered = sorted(
        articles,
        key=lambda article: (
            article.published_at,
            article.relevance_score,
            article.url,
        ),
        reverse=True,
    )
    return _deduplicate(ordered)


def _deduplicate(ordered: Sequence[Article]) -> DeduplicationResult:
    accepted: list[Article] = []
    exact_duplicates = canonical_url_duplicates = near_title_duplicates = 0
    for article in ordered:
        match = next((item for item in accepted if deduplication_reason(article, item)), None)
        if match is None:
            accepted.append(article)
            continue
        reason = deduplication_reason(article, match)
        if reason == "exact":
            exact_duplicates += 1
            continue
        if reason == "canonical_url":
            canonical_url_duplicates += 1
            continue
        if reason == "near_title":
            near_title_duplicates += 1
            continue
    return DeduplicationResult(
        articles=accepted,
        exact_duplicates=exact_duplicates,
        canonical_url_duplicates=canonical_url_duplicates,
        near_title_duplicates=near_title_duplicates,
    )


def deduplication_reason(first: Article, second: Article) -> str | None:
    """Return the first matching identity tier, in strict priority order."""

    if first.provider_article_id and first.provider_article_id == second.provider_article_id:
        return "exact"
    if first.url == second.url:
        return "exact"
    if normalize_url(first.url) == normalize_url(second.url):
        return "canonical_url"
    if near_duplicate_titles(first, second):
        return "near_title"
    return None


def deduplicate_articles(articles: Iterable[Article]) -> list[Article]:
    """Compatibility wrapper for callers that need only accepted articles."""

    return deduplicate_with_diagnostics(list(articles)).articles
