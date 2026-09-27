"""Functional and security-regression tests for
scripts/provision-embedding-pipeline-login.sh.

An earlier revision built the CREATE ROLE statement by interpolating the
password directly into a `psql -c "..."` string (`PASSWORD '$password'`) —
vulnerable to SQL injection if the password ever contained a single quote,
and exposed the password in plain text on the `docker exec`/`psql` command
line (visible to any local user via `ps`, and to shell history). The fixed
script never puts the password on a command line or in SQL text at all: it
crosses into the container only as an environment variable (`docker exec
-e`), and the SQL — delivered over stdin, not `-c` — reads it back with
psql's `\\getenv` and interpolates it as a quoted literal (`:'pw'`). The
username is validated as a plain identifier up front and always substituted
via psql's quoted-identifier form (`:"username"`), never string-built.

These tests stub `docker` on PATH (there is no real PostgreSQL container in
this test environment) and assert both the security property — a
single-quote-and-injection-shaped password never appears in any SQL text or
command-line argument, anywhere — and the ordinary role-provisioning
behavior.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "provision-embedding-pipeline-login.sh"

# Deliberately shaped like an injection attempt: a single quote followed by a
# second SQL statement. If the password ever reached a SQL string via direct
# interpolation, this would close the literal early and execute DROP ROLE.
INJECTION_PASSWORD = "it's-a-secret'; DROP ROLE embedding_pipeline; --"


def _stub_docker(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    """Install a fake `docker` on PATH that simulates `inspect`, the two
    `role_exists` lookups, the CREATE ROLE call (stdin + `-e` capture), and
    the final GRANT — driven by FAKE_ROLE_EMBEDDING_PIPELINE_EXISTS /
    FAKE_ROLE_TARGET_EXISTS so a test can pick the branch it wants to exercise.
    """
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    docker = stub_dir / "docker"
    logs = {
        "argv": tmp_path / "docker-argv.log",
        "stdin": tmp_path / "docker-stdin.log",
        "password_arg": tmp_path / "docker-password-arg.log",
    }
    docker.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            printf 'ARGV: %s\\n' "$*" >>"$FAKE_DOCKER_ARGV_LOG"

            if [[ "${1:-}" == "inspect" ]]; then
              echo "true"
              exit 0
            fi

            role=""
            has_stdin_flag=0
            password_arg=""
            for arg in "$@"; do
              case "$arg" in
                role=*) role="${arg#role=}" ;;
                -i) has_stdin_flag=1 ;;
                EMBEDDING_PIPELINE_PASSWORD=*) password_arg="$arg" ;;
              esac
            done

            if [[ -n "$role" ]]; then
              if [[ "$role" == "embedding_pipeline" ]]; then
                [[ "${FAKE_ROLE_EMBEDDING_PIPELINE_EXISTS:-1}" == "1" ]] && echo "1" || echo ""
              else
                [[ "${FAKE_ROLE_TARGET_EXISTS:-0}" == "1" ]] && echo "1" || echo ""
              fi
              exit 0
            fi

            if [[ "$has_stdin_flag" == "1" ]]; then
              cat >>"$FAKE_DOCKER_STDIN_LOG"
              printf '%s' "$password_arg" >"$FAKE_DOCKER_PASSWORD_ARG_LOG"
              exit 0
            fi

            exit 0
            """
        )
    )
    docker.chmod(0o755)
    return stub_dir, logs


def _run(
    tmp_path: Path,
    *,
    username: str | None = None,
    password: str = "unused-in-this-branch",
    embedding_pipeline_exists: bool = True,
    target_exists: bool = False,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Path]]:
    stub_dir, logs = _stub_docker(tmp_path)

    env = dict(os.environ)
    env["PATH"] = f"{stub_dir}:{env['PATH']}"
    env["EMBEDDING_PIPELINE_POSTGRES_PASSWORD"] = password
    env["FAKE_DOCKER_ARGV_LOG"] = str(logs["argv"])
    env["FAKE_DOCKER_STDIN_LOG"] = str(logs["stdin"])
    env["FAKE_DOCKER_PASSWORD_ARG_LOG"] = str(logs["password_arg"])
    env["FAKE_ROLE_EMBEDDING_PIPELINE_EXISTS"] = "1" if embedding_pipeline_exists else "0"
    env["FAKE_ROLE_TARGET_EXISTS"] = "1" if target_exists else "0"
    if username is not None:
        env["EMBEDDING_PIPELINE_POSTGRES_USERNAME"] = username
    else:
        env.pop("EMBEDDING_PIPELINE_POSTGRES_USERNAME", None)

    result = subprocess.run(
        [str(SCRIPT)],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
        timeout=10,
    )
    return result, logs


class TestProvisionEmbeddingPipelineLoginScript:
    def test_creates_login_with_an_injection_shaped_password(self, tmp_path: Path) -> None:
        result, logs = _run(
            tmp_path, username="embedding_pipeline_login", password=INJECTION_PASSWORD, embedding_pipeline_exists=True, target_exists=False
        )

        assert result.returncode == 0, result.stderr
        assert "created embedding_pipeline_login" in result.stdout
        assert "is a member of embedding_pipeline" in result.stdout

        # The password must never appear in the SQL sent over stdin — this is
        # the actual injection surface: whatever reaches this text is what
        # PostgreSQL parses as a statement.
        stdin_text = logs["stdin"].read_text()
        assert INJECTION_PASSWORD not in stdin_text
        assert "\\getenv pw EMBEDDING_PIPELINE_PASSWORD" in stdin_text
        assert "CREATE ROLE :\"username\" LOGIN PASSWORD :'pw';" in stdin_text

        # It reaches the container exactly once, unmodified, as an environment
        # variable assignment — never re-embedded into a SQL string.
        assert logs["password_arg"].read_text() == f"EMBEDDING_PIPELINE_PASSWORD={INJECTION_PASSWORD}"

        # Of every docker/psql invocation logged, the password appears only in
        # the one `docker exec -e EMBEDDING_PIPELINE_PASSWORD=...` call that
        # carries it into the container's environment — and never combined
        # with a `-c` flag, i.e. never as part of a SQL string handed to psql
        # on the command line the way the vulnerable revision did.
        argv_lines = logs["argv"].read_text().splitlines()
        carrying_lines = [line for line in argv_lines if INJECTION_PASSWORD in line]
        assert len(carrying_lines) == 1
        assert "EMBEDDING_PIPELINE_PASSWORD=" in carrying_lines[0]
        assert " -c " not in carrying_lines[0] and not carrying_lines[0].endswith(" -c")

    def test_leaves_existing_login_password_alone(self, tmp_path: Path) -> None:
        result, logs = _run(tmp_path, username="embedding_pipeline_login", embedding_pipeline_exists=True, target_exists=True)

        assert result.returncode == 0, result.stderr
        assert "already exists; leaving its password alone" in result.stdout
        assert "is a member of embedding_pipeline" in result.stdout
        # No CREATE ROLE call means no stdin/-e invocation at all.
        assert not logs["stdin"].exists() or not logs["stdin"].read_text()
        assert not logs["password_arg"].exists() or not logs["password_arg"].read_text()

    def test_skips_when_embedding_pipeline_role_does_not_exist_yet(self, tmp_path: Path) -> None:
        """The pgvector dependency (gm-deployment-kh5) hasn't landed: this must
        be a clean no-op, never a `just check`/CI failure."""
        result, logs = _run(tmp_path, embedding_pipeline_exists=False)

        assert result.returncode == 0, result.stderr
        assert "no embedding_pipeline role yet" in result.stderr
        assert "Nothing to do" in result.stderr
        assert not logs["stdin"].exists() or not logs["stdin"].read_text()

    def test_rejects_an_unsafe_username_without_calling_docker(self, tmp_path: Path) -> None:
        result, logs = _run(tmp_path, username="embedding_pipeline'; DROP TABLE artist_embeddings; --")

        assert result.returncode == 2
        assert "must match" in result.stderr
        assert not logs["argv"].exists(), "an unsafe username must be rejected before docker is ever invoked"

    def test_script_is_executable(self) -> None:
        assert SCRIPT.stat().st_mode & 0o111
