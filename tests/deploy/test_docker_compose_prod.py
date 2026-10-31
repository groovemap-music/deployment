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

import re
from pathlib import Path
from typing import Any

import yaml


def default_port(mapping: object) -> str:
    """Resolve a port mapping as Compose does with every variable unset."""
    text = re.sub(r"\$\{[A-Z0-9_]+:\+\$\{[A-Z0-9_]+\}:\}", "", str(mapping))
    return re.sub(r"\$\{[A-Z0-9_]+:-([^{}]*)\}", r"\1", text)


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
        assert [default_port(p) for p in ports] == ["6379:6379"]


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
        assert [default_port(p) for p in ports] == ["127.0.0.1:6379:6379"]

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


class TestConsoleObservabilityWiring:
    """The redesigned console's history and link URLs, and its optional Grafana token."""

    def test_dashboard_reads_history_and_links_from_the_base_file(self) -> None:
        env = _base_compose()["services"]["dashboard"]["environment"]
        assert env["VICTORIA_METRICS_URL"] == "http://victoria-metrics:8428"
        assert "GRAFANA_URL" in env
        assert env["GRAFANA_API_URL"] == "http://grafana:3000"
        assert "TRACES_URL" in env

    def test_dashboard_token_is_a_file_secret_never_an_env_value(self) -> None:
        dashboard = _prod_compose()["services"]["dashboard"]
        assert dashboard["environment"]["GRAFANA_TOKEN_FILE"] == "/run/secrets/grafana_viewer_token"
        assert "grafana_viewer_token" in dashboard["secrets"]
        assert not any("TOKEN" in name and not name.endswith("_FILE") for name in dashboard["environment"])

    def test_base_file_leaves_the_token_unset(self) -> None:
        assert "GRAFANA_TOKEN_FILE" not in _base_compose()["services"]["dashboard"]["environment"]


class TestConsoleSessionCookie:
    """The console's Secure session cookie: opted out in dev, never in production."""

    # The values the console accepts; anything else stops it starting.
    ACCEPTED = frozenset({"true", "1", "yes", "false", "0", "no"})

    def test_dev_stack_opts_out_so_plain_http_sign_in_works(self) -> None:
        env = _base_compose()["services"]["dashboard"]["environment"]
        assert str(env["SESSION_COOKIE_SECURE"]).lower() == "false"
        assert str(env["SESSION_COOKIE_SECURE"]).lower() in self.ACCEPTED

    def test_prod_overlay_resets_the_dev_opt_out_so_the_secure_default_applies(self) -> None:
        """Compose merges environment key by key, so the base opt-out would carry into prod.

        The overlay must `!reset` it (the test loader reads the tagged ``null`` as the string
        ``"null"``); a value of its own, true or false, is not the console's default.
        """
        env = _prod_compose()["services"]["dashboard"]["environment"]
        assert "SESSION_COOKIE_SECURE" in env
        assert env["SESSION_COOKIE_SECURE"] in (None, "null")
        assert "SESSION_COOKIE_SECURE: !reset null" in (REPO_ROOT / "docker-compose.prod.yml").read_text()

    def test_no_other_prod_service_sets_the_cookie_setting(self) -> None:
        for name, service in _prod_compose()["services"].items():
            if name == "dashboard":
                continue
            assert "SESSION_COOKIE_SECURE" not in (service.get("environment") or {}), name


def test_valkey_exporter_reads_a_json_password_map_not_the_plain_secret() -> None:
    """redis_exporter's --redis.password-file is a JSON map of URI to password.

    Pointing it at the plain valkey_password secret made it crash-loop. It must
    read the derived map, and the password itself must not enter its environment.
    """
    exporter = _prod_compose()["services"]["valkey-exporter"]
    secret_file = _prod_compose()["secrets"]["valkey_exporter_passwords"]["file"]

    assert exporter["environment"] == {"REDIS_PASSWORD_FILE": "/run/secrets/valkey_exporter_passwords"}
    assert exporter["secrets"] == ["valkey_exporter_passwords"]
    assert secret_file == "./secrets/valkey_exporter_passwords.json"


def _effective_restart_settings() -> dict[str, tuple[str | None, dict[str, Any] | None]]:
    """Per service: the ``restart:`` value and ``deploy.restart_policy`` after the overlay is applied."""
    base = _base_compose()["services"]
    prod = _prod_compose()["services"]
    effective: dict[str, tuple[str | None, dict[str, Any] | None]] = {}
    for name, spec in base.items():
        overlay = prod.get(name) or {}
        restart = overlay.get("restart", spec.get("restart"))
        policy = ((overlay.get("deploy") or {}).get("restart_policy")) or ((spec.get("deploy") or {}).get("restart_policy"))
        effective[name] = (restart, policy)
    return effective


def test_prod_restart_uses_one_mechanism_per_service() -> None:
    """The prod overlay restarts services through ``restart:`` alone.

    Docker Compose lets ``deploy.restart_policy`` override ``restart:``, and the
    engine only takes a retry cap (``max_attempts``) with ``on-failure``. The old
    overlay set ``restart: always`` and ``deploy.restart_policy`` with
    ``condition: any`` and ``max_attempts: 3``, which the engine rejects at
    ``docker compose up`` ("maximum retry count can only be used with
    'on-failure'", Compose 5.6.1). Setting both mechanisms is also ambiguous, so
    neither combination may return.
    """
    for name, (restart, policy) in _effective_restart_settings().items():
        if policy is None:
            continue
        assert restart is None, f"{name}: sets both restart: {restart!r} and deploy.restart_policy; use one mechanism"
        if "max_attempts" in policy:
            assert policy.get("condition") == "on-failure", f"{name}: max_attempts is only valid with condition: on-failure"


def test_prod_long_running_services_restart_always() -> None:
    """Restart-on-crash is not weakened: every service the overlay manages restarts ``always``.

    Services the overlay leaves alone (one-shot ``schema-init`` and
    ``rabbitmq-dlq-policy-init``, the ``embeddings`` job) keep their base value.
    """
    prod = _prod_compose()["services"]
    effective = _effective_restart_settings()
    one_shots = {"schema-init", "rabbitmq-dlq-policy-init", "embeddings"}

    for name in prod:
        if name in one_shots:
            continue
        assert effective[name][0] == "always", f"{name}: prod restart is {effective[name][0]!r}, expected 'always'"
    assert not (one_shots & {name for name in prod if (prod[name] or {}).get("restart")}), "one-shot jobs must not get a restart policy"
