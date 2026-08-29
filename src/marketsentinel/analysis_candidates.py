"""Pure, deterministic candidate selection for automatic article analysis."""

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from marketsentinel.article_sources import classify_article_source
from marketsentinel.domain import Article, AutomaticAnalysisDiagnostics, SourceClass
from marketsentinel.normalization import normalize_text
from marketsentinel.ownership_patterns import (
    CompanyIdentity,
    subject_name_variants,
    text_describes_external_institutional_holding,
)

MIN_USEFUL_RELEVANCE = 0.5
MEDIUM_RELEVANCE_THRESHOLD = 0.70
HIGH_RELEVANCE_THRESHOLD = 0.85
DEFAULT_PUBLISHER_CAP = 3
DEFAULT_OFFICIAL_COMPANY_CAP = 3
NEAR_TITLE_WINDOW_HOURS = 48
NEAR_TITLE_OVERLAP = 0.75

SOURCE_PRIORITY = {
    SourceClass.OFFICIAL_COMPANY: 5,
    SourceClass.REGULATORY_OR_FILING: 5,
    SourceClass.MAJOR_FINANCIAL_NEWS: 4,
    SourceClass.INDUSTRY_SPECIALIST: 3,
    SourceClass.GENERAL_NEWS: 2,
    SourceClass.UNKNOWN: 1,
    SourceClass.COMMENTARY_OR_OPINION: 0,
}

_PREDICTION_PATTERNS = (
    re.compile(r"\bprice prediction\b", re.I),
    re.compile(r"^prediction\s*:", re.I),
    re.compile(r"\bwill\b.{0,45}\b(?:stock|shares?)\b.{0,20}\b(?:rise|fall|go up|drop)\b", re.I),
    re.compile(r"\bwhere will\b.{0,45}\b(?:stock|shares?)\b", re.I),
    re.compile(r"\b(?:stock|shares?)\b.{0,25}\b(?:overvalued|undervalued)\b", re.I),
)
_MARKET_REACTION_ONLY_PATTERNS = (
    re.compile(r"\bstocks?\s+making\s+the\s+biggest\s+moves\b", re.I),
    re.compile(r"\bprice\s+today\b.*\b(?:live|chart|market\s+data)\b", re.I),
    # A record/biggest + market-capitalisation rule was removed: "record" and "biggest" are the
    # commonest earnings and buyback adjectives, so it rejected genuine results announcements
    # while failing to catch the pure market-value milestone it targeted.
    re.compile(
        r"\bshares?\s+(?:are\s+)?(?:surging|soaring|jumping|falling|sliding)\b"
        r".{0,80}\b(?:make\s+money|trade|trading)\b",
        re.I,
    ),
)
# A company's own results release and its own developer blog share one SourceClass, so ranking on
# source tier plus recency lets routine editorial content fill the official-company cap ahead of
# the quarter's actual disclosure. These patterns name the disclosure itself -- an issuer results
# release, a periodic filing, a guidance change, or a regulator/export action -- using generic
# corporate-disclosure vocabulary only, never a company, product, or sector term. A scheduling
# notice ("Sets Conference Call for Third-Quarter Financial Results") announces no result and is
# deliberately not matched: "announces"/"reports" must attach to the results themselves.
_DISCLOSURE_PATTERNS = (
    re.compile(r"\bannounces?\b.{0,20}\bfinancial results\b", re.I),
    re.compile(
        r"\breports?\b.{0,20}\b(?:quarterly|q[1-4]|first|second|third|fourth)\b"
        r".{0,20}\b(?:results|earnings|revenue)\b",
        re.I,
    ),
    re.compile(r"\bfiscal\s+(?:19|20)\d{2}\b.{0,10}\bresults\b", re.I),
    re.compile(r"\bform\s+(?:10-q|10-k|8-k|6-k|20-f)\b", re.I),
    re.compile(r"\b(?:raises?|cuts?|lowers?|updates?|withdraws?)\b.{0,25}\bguidance\b", re.I),
    re.compile(r"\bguidance\b.{0,20}\b(?:beat|miss|above|below|raised|cut)\b", re.I),
    re.compile(r"\bsec\b.{0,15}\b(?:filing|charges|investigation|settlement|subpoena)\b", re.I),
    re.compile(
        r"\b(?:fined|penalized|sanctioned)\b.{0,30}\b(?:regulator|commission|agency|ftc|doj|sec)\b",
        re.I,
    ),
    re.compile(r"\bexport\s+(?:ban|control|restriction|licen[cs]e)s?\b", re.I),
    re.compile(r"\b(?:earnings|revenue)\s+(?:beat|miss|top|surpass)\b", re.I),
)
# Deal, financing, and enforcement reporting loses its slot to routine coverage for the same
# reason a results release does: ranking sees source tier and recency, never what happened. These
# name the action itself in generic corporate vocabulary -- a stake or investment, an acquisition,
# a financing, a concrete government/regulator action, or litigation -- with no company, product,
# or sector term. Families with no real signal of their own are deliberately absent: a contract
# family matched nothing, and product-launch and partnership vocabulary is dominated by issuer
# editorial that the official-company tier already reaches.
_ACTION_PATTERNS = (
    # Strategic investment or stake. A bare "invests" needs a size marker so routine coverage of
    # someone else's small position does not qualify.
    re.compile(
        r"\b(?:takes?|acquires?|buys?|discloses?)\b.{0,25}\bstake\b"
        r"|\bstake\s+in\b"
        r"|\binvests?\b.{0,30}(?:\$|billion|bn\b|million)"
        r"|\bpledges?\b.{0,20}\$"
        r"|\bbacks?\b.{0,40}\$"
        # "bet" is deliberately absent from the amount-context nouns: retrospectives and
        # commentary use it figuratively ("the $7 billion bet that built..."), and the literal
        # transactions it would catch already carry invest/stake/backing vocabulary.
        r"|\$\s?\d[\d.,]*\s*(?:billion|bn|trillion|tn)\b"
        r".{0,35}\b(?:investment|stake|backing|guarantee|backstop)\b",
        re.I,
    ),
    # Acquisition or M&A. "buys" is bound to an acquirable object so it cannot match buying chips.
    re.compile(
        r"\b(?:acquires?|to\s+acquire|acquisition\s+of|acquiring|merger|takeover|buyout)\b"
        r"|\bbuy(?:s|ing)?\b.{0,40}\b(?:startup|company|assets|maker|firm)\b",
        re.I,
    ),
    # Financing.
    re.compile(
        r"\braises?\b.{0,25}\$|\bbond\s+sale\b|\bdebt\s+offering\b|\bjunk-?bond\b",
        re.I,
    ),
    # A concrete government or regulator action, bound to a traded object so that discussion of
    # policy in the abstract does not qualify.
    re.compile(
        r"\b(?:bans?|banned|blocks?|blocked|restricts?|suspends?|revokes?"
        r"|approves?|clears?|eases?|tightens?|imposes?)\b"
        r".{0,45}\b(?:chips?|exports?|sales?|shipments?|licen[cs]es?|imports?|loophole)\b"
        r"|\bentity\s+list\b|\bantitrust\b"
        r"|\bexport\s+(?:ban|control|restriction|licen[cs]e)s?\b"
        r"|\bsmuggl\w+\b|\breroute\b",
        re.I,
    ),
    # Litigation.
    re.compile(
        r"\bsue[sd]?\b|\bsued\b|\blawsuit\b|\bcharged\s+with\b|\bindicted\b"
        r"|\bsettl(?:es?|ed|ement)\b.{0,30}\b(?:lawsuit|charges?|case|dispute)\b",
        re.I,
    ),
)
# Action vocabulary appears just as readily in a piece explaining an action as in the report of
# one. These two guards remove the explanatory and the price-reaction framings, so priority is
# never granted for merely discussing an event.
_COMMENTARY_GUARD_PATTERNS = (
    re.compile(
        r"^(?:why|how|what|should|could|will|opinion|analysis|explainer|prediction)\b",
        re.I,
    ),
    re.compile(r"\b(?:should|explained|explainer|preview|what\s+to\s+expect)\b", re.I),
)
_MARKET_MOVE_GUARD_PATTERNS = (
    re.compile(
        r"\b(?:shares?|stock|market\s+(?:cap|value|capitalization))\b.{0,35}"
        r"\b(?:surge\w*|jump\w*|climb\w*|rise[sn]?|rose|fall\w*|fell|slid\w*|sink\w*"
        r"|gain\w*|drop\w*|record|tops?|close[sd]?|rally|swing)\b",
        re.I,
    ),
)
# An outside holder exiting a position carries the same "stake"/investment vocabulary as a
# genuine corporate action, and it is the commoner story: routine 13F-style portfolio reporting
# on a company's shares, not something the company did. This guard is deliberately blunt at the
# title-only level -- every disposal-shaped title is excluded by default -- because nothing here
# knows which company is under analysis; the one case that guard wrongly excludes, the subject
# itself disposing of a stake it holds in someone else, is restored where the selector grants
# priority, since only the selector knows the subject.
_STAKE_DISPOSAL_VERB = (
    r"(?:sells?|sold|offload(?:s|ed|ing)?|exits?|exited|dumps?|dumped"
    r"|divests?|divested|liquidat(?:es?|ed|ing))"
)
_STAKE_DISPOSAL_PATTERNS = (
    re.compile(rf"\b{_STAKE_DISPOSAL_VERB}\b.{{0,40}}\bstake\b", re.I),
    re.compile(r"\bstake\s+sale\b", re.I),
)
_EXTERNAL_OWNER_MARKER = (
    r"(?:advisors?|advisory|associates?|asset\s+manag(?:e)?ment|capital\s+partners|"
    r"financial\s+partners?|"
    r"investment\s+(?:manager|management|partners?)|investments|"
    r"institution(?:al\s+investor)?|"
    r"investors?|fund|hedge\s+fund|retirement\s+systems?|llc|llp|l\s*p|ltd|limited)"
)
_EXTERNAL_ENTITY_MARKER = (
    rf"(?:{_EXTERNAL_OWNER_MARKER}|co|company|corp|corporation|group|inc|partners|plc|trust)"
)
_OWNERSHIP_ACTION = (
    r"(?:buy|buys|bought|sell|sells|sold|acquire|acquires|acquired|purchase|purchases|purchased)"
)
_OWNED_SECURITY_NOUN = re.compile(r"\b(?:shares?|stakes?|positions?|holdings?)\b")
_CORPORATE_NAME_TOKEN = r"(?:inc|incorporated|corp|corporation|llc|llp|ltd|limited|plc|trust)"
# Routine Rule 10b5-1 sales are pre-scheduled and non-discretionary, so they are not company
# events. Both the literal marker and an explicit sale/disposition are required: a title merely
# mentioning a plan (for example cancelling one) stays eligible.
_RULE_10B5_1_MARKER = re.compile(r"\b10b5[-\s]?1\b", re.I)
_RULE_10B5_1_DISPOSAL = re.compile(
    r"\b(?:sell|sells|selling|sold|sale|sales|dispose[sd]?|disposal|disposition)\b", re.I
)
_SUBJECT_ACTIONS = (
    "acquire",
    "acquires",
    "acquired",
    "buy",
    "buys",
    "bought",
    "invest",
    "invests",
    "invested",
    "partner",
    "partners",
    "partnered",
    "raises capital",
    "raised capital",
    "raises funding",
    "launches",
    "launched",
    "announces",
    "announced",
    "report",
    "reports",
    "reported",
    "post",
    "posts",
    "posted",
    "beat",
    "beats",
    "miss",
    "misses",
    "missed",
    "unveil",
    "unveils",
    "unveiled",
    "win",
    "wins",
    "won",
    "secure",
    "secures",
    "secured",
    "settle",
    "settles",
    "settled",
    "file",
    "files",
    "filed",
    "expand",
    "expands",
    "expanded",
)
_TITLE_IGNORED = {
    "about",
    "after",
    "amid",
    "and",
    "for",
    "from",
    "into",
    "says",
    "that",
    "the",
    "this",
    "with",
}


@dataclass(frozen=True)
class CandidateSelection:
    candidates: tuple[Article, ...]
    diagnostics: AutomaticAnalysisDiagnostics


def has_financial_disclosure_signal(title: str) -> bool:
    """Whether a title names a periodic financial disclosure or a regulator/export action.

    Company-agnostic by construction: only generic corporate-disclosure vocabulary is matched,
    so the same rule works for any issuer without a per-company keyword list.
    """

    return any(pattern.search(title) is not None for pattern in _DISCLOSURE_PATTERNS)


def describes_market_move(title: str) -> bool:
    """Whether a title's subject is a price or market-capitalisation move rather than an event."""

    return any(pattern.search(title) is not None for pattern in _MARKET_MOVE_GUARD_PATTERNS)


def reads_as_commentary(title: str) -> bool:
    """Whether a title frames itself as explanation, advocacy, or a preview of a future event."""

    return any(pattern.search(title) is not None for pattern in _COMMENTARY_GUARD_PATTERNS)


def is_stake_disposal_shaped(title: str) -> bool:
    """Whether a title reports someone exiting a stake, without regard to who is exiting."""

    return any(pattern.search(title) is not None for pattern in _STAKE_DISPOSAL_PATTERNS)


def has_priority_signal(title: str) -> bool:
    """Whether a title reports a disclosure or a concrete corporate action worth prioritising.

    All three guards apply to the whole signal, including the disclosure vocabulary: a piece
    arguing why an export control should stay, reporting that a stock climbed on a deal, or
    reporting an outside holder's exit describes an action without being the report of a subject-
    company event, and must not take a slot from the report of one.
    """

    if (
        reads_as_commentary(title)
        or describes_market_move(title)
        or is_stake_disposal_shaped(title)
    ):
        return False
    if has_financial_disclosure_signal(title):
        return True
    return any(pattern.search(title) is not None for pattern in _ACTION_PATTERNS)


def _is_subject_initiated_stake_disposal(title: str, subject: CompanyIdentity) -> bool:
    """Whether the subject company itself is the party disposing of a stake it holds.

    ``has_priority_signal`` excludes every disposal-shaped title by default, since that
    vocabulary usually reports an outside holder exiting a position in the subject. When the
    subject itself is the seller -- divesting a stake it holds in someone else -- the same title
    is a genuine corporate action; only the caller here knows which company is under analysis, so
    only here is priority restored for that one case.
    """

    if not is_stake_disposal_shaped(title):
        return False
    normalized = normalize_text(title)
    for name in sorted(subject_name_variants(subject), key=len, reverse=True):
        # An optional stray "s" absorbs the possessive apostrophe normalize_text leaves behind
        # ("Nvidia's" -> "nvidia s"); it is not itself part of the subject name.
        if re.search(rf"\b{re.escape(name)}\b\s+(?:s\s+)?{_STAKE_DISPOSAL_VERB}\b", normalized):
            return True
    return False


def _grants_priority(title: str, subject: CompanyIdentity) -> bool:
    """The selector's actual priority decision: the base signal, with the disposal exception."""

    if has_priority_signal(title):
        return True
    return _is_subject_initiated_stake_disposal(title, subject)


def select_analysis_candidates(
    articles: Sequence[Article],
    now: datetime,
    limit: int,
    *,
    subject_company: CompanyIdentity,
    relevance_floor: float = MIN_USEFUL_RELEVANCE,
    publisher_cap: int = DEFAULT_PUBLISHER_CAP,
    official_company_cap: int = DEFAULT_OFFICIAL_COMPANY_CAP,
    prioritize_disclosures: bool = False,
    priority_bonus_limit: int = 0,
) -> list[Article]:
    """Return an ordered candidate list with no I/O or implicit clock access."""

    return list(
        select_analysis_candidates_with_diagnostics(
            articles,
            now,
            limit,
            subject_company=subject_company,
            relevance_floor=relevance_floor,
            publisher_cap=publisher_cap,
            official_company_cap=official_company_cap,
            prioritize_disclosures=prioritize_disclosures,
            priority_bonus_limit=priority_bonus_limit,
        ).candidates
    )


def select_analysis_candidates_with_diagnostics(
    articles: Sequence[Article],
    now: datetime,
    limit: int,
    *,
    subject_company: CompanyIdentity,
    relevance_floor: float = MIN_USEFUL_RELEVANCE,
    publisher_cap: int = DEFAULT_PUBLISHER_CAP,
    official_company_cap: int = DEFAULT_OFFICIAL_COMPANY_CAP,
    prioritize_disclosures: bool = False,
    priority_bonus_limit: int = 0,
) -> CandidateSelection:
    """Filter, rank, and diversify candidates deterministically.

    ``prioritize_disclosures`` orders financial disclosures and concrete corporate actions ahead
    of other articles *of the same source tier*, never above a higher tier. That vocabulary
    appears in commentary and analysis as readily as in the report of the event, so a signal that
    outranked source class would let an opinion piece displace a wire report merely for saying
    "earnings beat". It bypasses no cap: a prioritised article still competes under
    ``official_company_cap``, ``publisher_cap``, and near-title deduplication, so a period with no
    such article selects exactly what it selects today. Left off, ranking is unchanged.

    ``priority_bonus_limit`` then allows up to that many *additional* accepts, drawn only from
    articles carrying a priority signal that the ordinary walk did not reach. A period's cost is
    therefore ``limit`` when nothing material happened and at most ``limit + priority_bonus_limit``
    when it did, which keeps a quiet month cheap without capping a dense one at a quiet month's
    budget. The additions run through the same caps and deduplication as every other accept.
    """

    if not 0 <= limit <= 40:
        raise ValueError("candidate limit must be between 0 and 40")
    if priority_bonus_limit < 0:
        raise ValueError("priority bonus limit must not be negative")
    if limit + priority_bonus_limit > 40:
        raise ValueError("candidate limit plus priority bonus must not exceed 40")
    if publisher_cap < 1:
        raise ValueError("publisher cap must be at least 1")
    if official_company_cap < 1:
        raise ValueError("official company cap must be at least 1")

    eligible: list[Article] = []
    demo_rejected = low_relevance_rejected = obvious_holdings_rejected = 0
    excluded_prediction = commentary_deprioritized = market_reaction_rejected = 0
    scheduled_insider_sale_rejected = 0
    for article in articles:
        if article.is_demo:
            demo_rejected += 1
            continue
        if article.relevance_score < relevance_floor:
            low_relevance_rejected += 1
            continue
        if _is_obvious_holding_title(article.title, subject_company):
            obvious_holdings_rejected += 1
            continue
        if _is_obvious_prediction(article.title):
            excluded_prediction += 1
            continue
        if _is_market_reaction_only(article.title):
            market_reaction_rejected += 1
            continue
        if _is_routine_rule_10b5_1_sale(article.title, subject_company):
            scheduled_insider_sale_rejected += 1
            continue
        if (
            classify_article_source(article.source, article.url, article.title)
            is SourceClass.COMMENTARY_OR_OPINION
        ):
            commentary_deprioritized += 1
        eligible.append(article)

    ranked = sorted(
        eligible,
        key=lambda article: _disclosure_ranking_key(
            article, now, prioritize_disclosures, subject_company
        ),
    )
    accepted: list[Article] = []
    accepted_fingerprints: set[str] = set()
    publisher_counts: Counter[str] = Counter()
    official_company_count = 0
    publisher_cap_rejected = official_family_cap_rejected = near_title_rejected = 0

    def admit(candidates: Sequence[Article], target: int) -> None:
        """Accept from ``candidates`` until ``target`` is reached, sharing all cap state."""

        nonlocal official_company_count
        nonlocal publisher_cap_rejected, official_family_cap_rejected, near_title_rejected
        for article in candidates:
            if len(accepted) >= target:
                break
            if article.fingerprint in accepted_fingerprints:
                continue
            if any(_near_title(article, previous) for previous in accepted):
                near_title_rejected += 1
                continue
            source_class = classify_article_source(article.source, article.url, article.title)
            if (
                source_class is SourceClass.OFFICIAL_COMPANY
                and official_company_count >= official_company_cap
            ):
                official_family_cap_rejected += 1
                continue
            publisher = normalize_text(article.source) or "unknown source"
            if publisher_counts[publisher] >= publisher_cap:
                publisher_cap_rejected += 1
                continue
            accepted.append(article)
            accepted_fingerprints.add(article.fingerprint)
            publisher_counts[publisher] += 1
            if source_class is SourceClass.OFFICIAL_COMPANY:
                official_company_count += 1

    admit(ranked, limit)
    if priority_bonus_limit:
        # Ranked order is preserved, so the additions are the highest-ranked priority articles
        # the ordinary walk had no room for -- never a separate lane with its own ordering.
        remaining_priority = [
            article
            for article in ranked
            if article.fingerprint not in accepted_fingerprints
            and _grants_priority(article.title, subject_company)
        ]
        admit(remaining_priority, len(accepted) + priority_bonus_limit)

    diagnostics = AutomaticAnalysisDiagnostics(
        considered=len(articles),
        selected=len(accepted),
        demo_rejected=demo_rejected,
        low_relevance_rejected=low_relevance_rejected,
        obvious_holdings_rejected=obvious_holdings_rejected,
        excluded_prediction=excluded_prediction,
        commentary_deprioritized=commentary_deprioritized,
        publisher_cap_rejected=publisher_cap_rejected,
        official_family_cap_rejected=official_family_cap_rejected,
        near_title_rejected=near_title_rejected,
        market_reaction_rejected=market_reaction_rejected,
        scheduled_insider_sale_rejected=scheduled_insider_sale_rejected,
    )
    return CandidateSelection(tuple(accepted), diagnostics)


def _disclosure_ranking_key(
    article: Article,
    now: datetime,
    prioritize_disclosures: bool,
    subject: CompanyIdentity,
) -> tuple[float, int, int, float, float, str]:
    """``_ranking_key`` with the priority term spliced in directly below source tier.

    The term is a constant when the flag is off, so ordering is then decided entirely by
    ``_ranking_key`` exactly as before.
    """

    source_tier, *remainder = _ranking_key(article, now)
    disclosure = 0 if prioritize_disclosures and _grants_priority(article.title, subject) else 1
    return (source_tier, disclosure, *remainder)


def _ranking_key(article: Article, now: datetime) -> tuple[float, int, float, float, str]:
    source_class = classify_article_source(article.source, article.url, article.title)
    age_seconds = max(0.0, (now - article.published_at).total_seconds())
    return (
        -float(SOURCE_PRIORITY[source_class]),
        -_relevance_band(article.relevance_score),
        age_seconds,
        -article.relevance_score,
        article.fingerprint,
    )


def _relevance_band(relevance_score: float) -> int:
    if relevance_score >= HIGH_RELEVANCE_THRESHOLD:
        return 2
    if relevance_score >= MEDIUM_RELEVANCE_THRESHOLD:
        return 1
    return 0


def _is_obvious_prediction(title: str) -> bool:
    return any(pattern.search(title) is not None for pattern in _PREDICTION_PATTERNS)


def _is_market_reaction_only(title: str) -> bool:
    return any(pattern.search(title) is not None for pattern in _MARKET_REACTION_ONLY_PATTERNS)


def _is_routine_rule_10b5_1_sale(title: str, subject: CompanyIdentity) -> bool:
    """Reject a pre-scheduled insider disposal, never a mere mention of a trading plan.

    The raw title is used so the hyphenated rule marker stays detectable, and the shared
    subject-company-action escape hatch still applies.
    """

    if _RULE_10B5_1_MARKER.search(title) is None:
        return False
    if _RULE_10B5_1_DISPOSAL.search(title) is None:
        return False
    return not _title_contains_subject_company_action(title, subject)


def _is_obvious_holding_title(title: str, subject: CompanyIdentity) -> bool:
    """Reject external portfolio updates only when the subject itself took no action.

    Every ownership-title path shares one escape hatch: a title that also states a genuine
    subject-company action is never treated as a routine third-party holding report.
    """

    if not (
        _title_has_reverse_external_holding_structure(title, subject)
        or text_describes_external_institutional_holding(title, subject)
    ):
        return False
    return not _title_contains_subject_company_action(title, subject)


def _title_has_reverse_external_holding_structure(title: str, subject: CompanyIdentity) -> bool:
    normalized = normalize_text(title)
    subject_names = subject_name_variants(subject)
    quantity = r"(?:\$?\d[\d ]*(?:k|m|bn|b|thousand|million|billion)?\s+)?"
    for name in sorted(subject_names, key=len, reverse=True):
        subject_pattern = re.escape(name)
        security_first = re.compile(
            rf"\b{subject_pattern}\b.{{0,25}}?\bshares?\b\s+{_OWNERSHIP_ACTION}"
            rf"\s+by\s+.{{1,80}}?\b{_EXTERNAL_ENTITY_MARKER}\b"
        )
        actor_first = re.compile(
            rf"\b{_EXTERNAL_ENTITY_MARKER}\b.{{0,50}}?\b{_OWNERSHIP_ACTION}\b\s+{quantity}"
            rf"shares?\s+(?:of|in)\s+\b{subject_pattern}\b"
        )
        actor_first_reordered_quantity = re.compile(
            rf"\b{_EXTERNAL_ENTITY_MARKER}\b.{{0,50}}?\b{_OWNERSHIP_ACTION}\b\s+shares?\s+"
            rf"(?:of|in)\s+{quantity}\b{subject_pattern}\b"
        )
        actor_position = re.compile(
            rf"\b{_EXTERNAL_ENTITY_MARKER}\b.{{0,60}}?"
            rf"\b(?:has|holds|raises?|reduces?|trims?|increases?|decreases?|{_OWNERSHIP_ACTION})\b"
            rf".{{0,30}}?\b(?:new\s+)?(?:stake|position|holding)s?\s+(?:of|in)\s+"
            rf"\b{subject_pattern}\b"
        )
        subject_position = re.compile(
            rf"\b{subject_pattern}\b.{{0,25}}?\bis\b.{{0,60}}?\b{_EXTERNAL_ENTITY_MARKER}\b"
            rf".{{0,30}}?\b(?:largest|top)\s+(?:stake|position|holding)\b"
        )
        # A cash investment into the subject uses the tighter portfolio-owner marker: a corporate
        # legal suffix alone is not evidence of routine portfolio activity, and a strategic
        # investment by an operating company is a material subject-company event.
        actor_investment = re.compile(
            rf"\b{_EXTERNAL_OWNER_MARKER}\b.{{0,60}}?\binvests?\b\s+"
            rf"\$?\d[\d ]*(?:k|m|bn|b|thousand|million|billion)?\s+in\s+"
            rf"\b{subject_pattern}\b"
        )
        subject_position_change = re.compile(
            rf"\b{subject_pattern}\b.{{0,25}}?\b(?:stock\s+)?(?:stake|position|holding)s?\b"
            rf"\s+(?:decreased|lifted|raised|increased|reduced|trimmed)\s+by\s+.{{1,80}}?"
            rf"\b{_EXTERNAL_OWNER_MARKER}\b"
        )
        if any(
            pattern.search(normalized)
            for pattern in (
                security_first,
                actor_first,
                actor_first_reordered_quantity,
                actor_position,
                subject_position,
                actor_investment,
                subject_position_change,
            )
        ):
            return True
    return False


def _title_contains_subject_company_action(title: str, subject: CompanyIdentity) -> bool:
    """Whether the subject company itself is described as acting in this title.

    This escape hatch guards every ownership-rejection path, so it must not mistake the
    ownership construction itself for a subject action. Several verbs are shared between the
    two readings ("acquired", "invests"), and some are also nouns inside an investor's name
    ("... General Partner Inc."), so a match is ignored when its context marks it as part of
    the ownership phrase rather than something the subject did.
    """

    normalized = normalize_text(title)
    for name in sorted(subject_name_variants(subject), key=len, reverse=True):
        start = 0
        while (position := normalized.find(name, start)) >= 0:
            following = normalized[position + len(name) : position + len(name) + 45]
            if any(
                _is_subject_action_context(following, match)
                for action in _SUBJECT_ACTIONS
                if (match := re.search(rf"\b{re.escape(action)}\b", following)) is not None
            ):
                return True
            start = position + len(name)
    return False


def _is_subject_action_context(following: str, match: re.Match[str]) -> bool:
    """Reject a candidate action that belongs to the ownership phrase, not to the subject."""

    if _OWNED_SECURITY_NOUN.search(following[: match.start()]) is not None:
        # "<subject> shares acquired by ..." — the subject is the object, not the actor.
        return False
    # "... acquired by <fund>", or a noun inside an investor's name ("General Partner Inc").
    trailing = following[match.end() :]
    return re.match(rf"\s+(?:by\b|{_CORPORATE_NAME_TOKEN}\b)", trailing) is None


def _near_title(first: Article, second: Article) -> bool:
    if abs((first.published_at - second.published_at).total_seconds()) > (
        NEAR_TITLE_WINDOW_HOURS * 60 * 60
    ):
        return False
    first_terms = _title_terms(first.title)
    second_terms = _title_terms(second.title)
    if not first_terms or not second_terms:
        return False
    shared_terms = first_terms & second_terms
    overlap = len(shared_terms) / min(len(first_terms), len(second_terms))
    if overlap < NEAR_TITLE_OVERLAP:
        return False
    # High template overlap is not enough when each headline replaces a substantive detail,
    # such as a counterparty, place, acquisition target, or opposite earnings outcome.
    return not (first_terms - shared_terms and second_terms - shared_terms)


def _title_terms(title: str) -> set[str]:
    return {
        _stem_title_term(term)
        for term in re.findall(r"[a-z0-9]{3,}", title.casefold())
        if term not in _TITLE_IGNORED
    }


def _stem_title_term(term: str) -> str:
    if term.endswith("sses") and len(term) > 5:
        return term[:-2]
    if term.endswith("s") and not term.endswith("ss") and len(term) > 4:
        return term[:-1]
    for suffix in ("ing", "ed"):
        if term.endswith(suffix) and len(term) - len(suffix) >= 4:
            return term[: -len(suffix)]
    return term
