"""Automation policy for ordinary, scheduled, and dependency-update changes."""

import re
import shutil
import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
CI = ROOT / ".github" / "workflows" / "ci.yml"
DEPENDABOT = ROOT / ".github" / "dependabot.yml"
just_executable = shutil.which("just")
if just_executable is None:
    raise RuntimeError("just is required to validate the recipe graph")
JUST: str = just_executable


def dry_run(*recipes: str) -> list[str]:
    result: subprocess.CompletedProcess[str] = subprocess.run(
        (JUST, "--dry-run", *recipes),
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stderr.splitlines()


def test_ci_uses_immutable_public_automation() -> None:
    text = CI.read_text()
    references = re.findall(r"uses:\s+groovemap-music/automation/[^@\s]+@([^\s]+)", text)
    assert references
    assert all(re.fullmatch(r"[0-9a-f]{40}", reference) for reference in references)
    assert "@main" not in text


def test_ci_runs_the_same_required_graph_for_every_pull_request() -> None:
    text = CI.read_text()
    assert "pull_request:" in text
    assert "pull_request_target:" not in text
    assert "github.actor" not in text
    assert "dependabot[bot]" not in text
    assert re.search(r"^  required:\n", text, re.MULTILINE)

    for required_input in (
        "setup-command",
        "check-command",
        "coverage-command",
        "audit-command",
        "license-command",
        "secret-scan-command",
        "package-command",
        "install-command",
        "coverage-files",
    ):
        assert f"{required_input}:" in text


def test_ci_runs_scheduled_security_and_validation_checks() -> None:
    text = CI.read_text()
    assert "schedule:" in text
    assert text.count("cron:") == 2
    assert "audit-command: just audit" in text
    assert "secret-scan-command: just secret-scan" in text
    assert "license-command: just license-check" in text


def test_dependabot_covers_every_repository_ecosystem_with_ci_labels() -> None:
    config = yaml.safe_load(DEPENDABOT.read_text())
    updates = config["updates"]
    assert {update["package-ecosystem"] for update in updates} == {"docker-compose", "github-actions", "uv"}
    for update in updates:
        assert "dependencies" in update["labels"]
        assert update["open-pull-requests-limit"] > 0


def test_retired_automation_is_absent() -> None:
    active_files = [*ROOT.glob("renovate*"), *(ROOT / ".github" / "workflows").glob("*claude*")]
    assert not active_files


def test_recipe_dag_preserves_the_required_validation_graph() -> None:
    expected = [
        "uvx --from ruff==0.16.4 ruff format --check .",
        "uvx --from ruff==0.16.4 ruff check .",
        "uv run python scripts/check-images.py",
        "uv run python scripts/check-dashboards.py",
        "uv run python scripts/check-licenses.py",
        'uv run pip-licenses --fail-on "GPL-2.0-only;GPL-3.0-only;AGPL-3.0-only"',
        "bash scripts/check-compose.sh",
        "gitleaks git --config .gitleaks.toml --redact --no-banner",
        "gitleaks dir . --config .gitleaks.toml --redact --no-banner",
        "uv run mypy",
        "uv run pytest --cov=scripts --cov-report=term-missing --cov-report=xml",
    ]
    assert dry_run("check") == expected


def test_public_recipe_surface_remains_stable() -> None:
    result: subprocess.CompletedProcess[str] = subprocess.run(
        (JUST, "--summary"),
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert set(result.stdout.split()) == {
        "audit",
        "build",
        "check",
        "config",
        "config-prod",
        "coverage",
        "default",
        "down",
        "install-check",
        "license-check",
        "performance",
        "secret-scan",
        "secrets-bootstrap",
        "setup",
        "smoke",
        "smoke-erasure",
        "smoke-infra",
        "smoke-media",
        "smoke-released-fixture",
        "smoke-released",
        "source-check",
        "test",
        "typecheck",
    }


def test_equivalent_public_recipes_share_one_command_body() -> None:
    justfile = (ROOT / "Justfile").read_text()
    test_command = "uv run pytest --cov=scripts --cov-report=term-missing --cov-report=xml"
    compose_command = "bash scripts/check-compose.sh"

    assert justfile.count(test_command) == 1
    assert justfile.count(compose_command) == 1
    assert dry_run("test") == dry_run("coverage") == [test_command]
    assert dry_run("build") == dry_run("install-check") == [compose_command]


def test_live_network_and_stateful_recipes_stay_outside_check() -> None:
    expanded_check = set(dry_run("check"))
    outside_check = {
        "uv run pip-audit",
        "docker compose config",
        "docker compose -f docker-compose.yml -f docker-compose.prod.yml config",
        "bash scripts/create-secrets.sh",
        "bash scripts/run-perftest.sh",
        "docker compose up -d --wait",
        "docker compose ps",
        "bash scripts/smoke-erasure.sh",
        "bash scripts/smoke-media.sh",
        "bash scripts/smoke-released-fixture.sh",
        "bash scripts/smoke-infra.sh",
        "bash scripts/smoke-released-stack.sh",
        "docker compose down",
    }
    assert expanded_check.isdisjoint(outside_check)
