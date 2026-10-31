"""The external-stores mode (gm-deployment-xsg4): an existing PostgreSQL or Valkey without a weaker gate.

The invariant: the Discogs-before-MusicBrainz gate stays authoritative in every mode, and so do
safe reruns. An external store that is unreachable or wrong gives "cannot determine" (the gate's
exit 2) or a refusal (deploy's exit 3), and never enables MusicBrainz.

Nothing here starts a container. Renders use `docker compose config` (skipped without docker),
deploy.sh runs from a scratch copy with the fake docker and gate of test_deploy.py, the gate runs
against a fake runner, and the store-check scripts run with fake psql and valkey-cli on PATH.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml

from tests.deploy import test_deploy, test_discogs_import_status
from tests.deploy.test_deploy import PROD, Deploy, helpers


if TYPE_CHECKING:
    from collections.abc import Iterator


ROOT = Path(__file__).resolve().parents[2]
gate = test_discogs_import_status.gate
Status = gate.Status
stores = helpers.stores


def _load_image_policy() -> Any:
    spec = importlib.util.spec_from_file_location("check_images_for_tests", ROOT / "scripts" / "check-images.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


images = _load_image_policy()

EXTERNAL_ENV = {
    "EXTERNAL_POSTGRES_HOST": "db.lan",
    "EXTERNAL_POSTGRES_PORT": "5432",
    "EXTERNAL_POSTGRES_DATABASE": "groovemap",
    "EXTERNAL_VALKEY_HOST": "cache.lan",
    "EXTERNAL_VALKEY_PORT": "6379",
    "EXTERNAL_VALKEY_DB": "3",
    "EXTERNAL_VALKEY_KEY_PREFIX": "groovemap:",
}
BOTH = {"GROOVEMAP_POSTGRES": "external", "GROOVEMAP_VALKEY": "external", **EXTERNAL_ENV}
POSTGRES_OVERLAY = "docker-compose.external-postgres.yml"
VALKEY_OVERLAY = "docker-compose.external-valkey.yml"
EXTERNAL_FILES = f"-f {POSTGRES_OVERLAY} -f {VALKEY_OVERLAY}"
LOCAL_STORE_SERVICES = {"postgres", "postgres-exporter", "valkey", "valkey-exporter"}
WRITES = re.compile(r"\b(CREATE|DROP|ALTER|INSERT|UPDATE|DELETE|TRUNCATE|GRANT|REVOKE|COPY|VACUUM|REINDEX|CLUSTER|COMMENT|SECURITY)\b", re.IGNORECASE)


def _writes(sql: str) -> bool:
    """True when a statement could change anything: a write, DDL or privilege keyword outside a string literal."""
    return bool(WRITES.search(re.sub(r"'[^']*'", "''", sql)))


# --- renders -----------------------------------------------------------------------------


def _render(*files: str, env: dict[str, str] | None = None, profile: str | None = "*", fmt: str = "json") -> str:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("no docker binary to render the Compose files")
    argv = [docker, "compose", "--env-file", "config/validation.env", "-f", "docker-compose.yml", "-f", "docker-compose.prod.yml"]
    for name in files:
        argv += ["-f", name]
    if profile is not None:
        argv += ["--profile", profile]
    argv += ["config", "--format", fmt]
    completed = subprocess.run(
        argv,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        env={"PATH": str(Path(docker).parent), "HOME": str(Path.home()), **(env or {})},
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


@pytest.fixture(scope="module")
def local_render() -> dict[str, Any]:
    rendered: dict[str, Any] = json.loads(_render())
    return rendered


@pytest.fixture(scope="module")
def external_render() -> dict[str, Any]:
    rendered: dict[str, Any] = json.loads(_render(POSTGRES_OVERLAY, VALKEY_OVERLAY, env=EXTERNAL_ENV))
    return rendered


def test_the_dedicated_stack_is_byte_identical_whether_local_is_unset_or_explicit() -> None:
    """Dedicated mode adds no overlay, so the render cannot differ; the base files never mention the mode."""
    for profile, fmt in ((None, "yaml"), ("*", "json"), ("musicbrainz", "yaml")):
        unset = _render(profile=profile, fmt=fmt)
        explicit = _render(profile=profile, fmt=fmt, env={"GROOVEMAP_POSTGRES": "local", "GROOVEMAP_VALKEY": "local", **EXTERNAL_ENV})
        assert unset == explicit
    assert stores.read_stores({}, {}).overlays == []
    assert stores.read_stores({"GROOVEMAP_POSTGRES": "local", "GROOVEMAP_VALKEY": ""}, {}).overlays == []
    for name in ("docker-compose.yml", "docker-compose.prod.yml"):
        text = (ROOT / name).read_text()
        assert "local-stores" not in text
        assert "store-check" not in text
        assert "EXTERNAL_" not in text


def test_the_dedicated_stack_keeps_its_stores_and_their_health_gates(local_render: dict[str, Any]) -> None:
    services = local_render["services"]
    assert set(services) >= LOCAL_STORE_SERVICES
    assert not any(name.startswith("store-check") for name in services)
    for name in LOCAL_STORE_SERVICES:
        assert not services[name].get("profiles"), name
    for name in ("api", "insights"):
        assert services[name]["depends_on"]["valkey"]["condition"] == "service_healthy"
    for name in ("schema-init", "api", "tableinator", "brainztableinator", "dashboard", "insights", "embeddings"):
        assert services[name]["depends_on"]["postgres"]["condition"] == "service_healthy"
        assert "POSTGRES_PREPROVISIONED" not in services[name]["environment"]


def _store_dependents(render: dict[str, Any], store: str) -> set[str]:
    return {name for name, service in render["services"].items() if store in (service.get("depends_on") or {})} - LOCAL_STORE_SERVICES


def test_external_mode_omits_the_local_stores_without_removing_them(external_render: dict[str, Any]) -> None:
    services = external_render["services"]
    for name in LOCAL_STORE_SERVICES:
        assert services[name]["profiles"] == [stores.LOCAL_STORES_PROFILE], name
    default = set(json.loads(_render(POSTGRES_OVERLAY, VALKEY_OVERLAY, env=EXTERNAL_ENV, profile=None))["services"])
    assert not LOCAL_STORE_SERVICES & default
    assert {"store-check-postgres", "store-check-valkey"} <= default
    # Nothing that runs still waits on, or reaches, a local store.
    for name, service in services.items():
        if name in LOCAL_STORE_SERVICES:
            continue
        assert not {"postgres", "valkey"} & set(service.get("depends_on") or {}), name
        environment = service.get("environment") or {}
        assert environment.get("POSTGRES_HOST", "db.lan") == "db.lan", name
        assert environment.get("VALKEY_HOST", "cache.lan") == "cache.lan", name


def test_every_dependent_waits_for_its_store_check_and_keeps_its_other_dependencies(
    local_render: dict[str, Any], external_render: dict[str, Any]
) -> None:
    for store in ("postgres", "valkey"):
        dependents = _store_dependents(local_render, store)
        assert dependents, store
        for name in dependents:
            local = dict(local_render["services"][name]["depends_on"])
            external = dict(external_render["services"][name]["depends_on"])
            assert external.pop(f"store-check-{store}") == {"condition": "service_completed_successfully", "required": True}
            local.pop(store)
            for other in ("postgres", "valkey"):
                if other != store and other in local:
                    local.pop(other)
                    external.pop(f"store-check-{other}")
            assert external == local, name


def test_external_settings_reach_every_store_client(external_render: dict[str, Any]) -> None:
    services = external_render["services"]
    for name in _store_dependents(json.loads(_render()), "postgres"):
        environment = services[name]["environment"]
        assert (environment["POSTGRES_HOST"], environment["POSTGRES_PORT"], environment["POSTGRES_DATABASE"]) == ("db.lan", "5432", "groovemap"), name
    for name in ("api", "insights"):
        environment = services[name]["environment"]
        assert (environment["VALKEY_HOST"], environment["VALKEY_PORT"], environment["VALKEY_DB"]) == ("cache.lan", "6379", "3")
        assert environment["VALKEY_KEY_PREFIX"] == "groovemap:"
        assert environment["VALKEY_PASSWORD_FILE"] == "/run/secrets/valkey_password"
    assert services["dashboard"]["environment"]["VALKEY_HEALTH_ADDRESS"] == "cache.lan:6379"
    assert services["schema-init"]["environment"]["POSTGRES_PREPROVISIONED"] == "true"


def test_the_store_checks_run_the_stacks_own_images_and_take_credentials_only_from_secret_files(
    local_render: dict[str, Any], external_render: dict[str, Any]
) -> None:
    services = external_render["services"]
    for check, store in (("store-check-postgres", "postgres"), ("store-check-valkey", "valkey")):
        assert services[check]["image"] == local_render["services"][store]["image"]
        assert services[check]["restart"] == "no"
        assert services[check]["read_only"] is True
        assert "ports" not in services[check]
        for key, value in services[check]["environment"].items():
            assert "PASSWORD" not in key or (key.endswith("_FILE") and value.startswith("/run/secrets/")), key
    assert {secret["source"] for secret in services["store-check-postgres"]["secrets"]} == {"postgres_username", "postgres_password", "postgres_ca"}
    assert {secret["source"] for secret in services["store-check-valkey"]["secrets"]} == {"valkey_password", "valkey_ca"}
    images.check_overlay_images((ROOT / "docker-compose.yml").read_text(), {name: (ROOT / name).read_text() for name in images.OVERLAY_IMAGES})


def test_the_overlays_render_only_on_the_production_stack() -> None:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("no docker binary to render the Compose files")
    for overlay in (POSTGRES_OVERLAY, VALKEY_OVERLAY):
        completed = subprocess.run(
            [docker, "compose", "--env-file", "config/validation.env", "-f", "docker-compose.yml", "-f", overlay, "config", "--quiet"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
            env={"PATH": str(Path(docker).parent), "HOME": str(Path.home()), **EXTERNAL_ENV},
        )
        assert completed.returncode != 0
        assert "undefined secret" in completed.stderr


def test_a_missing_external_setting_fails_the_render_instead_of_falling_back() -> None:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("no docker binary to render the Compose files")
    for overlay, missing in ((POSTGRES_OVERLAY, "EXTERNAL_POSTGRES_HOST"), (VALKEY_OVERLAY, "EXTERNAL_VALKEY_KEY_PREFIX")):
        env = {key: value for key, value in EXTERNAL_ENV.items() if key != missing}
        completed = subprocess.run(
            [
                docker,
                "compose",
                "--env-file",
                "config/validation.env",
                "-f",
                "docker-compose.yml",
                "-f",
                "docker-compose.prod.yml",
                "-f",
                overlay,
                "config",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
            env={"PATH": str(Path(docker).parent), "HOME": str(Path.home()), **env},
        )
        assert completed.returncode != 0
        assert missing in completed.stderr


def test_the_override_check_accepts_the_supported_mode_and_still_refuses_ad_hoc_rewiring(external_render: dict[str, Any]) -> None:
    unchanged = json.loads(json.dumps(external_render))
    assert [finding.level for finding in helpers.check_override(external_render, unchanged)] == ["ok"]
    rewired = json.loads(json.dumps(external_render))
    rewired["services"]["api"]["environment"]["POSTGRES_HOST"] = "other.lan"
    stops = [finding for finding in helpers.check_override(external_render, rewired) if finding.level == "stop"]
    assert [finding.message for finding in stops] == ["docker-compose.override.yml changes POSTGRES_HOST on api"]
    assert "supported external-stores mode" in stops[0].hint


def test_an_override_cannot_switch_a_local_store_off_by_itself(local_render: dict[str, Any]) -> None:
    after = json.loads(json.dumps(local_render))
    after["services"]["postgres"]["profiles"] = [stores.LOCAL_STORES_PROFILE]
    stops = [finding.message for finding in helpers.check_override(local_render, after) if finding.level == "stop"]
    assert stops == ["docker-compose.override.yml changes profiles on postgres"]


def test_the_disk_check_skips_an_external_stores_volume(external_render: dict[str, Any], local_render: dict[str, Any]) -> None:
    def names(config: dict[str, Any]) -> set[str]:
        return {location.volume.name for location in helpers.resolve_locations(config, "/var/lib/docker", local=True, desktop=False)}

    assert "postgres_data" in names(local_render)
    assert "postgres_data" not in names(external_render)
    assert [volume.name for volume in helpers.external_store_volumes(external_render)] == ["postgres_data"]
    assert helpers.external_store_volumes(local_render) == []


def test_musicbrainz_evidence_is_the_same_in_external_mode(local_render: dict[str, Any], external_render: dict[str, Any]) -> None:
    """gm-deployment-p9mu: only MusicBrainz-exclusive volumes count; the local-stores profile and the store checks change nothing."""
    evidence = helpers.musicbrainz_exclusive_volumes(external_render)
    assert (
        evidence
        == helpers.musicbrainz_exclusive_volumes(local_render)
        == {"extractor_musicbrainz_logs", "brainzgraphinator_logs", "brainztableinator_logs"}
    )


def test_preflight_skips_the_host_ports_of_external_stores() -> None:
    variables = {port.variable for port in helpers.configured_ports("prod", {}, {})}
    skipped = helpers.external_store_ports({"GROOVEMAP_POSTGRES": "external", "GROOVEMAP_VALKEY": "external"}, {})
    assert skipped == ["POSTGRES_HOST_PORT", "VALKEY_HOST_PORT"]
    kept = {port.variable for port in helpers.configured_ports("prod", {}, {}, skipped)}
    assert variables - kept == {"POSTGRES_HOST_PORT", "VALKEY_HOST_PORT"}
    assert helpers.external_store_ports({}, {}) == []
    assert helpers.external_store_ports({"GROOVEMAP_POSTGRES": "bogus"}, {}) == []


# --- settings and capability floors ------------------------------------------------------


def test_store_modes_default_to_local_and_refuse_anything_else() -> None:
    assert stores.read_stores({}, {}) == stores.Stores("local", "local")
    assert stores.read_stores({"GROOVEMAP_POSTGRES": "external"}, {"GROOVEMAP_VALKEY": "external"}).external == ("postgres", "valkey")
    assert stores.read_stores({"GROOVEMAP_POSTGRES": "external"}, {"GROOVEMAP_POSTGRES": "local"}).external == ()  # the shell outranks .env
    for value in ("External", "yes", "shared", "1"):
        with pytest.raises(stores.StoreConfigError, match="is not a store mode"):
            stores.read_stores({"GROOVEMAP_VALKEY": value}, {})
    assert stores.Stores("external", "local").overlays == [POSTGRES_OVERLAY]
    assert stores.Stores("local", "external").overlays == [VALKEY_OVERLAY]
    assert stores.Stores("external", "external").overlays == [POSTGRES_OVERLAY, VALKEY_OVERLAY]
    assert stores.Stores().stack_container == "groovemap-postgres"
    assert stores.Stores("external", "local").stack_container == "groovemap-neo4j"


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"EXTERNAL_POSTGRES_HOST": ""}, "EXTERNAL_POSTGRES_HOST is not set"),
        ({"EXTERNAL_POSTGRES_HOST": "db lan"}, "is not a host name"),
        ({"EXTERNAL_POSTGRES_PORT": "70000"}, "is not a port number"),
        ({"EXTERNAL_POSTGRES_DATABASE": "postgres"}, "is a server database"),
        ({"EXTERNAL_POSTGRES_DATABASE": "template1"}, "is a server database"),
        ({"EXTERNAL_POSTGRES_DATABASE": "gm;drop"}, "is not a plain database name"),
        ({"EXTERNAL_VALKEY_HOST": ""}, "EXTERNAL_VALKEY_HOST is not set"),
        ({"EXTERNAL_VALKEY_DB": "256"}, "is not a Valkey database number"),
        ({"EXTERNAL_VALKEY_KEY_PREFIX": ""}, "EXTERNAL_VALKEY_KEY_PREFIX is not set"),
        ({"EXTERNAL_VALKEY_KEY_PREFIX": "gm*"}, "may only use letters"),
        ({"EXTERNAL_VALKEY_KEY_PREFIX": ":::"}, "must be more than colons"),
    ],
)
def test_unusable_external_settings_are_refused(change: dict[str, str], problem: str) -> None:
    problems = stores.setting_problems(stores.Stores("external", "external"), {**EXTERNAL_ENV, **change}, {})
    assert len(problems) == 1 and problem in problems[0], problems


def test_usable_external_settings_pass_and_local_stores_need_none() -> None:
    assert stores.setting_problems(stores.Stores("external", "external"), EXTERNAL_ENV, {}) == []
    assert stores.setting_problems(stores.Stores(), {}, {}) == []


def test_the_reviewed_release_set_meets_both_capability_floors() -> None:
    """database-schema 0.6.0 (pre-provisioned PostgreSQL), catalog-api 0.6.0 and analytics-engine 0.5.0 (Valkey prefix and TLS)."""
    assert images.EXTERNAL_STORE_FLOORS == {
        "postgres": {"DATABASE_SCHEMA_IMAGE": "0.6.0"},
        "valkey": {"CATALOG_API_IMAGE": "0.6.0", "ANALYTICS_ENGINE_IMAGE": "0.5.0"},
    }
    env = {
        variable: f"ghcr.io/groovemap-music/{images.IMAGE_OWNERS[variable]}@sha256:{digest}"
        for variable, digest in images.RELEASED_IMAGE_DIGESTS.items()
    }
    for store in ("postgres", "valkey"):
        assert not list(
            stores.floor_problems(
                store, images.EXTERNAL_STORE_FLOORS, images.RELEASED_IMAGE_VERSIONS, images.RELEASED_IMAGE_DIGESTS, images.IMAGE_OWNERS, env, {}
            )
        ), store


def test_an_unreleased_floor_refuses_both_external_stores() -> None:
    """The UNRELEASED sentinel is never met, so a capability no release has yet keeps the store from being external."""
    floors = {
        "postgres": {"DATABASE_SCHEMA_IMAGE": images.UNRELEASED},
        "valkey": {"CATALOG_API_IMAGE": images.UNRELEASED, "ANALYTICS_ENGINE_IMAGE": images.UNRELEASED},
    }
    env = {variable: f"ghcr.io/x@sha256:{digest}" for variable, digest in images.RELEASED_IMAGE_DIGESTS.items()}
    for store, repositories in (("postgres", ["database-schema"]), ("valkey", ["catalog-api", "analytics-engine"])):
        problems = stores.floor_problems(store, floors, images.RELEASED_IMAGE_VERSIONS, images.RELEASED_IMAGE_DIGESTS, images.IMAGE_OWNERS, env, {})
        assert [problem.split(" ", 1)[0] for problem in problems] == repositories
        assert all("has no release with external" in problem for problem in problems)


def _floors(version: str) -> dict[str, dict[str, str]]:
    return {"postgres": {"DATABASE_SCHEMA_IMAGE": version}}


def test_a_met_floor_also_needs_env_to_run_the_reviewed_digest() -> None:
    digests = images.RELEASED_IMAGE_DIGESTS
    reviewed = {"DATABASE_SCHEMA_IMAGE": f"ghcr.io/groovemap-music/database-schema@sha256:{digests['DATABASE_SCHEMA_IMAGE']}"}

    def problems(floors: dict[str, dict[str, str]], env: dict[str, str]) -> list[str]:
        return list(stores.floor_problems("postgres", floors, images.RELEASED_IMAGE_VERSIONS, digests, images.IMAGE_OWNERS, env, {}))

    assert problems(_floors("0.6.0"), reviewed) == []
    assert problems(_floors("0.5.9"), reviewed) == []
    assert "below v0.7.0" in problems(_floors("0.7.0"), reviewed)[0]
    older = {"DATABASE_SCHEMA_IMAGE": "ghcr.io/groovemap-music/database-schema@sha256:" + "0" * 64}
    assert "run `just env-init`" in problems(_floors("0.6.0"), older)[0]
    assert "run `just env-init`" in problems(_floors("0.6.0"), {})[0]


def test_released_versions_match_the_comments_beside_the_digests() -> None:
    text = (ROOT / "scripts" / "check-images.py").read_text()
    for variable, version in images.RELEASED_IMAGE_VERSIONS.items():
        line = next(line for line in text.splitlines() if line.strip().startswith(f'"{variable}": "') and "#" in line)
        assert f"{images.IMAGE_OWNERS[variable]} v{version} " in line, line
    images.check_declarations()


def _store_findings(
    mode: str = "prod", env: dict[str, str] | None = None, *, tmp_path: Path, policy: Any = None, volumes: tuple[str, ...] = ()
) -> list[Any]:
    findings: list[Any] = helpers.store_findings(
        mode,
        env_values=env if env is not None else BOTH,
        environ={},
        secrets_dir=tmp_path,
        image_policy=policy or images,
        local_volumes=lambda: volumes,
    )
    return findings


class PromotedPolicy:
    """The image policy with both upstream releases promoted."""

    EXTERNAL_STORE_FLOORS: dict[str, dict[str, str]] = {  # noqa: RUF012
        "postgres": {"DATABASE_SCHEMA_IMAGE": "0.6.0"},
        "valkey": {"CATALOG_API_IMAGE": "0.6.0", "ANALYTICS_ENGINE_IMAGE": "0.5.0"},
    }
    RELEASED_IMAGE_VERSIONS = images.RELEASED_IMAGE_VERSIONS
    RELEASED_IMAGE_DIGESTS = images.RELEASED_IMAGE_DIGESTS
    IMAGE_OWNERS = images.IMAGE_OWNERS


class UnreleasedPolicy(PromotedPolicy):
    """The image policy while no release supports either store."""

    EXTERNAL_STORE_FLOORS: dict[str, dict[str, str]] = {  # noqa: RUF012
        "postgres": {"DATABASE_SCHEMA_IMAGE": images.UNRELEASED},
        "valkey": {"CATALOG_API_IMAGE": images.UNRELEASED, "ANALYTICS_ENGINE_IMAGE": images.UNRELEASED},
    }


def _reviewed_env() -> dict[str, str]:
    return {
        variable: f"ghcr.io/groovemap-music/{images.IMAGE_OWNERS[variable]}@sha256:{digest}"
        for variable, digest in images.RELEASED_IMAGE_DIGESTS.items()
    }


def _write_secrets(directory: Path, *names: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name in names or ("postgres_username.txt", "postgres_password.txt", "valkey_password.txt", "postgres_ca.crt", "valkey_ca.crt"):
        (directory / name).write_text("operator-written\n")


def test_local_stores_need_nothing(tmp_path: Path) -> None:
    findings = _store_findings(env={}, tmp_path=tmp_path)
    assert [finding.level for finding in findings] == ["ok", "ok"]
    assert "the stack's own postgres container" in findings[0].message


def test_the_unreleased_floor_refuses_external_stores(tmp_path: Path) -> None:
    _write_secrets(tmp_path)
    stops = [
        finding for finding in _store_findings(env={**BOTH, **_reviewed_env()}, tmp_path=tmp_path, policy=UnreleasedPolicy) if finding.level == "stop"
    ]
    assert [finding.message.split(":")[0] for finding in stops] == [
        "External PostgreSQL is refused",
        "External Valkey is refused",
        "External Valkey is refused",
    ]
    assert "GROOVEMAP_POSTGRES=local" in stops[0].hint


def test_a_promoted_release_set_lets_external_stores_through(tmp_path: Path) -> None:
    _write_secrets(tmp_path)
    findings = _store_findings(env={**BOTH, **_reviewed_env()}, tmp_path=tmp_path, policy=PromotedPolicy)
    assert [finding.level for finding in findings] == ["ok", "ok"]
    assert findings[0].message == "PostgreSQL: external, db.lan:5432/groovemap (verified next by store-check-postgres)"
    assert findings[1].message == "Valkey: external, cache.lan:6379 database 3, keys under groovemap: (verified next by store-check-valkey)"


def test_external_stores_need_the_production_overlay(tmp_path: Path) -> None:
    findings = _store_findings("dev", env={**BOTH, **_reviewed_env()}, tmp_path=tmp_path, policy=PromotedPolicy)
    assert [finding.level for finding in findings] == ["stop"]
    assert "need the production overlay" in findings[0].message


def test_external_credentials_must_be_written_by_the_operator(tmp_path: Path) -> None:
    _write_secrets(tmp_path, "postgres_username.txt", "postgres_ca.crt", "valkey_ca.crt")
    (tmp_path / "postgres_password.txt").write_text("\n")
    stops = [
        finding.message
        for finding in _store_findings(env={**BOTH, **_reviewed_env()}, tmp_path=tmp_path, policy=PromotedPolicy)
        if finding.level == "stop"
    ]
    assert stops == [
        "External PostgreSQL needs secrets/postgres_password.txt, written by you before deploying",
        "External Valkey needs secrets/valkey_password.txt, written by you before deploying",
    ]


def test_switching_a_loaded_dedicated_deployment_to_external_postgres_is_refused(tmp_path: Path) -> None:
    _write_secrets(tmp_path)
    env = {**BOTH, **_reviewed_env()}
    stops = [
        finding
        for finding in _store_findings(env=env, tmp_path=tmp_path, policy=PromotedPolicy, volumes=("postgres_data", "neo4j_data"))
        if finding.level == "stop"
    ]
    assert len(stops) == 1 and "already has a postgres_data volume" in stops[0].message
    valkey_only = {**env, "GROOVEMAP_POSTGRES": "local"}
    assert not [
        finding
        for finding in _store_findings(env=valkey_only, tmp_path=tmp_path, policy=PromotedPolicy, volumes=("postgres_data",))
        if finding.level == "stop"
    ]


def test_an_unknown_mode_is_a_stop(tmp_path: Path) -> None:
    findings = _store_findings(env={"GROOVEMAP_POSTGRES": "shared"}, tmp_path=tmp_path)
    assert [finding.level for finding in findings] == ["stop"]
    assert "GROOVEMAP_POSTGRES='shared' is not a store mode" in findings[0].message


# --- the one store-access layer ----------------------------------------------------------


def test_every_postgres_probe_goes_through_one_routing_function() -> None:
    compose = ["docker", "compose", "-f", "docker-compose.yml"]
    local = stores.psql_argv(compose, "SELECT 1", stores.Stores())
    assert local == [*compose, "exec", "-T", "postgres", "sh", "-c", stores.PSQL_SHELL, "sh", "SELECT 1"]
    external = stores.psql_argv(compose, "SELECT 1", stores.Stores("external", "local"))
    assert external == [*compose, "run", "--rm", "--no-deps", "-T", "store-check-postgres", "sh", "-c", stores.PSQL_SHELL, "sh", "SELECT 1"]
    assert helpers.PSQL_SHELL is gate.PSQL_SHELL is stores.PSQL_SHELL
    for script in ("deploy_helpers.py", "discogs_import_status.py"):
        text = (ROOT / "scripts" / script).read_text()
        assert '"exec", "-T", "postgres"' not in text, script
        assert 'self.exec("postgres"' not in text, script


def test_the_psql_shell_reads_the_password_file_only_for_a_network_connection(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "psql").write_text('#!/bin/sh\nprintf "%s\\n" "$@"\necho "PGPASSWORD=${PGPASSWORD:-}"\n')
    (bin_dir / "psql").chmod(0o755)
    (tmp_path / "user").write_text("groovemap_app")
    (tmp_path / "password").write_text("s3cret-value")
    common = {
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin",
        "POSTGRES_USER_FILE": str(tmp_path / "user"),
        "POSTGRES_PASSWORD_FILE": str(tmp_path / "password"),
    }

    def run(env: dict[str, str]) -> list[str]:
        completed = subprocess.run(
            ["/bin/sh", "-c", stores.PSQL_SHELL, "sh", "SELECT 1"], env=common | env, capture_output=True, text=True, check=False
        )
        assert completed.returncode == 0, completed.stderr
        return completed.stdout.splitlines()

    external = run({"PGHOST": "db.lan", "POSTGRES_DB": "groovemap"})
    assert external[-1] == "PGPASSWORD=s3cret-value"
    assert "s3cret-value" not in external[:-1]  # the argv psql received
    assert external[:5] == ["-X", "-U", "groovemap_app", "-d", "groovemap"]
    local = run({})
    assert local[-1] == "PGPASSWORD="


class ExternalFakeDocker(test_discogs_import_status.FakeDocker):
    """The gate's fake runner, answering `run --rm --no-deps -T store-check-postgres` for an external PostgreSQL."""

    def __init__(self, *, unreachable: bool = False, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.unreachable = unreachable

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "store-check-postgres" in argv:
            assert argv[argv.index("store-check-postgres") - 4 : argv.index("store-check-postgres")] == ["run", "--rm", "--no-deps", "-T"]
            assert "postgres" not in argv[: argv.index("store-check-postgres")]
            self.calls.append(argv)
            if self.unreachable:
                return subprocess.CompletedProcess(argv, 2, "", 'psql: error: could not translate host name "db.lan" to address\n')
            sql = argv[-1]
            return subprocess.CompletedProcess(argv, 0, self.relations if "to_regclass" in sql else self.latch, "")
        assert "postgres" not in argv, "an external PostgreSQL must never be read through the local container"
        return super().__call__(argv, **kwargs)


def _external_compose(fake: Any, **kwargs: Any) -> Any:
    return gate.Compose(
        gate.compose_files(gate.parse_args(["--prod"]), stores.Stores("external", "external")),
        runner=fake,
        configured=stores.Stores("external", "external"),
        **kwargs,
    )


def test_the_gate_reads_the_external_database_through_the_store_check_image() -> None:
    fake = ExternalFakeDocker()
    report = gate.evaluate(gate.observe(_external_compose(fake)))
    assert report.status is Status.COMPLETE
    files = [fake.calls[0][index + 1] for index, word in enumerate(fake.calls[0]) if word == "-f"]
    assert files == ["docker-compose.yml", "docker-compose.prod.yml", POSTGRES_OVERLAY, VALKEY_OVERLAY]
    assert [call[call.index("-T") + 1] for call in fake.calls] == ["store-check-postgres", "store-check-postgres", "neo4j", "rabbitmq"]


def test_an_unreachable_external_store_cannot_be_determined() -> None:
    report = gate.evaluate(gate.observe(_external_compose(ExternalFakeDocker(unreachable=True))))
    assert report.status is Status.UNKNOWN
    assert "could not translate host name" in test_discogs_import_status.signal(report, "postgres latch").detail


def test_mid_wait_an_unreachable_external_store_never_completes_and_recovery_resumes_the_wait() -> None:
    loading = test_discogs_import_status.latch_output()
    sequence = iter(
        [
            gate.observe(_external_compose(ExternalFakeDocker(latch=loading))),  # reachable, import running
            gate.observe(_external_compose(ExternalFakeDocker(unreachable=True))),  # the server goes away
            gate.observe(_external_compose(ExternalFakeDocker(unreachable=True))),
            gate.observe(_external_compose(ExternalFakeDocker(latch=loading))),  # back, still loading
            gate.observe(_external_compose(ExternalFakeDocker())),
        ]
    )
    clock = test_discogs_import_status.Clock()
    out = io.StringIO()
    report = gate.watch(lambda: next(sequence), 30, 0, out, clock=clock, sleep=clock.sleep)
    assert report.status is Status.COMPLETE
    verdicts = [line.split(" [")[0].split(" ", 1)[1] for line in out.getvalue().splitlines()]
    assert verdicts == ["in progress", "cannot determine", "cannot determine", "in progress", "complete"]

    # Unreachable until the timeout: the last verdict is "cannot determine", never complete.
    unreachable = gate.observe(_external_compose(ExternalFakeDocker(unreachable=True)))
    clock = test_discogs_import_status.Clock()
    gave_up = gate.watch(lambda: unreachable, 30, 100, io.StringIO(), clock=clock, sleep=clock.sleep)
    assert gave_up.status is Status.UNKNOWN


def test_the_gate_takes_the_mode_from_env_and_needs_prod_for_an_external_postgres(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    env_file = tmp_path / "stack.env"
    env_file.write_text("GROOVEMAP_POSTGRES=external\nEXTERNAL_POSTGRES_HOST=db.lan\n")
    fake = ExternalFakeDocker()
    assert gate.main(["--prod", "--env-file", str(env_file)], runner=fake, environ={}) == 0
    assert POSTGRES_OVERLAY in fake.calls[0] and VALKEY_OVERLAY not in fake.calls[0]
    capsys.readouterr()

    without_prod = ExternalFakeDocker()
    assert gate.main(["--env-file", str(env_file)], runner=without_prod, environ={}) == 2
    assert "address the production stack with --prod" in capsys.readouterr().out
    assert not any("store-check-postgres" in call or "postgres" in call for call in without_prod.calls)

    env_file.write_text("GROOVEMAP_POSTGRES=elsewhere\n")
    assert gate.main(["--prod", "--env-file", str(env_file)], runner=ExternalFakeDocker(), environ={}) == 2
    assert "is not a store mode" in capsys.readouterr().out
    # The shell outranks the env file, as it does for Compose.
    assert (
        gate.main(["--prod", "--env-file", str(env_file)], runner=test_discogs_import_status.FakeDocker(), environ={"GROOVEMAP_POSTGRES": "local"})
        == 0
    )


def test_the_latch_probe_reads_an_external_database_through_the_store_check_image() -> None:
    seen: list[list[str]] = []

    def runner(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "t\n", "")

    assert helpers.musicbrainz_latch(["docker", "compose"], runner, stores.Stores("external", "local"))
    assert seen[0][:7] == ["docker", "compose", "run", "--rm", "--no-deps", "-T", "store-check-postgres"]

    def unreachable(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 2, "", "could not connect")

    assert not helpers.musicbrainz_latch(["docker", "compose"], unreachable, stores.Stores("external", "local"))


def test_every_query_the_gate_and_probes_send_only_reads() -> None:
    for sql in (gate.RELATIONS_SQL, gate.LATCH_SQL, gate.JOB_JOIN, helpers.MUSICBRAINZ_LATCH_SQL):
        assert sql.lstrip().upper().startswith(("SELECT", "LEFT JOIN"))
        assert not _writes(sql), sql


# --- deploy.sh in external mode ----------------------------------------------------------


class ExternalDeploy(Deploy):
    """A scratch repository whose .env runs both stores externally, with the operator's secrets written."""

    def __init__(self, root: Path, *, promoted: bool = True, secrets: bool = True) -> None:
        super().__init__(root)
        for name in ("docker-compose.external-postgres.yml", "docker-compose.external-valkey.yml"):
            shutil.copy(ROOT / name, root / name)
        if not promoted:
            policy = root / "scripts" / "check-images.py"
            text = policy.read_text()
            text = text.replace('"postgres": {"DATABASE_SCHEMA_IMAGE": "0.6.0"}', '"postgres": {"DATABASE_SCHEMA_IMAGE": UNRELEASED}')
            text = text.replace(
                '{"CATALOG_API_IMAGE": "0.6.0", "ANALYTICS_ENGINE_IMAGE": "0.5.0"}',
                '{"CATALOG_API_IMAGE": UNRELEASED, "ANALYTICS_ENGINE_IMAGE": UNRELEASED}',
            )
            assert text.count("UNRELEASED}") >= 2
            policy.write_text(text)
        created = subprocess.run([sys.executable, str(root / "scripts" / "env-init.py")], capture_output=True, text=True, check=False, cwd=root)
        assert created.returncode == 0, created.stderr
        self.write_env((root / ".env").read_text() + "".join(f"{key}={value}\n" for key, value in BOTH.items()))
        if secrets:
            _write_secrets(root / "secrets")


@pytest.fixture
def external(tmp_path: Path) -> Iterator[ExternalDeploy]:
    yield ExternalDeploy(tmp_path)


def _checks(deploy: Deploy) -> list[str]:
    """The store-check runs, without the flag that tells the postgres check the stack already runs."""
    calls = [call.replace(" -e STORE_CHECK_STACK_RUNNING=1", "") for call in deploy.calls]
    return [call for call in calls if " run --rm --no-deps -T store-check-" in call and " sh -c " not in call]


def test_external_happy_path_runs_both_phases_with_the_overlays_everywhere(external: ExternalDeploy) -> None:
    result = external.run(FAKE_GATE_WAIT="0")
    assert result.returncode == 0, result.stdout + result.stderr
    stack = f"docker compose {PROD} {EXTERNAL_FILES}"
    assert test_deploy._compose_calls(external.calls) == [
        f"{stack} --profile * config --format json",
        f"{stack} run --rm --no-deps -T store-check-postgres",
        f"{stack} run --rm --no-deps -T store-check-valkey",
        f"{stack} up -d --wait",
        "gate --prod --wait --interval 300",
        f"{stack} up -d --wait",
        f"{stack} exec -T api catalog-admin-setup --list",
    ]
    assert external.env_values["COMPOSE_PROFILES"] == "musicbrainz"
    assert "PostgreSQL: external, db.lan:5432/groovemap" in result.stdout
    assert "postgres_data is not checked" not in result.stdout  # the fake render has no services to skip
    for name in ("postgres_username.txt", "postgres_password.txt", "valkey_password.txt"):
        assert (external.root / "secrets" / name).read_text() == "operator-written\n"  # never replaced


def test_deploy_refuses_unreleased_external_stores_before_anything_changes(tmp_path: Path) -> None:
    deploy = ExternalDeploy(tmp_path, promoted=False)
    result = deploy.run(FAKE_GATE_WAIT="0")
    assert result.returncode == 3, result.stdout + result.stderr
    assert "External PostgreSQL is refused: database-schema has no release with external-postgres support yet" in result.stdout
    assert "External Valkey is refused: catalog-api has no release" in result.stdout
    assert test_deploy._compose_calls(deploy.calls) == []
    assert "COMPOSE_PROFILES" not in deploy.env_values
    assert not (tmp_path / "secrets" / "neo4j_password.txt").exists()  # create-secrets.sh never ran


def test_missing_operator_secrets_stop_before_create_secrets_invents_them(tmp_path: Path) -> None:
    deploy = ExternalDeploy(tmp_path, secrets=False)
    result = deploy.run(FAKE_GATE_WAIT="0")
    assert result.returncode == 3, result.stdout + result.stderr
    assert "External PostgreSQL needs secrets/postgres_username.txt" in result.stdout
    assert not (tmp_path / "secrets" / "postgres_password.txt").exists()
    assert test_deploy._compose_calls(deploy.calls) == []


def test_an_unreachable_store_at_deploy_stops_before_anything_starts(external: ExternalDeploy) -> None:
    result = external.run(FAKE_GATE="0", FAKE_GATE_WAIT="0", FAKE_STORE_CHECK="1")
    assert result.returncode == 3, result.stdout + result.stderr
    assert "store-check-postgres refused the external store" in result.stderr
    assert external.ups == []
    assert not any(call.startswith("gate") for call in external.calls)
    assert "COMPOSE_PROFILES" not in external.env_values


def test_resuming_with_the_store_unreachable_never_enables_musicbrainz(external: ExternalDeploy) -> None:
    first = external.run("--no-wait")
    assert first.returncode == 0, first.stdout + first.stderr
    for gate_code in ("2", "0"):  # even a gate that (wrongly) said complete is never consulted
        result = external.run(FAKE_RUNNING_NEO4J="abc", FAKE_GATE=gate_code, FAKE_GATE_WAIT=gate_code, FAKE_STORE_CHECK="1")
        assert result.returncode == 3, result.stdout + result.stderr
        assert external.ups == []
        assert "COMPOSE_PROFILES" not in external.env_values


def test_a_wait_that_cannot_determine_does_not_enable_musicbrainz(external: ExternalDeploy) -> None:
    result = external.run(FAKE_GATE_WAIT="2")
    assert result.returncode == 1
    assert "the Discogs import status check failed (exit 2)" in result.stderr
    assert "COMPOSE_PROFILES" not in external.env_values
    assert len(external.ups) == 1


def test_an_interrupted_first_load_resumes_in_external_mode(external: ExternalDeploy) -> None:
    stack = f"docker compose {PROD} {EXTERNAL_FILES}"
    interrupted = external.run("--no-admin", FAKE_GATE_WAIT="130")
    assert interrupted.returncode == 130
    assert "COMPOSE_PROFILES" not in external.env_values
    assert "bash scripts/deploy.sh --no-admin" in interrupted.stdout

    # The stack is up (neo4j runs; there is no local postgres to find), the import is still going.
    waiting = external.run("--no-admin", FAKE_RUNNING_NEO4J="abc", FAKE_GATE="3", FAKE_GATE_WAIT="130")
    assert waiting.returncode == 130
    assert "COMPOSE_PROFILES" not in external.env_values
    latch = [call for call in external.calls if "store-check-postgres sh -c" in call]
    assert len(latch) == 1 and latch[0].startswith(f"{stack} run --rm --no-deps -T store-check-postgres sh -c ")
    assert not any("exec -T postgres" in call for call in external.calls)

    done = external.run("--no-admin", FAKE_RUNNING_NEO4J="abc", FAKE_GATE="3", FAKE_GATE_WAIT="0")
    assert done.returncode == 0, done.stdout + done.stderr
    assert external.env_values["COMPOSE_PROFILES"] == "musicbrainz"
    assert external.ups == [f"{stack} up -d --wait"] * 2
    assert _checks(external) == [f"{stack} run --rm --no-deps -T store-check-postgres", f"{stack} run --rm --no-deps -T store-check-valkey"]


def test_a_stray_local_postgres_is_not_how_external_mode_finds_its_stack(external: ExternalDeploy) -> None:
    """With an external PostgreSQL the stack is found by neo4j: a groovemap-postgres container proves nothing."""
    result = external.run(FAKE_RUNNING="abc", FAKE_GATE="0", FAKE_GATE_WAIT="3", FAKE_LATCH="t\n")
    assert result.returncode == 1
    assert "COMPOSE_PROFILES" not in external.env_values
    assert not any("sh -c" in call for call in external.calls)  # no latch probe
    assert [call for call in external.calls if call.startswith("gate")] == ["gate --prod --wait --interval 300"]


def test_switching_an_existing_dedicated_deployment_is_refused(external: ExternalDeploy) -> None:
    result = external.run(FAKE_VOLUMES="postgres_data\nneo4j_data\n", FAKE_GATE_WAIT="0")
    assert result.returncode == 3
    assert "already has a postgres_data volume" in result.stdout
    assert external.ups == []


def test_external_stores_refuse_the_development_stack(external: ExternalDeploy) -> None:
    result = external.run("--dev", FAKE_GATE_WAIT="0")
    assert result.returncode == 3
    assert "need the production overlay" in result.stdout
    assert external.ups == []


def test_valkey_alone_can_be_external(external: ExternalDeploy) -> None:
    external.write_env((external.root / ".env").read_text().replace("GROOVEMAP_POSTGRES=external", "GROOVEMAP_POSTGRES=local"))
    result = external.run(FAKE_RUNNING="abc", FAKE_GATE="3", FAKE_GATE_WAIT="0")
    assert result.returncode == 0, result.stdout + result.stderr
    stack = f"docker compose {PROD} -f {VALKEY_OVERLAY}"
    assert external.ups == [f"{stack} up -d --wait"] * 2
    assert any(call.startswith(f"{stack} exec -T postgres sh -c") for call in external.calls)  # the latch probe, locally
    assert _checks(external) == [f"{stack} run --rm --no-deps -T store-check-valkey"]


# --- the store-check scripts -------------------------------------------------------------


FAKE_PSQL = r"""#!/bin/sh
# Answers the store check's queries from FAKE_* settings and logs each call.
all="$*"
sql=""
db=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -c) sql="$2"; shift ;;
    -d) db="$2"; shift ;;
  esac
  shift
done
[ -n "$sql" ] || sql="$(cat)"
case "$all" in
  *"${PGPASSWORD:-unset}"*) leaked=yes ;;
  *) leaked=no ;;
esac
printf 'db=%s auth=%s argv-has-password=%s sql=%s\n' "$db" "${PGPASSWORD:+set}" "$leaked" "$sql" >>"$FAKE_LOG"
if [ -n "${FAKE_DOWN:-}" ]; then echo "psql: error: $FAKE_DOWN" >&2; exit 2; fi
case "$sql" in
  "SELECT 1") echo 1 ;;
  "SHOW server_version_num") echo "${FAKE_VERSION_NUM:-180001}" ;;
  "SHOW server_version") echo "${FAKE_VERSION:-18.1}" ;;
  *"extname = 'pg_trgm'"*) echo "${FAKE_TRGM-1.6}" ;;
  *"extname = 'vector'"*) echo "${FAKE_VECTOR-}" ;;
  *has_database_privilege*) echo "${FAKE_CREATE:-t}" ;;
  *rolsuper*) echo "${FAKE_SUPER:-f}" ;;
  *pg_stat_ssl*) echo "${FAKE_TLS-TLSv1.3}" ;;
  *max_connections*) echo "${FAKE_USABLE:-97}" ;;
  *rolconnlimit*) echo "${FAKE_LIMITS:--1 -1}" ;;
  *pg_stat_activity*) echo "${FAKE_IN_USE:-3 2 2 2}" ;;
  "SELECT pg_backend_pid();") echo "${FAKE_FIRST_PID-101}" ;;
  "SELECT pg_backend_pid()") echo "${FAKE_SECOND_PID:-102}" ;;
  *) echo "unexpected query: $sql" >&2; exit 3 ;;
esac
"""

FAKE_VALKEY_CLI = r"""#!/bin/sh
# The cleartext path only: logs each call and answers from FAKE_* settings.
printf 'valkey-cli auth=%s argv=%s\n' "${REDISCLI_AUTH:+set}" "$*" >>"$FAKE_LOG"
case "$*" in
  *--tls*) echo "valkey-cli must never carry the password over TLS: it does not check the host name" >&2; exit 9 ;;
esac
while [ "$#" -gt 1 ]; do
  case "$1" in
    -h | -p | -n) shift 2 ;;
    --no-auth-warning) shift ;;
    *) break ;;
  esac
done
if [ -n "${FAKE_DOWN:-}" ]; then echo "Could not connect to Valkey at cache.lan:6379: $FAKE_DOWN" >&2; exit 1; fi
if [ -z "${REDISCLI_AUTH:-}" ]; then
  if [ -n "${FAKE_OPEN:-}" ]; then echo PONG; else echo "NOAUTH Authentication required."; fi
  exit 0
fi
case "$*" in
  PING) printf 'PONG\r\n' ;;
  "INFO server")
    printf '# Server\r\nredis_version:7.2.4\r\nserver_name:valkey\r\n'
    [ -z "${FAKE_VALKEY_VERSION-9.1.2}" ] || printf 'valkey_version:%s\r\n' "${FAKE_VALKEY_VERSION-9.1.2}"
    ;;
  "INFO memory") printf '# Memory\r\nmaxmemory_policy:%s\r\n' "${FAKE_POLICY:-noeviction}" ;;
  "INFO persistence") printf '# Persistence\r\naof_enabled:%s\r\n' "${FAKE_AOF:-1}" ;;
  *) echo "unexpected command: $*" >&2; exit 3 ;;
esac
"""

# `openssl s_client` as the TLS path uses it: verifies the fake server's certificate (names in
# FAKE_CERT_NAMES, chain good unless FAKE_BAD_CHAIN) before reading stdin, then answers the RESP
# commands on stdin. Logs argv, whether the password env reached it, and the commands it received.
FAKE_OPENSSL = r"""#!{python}
import json, os, sys

argv = sys.argv[1:]
log = {"argv": argv, "auth_env": bool(os.environ.get("REDISCLI_AUTH") or os.environ.get("VALKEYCLI_AUTH")), "commands": None}

def option(name):
    return argv[argv.index(name) + 1] if name in argv else None

def finish(code, err=""):
    with open(os.environ["FAKE_LOG"], "a") as handle:
        handle.write("openssl " + json.dumps(log) + "\n")
    if err:
        sys.stderr.write(err + "\n")
    sys.exit(code)

assert argv[0] == "s_client" and "-verify_return_error" in argv and option("-CAfile"), argv
if os.environ.get("FAKE_DOWN"):
    finish(1, "connect:errno=111")
if os.environ.get("FAKE_BAD_CHAIN"):
    finish(1, "verify error:num=20:unable to get local issuer certificate")
name = option("-verify_hostname") or option("-verify_ip")
if name is not None and name not in os.environ.get("FAKE_CERT_NAMES", "cache.lan").split(","):
    finish(1, "verify error:num=62:hostname mismatch")
# The handshake verified: only now does s_client read stdin and send it.
data = sys.stdin.buffer.read().decode()
words, commands, lines = [], [], data.split("\r\n")
index = 0
while index < len(lines):
    line = lines[index]
    if line.startswith("*"):
        count, current = int(line[1:]), []
        for _ in range(count):
            current.append(lines[index + 2])
            index += 2
        commands.append(current)
    index += 1
password = os.environ.get("FAKE_PASSWORD", "vk-s3cret")
log["commands"] = [["<password>" if word == password else word for word in command] for command in commands]
out, authed = [], False
for command in commands:
    verb = command[0].upper()
    if verb == "QUIT":
        out.append("+OK")
        break
    if verb == "AUTH":
        authed = command[-1] == password
        out.append("+OK" if authed else "-WRONGPASS invalid username-password pair or user is disabled.")
    elif not authed and not os.environ.get("FAKE_OPEN"):
        out.append("-NOAUTH Authentication required.")
    elif verb == "SELECT":
        out.append("+OK")
    elif verb == "PING":
        out.append("+PONG")
    elif verb == "INFO":
        section = command[1]
        version = os.environ.get("FAKE_VALKEY_VERSION", "9.1.2")
        body = {
            "server": "# Server\r\nredis_version:7.2.4\r\nserver_name:valkey" + (f"\r\nvalkey_version:{version}" if version else ""),
            "memory": "# Memory\r\nmaxmemory_policy:" + os.environ.get("FAKE_POLICY", "noeviction"),
            "persistence": "# Persistence\r\naof_enabled:" + os.environ.get("FAKE_AOF", "1"),
        }[section]
        out.append(f"${len(body)}\r\n{body}")
sys.stdout.write("".join(reply + "\r\n" for reply in out))
finish(0)
"""


def _openssl_calls(calls: list[str]) -> list[dict[str, Any]]:
    return [json.loads(call.removeprefix("openssl ")) for call in calls if call.startswith("openssl ")]


def _fake_tools(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    tools = (
        ("psql", FAKE_PSQL),
        ("valkey-cli", FAKE_VALKEY_CLI),
        ("openssl", FAKE_OPENSSL.replace("{python}", sys.executable)),
        ("sleep", "#!/bin/sh\nexit 0\n"),
    )
    for name, text in tools:
        (bin_dir / name).write_text(text)
        (bin_dir / name).chmod(0o755)
    return bin_dir


def _run_check(tmp_path: Path, script: str, env: dict[str, str]) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bin_dir = _fake_tools(tmp_path)
    log = tmp_path / "calls.log"
    log.write_text("")
    secrets = tmp_path / "run-secrets"
    secrets.mkdir(exist_ok=True)
    (secrets / "postgres_username").write_text("groovemap_app")
    (secrets / "postgres_password").write_text("pg-s3cret")
    (secrets / "valkey_password").write_text("vk-s3cret")
    for ca in ("postgres_ca", "valkey_ca"):
        (secrets / ca).write_text("-----BEGIN CERTIFICATE-----\n")
    base = {
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin",
        "FAKE_LOG": str(log),
        "TMPDIR": str(tmp_path),
        "PGHOST": "db.lan",
        "PGPORT": "5432",
        "POSTGRES_DB": "groovemap",
        "POSTGRES_USER_FILE": str(secrets / "postgres_username"),
        "POSTGRES_PASSWORD_FILE": str(secrets / "postgres_password"),
        "PGSSLMODE": "verify-full",
        "PGSSLROOTCERT": str(secrets / "postgres_ca"),
        "GROOVEMAP_ALLOW_PLAINTEXT": "false",
        "VALKEY_TLS": "true",
        "VALKEY_TLS_VERIFY_HOSTNAME": "true",
        "VALKEY_TLS_CA_FILE": str(secrets / "valkey_ca"),
        "VALKEY_HOST": "cache.lan",
        "VALKEY_PORT": "6379",
        "VALKEY_DB": "3",
        "VALKEY_KEY_PREFIX": "groovemap:",
        "VALKEY_PASSWORD_FILE": str(secrets / "valkey_password"),
    }
    completed = subprocess.run(["/bin/sh", str(ROOT / "scripts" / script)], env=base | env, capture_output=True, text=True, check=False, timeout=60)
    return completed, log.read_text().splitlines()


def test_the_postgres_check_passes_a_ready_server_and_only_reads_the_groovemap_database(tmp_path: Path) -> None:
    result, calls = _run_check(tmp_path, "store-check-postgres.sh", {})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "db.lan:5432/groovemap is ready for GrooveMap" in result.stdout
    assert calls
    for call in calls:
        assert call.startswith("db=groovemap auth=set argv-has-password=no sql="), call
        sql = call.split(" sql=", 1)[1]
        assert sql.upper().startswith(("SELECT", "SHOW")), sql
        assert not _writes(sql), sql
    assert "pg-s3cret" not in result.stdout + result.stderr
    assert "groovemap_app" not in result.stdout + result.stderr  # the role is never printed


def test_the_postgres_check_reports_every_connection_number(tmp_path: Path) -> None:
    result, _ = _run_check(tmp_path, "store-check-postgres.sh", {"FAKE_LIMITS": "100 -1", "FAKE_IN_USE": "30 12 14 9"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        "connection headroom 67 of the 60 the services need (server 67 free (max_connections minus the reserved ones 97, 30 in use); "
        "role limit 100 with 12 in use leaves 88; database unlimited (14 in use))"
    ) in result.stdout


def test_the_postgres_check_counts_groovemaps_own_connections_as_headroom_only_when_it_runs(tmp_path: Path) -> None:
    fake = {"FAKE_IN_USE": "50 30 30 30"}  # 47 free on the server, 77 once GrooveMap's own 30 are given back
    refused, _ = _run_check(tmp_path, "store-check-postgres.sh", fake)
    assert refused.returncode == 1
    assert "only 47 connections of headroom" in refused.stderr
    running, _ = _run_check(tmp_path, "store-check-postgres.sh", fake | {"STORE_CHECK_STACK_RUNNING": "1"})
    assert running.returncode == 0, running.stdout + running.stderr
    assert "connection headroom 77 of the 60" in running.stdout
    assert "30 of them GrooveMap's own" in running.stdout


@pytest.mark.parametrize(
    ("fake", "reason"),
    [
        ({"FAKE_DOWN": 'could not translate host name "db.lan" to address'}, "cannot connect to PostgreSQL at db.lan:5432/groovemap"),
        ({"FAKE_VERSION_NUM": "170004", "FAKE_VERSION": "17.4"}, "PostgreSQL 17.4 at db.lan:5432/groovemap; GrooveMap needs PostgreSQL 18 or newer"),
        ({"FAKE_TRGM": ""}, "pg_trgm is not installed in groovemap"),
        ({"FAKE_CREATE": "f"}, "the role cannot create schemas in groovemap"),
        ({"FAKE_USABLE": "40"}, "the server has 40 usable connections"),
        ({"FAKE_LIMITS": "20 -1"}, "a connection limit of 20 applies"),
        ({"FAKE_LIMITS": "-1 10"}, "a connection limit of 10 applies"),
        ({"FAKE_IN_USE": "50 2 2 2"}, "only 47 connections of headroom"),
        ({"FAKE_LIMITS": "70 -1", "FAKE_IN_USE": "3 20 3 3"}, "only 50 connections of headroom"),
        ({"FAKE_LIMITS": "-1 80", "FAKE_IN_USE": "3 3 30 3"}, "only 50 connections of headroom"),
        ({"FAKE_FIRST_PID": "4242", "FAKE_SECOND_PID": "4242"}, "two concurrent sessions shared one server backend (pid 4242)"),
        ({"FAKE_FIRST_PID": ""}, "could not hold two sessions open at once"),
    ],
    ids=[
        "unreachable",
        "postgres-17",
        "no-pg_trgm",
        "no-create",
        "small-server",
        "role-limit",
        "database-limit",
        "server-connections-in-use",
        "role-connections-in-use",
        "database-connections-in-use",
        "pgbouncer-transaction-mode",
        "no-second-session",
    ],
)
def test_the_postgres_check_refuses_with_a_precise_reason(tmp_path: Path, fake: dict[str, str], reason: str) -> None:
    result, _ = _run_check(tmp_path, "store-check-postgres.sh", fake)
    assert result.returncode == 1
    assert f"store-check-postgres: FAILED: {reason}" in result.stderr
    assert "is ready for GrooveMap" not in result.stdout


def test_the_postgres_check_accepts_session_pooling_and_notes_optional_gaps(tmp_path: Path) -> None:
    result, _ = _run_check(tmp_path, "store-check-postgres.sh", {"FAKE_FIRST_PID": "7", "FAKE_SECOND_PID": "8", "FAKE_SUPER": "t"})
    assert result.returncode == 0, result.stderr
    assert "each session gets its own server backend" in result.stdout
    assert "vector is not installed" in result.stdout
    assert "the role is a superuser" in result.stdout


def test_the_postgres_check_needs_both_credentials(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.write_text("")
    result, calls = _run_check(tmp_path, "store-check-postgres.sh", {"POSTGRES_PASSWORD_FILE": str(empty)})
    assert result.returncode == 1 and "no PostgreSQL password" in result.stderr
    assert calls == []


def test_the_valkey_check_passes_a_ready_server_with_reads_only(tmp_path: Path) -> None:
    result, calls = _run_check(tmp_path, "store-check-valkey.sh", {})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "cache.lan:6379 database 3 is ready for GrooveMap (keys under groovemap:)" in result.stdout
    assert not [call for call in calls if call.startswith("valkey-cli")]  # never over TLS: it does not check the host name
    probe, session, anonymous = _openssl_calls(calls)
    ca = str(tmp_path / "run-secrets" / "valkey_ca")
    for call in (probe, session, anonymous):
        argv = call["argv"]
        assert argv[argv.index("-verify_hostname") + 1] == "cache.lan" and argv[argv.index("-servername") + 1] == "cache.lan"
        assert argv[argv.index("-CAfile") + 1] == ca and "-verify_return_error" in argv
        assert "vk-s3cret" not in " ".join(argv) and not call["auth_env"]
    assert "-no_ign_eof" in probe["argv"] and probe["commands"] == []  # verified before anything is sent
    assert session["commands"] == [
        ["AUTH", "<password>"], ["SELECT", "3"], ["PING"], ["INFO", "server"], ["INFO", "memory"], ["INFO", "persistence"], ["QUIT"],
    ]  # fmt: skip
    assert anonymous["commands"] == [["PING"], ["QUIT"]]
    assert "ok: TLS to cache.lan:6379, certificate verified against secrets/valkey_ca.crt for cache.lan" in result.stdout
    assert "vk-s3cret" not in result.stdout + result.stderr


def test_a_hostname_mismatch_refuses_before_any_credential_is_sent(tmp_path: Path) -> None:
    result, calls = _run_check(tmp_path, "store-check-valkey.sh", {"FAKE_CERT_NAMES": "other.lan"})
    assert result.returncode == 1
    assert "cannot open a TLS connection to cache.lan:6379 that verifies its certificate against secrets/valkey_ca.crt for cache.lan" in result.stderr
    assert "hostname mismatch" in result.stderr and "refusing to send the Valkey password" in result.stderr
    assert _openssl_calls(calls) == [{"argv": _openssl_calls(calls)[0]["argv"], "auth_env": False, "commands": None}]  # the probe only
    assert not [call for call in calls if call.startswith("valkey-cli")]


def test_a_bad_chain_refuses_even_with_hostname_checks_off(tmp_path: Path) -> None:
    result, calls = _run_check(tmp_path, "store-check-valkey.sh", {"VALKEY_TLS_VERIFY_HOSTNAME": "false", "FAKE_BAD_CHAIN": "1"})
    assert result.returncode == 1 and "unable to get local issuer certificate" in result.stderr
    assert [call["commands"] for call in _openssl_calls(calls)] == [None]


def test_the_hostname_opt_out_warns_and_still_verifies_the_chain(tmp_path: Path) -> None:
    result, calls = _run_check(tmp_path, "store-check-valkey.sh", {"VALKEY_TLS_VERIFY_HOSTNAME": "false", "FAKE_CERT_NAMES": "other.lan"})
    assert result.returncode == 0, result.stderr
    assert "WARNING: TLS to cache.lan:6379, certificate chain verified against secrets/valkey_ca.crt but not the host name" in result.stdout
    for call in _openssl_calls(calls):
        assert "-verify_return_error" in call["argv"] and "-CAfile" in call["argv"]
        assert "-verify_hostname" not in call["argv"]


def test_an_ip_address_is_verified_as_an_address(tmp_path: Path) -> None:
    result, calls = _run_check(tmp_path, "store-check-valkey.sh", {"VALKEY_HOST": "10.0.0.5", "FAKE_CERT_NAMES": "10.0.0.5"})
    assert result.returncode == 0, result.stderr
    argv = _openssl_calls(calls)[0]["argv"]
    assert argv[argv.index("-verify_ip") + 1] == "10.0.0.5" and "-servername" not in argv


@pytest.mark.parametrize(
    ("env", "reason"),
    [
        ({"VALKEY_KEY_PREFIX": ""}, "VALKEY_KEY_PREFIX is empty"),
        ({"VALKEY_KEY_PREFIX": "gm*"}, "VALKEY_KEY_PREFIX may only contain"),
        ({"FAKE_DOWN": "Connection refused"}, "cannot open a TLS connection to cache.lan:6379"),
        ({"FAKE_PASSWORD": "rotated"}, "cannot reach or authenticate to Valkey at cache.lan:6379 database 3: -WRONGPASS"),
        ({"FAKE_OPEN": "1"}, "Valkey at cache.lan:6379 database 3 answers clients without a password"),
        ({"FAKE_VALKEY_VERSION": "7.2.5"}, "Valkey 7.2.5 at cache.lan:6379 database 3; GrooveMap needs Valkey 8.0 or newer"),
        ({"FAKE_POLICY": "allkeys-lru"}, "maxmemory-policy is allkeys-lru"),
        ({"FAKE_AOF": "0"}, "AOF persistence is off"),
    ],
    ids=["no-prefix", "glob-prefix", "unreachable", "wrong-password", "no-auth", "old-valkey", "evicting", "no-aof"],
)
def test_the_valkey_check_refuses_with_a_precise_reason(tmp_path: Path, env: dict[str, str], reason: str) -> None:
    result, _ = _run_check(tmp_path, "store-check-valkey.sh", env)
    assert result.returncode == 1
    assert f"store-check-valkey: FAILED: {reason}" in result.stderr


def test_the_valkey_check_reads_redis_versions_too(tmp_path: Path) -> None:
    result, _ = _run_check(tmp_path, "store-check-valkey.sh", {"FAKE_VALKEY_VERSION": ""})
    assert result.returncode == 0, result.stderr
    assert "ok: Redis 7.2.4" in result.stdout


def test_nothing_in_the_external_mode_can_write_to_another_applications_data() -> None:
    """No DDL outside the GrooveMap database, no FLUSH*/CONFIG SET, and GrooveMap's keys only under its prefix."""
    sources = {
        name: (ROOT / name).read_text()
        for name in ("scripts/store-check-postgres.sh", "scripts/store-check-valkey.sh", POSTGRES_OVERLAY, VALKEY_OVERLAY, "scripts/stores.py")
    }
    for name, text in sources.items():
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        assert not re.search(r"\b(FLUSHALL|FLUSHDB|CONFIG\s+SET|CONFIG\s+REWRITE|DEBUG|SHUTDOWN)\b", code, re.IGNORECASE), name
    # Every psql in the check names the configured database; nothing connects to `postgres` or a template.
    check = "\n".join(line for line in sources["scripts/store-check-postgres.sh"].splitlines() if not line.lstrip().startswith("#"))
    assert check.count('psql -X -U "$user" -d "$db"') == 2
    assert len(re.findall(r"\bpsql\b", check)) == 2
    # schema-init verifies instead of creating, inside that one database.
    assert 'POSTGRES_PREPROVISIONED: "true"' in sources[POSTGRES_OVERLAY]
    # Both Valkey clients always get the prefix, and an empty one is refused before anything starts.
    assert sources[VALKEY_OVERLAY].count("<<: *external-valkey-env") == 3
    assert 'VALKEY_KEY_PREFIX: "${EXTERNAL_VALKEY_KEY_PREFIX:?' in sources[VALKEY_OVERLAY]


def test_the_store_helper_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    env = tmp_path / ".env"
    monkeypatch.setattr(helpers, "ENV_FILE", env)
    monkeypatch.setattr(helpers, "SECRETS_DIR", tmp_path / "secrets")
    seen: list[list[str]] = []

    def runner(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "neo4j_data\n", "")

    docker = helpers.Docker(runner)
    env.write_text("COMPOSE_PROJECT_NAME=lab\n")
    assert helpers.main(["stores"], docker=docker, environ={}) == 0
    assert helpers.main(["store-files"], docker=docker, environ={}) == 0
    assert helpers.main(["stack-container"], docker=docker, environ={}) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "groovemap-postgres"
    assert seen == []  # local stores need no Docker call

    env.write_text("COMPOSE_PROJECT_NAME=lab\n" + "".join(f"{key}={value}\n" for key, value in {**BOTH, **_reviewed_env()}.items()))
    _write_secrets(tmp_path / "secrets")
    monkeypatch.setattr(helpers, "load_image_policy", lambda: UnreleasedPolicy)
    assert helpers.main(["stores"], docker=docker, environ={}) == 3  # the floors are unreleased
    assert "External PostgreSQL is refused" in capsys.readouterr().out
    assert seen[-1][:5] == ["docker", "volume", "ls", "--filter", "label=com.docker.compose.project=lab"]
    assert helpers.main(["store-files"], docker=docker, environ={}) == 0
    assert helpers.main(["stack-container"], docker=docker, environ={}) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"{POSTGRES_OVERLAY} store-check-postgres",
        f"{VALKEY_OVERLAY} store-check-valkey",
        "groovemap-neo4j",
    ]
    monkeypatch.setattr(helpers, "load_image_policy", lambda: PromotedPolicy)
    assert helpers.main(["stores", "--project-name", "lab"], docker=docker, environ={}) == 0

    env.write_text("GROOVEMAP_VALKEY=maybe\n")
    for command in (["stores"], ["store-files"], ["stack-container"]):
        assert helpers.main(command, docker=docker, environ={}) == 3
    assert helpers.main(["musicbrainz-latch", "--", "docker", "compose"], docker=docker, environ={}) == 1


# --- transport security ------------------------------------------------------------------


def _problems(**env: str) -> list[str]:
    return list(stores.setting_problems(stores.Stores("external", "external"), {**EXTERNAL_ENV, **env}, {}))


def _warnings(**env: str) -> list[str]:
    return list(stores.transport_warnings(stores.Stores("external", "external"), {**EXTERNAL_ENV, **env}, {}))


def test_tls_is_the_default_for_both_stores() -> None:
    assert _problems() == []
    assert _warnings() == []
    assert stores.verifies("postgres", {}, {}) and stores.verifies("valkey", {}, {})
    assert stores.sslmode({}, {}) == "verify-full"


@pytest.mark.parametrize("mode", ["disable", "allow", "prefer"])
def test_cleartext_postgres_to_a_server_off_this_machine_needs_the_opt_out(mode: str) -> None:
    problems = _problems(EXTERNAL_POSTGRES_SSLMODE=mode)
    assert len(problems) == 1
    assert f"EXTERNAL_POSTGRES_SSLMODE={mode} sends credentials and data to db.lan in the clear" in problems[0]
    assert "anyone on the network path" in problems[0] and "EXTERNAL_POSTGRES_ALLOW_PLAINTEXT=true" in problems[0]
    assert _problems(EXTERNAL_POSTGRES_SSLMODE=mode, EXTERNAL_POSTGRES_ALLOW_PLAINTEXT="true") == []
    warnings = _warnings(EXTERNAL_POSTGRES_SSLMODE=mode, EXTERNAL_POSTGRES_ALLOW_PLAINTEXT="true")
    assert len(warnings) == 1 and "PostgreSQL at db.lan is reached in the clear" in warnings[0]


def test_cleartext_valkey_to_a_server_off_this_machine_needs_the_opt_out() -> None:
    problems = _problems(EXTERNAL_VALKEY_TLS="false")
    assert len(problems) == 1 and "EXTERNAL_VALKEY_TLS=false sends credentials and data to cache.lan in the clear" in problems[0]
    assert "EXTERNAL_VALKEY_ALLOW_PLAINTEXT=true" in problems[0]
    assert _problems(EXTERNAL_VALKEY_TLS="false", EXTERNAL_VALKEY_ALLOW_PLAINTEXT="yes") == []
    assert "Valkey at cache.lan is reached in the clear" in _warnings(EXTERNAL_VALKEY_TLS="0", EXTERNAL_VALKEY_ALLOW_PLAINTEXT="true")[0]


@pytest.mark.parametrize("host", ["host.docker.internal", "127.0.0.1", "localhost"])
def test_cleartext_on_this_machine_needs_no_opt_out(host: str) -> None:
    env = {"EXTERNAL_POSTGRES_HOST": host, "EXTERNAL_VALKEY_HOST": host, "EXTERNAL_POSTGRES_SSLMODE": "disable", "EXTERNAL_VALKEY_TLS": "false"}
    assert _problems(**env) == []
    assert _warnings(**env) == []


def test_a_container_name_on_a_shared_network_is_not_taken_for_this_machine() -> None:
    assert not stores.is_local_host("postgres-shared")
    assert _problems(EXTERNAL_POSTGRES_HOST="postgres-shared", EXTERNAL_POSTGRES_SSLMODE="disable")


@pytest.mark.parametrize(
    ("env", "problem"),
    [
        ({"EXTERNAL_POSTGRES_SSLMODE": "verify"}, "is not a libpq sslmode"),
        ({"EXTERNAL_VALKEY_TLS": "maybe"}, "EXTERNAL_VALKEY_TLS='maybe' must be true or false"),
        ({"EXTERNAL_VALKEY_TLS_VERIFY_HOSTNAME": "sure"}, "EXTERNAL_VALKEY_TLS_VERIFY_HOSTNAME='sure' must be true or false"),
        ({"EXTERNAL_POSTGRES_ALLOW_PLAINTEXT": "ok"}, "EXTERNAL_POSTGRES_ALLOW_PLAINTEXT='ok' must be true or false"),
    ],
)
def test_transport_settings_are_read_strictly(env: dict[str, str], problem: str) -> None:
    problems = _problems(**env)
    assert len(problems) == 1 and problem in problems[0], problems


def test_unverified_tls_is_allowed_with_a_warning() -> None:
    assert _problems(EXTERNAL_POSTGRES_SSLMODE="require", EXTERNAL_VALKEY_TLS_VERIFY_HOSTNAME="false") == []
    assert _warnings(EXTERNAL_POSTGRES_SSLMODE="require", EXTERNAL_VALKEY_TLS_VERIFY_HOSTNAME="false") == [
        "PostgreSQL at db.lan is encrypted but not verified (EXTERNAL_POSTGRES_SSLMODE=require): a server that impersonates it is not detected",
        "Valkey at cache.lan: the certificate chain is verified but not the host name (EXTERNAL_VALKEY_TLS_VERIFY_HOSTNAME=false)",
    ]


def test_a_verifying_store_needs_its_ca_and_a_cleartext_one_gets_an_inert_placeholder(tmp_path: Path) -> None:
    _write_secrets(tmp_path, "postgres_username.txt", "postgres_password.txt", "valkey_password.txt")
    env = {**BOTH, **_reviewed_env()}
    stops = [finding.message for finding in _store_findings(env=env, tmp_path=tmp_path, policy=PromotedPolicy) if finding.level == "stop"]
    assert stops == [
        "External PostgreSQL verifies its server's certificate and needs secrets/postgres_ca.crt",
        "External Valkey verifies its server's certificate and needs secrets/valkey_ca.crt",
    ]
    plaintext = {**env, "EXTERNAL_POSTGRES_SSLMODE": "disable", "EXTERNAL_POSTGRES_ALLOW_PLAINTEXT": "true", "EXTERNAL_VALKEY_TLS": "false"}
    plaintext["EXTERNAL_VALKEY_ALLOW_PLAINTEXT"] = "true"
    findings = _store_findings(env=plaintext, tmp_path=tmp_path, policy=PromotedPolicy)
    assert [finding.level for finding in findings] == ["warn", "warn", "ok", "ok"]
    assert helpers.leave_ca_placeholders(plaintext, {}, tmp_path) == ["secrets/postgres_ca.crt", "secrets/valkey_ca.crt"]
    assert (tmp_path / "postgres_ca.crt").read_text() == ""
    assert (tmp_path / "postgres_ca.crt").stat().st_mode & 0o777 == 0o600
    assert helpers.leave_ca_placeholders(plaintext, {}, tmp_path) == []  # never replaced
    assert helpers.leave_ca_placeholders(env, {}, tmp_path / "other") == []  # a verifying store never gets one


@pytest.fixture(scope="module")
def require_render() -> dict[str, Any]:
    rendered: dict[str, Any] = json.loads(_render(POSTGRES_OVERLAY, VALKEY_OVERLAY, env={**EXTERNAL_ENV, "EXTERNAL_POSTGRES_SSLMODE": "require"}))
    return rendered


def test_verify_full_reaches_every_postgresql_client(
    local_render: dict[str, Any], external_render: dict[str, Any], require_render: dict[str, Any]
) -> None:
    clients = _store_dependents(local_render, "postgres") | {"store-check-postgres"}
    assert clients == {"schema-init", "api", "tableinator", "brainztableinator", "dashboard", "insights", "embeddings", "store-check-postgres"}
    for name in clients:
        service = external_render["services"][name]
        assert service["environment"]["PGSSLMODE"] == "verify-full", name
        assert service["environment"]["PGSSLROOTCERT"] == "/run/secrets/postgres_ca", name
        assert {"source": "postgres_ca", "target": "/run/secrets/postgres_ca"} in service["secrets"], name
        # `require` must not get a root file: libpq would then verify after all.
        assert require_render["services"][name]["environment"]["PGSSLMODE"] == "require"
        assert "PGSSLROOTCERT" not in require_render["services"][name]["environment"], name
    assert external_render["secrets"]["postgres_ca"]["file"].endswith("/secrets/postgres_ca.crt")
    # The gate's and the latch probe's psql run in store-check-postgres, so they get the same TLS settings.
    assert stores.psql_argv(["docker", "compose"], "SELECT 1", stores.Stores("external", "local"))[6] == "store-check-postgres"


def test_tls_reaches_every_valkey_client(local_render: dict[str, Any], external_render: dict[str, Any]) -> None:
    for name in _store_dependents(local_render, "valkey") | {"store-check-valkey"}:
        environment = external_render["services"][name]["environment"]
        assert (environment["VALKEY_TLS"], environment["VALKEY_TLS_VERIFY_HOSTNAME"]) == ("true", "true"), name
        assert environment["VALKEY_TLS_CA_FILE"] == "/run/secrets/valkey_ca", name
        assert {"source": "valkey_ca", "target": "/run/secrets/valkey_ca"} in external_render["services"][name]["secrets"], name


def test_the_postgres_check_refuses_cleartext_off_this_machine_and_honors_the_opt_out(tmp_path: Path) -> None:
    refused, calls = _run_check(tmp_path, "store-check-postgres.sh", {"PGSSLMODE": "disable"})
    assert refused.returncode == 1 and "sslmode=disable may send the password and every query to db.lan in the clear" in refused.stderr
    assert calls == []  # refused before connecting
    accepted, _ = _run_check(tmp_path, "store-check-postgres.sh", {"PGSSLMODE": "disable", "GROOVEMAP_ALLOW_PLAINTEXT": "true", "FAKE_TLS": "off"})
    assert accepted.returncode == 0, accepted.stderr
    assert "WARNING: the session to db.lan:5432/groovemap is not encrypted" in accepted.stdout
    local, _ = _run_check(tmp_path, "store-check-postgres.sh", {"PGSSLMODE": "disable", "PGHOST": "host.docker.internal", "FAKE_TLS": "off"})
    assert local.returncode == 0, local.stderr


def test_the_postgres_check_proves_the_tls_it_was_told_to_use(tmp_path: Path) -> None:
    verified, _ = _run_check(tmp_path, "store-check-postgres.sh", {})
    assert "ok: TLS TLSv1.3, server certificate and host name verified (sslmode=verify-full)" in verified.stdout
    empty = tmp_path / "empty.crt"
    empty.write_text("")
    no_ca, _ = _run_check(tmp_path, "store-check-postgres.sh", {"PGSSLROOTCERT": str(empty)})
    assert no_ca.returncode == 1 and "sslmode=verify-full needs the CA" in no_ca.stderr
    unencrypted, _ = _run_check(tmp_path, "store-check-postgres.sh", {"FAKE_TLS": "off"})
    assert unencrypted.returncode == 1 and "sslmode=verify-full but the session to db.lan:5432/groovemap is not encrypted" in unencrypted.stderr
    unverified, _ = _run_check(tmp_path, "store-check-postgres.sh", {"PGSSLMODE": "require"})
    assert unverified.returncode == 0 and "WARNING: TLS TLSv1.3 but the server is not verified" in unverified.stdout


def test_the_valkey_check_refuses_cleartext_off_this_machine_and_honors_the_opt_out(tmp_path: Path) -> None:
    plain = {"VALKEY_TLS": "false"}
    refused, calls = _run_check(tmp_path, "store-check-valkey.sh", plain)
    assert refused.returncode == 1 and "TLS is off and cache.lan is not on this machine" in refused.stderr
    assert calls == []
    accepted, calls = _run_check(tmp_path, "store-check-valkey.sh", {**plain, "GROOVEMAP_ALLOW_PLAINTEXT": "true"})
    assert accepted.returncode == 0, accepted.stderr
    assert "WARNING: TLS is off (EXTERNAL_VALKEY_ALLOW_PLAINTEXT=true)" in accepted.stdout
    assert calls and not _openssl_calls(calls) and all(call.startswith("valkey-cli") for call in calls)
    assert sum(1 for call in calls if call.startswith("valkey-cli auth= ")) == 1  # the single anonymous PING
    local, _ = _run_check(tmp_path, "store-check-valkey.sh", {**plain, "VALKEY_HOST": "host.docker.internal"})
    assert local.returncode == 0, local.stderr
    missing, _ = _run_check(tmp_path, "store-check-valkey.sh", {"VALKEY_TLS_CA_FILE": str(tmp_path / "nowhere")})
    assert missing.returncode == 1 and "TLS needs the CA" in missing.stderr
    down, _ = _run_check(tmp_path, "store-check-valkey.sh", {**plain, "GROOVEMAP_ALLOW_PLAINTEXT": "true", "FAKE_DOWN": "Connection refused"})
    assert down.returncode == 1 and "cannot reach or authenticate to Valkey" in down.stderr


# --- bare Compose uses deploy's files ----------------------------------------------------


def test_compose_file_follows_deploys_files_only_while_a_store_is_external(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("APP_BASE_URL=https://x\n")
    files = ["docker-compose.yml", "docker-compose.prod.yml", POSTGRES_OVERLAY]
    assert "Set COMPOSE_FILE=" in helpers.sync_compose_file(env, files, external=True, separator=":")
    assert helpers.parse_env(env.read_text())["COMPOSE_FILE"] == f"docker-compose.yml:docker-compose.prod.yml:{POSTGRES_OVERLAY}"
    assert stores.COMPOSE_FILE_MARKER in env.read_text()
    assert helpers.sync_compose_file(env, files, external=True, separator=":") == ""  # in sync: untouched
    helpers.sync_compose_file(env, [*files, VALKEY_OVERLAY, "docker-compose.override.yml"], external=True, separator=";")
    text = env.read_text()
    assert text.count("COMPOSE_FILE=") == 1 and text.count(stores.COMPOSE_FILE_MARKER) == 1
    assert helpers.parse_env(text)["COMPOSE_FILE"].split(";")[-2:] == [VALKEY_OVERLAY, "docker-compose.override.yml"]
    assert "Removed COMPOSE_FILE" in helpers.sync_compose_file(env, files[:2], external=False)
    assert env.read_text() == "APP_BASE_URL=https://x\n"
    # An operator's own COMPOSE_FILE in local mode is theirs: nothing changes.
    env.write_text("COMPOSE_FILE=mine.yml\n")
    assert helpers.sync_compose_file(env, files[:2], external=False) == ""
    assert env.read_text() == "COMPOSE_FILE=mine.yml\n"


def test_an_operators_own_compose_file_is_refused_in_external_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_secrets(tmp_path / "secrets")
    env_file = tmp_path / ".env"
    monkeypatch.setattr(helpers, "ENV_FILE", env_file)
    env = {**BOTH, **_reviewed_env(), "COMPOSE_FILE": "mine.yml"}
    env_file.write_text("COMPOSE_FILE=mine.yml\n")

    def stops(environ: dict[str, str], values: dict[str, str]) -> list[str]:
        findings = helpers.store_findings(
            "prod", env_values=values, environ=environ, secrets_dir=tmp_path / "secrets", image_policy=PromotedPolicy, local_volumes=lambda: ()
        )
        return [finding.message for finding in findings if finding.level == "stop"]

    assert stops({}, env) == [
        "COMPOSE_FILE in .env was not written by just deploy; with an external store deploy must own it, or a bare docker compose "
        "could start the local stores and point the services at them"
    ]
    assert stops({"COMPOSE_FILE": "x.yml"}, {**BOTH, **_reviewed_env()})[0].startswith("COMPOSE_FILE is set in your shell")
    helpers.sync_compose_file(env_file, ["docker-compose.yml"], external=True)
    env_file.write_text(env_file.read_text().replace("COMPOSE_FILE=docker-compose.yml", "COMPOSE_FILE=mine.yml"))
    assert stops({}, env) == []  # deploy's own (marked) entry is deploy's to rewrite


def test_deploy_writes_compose_file_in_external_mode_and_keeps_it_in_sync(external: ExternalDeploy) -> None:
    sep = os.pathsep
    assert external.run("--no-wait").returncode == 0
    expected = sep.join(["docker-compose.yml", "docker-compose.prod.yml", POSTGRES_OVERLAY, VALKEY_OVERLAY])
    assert external.env_values["COMPOSE_FILE"] == expected
    (external.root / "docker-compose.override.yml").write_text("---\nservices: {}\n")
    result = external.run("--no-wait")
    assert result.returncode == 0, result.stdout + result.stderr
    assert external.env_values["COMPOSE_FILE"] == f"{expected}{sep}docker-compose.override.yml"
    # The override check compares against the stack with the overlays, so their rewiring is its baseline.
    renders = [call for call in external.calls if call.endswith("--profile * config --format json")]
    assert renders[:2] == [
        f"docker compose {PROD} {EXTERNAL_FILES} --profile * config --format json",
        f"docker compose {PROD} {EXTERNAL_FILES} -f docker-compose.override.yml --profile * config --format json",
    ]
    # Back to local stores: deploy removes only what it wrote.
    external.write_env(re.sub(r"GROOVEMAP_(POSTGRES|VALKEY)=external", r"GROOVEMAP_\1=local", (external.root / ".env").read_text()))
    assert external.run("--no-wait").returncode == 0
    assert "COMPOSE_FILE" not in external.env_values
    assert stores.COMPOSE_FILE_MARKER not in (external.root / ".env").read_text()


def test_deploy_in_local_mode_never_writes_compose_file(deploy_local: Deploy) -> None:
    assert deploy_local.run("--no-wait").returncode == 0
    assert "COMPOSE_FILE" not in deploy_local.env_values


@pytest.fixture
def deploy_local(tmp_path: Path) -> Iterator[Deploy]:
    yield Deploy(tmp_path)


def test_deploy_refuses_an_operators_compose_file_in_external_mode(external: ExternalDeploy) -> None:
    external.write_env((external.root / ".env").read_text() + "COMPOSE_FILE=docker-compose.yml\n")
    result = external.run("--no-wait")
    assert result.returncode == 3
    assert "COMPOSE_FILE in .env was not written by just deploy" in result.stdout
    assert external.ups == []
    assert external.env_values["COMPOSE_FILE"] == "docker-compose.yml"


def test_a_bare_render_from_env_equals_deploys_render_and_the_gates_files(tmp_path: Path) -> None:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("no docker binary to render the Compose files")
    for name in ("docker-compose.yml", "docker-compose.prod.yml", POSTGRES_OVERLAY, VALKEY_OVERLAY):
        shutil.copy(ROOT / name, tmp_path / name)
    shutil.copytree(ROOT / "config", tmp_path / "config")
    (tmp_path / "docker-compose.override.yml").write_text("---\nservices:\n  explore:\n    labels:\n      lab.example: proxied\n")
    files = ["docker-compose.yml", "docker-compose.prod.yml", POSTGRES_OVERLAY, VALKEY_OVERLAY, "docker-compose.override.yml"]
    env = tmp_path / ".env"
    env.write_text((ROOT / "config" / "validation.env").read_text() + "".join(f"{key}={value}\n" for key, value in BOTH.items()))
    helpers.sync_compose_file(env, files, external=True)
    run_env = {"PATH": str(Path(docker).parent), "HOME": str(Path.home())}

    def render(*args: str) -> str:
        completed = subprocess.run(
            [docker, "compose", *args, "--profile", "*", "config", "--format", "json"],
            cwd=tmp_path, capture_output=True, text=True, check=False, timeout=60, env=run_env,
        )  # fmt: skip
        assert completed.returncode == 0, completed.stderr
        return completed.stdout

    deploys = render(*[word for name in files for word in ("-f", name)])
    bare = render()
    assert bare == deploys
    assert set(json.loads(bare)["services"]) >= {"store-check-postgres", "store-check-valkey"}
    assert json.loads(bare)["services"]["api"]["environment"]["POSTGRES_HOST"] == "db.lan"
    # The gate (`--prod -f docker-compose.override.yml`, as deploy runs it) names the same files in the same order.
    args = gate.parse_args(["--prod", "-f", str(tmp_path / "docker-compose.override.yml")])
    gate_files = gate.compose_files(args, stores.read_stores(helpers.parse_env(env.read_text()), {}))
    assert [Path(name).name for name in gate_files] == files


def test_a_real_override_is_judged_against_the_stack_with_the_overlays(tmp_path: Path, external_render: dict[str, Any]) -> None:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("no docker binary to render the Compose files")

    def with_override(text: str) -> dict[str, Any]:
        (tmp_path / "override.yml").write_text(text)
        rendered: dict[str, Any] = json.loads(_render(POSTGRES_OVERLAY, VALKEY_OVERLAY, str(tmp_path / "override.yml"), env=EXTERNAL_ENV))
        return rendered

    proxied = with_override("services:\n  explore:\n    labels:\n      lab.example: proxied\n")
    assert [finding.level for finding in helpers.check_override(external_render, proxied)] == ["ok"]
    rewired = with_override("services:\n  insights:\n    environment:\n      VALKEY_HOST: other.lan\n      PGSSLMODE: disable\n")
    stops = [finding.message for finding in helpers.check_override(external_render, rewired) if finding.level == "stop"]
    assert stops == ["docker-compose.override.yml changes VALKEY_HOST on insights"]


def test_deploy_refuses_cleartext_without_the_opt_out_and_accepts_it_with_a_warning(tmp_path: Path) -> None:
    deploy = ExternalDeploy(tmp_path)
    for name in ("postgres_ca.crt", "valkey_ca.crt"):
        (tmp_path / "secrets" / name).unlink()
    plaintext = (tmp_path / ".env").read_text() + "EXTERNAL_POSTGRES_SSLMODE=disable\nEXTERNAL_VALKEY_TLS=false\n"
    deploy.write_env(plaintext)
    refused = deploy.run("--no-wait")
    assert refused.returncode == 3, refused.stdout + refused.stderr
    assert "EXTERNAL_POSTGRES_SSLMODE=disable sends credentials and data to db.lan in the clear" in refused.stdout
    assert "EXTERNAL_VALKEY_TLS=false sends credentials and data to cache.lan in the clear" in refused.stdout
    assert deploy.ups == []

    deploy.write_env(plaintext + "EXTERNAL_POSTGRES_ALLOW_PLAINTEXT=true\nEXTERNAL_VALKEY_ALLOW_PLAINTEXT=true\n")
    accepted = deploy.run("--no-wait")
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    assert "[warn] PostgreSQL at db.lan is reached in the clear" in accepted.stdout
    assert "[warn] Valkey at cache.lan is reached in the clear" in accepted.stdout
    assert (tmp_path / "secrets" / "postgres_ca.crt").read_text() == ""  # the inert placeholder the overlay mounts
    assert len(deploy.ups) == 1


def test_postgres_defaults_to_verify_full_everywhere_never_verify_ca(tmp_path: Path) -> None:
    """libpq's verify-full checks the host name as well as the chain; nothing defaults to verify-ca."""
    unset, _ = _run_check(tmp_path, "store-check-postgres.sh", {"PGSSLMODE": ""})
    assert unset.returncode == 0, unset.stderr
    assert "server certificate and host name verified (sslmode=verify-full)" in unset.stdout
    overlay = (ROOT / POSTGRES_OVERLAY).read_text()
    defaults = set(re.findall(r"\$\{EXTERNAL_POSTGRES_SSLMODE:-([a-z-]+)\}", overlay))
    assert defaults == {"verify-full"}
    assert 'sslmode="${PGSSLMODE:-verify-full}"' in (ROOT / "scripts" / "store-check-postgres.sh").read_text()
    assert stores.DEFAULT_SSLMODE == "verify-full"


# --- every service that talks to an external store ---------------------------------------

ASSET = ROOT / "skills" / "groovemap-homelab" / "assets" / "override-external-store-network.yml"
SKILL_REFERENCES = ROOT / "skills" / "groovemap-homelab" / "references"
STORE_ENV_KEYS = {
    "postgres": ("POSTGRES_HOST", "PGHOST"),
    "valkey": ("VALKEY_HOST", "VALKEY_HEALTH_ADDRESS"),
}


def _rendered_clients(render: dict[str, Any], store: str) -> set[str]:
    """Services the overlays point at the external server: those with its host in their environment."""
    marker = EXTERNAL_ENV[f"EXTERNAL_{store.upper()}_HOST"]
    found = set()
    for name, service in render["services"].items():
        if "local-stores" in service.get("profiles", []):
            continue
        environment = service.get("environment", {})
        if any(marker in str(environment.get(key, "")) for key in STORE_ENV_KEYS[store]):
            found.add(name)
    return found


@pytest.mark.parametrize("store", stores.STORES)
def test_the_client_list_is_exactly_the_services_the_overlays_point_at_the_store(external_render: dict[str, Any], store: str) -> None:
    assert _rendered_clients(external_render, store) == set(stores.STORE_CLIENTS[store])
    assert stores.STORE_CHECK_SERVICES[store] in stores.STORE_CLIENTS[store]
    assert _store_dependents(external_render, store) <= set(stores.STORE_CLIENTS[store])


def test_the_gate_and_latch_helper_path_runs_in_a_listed_client() -> None:
    argv = stores.psql_argv(["docker", "compose"], "SELECT 1", stores.Stores("external", "local"))
    assert stores.STORE_CHECK_SERVICES["postgres"] in argv
    assert stores.STORE_CHECK_SERVICES["postgres"] in stores.STORE_CLIENTS["postgres"]


def test_the_network_example_attaches_exactly_the_clients_of_each_store() -> None:
    example = yaml.safe_load(ASSET.read_text())
    attached = {store: {name for name, service in example["services"].items() if store in service["networks"]} for store in stores.STORES}
    assert attached == {store: set(stores.STORE_CLIENTS[store]) for store in stores.STORES}
    assert {name: network["external"] for name, network in example["networks"].items()} == {"postgres": True, "valkey": True}


@pytest.mark.parametrize("document", ["override.md", "shared-services.md", "discovery.md"])
def test_every_skill_reference_lists_every_store_client(document: str) -> None:
    text = (SKILL_REFERENCES / document).read_text()
    for store in stores.STORES:
        missing = [name for name in stores.STORE_CLIENTS[store] if f"`{name}`" not in text]
        assert not missing, f"{document} omits {store} clients {missing}"
    assert "override-external-store-network.yml" in text or document == "discovery.md"


def _asset_override_render() -> tuple[dict[str, Any], dict[str, Any]]:
    both = (POSTGRES_OVERLAY, VALKEY_OVERLAY)
    before = json.loads(_render(*both, env=EXTERNAL_ENV))
    after = json.loads(_render(*both, str(ASSET), env=EXTERNAL_ENV))
    return before, after


def test_the_override_check_accepts_the_network_example_and_every_client_joins_both_networks() -> None:
    before, after = _asset_override_render()
    assert [finding.level for finding in helpers.check_override(before, after)] == ["ok"]
    for store, network in (("postgres", "postgres_network"), ("valkey", "valkey_network")):
        for name in stores.STORE_CLIENTS[store]:
            assert network in helpers.service_networks(after, name), (store, name)
            assert "groovemap" in set(after["services"][name]["networks"]), name  # merged, not replaced


def test_the_data_tier_stays_pinned_and_rewiring_still_stops_beside_the_example() -> None:
    before, after = _asset_override_render()
    pinned = json.loads(json.dumps(after))
    pinned["services"]["neo4j"]["networks"] = {**pinned["services"]["neo4j"]["networks"], "postgres": None}
    assert any(finding.level == "stop" and "neo4j" in finding.message for finding in helpers.check_override(before, pinned))
    rewired = json.loads(json.dumps(after))
    rewired["services"]["store-check-postgres"]["environment"]["PGHOST"] = "elsewhere"
    assert any(finding.level == "stop" for finding in helpers.check_override(before, rewired))


# --- a check container that cannot reach the store's network -----------------------------

UNRESOLVED = 'psql: error: could not translate host name "db.lan" to address: Name or service not known\n'


def test_the_failure_message_names_the_store_host_service_and_networks(external_render: dict[str, Any]) -> None:
    message = helpers.store_check_failure("postgres", UNRESOLVED, external_render, EXTERNAL_ENV, {})
    assert message is not None
    for expected in (
        "PostgreSQL",
        "db.lan",
        "store-check-postgres",
        "groovemap",
        "attach store-check-postgres",
        "override-external-store-network.yml",
    ):
        assert expected in message
    for client in stores.STORE_CLIENTS["postgres"]:
        assert client in message
    valkey = helpers.store_check_failure(
        "valkey", "Could not connect to Valkey at cache.lan:6379: Connection refused", external_render, EXTERNAL_ENV, {}
    )
    assert valkey is not None
    assert "cache.lan" in valkey and "store-check-valkey" in valkey


def test_only_a_resolve_or_connect_failure_gets_the_network_hint(external_render: dict[str, Any]) -> None:
    for output in ("FATAL: password authentication failed for user", "refusing: PostgreSQL 16 is older than 18", "TLS certificate verify failed"):
        assert helpers.store_check_failure("postgres", output, external_render, EXTERNAL_ENV, {}) is None


def test_a_check_container_missing_the_stores_network_fails_closed_and_says_how_to_fix_it(external: ExternalDeploy, tmp_path: Path) -> None:
    render = tmp_path / "render.json"
    render.write_text(_render(POSTGRES_OVERLAY, VALKEY_OVERLAY, env=EXTERNAL_ENV))
    result = external.run(FAKE_GATE="0", FAKE_GATE_WAIT="0", FAKE_STORE_CHECK="1", FAKE_STORE_CHECK_OUTPUT=UNRESOLVED, FAKE_RENDER_FILE=str(render))
    assert result.returncode == 3, result.stdout + result.stderr
    assert "store-check-postgres refused the external store" in result.stderr
    assert "PostgreSQL host" in result.stderr and "did not resolve or accept a connection" in result.stderr
    assert "store-check-postgres, which is on the network(s): " in result.stderr and "groovemap" in result.stderr
    assert "attach store-check-postgres to that network" in result.stderr
    assert external.ups == []
    assert not any(call.startswith("gate") for call in external.calls)
    assert "COMPOSE_PROFILES" not in external.env_values


def test_a_refusal_by_the_server_itself_gets_no_network_hint(external: ExternalDeploy, tmp_path: Path) -> None:
    render = tmp_path / "render.json"
    render.write_text(_render(POSTGRES_OVERLAY, VALKEY_OVERLAY, env=EXTERNAL_ENV))
    result = external.run(
        FAKE_GATE="0",
        FAKE_GATE_WAIT="0",
        FAKE_STORE_CHECK="1",
        FAKE_STORE_CHECK_OUTPUT="FATAL: password authentication failed\n",
        FAKE_RENDER_FILE=str(render),
    )
    assert result.returncode == 3
    assert "refused the external store" in result.stderr
    assert "did not resolve" not in result.stderr
