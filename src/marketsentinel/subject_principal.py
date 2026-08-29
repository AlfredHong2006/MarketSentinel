"""Whether the subject company is a principal to the development an article reports.

An article is a development for the subject company when that company is a principal to the
underlying event -- buyer, seller, bidder, target, plaintiff or defendant, contractual
counterparty, or owner of the affected asset, right, or liability. Which company holds the
grammatical subject position in the headline decides nothing: "Sanofi sues Pfizer", "Shionogi
Acquires ViiV Healthcare Shareholdings from Pfizer", and "Novo Nordisk Seeks to Outmuscle Pfizer
With $9 Billion Bid" are all Pfizer developments, and a rule keyed on headline subject position
would discard every one of them.

So nothing here asks who the sentence is about. It recognises one specific shape in which the
subject company is demonstrably *not* a party: the company named solely as the employer attached
to a person, in an article reporting that person being appointed somewhere. "Nike Appoints Pfizer
CFO as New Finance Chief" is a Nike development; the subject company is the departing executive's
employer, not a party to the appointment, so the row must not become a subject-company development
and must not feed the subject-company risk layer.

Deliberately one narrow shape rather than a general principal-involvement classifier. Every rule
here deletes a development from the product, so a false positive costs more than a miss, and the
other incidental readings the definition names -- technology merely mentioned, market colour --
are already carried by the commentary guard and the shared meaningful-event floor. Extending this
module means adding another equally specific shape, never loosening this one.
"""

import re

from marketsentinel.normalization import normalize_text
from marketsentinel.ownership_patterns import CompanyIdentity, subject_name_variants

# Roles a person holds, written as they appear directly after a company name ("Pfizer CFO",
# "Pfizer's finance chief", "ex-Pfizer scientist"). Multi-word roles are spelled out here rather
# than allowing filler words between the company and the role: a filler allowance would read
# "Pfizer names chief executive" as a person-descriptor and silently discard the company's own
# appointment, which is the exact failure this module exists to avoid causing.
_ROLE = (
    r"(?:c[efimopst]o"
    r"|chief(?:\s+\w+){0,2}\s+officer"
    r"|chief\s+(?:executive|financial|operating|medical|scientific|technology|technical"
    r"|information|legal|marketing|commercial|people)"
    r"|(?:finance|operating|technology|legal|medical|research|marketing|commercial)\s+chief"
    r"|(?:executive\s+)?(?:vice\s+)?president"
    r"|general\s+counsel"
    r"|head\s+of\s+\w+"
    r"|chair(?:man|woman|person)?"
    r"|executives?|exec|boss|veteran|scientist|researcher|manager|director"
    r"|treasurer|controller|alum(?:nus|na|ni)?|staffer|employee)"
)

# Verbs that mean hiring and nothing else. ``promote`` and ``elevate`` are deliberately absent:
# their dominant reading is an internal move inside the subject company ("Pfizer CFO promoted to
# CEO"), which is a genuine subject-company development. So are ``name`` and ``pick``, which read
# as readily as identification ("Regulators name Pfizer executive in probe") -- they are admitted
# only by the second arm below, which demands an explicit destination role.
_HIRING_VERB = re.compile(
    r"\b(?:appoints?|appointed|appointing|hires?|hired|hiring"
    r"|taps?|tapped|tapping|poach(?:es|ed|ing)?|recruits?|recruited|recruiting)\b"
)
_APPOINTMENT_VERB = re.compile(rf"{_HIRING_VERB.pattern}|\b(?:names?|named|naming|picks?|picked)\b")
# The destination role is what separates an appointment from every other use of those verbs, and
# ``named`` is the reason it is required: without it, "Pfizer CFO named in bribery indictment" --
# unambiguously a subject-company development -- would match the verb alone.
_APPOINTMENT_DESTINATION = re.compile(
    rf"\b(?:as|to)\s+(?:be\s+|become\s+)?(?:the\s+|its\s+|their\s+|a\s+|an\s+)?"
    rf"(?:new\s+|next\s+|incoming\s+|interim\s+|first\s+)?{_ROLE}(?![a-z0-9])"
    rf"|\bnew\s+{_ROLE}(?![a-z0-9])"
)
# ``normalize_text`` turns "Pfizer's" into "pfizer s", so a lone "s" between the company and the
# role is the possessive apostrophe rather than a word of its own.
_TRAILING_ROLE = re.compile(rf"\s+(?:s\s+)?{_ROLE}(?![a-z0-9])")


def reads_as_third_party_appointment(title: str, subject: CompanyIdentity) -> bool:
    """Whether a title reports another organisation appointing someone the subject employs.

    The precondition is the same either way: the title must name the subject company *only* as a
    person's employer, so a title in which the subject appoints, is appointed to, or does anything
    else at all keeps its normal reading. What then establishes the appointment has two forms,
    because the headline shape has two.

    An unambiguous hiring verb standing before that person makes them the object of the hiring
    ("Nike appoints Pfizer CFO as turnaround enters next phase"), and needs nothing further. A
    verb that also reads as identification -- ``named``, ``picked`` -- proves nothing on its own,
    so it must additionally state the role being filled ("Nike names Pfizer's finance chief as its
    new CFO"); without that requirement "Pfizer CFO named in bribery indictment", unambiguously a
    subject-company development, would be discarded.

    Knowingly blunt in one direction: an internal promotion that names no appointing party
    ("Pfizer CFO named as interim CEO") satisfies the second form and would be rejected. No such
    row appears in either analysed corpus -- an internal appointment headline names the company as
    the actor, which fails the precondition -- and the wording needed to exclude it would also
    exclude the third-party case this rule exists for.
    """

    normalized = normalize_text(title)
    employer_mention = _subject_named_only_as_an_employer(normalized, subject)
    if employer_mention is None:
        return False
    if _hiring_verb_precedes(normalized, employer_mention):
        return True
    return (
        _APPOINTMENT_VERB.search(normalized) is not None
        and _APPOINTMENT_DESTINATION.search(normalized) is not None
    )


def _hiring_verb_precedes(normalized_title: str, position: int) -> bool:
    return any(match.start() < position for match in _HIRING_VERB.finditer(normalized_title))


def _subject_named_only_as_an_employer(
    normalized_title: str, subject: CompanyIdentity
) -> int | None:
    """Where the subject is first named, if every mention of it qualifies a person.

    The "every" is the safeguard: one mention of the company acting in its own right is enough to
    make the article the company's own story again, however the rest of the headline reads.
    ``None`` therefore covers both a company that acts and a company the title never names.
    """

    mentions: list[int] = []
    for name in subject_name_variants(subject):
        pattern = rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])"
        for match in re.finditer(pattern, normalized_title):
            if _TRAILING_ROLE.match(normalized_title, match.end()) is None:
                return None
            mentions.append(match.start())
    return min(mentions, default=None)
