"""The nightly similar-artists job: its rendered service, its spool volume and `just similar-artists-run`.

The wrapper is exercised for real with `docker` stubbed on PATH. The stub records the argv it was
given, renders the same Compose files with `docker compose config`, and, for the job's own `run`,
exits with the code the test chose, which is how the exit-code pass-through is checked. Skipped
without a docker binary, like the other Compose renders.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "run-similar-artists.sh"
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
PROD_FILES = ["-f", "docker-compose.yml", "-f", "docker-compose.prod.yml"]
SPOOL_VOLUME = "groovemap_similar_artists_spool"
STUB = """#!/usr/bin/env python3
import json, os, subprocess, sys
argv = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a") as log:
    log.write(json.dumps(argv) + "\\n")
globals_ = argv[: argv.index("run")]
rendered = subprocess.run([os.environ["REAL_DOCKER"], *globals_, "config", "--format", "json"], capture_output=True, text=True)
sys.stdout.write(rendered.stdout)
sys.stderr.write(rendered.stderr)
if rendered.returncode:
    sys.exit(rendered.returncode)
if argv[-1] == "similar-artists":
    sys.exit(int(os.environ["FAKE_JOB_EXIT"]))
if argv[-1] == "data-volume-init":
    sys.exit(int(os.environ.get("FAKE_INIT_EXIT", "0")))
"""


def _docker() -> str:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("no docker binary to render the Compose files")
    return docker


EXTERNAL_ENV = {
    "EXTERNAL_POSTGRES_HOST": "db.validation.invalid",
    "EXTERNAL_VALKEY_HOST": "cache.validation.invalid",
    "EXTERNAL_VALKEY_KEY_PREFIX": "groovemap:",
}


def _render(*files: str, env: dict[str, str] | None = None) -> dict[str, Any]:
    docker = _docker()
    argv = [docker, "compose", "--env-file", str(ROOT / "config" / "validation.env")]
    for f in files:
        argv += ["-f", f]
    completed = subprocess.run(
        [*argv, "--profile", "jobs", "config", "--format", "json"], cwd=ROOT, capture_output=True, text=True, check=False, timeout=120,
        env={**os.environ, **(env or {})},
    )  # fmt: skip
    assert completed.returncode == 0, completed.stderr
    rendered: dict[str, Any] = json.loads(completed.stdout)
    return rendered


def test_the_job_is_a_jobs_profile_service_up_never_starts() -> None:
    service = _render("docker-compose.yml")["services"]["similar-artists"]
    assert service["profiles"] == ["jobs"]
    assert service["restart"] == "no"
    # Rendered without the profile, `up -d` has no such service to start.
    docker = _docker()
    plain = subprocess.run(
        [docker, "compose", "--env-file", str(ROOT / "config" / "validation.env"), "config", "--services"],
        cwd=ROOT, capture_output=True, text=True, check=False, timeout=120,
    )  # fmt: skip
    assert plain.returncode == 0, plain.stderr
    assert "similar-artists" not in plain.stdout.split()
    assert "embeddings" not in plain.stdout.split()


def test_the_job_runs_from_the_digest_pinned_analytics_engine_image() -> None:
    rendered = _render("docker-compose.yml")
    service = rendered["services"]["similar-artists"]
    assert DIGEST.search(service["image"]), service["image"]
    assert service["image"] == rendered["services"]["embeddings"]["image"] == rendered["services"]["insights"]["image"]
    assert service["entrypoint"] == ["analytics-engine-similar-artists"]


def test_the_job_gets_a_window_a_persistent_spool_and_a_status_file() -> None:
    service = _render("docker-compose.yml")["services"]["similar-artists"]
    assert service["command"] == [
        "--spool-dir", "/spool", "--max-runtime", "19800", "--threads", "8", "--status-file", "/spool/status.json",
    ]  # fmt: skip
    assert [(v["type"], v["source"], v["target"]) for v in service["volumes"]] == [("volume", "similar_artists_spool", "/spool")]
    assert not any(v.get("read_only") for v in service["volumes"])


def test_the_window_comes_from_the_environment(tmp_path: Path) -> None:
    docker = _docker()
    env = tmp_path / "window.env"
    env.write_text((ROOT / "config" / "validation.env").read_text() + "SIMILAR_ARTISTS_MAX_RUNTIME=7200\nSIMILAR_ARTISTS_THREADS=4\n")
    completed = subprocess.run(
        [docker, "compose", "--env-file", str(env), "--profile", "jobs", "config", "--format", "json"],
        cwd=ROOT, capture_output=True, text=True, check=False, timeout=120,
    )  # fmt: skip
    assert completed.returncode == 0, completed.stderr
    command = json.loads(completed.stdout)["services"]["similar-artists"]["command"]
    assert command[command.index("--max-runtime") + 1] == "7200"
    assert command[command.index("--threads") + 1] == "4"


def test_the_spool_volume_has_a_stable_name_and_is_not_external() -> None:
    volumes = _render("docker-compose.yml")["volumes"]
    assert volumes["similar_artists_spool"]["name"] == SPOOL_VOLUME
    assert not volumes["similar_artists_spool"].get("external")
    # The extractor volumes keep their project-prefixed names: only the one a scheduler mounts is pinned.
    assert volumes["discogs_data"]["name"] != "discogs_data"


def test_the_spool_volume_is_handed_to_the_deploying_user_by_data_volume_init() -> None:
    init = _render("docker-compose.yml")["services"]["data-volume-init"]
    assert ("similar_artists_spool", "/volumes/similar-artists-spool") in {
        (v["source"], v["target"]) for v in init["volumes"] if v["type"] == "volume"
    }


def test_the_job_has_the_embeddings_jobs_login_network_and_hardening() -> None:
    services = _render("docker-compose.yml")["services"]
    job, embeddings = services["similar-artists"], services["embeddings"]
    for key in ("user", "networks", "cap_drop", "read_only", "security_opt", "tmpfs", "depends_on"):
        assert job[key] == embeddings[key], key
    for key in ("POSTGRES_HOST", "POSTGRES_DATABASE", "EMBEDDING_PIPELINE_POSTGRES_USERNAME", "EMBEDDING_PIPELINE_POSTGRES_PASSWORD"):
        assert job["environment"][key] == embeddings["environment"][key], key
    assert job["environment"]["EMBEDDING_PIPELINE_POSTGRES_USERNAME"] == "embedding_pipeline"
    assert "SOURCE_DUMP_ID" not in job["environment"]
    assert "ENABLE_SIMILAR_ARTISTS" not in job["environment"]


def test_the_job_is_capped_at_16_gib_without_swap_and_8_cpus() -> None:
    service = _render("docker-compose.yml")["services"]["similar-artists"]
    sixteen_gib = 16 * 1024**3
    assert int(service["mem_limit"]) == sixteen_gib
    assert int(service["memswap_limit"]) == sixteen_gib
    assert float(service["cpus"]) == 8


def test_production_and_external_postgres_wire_the_same_login_secrets_and_tls() -> None:
    files = ("docker-compose.yml", "docker-compose.prod.yml")
    prod = _render(*files)["services"]
    job = prod["similar-artists"]
    assert job["environment"]["EMBEDDING_PIPELINE_POSTGRES_USERNAME_FILE"] == "/run/secrets/embedding_pipeline_username"
    assert job["environment"]["EMBEDDING_PIPELINE_POSTGRES_PASSWORD_FILE"] == "/run/secrets/embedding_pipeline_password"
    assert {s["source"] for s in job["secrets"]} == {"embedding_pipeline_username", "embedding_pipeline_password"}
    assert job["restart"] == "no"

    external = _render(*files, "docker-compose.external-postgres.yml", env=EXTERNAL_ENV)["services"]
    ext_job, ext_embeddings = external["similar-artists"], external["embeddings"]
    assert ext_job["environment"]["PGSSLMODE"] == "verify-full"
    assert "postgres_ca" in {s["source"] for s in ext_job["secrets"]}
    assert "postgres" not in ext_job["depends_on"]
    assert ext_job["depends_on"] == ext_embeddings["depends_on"]


def _workspace(tmp_path: Path, *, env_lines: list[str]) -> tuple[Path, Path]:
    work = tmp_path / "checkout"
    work.mkdir(parents=True)
    for entry in ROOT.iterdir():
        if entry.name in {"docker-compose.override.yml", ".env"} or entry.name.startswith("."):
            continue
        (work / entry.name).symlink_to(entry)
    env_file = tmp_path / "stack.env"
    base = (ROOT / "config" / "validation.env").read_text()
    env_file.write_text(base.rstrip("\n") + "\n" + "".join(f"{line}\n" for line in env_lines))
    return work, env_file


def _run(
    tmp_path: Path, *, job_exit: int = 0, init_exit: int = 0, env_lines: list[str] | None = None, extra: dict[str, str] | None = None
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    docker = _docker()
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("no bash")
    work, env_file = _workspace(tmp_path, env_lines=env_lines or [])
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
        "FAKE_JOB_EXIT": str(job_exit),
        "FAKE_INIT_EXIT": str(init_exit),
        "SIMILAR_ARTISTS_ENV_FILE": str(env_file),
        **(extra or {}),
    }
    completed = subprocess.run(
        [bash, str(SCRIPT)], cwd=work, capture_output=True, text=True, check=False, timeout=120, env=env
    )  # fmt: skip
    argv = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return completed, argv


@pytest.mark.parametrize("code", [0, 75, 1, 2])
def test_the_recipe_exits_with_the_jobs_own_code_unchanged(tmp_path: Path, code: int) -> None:
    completed, argv = _run(tmp_path, job_exit=code)
    assert completed.returncode == code, completed.stderr
    env_file = str(tmp_path / "stack.env")
    assert argv == [
        ["compose", "--env-file", env_file, *PROD_FILES, "run", "--rm", "--no-deps", "--name", "groovemap-similar-artists-volume-init", "data-volume-init"],
        ["compose", "--env-file", env_file, *PROD_FILES, "--profile", "jobs", "run", "--rm", "--no-deps", "--name", "groovemap-similar-artists-run", "similar-artists"],
    ]  # fmt: skip
    job = json.loads(completed.stdout)["services"]["similar-artists"]
    assert job["command"][job["command"].index("--max-runtime") + 1] == "19800"


def test_a_failed_volume_hand_over_stops_before_the_job_runs(tmp_path: Path) -> None:
    completed, argv = _run(tmp_path, init_exit=1)
    assert completed.returncode == 2
    assert "data-volume-init failed" in completed.stderr
    assert [call[-1] for call in argv] == ["data-volume-init"]


def test_compose_file_in_the_env_file_is_the_stacks_own_file_list(tmp_path: Path) -> None:
    files = "docker-compose.yml:docker-compose.prod.yml:docker-compose.external-postgres.yml"
    external = [
        "EXTERNAL_POSTGRES_HOST=db.validation.invalid",
        "EXTERNAL_VALKEY_HOST=cache.validation.invalid",
        "EXTERNAL_VALKEY_KEY_PREFIX=groovemap:",
    ]
    completed, argv = _run(tmp_path, job_exit=75, env_lines=[f"COMPOSE_FILE={files}", *external])
    assert completed.returncode == 75, completed.stderr
    assert all("-f" not in call for call in argv)
    job = json.loads(completed.stdout)["services"]["similar-artists"]
    assert job["environment"]["POSTGRES_HOST"] == "db.validation.invalid"


def test_an_unknown_stack_and_a_missing_env_file_are_refused_before_docker_runs(tmp_path: Path) -> None:
    completed, argv = _run(tmp_path / "a", extra={"SIMILAR_ARTISTS_STACK": "lux"})
    assert completed.returncode == 2
    assert "SIMILAR_ARTISTS_STACK must be 'prod' or 'dev'" in completed.stderr
    assert argv == []
    completed, argv = _run(tmp_path / "b", extra={"SIMILAR_ARTISTS_ENV_FILE": str(tmp_path / "absent.env")})
    assert completed.returncode == 2
    assert argv == []


def _cronduit_job() -> dict[str, Any]:
    import tomllib

    text = (ROOT / "docs" / "maintenance.md").read_text()
    section = text.split("#### Cronduit (recommended)", maxsplit=1)[1].split("#### Alternatives", maxsplit=1)[0]
    block = re.search(r"```toml\n(.*?)```", section, flags=re.DOTALL)
    assert block is not None, "the Cronduit section has no toml example"
    jobs = tomllib.loads(block.group(1))["jobs"]
    assert len(jobs) == 1
    job: dict[str, Any] = jobs[0]
    return job


def test_the_documented_cronduit_job_matches_the_homelab_job_definition() -> None:
    job = _cronduit_job()
    assert job["name"] == "similar-artists"
    assert job["schedule"] == "0 1 * * *"
    assert job["cmd"] == ["analytics-engine-similar-artists", "--spool-dir", "/spool", "--max-runtime", "19800"]
    assert job["success_exit_codes"] == [0, 75]
    assert job["timeout"] == "6h30m"
    assert job["memory"] == "16g"
    assert job["volumes"] == [f"{SPOOL_VOLUME}:/spool"]
    assert job["network"].endswith("_groovemap")


def test_the_documented_cronduit_job_pins_the_image_by_digest_from_the_analytics_engine_repository() -> None:
    image = _cronduit_job()["image"]
    assert image.startswith("ghcr.io/groovemap-music/analytics-engine@sha256:")
    assert ":latest" not in image


def test_the_documented_cronduit_job_references_secrets_instead_of_inlining_them() -> None:
    env = _cronduit_job()["env"]
    for key in ("EMBEDDING_PIPELINE_POSTGRES_USERNAME", "EMBEDDING_PIPELINE_POSTGRES_PASSWORD", "POSTGRES_HOST"):
        assert re.fullmatch(r"\$\{[A-Z_]+\}", env[key]), key


def test_the_documented_window_ends_well_before_the_timeout() -> None:
    job = _cronduit_job()
    max_runtime = int(job["cmd"][job["cmd"].index("--max-runtime") + 1])
    hours, minutes = re.fullmatch(r"(\d+)h(\d+)m", job["timeout"]).groups()  # type: ignore[union-attr]
    timeout = int(hours) * 3600 + int(minutes) * 60
    assert timeout - max_runtime >= 30 * 60
    compose_default = _render("docker-compose.yml")["services"]["similar-artists"]["command"]
    assert compose_default[compose_default.index("--max-runtime") + 1] == str(max_runtime)


def test_the_documentation_covers_the_alternatives_and_the_three_scheduler_rules() -> None:
    text = (ROOT / "docs" / "maintenance.md").read_text().split("### Scheduling the nightly run", maxsplit=1)[1]
    text = text.split("## Media-aware loader upgrade", maxsplit=1)[0]
    for needle in (
        "systemd timer",
        "Host cron",
        "Ofelia",
        "Kubernetes CronJob",
        "SuccessExitStatus=75",
        "concurrencyPolicy: Forbid",
        "Concurrency 1",
        "Exit 75",
    ):
        assert needle in text, needle
