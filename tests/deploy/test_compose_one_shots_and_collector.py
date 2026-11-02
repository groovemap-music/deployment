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


# gm-deployment-lvhq: every supported overlay combination must render the extractor data mounts with
# `nocopy`. Compose merges `volumes` by target, so one short-syntax redeclaration in an overlay
# silently drops it, and Docker's copy-up then re-applies the image's 1000:1000 ownership to the
# fresh volume, which a host UID other than 1000 cannot write.
DATA_MOUNTS = {
    "extractor-discogs": ("discogs_data", "/discogs-data"),
    "extractor-musicbrainz": ("musicbrainz_data", "/musicbrainz-data"),
}
OVERLAY_COMBINATIONS = {
    "base": ["docker-compose.yml"],
    "prod": ["docker-compose.yml", "docker-compose.prod.yml"],
    "smoke": ["docker-compose.yml", "docker-compose.smoke.yml"],
    "released-fixture": ["docker-compose.yml", "docker-compose.released-fixture-smoke.yml"],
    "media-smoke": ["docker-compose.yml", "docker-compose.media-smoke.yml"],
    "erasure-smoke": ["docker-compose.yml", "docker-compose.erasure-smoke.yml"],
    "shared-monitoring": ["docker-compose.yml", "docker-compose.shared-monitoring.yml"],
    "homelab-storage-override": ["docker-compose.yml", "skills/groovemap-homelab/assets/override-storage.yml"],
}


@pytest.mark.parametrize("combination", sorted(OVERLAY_COMBINATIONS))
def test_extractor_data_mounts_keep_nocopy_and_wait_for_the_volume_init(combination: str) -> None:
    extra = {"UID": "1001", "GID": "1001"}
    if combination == "shared-monitoring":
        extra |= {
            "GROOVEMAP_MONITORING": "shared",
            "SHARED_VICTORIA_METRICS_URL": "http://vm.lan:8428",
            "SHARED_VICTORIA_TRACES_URL": "https://traces.lan/vt",
            "SHARED_GRAFANA_URL": "http://grafana:3000",
            "SHARED_NODE_EXPORTER_ADDRESS": "node-exporter:9100",
            "SHARED_CADVISOR_ADDRESS": "cadvisor.lan:8080",
        }
    services = _render(OVERLAY_COMBINATIONS[combination], extra_env=extra)["services"]
    init = services["data-volume-init"]
    assert init["user"] == "0:0"
    assert init["environment"] == {"TARGET_UID": "1001", "TARGET_GID": "1001"}
    assert sorted(init["cap_add"]) == ["CHOWN", "DAC_READ_SEARCH"] and init["cap_drop"] == ["ALL"]
    assert init["network_mode"] == "none" and init["read_only"] is True and init["restart"] == "no"
    assert {m["target"] for m in init["volumes"] if m["type"] == "volume"} == {"/volumes/discogs-data", "/volumes/musicbrainz-data"}
    for name, (volume, target) in DATA_MOUNTS.items():
        service = services.get(name)
        if service is None:
            continue  # an overlay that drops the extractor needs no data mount
        mounts = [m for m in service["volumes"] if m["target"] == target]
        assert len(mounts) == 1, (combination, name)
        assert mounts[0]["source"] == volume and mounts[0]["type"] == "volume"
        assert mounts[0]["volume"] == {"nocopy": True}, f"{combination}: {name} lost nocopy on {target}"
        assert service["depends_on"]["data-volume-init"]["condition"] == "service_completed_successfully", (combination, name)


def test_the_volume_init_script_is_a_no_op_for_matching_ownership_and_changes_nothing_else() -> None:
    text = (ROOT / "scripts" / "data-volume-init.sh").read_text()
    script = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#") or line.startswith("#!"))
    assert script.startswith("#!/bin/sh\n") and "set -eu" in script
    # Only ownership of mismatched entries is ever touched: no delete, chmod, or content rewrite.
    for forbidden in ("rm ", "chmod", "-delete", "mv ", "cp "):
        assert forbidden not in script, forbidden
    assert "! -uid" in script and "! -gid" in script and "chown -h" in script and "-xdev" in script
