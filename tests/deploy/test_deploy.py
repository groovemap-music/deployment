"""Regression tests for `just deploy` (scripts/deploy.sh and scripts/deploy_helpers.py).

Nothing here starts a container. The helpers' decisions are pure and tested in-process; the
shell script runs from a scratch copy of the repository layout with a fake `docker` and a fake
import gate on PATH, so every Docker call is recorded instead of made, and the real repository's
.env and secrets/ are never touched.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from tests.deploy.test_docker_compose_prod import _load_compose


if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence


ROOT = Path(__file__).resolve().parents[2]
GIB = 1024**3
_BASH = shutil.which("bash") or "/bin/bash"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("deploy_helpers", ROOT / "scripts" / "deploy_helpers.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


helpers = _load()


# --- .env handling ---------------------------------------------------------------------


def test_parse_env_reads_assignments_like_compose() -> None:
    text = "# COMPOSE_PROFILES=musicbrainz\nA=1\nexport B=two\nC='quoted # kept'\nD=value # comment\nE=\n  # F=no\n"
    assert helpers.parse_env(text) == {"A": "1", "B": "two", "C": "quoted # kept", "D": "value", "E": ""}


def test_set_env_values_replaces_in_place_and_appends_the_rest_under_a_comment() -> None:
    text = "# COMPOSE_PROFILES=musicbrainz\nNEO4J_HEAP_SIZE=2G\nOTHER=x"
    updated = helpers.set_env_values(text, {"NEO4J_HEAP_SIZE": "8g", "COMPOSE_PROFILES": "musicbrainz"}, "# added")
    assert updated == "# COMPOSE_PROFILES=musicbrainz\nNEO4J_HEAP_SIZE=8g\nOTHER=x\n\n# added\nCOMPOSE_PROFILES=musicbrainz\n"


def test_shell_environment_outranks_env_file_and_empty_means_default() -> None:
    assert helpers.effective("K", {"K": "file"}, {"K": "shell"}) == "shell"
    assert helpers.effective("K", {"K": "file"}, {}) == "file"
    assert helpers.effective("K", {"K": ""}, {}, "default") == "default"


def test_profiles_merge_without_duplicates() -> None:
    assert helpers.merge_profiles("") == "musicbrainz"
    assert helpers.merge_profiles("jobs") == "jobs,musicbrainz"
    assert helpers.merge_profiles("musicbrainz, jobs") == "musicbrainz,jobs"
    assert helpers.has_profile("jobs, musicbrainz")
    assert not helpers.has_profile("musicbrainz-extra")


@pytest.mark.parametrize(
    ("explicit", "env_values", "expected"),
    [("Custom.Name", {}, "customname"), (None, {"COMPOSE_PROJECT_NAME": "fromenv"}, "fromenv"), (None, {}, "deployment")],
)
def test_project_name_follows_compose(explicit: str | None, env_values: dict[str, str], expected: str) -> None:
    assert helpers.project_name(explicit, env_values, {}, Path("/srv/Deployment")) == expected


# --- sizing ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("total", "expected"),
    [
        (8, (1, 3, 5, 1)),
        (16, (2, 6, 12, 1)),
        (24, (4, 8, 20, 2)),
        (32, (5, 11, 28, 3)),
        (48, (8, 16, 44, 5)),
        (64, (8, 16, 48, 5)),
        (256, (8, 16, 48, 5)),
    ],
)
def test_neo4j_sizing_formula(total: int, expected: tuple[int, int, int, int]) -> None:
    sizing = helpers.neo4j_sizing(total)
    assert (sizing.heap, sizing.pagecache, sizing.limit, sizing.transaction) == expected
    assert sizing.heap + sizing.pagecache < sizing.limit
    if total >= 16:
        assert sizing.limit <= total - 4
        # The transaction cap lives inside the heap and only bounds anything below it.
        assert sizing.transaction < sizing.heap


def test_large_hosts_get_the_overlay_defaults_with_a_transaction_cap_below_the_heap() -> None:
    overlay = (ROOT / "docker-compose.prod.yml").read_text()
    sizing = helpers.neo4j_sizing(64).env()
    for key in ("NEO4J_HEAP_SIZE", "NEO4J_PAGECACHE_SIZE", "NEO4J_MEMORY_LIMIT"):
        assert f"${{{key}:-{sizing[key]}}}" in overlay
    # The overlay's own 8G default equals its 8g heap, so the formula's 5g replaces it.
    assert "NEO4J_dbms_memory_transaction_total_max: ${NEO4J_TRANSACTION_MEMORY_MAX:-8G}" in overlay
    assert sizing["NEO4J_TRANSACTION_MEMORY_MAX"] == "5g"


@pytest.mark.parametrize(("heap", "expected"), [(0.5, 1), (1, 1), (2, 1), (4.5, 3), (8, 5), (16, 11)])
def test_transaction_cap_is_70_percent_of_the_heap_floor_1g(heap: float, expected: int) -> None:
    assert helpers.transaction_max_gb(heap) == expected


@pytest.mark.parametrize(
    ("value", "gb"), [("2G", 2.0), ("4096m", 4.0), ("1048576k", 1.0), ("8589934592", 8.0), ("1.5g", 1.5), ("lots", None), ("", None)]
)
def test_neo4j_memory_values_are_read_in_gb(value: str, gb: float | None) -> None:
    assert helpers.memory_gb(value) == gb


def test_memory_rounds_up_to_what_the_machine_was_sold_as() -> None:
    assert helpers.nominal_gb(int(15.4 * GIB)) == 16
    assert helpers.nominal_gb(32 * GIB) == 32


def test_template_heap_counts_as_unset_but_an_operator_choice_is_kept() -> None:
    template = helpers.parse_env((ROOT / ".env.example").read_text())
    assert template["NEO4J_HEAP_SIZE"] == "2G", "the template value this rule is about"
    sizing = helpers.neo4j_sizing(32)
    assert helpers.plan_sizing("NEO4J_HEAP_SIZE=2G\n", template, sizing) == sizing.env()
    # An operator's heap is kept, and the transaction cap follows it rather than the formula's 5g.
    assert helpers.plan_sizing("NEO4J_HEAP_SIZE=6144m\nNEO4J_PAGECACHE_SIZE=\n", template, sizing) == {
        "NEO4J_PAGECACHE_SIZE": "11g",
        "NEO4J_MEMORY_LIMIT": "28G",
        "NEO4J_TRANSACTION_MEMORY_MAX": "4g",
    }
    assert helpers.plan_sizing("NEO4J_HEAP_SIZE=big\n", template, sizing)["NEO4J_TRANSACTION_MEMORY_MAX"] == "3g"
    assert helpers.plan_sizing("NEO4J_TRANSACTION_MEMORY_MAX=2g\n", template, sizing).keys() == set(helpers.SIZING_KEYS) - {
        "NEO4J_TRANSACTION_MEMORY_MAX"
    }
    assert helpers.plan_sizing(f"{helpers.SIZING_MARKER}\nNEO4J_HEAP_SIZE=2G\n", template, sizing) == {}


def test_configure_writes_sizing_and_base_url_once(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("NEO4J_HEAP_SIZE=2G\n# APP_BASE_URL=\n")
    messages = helpers.configure(env, ROOT / ".env.example", 32 * GIB, "lab", {})
    values = helpers.parse_env(env.read_text())
    assert values["NEO4J_HEAP_SIZE"] == "5g"
    assert values["NEO4J_PAGECACHE_SIZE"] == "11g"
    assert values["NEO4J_MEMORY_LIMIT"] == "28G"
    assert values["NEO4J_TRANSACTION_MEMORY_MAX"] == "3g"
    assert values["APP_BASE_URL"] == "http://lab:8006"
    assert "Sized Neo4j" in messages[0]

    env.write_text(env.read_text().replace("NEO4J_HEAP_SIZE=5g", "NEO4J_HEAP_SIZE=6g"))
    before = env.read_text()
    messages = helpers.configure(env, ROOT / ".env.example", 128 * GIB, "lab", {})
    assert env.read_text() == before
    assert "Keeping the Neo4j sizing" in messages[0]


def test_configure_keeps_a_set_base_url_and_uses_the_web_bind_address(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("APP_BASE_URL=https://music.example\n")
    helpers.configure(env, ROOT / ".env.example", 64 * GIB, "lab", {})
    assert helpers.parse_env(env.read_text())["APP_BASE_URL"] == "https://music.example"
    env.write_text("WEB_BIND_ADDRESS=192.168.1.5\nEXPLORE_HOST_PORT=9006\n")
    helpers.configure(env, ROOT / ".env.example", 64 * GIB, "lab", {})
    assert helpers.parse_env(env.read_text())["APP_BASE_URL"] == "http://192.168.1.5:9006"


# --- preflight findings ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "level"),
    [(None, "stop"), ("1.29.2", "stop"), ("garbage", "stop"), ("v2.29.1", "ok"), ("5.6.1", "ok")],
)
def test_compose_version(version: str | None, level: str) -> None:
    assert helpers.check_compose(version).level == level


def test_engine_finding() -> None:
    assert helpers.check_engine(None, "permission denied").level == "stop"
    assert "Docker Desktop" in helpers.check_engine({"ServerVersion": "29.5.2", "OperatingSystem": "Docker Desktop"}, "").message


def test_arm64_needs_confirmation_and_says_how_to_emulate() -> None:
    assert helpers.check_architecture("x86_64", desktop=False, amd64_emulation=None).level == "ok"
    linux = helpers.check_architecture("aarch64", desktop=False, amd64_emulation=False)
    assert linux.level == "confirm"
    assert "linux/amd64 only" in linux.message
    assert "binfmt" in linux.hint
    assert "registered" in helpers.check_architecture("aarch64", desktop=False, amd64_emulation=True).hint
    assert "Rosetta" in helpers.check_architecture("aarch64", desktop=True, amd64_emulation=None).hint


@pytest.mark.parametrize(
    ("mode", "gib", "level"),
    [("prod", 12, "stop"), ("prod", 15.4, "warn"), ("prod", 31.3, "ok"), ("dev", 7, "stop"), ("dev", 8, "warn"), ("dev", 16, "ok")],
)
def test_memory_minimums(mode: str, gib: float, level: str) -> None:
    finding = helpers.check_memory(int(gib * GIB), mode, desktop=False)
    assert finding.level == level
    assert finding.forcible == (level == "stop")


def test_docker_desktop_memory_points_at_its_settings() -> None:
    assert "Settings > Resources" in helpers.check_memory(8 * GIB, "prod", desktop=True).message


def volume(name: str) -> Any:
    return next(item for item in helpers.DATA_VOLUMES if item.name == name)


def mounts(**overrides: dict[str, Any]) -> dict[str, Any]:
    """A rendered stack (`docker compose config --format json`) with every data volume named."""
    services: dict[str, Any] = {
        item.service: {"volumes": [{"type": "volume", "source": item.name, "target": item.target}]} for item in helpers.DATA_VOLUMES
    }
    return {"services": services, "volumes": {item.name: {} for item in helpers.DATA_VOLUMES}} | overrides


def bind_volume(device: str) -> dict[str, Any]:
    return {"driver": "local", "driver_opts": {"type": "none", "o": "bind", "device": device}}


class Disks:
    """A stub for the filesystem probe: the longest matching prefix decides the disk and its free GB."""

    def __init__(self, **free_gb: int) -> None:
        self.free_gb = free_gb  # prefix -> free GB; prefix "/tank" and "/docker" are two filesystems

    def __call__(self, path: str) -> tuple[int, int]:
        prefix = max((p for p in self.free_gb if path.startswith(p)), key=len, default=None)
        if prefix is None:
            raise FileNotFoundError(2, "No such file or directory")
        return hash(prefix), self.free_gb[prefix] * 10**9


def run_disk_check(config: dict[str, Any] | None, disks: Disks, *, local: bool = True, desktop: bool = False, existing: bool = False) -> list[Any]:
    root = "/docker"
    locations = helpers.resolve_locations(config, root, local, desktop)
    findings: list[Any] = helpers.check_volumes(locations, root, local, desktop, existing, disks)
    return findings


@pytest.mark.parametrize(
    ("free_gb", "existing", "level"),
    [(80, False, "stop"), (150, False, "warn"), (250, False, "ok"), (15, True, "warn"), (80, True, "ok")],
)
def test_default_volumes_share_the_docker_root_and_keep_the_old_minimums(free_gb: int, existing: bool, level: str) -> None:
    (finding,) = run_disk_check(mounts(), Disks(**{"/docker": free_gb}), existing=existing)
    assert finding.level == level


def test_the_shares_add_up_to_the_documented_minimum_and_recommendation() -> None:
    shares = [*helpers.DATA_VOLUMES, helpers.DOCKER_ROOT_SHARE]
    assert sum(item.minimum_gb for item in shares) == 100
    assert sum(item.recommended_gb for item in shares) == 200
    # The Discogs numbers come from docs/architecture.md: 48 GB PostgreSQL, 28 GB Neo4j, ~11.3 GB dump.
    assert (volume("postgres_data").minimum_gb, volume("neo4j_data").minimum_gb, volume("discogs_data").minimum_gb) == (48, 28, 12)


def test_a_relocated_override_bind_is_measured_where_it_lives() -> None:
    config = mounts()
    config["services"]["postgres"]["volumes"] = [{"type": "bind", "source": "/tank/pg", "target": "/var/lib/postgresql/data"}]
    findings = run_disk_check(config, Disks(**{"/docker": 30, "/tank": 400}))
    docker, tank = sorted(findings, key=lambda finding: "/tank" in finding.message)
    assert tank.level == "ok"
    assert "postgres_data" in tank.message
    # postgres_data left the data root, so what is left there needs 100 - 48 = 52 GB: 30 GB is not enough.
    assert docker.level == "stop"
    assert "postgres_data" not in docker.message
    assert "52 GB" in docker.message


def test_named_volume_driver_options_move_the_volume_too() -> None:
    config = mounts()
    for name in ("postgres_data", "neo4j_data", "discogs_data", "musicbrainz_data"):
        config["volumes"][name] = bind_volume(f"/tank/{name}")
    # The data root keeps only 8 GB of images and 4 GB of small volumes; /tank holds the 88 GB of data.
    findings = run_disk_check(config, Disks(**{"/docker": 15, "/tank": 120}))
    levels = {("/tank" in finding.message): finding.level for finding in findings}
    assert levels == {True: "warn", False: "ok"}
    tank = next(finding for finding in findings if "/tank" in finding.message)
    assert "recommended" in tank.message
    assert "estimate" in tank.message


def test_two_filesystems_are_each_judged_against_their_own_share() -> None:
    config = mounts()
    config["volumes"]["postgres_data"] = bind_volume("/tank/pg")
    config["volumes"]["neo4j_data"] = bind_volume("/ssd/neo4j")
    findings = run_disk_check(config, Disks(**{"/docker": 100, "/tank": 60, "/ssd": 20}))
    by_disk = {("/tank" if "/tank" in finding.message else "/ssd" if "/ssd" in finding.message else "/docker"): finding for finding in findings}
    assert by_disk["/tank"].level == "ok"  # PostgreSQL alone needs 48 GB
    assert by_disk["/ssd"].level == "stop"  # 28 GB needed
    assert by_disk["/docker"].level == "warn"  # the 24 GB minimum fits; MusicBrainz's estimate does not
    assert "neo4j_data" in by_disk["/ssd"].message


def test_volumes_on_one_filesystem_pool_their_shares() -> None:
    config = mounts()
    config["volumes"]["postgres_data"] = bind_volume("/tank/pg")
    config["volumes"]["neo4j_data"] = bind_volume("/tank/neo4j")
    findings = run_disk_check(config, Disks(**{"/docker": 500, "/tank": 70}))
    tank = next(finding for finding in findings if "/tank" in finding.message)
    # 48 + 28 = 76 GB needed together; 70 GB would pass either volume alone.
    assert tank.level == "stop"
    assert "76 GB" in tank.message


@pytest.mark.parametrize(
    "driver_opts",
    [{"type": "nfs", "o": "addr=nas,rw", "device": ":/export"}, {"type": "none", "o": "ro", "device": "/x"}],
)
def test_network_and_non_bind_mounts_are_unmeasurable(driver_opts: dict[str, str]) -> None:
    config = mounts()
    config["volumes"]["postgres_data"] = {"driver": "local", "driver_opts": driver_opts}
    findings = run_disk_check(config, Disks(**{"/docker": 500}))
    unmeasured = next(finding for finding in findings if "Could not measure" in finding.message)
    assert "postgres_data" in unmeasured.message
    assert unmeasured.level == "warn"


@pytest.mark.parametrize(("local", "desktop"), [(False, False), (True, True)])
def test_an_unmeasurable_host_warns_with_the_sizes(local: bool, desktop: bool) -> None:
    (finding,) = run_disk_check(mounts(), Disks(), local=local, desktop=desktop)
    assert finding.level == "warn"
    assert "Could not measure free disk space" in finding.message
    assert "100 GB" in finding.message
    assert "200 GB" in finding.message


def test_docker_desktop_still_measures_a_host_bind_mount() -> None:
    config = mounts()
    config["services"]["postgres"]["volumes"] = [{"type": "bind", "source": "/tank/pg", "target": "/var/lib/postgresql/data"}]
    findings = run_disk_check(config, Disks(**{"/tank": 300}), desktop=True)
    assert [finding.level for finding in findings if "Could not" not in finding.message] == ["ok"]
    assert any("Could not measure" in finding.message and "postgres_data" not in finding.message for finding in findings)


def test_a_path_that_cannot_be_read_is_reported_not_raised() -> None:
    config = mounts()
    config["volumes"]["postgres_data"] = bind_volume("/nowhere/pg")
    findings = run_disk_check(config, Disks(**{"/docker": 500}))
    assert any("Could not measure" in finding.message and "/nowhere/pg" in finding.message for finding in findings)


def test_a_render_without_a_service_skips_its_volume() -> None:
    config = mounts()
    del config["services"]["extractor-musicbrainz"]
    (finding,) = run_disk_check(config, Disks(**{"/docker": 500}))
    assert "musicbrainz_data" not in finding.message


def test_probe_path_walks_up_to_the_nearest_existing_directory(tmp_path: Path) -> None:
    device, free = helpers.probe_path(str(tmp_path / "not" / "yet" / "created"))
    assert device == tmp_path.stat().st_dev
    assert free > 0


def test_disk_check_command_reads_the_render_and_honours_force(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = mounts()
    config["volumes"]["postgres_data"] = bind_volume("/tank/pg")
    render = tmp_path / "config.json"
    render.write_text(json.dumps(config))
    docker = helpers.Docker(FakeRunner(docker_info(DockerRootDir="/docker")))
    low = Disks(**{"/docker": 500, "/tank": 10})
    assert helpers.disk_check(config, force=False, docker=docker, environ={}, probe=low) == 3
    assert "[stop]" in capsys.readouterr().out
    assert helpers.disk_check(config, force=True, docker=docker, environ={}, probe=low) == 0
    assert "going ahead below the disk minimums" in capsys.readouterr().out
    assert helpers.main(["disk-check", str(tmp_path / "missing.json")], docker=docker, environ={}) == 1
    assert helpers.disk_check(None, force=False, docker=helpers.Docker(FakeRunner(info={})), environ={}) == 1


def test_configured_ports_follow_the_overlay_bind_groups() -> None:
    prod = {port.variable: port for port in helpers.configured_ports("prod", {"POSTGRES_HOST_PORT": "6543"}, {"DATA_BIND_ADDRESS": "10.0.0.2"})}
    dev = {port.variable: port for port in helpers.configured_ports("dev", {}, {})}
    assert len(prod) == 15
    assert (prod["POSTGRES_HOST_PORT"].address, prod["POSTGRES_HOST_PORT"].port) == ("10.0.0.2", 6543)
    assert prod["VALKEY_HOST_PORT"].address == "127.0.0.1"
    assert dev["VALKEY_HOST_PORT"].address == ""
    assert prod["GRAFANA_HOST_PORT"].port == 3000


def test_port_table_covers_every_published_host_port() -> None:
    compose = (ROOT / "docker-compose.yml").read_text() + (ROOT / "docker-compose.prod.yml").read_text()
    published = dict(re.findall(r"\$\{([A-Z0-9_]+_HOST_PORT):-(\d+)\}", compose))
    assert published == {variable: str(default) for variable, default, *_ in helpers.PORTS}


def _bind_groups(name: str) -> dict[str, str]:
    lines = (ROOT / name).read_text().splitlines()
    return {
        port: group.lower()
        for line in lines
        for group, port in re.findall(r"\$\{(WEB|DATA|INTERNAL)_BIND_ADDRESS.*?\$\{([A-Z0-9_]+_HOST_PORT):-", line)
    }


def test_port_bind_groups_match_the_compose_files() -> None:
    base = _bind_groups("docker-compose.yml")
    prod = base | _bind_groups("docker-compose.prod.yml")
    assert {variable: dev for variable, _, _, dev, _ in helpers.PORTS} == base
    assert {variable: group for variable, _, group, _, _ in helpers.PORTS} == prod


def test_data_tier_is_every_service_published_on_the_data_or_internal_address() -> None:
    services = {**_load_compose(ROOT / "docker-compose.yml")["services"]}
    for name, overlay in _load_compose(ROOT / "docker-compose.prod.yml")["services"].items():
        services[name] = {**services.get(name, {}), **overlay}
    published = {name for name, service in services.items() if re.search(r"(DATA|INTERNAL)_BIND_ADDRESS", str(service.get("ports", "")))}
    assert published == set(helpers.DATA_TIER)


def _render(**services: dict[str, Any]) -> dict[str, Any]:
    stack: dict[str, dict[str, Any]] = {
        "postgres": {"image": "postgres@sha256:aa", "container_name": "groovemap-postgres", "ports": [{"target": 5432, "published": "5433"}]},
        "explore": {"image": "explore@sha256:bb", "container_name": "groovemap-explore", "networks": {"groovemap": None}},
        "extractor-musicbrainz": {"image": "mb@sha256:cc", "container_name": "groovemap-extractor-musicbrainz", "profiles": ["musicbrainz"]},
    }
    return {"services": {name: {**service, **services.get(name, {})} for name, service in stack.items()}}


PINNED = "backup@sha256:" + "ab" * 32
FORWARDER = {"image": PINNED, "ports": [{"target": 5432, "published": "5432"}], "networks": {"groovemap": None}}


def _with_new(name: str, service: dict[str, Any]) -> dict[str, Any]:
    after = _render()
    after["services"][name] = service
    after["networks"] = {"groovemap": {"name": "lab_groovemap"}, "proxy": {"name": "proxy_network"}}
    return after


def test_override_may_add_proxy_networks_labels_storage_and_restart_policies() -> None:
    proxied = _render(
        explore={"networks": {"groovemap": None, "proxy": {}}, "labels": {"traefik.enable": "true"}, "restart": "unless-stopped"},
        postgres={"volumes": [{"type": "bind", "source": "/mnt/tank/pg", "target": "/var/lib/postgresql/data"}]},
    )
    proxied["services"]["sidecar"] = {"image": PINNED}
    findings = helpers.check_override(_render(), proxied)
    assert [finding.level for finding in findings] == ["ok"]


@pytest.mark.parametrize(
    ("service", "change", "why"),
    [
        ("explore", {"image": "explore:latest"}, "never a tag or `latest`"),
        ("explore", {"build": {"context": "."}}, "never builds them"),
        ("explore", {"platform": "linux/arm64"}, "linux/amd64 only"),
        ("explore", {"container_name": "explore"}, "groovemap-* container names"),
        ("extractor-musicbrainz", {"profiles": []}, "import gate reports complete"),
        ("postgres", {"ports": [{"target": 5432, "published": "5432", "host_ip": "192.168.1.20"}]}, "DATA_BIND_ADDRESS"),
        ("postgres", {"network_mode": "host"}, "stay off the LAN"),
    ],
)
def test_override_must_not_bypass_digests_the_gate_or_the_data_tier_binds(service: str, change: dict[str, Any], why: str) -> None:
    findings = helpers.check_override(_render(), _render(**{service: change}))
    assert [finding.level for finding in findings] == ["stop"]
    assert service in findings[0].message
    assert why in findings[0].hint


@pytest.mark.parametrize(
    ("service", "change", "rule"),
    [
        ("postgres", {"networks": {"groovemap": None, "proxy": {}}}, "changes networks on postgres"),
        ("neo4j", {"labels": {"traefik.tcp.routers.bolt.rule": "HostSNI(`*`)"}}, "changes labels on neo4j"),
        ("rabbitmq", {"extra_hosts": ["postgres:10.0.0.9"]}, "changes extra_hosts on rabbitmq"),
    ],
)
def test_override_keeps_the_data_tier_off_proxies_and_hosts_entries(service: str, change: dict[str, Any], rule: str) -> None:
    before = _render(neo4j={"image": "n@sha256:dd"}, rabbitmq={"image": "r@sha256:ee"})
    before["services"]["neo4j"] = {"image": "n@sha256:dd"}
    before["services"]["rabbitmq"] = {"image": "r@sha256:ee"}
    after = json.loads(json.dumps(before))
    after["services"].setdefault(service, {}).update(change)
    findings = helpers.check_override(before, after)
    assert [finding.level for finding in findings] == ["stop"]
    assert rule in findings[0].message


def test_override_may_still_proxy_a_service_outside_the_data_tier() -> None:
    after = _render(explore={"networks": {"groovemap": None, "proxy": {}}, "labels": {"traefik.enable": "true"}, "extra_hosts": ["a:10.0.0.1"]})
    assert [finding.level for finding in helpers.check_override(_render(), after)] == ["ok"]


@pytest.mark.parametrize("image", ["backup:latest", "backup@sha256:aa", ""])
def test_a_new_service_needs_a_digest_pinned_image(image: str) -> None:
    findings = helpers.check_override(_render(), _with_new("backup", {"image": image}), frozenset({"backup"}))
    assert [finding.level for finding in findings] == ["stop"]
    assert "adds the backup service with an image that is not pinned by digest" in findings[0].message


def test_a_new_forwarder_publishing_a_port_into_the_stack_network_stops() -> None:
    findings = helpers.check_override(_render(), _with_new("pg-forward", FORWARDER))
    assert [finding.level for finding in findings] == ["stop", "stop"]
    assert "adds the pg-forward service and publishes ports" in findings[0].message
    assert "adds the pg-forward service on the groovemap network" in findings[1].message
    assert "OVERRIDE_ALLOW_SERVICES" in findings[0].hint


def test_a_new_service_on_a_renamed_stack_network_or_a_shared_namespace_counts_as_joining() -> None:
    renamed = _with_new("tool", {"image": PINNED, "networks": {"proxy": None}})
    renamed["networks"]["proxy"] = {"name": "lab_groovemap"}
    assert "on the groovemap network" in helpers.check_override(_render(), renamed)[0].message
    shared = _with_new("tool", {"image": PINNED, "network_mode": "service:postgres"})
    assert "on the groovemap network" in helpers.check_override(_render(), shared)[0].message


def test_an_allowlisted_new_service_may_publish_and_join_but_still_needs_a_digest() -> None:
    assert [finding.level for finding in helpers.check_override(_render(), _with_new("pg-forward", FORWARDER), frozenset({"pg-forward"}))] == ["ok"]
    unpinned = _with_new("pg-forward", {**FORWARDER, "image": "alpine/socat:latest"})
    assert [finding.level for finding in helpers.check_override(_render(), unpinned, frozenset({"pg-forward"}))] == ["stop"]


def test_a_new_service_on_the_default_network_only_is_allowed() -> None:
    assert [
        finding.level for finding in helpers.check_override(_render(), _with_new("backup", {"image": PINNED, "networks": {"default": None}}))
    ] == ["ok"]


def test_the_allowlist_variable_comes_from_env_or_the_shell() -> None:
    assert helpers.parse_allowed_services("a, b  c,") == {"a", "b", "c"}


def test_override_check_command_honours_the_allowlist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    before, after = tmp_path / "before.json", tmp_path / "after.json"
    before.write_text(json.dumps(_render()))
    after.write_text(json.dumps(_with_new("pg-forward", FORWARDER)))
    monkeypatch.delenv("OVERRIDE_ALLOW_SERVICES", raising=False)
    assert helpers.main(["override-check", str(before), str(after)]) == 3
    monkeypatch.setenv("OVERRIDE_ALLOW_SERVICES", "pg-forward")
    assert helpers.main(["override-check", str(before), str(after)]) == 0


def _stack_with_volumes() -> dict[str, Any]:
    stack = _render(
        postgres={"volumes": [{"type": "volume", "source": "postgres_data", "target": "/var/lib/postgresql/data"}]},
        explore={"volumes": [{"type": "volume", "source": "explore_cache", "target": "/cache"}]},
    )
    stack["volumes"] = {"postgres_data": {}, "explore_cache": {}, "unused_data": {}}
    return stack


def _stack_after(service: str, change: dict[str, Any], volumes: dict[str, Any] | None = None) -> dict[str, Any]:
    after: dict[str, Any] = json.loads(json.dumps(_stack_with_volumes()))
    after["services"].setdefault(service, {"image": PINNED}).update(change)
    after["volumes"].update(volumes or {})
    return after


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("privileged", True),
        ("cap_add", ["SYS_ADMIN"]),
        ("devices", ["/dev/sda:/dev/sda"]),
        ("security_opt", ["seccomp:unconfined"]),
        ("pid", "host"),
        ("ipc", "service:postgres"),
        ("uts", "host"),
        ("cgroup", "host"),
        ("volumes_from", ["postgres"]),
    ],
)
def test_existing_service_may_not_gain_host_access(key: str, value: Any) -> None:
    findings = helpers.check_override(_stack_with_volumes(), _stack_after("explore", {key: value}))
    assert [finding.level for finding in findings] == ["stop"]
    assert f"adds {key} to explore" in findings[0].message
    assert "no opt-in" in findings[0].hint


@pytest.mark.parametrize(
    "mount",
    [
        {"type": "bind", "source": "/var/run/docker.sock", "target": "/var/run/docker.sock"},
        {"type": "bind", "source": "/run/podman/podman.sock", "target": "/p.sock"},
        {"type": "bind", "source": "/run/user/1000/podman/podman.sock", "target": "/p.sock"},
        {"type": "bind", "source": "/var/lib/docker/", "target": "/d"},
        {"type": "bind", "source": "/var/lib", "target": "/d"},
        {"type": "bind", "source": "/proc/1/root", "target": "/h"},
        {"type": "bind", "source": "/etc/../etc/shadow", "target": "/h"},
        {"type": "bind", "source": "/", "target": "/host"},
        {"type": "bind", "source": "/root", "target": "/r"},
        {"type": "bind", "source": "/boot", "target": "/b"},
        {"type": "bind", "source": "/dev", "target": "/dev"},
        {"type": "bind", "source": "/sys", "target": "/sys"},
    ],
)
def test_sensitive_host_paths_stop_on_existing_and_new_services(mount: dict[str, Any]) -> None:
    findings = helpers.check_override(_stack_with_volumes(), _stack_after("explore", {"volumes": [mount]}))
    assert [finding.level for finding in findings] == ["stop"]
    assert "sensitive host path" in findings[0].message
    assert "explore" in findings[0].message
    added = helpers.check_override(_stack_with_volumes(), _stack_after("backup", {"image": PINNED, "volumes": [mount]}))
    assert [finding.level for finding in added] == ["stop"]
    assert "backup service" in added[0].message
    assert "OVERRIDE_ALLOW_SERVICES" in added[0].hint


def test_short_volume_syntax_is_judged_like_the_long_form() -> None:
    findings = helpers.check_override(
        _stack_with_volumes(), _stack_after("backup", {"image": PINNED, "volumes": ["/var/run/docker.sock:/s:ro", "postgres_data:/pg"]})
    )
    assert len(findings) == 2
    assert all(finding.level == "stop" for finding in findings)


def test_another_services_data_volume_stops_but_the_services_own_may_move() -> None:
    stolen = {"volumes": [{"type": "volume", "source": "postgres_data", "target": "/pg"}]}
    findings = helpers.check_override(_stack_with_volumes(), _stack_after("explore", stolen))
    assert [finding.level for finding in findings] == ["stop"]
    assert "stack volume postgres_data (used by postgres) into explore" in findings[0].message
    unused = {"volumes": [{"type": "volume", "source": "unused_data", "target": "/u"}]}
    assert helpers.check_override(_stack_with_volumes(), _stack_after("explore", unused))[0].level == "stop"
    bind = {"volumes": [{"type": "bind", "source": "/mnt/tank/pg", "target": "/var/lib/postgresql/data"}]}
    assert [finding.level for finding in helpers.check_override(_stack_with_volumes(), _stack_after("postgres", bind))] == ["ok"]


def test_relocating_a_services_own_volume_with_driver_opts_is_allowed_unless_the_path_is_sensitive() -> None:
    def relocate(device: str) -> dict[str, Any]:
        return _stack_after("postgres", {}, {"postgres_data": {"driver": "local", "driver_opts": {"type": "none", "o": "bind", "device": device}}})

    assert [finding.level for finding in helpers.check_override(_stack_with_volumes(), relocate("/mnt/tank/groovemap/postgres"))] == ["ok"]
    findings = helpers.check_override(_stack_with_volumes(), relocate("/var/run/docker.sock"))
    assert [finding.level for finding in findings] == ["stop"]
    assert "backs the volume postgres_data of postgres" in findings[0].message


def test_a_new_service_may_not_reach_a_stack_volume_through_a_relocated_definition() -> None:
    relocated = {"postgres_data": {"driver_opts": {"type": "none", "o": "bind", "device": "/mnt/tank/pg"}}}
    mount = {"volumes": [{"type": "volume", "source": "postgres_data", "target": "/pg"}]}
    findings = helpers.check_override(_stack_with_volumes(), _stack_after("backup", {"image": PINNED, **mount}, relocated))
    assert [finding.level for finding in findings] == ["stop"]
    assert "stack volume postgres_data" in findings[0].message


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("privileged", True),
        ("cap_add", ["NET_ADMIN"]),
        ("devices", ["/dev/fuse"]),
        ("security_opt", ["apparmor:unconfined"]),
        ("pid", "host"),
        ("volumes_from", ["postgres"]),
        ("cpu_shares", 512),
        ("deploy", {"replicas": 2}),
    ],
)
def test_a_new_service_may_set_only_allowlisted_keys(key: str, value: Any) -> None:
    findings = helpers.check_override(_stack_with_volumes(), _stack_after("backup", {"image": PINNED, key: value}))
    assert [finding.level for finding in findings] == ["stop"]
    assert "backup service" in findings[0].message
    assert key in findings[0].message
    assert "OVERRIDE_ALLOW_SERVICES" in findings[0].hint


def test_a_new_service_may_use_the_allowlisted_keys_and_new_or_safe_volumes() -> None:
    backup = {
        "image": PINNED,
        "command": ["sh", "-c", "backup"],
        "environment": {"A": "b"},
        "restart": "unless-stopped",
        "user": "1000",
        "read_only": True,
        "deploy": {"resources": {"limits": {"memory": "256M"}}},
        "volumes": [
            {"type": "volume", "source": "backup_scratch", "target": "/scratch"},
            {"type": "bind", "source": "/mnt/tank/backups", "target": "/backups"},
            {"type": "tmpfs", "target": "/scratch-ram"},
        ],
        "privileged": False,
        "ports": [],
    }
    after = _stack_after("backup", backup, {"backup_scratch": {}})
    assert [finding.level for finding in helpers.check_override(_stack_with_volumes(), after)] == ["ok"]


def test_opt_in_lets_a_backup_sidecar_mount_stack_data_but_not_skip_the_digest() -> None:
    sidecar = {
        "image": PINNED,
        "privileged": False,
        "cap_add": ["DAC_READ_SEARCH"],
        "volumes": [
            {"type": "volume", "source": "postgres_data", "target": "/pg", "read_only": True},
            {"type": "bind", "source": "/var/run/docker.sock", "target": "/var/run/docker.sock"},
        ],
    }
    before = _stack_with_volumes()
    assert [finding.level for finding in helpers.check_override(before, _stack_after("backup", sidecar))] != ["ok"]
    assert [finding.level for finding in helpers.check_override(before, _stack_after("backup", sidecar), frozenset({"backup"}))] == ["ok"]
    unpinned = _stack_after("backup", {**sidecar, "image": "backup:latest", "build": {"context": "."}})
    findings = helpers.check_override(before, unpinned, frozenset({"backup"}))
    assert [finding.level for finding in findings] == ["stop", "stop"]


def test_override_must_not_drop_a_service() -> None:
    after = _render()
    del after["services"]["explore"]
    assert helpers.check_override(_render(), after)[0].message == "docker-compose.override.yml removes the explore service"


def test_override_check_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    before, after = tmp_path / "before.json", tmp_path / "after.json"
    before.write_text(json.dumps(_render()))
    after.write_text(json.dumps(_render(explore={"labels": {"a": "b"}})))
    assert helpers.main(["override-check", str(before), str(after)]) == 0
    after.write_text(json.dumps(_render(explore={"image": "explore:latest"})))
    assert helpers.main(["override-check", str(before), str(after)]) == 3
    assert "Stopping before the stack starts" in capsys.readouterr().out


def test_port_conflicts_skip_the_stacks_own_ports() -> None:
    ports = [helpers.Port("GRAFANA_HOST_PORT", "Grafana", "", 3000), helpers.Port("API_HOST_PORT", "Catalog API", "", 8004)]
    findings = helpers.check_ports(ports, {3000}, lambda _address, _port: True)
    assert [finding.level for finding in findings] == ["stop"]
    assert "8004" in findings[0].message
    assert "API_HOST_PORT" in findings[0].hint
    assert not findings[0].forcible
    assert helpers.check_ports(ports, set(), lambda _address, _port: False)[0].level == "ok"
    assert helpers.check_ports([helpers.Port("X_HOST_PORT", "X", "", -1)], set(), lambda _address, _port: False)[0].level == "stop"


def test_port_probe_sees_a_listener_and_a_free_port() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        assert helpers.port_in_use("", port)
        assert helpers.port_in_use("127.0.0.1", port)
    assert not helpers.port_in_use("127.0.0.1", _free_port())


def test_published_ports_are_read_from_docker_ps() -> None:
    assert helpers.published_ports("groovemap-postgres\t0.0.0.0:5433->5432/tcp, [::]:5433->5432/tcp, 127.0.0.1:15692->15692/tcp") == {5433, 15692}


def test_amd64_emulation_detection(tmp_path: Path) -> None:
    assert helpers.amd64_emulation_registered(tmp_path / "absent") is None
    assert helpers.amd64_emulation_registered(tmp_path) is False
    (tmp_path / "qemu-x86_64").write_text("enabled")
    assert helpers.amd64_emulation_registered(tmp_path) is True


# --- preflight end to end, with Docker faked -------------------------------------------


def docker_info(**overrides: Any) -> dict[str, Any]:
    info = {
        "ServerVersion": "29.5.2",
        "OperatingSystem": "Ubuntu 26.04 LTS",
        "Architecture": "x86_64",
        "MemTotal": 64 * GIB,
        "DockerRootDir": "/nonexistent/docker",
    }
    return info | overrides


class FakeRunner:
    """Answers `docker ...` argv the way the CLI does; records every call."""

    def __init__(self, info: dict[str, Any] | None = None, compose: str | None = "5.6.1", containers: str = "", running: str = "") -> None:
        self.info = info if info is not None else docker_info()
        self.compose = compose
        self.containers = containers
        self.running = running
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        args = argv[1:]
        if args[:1] == ["info"]:
            if not self.info:
                return subprocess.CompletedProcess(argv, 1, '{"ServerErrors":["down"]}', "Cannot connect to the Docker daemon\n")
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.info), "")
        if args[:2] == ["compose", "version"]:
            return subprocess.CompletedProcess(argv, 0 if self.compose else 1, self.compose or "", "")
        if args[:1] == ["ps"]:
            return subprocess.CompletedProcess(argv, 0, self.containers if "-a" in args else self.running, "")
        return subprocess.CompletedProcess(argv, 0, "", "")


def run_preflight(runner: FakeRunner, mode: str = "prod", **kwargs: Any) -> int:
    options: dict[str, Any] = {
        "assume_yes": False,
        "force": False,
        "environ": {},
        "env_values": {},
        "in_use": lambda _address, _port: False,
        "binfmt": lambda: False,
    } | kwargs
    result: int = helpers.preflight(mode, docker=helpers.Docker(runner), **options)
    return result


def test_preflight_passes_a_ready_host(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_preflight(FakeRunner()) == 0
    out = capsys.readouterr().out
    assert "[ok  ] Docker Engine 29.5.2" in out
    assert "[ok  ] Docker Compose 5.6.1" in out


@pytest.mark.parametrize("runner", [FakeRunner(info={}), FakeRunner(compose=None)])
def test_preflight_stops_without_working_docker(runner: FakeRunner, capsys: pytest.CaptureFixture[str]) -> None:
    assert run_preflight(runner, force=True, assume_yes=True) == 3
    assert "[stop]" in capsys.readouterr().out


def test_missing_docker_binary_is_reported() -> None:
    def missing(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(argv[0])

    assert helpers.Docker(missing).info() == (None, "the docker command is not installed or not on PATH")
    assert helpers.Docker(missing).compose_version() is None
    assert helpers.Docker(missing).lines("ps") == []


def test_low_memory_stops_unless_forced(capsys: pytest.CaptureFixture[str]) -> None:
    low = FakeRunner(docker_info(MemTotal=8 * GIB))
    assert run_preflight(low) == 3
    assert "--force" in capsys.readouterr().out
    assert run_preflight(low, force=True) == 0
    assert "going ahead below the minimums" in capsys.readouterr().out


def test_force_never_overrides_a_port_conflict(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_preflight(FakeRunner(), force=True, in_use=lambda _address, port: port == 3000) == 3
    out = capsys.readouterr().out
    assert "Port 3000 (Grafana) is already in use" in out
    assert "--force to go ahead" not in out


def test_rerun_ignores_ports_the_stack_already_holds() -> None:
    runner = FakeRunner(running="groovemap-grafana\t0.0.0.0:3000->3000/tcp")
    assert run_preflight(runner, in_use=lambda _address, port: port == 3000) == 0


def test_remote_engine_skips_the_local_port_probe(capsys: pytest.CaptureFixture[str]) -> None:
    def explode(_address: str, _port: int) -> bool:
        raise AssertionError("probed a local port for a remote engine")

    assert run_preflight(FakeRunner(), environ={"DOCKER_HOST": "ssh://lab"}, in_use=explode) == 0
    assert "remote" in capsys.readouterr().out


def test_arm64_asks_or_needs_yes(capsys: pytest.CaptureFixture[str]) -> None:
    arm = FakeRunner(docker_info(Architecture="aarch64"))
    assert run_preflight(arm) == 3
    assert "--yes" in capsys.readouterr().out
    assert run_preflight(arm, assume_yes=True) == 0
    assert run_preflight(arm, interactive=True, ask=lambda _prompt: "y") == 0
    assert run_preflight(arm, interactive=True, ask=lambda _prompt: "") == 3


# --- endpoints and the command line ----------------------------------------------------


def test_endpoints_use_configured_ports_and_addresses() -> None:
    rows = helpers.endpoints("prod", {"DASHBOARD_HOST_PORT": "9003", "WEB_BIND_ADDRESS": "192.168.1.5"}, {}, "lab")
    urls = {label: url for label, url, _ in rows}
    assert urls["Operations console"] == "http://192.168.1.5:9003"
    assert urls["Neo4j Browser"] == "http://lab:7474"
    assert "secrets/grafana_admin_password.txt" in {label: note for label, _, note in rows}["Grafana"]
    dev = helpers.render_endpoints(helpers.endpoints("dev", {"DATA_BIND_ADDRESS": "::1"}, {}, "lab"))
    assert "http://[::1]:15672" in dev
    assert "password groovemap" in dev


@pytest.fixture
def scratch_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    env = tmp_path / ".env"
    monkeypatch.setattr(helpers, "ENV_FILE", env)
    monkeypatch.setattr(helpers, "TEMPLATE", ROOT / ".env.example")
    return env


def test_main_profile_commands(scratch_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    scratch_env.write_text("# COMPOSE_PROFILES=musicbrainz\nCOMPOSE_PROJECT_NAME=lab\n")
    assert helpers.main(["profile-enabled"], environ={}) == 1
    assert helpers.main(["profile-enabled"], environ={"COMPOSE_PROFILES": "musicbrainz"}) == 0
    assert helpers.main(["enable-profile"], environ={}) == 0
    assert helpers.parse_env(scratch_env.read_text())["COMPOSE_PROFILES"] == "musicbrainz"
    assert helpers.main(["profile-enabled"], environ={}) == 0
    assert helpers.main(["project-name"], environ={}) == 0
    assert helpers.main(["project-name", "--project-name", "Other"], environ={}) == 0
    assert capsys.readouterr().out.splitlines()[-2:] == ["lab", "other"]


def test_main_configure_and_endpoints(scratch_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    scratch_env.write_text("NEO4J_HEAP_SIZE=2G\n")
    assert helpers.main(["configure", "--mode", "dev"], docker=helpers.Docker(FakeRunner()), environ={}) == 0
    assert scratch_env.read_text() == "NEO4J_HEAP_SIZE=2G\n"
    assert helpers.main(["configure"], docker=helpers.Docker(FakeRunner(info={})), environ={}) == 1
    assert helpers.main(["configure"], docker=helpers.Docker(FakeRunner()), environ={}) == 0
    assert helpers.parse_env(scratch_env.read_text())["NEO4J_MEMORY_LIMIT"] == "48G"
    assert helpers.main(["endpoints", "--mode", "prod"], environ={}) == 0
    assert "Operations console" in capsys.readouterr().out
    assert helpers.main(["preflight", "--yes"], docker=helpers.Docker(FakeRunner(info={})), environ={}) == 3


# --- scripts/deploy.sh, with docker and the gate faked ---------------------------------


FAKE_DOCKER = r"""#!/usr/bin/env bash
printf 'docker %s\n' "$*" >>"$DEPLOY_LOG"
empty='{"services": {}}'
case "$*" in
  "info --format {{json .}}") printf '%s\n' "$FAKE_DOCKER_INFO" ;;
  "compose version --short") echo 5.6.1 ;;
  *"name=^groovemap-extractor-musicbrainz\$"*) printf '%s' "${FAKE_MUSICBRAINZ_CONTAINERS:-}" ;;
  *"name=^groovemap-postgres\$"*) printf '%s' "${FAKE_RUNNING:-}" ;;
  "ps -a --filter name=^groovemap- "*) printf '%s' "${FAKE_CONTAINERS:-}" ;;
  "volume ls "*) printf '%s' "${FAKE_VOLUMES:-}" ;;
  *"catalog-admin-setup --list") printf '%s' "${FAKE_ADMINS:-}" ;;
  *"-f docker-compose.override.yml --profile * config --format json") printf '%s' "${FAKE_RENDER_OVERRIDE:-$empty}" ;;
  *"--profile * config --format json") printf '%s' "${FAKE_RENDER:-$empty}" ;;
esac
exit 0
"""

# Stands in for scripts/discogs_import_status.py: one exit code for the quick probe, one for --wait.
FAKE_GATE = r"""import os, sys
argv = sys.argv[1:]
with open(os.environ["DEPLOY_LOG"], "a") as log:
    log.write("gate " + " ".join(argv) + "\n")
sys.exit(int(os.environ.get("FAKE_GATE_WAIT" if "--wait" in argv else "FAKE_GATE", "2")))
"""

COPIED = (
    "scripts/deploy.sh",
    "scripts/deploy_helpers.py",
    "scripts/env-init.py",
    "scripts/check-images.py",
    "scripts/create-secrets.sh",
    ".env.example",
)
PROD = "-f docker-compose.yml -f docker-compose.prod.yml"


_CANDIDATES = iter(range(20000, 30000))


def _free_port() -> int:
    """A free port below every OS's ephemeral range, so no outgoing connection takes it meanwhile."""
    return next(port for port in _CANDIDATES if not helpers.port_in_use("", port))


class Deploy:
    """A scratch repository with a fake docker and gate; `run` executes scripts/deploy.sh in it."""

    def __init__(self, root: Path) -> None:
        self.root = root
        for relative in COPIED:
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(ROOT / relative, root / relative)
        (root / "scripts" / "discogs_import_status.py").write_text(FAKE_GATE)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        (bin_dir / "docker").write_text(FAKE_DOCKER)
        (bin_dir / "docker").chmod(0o755)
        self.log = root / "deploy.log"
        self.env = {key: value for key, value in os.environ.items() if not key.startswith(("COMPOSE_", "DOCKER_"))}
        # Every configured host port points at a port free right now, so the real probe passes.
        self.env |= {variable: str(_free_port()) for variable, *_ in helpers.PORTS}
        self.env |= {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "DEPLOY_LOG": str(self.log),
            "DEPLOY_PYTHON": sys.executable,
            "FAKE_DOCKER_INFO": json.dumps(docker_info()),
        }

    def run(self, *args: str, **fake: str) -> subprocess.CompletedProcess[str]:
        self.log.write_text("")
        return subprocess.run(
            [_BASH, str(self.root / "scripts" / "deploy.sh"), *args],
            capture_output=True,
            text=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
            env=self.env | fake,
        )

    @property
    def calls(self) -> list[str]:
        return [line for line in self.log.read_text().splitlines() if line.startswith("gate ") or " compose " in f" {line}"]

    @property
    def ups(self) -> list[str]:
        return [line for line in self.calls if line.endswith(" up -d --wait")]

    @property
    def env_values(self) -> dict[str, str]:
        result: dict[str, str] = helpers.read_env(self.root / ".env")
        return result

    def write_env(self, text: str) -> None:
        (self.root / ".env").write_text(text)


@pytest.fixture
def deploy(tmp_path: Path) -> Iterator[Deploy]:
    yield Deploy(tmp_path)


def _compose_calls(calls: Sequence[str]) -> list[str]:
    return [call for call in calls if not call.startswith("docker compose version")]


def test_fresh_host_runs_both_phases_in_order(deploy: Deploy) -> None:
    result = deploy.run(FAKE_GATE_WAIT="0")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _compose_calls(deploy.calls) == [
        f"docker compose {PROD} --profile * config --format json",  # the disk check's render
        f"docker compose {PROD} up -d --wait",
        "gate --prod --wait --interval 300",
        f"docker compose {PROD} up -d --wait",
        f"docker compose {PROD} exec -T api catalog-admin-setup --list",
    ]
    values = deploy.env_values
    assert values["CATALOG_API_IMAGE"].startswith("ghcr.io/groovemap-music/catalog-api@sha256:")
    assert tuple(values[key] for key in helpers.SIZING_KEYS) == ("8g", "16g", "48G", "5g")
    assert values["APP_BASE_URL"].startswith("http://")
    assert values["COMPOSE_PROFILES"] == "musicbrainz"
    assert (deploy.root / "secrets" / "neo4j_password.txt").exists()
    out = result.stdout
    assert out.index("Phase 1 of 2") < out.index("about two days") < out.index("Phase 2 of 2") < out.index("Your GrooveMap")
    assert "catalog-admin-setup --email" in out


def test_the_profile_is_off_while_phase_one_runs(deploy: Deploy) -> None:
    result = deploy.run("--no-wait")
    assert result.returncode == 0, result.stderr
    assert deploy.ups == [f"docker compose {PROD} up -d --wait"]
    assert not any(call.startswith("gate") for call in deploy.calls)
    assert "COMPOSE_PROFILES" not in deploy.env_values
    assert "bash scripts/deploy.sh\n" in result.stdout
    assert f"{sys.executable} scripts/discogs_import_status.py --prod" in result.stdout
    assert "just" not in result.stdout


def test_hints_print_the_just_form_when_started_through_just(deploy: Deploy) -> None:
    result = deploy.run("--no-wait", DEPLOY_INVOKED_AS="just deploy")
    assert result.returncode == 0, result.stderr
    assert "just deploy\n" in result.stdout
    assert "just discogs-import-status --prod" in result.stdout
    assert "bash scripts/deploy.sh" not in result.stdout


def test_help_and_usage_errors_name_the_form_the_user_typed(deploy: Deploy) -> None:
    assert "Usage: bash scripts/deploy.sh [options]" in deploy.run("--help").stdout
    assert "Usage: just deploy [options]" in deploy.run("--help", DEPLOY_INVOKED_AS="just deploy").stdout
    failed = deploy.run("--bogus")
    assert failed.returncode == 2
    assert "Run 'bash scripts/deploy.sh --help'" in failed.stderr


def test_a_python_older_than_3_10_stops_with_a_clear_message(deploy: Deploy) -> None:
    (deploy.root / "bin" / "python3").write_text("#!/bin/sh\nexit 1\n")
    (deploy.root / "bin" / "python3").chmod(0o755)
    result = deploy.run(DEPLOY_PYTHON="python3")
    assert result.returncode == 1
    assert "Python 3.10 or newer is needed" in result.stderr
    assert not any(call.startswith("gate") or "compose" in call for call in deploy.calls)


def test_rerun_during_the_import_resumes_the_wait(deploy: Deploy) -> None:
    deploy.run("--no-wait")
    env_before = (deploy.root / ".env").read_text()
    result = deploy.run(FAKE_RUNNING="abc", FAKE_GATE="3", FAKE_GATE_WAIT="0")
    assert result.returncode == 0, result.stderr
    assert _compose_calls(deploy.calls)[1:5] == [
        "gate --prod",
        f"docker compose {PROD} up -d --wait",
        "gate --prod --wait --interval 300",
        f"docker compose {PROD} up -d --wait",
    ]
    assert ".env already exists; keeping it." in result.stdout
    assert helpers.read_env(deploy.root / ".env") == helpers.parse_env(env_before) | {"COMPOSE_PROFILES": "musicbrainz"}


def test_interrupting_the_wait_exits_130_and_says_how_to_resume(deploy: Deploy) -> None:
    result = deploy.run("--interval", "60", "--no-admin", FAKE_GATE_WAIT="130")
    assert result.returncode == 130
    assert "COMPOSE_PROFILES" not in deploy.env_values
    assert "bash scripts/deploy.sh --no-admin --interval 60" in result.stdout
    assert not any("catalog-admin-setup" in call for call in deploy.calls)


def test_a_failed_wait_is_an_error(deploy: Deploy) -> None:
    assert deploy.run(FAKE_GATE_WAIT="2").returncode == 1
    assert "COMPOSE_PROFILES" not in deploy.env_values


def test_an_enabled_profile_only_reconciles(deploy: Deploy) -> None:
    deploy.run("--no-wait")
    deploy.write_env((deploy.root / ".env").read_text() + "COMPOSE_PROFILES=musicbrainz\n")
    result = deploy.run(FAKE_GATE="3", FAKE_GATE_WAIT="3")
    assert result.returncode == 0, result.stderr
    assert deploy.ups == [f"docker compose {PROD} up -d --wait"]
    assert not any(call.startswith("gate") for call in deploy.calls)
    assert "only brings the stack up to date" in result.stdout


def test_profile_exported_in_the_shell_counts_as_enabled(deploy: Deploy) -> None:
    result = deploy.run(COMPOSE_PROFILES="musicbrainz", FAKE_GATE_WAIT="3")
    assert result.returncode == 0, result.stderr
    assert not any(call.startswith("gate") for call in deploy.calls)


@pytest.mark.parametrize(
    "fake",
    [
        {"FAKE_MUSICBRAINZ_CONTAINERS": "0123456789ab\n"},
        {"FAKE_VOLUMES": "postgres_data\nmusicbrainz_data\n"},
        {"FAKE_RUNNING": "abc", "FAKE_GATE": "0"},
    ],
    ids=["musicbrainz-containers", "musicbrainz-volume", "import-already-complete"],
)
def test_an_existing_musicbrainz_path_is_kept_running(deploy: Deploy, fake: dict[str, str]) -> None:
    result = deploy.run(FAKE_GATE_WAIT="3", **fake)
    assert result.returncode == 0, result.stderr
    assert deploy.env_values["COMPOSE_PROFILES"] == "musicbrainz"
    assert deploy.ups == [f"docker compose {PROD} up -d --wait"]
    assert not any("--wait --interval" in call for call in deploy.calls)
    assert not any(" stop" in call or " down" in call for call in deploy.calls)


def test_dev_uses_the_base_stack_without_secrets_or_sizing(deploy: Deploy) -> None:
    result = deploy.run("--dev", FAKE_GATE_WAIT="0")
    assert result.returncode == 0, result.stderr
    assert deploy.ups == ["docker compose -f docker-compose.yml up -d --wait"] * 2
    assert "gate --wait --interval 300" in deploy.calls
    assert not (deploy.root / "secrets").exists()
    assert deploy.env_values["NEO4J_HEAP_SIZE"] == "2G"
    assert not {"NEO4J_MEMORY_LIMIT", "NEO4J_TRANSACTION_MEMORY_MAX"} & deploy.env_values.keys()
    assert "user admin, password groovemap" in result.stdout


def test_project_name_reaches_compose_the_gate_and_the_volume_lookup(deploy: Deploy) -> None:
    result = deploy.run("-p", "lab", "--no-admin", FAKE_GATE_WAIT="0")
    assert result.returncode == 0, result.stderr
    assert deploy.ups[0] == f"docker compose --project-name lab {PROD} up -d --wait"
    assert "gate --project-name lab --prod --wait --interval 300" in deploy.calls
    assert "label=com.docker.compose.project=lab" in deploy.log.read_text()


def test_no_admin_skips_the_admin_offer(deploy: Deploy) -> None:
    result = deploy.run("--no-admin", FAKE_GATE_WAIT="0")
    assert result.returncode == 0, result.stderr
    assert not any("catalog-admin-setup" in call for call in deploy.calls)
    assert "catalog-admin-setup" not in result.stdout


def test_admin_email_creates_the_admin_interactively(deploy: Deploy) -> None:
    result = deploy.run("--admin-email", "me@example.com", FAKE_GATE_WAIT="0")
    assert result.returncode == 0, result.stderr
    assert deploy.calls[-1] == f"docker compose {PROD} exec api catalog-admin-setup --email me@example.com"


def test_an_existing_admin_is_not_offered_again(deploy: Deploy) -> None:
    result = deploy.run(FAKE_GATE_WAIT="0", FAKE_ADMINS="admin@example.com\n")
    assert result.returncode == 0, result.stderr
    assert "An admin account already exists" in result.stdout


def test_arm64_stops_before_changing_anything_unless_confirmed(deploy: Deploy) -> None:
    arm = json.dumps(docker_info(Architecture="aarch64"))
    result = deploy.run(FAKE_DOCKER_INFO=arm)
    assert result.returncode == 3
    assert "linux/amd64 only" in result.stdout
    assert not (deploy.root / ".env").exists()
    assert deploy.ups == []
    assert deploy.run("--yes", "--no-wait", FAKE_DOCKER_INFO=arm).returncode == 0
    assert deploy.ups


def test_deploy_stops_on_low_memory_unless_forced(deploy: Deploy) -> None:
    small = json.dumps(docker_info(MemTotal=8 * GIB))
    result = deploy.run(FAKE_DOCKER_INFO=small)
    assert result.returncode == 3
    assert "needs at least 16 GB" in result.stdout
    assert not (deploy.root / ".env").exists()
    assert deploy.run("--force", "--no-wait", FAKE_DOCKER_INFO=small).returncode == 0
    assert tuple(deploy.env_values[key] for key in helpers.SIZING_KEYS) == ("1g", "3g", "5G", "1g")


@pytest.mark.parametrize("args", [["--bogus"], ["--interval", "0"], ["--interval", "soon"], ["--admin-email"], ["-p", ""]])
def test_bad_arguments_exit_2_before_doing_anything(deploy: Deploy, args: list[str]) -> None:
    result = deploy.run(*args)
    assert result.returncode == 2
    assert deploy.log.read_text() == ""


def test_help_lists_every_option(deploy: Deploy) -> None:
    result = deploy.run("--help")
    assert result.returncode == 0
    for option in ("--dev", "--no-wait", "--no-admin", "--admin-email", "--project-name", "--interval", "--yes", "--force"):
        assert option in result.stdout
    assert deploy.log.read_text() == ""


OVERRIDE = "-f docker-compose.override.yml"


def test_an_override_file_reaches_every_compose_call_and_the_gate(deploy: Deploy) -> None:
    (deploy.root / "docker-compose.override.yml").write_text("---\nservices: {}\n")
    result = deploy.run(FAKE_GATE_WAIT="0")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _compose_calls(deploy.calls) == [
        f"docker compose {PROD} --profile * config --format json",
        f"docker compose {PROD} {OVERRIDE} --profile * config --format json",
        f"docker compose {PROD} {OVERRIDE} --profile * config --format json",  # the disk check's render
        f"docker compose {PROD} {OVERRIDE} up -d --wait",
        f"gate --prod {OVERRIDE} --wait --interval 300",
        f"docker compose {PROD} {OVERRIDE} up -d --wait",
        f"docker compose {PROD} {OVERRIDE} exec -T api catalog-admin-setup --list",
    ]
    assert "changes only host-specific settings" in result.stdout


def test_no_wait_with_an_override_names_it_in_the_status_command(deploy: Deploy) -> None:
    (deploy.root / "docker-compose.override.yml").write_text("---\nservices: {}\n")
    result = deploy.run("--no-wait", "-p", "lab", "--dev", DEPLOY_INVOKED_AS="just deploy")
    assert result.returncode == 0, result.stderr
    assert "just discogs-import-status -p lab -f docker-compose.override.yml\n" in result.stdout
    assert deploy.ups == [f"docker compose --project-name lab -f docker-compose.yml {OVERRIDE} up -d --wait"]


def test_an_override_that_bypasses_the_rules_stops_before_anything_starts(deploy: Deploy) -> None:
    (deploy.root / "docker-compose.override.yml").write_text("---\nservices: {}\n")
    before = json.dumps(_render())
    after = json.dumps(_render(**{"extractor-musicbrainz": {"profiles": []}}))
    result = deploy.run(FAKE_GATE_WAIT="0", FAKE_RENDER=before, FAKE_RENDER_OVERRIDE=after)
    assert result.returncode == 3
    assert "changes profiles on extractor-musicbrainz" in result.stdout
    assert deploy.ups == []
    assert not any(call.startswith("gate") for call in deploy.calls)


def test_an_unreadable_override_stops_before_anything_starts(deploy: Deploy) -> None:
    (deploy.root / "docker-compose.override.yml").write_text("---\nservices: {}\n")
    result = deploy.run(FAKE_GATE_WAIT="0", FAKE_RENDER_OVERRIDE="not json")
    assert result.returncode == 1
    assert deploy.ups == []


def test_recipe_runs_the_script_and_check_never_does() -> None:
    justfile = (ROOT / "Justfile").read_text()
    assert 'deploy *args:\n    DEPLOY_INVOKED_AS="just deploy" bash scripts/deploy.sh "$@"' in justfile
    check_line = next(line for line in justfile.splitlines() if line.startswith("check:"))
    assert "deploy" not in check_line
    for workflow in (ROOT / ".github/workflows").glob("*.yml"):
        assert "deploy.sh" not in workflow.read_text()
        assert "just deploy" not in workflow.read_text()
