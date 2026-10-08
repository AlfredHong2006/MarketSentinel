"""The role-stage caps on the coverage CLI and the scheduled workflow: all default to zero.

No `.env` is read and nothing is built for real: settings are constructed with the env file
disabled and the service is replaced by a stub, so no provider, database, or network is reachable.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from marketsentinel.company_role import CompanyRoleStore
from marketsentinel.config import Settings
from marketsentinel.storage.sqlite import SQLiteRepository
from scripts import run_coverage_cycle
from scripts.run_coverage_cycle import (
    build_parser,
    role_budget_from,
    role_tickers_from,
    validate_arguments,
)

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "coverage.yml"


def parse(*argv: str):
    parser = build_parser()
    arguments = parser.parse_args(list(argv))
    validate_arguments(parser, arguments)
    return arguments


# --- command line ---------------------------------------------------------------------------------


def test_every_role_cap_defaults_to_zero_and_the_stage_is_off():
    arguments = parse("--ticker", "NVDA")

    assert (
        arguments.max_new_roles,
        arguments.max_new_roles_total,
        arguments.max_backfill_roles,
    ) == (
        0,
        0,
        0,
    )
    assert not role_budget_from(arguments).enabled


def test_positive_caps_enable_the_budget_and_negative_caps_are_rejected():
    arguments = parse(
        "--ticker",
        "NVDA",
        "--max-new-roles",
        "3",
        "--max-new-roles-total",
        "5",
        "--max-backfill-roles",
        "100",
        "--role-tickers",
        "NVDA",
    )
    budget = role_budget_from(arguments)
    assert (budget.max_new_per_ticker, budget.max_new_total, budget.max_backfill_total) == (
        3,
        5,
        100,
    )
    assert budget.enabled

    for flag in ("--max-new-roles", "--max-new-roles-total", "--max-backfill-roles"):
        with pytest.raises(SystemExit):
            parse("--ticker", "NVDA", flag, "-1")


def test_the_role_caps_only_apply_to_a_cycle():
    with pytest.raises(SystemExit):
        parse("--mode", "status", "--ticker", "NVDA", "--max-backfill-roles", "5")


@pytest.mark.parametrize(
    "caps",
    [
        ["--max-backfill-roles", "5"],
        ["--max-new-roles", "2", "--max-new-roles-total", "4"],
    ],
)
@pytest.mark.parametrize("tickers", [[], ["--role-tickers", ""], ["--role-tickers", " , "]])
def test_a_positive_role_cap_without_a_ticker_list_is_refused_with_a_message(caps, tickers, capsys):
    with pytest.raises(SystemExit) as refused:
        parse("--ticker", "NVDA", *caps, *tickers)

    assert refused.value.code == 2
    assert "needs --role-tickers" in capsys.readouterr().err


def test_a_refused_run_never_builds_the_service(monkeypatch):
    # The refusal happens in argument validation, before settings, database or provider exist.
    def boom(*args, **kwargs):
        raise AssertionError("the service must not be built")

    monkeypatch.setattr(run_coverage_cycle, "build_coverage_service", boom)
    monkeypatch.setattr(run_coverage_cycle, "get_settings", boom)

    with pytest.raises(SystemExit) as refused:
        run_coverage_cycle.main(["--all-active", "--max-backfill-roles", "5"])

    assert refused.value.code == 2


def test_the_role_ticker_list_is_normalised_and_optional_when_every_cap_is_zero():
    assert role_tickers_from(parse("--ticker", "NVDA")) == []
    assert role_tickers_from(parse("--ticker", "NVDA", "--role-tickers", "NVDA")) == ["NVDA"]
    arguments = parse(
        "--all-active", "--max-backfill-roles", "5", "--role-tickers", " nvda, PFE ,nvda,"
    )
    assert role_tickers_from(arguments) == ["NVDA", "PFE"]
    with pytest.raises(SystemExit):
        parse("--mode", "status", "--ticker", "NVDA", "--role-tickers", "NVDA")


class _RoleStoreRepository:
    """Satisfies CompanyRoleStore and lists coverage; nothing else, and no database."""

    def __init__(self, covered: list[str]) -> None:
        self.covered = covered

    def get_company_role(self, *args): ...

    def store_company_role(self, label): ...

    def list_company_roles(self, *args): ...

    def list_company_coverage(self):
        return [SimpleNamespace(ticker=t, active=True) for t in self.covered]

    def list_ingestion_watermarks(self, ticker):
        return []


def test_main_passes_the_list_to_the_cycle_and_reports_the_scope(monkeypatch, capsys):
    seen = {}

    class StubService:
        repository = _RoleStoreRepository(["NVDA", "PFE", "AMD"])

        def run_all(self, tickers, **kwargs):
            seen.update(kwargs)
            return []

    monkeypatch.setattr(run_coverage_cycle, "get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(run_coverage_cycle, "build_coverage_service", lambda *a, **k: StubService())

    code = run_coverage_cycle.main(
        ["--all-active", "--max-backfill-roles", "5", "--role-tickers", "NVDA,PFE,GHOST"]
    )

    assert code == 0
    assert seen["role_tickers"] == ["NVDA", "PFE", "GHOST"]
    assert seen["role_budget"].max_backfill_total == 5
    assert (
        "role scope: labelled tickers=NVDA, PFE; covered tickers left out=AMD; "
        "listed but not covered=GHOST"
    ) in capsys.readouterr().out


def test_main_prints_no_scope_line_when_the_role_stage_is_off(monkeypatch, capsys):
    class StubService:
        repository = object()

        def run_all(self, tickers, **kwargs):
            return []

    monkeypatch.setattr(run_coverage_cycle, "get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(run_coverage_cycle, "build_coverage_service", lambda *a, **k: StubService())

    assert run_coverage_cycle.main(["--ticker", "NVDA", "--role-tickers", "NVDA"]) == 0
    assert "role scope" not in capsys.readouterr().out


def test_main_refuses_a_positive_cap_before_any_spend_when_storage_is_missing(monkeypatch, capsys):
    # A positive cap needs somewhere to store labels. A repository without a role store must fail
    # the run closed instead of paying and then failing to save.
    stub = SimpleNamespace(repository=object())
    assert not isinstance(stub.repository, CompanyRoleStore)
    monkeypatch.setattr(run_coverage_cycle, "get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(run_coverage_cycle, "build_coverage_service", lambda *a, **k: stub)

    code = run_coverage_cycle.main(
        ["--ticker", "ACME", "--max-backfill-roles", "5", "--role-tickers", "ACME"]
    )

    assert code == 2
    assert "role-label storage" in capsys.readouterr().err


def test_the_real_repository_provides_role_storage():
    assert isinstance(SQLiteRepository(Path("data/test-runtime/unused.db")), CompanyRoleStore)


def test_main_passes_a_disabled_budget_by_default(monkeypatch):
    seen = {}

    class StubService:
        repository = object()

        def run_all(self, tickers, **kwargs):
            seen.update(kwargs)
            return []

    monkeypatch.setattr(run_coverage_cycle, "get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(run_coverage_cycle, "build_coverage_service", lambda *a, **k: StubService())

    assert run_coverage_cycle.main(["--ticker", "ACME"]) == 0
    assert seen["role_budget"].enabled is False


def test_the_role_model_setting_is_optional_and_falls_back_to_the_main_model():
    settings = Settings(_env_file=None)
    assert settings.company_role_model is None
    assert Settings(_env_file=None, company_role_model="  ").company_role_model is None


# --- the scheduled workflow -----------------------------------------------------------------------


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def steps(workflow: dict) -> list[dict]:
    return workflow["jobs"]["coverage"]["steps"]


def step_index(workflow: dict, name: str) -> int:
    return next(i for i, step in enumerate(steps(workflow)) if step.get("name") == name)


def test_the_workflow_inputs_for_the_role_stage_default_to_zero(workflow):
    # PyYAML reads the bare key `on` as boolean True.
    inputs = (workflow.get("on") or workflow[True])["workflow_dispatch"]["inputs"]
    for name in ("max_new_roles", "max_new_roles_total", "max_backfill_roles"):
        assert inputs[name]["default"] == "0"


def test_a_scheduled_run_with_no_inputs_falls_back_to_zero_role_caps(workflow):
    env = workflow["env"]
    for name in ("MAX_NEW_ROLES", "MAX_NEW_ROLES_TOTAL", "MAX_BACKFILL_ROLES"):
        assert env[name].endswith("|| '0'}}") or env[name].endswith("|| '0' }}")


def test_only_the_cycle_step_receives_the_role_caps_and_it_runs_before_the_checkpoint(workflow):
    cycle = step_index(workflow, "Run bounded coverage cycle")
    gate = step_index(workflow, "Validate private database integrity (after the coverage cycle)")
    checkpoint = step_index(workflow, "Checkpoint private state to R2 (after the coverage cycle)")
    snapshot = step_index(workflow, "Build sanitized public snapshot")

    assert cycle < gate < checkpoint < snapshot  # paid labels reach R2 before any public step
    for number, step in enumerate(steps(workflow)):
        text = step.get("run", "")
        has_role_flags = "--max-new-roles" in text or "--max-backfill-roles" in text
        assert has_role_flags == (number == cycle)
    command = steps(workflow)[cycle]["run"]
    assert '--max-new-roles "$MAX_NEW_ROLES"' in command
    assert '--max-new-roles-total "$MAX_NEW_ROLES_TOTAL"' in command
    assert '--max-backfill-roles "$MAX_BACKFILL_ROLES"' in command


def test_the_workflow_passes_the_dispatch_tickers_to_the_cycle_step_only(workflow):
    cycle = step_index(workflow, "Run bounded coverage cycle")
    assert '--role-tickers "$TICKERS"' in steps(workflow)[cycle]["run"]
    assert "--all-active" in steps(workflow)[cycle]["run"]  # the cycle itself is unchanged
    for number, step in enumerate(steps(workflow)):
        assert ("--role-tickers" in step.get("run", "")) == (number == cycle)
    assert workflow["env"]["TICKERS"] == "${{ github.event.inputs.tickers || 'NVDA,PFE' }}"
    inputs = (workflow.get("on") or workflow[True])["workflow_dispatch"]["inputs"]
    assert inputs["tickers"]["default"] == "NVDA,PFE"


def test_the_workflow_says_why_a_scheduled_run_uses_the_default_ticker_list():
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    comments = " ".join(line.strip().lstrip("#").strip() for line in lines if "#" in line)
    assert comments.count("a company activated by a public request gets no role labels") == 2


def test_the_workflow_gives_the_public_side_no_new_credential_or_spend_path(workflow):
    secrets_by_step = {
        step.get("name"): sorted(
            value for value in (step.get("env") or {}).values() if "OPENAI_API_KEY" in str(value)
        )
        for step in steps(workflow)
    }
    holders = [name for name, values in secrets_by_step.items() if values]
    # The key is still handed only to the admission and cycle steps, exactly as before.
    assert holders == ["Admit public requests", "Run bounded coverage cycle"]
