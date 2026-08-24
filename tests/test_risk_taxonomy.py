import pytest

from marketsentinel.domain import EventType, RiskTheme
from marketsentinel.risk_taxonomy import (
    REALIZED_NEGATIVE_EVENT_THEMES,
    theme_for_mechanism,
    theme_for_realized_negative_event,
    theme_label,
)

# One matching mechanism and one adversarial near-miss for every ranked theme. The negatives
# are deliberately chosen to contain a related word without a concrete downside mechanism.
_THEME_CASES: tuple[tuple[RiskTheme, str, str], ...] = (
    (
        RiskTheme.EXPORT_TRADE,
        "new export controls restrict shipments of advanced accelerators",
        "expands exports of consumer accessories into new markets",
    ),
    (
        RiskTheme.CYBERSECURITY,
        "a data breach exposes customer records held by the company",
        "improves physical badge security at the campus entrance",
    ),
    (
        RiskTheme.REGULATORY_ANTITRUST,
        "an antitrust investigation could force changes to store terms",
        "publishes a voluntary industry code of good practice",
    ),
    (
        RiskTheme.LEGAL_LITIGATION,
        "a class action lawsuit seeks damages over battery performance",
        "hires a new general counsel to lead the legal team",
    ),
    (
        RiskTheme.SUPPLY_CONSTRAINT,
        "creates dependence on a constrained foundry supplier",
        "opens a second warehouse to hold more finished inventory",
    ),
    (
        RiskTheme.CUSTOMER_CONCENTRATION,
        "concentrates revenue in a single anchor customer",
        "adds thousands of small business customers to the platform",
    ),
    (
        RiskTheme.DEMAND_SLOWDOWN,
        "softening demand reduces expected unit shipments",
        "demand for the new handset exceeded internal plans",
    ),
    (
        RiskTheme.COMPETITIVE_PRESSURE,
        "competitors may undercut pricing on comparable accelerators",
        "wins an industry award for engineering excellence",
    ),
    (
        RiskTheme.GUIDANCE_VALUATION,
        "management cut guidance for the coming quarter",
        "provides more detailed segment reporting from next year",
    ),
    (
        RiskTheme.CAPITAL_ALLOCATION,
        "increases capital committed before utilisation is proven",
        "returns surplus cash through an ordinary quarterly dividend",
    ),
    (
        RiskTheme.EXECUTION_OPERATIONAL,
        "adds integration requirements across two manufacturing sites",
        "completes a routine software update ahead of schedule",
    ),
    (
        RiskTheme.KEY_PERSON_MANAGEMENT,
        "the chief financial officer resigns with no named successor",
        "appoints three additional regional sales managers",
    ),
    (
        RiskTheme.MACRO_GEOGRAPHIC,
        "geopolitical tension raises regional operating risk",
        "translates its developer documentation into more languages",
    ),
)


@pytest.mark.parametrize(("theme", "mechanism", "_negative"), _THEME_CASES)
def test_every_theme_has_a_matching_mechanism(
    theme: RiskTheme, mechanism: str, _negative: str
) -> None:
    assert theme_for_mechanism(mechanism) is theme


@pytest.mark.parametrize(("theme", "_mechanism", "negative"), _THEME_CASES)
def test_every_theme_has_an_adversarial_non_matching_mechanism(
    theme: RiskTheme, _mechanism: str, negative: str
) -> None:
    assert theme_for_mechanism(negative) is not theme


def test_all_ranked_themes_are_covered_by_the_case_table() -> None:
    """A new theme cannot be added without matching and non-matching fixtures."""

    covered = {theme for theme, _, _ in _THEME_CASES}
    ranked = set(RiskTheme) - {RiskTheme.UNMAPPED}
    assert covered == ranked


def test_unrecognised_mechanism_becomes_unmapped_rather_than_approximated() -> None:
    assert theme_for_mechanism("the company continues to operate normally") is RiskTheme.UNMAPPED
    assert theme_for_mechanism("") is RiskTheme.UNMAPPED


@pytest.mark.parametrize(
    ("event_type", "theme"),
    [
        (EventType.REGULATION, RiskTheme.REGULATORY_ANTITRUST),
        (EventType.LITIGATION, RiskTheme.LEGAL_LITIGATION),
        (EventType.SUPPLY_DISRUPTION, RiskTheme.SUPPLY_CONSTRAINT),
        (EventType.MANAGEMENT_CHANGE, RiskTheme.KEY_PERSON_MANAGEMENT),
        (EventType.MACROECONOMIC_EXPOSURE, RiskTheme.MACRO_GEOGRAPHIC),
        (EventType.CONTRACT_LOSS, RiskTheme.DEMAND_SLOWDOWN),
        (EventType.ANALYST_OR_GUIDANCE_CHANGE, RiskTheme.GUIDANCE_VALUATION),
    ],
)
def test_safe_realized_negative_event_mappings(event_type: EventType, theme: RiskTheme) -> None:
    assert theme_for_realized_negative_event(event_type) is theme


@pytest.mark.parametrize(
    "event_type",
    [
        EventType.ACQUISITION,
        EventType.INVESTMENT,
        EventType.PARTNERSHIP,
        EventType.PRODUCT_LAUNCH,
        EventType.EARNINGS,
        EventType.CONTRACT_AWARD,
        EventType.OTHER,
        EventType.UNCERTAIN,
    ],
)
def test_event_types_without_a_safe_mapping_require_an_explicit_mechanism(
    event_type: EventType,
) -> None:
    assert theme_for_realized_negative_event(event_type) is None
    assert event_type not in REALIZED_NEGATIVE_EVENT_THEMES


def test_regulatory_wins_over_competitive_for_a_competition_authority_mechanism() -> None:
    """Deterministic priority resolves the deliberate keyword overlap."""

    mechanism = "a competition authority investigation could restrict bundling"
    assert theme_for_mechanism(mechanism) is RiskTheme.REGULATORY_ANTITRUST


def test_one_mechanism_yields_exactly_one_theme() -> None:
    """Several loose keywords in one short mechanism must not emit several themes."""

    mechanism = "tariff and litigation and supplier shortage and competitor pricing"
    assert theme_for_mechanism(mechanism) is RiskTheme.EXPORT_TRADE


def test_every_theme_has_a_human_label() -> None:
    assert all(theme_label(theme) for theme in RiskTheme)


def test_regulatory_theme_label_covers_government_action_not_only_antitrust() -> None:
    """The theme also carries state action such as a government procurement ban, so the
    displayed wording must not narrow it to antitrust. The enum value is unchanged."""

    assert theme_label(RiskTheme.REGULATORY_ANTITRUST) == "Regulatory & government action"
    assert RiskTheme.REGULATORY_ANTITRUST.value == "regulatory_antitrust"


# ------------------------------------------------- data-gated recall additions (fresh v5 text)

# Verbatim negative-channel text observed in the fresh Stage A v5 AAPL/MSFT analyses. Each of
# these mapped to UNMAPPED and was therefore silently dropped from Top Risks.
_OBSERVED_V5_CHANNELS: tuple[tuple[str, RiskTheme], ...] = (
    (
        "Reduced product availability may lead to lower revenue.",
        RiskTheme.SUPPLY_CONSTRAINT,
    ),
    (
        "Potential decrease in revenue from government contracts in China.",
        RiskTheme.DEMAND_SLOWDOWN,
    ),
    (
        "Increased competition from local software providers as a result of the ban.",
        RiskTheme.COMPETITIVE_PRESSURE,
    ),
)


@pytest.mark.parametrize(("mechanism", "theme"), _OBSERVED_V5_CHANNELS)
def test_observed_v5_channels_reach_their_theme(mechanism: str, theme: RiskTheme) -> None:
    assert theme_for_mechanism(mechanism) is theme


# Mechanisms taken verbatim from stored NVDA analyses that reached no theme because the phrasing
# ran the other way round from the existing patterns ("loss of market share" rather than "market
# share loss", "disruption in supply chain" rather than "supply disruption").
_PREVIOUSLY_UNMAPPED_CHANNELS: tuple[tuple[str, RiskTheme], ...] = (
    (
        "Potential loss of market share in the AI chip sector in China.",
        RiskTheme.COMPETITIVE_PRESSURE,
    ),
    (
        "Potential loss of competitive advantage in the Chinese market.",
        RiskTheme.COMPETITIVE_PRESSURE,
    ),
    (
        "Potential loss of revenue from reduced sales in the Chinese market.",
        RiskTheme.DEMAND_SLOWDOWN,
    ),
    (
        "Disruption in supply chain relationships with Chinese partners.",
        RiskTheme.SUPPLY_CONSTRAINT,
    ),
    (
        "Limited supply may restrict Nvidia's ability to meet demand in the Chinese market.",
        RiskTheme.SUPPLY_CONSTRAINT,
    ),
    (
        "Potential delays in future shipments could impact revenue from the region.",
        RiskTheme.SUPPLY_CONSTRAINT,
    ),
)


@pytest.mark.parametrize(("mechanism", "theme"), _PREVIOUSLY_UNMAPPED_CHANNELS)
def test_recovered_phrasings_reach_their_theme(mechanism: str, theme: RiskTheme) -> None:
    assert theme_for_mechanism(mechanism) is theme


@pytest.mark.parametrize(
    "mechanism",
    [
        # Also observed unmapped, and deliberately left that way. Perception, another company's
        # exposure, and a partnership that never existed are not this company's risk mechanisms.
        "Loss of investor confidence could affect Supermicro's market position.",
        "Potential legal penalties could impact Supermicro's operations and financial stability.",
        (
            "Loss of a significant potential partnership that could have enhanced Nvidia's "
            "market position in AI."
        ),
        "Potential delays in market entry for AI products in China could affect revenue growth.",
        # Near misses for the recovered patterns: the object must be share or advantage, the
        # contraction must name a market, and a delay must concern shipments.
        "Loss of a board member with semiconductor experience.",
        "Reduced sales commissions lower operating expenses.",
        "Delays in the annual developer conference agenda.",
    ],
)
def test_recovered_patterns_do_not_overmatch(mechanism: str) -> None:
    assert theme_for_mechanism(mechanism) is RiskTheme.UNMAPPED


@pytest.mark.parametrize(
    "mechanism",
    [
        # "product" is mandatory: availability of credit, financing, or labour is not a product
        # supply constraint, and treating it as one would invent an exposure the text never states.
        "Lower availability of credit may increase financing costs.",
        "Reduced availability of financing could constrain investment.",
        "Constrained availability of skilled labour may increase hiring costs.",
        # Scarcity used as a pricing positive, and ordinary improvements.
        "Limited availability of the anniversary edition supports premium pricing.",
        "Improved product availability after the second plant opened.",
        "Greater availability of developer tooling accelerates adoption.",
    ],
)
def test_availability_without_product_context_is_not_a_supply_constraint(mechanism: str) -> None:
    assert theme_for_mechanism(mechanism) is not RiskTheme.SUPPLY_CONSTRAINT


@pytest.mark.parametrize(
    "mechanism",
    [
        # A fall in revenue does not prove a demand mechanism. These are supply-, FX-, execution-
        # and pricing-driven, so none of them may be reported as a demand slowdown.
        "Decline in revenue due to supply shortages.",
        "Decrease in revenue due to foreign-exchange headwinds.",
        "Drop in sales following a product recall.",
        "Fall in revenue due to lower average selling prices.",
        # A reduction in a cost line is a margin improvement, not a downside mechanism.
        "A reduction in cost of sales improves gross margin.",
        # Growth phrasing must never reach a downside theme.
        "An increase in revenue from services.",
        "Potential for higher revenue from next-generation products.",
    ],
)
def test_revenue_contraction_without_customer_context_is_not_a_demand_slowdown(
    mechanism: str,
) -> None:
    assert theme_for_mechanism(mechanism) is not RiskTheme.DEMAND_SLOWDOWN


@pytest.mark.parametrize(
    "mechanism",
    [
        # "competitive"/"competitiveness" describe the company's own strength, not rival pressure.
        "Positive financial results could enhance Microsoft's competitive position in the market.",
        "Enhanced design capabilities could improve product performance and competitiveness.",
        # Competition falling away is not competitive pressure.
        "Reduced competition following the merger.",
    ],
)
def test_competition_wording_without_an_intensifier_is_not_competitive_pressure(
    mechanism: str,
) -> None:
    assert theme_for_mechanism(mechanism) is not RiskTheme.COMPETITIVE_PRESSURE


def test_competition_authority_still_outranks_the_new_competition_trigger() -> None:
    """Theme order must keep a regulator ahead of the intensified-competition addition."""

    mechanism = "A greater competition authority probe into bundling."
    assert theme_for_mechanism(mechanism) is RiskTheme.REGULATORY_ANTITRUST


# --------------------------------------------------------------------------- cyber precision


@pytest.mark.parametrize(
    "mechanism",
    [
        "Ransomware could disrupt customer systems",
        "a data breach exposed customer records",
        "a cyberattack disrupted order processing",
        "a cyber attack took the storefront offline",
        "malware was found on production build servers",
        "Hackers are exploiting a vulnerability in Teams",
        "Potential loss of user trust due to security vulnerabilities",
        "unauthorised access to an internal administrative console",
    ],
)
def test_cybersecurity_requires_a_threat_compromise_or_vulnerability(mechanism: str) -> None:
    assert theme_for_mechanism(mechanism) is RiskTheme.CYBERSECURITY


@pytest.mark.parametrize(
    "mechanism",
    [
        "Increased regulatory scrutiny on Microsoft regarding cybersecurity measures",
        "New cybersecurity regulation increases compliance costs",
        "cybersecurity certification requirements add compliance burden",
    ],
)
def test_regulatory_mechanisms_about_cybersecurity_stay_regulatory(mechanism: str) -> None:
    """The bare topic must not win the theme: only an actual incident is cybersecurity risk."""

    assert theme_for_mechanism(mechanism) is RiskTheme.REGULATORY_ANTITRUST


def test_commercial_exploitation_is_not_a_cybersecurity_mechanism() -> None:
    """The word exploit also means taking commercial advantage, so it needs a vulnerability."""

    assert theme_for_mechanism("competitors may exploit the delay to win share") is not (
        RiskTheme.CYBERSECURITY
    )


# ------------------------------------------------- realized negative cyber-incident override


@pytest.mark.parametrize(
    "summary",
    [
        "Hackers are exploiting Microsoft Teams in a ransomware campaign, posing as fake IT support.",
        "A data breach exposed several million customer records held by the company.",
        "A cyberattack forced the company to take order processing offline.",
        "A cyber attack disrupted European logistics for two days.",
        "Malware discovered on build servers halted software releases.",
    ],
)
def test_regulation_summary_describing_a_cyber_incident_is_rethemed(summary: str) -> None:
    assert (
        theme_for_realized_negative_event(EventType.REGULATION, summary) is RiskTheme.CYBERSECURITY
    )


@pytest.mark.parametrize(
    "summary",
    [
        "Regulators introduced new cybersecurity rules for critical infrastructure operators.",
        "The company must meet new cybersecurity measures under an updated regulation.",
        "The EU opened a formal antitrust investigation into the company's bundling practices.",
        "The company received a vague notice from authorities; details were not disclosed.",
        "",
    ],
)
def test_regulation_summary_without_a_cyber_incident_keeps_the_regulatory_theme(
    summary: str,
) -> None:
    assert (
        theme_for_realized_negative_event(EventType.REGULATION, summary)
        is RiskTheme.REGULATORY_ANTITRUST
    )


@pytest.mark.parametrize(
    ("event_type", "theme"),
    [
        (EventType.LITIGATION, RiskTheme.LEGAL_LITIGATION),
        (EventType.SUPPLY_DISRUPTION, RiskTheme.SUPPLY_CONSTRAINT),
        (EventType.MANAGEMENT_CHANGE, RiskTheme.KEY_PERSON_MANAGEMENT),
        (EventType.CONTRACT_LOSS, RiskTheme.DEMAND_SLOWDOWN),
        (EventType.MACROECONOMIC_EXPOSURE, RiskTheme.MACRO_GEOGRAPHIC),
        (EventType.ANALYST_OR_GUIDANCE_CHANGE, RiskTheme.GUIDANCE_VALUATION),
    ],
)
def test_other_event_types_are_never_rethemed_by_an_incidental_cyber_mention(
    event_type: EventType, theme: RiskTheme
) -> None:
    """The override is scoped to REGULATION only; every other event type is left alone."""

    summary = (
        "The filing referenced an unrelated ransomware campaign and a data breach at a supplier, "
        "alongside a cyberattack reported elsewhere in the industry."
    )

    assert theme_for_realized_negative_event(event_type, summary) is theme


def test_override_default_argument_preserves_the_plain_event_type_mapping() -> None:
    assert theme_for_realized_negative_event(EventType.REGULATION) is RiskTheme.REGULATORY_ANTITRUST
    assert theme_for_realized_negative_event(EventType.EARNINGS, "ransomware") is None
