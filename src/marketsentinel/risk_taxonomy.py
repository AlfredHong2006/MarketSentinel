"""Deterministic mapping from stored event data to a fixed downside-risk taxonomy.

There is no embedding model, no fuzzy matcher, and no LLM classifier here. Every theme is
reached through small boundary-aware patterns so a ranked risk can always be explained by the
exact trigger that produced it. Unrecognised mechanisms become ``UNMAPPED`` rather than being
forced into an approximate theme.
"""

import re

from marketsentinel.domain import EventType, RiskTheme

# Event types whose realized negative form is itself the risk. Types absent from this map
# (acquisition, investment, partnership, product launch, earnings, contract award, other)
# require an explicit downside mechanism: being large or newsworthy is not a risk.
REALIZED_NEGATIVE_EVENT_THEMES: dict[EventType, RiskTheme] = {
    EventType.REGULATION: RiskTheme.REGULATORY_ANTITRUST,
    EventType.LITIGATION: RiskTheme.LEGAL_LITIGATION,
    EventType.SUPPLY_DISRUPTION: RiskTheme.SUPPLY_CONSTRAINT,
    EventType.MANAGEMENT_CHANGE: RiskTheme.KEY_PERSON_MANAGEMENT,
    EventType.MACROECONOMIC_EXPOSURE: RiskTheme.MACRO_GEOGRAPHIC,
    EventType.CONTRACT_LOSS: RiskTheme.DEMAND_SLOWDOWN,
    EventType.ANALYST_OR_GUIDANCE_CHANGE: RiskTheme.GUIDANCE_VALUATION,
}

# Ordered most specific first: the first matching theme wins so one mechanism yields exactly
# one theme. Order also resolves deliberate overlaps, for example "competition authority"
# must reach REGULATORY_ANTITRUST rather than COMPETITIVE_PRESSURE.
_THEME_TRIGGERS: tuple[tuple[RiskTheme, tuple[str, ...]], ...] = (
    (
        RiskTheme.EXPORT_TRADE,
        (
            r"export\s+(?:control|restriction|licen[cs]|ban|curb|rule)",
            r"\btariff",
            r"trade\s+(?:restriction|barrier|ban|war)",
            r"\bsanction",
            r"\bembargo",
            r"entity\s+list",
            r"import\s+(?:restriction|ban|duty)",
        ),
    ),
    (
        RiskTheme.CYBERSECURITY,
        # A threat, compromise, or vulnerability -- never the bare topic. "cybersecurity" alone
        # describes subject matter, so a regulatory mechanism that merely concerns cybersecurity
        # must still reach REGULATORY_ANTITRUST.
        (
            r"\bransomware\b",
            r"data\s+breach",
            r"cyber\s*attack",
            r"cyber\s+(?:incident|intrusion|breach)",
            r"\bmalware\b",
            r"\bhack(?:ing|ers?|ed)\b",
            # "exploit" is bound to a vulnerability object: bare "exploit" also means to take
            # commercial advantage ("competitors may exploit the delay") and would misfire.
            r"exploit\w*\s+(?:a\s+|the\s+)?(?:vulnerabilit|flaw|bug|weakness|zero.?day|security)",
            r"security\s+(?:vulnerabilit|incident|breach|flaw)",
            r"unauthori[sz]ed\s+access",
        ),
    ),
    (
        RiskTheme.REGULATORY_ANTITRUST,
        (
            r"\bantitrust\b",
            r"competition\s+(?:authority|regulator|investigation|probe)",
            r"\bregulator(?:y|s)?\b",
            r"\bregulation\b",
            r"compliance\s+(?:burden|requirement|cost)",
            r"\bprobe\b",
            r"licen[cs]e\s+(?:revocation|withdrawal)",
            r"\bantimonopoly\b",
        ),
    ),
    (
        RiskTheme.LEGAL_LITIGATION,
        (
            r"\blawsuit",
            r"\blitigation\b",
            r"class\s+action",
            r"patent\s+(?:infringement|dispute)",
            r"\bcourt\b",
            r"\bdamages\b",
            r"legal\s+(?:claim|exposure|proceeding)",
            r"\binjunction\b",
        ),
    ),
    (
        RiskTheme.SUPPLY_CONSTRAINT,
        (
            r"suppl(?:y|ier)\s+(?:constraint|shortage|disruption|dependence|risk|bottleneck)",
            r"component\s+shortage",
            r"capacity\s+(?:constraint|shortage|limit)",
            r"\bbottleneck",
            r"single[-\s]source",
            r"depend(?:ence|ency|ent)\s+on\s+(?:a\s+)?(?:constrained|single|sole)",
            r"lead\s+times?",
            r"foundry\s+capacity",
            # "product" is required, not optional: bare "lower availability" also describes
            # credit, financing, or labour, none of which is a product supply constraint.
            r"(?:reduced|lower|constrained)\s+product\s+availability",
        ),
    ),
    (
        RiskTheme.CUSTOMER_CONCENTRATION,
        (
            r"customer\s+concentration",
            r"(?:largest|key|single|anchor)\s+customer",
            r"concentrated\s+(?:revenue|demand|customer)",
            r"relian(?:ce|t)\s+on\s+(?:a\s+)?(?:few|single|one)\s+customer",
        ),
    ),
    (
        RiskTheme.DEMAND_SLOWDOWN,
        (
            r"demand\s+(?:slowdown|weakness|decline|softness|destruction)",
            r"(?:slowing|weaker|falling|softening|reduced)\s+demand",
            r"order\s+(?:cut|cancellation|reduction)",
            r"cancel(?:led|ed|s|lation)?\s+orders?",
            r"lost?\s+(?:a\s+)?contract",
            r"contract\s+loss",
            r"restrict(?:s|ed|ing)?\s+(?:the\s+)?addressable\s+market",
            r"\bchurn\b",
            # Falling revenue alone is not a demand mechanism: it can be supply-, pricing-, FX-,
            # regulatory-, or execution-driven. The contraction must be attributed to a customer,
            # contract, or order relationship before this theme is the right home.
            r"(?:decreas\w*|declin\w*|reduction|drop|fall)\s+in\s+(?:revenue|sales|orders?)"
            r"\s+from\s+(?:\w+\s+){0,2}"
            r"(?:customer|client|contract|order|account|reseller|distributor)s?\b",
        ),
    ),
    (
        RiskTheme.COMPETITIVE_PRESSURE,
        (
            r"competitive\s+(?:pressure|threat|intensity|response)",
            r"\bcompetitors?\b",
            r"market\s+share\s+(?:loss|erosion|pressure)",
            r"price\s+(?:competition|war|pressure\s+from\s+rivals)",
            r"\brivals?\b",
            r"commodit(?:isation|ization)",
            # An intensifier is required, and the noun must be "competition" rather than
            # "competitive"/"competitiveness", which read positively about the company itself.
            # Bare "competition" would also steal "competition authority" from REGULATORY_ANTITRUST.
            r"(?:increas\w*|intensif\w*|greater|heightened|growing|stronger|more)\s+competition\b",
        ),
    ),
    (
        RiskTheme.GUIDANCE_VALUATION,
        (
            r"guidance\s+(?:cut|reduction|lowered|withdrawn|risk)",
            r"(?:cut|lowered|reduced|withdrew)\s+(?:its\s+)?guidance",
            r"price\s+target",
            r"\bdowngrade",
            r"\bvaluation\b",
            r"multiple\s+compression",
            r"de[-\s]rating",
        ),
    ),
    (
        RiskTheme.CAPITAL_ALLOCATION,
        (
            r"capital\s+(?:commit|allocation|intensity|expenditure|outlay)",
            r"commit(?:s|ted|ting|ment)?\s+capital",
            r"\bcapex\b",
            r"before\s+(?:demand|utilisation|utilization|returns?)\s+(?:is|are)\s+proven",
            r"\bdilution\b",
            r"(?:debt|leverage)\s+(?:burden|load|risk)",
            r"funding\s+commitment",
        ),
    ),
    (
        RiskTheme.EXECUTION_OPERATIONAL,
        (
            r"execution\s+(?:risk|burden|requirement|uncertaint)",
            r"integration\s+(?:risk|burden|requirement|challenge|complexity)",
            r"deployment\s+(?:risk|burden|complexity|delay)",
            r"operational\s+(?:complexity|risk|disruption)",
            r"\brecall\b",
            r"manufacturing\s+(?:defect|delay|issue)",
            r"(?:ramp|rollout|project)\s+(?:risk|delay)",
            r"\boutage\b",
        ),
    ),
    (
        RiskTheme.KEY_PERSON_MANAGEMENT,
        (
            r"key[-\s]person",
            r"(?:executive|leadership|management)\s+(?:departure|transition|turnover|change)",
            r"(?:chief\s+\w+\s+officer|ceo|cfo|coo|cto)\s+(?:departure|resignation|exit|steps?\s+down)",
            r"\bsuccession\b",
            r"(?:resigns?|resignation|steps?\s+down)\b",
        ),
    ),
    (
        RiskTheme.MACRO_GEOGRAPHIC,
        (
            r"macro(?:economic)?\s+(?:exposure|headwind|weakness|risk)",
            r"\bgeopolitical\b",
            r"(?:currency|foreign\s+exchange|\bfx\b)\s*(?:exposure|risk|headwind)?",
            r"interest\s+rate",
            r"\binflation\b",
            r"\brecession\b",
            r"(?:regional|geographic)\s+concentration",
        ),
    ),
)

_COMPILED_TRIGGERS: tuple[tuple[RiskTheme, tuple[re.Pattern[str], ...]], ...] = tuple(
    (theme, tuple(re.compile(pattern, re.I) for pattern in patterns))
    for theme, patterns in _THEME_TRIGGERS
)

# Concrete cyber-incident concepts only. Bare "cyber" and "cybersecurity" are excluded on purpose:
# they name a subject area, so "new cybersecurity regulation" is a regulatory event, not a breach.
_CYBER_INCIDENT_OVERRIDE = re.compile(
    r"\bransomware\b|data\s+breach|cyber\s*attack|\bmalware\b",
    re.I,
)


def theme_for_realized_negative_event(
    event_type: EventType,
    summary: str = "",
) -> RiskTheme | None:
    """Return the theme for a realized negative event, or ``None`` when none is safe.

    One narrow defensive correction applies: an upstream ``REGULATION`` classification whose
    summary describes an actual cybersecurity incident is re-themed to ``CYBERSECURITY``. This is
    not a general summary classifier. The mechanism matcher is tuned for short purpose-written
    channel phrases, and applying it to a narrative summary was measured to override legitimate
    event types far more often than it corrected them, so the override is restricted to the single
    observed misclassification class and to concrete incident concepts only.
    """

    if event_type is EventType.REGULATION and _CYBER_INCIDENT_OVERRIDE.search(summary) is not None:
        return RiskTheme.CYBERSECURITY
    return REALIZED_NEGATIVE_EVENT_THEMES.get(event_type)


def theme_for_mechanism(mechanism: str) -> RiskTheme:
    """Classify one downside mechanism into exactly one theme, most specific first."""

    for theme, patterns in _COMPILED_TRIGGERS:
        if any(pattern.search(mechanism) is not None for pattern in patterns):
            return theme
    return RiskTheme.UNMAPPED


THEME_LABELS: dict[RiskTheme, str] = {
    RiskTheme.EXPORT_TRADE: "Export / trade restrictions",
    RiskTheme.REGULATORY_ANTITRUST: "Regulatory & government action",
    RiskTheme.LEGAL_LITIGATION: "Legal & litigation exposure",
    RiskTheme.CYBERSECURITY: "Cybersecurity exposure",
    RiskTheme.SUPPLY_CONSTRAINT: "Supply constraint",
    RiskTheme.DEMAND_SLOWDOWN: "Demand slowdown",
    RiskTheme.CUSTOMER_CONCENTRATION: "Customer concentration",
    RiskTheme.COMPETITIVE_PRESSURE: "Competitive pressure",
    RiskTheme.EXECUTION_OPERATIONAL: "Execution & operational risk",
    RiskTheme.CAPITAL_ALLOCATION: "Capital allocation",
    RiskTheme.GUIDANCE_VALUATION: "Guidance & valuation signals",
    RiskTheme.MACRO_GEOGRAPHIC: "Macro & geographic exposure",
    RiskTheme.KEY_PERSON_MANAGEMENT: "Key-person & management change",
    RiskTheme.UNMAPPED: "Unclassified",
}


def theme_label(theme: RiskTheme) -> str:
    return THEME_LABELS[theme]
