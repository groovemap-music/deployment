"""Compose-level lux regressions: collector config source, one-shot dependents, inactive-job inputs.

Static checks over the Compose files plus `docker compose config` renders (skipped without
docker). The real-container behaviour (an invalid collector config turning unhealthy, a failing
initializer failing `up --wait`) is proven by the Linux CI smoke, not here.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
CATALOG_QUEUE_SERVICES = {
    "extractor-discogs", "extractor-musicbrainz", "graphinator", "brainzgraphinator", "tableinator", "brainztableinator",
}  # fmt: skip
EXTERNAL_ENV = {"EXTERNAL_POSTGRES_HOST": "db.invalid", "EXTERNAL_VALKEY_HOST": "cache.invalid", "EXTERNAL_VALKEY_KEY_PREFIX": "g:"}


def _render(files: list[str], *, external: bool = False, extra_env: dict[str, str] | None = None) -> dict[str, Any]:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("no docker binary to render the Compose files")
    command = [docker, "compose", "--profile", "*", "--env-file", "config/validation.env"]
    for name in files:
        command += ["-f", name]
    env = {"PATH": str(Path(docker).parent), "HOME": str(Path.home()), **(EXTERNAL_ENV if external else {}), **(extra_env or {})}
    completed = subprocess.run(
        [*command, "config", "--format", "json"], cwd=ROOT, capture_output=True, text=True, check=False, timeout=60, env=env
    )  # fmt: skip
    assert completed.returncode == 0, completed.stderr
    rendered: dict[str, Any] = json.loads(completed.stdout)
    return rendered


def _stack(external: bool = False) -> dict[str, Any]:
    files = ["docker-compose.yml", "docker-compose.prod.yml"]
    if external:
        files += ["docker-compose.external-postgres.yml", "docker-compose.external-valkey.yml"]
    return _render(files, external=external)


def test_collector_healthcheck_validates_exactly_the_config_the_command_loads() -> None:
    service = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]["otel-collector"]
    command_sources = [arg for arg in service["command"] if arg.startswith("--config=")]
    probe = service["healthcheck"]["test"]
    probe_sources = [arg for arg in probe if arg.startswith("--config=")]
    assert len(command_sources) == 1
    assert probe_sources == command_sources, "the probe must validate the source the collector runs"
    assert probe[:3] == ["CMD", "/otelcol-contrib", "validate"]
    source = command_sources[0].removeprefix("--config=")
    assert source.startswith(("file:", "env:")), "name the provider explicitly"
    if source.startswith("file:"):
        target = source.removeprefix("file:")
        mounts = [v for v in service["volumes"] if v.split(":")[1] == target]
        assert mounts == [f"./config/otel-collector.yaml:{target}:ro"], "the canonical repo config is the one mounted"


@pytest.mark.parametrize("external", [False, True])
def test_catalog_queue_services_wait_for_the_dlq_policy_to_complete(external: bool) -> None:
    services = _stack(external)["services"]
    for name in CATALOG_QUEUE_SERVICES:
        dependency = services[name]["depends_on"]["rabbitmq-dlq-policy-init"]
        assert dependency["condition"] == "service_completed_successfully", name


@pytest.mark.parametrize("external", [False, True])
def test_every_one_shot_has_a_completed_successfully_dependent(external: bool) -> None:
    """`up --wait` fails on a one-shot that exits 0 with no dependent waiting on its completion."""
    services = _stack(external)["services"]
    one_shots = {name for name, service in services.items() if service.get("restart") == "no" and "jobs" not in service.get("profiles", [])}
    assert {"schema-init", "rabbitmq-dlq-policy-init"} <= one_shots
    for name in sorted(one_shots):
        dependents = [
            other for other, service in services.items()
            if service.get("depends_on", {}).get(name, {}).get("condition") == "service_completed_successfully"
        ]  # fmt: skip
        assert dependents, f"{name} exits 0 and nothing depends on its completion, so `up --wait` would fail"


@pytest.mark.parametrize("files", [["docker-compose.yml"], ["docker-compose.yml", "docker-compose.prod.yml"]])
def test_the_stack_renders_without_inactive_job_inputs(files: list[str]) -> None:
    rendered = _render(files)
    environment = rendered["services"]["embeddings"]["environment"]
    assert environment["SOURCE_DUMP_ID"] == ""
    assert environment["SOURCE_DUMP_DATE"] == ""
    assert "SOURCE_DUMP" not in (ROOT / "config" / "validation.env").read_text()


def test_the_embeddings_wrapper_refuses_to_run_without_real_values(tmp_path: Path) -> None:
    bash = shutil.which("bash")
    assert bash is not None
    script = ROOT / "scripts" / "run-embeddings.sh"
    env_file = tmp_path / ".env"
    env_file.write_text("")
    base = {"PATH": "/usr/bin:/bin", "RUN_EMBEDDINGS_ENV_FILE": str(env_file)}

    def run(**values: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([bash, str(script)], capture_output=True, text=True, check=False, env={**base, **values}, cwd=tmp_path)

    missing = run()
    assert missing.returncode != 0
    assert "SOURCE_DUMP_ID" in missing.stderr
    assert "SOURCE_DUMP_DATE" in run(SOURCE_DUMP_ID="discogs-2026-09").stderr
    bad_date = run(SOURCE_DUMP_ID="discogs-2026-09", SOURCE_DUMP_DATE="September")
    assert bad_date.returncode == 2
    assert "ISO date" in bad_date.stderr
