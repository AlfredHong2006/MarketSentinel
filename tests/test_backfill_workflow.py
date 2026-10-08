"""MR-009: the manual-dispatch backfill step in the scheduled coverage workflow (structure only)."""

import re
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "coverage.yml"
BACKFILL_STEP = "Historical backfill to the stored start (manual dispatch only)"
SCRIPT = "scripts/backfill_historical_intelligence.py"


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def steps(workflow: dict) -> list[dict]:
    return workflow["jobs"]["coverage"]["steps"]


def step_index(workflow: dict, name: str) -> int:
    return next(i for i, step in enumerate(steps(workflow)) if step.get("name") == name)


def backfill_step(workflow: dict) -> dict:
    return steps(workflow)[step_index(workflow, BACKFILL_STEP)]


def dispatch_inputs(workflow: dict) -> dict:
    # PyYAML reads the bare key `on` as boolean True.
    return (workflow.get("on") or workflow[True])["workflow_dispatch"]["inputs"]


def test_the_dispatch_takes_one_ticker_and_an_optional_plan_only_flag(workflow):
    inputs = dispatch_inputs(workflow)

    assert inputs["backfill_ticker"]["default"] == ""
    assert inputs["backfill_plan_only"]["type"] == "boolean"
    assert inputs["backfill_plan_only"]["default"] is False


def test_the_step_runs_only_on_a_manual_dispatch_that_sets_the_ticker(workflow):
    condition = backfill_step(workflow)["if"]

    assert "github.event_name == 'workflow_dispatch'" in condition
    assert "github.event.inputs.backfill_ticker != ''" in condition
    # A scheduled run has no event inputs and a different event name: both halves fail.
    assert "schedule" not in condition


def test_only_that_step_runs_the_backfill_and_nothing_else_reads_its_inputs(workflow):
    for number, step in enumerate(steps(workflow)):
        text = str(step)
        is_backfill = number == step_index(workflow, BACKFILL_STEP)
        assert (SCRIPT in text) == is_backfill
        assert ("backfill_ticker" in text) == is_backfill
        assert ("backfill_plan_only" in text) == is_backfill


def test_the_step_gets_no_secret_and_so_no_openai_key(workflow):
    step = backfill_step(workflow)

    assert "secrets." not in str(step)
    assert "OPENAI" not in str(step).upper()
    assert set(step["env"]) == {"BACKFILL_TICKER", "BACKFILL_PLAN_ONLY"}
    # The key is still handed only to admission and the cycle, exactly as before.
    holders = [
        s.get("name")
        for s in steps(workflow)
        if any("OPENAI_API_KEY" in str(value) for value in (s.get("env") or {}).values())
    ]
    assert holders == ["Admit public requests", "Run bounded coverage cycle"]


def test_the_step_runs_before_admission_the_cycle_and_every_checkpoint(workflow):
    backfill = step_index(workflow, BACKFILL_STEP)
    activate = step_index(workflow, "Activate coverage")
    admission = step_index(workflow, "Admit public requests")
    first_checkpoint = step_index(
        workflow, "Checkpoint private state to R2 (after public requests)"
    )
    cycle = step_index(workflow, "Run bounded coverage cycle")
    gate = step_index(workflow, "Validate private database integrity (after the coverage cycle)")
    checkpoint = step_index(workflow, "Checkpoint private state to R2 (after the coverage cycle)")
    snapshot = step_index(workflow, "Build sanitized public snapshot")

    assert activate < backfill < admission < first_checkpoint < cycle < gate < checkpoint < snapshot


def test_the_step_passes_the_five_arguments(workflow):
    command = backfill_step(workflow)["run"]

    for argument in (
        "--months 36",
        "--until-stored-start",
        "--google-only",
        "--max-new-analyses 0",
        "--request-interval-seconds 5.25",
    ):
        assert argument in command
    assert '--ticker "$BACKFILL_TICKER"' in command
    # No other boundary or spend option can ride along.
    for forbidden in ("--skip-recent-months", "--until ", "--bucket-candidate-cap", "--mode"):
        assert forbidden not in command


def test_the_ticker_reaches_the_shell_only_through_the_environment(workflow):
    command = backfill_step(workflow)["run"]

    assert "${{" not in command, "an input expanded into the script text could inject shell"


def test_a_value_with_more_than_one_ticker_is_rejected_before_the_script_runs(workflow):
    command = backfill_step(workflow)["run"]

    pattern = re.search(r"=~ \^(?P<body>.+)\$\s*\]\]", command)
    assert pattern is not None
    assert command.index("=~") < command.index(SCRIPT)
    valid = re.compile(pattern.group("body"))
    assert valid.fullmatch("NVDA") and valid.fullmatch("BRK.B")
    for bad in ("NVDA,PFE", "NVDA PFE", "NVDA, PFE", ",NVDA", "NVDA,", "", "NVDA;ls"):
        assert valid.fullmatch(bad) is None
    assert "exit 1" in command[: command.index(SCRIPT)]


def test_plan_only_adds_the_flag_and_then_stops_the_job_on_purpose(workflow):
    command = backfill_step(workflow)["run"]

    assert "ARGS+=(--plan-only)" in command
    after_script = command[command.index(SCRIPT) :]
    stop = after_script[after_script.index('"$BACKFILL_PLAN_ONLY" = "true"') :]
    assert "exit 1" in stop, (
        "a green plan-only run would let admission, the cycle and a checkpoint write"
    )
    assert "no later step ran" in stop


def test_a_real_backfill_dispatch_must_have_every_paid_cap_at_zero(workflow):
    command = backfill_step(workflow)["run"]
    guard = command[: command.index(SCRIPT)]

    for cap in (
        "MAX_NEW_TOTAL",
        "MAX_ARTICLE_REQUESTS",
        "MAX_NEW_ROLES_TOTAL",
        "MAX_BACKFILL_ROLES",
    ):
        assert cap in guard
    assert '!= "0"' in guard and "exit 1" in guard
    # The guard sits in the non-plan-only branch, so a plan-only dispatch does not need the caps.
    assert '"$BACKFILL_PLAN_ONLY" != "true"' in guard


def test_the_cycle_step_still_takes_every_cap_from_the_environment(workflow):
    names = [step.get("name") for step in steps(workflow)]
    cycle = steps(workflow)[step_index(workflow, "Run bounded coverage cycle")]["run"]

    assert names.count(BACKFILL_STEP) == 1
    for flag, variable in (
        ("--max-new", "MAX_NEW"),
        ("--max-new-total", "MAX_NEW_TOTAL"),
        ("--max-new-roles-total", "MAX_NEW_ROLES_TOTAL"),
        ("--max-backfill-roles", "MAX_BACKFILL_ROLES"),
    ):
        assert f'{flag} "${variable}"' in cycle
