"""Article normalization, relevance scoring, and deterministic deduplication."""

import hashlib
import re
from collections.abc import Iterable
from difflib import SequenceMatcher
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


def near_duplicate_titles(first: str, second: str) -> bool:
    """Conservatively collapse near-identical syndicated headlines."""

    normalized_first = normalize_text(first)
    normalized_second = normalize_text(second)
    if normalized_first == normalized_second:
        return True
    if min(len(normalized_first), len(normalized_second)) < 20:
        return False
    ratio = SequenceMatcher(None, normalized_first, normalized_second).ratio()
    tokens_first = set(normalized_first.split())
    tokens_second = set(normalized_second.split())
    token_overlap = len(tokens_first & tokens_second) / len(tokens_first | tokens_second)
    return ratio >= 0.88 and token_overlap >= 0.75


def deduplicate_articles(articles: Iterable[Article]) -> list[Article]:
    """Keep the newest relevant article per canonical URL or near-duplicate title."""

    ordered = sorted(
        articles,
        key=lambda article: (article.published_at, article.relevance_score),
        reverse=True,
    )
    groups: list[tuple[list[Article], set[str]]] = []
    for article in ordered:
        url = normalize_url(article.url)
        matches = [
            index
            for index, (members, urls) in enumerate(groups)
            if url in urls
            or any(near_duplicate_titles(article.title, member.title) for member in members)
        ]
        if not matches:
            groups.append(([article], {url}))
            continue

        target = matches[0]
        groups[target][0].append(article)
        groups[target][1].add(url)
        for index in reversed(matches[1:]):
            members, urls = groups.pop(index)
            groups[target][0].extend(members)
            groups[target][1].update(urls)
    return [members[0] for members, _ in groups]
