"""The console's Secure session cookie on a plain-http homelab: the opt-in and the deploy-time notice.

The overlay is checked from the YAML and, where a docker binary exists, from Compose's own
render. Nothing here starts a container; the shell script runs against the fake docker from
test_deploy.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.deploy.test_deploy import PROD, Deploy, helpers
from tests.deploy.test_docker_compose_prod import REPO_ROOT, _prod_compose


OPT_IN = "GROOVEMAP_ALLOW_INSECURE_CONSOLE"
COMPOSE_FILES = ("-f", "docker-compose.yml", "-f", "docker-compose.prod.yml")


# --- the overlay -----------------------------------------------------------------------


def test_overlay_reads_the_cookie_setting_from_a_file_that_exists_only_when_opted_in() -> None:
    entry = _prod_compose()["services"]["dashboard"]["env_file"][0]
    assert entry["required"] is False
    assert entry["path"] == "config/console-insecure.${" + OPT_IN + ":-false}.env"
    assert not (REPO_ROOT / "config" / "console-insecure.false.env").exists()
    assert helpers.parse_env((REPO_ROOT / "config" / "console-insecure.true.env").read_text()) == {"SESSION_COOKIE_SECURE": "false"}


@pytest.fixture(scope="module")
def render() -> object:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("no docker binary to render the Compose files")

    def run(*extra: str, **env: str) -> bytes:
        return subprocess.run(
            [docker, "compose", "--env-file", "config/validation.env", *COMPOSE_FILES, *extra, "config", "--format", "json"],
            cwd=REPO_ROOT,
            capture_output=True,
            check=True,
            env={"PATH": str(Path(docker).parent), "HOME": str(Path.home()), **env},
            timeout=60,
        ).stdout

    return run


def test_default_render_leaves_the_cookie_unset_and_ignores_other_values(render: object) -> None:
    default = render()  # type: ignore[operator]
    assert "SESSION_COOKIE_SECURE" not in json.loads(default)["services"]["dashboard"]["environment"]
    for value in ("false", "", "yes", "1"):
        assert render(**{OPT_IN: value}) == default  # type: ignore[operator]


def test_opt_in_turns_the_cookie_off_for_the_dashboard_only(render: object) -> None:
    services = json.loads(render(**{OPT_IN: "true"}))["services"]  # type: ignore[operator]
    assert services["dashboard"]["environment"]["SESSION_COOKIE_SECURE"] == "false"
    assert "env_file" not in services["dashboard"]
    assert all("SESSION_COOKIE_SECURE" not in (s.get("environment") or {}) for n, s in services.items() if n != "dashboard")


# --- detection -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://192.168.1.20:8006", True),
        ("http://nas:8006", True),
        ("http://nas.lan", True),
        ("http://[fd00::5]:8006", True),
        ("HTTP://10.0.0.2", True),
        ("https://groovemap.example.com", False),
        ("http://localhost:8006", False),
        ("http://127.0.0.1:8006", False),
        ("http://[::1]:8006", False),
        ("http://app.localhost", False),
        ("", False),
        ("groovemap.lan", False),
    ],
)
def test_plain_http_on_a_remote_host(url: str, expected: bool) -> None:
    assert helpers.is_plain_http_remote(url) is expected


def test_the_notice_applies_to_production_over_plain_http_until_opted_in() -> None:
    needs = helpers.console_needs_https
    lan = {"APP_BASE_URL": "http://nas:8006"}
    assert needs("prod", lan, {}, "nas") == "http://nas:8006"
    assert needs("dev", lan, {}, "nas") is None
    assert needs("prod", {"APP_BASE_URL": "https://groovemap.example.com"}, {}, "nas") is None
    assert needs("prod", {"APP_BASE_URL": "http://localhost:8006"}, {}, "nas") is None
    assert needs("prod", lan | {OPT_IN: "true"}, {}, "nas") is None
    assert needs("prod", lan | {OPT_IN: "yes"}, {}, "nas") == "http://nas:8006"
    assert needs("prod", lan, {OPT_IN: "true"}, "nas") is None


def test_without_a_base_url_the_console_port_itself_is_judged() -> None:
    assert helpers.console_needs_https("prod", {}, {}, "nas") == "http://nas:8003"
    assert helpers.console_needs_https("prod", {"WEB_BIND_ADDRESS": "127.0.0.1"}, {}, "nas") is None


# --- the offer -------------------------------------------------------------------------


def _console_http(
    tmp_path: Path, answer: str | None, text: str = "APP_BASE_URL=http://nas:8006\n", environ: dict[str, str] | None = None
) -> tuple[int, str]:
    env_path = tmp_path / ".env"
    env_path.write_text(text)
    answers: list[str] = []

    def ask(prompt: str) -> str:
        answers.append(prompt)
        return answer or ""

    status = helpers.console_http(
        "prod",
        env_path=env_path,
        environ=environ or {},
        hostname="nas",
        recreate_command="docker compose up -d --no-deps dashboard",
        interactive=answer is not None,
        ask=ask,
    )
    return status, env_path.read_text()


def test_the_notice_names_both_options_and_the_proxy_docs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    status, env = _console_http(tmp_path, None)
    out = capsys.readouterr().out
    assert status == 0
    assert env == "APP_BASE_URL=http://nas:8006\n"
    assert "http://nas:8006" in out
    assert "Recommended" in out
    assert "skills/groovemap-homelab/references/reverse-proxy.md" in out
    assert "docs/homelab.md" in out
    assert f"{OPT_IN}=true" in out
    assert "docker compose up -d --no-deps dashboard" in out


@pytest.mark.parametrize("answer", ["", "n", "no", "maybe"])
def test_the_interactive_offer_defaults_to_no(tmp_path: Path, answer: str) -> None:
    status, env = _console_http(tmp_path, answer)
    assert status == 0
    assert env == "APP_BASE_URL=http://nas:8006\n"


@pytest.mark.parametrize("answer", ["y", "YES"])
def test_yes_writes_the_opt_in_and_asks_for_the_console_to_be_recreated(tmp_path: Path, answer: str) -> None:
    status, env = _console_http(tmp_path, answer)
    assert status == helpers.CONSOLE_RECREATE
    assert helpers.parse_env(env) == {"APP_BASE_URL": "http://nas:8006", OPT_IN: "true"}


def test_a_shell_value_outranks_env_so_nothing_is_written(tmp_path: Path) -> None:
    status, env = _console_http(tmp_path, "y", environ={OPT_IN: "false"})
    assert status == 0
    assert env == "APP_BASE_URL=http://nas:8006\n"


def test_nothing_is_said_when_the_console_is_fine(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _console_http(tmp_path, "y", "APP_BASE_URL=https://groovemap.example.com\n")[0] == 0
    assert _console_http(tmp_path, "y", f"APP_BASE_URL=http://nas:8006\n{OPT_IN}=true\n")[0] == 0
    assert capsys.readouterr().out == ""


# --- scripts/deploy.sh -----------------------------------------------------------------


@pytest.fixture
def deploy(tmp_path: Path) -> Deploy:
    return Deploy(tmp_path)


def test_deploy_explains_before_printing_the_endpoints_and_changes_nothing(deploy: Deploy) -> None:
    deploy.write_env("APP_BASE_URL=http://nas:8006\n")
    result = deploy.run("--no-wait")
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout
    assert out.index("Console sign-in") < out.index("Your GrooveMap")
    assert "reverse-proxy.md" in out
    assert OPT_IN not in deploy.env_values
    assert deploy.ups == [f"docker compose {PROD} up -d --wait"]


def test_deploy_is_quiet_with_https_the_opt_in_or_the_dev_stack(deploy: Deploy) -> None:
    for text, args in (
        ("APP_BASE_URL=https://groovemap.example.com\n", ("--no-wait",)),
        (f"APP_BASE_URL=http://nas:8006\n{OPT_IN}=true\n", ("--no-wait",)),
        ("APP_BASE_URL=http://nas:8006\n", ("--no-wait", "--dev")),
    ):
        deploy.write_env(text)
        result = deploy.run(*args)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Console sign-in" not in result.stdout
