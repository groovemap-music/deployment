"""Functional tests for scripts/valkey-entrypoint.sh.

Valkey doesn't natively support the Docker ``_FILE`` secret convention, so the
production overlay delegates to this wrapper to read
``/run/secrets/valkey_password`` and append ``--requirepass`` before handing
off to the official image entrypoint — mirroring scripts/neo4j-entrypoint.sh
and scripts/rabbitmq-entrypoint.sh.

These tests stub out ``docker-entrypoint.sh`` on PATH (the real one lives
inside the valkey/valkey:9-alpine image, not on the host) and assert the wrapper
invokes it with the right arguments.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "valkey-entrypoint.sh"


def _run_with_stub_entrypoint(tmp_path: Path, secret_file: Path | None) -> subprocess.CompletedProcess[str]:
    """Run valkey-entrypoint.sh with a stub docker-entrypoint.sh on PATH that
    just echoes its argv, and a fake /run/secrets path (via a symlink-free
    override: the script hardcodes /run/secrets/valkey_password, so this only
    works when we can write there — instead we exercise the script's logic
    via a copy with the secrets path parameterized through an env var for
    testability isn't available, so we assert behavior by placing/removing a
    real file at a location the test controls by invoking a modified PATH
    and HOME instead).
    """
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "docker-entrypoint.sh"
    stub.write_text(
        textwrap.dedent(
            """\
            #!/bin/sh
            echo "ARGS:$*"
            """
        )
    )
    stub.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{stub_dir}:{env['PATH']}"

    secrets_dir = tmp_path / "run_secrets"
    secrets_dir.mkdir()
    if secret_file is not None:
        (secrets_dir / "valkey_password").write_text(secret_file.read_text() if secret_file.exists() else "")

    # The script hardcodes /run/secrets/valkey_password. We can't safely write
    # to the real /run/secrets on a dev/CI box, so instead run the script
    # under a mount-namespace-free substitution: copy it and rewrite the one
    # path constant to our tmp secrets dir. This keeps the test hermetic
    # while still exercising the exact conditional/exec logic byte-for-byte.
    patched_script = tmp_path / "valkey-entrypoint.sh"
    patched_script.write_text(SCRIPT.read_text().replace("/run/secrets/valkey_password", str(secrets_dir / "valkey_password")))
    patched_script.chmod(0o755)

    return subprocess.run(
        [str(patched_script), "valkey-server", "--appendonly", "yes"],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )


class TestValkeyEntrypointScript:
    def test_appends_requirepass_when_secret_present(self, tmp_path: Path) -> None:
        secret = tmp_path / "secret_source"
        secret.write_text("s3cr3t-pw\n")

        result = _run_with_stub_entrypoint(tmp_path, secret_file=secret)

        assert result.returncode == 0, result.stderr
        assert "--requirepass s3cr3t-pw" in result.stdout
        assert "valkey-server --appendonly yes" in result.stdout

    def test_does_not_leak_password_on_failure_path(self, tmp_path: Path) -> None:
        """Sanity check: with no secret file, no --requirepass flag is added at all
        (the server would run unauthenticated, matching today's dev behavior) —
        it must not silently pass an empty/garbage password.
        """
        result = _run_with_stub_entrypoint(tmp_path, secret_file=None)

        assert result.returncode == 0, result.stderr
        assert "--requirepass" not in result.stdout
        assert "valkey-server --appendonly yes" in result.stdout

    def test_script_is_executable(self) -> None:
        assert SCRIPT.stat().st_mode & 0o111
