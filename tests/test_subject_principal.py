"""Semantics of the shared subject-principal rule.

The rule these tests pin is the settled one: an article is a development for the subject company
when that company is a principal to the underlying event, and which company holds the headline's
subject position decides nothing. So the keep cases matter at least as much as the reject case --
every one of them names another company first, and every one of them is still a Pfizer
development. Nothing here reads a database or a model.
"""

import pytest

from marketsentinel.domain import CompanyReference
from marketsentinel.subject_principal import reads_as_third_party_appointment

PFIZER = CompanyReference(symbol="PFE", name="Pfizer")

# The leak this rule exists for: the subject company named only as the employer of the person
# being hired somewhere else.
THIRD_PARTY_APPOINTMENTS = (
    "Watch Nike Appoints Pfizer CFO as New Finance Chief Amid Industry Experience Questions",
    "Nike appoints Pfizer CFO as turnaround enters next phase",
    "Nike names Pfizer's finance chief as its new CFO",
    "Nike taps former Pfizer executive as new chief financial officer",
    "Nike hires ex-Pfizer scientist as its new head of research",
    "Boeing recruits Pfizer's general counsel to be its next chief legal officer",
)

# Another company leads the headline in every one of these, and the subject company is still a
# principal: seller, defendant, rival bidder, or counterparty.
SUBJECT_IS_A_PRINCIPAL = (
    "Shionogi Acquires $2.1 Billion ViiV Healthcare Shareholdings from Pfizer",
    "ViiV ownership shifts as Pfizer sells its stake",
    "Pfizer exits ViiV, selling stake to Shionogi in $2.1B deal",
    "Pfizer to exit ViiV Healthcare in $1.9 billion deal as Shionogi doubles stake",
    "Sanofi sues Pfizer and Moderna for patent infringements related to COVID vaccines",
    "Sanofi sues Pfizer, Moderna over COVID shot technology",
    "Sanofi Sues Moderna, Pfizer for Vaccine-Tech Patent Royalties",
    "Novo Nordisk Seeks to Outmuscle Pfizer With $9 Billion Bid for Metsera",
    "Novo Nordisk said to make higher bid for Metsera to challenge Pfizer, Bloomberg reports",
    "Novo Nordisk changes tack with bold raid on Pfizer obesity deal",
    "Metsera the US obesity biotech at centre of Novo, Pfizer bidding war",
    "Adaptive signs deal with Pfizer worth up to $890 million for arthritis research",
)

# Personnel wording that is the subject company's own story, so the rule must leave it alone.
SUBJECT_PERSONNEL_EVENTS = (
    "Pfizer appoints new chief financial officer as turnaround enters next phase",
    "Pfizer names Jane Doe as its new CFO",
    "Pfizer hires ex-Merck executive as new head of research",
    "Pfizer taps a banker to lead its obesity deal",
    "Pfizer CFO named in bribery indictment",
    "Regulators name Pfizer executive in probe",
    "Pfizer CFO to step down as finance chief",
    "Pfizer CFO promoted to chief executive",
    "Pfizer CFO hires new deputy for the finance team",
    "Pfizer Is Cutting Hundreds of Jobs in Switzerland to Lower Costs",
)


@pytest.mark.parametrize("title", THIRD_PARTY_APPOINTMENTS)
def test_another_organisation_hiring_a_subject_executive_is_not_a_subject_event(
    title: str,
) -> None:
    assert reads_as_third_party_appointment(title, PFIZER)


@pytest.mark.parametrize("title", SUBJECT_IS_A_PRINCIPAL)
def test_a_third_party_headline_subject_never_disqualifies_a_principal(title: str) -> None:
    """The rule that was rejected in favour of this one: leading with another company's name."""

    assert not reads_as_third_party_appointment(title, PFIZER)


@pytest.mark.parametrize("title", SUBJECT_PERSONNEL_EVENTS)
def test_the_company_own_personnel_news_is_untouched(title: str) -> None:
    assert not reads_as_third_party_appointment(title, PFIZER)


def test_the_rule_needs_an_appointment_and_not_merely_a_person_descriptor() -> None:
    """Both halves are load-bearing, so each is checked with the other held constant."""

    # A person descriptor with no appointment at all.
    assert not reads_as_third_party_appointment("Pfizer CFO warns on drug pricing", PFIZER)
    # An appointment in which the subject is never named as anyone's employer.
    assert not reads_as_third_party_appointment("Nike appoints a new finance chief", PFIZER)


def test_one_mention_of_the_company_acting_restores_its_own_reading() -> None:
    """One mention of the company acting is the safeguard: it owns the story again."""

    assert not reads_as_third_party_appointment(
        "Nike appoints Pfizer CFO as finance chief while Pfizer starts its own search",
        PFIZER,
    )


def test_an_unnamed_company_is_never_incidental_by_default() -> None:
    """A title that never names the subject must not satisfy the rule vacuously."""

    assert not reads_as_third_party_appointment("Nike appoints Merck CFO as finance chief", PFIZER)


def test_the_ticker_counts_as_naming_the_company() -> None:
    """A ticker reference is the company acting, so it blocks the person-descriptor reading."""

    assert not reads_as_third_party_appointment(
        "Nike appoints Pfizer CFO as finance chief, PFE confirms the departure", PFIZER
    )
