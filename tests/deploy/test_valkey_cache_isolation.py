"""Filling the catalog cache must not make an auth write fail (gm-deployment-t52v).

`valkey` is noeviction and holds auth state (revoked tokens, password changes) that eviction would
silently drop. Catalog cache keys come from caller-chosen values, so a full cache on the same
instance rejects every write, auth included. The overlay gives the caches their own evicting
instance. The static tests pin its shape; the `integration` tests run real Valkey containers from
the stack's pinned image and show the failure on a shared instance and its absence on the pair.
"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml


if TYPE_CHECKING:
    from collections.abc import Iterator

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
OVERLAY = yaml.safe_load((REPO_ROOT / "docker-compose.cache-isolation.yml").read_text())
CACHE = OVERLAY["services"]["valkey-cache"]


class TestOverlayShape:
    def test_the_cache_instance_evicts_and_does_not_persist(self) -> None:
        command = CACHE["command"]
        assert "--maxmemory-policy allkeys-lru" in command
        assert "--appendonly no" in command
        assert '--save ""' in command
        assert "--maxmemory " in command

    def test_the_auth_instance_is_untouched_and_still_noeviction(self) -> None:
        assert "--maxmemory-policy noeviction" in BASE["services"]["valkey"]["command"]
        assert "valkey" not in OVERLAY["services"], "the overlay adds an instance, it never changes the auth one"
        assert "allkeys" not in BASE["services"]["valkey"]["command"]

    def test_same_image_pin_as_the_auth_instance(self) -> None:
        assert CACHE["image"] == BASE["services"]["valkey"]["image"]
        assert "@sha256:" in CACHE["image"]

    def test_it_authenticates_with_the_stack_secret_and_is_not_published(self) -> None:
        assert CACHE["entrypoint"] == ["/scripts/valkey-entrypoint.sh"]
        assert CACHE["secrets"] == ["valkey_password"]
        assert "ports" not in CACHE, "the cache is reached over the internal network only"
        assert "valkey_password" in CACHE["healthcheck"]["test"][1]

    def test_api_is_pointed_at_it_and_waits_for_it(self) -> None:
        api = OVERLAY["services"]["api"]
        assert api["environment"]["CATALOG_CACHE_VALKEY_HOST"] == "valkey-cache"
        assert api["depends_on"]["valkey-cache"]["condition"] == "service_healthy"

    def test_the_overlay_is_validated_by_the_compose_check(self) -> None:
        assert "docker-compose.cache-isolation.yml" in (REPO_ROOT / "scripts" / "check-compose.sh").read_text()


# ---- real Valkey -----------------------------------------------------------------------------------

DOCKER = shutil.which("docker")
IMAGE = BASE["services"]["valkey"]["image"]
SMALL = "8mb"
VALUE = "x" * 1024
KEYS = 20_000  # about 20 MB of values against an 8 MB cap


def _docker(*args: str, stdin: str | None = None, timeout: int = 60) -> str:
    result = subprocess.run([DOCKER or "docker", *args], input=stdin, capture_output=True, text=True, timeout=timeout, check=False)
    return (result.stdout + result.stderr).strip()


def _server_args(command: str) -> list[str]:
    """The compose command with the cap shrunk so a test fills it in a moment."""
    command = re.sub(r"\$\{CATALOG_CACHE_MAXMEMORY:-\w+\}", SMALL, command)
    command = re.sub(r"--maxmemory \d+mb", f"--maxmemory {SMALL}", command)
    command = command.replace("--appendonly yes", "--appendonly no")
    return shlex.split(command)


def _start(command: str) -> str:
    name = f"gm-t52v-{uuid.uuid4().hex[:8]}"
    out = _docker("run", "--detach", "--rm", "--name", name, "--memory", "128m", IMAGE, *_server_args(command))
    assert not out.startswith("docker:"), out
    for _ in range(50):
        if _docker("exec", name, "valkey-cli", "ping") == "PONG":
            return name
        _docker("exec", name, "sleep", "0.2")
    raise AssertionError(f"{name} did not become ready")


def _fill(name: str, prefix: str) -> int:
    """Pipe KEYS distinct one-kilobyte SETs at the server; returns how many it rejected."""
    protocol = "".join(f"*3\r\n$3\r\nSET\r\n${len(f'{prefix}{i}')}\r\n{prefix}{i}\r\n${len(VALUE)}\r\n{VALUE}\r\n" for i in range(KEYS))
    out = _docker("exec", "-i", name, "valkey-cli", "--pipe", stdin=protocol, timeout=120)
    match = re.search(r"errors: (\d+)", out)
    assert match, out
    return int(match.group(1))


def _info(name: str, field: str) -> int:
    out = _docker("exec", name, "valkey-cli", "INFO", "stats")
    match = re.search(rf"^{field}:(\d+)", out, re.MULTILINE)
    assert match, out
    return int(match.group(1))


@pytest.fixture
def started() -> Iterator[list[str]]:
    names: list[str] = []
    yield names
    for name in names:
        _docker("rm", "--force", "--volumes", name)


needs_docker = pytest.mark.skipif(DOCKER is None, reason="docker is required")


@needs_docker
@pytest.mark.integration
@pytest.mark.timeout(240)
class TestOnRealValkey:
    def test_before_a_full_cache_on_the_shared_instance_fails_an_auth_write(self, started: list[str]) -> None:
        shared = _start(BASE["services"]["valkey"]["command"])
        started.append(shared)
        rejected = _fill(shared, "cache:explore:")
        denied = _docker("exec", shared, "valkey-cli", "SET", "revoked:jti:abc", "1")
        print(f"\nBEFORE (one noeviction instance, {SMALL}): {rejected} of {KEYS} cache writes rejected; auth write -> {denied!r}")
        assert rejected > 0
        assert "OOM" in denied, "the auth write was rejected because the cache filled the shared instance"

    def test_after_the_same_fill_leaves_the_auth_instance_writable_and_its_keys_intact(self, started: list[str]) -> None:
        auth = _start(BASE["services"]["valkey"]["command"])
        cache = _start(CACHE["command"])
        started.extend([auth, cache])
        _docker("exec", auth, "valkey-cli", "SET", "revoked:jti:before", "1")

        rejected = _fill(cache, "cache:explore:")
        evicted = _info(cache, "evicted_keys")
        written = _docker("exec", auth, "valkey-cli", "SET", "revoked:jti:abc", "1")
        survives = _docker("exec", auth, "valkey-cli", "GET", "revoked:jti:before")
        print(
            f"\nAFTER (separate allkeys-lru cache, {SMALL}): {rejected} of {KEYS} cache writes rejected, {evicted} keys evicted; auth write -> {written!r}; earlier revoked key -> {survives!r}"
        )
        assert rejected == 0, "an evicting cache accepts every write"
        assert evicted > 0, "the cache really did overfill and evict"
        assert written == "OK"
        assert survives == "1"
        assert _info(auth, "evicted_keys") == 0


def test_the_helper_commands_shrink_the_cap() -> None:
    args: Any = _server_args(CACHE["command"])
    assert SMALL in args
    assert "allkeys-lru" in args
