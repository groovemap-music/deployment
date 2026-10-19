"""Deploy-config regression tests for production Valkey authentication.

Base docker-compose.yml runs valkey with no ``--requirepass`` and publishes
6379 to 0.0.0.0. Every OTHER stateful service (postgres, neo4j, rabbitmq) gets
a docker-compose.prod.yml hardening override that wires a Docker _FILE secret
credential — valkey got none, so a prod deploy left it unauthenticated and
reachable from the network with zero credentials (FLUSHALL, cache poisoning).

These tests parse the compose YAML directly (no ``docker`` binary required)
and assert the prod overlay closes that gap: a valkey_password secret exists,
the valkey service authenticates with it via a custom entrypoint, the port is
rebound to loopback only, and every service that talks to valkey
(api/dashboard/insights) gets VALKEY_PASSWORD_FILE wired in.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]


class _IgnoreUnknownTagsLoader(yaml.SafeLoader):
    """YAML loader that treats Compose-spec merge tags (``!override``, ``!reset``)
    as plain nodes, so we can parse docker-compose.prod.yml without teaching
    PyYAML the full Compose merge semantics — we only need the tagged node's
    own value for these assertions.
    """


def _construct_undefined(loader: yaml.SafeLoader, node: yaml.Node) -> Any:
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return None


# PyYAML supports a None tag as the default/fallback constructor at runtime;
# the type stub only types the tag param as str, hence the ignore below.
_IgnoreUnknownTagsLoader.add_constructor(None, _construct_undefined)  # type: ignore[arg-type]


def _load_compose(path: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.load(path.read_text(), Loader=_IgnoreUnknownTagsLoader)  # noqa: S506 — trusted repo file, custom loader ignores unknown tags only
    return loaded


def _base_compose() -> dict[str, Any]:
    return _load_compose(REPO_ROOT / "docker-compose.yml")


def _prod_compose() -> dict[str, Any]:
    return _load_compose(REPO_ROOT / "docker-compose.prod.yml")


class TestBaseValkeyIsUnauthenticatedByDefault:
    """Document the vulnerable baseline so a regression in the prod fix is obvious."""

    def test_base_valkey_has_no_requirepass(self) -> None:
        valkey = _base_compose()["services"]["valkey"]
        assert "--requirepass" not in valkey["command"]

    def test_base_valkey_publishes_to_all_interfaces(self) -> None:
        valkey = _base_compose()["services"]["valkey"]
        ports = valkey["ports"]
        assert any(str(p) == "6379:6379" for p in ports)


class TestBaseValkeyService:
    def test_valkey_is_the_cache_service(self) -> None:
        services = _base_compose()["services"]
        assert "valkey" in services
        assert [name for name in services if name.endswith(("cache", "valkey"))] == ["valkey"]

    def test_valkey_identity_and_alias(self) -> None:
        valkey = _base_compose()["services"]["valkey"]
        assert valkey["container_name"] == "groovemap-valkey"
        assert valkey["hostname"] == "valkey"
        assert valkey["networks"]["groovemap"]["aliases"] == ["cache"]

    def test_valkey_image_is_digest_pinned(self) -> None:
        image = _base_compose()["services"]["valkey"]["image"]
        assert image.startswith("valkey/valkey:9.")
        assert "@sha256:" in image

    def test_valkey_runs_valkey_server_with_the_cache_flags(self) -> None:
        command = _base_compose()["services"]["valkey"]["command"]
        assert command.startswith("valkey-server ")
        assert "--appendonly yes" in command
        assert "--maxmemory 512mb" in command

    def test_valkey_never_evicts_security_state(self) -> None:
        # revoked:jti:* and password_changed:* live only in Valkey. Any evicting policy
        # (allkeys-*, volatile-*) can drop them under memory pressure and silently
        # re-validate revoked tokens, so writes must fail at maxmemory instead.
        command = _base_compose()["services"]["valkey"]["command"].split()
        assert command[command.index("--maxmemory-policy") + 1] == "noeviction"
        assert not any("lru" in part or "lfu" in part or "random" in part for part in command)

    def test_valkey_uses_valkey_data_as_the_only_cache_volume(self) -> None:
        compose = _base_compose()
        assert compose["services"]["valkey"]["volumes"] == ["valkey_data:/data"]
        assert "valkey_data" in compose["volumes"]
        mounted = [v for svc in compose["services"].values() for v in svc.get("volumes", []) if isinstance(v, str)]
        assert "valkey_data:/data" in mounted
        # valkey_data is the only cache volume: no other declared volume is cache-related.
        assert [name for name in compose["volumes"] if "cache" in name or "valkey" in name] == ["valkey_data"]

    def test_valkey_healthcheck_uses_valkey_cli(self) -> None:
        assert _base_compose()["services"]["valkey"]["healthcheck"]["test"] == ["CMD", "valkey-cli", "ping"]

    def test_consumers_get_valkey_env_only(self) -> None:
        services = _base_compose()["services"]
        for name in ("api", "insights"):
            environment = services[name]["environment"]
            assert environment["VALKEY_HOST"] == "valkey", name
            assert "valkey" in services[name]["depends_on"], name


class TestProdValkeySecret:
    def test_valkey_password_secret_declared(self) -> None:
        prod = _prod_compose()
        assert "valkey_password" in prod["secrets"]
        assert prod["secrets"]["valkey_password"]["file"] == "./secrets/valkey_password.txt"

    def test_secrets_example_placeholder_exists(self) -> None:
        assert (REPO_ROOT / "secrets.example" / "valkey_password.txt").exists()


class TestProdValkeyService:
    def test_valkey_service_uses_custom_entrypoint(self) -> None:
        valkey = _prod_compose()["services"]["valkey"]
        assert valkey["entrypoint"] == ["/scripts/valkey-entrypoint.sh"]

    def test_valkey_service_mounts_entrypoint_script(self) -> None:
        valkey = _prod_compose()["services"]["valkey"]
        assert any("valkey-entrypoint.sh" in str(v) for v in valkey["volumes"])

    def test_valkey_service_declares_password_secret(self) -> None:
        valkey = _prod_compose()["services"]["valkey"]
        assert "valkey_password" in valkey["secrets"]

    def test_valkey_port_rebound_to_loopback(self) -> None:
        valkey = _prod_compose()["services"]["valkey"]
        ports = valkey["ports"]
        assert ports == ["127.0.0.1:6379:6379"]
        assert not any(str(p) == "6379:6379" for p in ports)

    def test_valkey_healthcheck_authenticates(self) -> None:
        valkey = _prod_compose()["services"]["valkey"]
        test_cmd = " ".join(valkey["healthcheck"]["test"])
        assert "valkey_password" in test_cmd
        assert "-a" in test_cmd

    def test_entrypoint_script_exists_and_is_executable(self) -> None:
        script = REPO_ROOT / "scripts" / "valkey-entrypoint.sh"
        assert script.exists()
        assert script.stat().st_mode & 0o111, "valkey-entrypoint.sh must be executable"


class TestProdValkeyPasswordWiredIntoConsumers:
    """Every Compose Valkey consumer must receive the production password file."""

    def test_api_gets_valkey_password_file(self) -> None:
        api = _prod_compose()["services"]["api"]
        assert api["environment"]["VALKEY_PASSWORD_FILE"] == "/run/secrets/valkey_password"
        assert "valkey_password" in api["secrets"]

    def test_dashboard_has_no_cache_wiring(self) -> None:
        """operations-console no longer uses the cache, so it gets no credential for it."""
        dashboard = _prod_compose()["services"]["dashboard"]
        assert not any(name.startswith("VALKEY_") for name in dashboard["environment"])
        assert "valkey_password" not in dashboard["secrets"]
        base = _base_compose()["services"]["dashboard"]
        assert not any(name.startswith("VALKEY_") for name in base["environment"])
        assert "valkey" not in base["depends_on"]

    def test_insights_gets_valkey_password_file(self) -> None:
        insights = _prod_compose()["services"]["insights"]
        assert insights["environment"]["VALKEY_PASSWORD_FILE"] == "/run/secrets/valkey_password"
        assert "valkey_password" in insights["secrets"]
