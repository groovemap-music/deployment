"""Offline coverage for the homelab-shape smoke (scripts/smoke-homelab.sh, scripts/smoke_homelab.py).

Nothing here starts a container: the smoke itself does that, on a Linux runner
(.github/workflows/smoke-homelab.yml). These tests pin the decisions it makes without Docker: the
provisioning SQL taken from docs/homelab.md, the override generated from the skill example (which
must pass the same override check `just deploy` runs), the stack, secrets and blocked-dependent
checks, and the script's and workflow's isolation and pinning rules.
"""

from __future__ import annotations

import importlib.util
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


if TYPE_CHECKING:
    from collections.abc import Mapping


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "smoke-homelab.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "smoke-homelab.yml"
HOMELAB_COMPOSE = ROOT / "config" / "homelab-smoke" / "compose.yml"
ASSET = ROOT / "skills" / "groovemap-homelab" / "assets" / "override-external-store-network.yml"
MONITORING_ASSET = ROOT / "skills" / "groovemap-homelab" / "assets" / "override-shared-monitoring-network.yml"
NETWORK = "gm-homelab-smoke-1-homelab"
MONITORING_NETWORK = "gm-homelab-smoke-1-monitoring"
SHARED_ENV = {
    "GROOVEMAP_MONITORING": "shared",
    "SHARED_VICTORIA_METRICS_URL": "http://homelab-victoria-metrics:8428",
    "SHARED_VICTORIA_TRACES_URL": "http://homelab-victoria-traces:10428",
    "SHARED_GRAFANA_URL": "http://homelab-grafana:3000",
    "SHARED_NODE_EXPORTER_ADDRESS": "homelab-node-exporter:9100",
    "SHARED_CADVISOR_ADDRESS": "homelab-cadvisor:8080",
    "EXTERNAL_POSTGRES_EXPORTER_ADDRESS": "homelab-postgres-exporter:9187",
}
USER = "1001:118"


def _load(name: str, filename: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


smoke = _load("smoke_homelab_for_tests", "smoke_homelab.py")
helpers = _load("deploy_helpers_for_homelab_smoke", "deploy_helpers.py")
stores = smoke.stores


class ComposeLoader(yaml.SafeLoader):
    """Read Compose merge tags as the values they leave after a merge."""


def _override_tag(loader: yaml.SafeLoader, node: yaml.Node) -> Any:
    assert isinstance(node, yaml.MappingNode)
    return loader.construct_mapping(node, deep=True)


ComposeLoader.add_constructor("!override", _override_tag)


# --- provisioning SQL ------------------------------------------------------------------


def test_the_provisioning_sql_is_docs_homelab_sql_with_lux_connection_limits() -> None:
    sql = smoke.provisioning_sql((ROOT / "docs" / "homelab.md").read_text(), "s3cret")
    lines = sql.splitlines()
    assert lines[0] == "CREATE ROLE groovemap LOGIN PASSWORD 's3cret' NOSUPERUSER NOCREATEDB NOCREATEROLE;"
    assert "CREATE DATABASE groovemap OWNER groovemap;" in lines
    assert lines.index("\\c groovemap") < lines.index("CREATE EXTENSION IF NOT EXISTS pg_trgm;")
    assert lines[-2:] == ["ALTER ROLE groovemap CONNECTION LIMIT 60;", "ALTER DATABASE groovemap CONNECTION LIMIT 60;"]
    # pgvector is optional and superuser-only; the homelab image does not ship it.
    assert "vector" not in sql
    assert "<password>" not in sql
    assert smoke.CONNECTION_LIMIT == 60


def test_the_password_is_quoted_as_a_literal() -> None:
    sql = smoke.provisioning_sql((ROOT / "docs" / "homelab.md").read_text(), "it's")
    assert "PASSWORD 'it''s'" in sql


@pytest.mark.parametrize(
    ("doc", "message"),
    [
        ("no sql here", "provisioning SQL, run once"),
        ("The provisioning SQL, run once as an administrator:\n\nnothing", "no ```sql block"),
        ("The provisioning SQL, run once as an administrator:\n```sql\nSELECT 1;\n```\n", "no longer contains 'CREATE ROLE groovemap'"),
    ],
)
def test_a_changed_doc_stops_the_smoke_instead_of_provisioning_something_else(doc: str, message: str) -> None:
    with pytest.raises(smoke.SmokeError, match=re.escape(message)):
        smoke.provisioning_sql(doc, "pw")


def test_a_doc_without_the_password_placeholder_or_a_multiline_password_is_refused() -> None:
    doc = (ROOT / "docs" / "homelab.md").read_text()
    with pytest.raises(smoke.SmokeError, match="placeholder"):
        smoke.provisioning_sql(doc.replace("'<password>'", "'fixed'"), "pw")
    with pytest.raises(smoke.SmokeError, match="one non-empty line"):
        smoke.provisioning_sql(doc, "two\nlines")


# --- the override ----------------------------------------------------------------------


def _asset() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(ASSET.read_text())
    return loaded


def test_the_override_attaches_exactly_the_postgresql_clients_from_the_skill_example() -> None:
    model = smoke.override_model(_asset(), NETWORK)
    attached = {name for name, service in model["services"].items() if "networks" in service}
    assert attached == set(stores.STORE_CLIENTS["postgres"])
    for name in attached:
        assert model["services"][name]["networks"] == {"homelab": {}}
    assert model["networks"] == {"homelab": {"external": True, "name": NETWORK}}
    # The Valkey half is dropped: this shape runs its own Valkey.
    assert "store-check-valkey" not in model["services"]


def test_shared_monitoring_attaches_exactly_the_monitoring_clients_from_its_example() -> None:
    monitoring = yaml.safe_load(MONITORING_ASSET.read_text())
    model = smoke.override_model(_asset(), NETWORK, monitoring_asset=monitoring, monitoring_network=MONITORING_NETWORK)
    on_monitoring = {name for name, service in model["services"].items() if "monitoring" in service.get("networks", {})}
    assert on_monitoring == set(stores.MONITORING_CLIENTS) == {"dashboard", "otel-collector"}
    assert model["services"]["dashboard"]["networks"] == {"homelab": {}, "monitoring": {}}
    assert model["networks"]["monitoring"] == {"external": True, "name": MONITORING_NETWORK}
    with pytest.raises(smoke.SmokeError, match="needs the shared-monitoring example"):
        smoke.override_model(_asset(), NETWORK, monitoring_network=MONITORING_NETWORK)


def test_only_the_extractors_are_stand_ins() -> None:
    model = smoke.override_model(_asset(), NETWORK)
    stand_ins = {name for name, service in model["services"].items() if "entrypoint" in service}
    assert stand_ins == set(smoke.STAND_INS) == {"extractor-discogs", "extractor-musicbrainz"}
    for name in stand_ins:
        assert model["services"][name]["entrypoint"] == ["sleep", "infinity"]
        assert set(model["services"][name]) == {"entrypoint", "init", "healthcheck"}


def test_negative_cases_omit_an_attachment_or_add_environment() -> None:
    model = smoke.override_model(
        _asset(), NETWORK, omit=["store-check-postgres"], environment={"api": {"POSTGRES_HOST": "elsewhere"}, "rabbitmq-dlq-policy-init": {"X": "1"}}
    )
    assert "store-check-postgres" not in model["services"]
    assert model["services"]["api"] == {"networks": {"homelab": {}}, "environment": {"POSTGRES_HOST": "elsewhere"}}
    assert model["services"]["rabbitmq-dlq-policy-init"] == {"environment": {"X": "1"}}
    with pytest.raises(smoke.SmokeError, match="no service nope"):
        smoke.override_model(_asset(), NETWORK, omit=["nope"])


def test_set_env_entries_are_parsed_per_service() -> None:
    assert smoke.parse_environment(["api=POSTGRES_HOST=h", "api=A=b=c", "x=Y="]) == {"api": {"POSTGRES_HOST": "h", "A": "b=c"}, "x": {"Y": ""}}
    with pytest.raises(smoke.SmokeError, match="SERVICE=KEY=VALUE"):
        smoke.parse_environment(["api"])


def test_the_rendered_override_replaces_only_the_stack_networks_ipam() -> None:
    text = smoke.render_override(smoke.override_model(_asset(), NETWORK), "172.29.0.0/16")
    assert text.startswith("# Generated by scripts/smoke_homelab.py")
    loaded = yaml.load(text, Loader=ComposeLoader)  # noqa: S506 - ComposeLoader is a SafeLoader
    assert loaded["networks"]["groovemap"] == {"ipam": {"config": [{"subnet": "172.29.0.0/16"}]}}
    assert "ipam: !override" in text


def _render(*files: Path | str, env: Mapping[str, str]) -> dict[str, Any]:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("no docker binary to render the Compose files")
    argv = [docker, "compose", "--env-file", "config/validation.env", "-f", "docker-compose.yml", "-f", "docker-compose.prod.yml"]
    argv += ["-f", "docker-compose.external-postgres.yml"]
    if env.get("GROOVEMAP_MONITORING") == "shared":
        argv += ["-f", "docker-compose.shared-monitoring.yml"]
    for name in files:
        argv += ["-f", str(name)]
    argv += ["--profile", "*", "config", "--format", "json"]
    completed = subprocess.run(
        argv,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        env={"PATH": str(Path(docker).parent), "HOME": str(Path.home()), **env},
    )
    assert completed.returncode == 0, completed.stderr
    rendered: dict[str, Any] = json.loads(completed.stdout)
    return rendered


@pytest.mark.parametrize(
    ("omit", "environment", "stop"),
    [
        ((), {}, None),
        (("store-check-postgres",), {}, None),
        ((), {"rabbitmq-dlq-policy-init": {"RABBITMQ_DLQ_OVERFLOW": "not-an-overflow-mode"}}, None),
        ((), {"api": {"POSTGRES_HOST": "homelab-postgres-elsewhere"}}, "docker-compose.override.yml changes POSTGRES_HOST on api"),
    ],
)
def test_every_generated_override_meets_the_check_deploy_runs(
    tmp_path: Path, omit: tuple[str, ...], environment: dict[str, Any], stop: str | None
) -> None:
    """The smoke's override is an operator file: deploy's override check must accept it, except the rewiring case."""
    env = {"EXTERNAL_POSTGRES_HOST": "homelab-postgres"}
    override = tmp_path / "docker-compose.override.yml"
    override.write_text(smoke.render_override(smoke.override_model(_asset(), NETWORK, omit=omit, environment=environment), "172.29.0.0/16"))
    before, after = _render(env=env), _render(override, env=env)
    findings = helpers.check_override(before, after)
    stops = [finding.message for finding in findings if finding.level == "stop"]
    assert stops == ([stop] if stop else []), stops
    clients = {
        name for name, service in after["services"].items() if NETWORK in {after["networks"][key]["name"] for key in service.get("networks") or {}}
    }
    assert clients == set(stores.STORE_CLIENTS["postgres"]) - set(omit)
    assert after["networks"]["groovemap"]["ipam"]["config"] == [{"subnet": "172.29.0.0/16"}]


def test_the_lux_shape_override_meets_the_check_deploy_runs_with_shared_monitoring(tmp_path: Path) -> None:
    env = {"EXTERNAL_POSTGRES_HOST": "homelab-postgres", **SHARED_ENV}
    override = tmp_path / "docker-compose.override.yml"
    monitoring = yaml.safe_load(MONITORING_ASSET.read_text())
    model = smoke.override_model(_asset(), NETWORK, monitoring_asset=monitoring, monitoring_network=MONITORING_NETWORK)
    override.write_text(smoke.render_override(model, "172.29.0.0/16"))
    before, after = _render(env=env), _render(override, env=env)
    assert [finding.level for finding in helpers.check_override(before, after)] == ["ok"]
    on_monitoring = {
        name
        for name, service in after["services"].items()
        if MONITORING_NETWORK in {after["networks"][key]["name"] for key in service.get("networks") or {}}
    }
    assert on_monitoring == set(stores.MONITORING_CLIENTS)


# --- the stack checks ------------------------------------------------------------------


def _inspect(
    service: str,
    *,
    status: str = "running",
    exit_code: int = 0,
    health: str = "healthy",
    read_only: bool = False,
    user: str = "",
    image: str = "img",
    networks: tuple[str, ...] = ("p_groovemap",),
    started: str = "2026-10-31T00:00:00Z",
    one_off: bool = False,
) -> dict[str, Any]:
    return {
        "Name": f"/groovemap-{service}",
        "Config": {
            "Labels": {"com.docker.compose.service": service, "com.docker.compose.oneoff": "True" if one_off else "False"},
            "User": user,
            "Image": image,
        },
        "State": {"Status": status, "ExitCode": exit_code, "StartedAt": started, **({"Health": {"Status": health}} if health else {})},
        "HostConfig": {"ReadonlyRootfs": read_only},
        "NetworkSettings": {"Networks": {name: {} for name in networks}},
    }


HEALTH = {"test": ["CMD", "true"]}
RENDERED = {
    "services": {
        "rabbitmq-dlq-policy-init": {"image": "img", "restart": "no"},
        "store-check-postgres": {"image": "img", "user": USER, "read_only": True},
        "schema-init": {"image": "img", "user": USER, "depends_on": {"store-check-postgres": {"condition": "service_completed_successfully"}}},
        "api": {
            "image": "img",
            "user": USER,
            "read_only": True,
            "healthcheck": HEALTH,
            "depends_on": {"schema-init": {"condition": "service_completed_successfully"}},
        },
        "tableinator": {
            "image": "img",
            "healthcheck": HEALTH,
            "depends_on": {"rabbitmq-dlq-policy-init": {"condition": "service_completed_successfully"}},
        },
        "graphinator": {"image": "img", "depends_on": {"tableinator": {"condition": "service_started"}}},
        "valkey-exporter": {"image": "img", "user": USER, "read_only": True},
    }
}


def _good() -> list[dict[str, Any]]:
    return [
        _inspect("rabbitmq-dlq-policy-init", status="exited", health=""),
        _inspect("store-check-postgres", status="exited", health="", user=USER, read_only=True, networks=("p_groovemap", NETWORK)),
        _inspect("schema-init", status="exited", health="", user=USER, networks=("p_groovemap", NETWORK)),
        _inspect("api", user=USER, read_only=True, networks=("p_groovemap", NETWORK)),
        _inspect("tableinator", networks=("p_groovemap", NETWORK)),
        _inspect("graphinator", health=""),
        _inspect("valkey-exporter", health="", user=USER, read_only=True),
        _inspect("store-check-postgres", status="exited", health="", one_off=True),
    ]


def _failures(inspect: list[dict[str, Any]]) -> list[str]:
    findings = smoke.check_stack(RENDERED, smoke.read_containers(inspect), user=USER, homelab_network=NETWORK)
    return [finding.text for finding in findings if not finding.ok]


def test_a_stack_running_as_rendered_passes() -> None:
    findings = smoke.check_stack(RENDERED, smoke.read_containers(_good()), user=USER, homelab_network=NETWORK)
    assert findings and all(finding.ok for finding in findings), [finding.line() for finding in findings if not finding.ok]
    assert smoke.one_shots(RENDERED) == {"store-check-postgres", "schema-init", "rabbitmq-dlq-policy-init"}


@pytest.mark.parametrize(
    ("index", "change", "failure"),
    [
        (0, {"exit_code": 1}, "rabbitmq-dlq-policy-init: one-shot exited (1)"),
        (3, {"health": "unhealthy"}, "api: running, unhealthy"),
        (3, {"read_only": False}, "api: read-only root False (configured True)"),
        (6, {"user": "59000"}, "valkey-exporter: reads its 0600 secret as 59000 (owner 1001:118)"),
        (1, {"networks": ("p_groovemap",)}, f"store-check-postgres: PostgreSQL client NOT on {NETWORK}"),
        (5, {"networks": ("p_groovemap", NETWORK)}, f"graphinator: on {NETWORK} but not a PostgreSQL client (STORE_CLIENTS)"),
        (6, {"status": "restarting"}, "valkey-exporter: restarting (no healthcheck)"),
        (3, {"image": "other"}, "api: image other"),
    ],
)
def test_each_lux_failure_shape_is_caught(index: int, change: dict[str, Any], failure: str) -> None:
    inspect = _good()
    service = inspect[index]["Config"]["Labels"]["com.docker.compose.service"]
    defaults = {
        "status": inspect[index]["State"]["Status"],
        "health": (inspect[index]["State"].get("Health") or {}).get("Status", ""),
        "read_only": inspect[index]["HostConfig"]["ReadonlyRootfs"],
        "user": inspect[index]["Config"]["User"],
        "networks": tuple(inspect[index]["NetworkSettings"]["Networks"]),
    }
    inspect[index] = _inspect(service, **{**defaults, **change})
    assert failure in _failures(inspect)


def test_monitoring_clients_are_checked_on_the_monitoring_network() -> None:
    rendered = {"services": {**RENDERED["services"], "dashboard": {"image": "img"}}}
    inspect = [*_good(), _inspect("dashboard", health="", networks=("p_groovemap", NETWORK))]
    findings = smoke.check_stack(rendered, smoke.read_containers(inspect), user=USER, homelab_network=NETWORK, monitoring_network=MONITORING_NETWORK)
    assert [finding.text for finding in findings if not finding.ok] == [f"dashboard: monitoring client NOT on {MONITORING_NETWORK}"]
    inspect[-1] = _inspect("dashboard", health="", networks=("p_groovemap", NETWORK, MONITORING_NETWORK))
    inspect[3] = _inspect("api", user=USER, read_only=True, networks=("p_groovemap", NETWORK, MONITORING_NETWORK))
    findings = smoke.check_stack(rendered, smoke.read_containers(inspect), user=USER, homelab_network=NETWORK, monitoring_network=MONITORING_NETWORK)
    assert [finding.text for finding in findings if not finding.ok] == [
        f"api: on {MONITORING_NETWORK} but not a monitoring client (MONITORING_CLIENTS)"
    ]


def _console_row(name: str, status: str = "healthy", mode: str | None = "shared", url: str = "http://x", error: str | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {"name": name, "status": status, "url": url, "error": error}
    if mode is not None:
        row["mode"] = mode
    return row


def test_the_console_must_report_the_shared_services_healthy_and_shared() -> None:
    rows = [_console_row(name) for name in smoke.SHARED_ROWS] + [_console_row("catalog-api", mode="local")]
    assert all(finding.ok for finding in smoke.check_console(rows))
    # lux's failure 5: a console that predates configurable targets probes the compose name and reads unreachable.
    old = [
        _console_row(name, mode=None)
        if name != "postgres-exporter"
        else _console_row(name, "unreachable", None, "http://postgres-exporter:9187/", "Name or service not known")
        for name in smoke.SHARED_ROWS
    ]
    failures = [finding.text for finding in smoke.check_console(old) if not finding.ok]
    assert "console postgres-exporter: unreachable at http://postgres-exporter:9187/: Name or service not known" in failures
    assert f"console grafana: not probed as shared ({smoke.CONSOLE_TARGETS_HINT})" in failures
    assert "console has no cadvisor row" in [finding.text for finding in smoke.check_console(rows[:4])]


def test_the_shared_victoria_metrics_must_hold_up_for_each_job() -> None:
    answer = {"data": {"result": [{"metric": {"job": "rabbitmq"}, "value": [1, "1"]}, {"metric": {"job": "postgres-exporter"}, "value": [1, "0"]}]}}
    findings = smoke.check_up(answer, ["rabbitmq", "postgres-exporter", "cadvisor"])
    assert [finding.ok for finding in findings] == [True, False, False]
    assert findings[2].text == "shared VictoriaMetrics up{job='cadvisor'}: no series"
    assert "build_probes" in smoke.CONSOLE_PROBE and "poll_platform" in smoke.CONSOLE_PROBE


def test_a_missing_or_extra_container_is_a_failure() -> None:
    inspect = [entry for entry in _good() if entry["Name"] != "/groovemap-api"] + [_inspect("stray")]
    failures = _failures(inspect)
    assert "api: no container" in failures
    assert "stray: container groovemap-stray is not in the render" in failures


def test_two_containers_for_one_service_cannot_be_read() -> None:
    with pytest.raises(smoke.SmokeError, match="two containers for api"):
        smoke.read_containers([_inspect("api"), _inspect("api")])


def test_a_failed_one_shot_must_block_every_dependent_transitively() -> None:
    blocked = [
        _inspect("rabbitmq-dlq-policy-init", status="exited", exit_code=1, health=""),
        _inspect("tableinator", status="created", health="", started=smoke.NEVER_STARTED),
    ]
    assert smoke.dependents(RENDERED, "rabbitmq-dlq-policy-init") == {"tableinator", "graphinator"}
    findings = smoke.check_blocked(RENDERED, smoke.read_containers(blocked), "rabbitmq-dlq-policy-init")
    assert all(finding.ok for finding in findings), [finding.line() for finding in findings]
    assert "graphinator: never created" in [finding.text for finding in findings]
    started = [*blocked[:1], _inspect("tableinator")]
    assert not all(finding.ok for finding in smoke.check_blocked(RENDERED, smoke.read_containers(started), "rabbitmq-dlq-policy-init"))
    succeeded = [_inspect("rabbitmq-dlq-policy-init", status="exited", health=""), blocked[1]]
    assert not all(finding.ok for finding in smoke.check_blocked(RENDERED, smoke.read_containers(succeeded), "rabbitmq-dlq-policy-init"))


def test_secrets_must_be_0700_with_0600_files_owned_by_the_deploying_user(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir(mode=0o700)
    (secrets / "a.txt").write_text("x")
    (secrets / "a.txt").chmod(0o600)
    uid = os.getuid()
    assert all(finding.ok for finding in smoke.check_secrets(secrets, uid))
    (secrets / "a.txt").chmod(0o644)
    assert [finding.text for finding in smoke.check_secrets(secrets, uid) if not finding.ok] == ["secrets/a.txt: mode 0644, owner " + str(uid)]
    assert not all(finding.ok for finding in smoke.check_secrets(secrets, uid + 1))


def test_the_command_line_reports_and_exits_by_outcome(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rendered, inspect = tmp_path / "rendered.json", tmp_path / "inspect.json"
    rendered.write_text(json.dumps(RENDERED))
    inspect.write_text(json.dumps(_good()))
    assert smoke.main(["check-stack", "--rendered", str(rendered), "--inspect", str(inspect), "--user", USER, "--homelab-network", NETWORK]) == 0
    assert capsys.readouterr().out.startswith("ok   ")
    inspect.write_text(json.dumps(_good()[1:]))
    assert smoke.main(["check-stack", "--rendered", str(rendered), "--inspect", str(inspect), "--user", USER, "--homelab-network", NETWORK]) == 1
    assert "FAIL rabbitmq-dlq-policy-init: no container" in capsys.readouterr().out
    password = tmp_path / "pw"
    password.write_text("pw\n")
    assert smoke.main(["provision-sql", str(password)]) == 0
    assert "PASSWORD 'pw'" in capsys.readouterr().out
    out = tmp_path / "override.yml"
    assert smoke.main(["override", str(out), "--network", NETWORK, "--subnet", "172.29.0.0/16", "--set-env", "api=A=1"]) == 0
    assert "A: '1'" in out.read_text()
    assert smoke.main(["override", str(out), "--network", NETWORK, "--subnet", "s", "--set-env", "bad"]) == 2


# --- the script, the homelab server and the workflow -----------------------------------


def test_the_homelab_stand_ins_run_the_stacks_own_pinned_images() -> None:
    homelab = yaml.safe_load(HOMELAB_COMPOSE.read_text())["services"]
    stack = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
    for name, service in homelab.items():
        assert service["image"] == stack[name.removeprefix("homelab-")]["image"], name
        assert "ports" not in service, name
    assert set(homelab) == {f"homelab-{name}" for name in ("postgres", *smoke.SHARED_ROWS)}


def test_the_homelab_server_runs_the_stacks_pinned_postgres_with_tls() -> None:
    homelab = yaml.safe_load(HOMELAB_COMPOSE.read_text())["services"]["homelab-postgres"]
    stack = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]["postgres"]
    assert homelab["image"] == stack["image"]
    assert re.search(r"@sha256:[0-9a-f]{64}$", homelab["image"])
    assert "ssl=on" in homelab["command"]
    assert homelab["environment"]["POSTGRES_USER"] != "groovemap"
    assert "ports" not in homelab


def test_the_script_is_disposable_and_scoped_to_its_own_projects() -> None:
    text = SCRIPT.read_text()
    assert "trap cleanup EXIT" in text
    # Teardown is by the Compose project label, never by name patterns another project could match.
    assert 'containers() { docker ps -aq --filter "label=$LABEL=$1"; }' in text
    assert "docker system prune" not in text and "docker volume prune" not in text
    assert "filter name=^groovemap-" in text  # refuses to run beside an existing stack
    # Operator recipes run with uv off PATH; the smoke's own helpers may use it.
    assert "just deploy --no-wait --no-admin --force" in text
    assert 'PATH="$NO_UV_PATH"' in text
    for case in (
        "plaintext-sslmode",
        "store-check-off-network",
        "wrong-ca",
        "override-rewires-postgres-host",
        "valkey-exporter as 59000",
        "invalid collector config",
        "failing one-shot",
    ):
        assert case in text


def test_the_script_passes_shellcheck() -> None:
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck is not installed")
    completed = subprocess.run([shellcheck, "-x", str(SCRIPT)], capture_output=True, text=True, check=False, timeout=60)
    assert completed.returncode == 0, completed.stdout


@pytest.mark.parametrize(
    ("candidate", "version", "revision", "reason"),
    [
        ("ghcr.io/groovemap-music/discogs-graph-enricher:latest", "v0.4.0", "a" * 40, "digest-pinned"),
        ("ghcr.io/groovemap-music/discogs-graph-enricher@sha256:" + "a" * 64, "", "", "expected OCI version"),
        ("ghcr.io/groovemap-music/discogs-graph-enricher@sha256:" + "a" * 64, "v0.4.0", "short", "full source revision"),
    ],
)
def test_graph_candidate_attestation_is_required_before_docker(tmp_path: Path, candidate: str, version: str, revision: str, reason: str) -> None:
    tools = tmp_path / "bin"
    tools.mkdir()
    marker = tmp_path / "docker-called"
    for tool in ("docker", "git", "python3", "openssl", "just", "uv", "tar"):
        stub = tools / tool
        stub.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 99\n")
        stub.chmod(0o755)
    uname = tools / "uname"
    uname.write_text("#!/bin/sh\necho Linux\n")
    uname.chmod(0o755)
    bash = shutil.which("bash")
    assert bash is not None
    result = subprocess.run(
        [bash, str(SCRIPT)],
        env={
            **os.environ,
            "PATH": f"{tools}:/usr/bin:/bin",
            "TMPDIR": str(tmp_path),
            "RUNNER_TEMP": str(tmp_path),
            "SMOKE_HOMELAB_SQL_LOADER_IMAGE": "",
            "SMOKE_HOMELAB_GRAPH_LOADER_IMAGE": candidate,
            "SMOKE_HOMELAB_GRAPH_LOADER_VERSION": version,
            "SMOKE_HOMELAB_GRAPH_LOADER_REVISION": revision,
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 2
    assert reason in result.stderr
    assert not marker.exists()


def test_the_workflow_runs_on_linux_with_pinned_tools_and_no_secrets() -> None:
    text = WORKFLOW.read_text()
    workflow = yaml.safe_load(text)
    triggers = workflow[True]  # PyYAML reads the bare `on` key as True
    assert set(triggers) == {"pull_request", "push", "workflow_dispatch"}
    # Ordinary CI must follow future reviewed promotions; candidates are explicit manual inputs.
    for loader in ("sql", "graph"):
        assert triggers["workflow_dispatch"]["inputs"][f"{loader}_loader_image"]["default"] == ""
    assert "main" in triggers["push"]["branches"]
    assert workflow["permissions"] == {"contents": "read"}
    job = workflow["jobs"]["smoke"]
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["timeout-minutes"] <= 60
    for reference in re.findall(r"uses:\s+(\S+)", text):
        assert re.search(r"@[0-9a-f]{40}$", reference), reference
    assert "secrets." not in text
    assert "47737f4236e6d3ccea7291995203f8f018058783b4c3a9469119811a0b5db8c0" in text  # Docker Compose 5.6.1, lux's version
    assert "just smoke-homelab" in text
    assert 'getent passwd "$SMOKE_HOST_UID"' in text  # lux's operator UID
    # The label opt-in and the path filter agree on what counts as a change to the stack.
    for path in ("docker-compose", "scripts/", "config/", "skills/"):
        assert path in text
