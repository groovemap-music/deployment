"""`just embeddings-run` runs the embeddings one-shot with the files the stack really runs with.

The script is exercised for real, but `docker` is a stub on PATH: it records the argv the script
would have run and then renders the same Compose invocation with `docker compose config`, so the
assertions are about the rendered `embeddings` service, not about the script's text. Skipped
without a docker binary, like the other Compose renders.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "run-embeddings.sh"
STUB = """#!/usr/bin/env python3
import json, os, subprocess, sys
argv = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a") as log:
    log.write(json.dumps(argv) + "\\n")
globals_ = argv[: argv.index("run")]
rendered = subprocess.run([os.environ["REAL_DOCKER"], *globals_, "config", "--format", "json"], capture_output=True, text=True)
sys.stdout.write(rendered.stdout)
sys.stderr.write(rendered.stderr)
sys.exit(rendered.returncode)
"""
EXTERNAL_POSTGRES = {
    "EXTERNAL_POSTGRES_HOST": "db.validation.invalid",
    "EXTERNAL_VALKEY_HOST": "cache.validation.invalid",
    "EXTERNAL_VALKEY_KEY_PREFIX": "groovemap:",
}
PROD_FILES = ["-f", "docker-compose.yml", "-f", "docker-compose.prod.yml"]


def _workspace(tmp_path: Path, *, env_lines: list[str], override: str | None = None) -> tuple[Path, Path]:
    """A copy of the repository's top level (symlinked) with its own env file, so no operator file leaks in."""
    work = tmp_path / "checkout"
    work.mkdir(parents=True)
    for entry in ROOT.iterdir():
        if entry.name in {"docker-compose.override.yml", ".env"} or entry.name.startswith("."):
            continue
        (work / entry.name).symlink_to(entry)
    if override is not None:
        (work / "docker-compose.override.yml").write_text(override)
    env_file = tmp_path / "stack.env"
    base = (ROOT / "config" / "validation.env").read_text()
    env_file.write_text(base.rstrip("\n") + "\n" + "".join(f"{line}\n" for line in env_lines))
    return work, env_file


def _run(
    tmp_path: Path, *, env_lines: list[str], extra: dict[str, str] | None = None, override: str | None = None
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    docker = shutil.which("docker")
    bash = shutil.which("bash")
    if docker is None or bash is None:
        pytest.skip("no docker binary to render the Compose files")
    work, env_file = _workspace(tmp_path, env_lines=env_lines, override=override)
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "docker"
    stub.write_text(STUB)
    stub.chmod(0o755)
    log = tmp_path / "docker.log"
    env = {
        "PATH": f"{stub_dir}{os.pathsep}{Path(docker).parent}",
        "HOME": str(Path.home()),
        "REAL_DOCKER": docker,
        "FAKE_DOCKER_LOG": str(log),
        "RUN_EMBEDDINGS_ENV_FILE": str(env_file),
        "SOURCE_DUMP_ID": "discogs-2026-09",
        "SOURCE_DUMP_DATE": "2026-09-01",
        **(extra or {}),
    }
    completed = subprocess.run(
        [bash, str(SCRIPT)], cwd=work, capture_output=True, text=True, check=False, timeout=120, env=env
    )  # fmt: skip
    argv = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return completed, argv


def _embeddings(stdout: str) -> dict[str, Any]:
    service: dict[str, Any] = json.loads(stdout)["services"]["embeddings"]
    return service


def test_dedicated_production_stack_renders_the_prod_overlay_and_the_secret_files(tmp_path: Path) -> None:
    completed, argv = _run(tmp_path, env_lines=[])
    assert completed.returncode == 0, completed.stderr
    assert argv == [
        ["compose", "--env-file", str(tmp_path / "stack.env"), *PROD_FILES, "--profile", "jobs", "run", "--rm", "--no-deps",
         "--name", "groovemap-embeddings-run", "embeddings"],
    ]  # fmt: skip
    service = _embeddings(completed.stdout)
    assert service["environment"]["EMBEDDING_PIPELINE_POSTGRES_USERNAME_FILE"] == "/run/secrets/embedding_pipeline_username"
    assert service["environment"]["EMBEDDING_PIPELINE_POSTGRES_PASSWORD_FILE"] == "/run/secrets/embedding_pipeline_password"
    assert {secret["source"] for secret in service["secrets"]} == {"embedding_pipeline_username", "embedding_pipeline_password"}
    assert service["environment"]["SOURCE_DUMP_ID"] == "discogs-2026-09"
    assert service["environment"]["SOURCE_DUMP_DATE"] == "2026-09-01"


def test_compose_file_in_the_env_file_is_the_stacks_own_file_list(tmp_path: Path) -> None:
    """The lux shape: deploy wrote COMPOSE_FILE into .env, so the script passes no -f and Compose reads it."""
    files = "docker-compose.yml:docker-compose.prod.yml:docker-compose.external-postgres.yml"
    completed, argv = _run(tmp_path, env_lines=[f"COMPOSE_FILE={files}", *(f"{k}={v}" for k, v in EXTERNAL_POSTGRES.items())])
    assert completed.returncode == 0, completed.stderr
    assert argv == [
        ["compose", "--env-file", str(tmp_path / "stack.env"), "--profile", "jobs", "run", "--rm", "--no-deps",
         "--name", "groovemap-embeddings-run", "embeddings"],
    ]  # fmt: skip
    service = _embeddings(completed.stdout)
    assert service["environment"]["POSTGRES_HOST"] == "db.validation.invalid"
    assert service["environment"]["PGSSLMODE"] == "verify-full"
    assert service["environment"]["EMBEDDING_PIPELINE_POSTGRES_PASSWORD_FILE"] == "/run/secrets/embedding_pipeline_password"
    assert "postgres_ca" in {secret["source"] for secret in service["secrets"]}
    assert "postgres" not in service.get("depends_on", {})
    assert "store-check-postgres" in service["depends_on"]


def test_the_host_override_is_part_of_the_stack_on_both_paths(tmp_path: Path) -> None:
    override = "services:\n  embeddings:\n    labels:\n      groovemap.test.override: 'yes'\n"
    completed, argv = _run(tmp_path / "prod", env_lines=[], override=override)
    assert completed.returncode == 0, completed.stderr
    assert argv[0][3:12] == [*PROD_FILES, "-f", "docker-compose.override.yml", "--profile", "jobs", "run"]
    assert _embeddings(completed.stdout)["labels"]["groovemap.test.override"] == "yes"

    completed, argv = _run(tmp_path / "dev", env_lines=[], override=override, extra={"RUN_EMBEDDINGS_STACK": "dev"})
    assert completed.returncode == 0, completed.stderr
    assert argv[0][3:7] == ["-f", "docker-compose.yml", "-f", "docker-compose.override.yml"]
    assert _embeddings(completed.stdout)["labels"]["groovemap.test.override"] == "yes"


def test_the_development_stack_keeps_the_base_file_alone(tmp_path: Path) -> None:
    completed, argv = _run(tmp_path, env_lines=[], extra={"RUN_EMBEDDINGS_STACK": "dev"})
    assert completed.returncode == 0, completed.stderr
    assert argv == [
        ["compose", "--env-file", str(tmp_path / "stack.env"), "-f", "docker-compose.yml", "--profile", "jobs", "run", "--rm",
         "--no-deps", "--name", "groovemap-embeddings-run", "embeddings"],
    ]  # fmt: skip
    service = _embeddings(completed.stdout)
    assert "EMBEDDING_PIPELINE_POSTGRES_USERNAME_FILE" not in service["environment"]
    assert "secrets" not in service


def test_an_unknown_stack_is_refused_before_docker_runs(tmp_path: Path) -> None:
    completed, argv = _run(tmp_path, env_lines=[], extra={"RUN_EMBEDDINGS_STACK": "lux"})
    assert completed.returncode == 2
    assert "RUN_EMBEDDINGS_STACK must be 'prod' or 'dev'" in completed.stderr
    assert argv == []
