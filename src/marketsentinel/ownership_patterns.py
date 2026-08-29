"""High-precision deterministic ownership-report patterns."""

import re
from typing import Protocol

from marketsentinel.normalization import normalize_text


class CompanyIdentity(Protocol):
    symbol: str
    name: str


_LEGAL_SUFFIX_PATTERN = re.compile(
    r"\s+(?:inc\.?|incorporated|corporation|corp\.?|plc|ltd\.?|limited|llc|llp|lp)$",
    re.I,
)
_TRAILING_PARENTHETICAL_PATTERN = re.compile(r"\s*\([^)]*\)$")


def text_describes_external_institutional_holding(
    text: str,
    subject: CompanyIdentity,
) -> bool:
    """Recognize explicit third-party ownership reporting with conservative syntax."""

    text = text.casefold()
    subject_pattern = _company_reference_pattern(subject)
    external_actor = r"\b(?:fund|hedge fund|asset manager|investment manager|investment management|capital management|capital partners|institution(?:al investor)?|investors?|berkshire|blackrock|vanguard|llc|lp|llp)\b"
    owned_security = (
        rf"(?:shares?\s+(?:of|in)\s+{subject_pattern}|(?:of\s+)?{subject_pattern}\s+shares?)"
    )
    owned_position = rf"(?:\b(?:position|stake|holding)s?\s+(?:in|of)\s+{subject_pattern}|{subject_pattern}\s+(?:position|stake|holding)s?)"
    quantity = r"(?:[$£€]?\d[\d,.]*(?:\s?(?:k|m|bn|b|thousand|million|billion))?\s+)?"
    ownership_change = rf"(?:{external_actor}).{{0,100}}?(?:\b(?:buy|buys|bought|sell|sells|sold|acquire|acquires|acquired|purchase|purchases|purchased)\b\s+{quantity}{owned_security}|\b(?:raise|raises|raised|reduce|reduces|reduced|increase|increases|increased|decrease|decreases|decreased|trim|trims|trimmed|take|takes|took)\b\s+(?:its\s+)?{owned_position}|\b(?:report|reports|reported|disclose|discloses|disclosed)\b.{{0,80}}?{quantity}{owned_position})"
    return re.search(ownership_change, text) is not None


def _company_reference_pattern(subject: CompanyIdentity) -> str:
    names = _company_name_variants(subject.name)
    patterns = [_phrase_pattern(name) for name in names if name]
    ticker = re.escape(subject.symbol.casefold())
    if len(subject.symbol) <= 2:
        patterns.append(rf"(?:\${ticker}\b|\b(?:nasdaq|nyse|lse)\s*:\s*{ticker}\b)")
    else:
        patterns.append(rf"(?<![a-z0-9]){ticker}(?![a-z0-9])")
    return rf"(?:{'|'.join(patterns)})"


def _company_name_variants(name: str) -> set[str]:
    without_parenthetical = _TRAILING_PARENTHETICAL_PATTERN.sub("", name).strip()
    variants = {name, without_parenthetical}
    variants.update(_LEGAL_SUFFIX_PATTERN.sub("", value).strip() for value in tuple(variants))
    return {value for value in variants if value}


def _phrase_pattern(phrase: str) -> str:
    words = [re.escape(word) for word in re.findall(r"[a-z0-9]+", phrase.casefold())]
    return rf"(?<![a-z0-9]){'[^a-z0-9]+'.join(words)}(?![a-z0-9])"


def subject_name_variants(subject: CompanyIdentity) -> set[str]:
    """Normalized names and ticker a title may use for the selected company.

    Shared rather than re-derived per caller: every rule that asks "is this company named here"
    has to agree on the same set of spellings, and a second copy of this list would let them
    drift apart silently. The output is ``normalize_text`` form, so callers match it against a
    normalized title, never a raw one.
    """

    names = {normalize_text(subject.name), normalize_text(subject.symbol)}
    legal_suffixes = (" inc", " incorporated", " corporation", " corp", " plc", " limited", " ltd")
    names.update(
        name[: -len(suffix)]
        for name in tuple(names)
        for suffix in legal_suffixes
        if name.endswith(suffix)
    )
    return {name for name in names if name}
