"""Every service that reads a file secret can read it: runtime identity vs the secrets' owner.

scripts/create-secrets.sh writes each secret mode 0600 for the user who runs it, which
docker-compose.yml names `${UID:-1000}:${GID:-1000}`. A process running as any other non-root
UID gets "permission denied" (redis_exporter, UID 59000, crash-looped on exactly this). Compose
secret uid/gid remapping, world-readable modes and added capabilities are not the fix; the
service runs as the files' owner, or starts as root and reads the file before dropping
privileges (root bypasses mode bits while CAP_DAC_OVERRIDE is kept).

INVENTORY records, per third-party image, the user read from the pinned image's config blob
(registry API, linux/amd64; no image pulled) and the entrypoint behaviour that decides which
identity opens the secret. A new pin or a new secret-reading service fails here until its
identity is recorded and verified. The GrooveMap images' own USER is irrelevant: Compose sets
`user:` explicitly and this test requires it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]
OWNER = "1000:1000"  # ${UID:-1000}:${GID:-1000}, what an unset UID/GID resolves to

# service -> (image reference without the registry digest check, image config User, how it opens secrets)
#   "owner": runs as the secrets' owner via an explicit compose `user:`
#   "root":  image config has no USER; the entrypoint (or the wrapper in scripts/) reads the
#            secret as root, then drops privileges. Needs root and CAP_DAC_OVERRIDE.
INVENTORY: dict[str, dict[str, str]] = {
    "grafana": {
        "image": "grafana/grafana:13.2.4@sha256:dbbc38ea21103cefb50396340cd88afec779c7e070b0a7bf5a75b4ce46651d3f",
        "image_user": "472",
        "reads_as": "owner",
    },
    "postgres-exporter": {
        "image": "prometheuscommunity/postgres-exporter:v0.20.1@sha256:ac5ec343104fae0e2d84a27bb8d69b38430a11910c5382cad85d478d2bab713e",
        "image_user": "nobody",
        "reads_as": "owner",
    },
    "valkey-exporter": {
        "image": "oliver006/redis_exporter:v1.95.0@sha256:afb21407aba3df1b4010899f155fc6b1ba2026c97b725d72aca4019d772e7b6b",
        "image_user": "59000:59000",
        "reads_as": "owner",
    },
    "postgres": {
        "image": "postgres:18-alpine@sha256:77f585114c32fbca283dc835b0596f4e52b51b4c6662d7810b2f4084f60a1873",
        "image_user": "",
        "reads_as": "root",  # docker-entrypoint.sh file_env, before gosu postgres
    },
    "rabbitmq": {
        "image": "rabbitmq:4-management@sha256:c200fd496482cda18c412be4e6f244830e1e75949b8cf5f0e887cea9ea456109",
        "image_user": "",
        "reads_as": "root",  # scripts/rabbitmq-entrypoint.sh, before docker-entrypoint.sh drops
    },
    "rabbitmq-dlq-policy-init": {
        "image": "rabbitmq:4-management@sha256:c200fd496482cda18c412be4e6f244830e1e75949b8cf5f0e887cea9ea456109",
        "image_user": "",
        "reads_as": "root",  # runs as the image default (root); scripts/rabbitmq-dlq-policy-init.sh
    },
    "neo4j": {
        "image": "neo4j:2026.10.0-community@sha256:388c0e5c6d4543e3d0e6cee3d1e751c05ad9314aa73342cc63cd0ea0946e38bb",
        "image_user": "",
        "reads_as": "root",  # scripts/neo4j-entrypoint.sh, before /startup/docker-entrypoint.sh drops
    },
    "valkey": {
        "image": "valkey/valkey:9.1.2-alpine@sha256:48332870af354a799964c0012ae1194a0bf2bf894eb508f945810596dc2d8d11",
        "image_user": "",
        "reads_as": "root",  # scripts/valkey-entrypoint.sh, before docker-entrypoint.sh drops
    },
    # The same pinned images, run as one-shot store checks under the owner (external overlays).
    "store-check-postgres": {
        "image": "postgres:18-alpine@sha256:77f585114c32fbca283dc835b0596f4e52b51b4c6662d7810b2f4084f60a1873",
        "image_user": "",
        "reads_as": "owner",
    },
    "store-check-valkey": {
        "image": "valkey/valkey:9.1.2-alpine@sha256:48332870af354a799964c0012ae1194a0bf2bf894eb508f945810596dc2d8d11",
        "image_user": "",
        "reads_as": "owner",
    },
}
# GrooveMap-built services: they must run under the explicit owner identity.
OWNER_SERVICES = {
    "api", "brainzgraphinator", "brainztableinator", "dashboard", "embeddings", "extractor-discogs",
    "extractor-musicbrainz", "graphinator", "insights", "schema-init", "tableinator",
}  # fmt: skip

STACKS = {
    "prod": ["docker-compose.yml", "docker-compose.prod.yml"],
    "external": [
        "docker-compose.yml", "docker-compose.prod.yml",
        "docker-compose.external-postgres.yml", "docker-compose.external-valkey.yml",
    ],
}  # fmt: skip
EXTERNAL_ENV = {"EXTERNAL_POSTGRES_HOST": "db.invalid", "EXTERNAL_VALKEY_HOST": "cache.invalid", "EXTERNAL_VALKEY_KEY_PREFIX": "g:"}


def _render(files: list[str]) -> dict[str, Any]:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("no docker binary to render the Compose files")
    command = [docker, "compose", "--profile", "*", "--env-file", "config/validation.env"]
    for name in files:
        command += ["-f", name]
    env = {"PATH": str(Path(docker).parent), "HOME": str(Path.home()), **EXTERNAL_ENV}
    completed = subprocess.run(
        [*command, "config", "--format", "json"], cwd=ROOT, capture_output=True, text=True, check=False, timeout=60, env=env
    )  # fmt: skip
    assert completed.returncode == 0, completed.stderr
    rendered: dict[str, Any] = json.loads(completed.stdout)
    return rendered


def _secret_readers(rendered: dict[str, Any]) -> dict[str, dict[str, Any]]:
    file_secrets = {name for name, spec in rendered.get("secrets", {}).items() if "file" in spec}
    readers = {}
    for name, service in rendered["services"].items():
        mounted = {secret if isinstance(secret, str) else secret["source"] for secret in service.get("secrets", [])}
        if mounted & file_secrets:
            readers[name] = service
    return readers


@pytest.mark.parametrize("stack", sorted(STACKS))
def test_every_secret_reading_service_is_inventoried_and_can_read_the_secrets(stack: str) -> None:
    readers = _secret_readers(_render(STACKS[stack]))
    assert readers, "the production stack reads file secrets"
    for name, service in readers.items():
        if name in OWNER_SERVICES:
            assert service.get("user") == OWNER, f"{name} must run as the secrets' owner {OWNER}"
            continue
        assert name in INVENTORY, f"{name} reads file secrets but its image's user is not recorded in INVENTORY"
        recorded = INVENTORY[name]
        assert service["image"] == recorded["image"], f"{name}: the pin changed; re-read the image config User and update INVENTORY"
        if recorded["reads_as"] == "owner":
            assert service.get("user") == OWNER, f"{name} (image user {recorded['image_user'] or 'root'}) must run as {OWNER}"
        else:
            assert recorded["image_user"] == "", f"{name}: a non-root image user cannot open 0600 files owned by {OWNER}"
            assert "user" not in service, f"{name} starts as root to read its secret before dropping privileges"
            assert "ALL" not in service.get("cap_drop", []), f"{name} needs CAP_DAC_OVERRIDE to read the secret as root"
            assert "DAC_OVERRIDE" not in service.get("cap_add", [])


@pytest.mark.parametrize("stack", sorted(STACKS))
def test_no_secret_is_remapped_or_made_readable_to_others(stack: str) -> None:
    rendered = _render(STACKS[stack])
    for name, service in rendered["services"].items():
        for secret in service.get("secrets", []):
            if not isinstance(secret, str):
                for key in ("uid", "gid", "mode"):
                    assert key not in secret, f"{name}: secret {secret['source']} sets {key}; run as the owner instead"
        assert not {"DAC_OVERRIDE", "DAC_READ_SEARCH"} & set(service.get("cap_add", [])) or name == "cadvisor"
    script = (ROOT / "scripts" / "create-secrets.sh").read_text()
    assert "chmod 644" not in script
    assert "chmod 666" not in script
