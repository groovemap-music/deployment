"""Contracts for the disposable released-image stack verifier.

`just smoke-released` starts containers, so it stays outside `just check` and CI. What is
checkable without containers, and pinned here:

- the verifier demands every image variable the split-producer topology declares, and no
  retired combined one;
- `RELEASED_IMAGE_DIGESTS` is the reviewed release set for exactly those variables, so the
  checker and the verifier can never drift apart;
- the env gate refuses a tag, a placeholder, the validation-only digest, and a real digest
  that is not the reviewed one, and decides all of that before any container command runs;
- schema initialization is proved, and proved idempotent, before an application starts;
- a failed run retains logs and always destroys its own stack.
"""

from __future__ import annotations

import itertools
import os
import runpy
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "smoke-released-stack.sh"


def _check_images() -> dict[str, Any]:
    """Execute the validation script in-process and return its module namespace."""
    return runpy.run_path(str(ROOT / "scripts" / "check-images.py"), run_name="check_images")


def test_reviewed_release_set_covers_exactly_the_owned_image_variables() -> None:
    namespace = _check_images()
    released = namespace["RELEASED_IMAGE_DIGESTS"]
    owners = namespace["IMAGE_OWNERS"]

    # A variable with no reviewed digest could never be promoted by the verifier, and a
    # reviewed digest for a variable nothing owns is a pin left behind by a retired image.
    assert set(released) == set(owners), "RELEASED_IMAGE_DIGESTS must cover exactly the owned image variables"
    for variable, digest in released.items():
        assert len(digest) == 64 and set(digest) <= set("0123456789abcdef"), f"{variable} must pin a full manifest digest"


def test_released_stack_verifier_requires_every_internal_image() -> None:
    text = SCRIPT.read_text()
    for variable in _check_images()["IMAGE_OWNERS"]:
        assert variable in text, f"the verifier must demand {variable}"
    assert "CATALOG_INGESTION_IMAGE" not in text, "the retired combined variable must not come back"
    assert "@sha256:[0-9a-f]{64}" in text
    assert "validation-only digest" in text


def test_released_stack_verifier_validates_against_the_reviewed_release_set() -> None:
    text = SCRIPT.read_text()
    assert "RELEASED_IMAGE_DIGESTS" in text, "the verifier must read the reviewed release set"
    assert "must promote the reviewed release digest" in text


def test_released_stack_verifier_orders_schema_before_applications() -> None:
    text = SCRIPT.read_text()
    first_schema_run = text.index("run --rm --no-deps schema-init")
    idempotency_run = text.index("run --rm --no-deps schema-init", first_schema_run + 1)
    application_start = text.index('up -d --wait "${GM_APPLICATION_SERVICES[@]}"')
    assert first_schema_run < idempotency_run < application_start

    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    for name in (
        "api",
        "graphinator",
        "brainzgraphinator",
        "tableinator",
        "brainztableinator",
        "dashboard",
        "explore",
        "insights",
    ):
        assert compose["services"][name]["depends_on"]["schema-init"]["condition"] == "service_completed_successfully"


def test_released_stack_verifier_checks_health_and_graceful_stop() -> None:
    text = SCRIPT.read_text()
    assert "GM_INFRASTRUCTURE_SERVICES=(rabbitmq postgres neo4j redis)" in text
    assert 'up -d --wait "${GM_INFRASTRUCTURE_SERVICES[@]}"' in text
    assert 'stop --timeout 30 "${GM_APPLICATION_SERVICES[@]}"' in text


def test_released_stack_failures_retain_logs_and_always_clean_up() -> None:
    text = SCRIPT.read_text()
    assert "ps --all >&2 || true" in text
    assert "logs --timestamps --no-color --tail 200 >&2 || true" in text
    assert "down --volumes --remove-orphans || true" in text
    assert "trap gm_cleanup EXIT" in text


def test_recipe_exists_and_stays_out_of_the_credential_free_gate() -> None:
    justfile = (ROOT / "Justfile").read_text()
    assert "smoke-released:\n    bash scripts/smoke-released-stack.sh" in justfile

    for workflow in (ROOT / ".github" / "workflows").glob("*.yml"):
        assert "smoke-released" not in workflow.read_text(), f"{workflow.name} must not start containers"


def test_maintenance_runbook_lists_the_recipe_as_approval_gated() -> None:
    runbook = (ROOT / "docs" / "maintenance.md").read_text()
    approvals = next(line for line in runbook.splitlines() if line.startswith("- `just smoke`"))
    for recipe in ("just smoke", "just smoke-infra", "just smoke-media", "just smoke-released", "just down"):
        assert f"`{recipe}`" in approvals, f"{recipe} changes live state and must require approval"


_RUN_COUNTER = itertools.count()


def _approved_env(overrides: dict[str, str] | None = None) -> str:
    """Render an env file promoting every reviewed release digest, with overrides applied."""
    namespace = _check_images()
    registry = namespace["REGISTRY"]
    lines = {
        variable: f"{registry}/{namespace['IMAGE_OWNERS'][variable]}@sha256:{digest}"
        for variable, digest in namespace["RELEASED_IMAGE_DIGESTS"].items()
    }
    lines.update(overrides or {})
    return "".join(f"{variable}={value}\n" for variable, value in lines.items())


def run_env_gate(tmp_path: Path, env_body: str | None) -> subprocess.CompletedProcess[str]:
    """Run the verifier far enough to see its env gate decide, and no further.

    `docker` is stubbed with a binary that logs its arguments and fails, so a run that
    reaches the stack at all is distinguishable from one the gate stopped, and neither
    starts a container.
    """
    # Each call gets its own directory: a `None` body must mean the file is absent, even
    # when an earlier call in the same test already wrote one.
    tmp_path = tmp_path / f"run-{next(_RUN_COUNTER)}"
    tmp_path.mkdir()

    docker = tmp_path / "docker"
    docker_log = tmp_path / "docker.log"
    docker.write_text('#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >>"$DOCKER_LOG"\nexit 1\n')
    docker.chmod(0o755)

    env_file = tmp_path / "released.env"
    if env_body is not None:
        env_file.write_text(env_body)

    completed = subprocess.run(
        ["/bin/bash", str(SCRIPT)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=os.environ
        | {
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "DOCKER_LOG": str(docker_log),
            "GM_RELEASED_STACK_ENV_FILE": str(env_file),
        },
    )
    completed.stdout = docker_log.read_text() if docker_log.exists() else ""
    return completed


def test_env_gate_refuses_a_missing_env_file(tmp_path: Path) -> None:
    result = run_env_gate(tmp_path, None)

    assert result.returncode == 2
    assert "reviewed released-image environment file" in result.stderr
    assert result.stdout == "", "the gate must decide before any container command runs"


def test_env_gate_refuses_an_env_that_omits_a_required_variable(tmp_path: Path) -> None:
    body = "".join(line + "\n" for line in _approved_env().splitlines() if not line.startswith("MUSICBRAINZ_INGESTION_IMAGE="))
    result = run_env_gate(tmp_path, body)

    assert result.returncode == 2
    assert "MUSICBRAINZ_INGESTION_IMAGE must be an approved immutable GrooveMap image digest" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("ghcr.io/groovemap-music/discogs-ingestion:v0.2.1", "immutable GrooveMap image digest"),
        ("REPLACE_WITH_64_HEX_CHARACTERS", "immutable GrooveMap image digest"),
        ("ghcr.io/groovemap-music/discogs-ingestion@sha256:" + "1" * 64, "validation-only digest"),
        ("ghcr.io/groovemap-music/discogs-ingestion@sha256:" + "a" * 64, "must promote the reviewed release digest"),
    ],
)
def test_env_gate_refuses_an_image_that_is_not_the_reviewed_release(tmp_path: Path, value: str, expected: str) -> None:
    result = run_env_gate(tmp_path, _approved_env({"DISCOGS_INGESTION_IMAGE": value}))

    assert result.returncode == 2
    assert expected in result.stderr
    assert result.stdout == "", "the gate must decide before any container command runs"


def test_env_gate_admits_an_env_that_promotes_every_reviewed_release(tmp_path: Path) -> None:
    result = run_env_gate(tmp_path, _approved_env())

    # The stub `docker` fails, so the run still ends non-zero — but it ended at the stack,
    # not at the gate, which is what proves a reviewed .env is admitted.
    assert "error:" not in result.stderr
    assert "config --quiet" in result.stdout
