"""Tests for the catalog-DLQ size-cap policy (gm-deployment-8mb.1).

The policy is declared AFTER the broker is healthy, via the one-shot
rabbitmq-dlq-policy-init service (scripts/rabbitmq-dlq-policy-init.sh, using
rabbitmqadmin over the management HTTP API) -- not at broker boot via
`load_definitions`, which was tried first and rejected in review: on a blank
node, a boot-time definitions import skips creating the default vhost/user
that RABBITMQ_DEFAULT_USER/PASS/VHOST would otherwise create ("Nuances of
Boot-time Definition Import", rabbitmq.com/docs/definitions), breaking every
service's auth on a fresh install.

Four layers:

- ``scripts/check-rabbitmq-dlq-policy.py`` is exercised directly, including against
  synthetic bad inputs, because a gate that cannot fail is not a gate (mirrors
  test_dashboards.py's rationale for scripts/check-dashboards.py).
- ``scripts/rabbitmq-dlq-policy-init.sh`` is run for real (with a stubbed
  ``rabbitmqadmin`` on PATH, mirroring test_redis_entrypoint_script.py's approach for
  scripts/redis-entrypoint.sh) to prove it builds the right policy definition from env
  vars and still forwards Docker secrets when present.
- docker-compose.yml is parsed to confirm the broker runs stock (no boot-time
  definitions import) and the init service is wired to wait for it, reuse its image,
  and declare the policy.

The real, disposable-stack proof that a blank broker still gets the default
`groovemap` user, the `/` vhost, AND the catalog-dlq-cap policy lives in
scripts/smoke-infra.sh -- this repository's only Docker/integration test tier. It is
operator-triggered (`just smoke-infra`), not part of `just check` or CI (see
docs/testing-guide.md's live-checks table and test_automation_policy.py's
test_live_network_and_stateful_recipes_stay_outside_check), because it starts real
containers; a pytest-level test here would either have to join that tier's
container-starting cost into every `just check` run or invent new opt-in machinery this
repository doesn't otherwise use, so it lives with its sibling infrastructure smoke
checks instead.
"""

from __future__ import annotations

import json
import os
import re
import runpy
import subprocess
import textwrap
from pathlib import Path
from typing import Any

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
INIT_SCRIPT = REPO_ROOT / "scripts" / "rabbitmq-dlq-policy-init.sh"
ADMIN_GUIDE = REPO_ROOT / "docs" / "admin-guide.md"


class _IgnoreUnknownTagsLoader(yaml.SafeLoader):
    """Mirrors test_docker_compose_prod.py: docker-compose.prod.yml uses Compose-spec
    merge tags (``!override``) that plain ``yaml.safe_load`` cannot parse."""


def _construct_undefined(loader: yaml.SafeLoader, node: yaml.Node) -> Any:
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return None


_IgnoreUnknownTagsLoader.add_constructor(None, _construct_undefined)  # type: ignore[arg-type]


# The 16 real DLQ names, computed the same way as the checker script, kept here
# independently so a bug in the checker's own naming table doesn't hide from these tests.
REAL_DLQ_NAMES = {
    f"groovemap-{source}-{consumer}-{entity}.dlq"
    for source, consumers, entities in (
        ("discogs", ("graphinator", "tableinator"), ("artists", "labels", "masters", "releases")),
        (
            "musicbrainz",
            ("brainzgraphinator", "brainztableinator"),
            ("artists", "labels", "release-groups", "releases"),
        ),
    )
    for consumer in consumers
    for entity in entities
}


def _run_checker() -> dict[str, Any]:
    """Execute the validation script in-process and return its module namespace."""
    return runpy.run_path(str(REPO_ROOT / "scripts" / "check-rabbitmq-dlq-policy.py"), run_name="check_rabbitmq_dlq_policy")


checker = _run_checker()


class TestRealDlqNamingMatchesTheCatalogServices:
    """Cross-check against the DLQ names read from the four sibling repositories."""

    def test_checker_computes_the_same_16_real_dlq_names(self) -> None:
        assert checker["real_dlq_names"]() == REAL_DLQ_NAMES
        assert len(REAL_DLQ_NAMES) == 16

    def test_live_queue_names_are_the_dlq_names_without_the_suffix(self) -> None:
        live = checker["live_queue_names"]()
        assert live == {name.removesuffix(".dlq") for name in REAL_DLQ_NAMES}
        assert live.isdisjoint(REAL_DLQ_NAMES)


class TestPolicyPatternMatchesRealDlqsAndNoLiveQueue:
    def test_default_pattern_passes(self) -> None:
        checker["check_pattern"]()

    def test_pattern_missing_a_real_source_prefix_fails(self) -> None:
        with pytest.raises(AssertionError, match="does not match real DLQ"):
            checker["check_pattern"](r"^groovemap-discogs-.*\.dlq$")

    def test_pattern_matching_live_queues_fails(self) -> None:
        with pytest.raises(AssertionError, match="matches non-DLQ name"):
            checker["check_pattern"](r"^groovemap-.*$")

    def test_pattern_does_not_match_a_dead_letter_exchange(self) -> None:
        compiled = re.compile(checker["POLICY_PATTERN"])
        for name in REAL_DLQ_NAMES:
            dlx = name.removesuffix(".dlq") + ".dlx"
            assert not compiled.fullmatch(dlx), f"pattern must not match the exchange {dlx}"


class TestInitScriptDeclaresOnlyThePolicy:
    def test_real_script_passes(self) -> None:
        checker["check_init_script"](INIT_SCRIPT.read_text())

    def test_a_script_that_also_declares_a_queue_fails(self) -> None:
        bad = INIT_SCRIPT.read_text() + "\nrabbitmqadmin declare queue --name=oops\n"
        with pytest.raises(AssertionError, match="must never run"):
            checker["check_init_script"](bad)

    def test_a_script_that_loads_boot_time_definitions_fails(self) -> None:
        bad = INIT_SCRIPT.read_text() + "\nload_definitions = /etc/rabbitmq/definitions.json\n"
        with pytest.raises(AssertionError, match="must never run"):
            checker["check_init_script"](bad)

    def test_a_script_missing_a_default_fails(self) -> None:
        text = INIT_SCRIPT.read_text().replace("${RABBITMQ_DLQ_OVERFLOW:-drop-head}", "$RABBITMQ_DLQ_OVERFLOW")
        with pytest.raises(AssertionError, match="must default"):
            checker["check_init_script"](text)

    def test_trailing_command_after_the_exec_fails(self) -> None:
        text = INIT_SCRIPT.read_text().rstrip() + '\necho "leftover"\n'
        with pytest.raises(AssertionError, match="nothing may follow"):
            checker["check_init_script"](text)


class TestComposeRunsTheBrokerStockAndDeclaresThePolicyAfterHealthy:
    def test_real_compose_passes(self) -> None:
        checker["check_compose"]((REPO_ROOT / "docker-compose.yml").read_text())

    def test_a_boot_time_entrypoint_on_rabbitmq_fails(self) -> None:
        compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
        compose["services"]["rabbitmq"]["entrypoint"] = ["/scripts/rabbitmq-entrypoint.sh"]
        with pytest.raises(AssertionError, match="stock image entrypoint"):
            checker["check_compose"](yaml.dump(compose))

    def test_init_service_not_waiting_on_health_fails(self) -> None:
        compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
        compose["services"]["rabbitmq-dlq-policy-init"]["depends_on"] = {"rabbitmq": {"condition": "service_started"}}
        with pytest.raises(AssertionError, match="wait for the broker to be healthy"):
            checker["check_compose"](yaml.dump(compose))

    def test_init_service_using_a_new_third_party_image_fails(self) -> None:
        compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
        compose["services"]["rabbitmq-dlq-policy-init"]["image"] = "curlimages/curl:latest"
        with pytest.raises(AssertionError, match="reuse rabbitmq's own"):
            checker["check_compose"](yaml.dump(compose))


class TestAdminGuideDocumentsTheRealDlqNames:
    def test_real_admin_guide_passes(self) -> None:
        checker["check_admin_guide"](ADMIN_GUIDE.read_text())

    def test_admin_guide_missing_a_dlq_name_fails(self) -> None:
        text = ADMIN_GUIDE.read_text().replace("groovemap-musicbrainz-brainztableinator-releases.dlq", "typo")
        with pytest.raises(AssertionError, match="missing DLQ name"):
            checker["check_admin_guide"](text)


def test_full_repository_check_passes() -> None:
    checker["check_repository"]()


class TestComposeServiceWiring:
    """docker-compose.yml itself, independent of the checker script."""

    def _services(self) -> dict[str, Any]:
        compose: dict[str, Any] = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
        services: dict[str, Any] = compose["services"]
        return services

    def test_rabbitmq_runs_the_stock_entrypoint(self) -> None:
        assert "entrypoint" not in self._services()["rabbitmq"]

    def test_init_service_reuses_the_rabbitmq_image_and_waits_for_health(self) -> None:
        services = self._services()
        init_service = services["rabbitmq-dlq-policy-init"]
        assert init_service["image"] == services["rabbitmq"]["image"]
        assert init_service["depends_on"] == {"rabbitmq": {"condition": "service_healthy"}}
        assert init_service["restart"] == "no"
        assert init_service["entrypoint"] == ["/scripts/rabbitmq-dlq-policy-init.sh"]

    def test_cap_env_vars_default_without_requiring_operator_input(self) -> None:
        environment = self._services()["rabbitmq-dlq-policy-init"]["environment"]
        assert environment["RABBITMQ_DLQ_MAX_LENGTH"] == "${RABBITMQ_DLQ_MAX_LENGTH:-50000}"
        assert environment["RABBITMQ_DLQ_MAX_LENGTH_BYTES"] == "${RABBITMQ_DLQ_MAX_LENGTH_BYTES:-536870912}"
        assert environment["RABBITMQ_DLQ_OVERFLOW"] == "${RABBITMQ_DLQ_OVERFLOW:-drop-head}"

    def test_prod_overlay_wires_the_same_secret_to_both_rabbitmq_and_the_init_service(self) -> None:
        prod = yaml.load((REPO_ROOT / "docker-compose.prod.yml").read_text(), Loader=_IgnoreUnknownTagsLoader)["services"]  # noqa: S506
        assert prod["rabbitmq"]["secrets"] == ["rabbitmq_password", "rabbitmq_username"]
        assert prod["rabbitmq-dlq-policy-init"]["secrets"] == ["rabbitmq_password", "rabbitmq_username"]
        # The init service must not fork the broker's own boot-time entrypoint wiring.
        assert "entrypoint" not in prod["rabbitmq-dlq-policy-init"]


class TestInitScriptFunctional:
    """Runs the real script with a stubbed rabbitmqadmin, mirroring
    test_redis_entrypoint_script.py's approach for scripts/redis-entrypoint.sh."""

    def _run(self, tmp_path: Path, env_overrides: dict[str, str], secrets: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        stub_dir = tmp_path / "bin"
        stub_dir.mkdir()
        stub = stub_dir / "rabbitmqadmin"
        # Echo argv and the credential env vars the stub actually sees, so the test can
        # assert both the rendered --definition and which credentials were forwarded.
        stub.write_text(
            textwrap.dedent(
                """\
                #!/bin/sh
                echo "ARGS:$*"
                echo "USER:${RABBITMQADMIN_USERNAME-}"
                echo "PASS:${RABBITMQADMIN_PASSWORD-}"
                """
            )
        )
        stub.chmod(0o755)

        secrets_dir = tmp_path / "run_secrets"
        secrets_dir.mkdir()
        for name, value in (secrets or {}).items():
            (secrets_dir / name).write_text(value)

        # The script hardcodes /run/secrets/*; patch it to a tmp_path-local copy so the
        # test is hermetic (same technique test_redis_entrypoint_script.py uses).
        patched = tmp_path / "rabbitmq-dlq-policy-init.sh"
        text = INIT_SCRIPT.read_text()
        text = text.replace("/run/secrets/rabbitmq_username", str(secrets_dir / "rabbitmq_username"))
        text = text.replace("/run/secrets/rabbitmq_password", str(secrets_dir / "rabbitmq_password"))
        patched.write_text(text)
        patched.chmod(0o755)

        env = dict(os.environ)
        env["PATH"] = f"{stub_dir}:{env['PATH']}"
        env["RABBITMQADMIN_USERNAME"] = "groovemap"
        env["RABBITMQADMIN_PASSWORD"] = "groovemap"
        env.update(env_overrides)

        return subprocess.run([str(patched)], capture_output=True, text=True, env=env, timeout=10)

    @staticmethod
    def _definition(args_line: str) -> dict[str, object]:
        match = re.search(r"--definition=(\{.*\})", args_line)
        assert match is not None, args_line
        definition: dict[str, object] = json.loads(match.group(1))
        return definition

    def test_declares_the_policy_with_default_cap_values(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, {})
        assert result.returncode == 0, result.stderr
        lines = result.stdout.splitlines()
        assert "declare policy" in lines[0]
        assert "--name=catalog-dlq-cap" in lines[0]
        assert r"--pattern=^groovemap-.*\.dlq$" in lines[0]
        assert "--apply-to=queues" in lines[0]
        assert "--priority=10" in lines[0]
        assert self._definition(lines[0]) == {"max-length": 50000, "max-length-bytes": 536870912, "overflow": "drop-head"}

    def test_declares_the_policy_with_operator_supplied_cap_values(self, tmp_path: Path) -> None:
        result = self._run(
            tmp_path,
            {
                "RABBITMQ_DLQ_MAX_LENGTH": "1000",
                "RABBITMQ_DLQ_MAX_LENGTH_BYTES": "2000000",
                "RABBITMQ_DLQ_OVERFLOW": "reject-publish",
            },
        )
        assert result.returncode == 0, result.stderr
        definition = self._definition(result.stdout.splitlines()[0])
        assert definition == {"max-length": 1000, "max-length-bytes": 2000000, "overflow": "reject-publish"}

    def test_uses_the_env_credentials_when_no_secret_files_are_present(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, {})
        assert result.returncode == 0, result.stderr
        assert "USER:groovemap" in result.stdout
        assert "PASS:groovemap" in result.stdout

    def test_prefers_docker_secrets_over_the_env_credentials_when_present(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, {}, secrets={"rabbitmq_username": "prod-user\n", "rabbitmq_password": "prod-pass\n"})
        assert result.returncode == 0, result.stderr
        assert "USER:prod-user" in result.stdout
        assert "PASS:prod-pass" in result.stdout

    def test_script_is_executable(self) -> None:
        assert INIT_SCRIPT.stat().st_mode & 0o111
