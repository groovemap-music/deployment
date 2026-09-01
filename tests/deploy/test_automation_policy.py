"""Automation policy for ordinary, scheduled, and dependency-update changes."""

import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
CI = ROOT / ".github" / "workflows" / "ci.yml"
DEPENDABOT = ROOT / ".github" / "dependabot.yml"


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
