"""Test support for the company-role stage. Nothing here can reach a provider or a real database.

Labels live in the real `article_company_roles` table of a throwaway `SQLiteRepository`;
`stored_labels` reads that table back so tests can assert what was persisted.
"""

from collections.abc import Sequence
from contextlib import closing

from marketsentinel.company_role import CompanyRoleRequest
from marketsentinel.domain import CompanyRole, CompanyRoleExtraction, CompanyRoleLabel
from marketsentinel.storage.sqlite import SQLiteRepository, _row_to_company_role


def stored_labels(repository: SQLiteRepository) -> list[CompanyRoleLabel]:
    """Every label in the real `article_company_roles` table, for asserting what was persisted."""

    with closing(repository._connect()) as connection:
        rows = connection.execute("SELECT * FROM article_company_roles").fetchall()
    return [label for row in rows if (label := _row_to_company_role(row)) is not None]


class ScriptedRoleProvider:
    """Deterministic provider double: the role is looked up by headline, failures on demand.

    It records every request, so tests can assert what was (and was not) sent. It says nothing
    about how a real model would label an article.
    """

    model_version = "role-test-model"

    def __init__(
        self,
        roles: dict[str, CompanyRole] | None = None,
        *,
        default: CompanyRole = CompanyRole.PRINCIPAL,
        failures: Sequence[Exception] = (),
        always: Exception | None = None,
        symbol: str | None = "ACME",
    ) -> None:
        self.roles = roles or {}
        self.default = default
        self.failures = list(failures)
        self.always = always
        self.symbol = symbol
        self.requests: list[CompanyRoleRequest] = []
        self.last_usage: dict[str, tuple[int | None, int | None]] = {}

    @property
    def calls(self) -> int:
        return len(self.requests)

    def extract_role(self, request: CompanyRoleRequest) -> CompanyRoleExtraction:
        self.requests.append(request)
        if self.always is not None:
            raise self.always
        if self.failures:
            raise self.failures.pop(0)
        self.last_usage["role"] = (300, 40)
        return CompanyRoleExtraction(
            subject_symbol=self.symbol or request.subject_company.symbol,
            role=self.roles.get(request.title, self.default),
            confidence=0.9,
            rationale="Scripted test rationale.",
        )
