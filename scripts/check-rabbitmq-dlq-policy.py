"""Validate the catalog-DLQ size-cap policy (gm-deployment-8mb.1).

The release-groups incident parked 3.5M messages in one unbounded classic DLQ.
RabbitMQ cannot change an existing classic queue's type (``PRECONDITION_FAILED``
at startup), and every catalog consumer (discogs-sql-loader, discogs-graph-enricher,
musicbrainz-sql-loader, musicbrainz-graph-enricher) deliberately keeps its DLQ
classic, so the cap is a broker policy matched by queue name -- never a queue
redeclare -- provisioned reproducibly with the stack.

The policy is declared AFTER the broker is healthy, by the one-shot
``rabbitmq-dlq-policy-init`` service (``scripts/rabbitmq-dlq-policy-init.sh``,
via ``rabbitmqadmin declare policy`` over the management HTTP API), not at
broker boot via ``load_definitions``: on a blank node, a boot-time definitions
import skips creating the default vhost/user that RABBITMQ_DEFAULT_USER/PASS/
VHOST would otherwise create ("Nuances of Boot-time Definition Import",
rabbitmq.com/docs/definitions), which would break every service's auth on a
fresh install.

This script is this repository's read-only cross-check of the four catalog
repositories' frozen naming contract (ADR 0005: ``{exchange_prefix}-{consumer}-{entity}``,
dead-lettered as ``{queue}.dlq``) -- it does not vendor or import those repositories, so
CATALOG_QUEUE_NAMING below is this check's single source of truth and must be updated by
hand if a catalog service's naming ever changes.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any


ROOT = Path(__file__).resolve().parents[1]

RABBITMQ_IMAGE = "rabbitmq:4-management@sha256:14f0bd24fd0314bac3c89bff2c736ad8ce4cd0ff800fdce0d784f55f595895e8"

POLICY_NAME = "catalog-dlq-cap"
POLICY_PATTERN = r"^groovemap-.*\.dlq$"

# exchange_prefix -> (consumers, entities). Source of truth: each catalog service's
# catalog_contract.py (EXCHANGE_PREFIX/CONSUMERS/ENTITY_TYPES) and queue_names.py
# (f"{queue_name(consumer, entity)}.dlq"), promoted byte-for-byte from
# discogs-ingestion / musicbrainz-ingestion per ADR 0005.
CATALOG_QUEUE_NAMING: Mapping[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "groovemap-discogs": (
        ("graphinator", "tableinator"),
        ("artists", "labels", "masters", "releases"),
    ),
    "groovemap-musicbrainz": (
        ("brainzgraphinator", "brainztableinator"),
        ("artists", "labels", "release-groups", "releases"),
    ),
}


def real_dlq_names() -> set[str]:
    """The exact DLQ names the four catalog services declare today."""
    return {
        f"{prefix}-{consumer}-{entity}.dlq"
        for prefix, (consumers, entities) in CATALOG_QUEUE_NAMING.items()
        for consumer in consumers
        for entity in entities
    }


def live_queue_names() -> set[str]:
    """The consumer queues the DLQ pattern must never match."""
    return {
        f"{prefix}-{consumer}-{entity}"
        for prefix, (consumers, entities) in CATALOG_QUEUE_NAMING.items()
        for consumer in consumers
        for entity in entities
    }


def check_pattern(pattern: str = POLICY_PATTERN) -> None:
    """The policy pattern must match every real DLQ and no live queue or DLX."""
    compiled = re.compile(pattern)
    unmatched = {name for name in real_dlq_names() if not compiled.fullmatch(name)}
    assert not unmatched, f"policy pattern {pattern!r} does not match real DLQ(s): {sorted(unmatched)}"

    live = live_queue_names()
    dlx_names = {f"{name}.dlx" for name in live}
    matched = {name for name in (live | dlx_names) if compiled.fullmatch(name)}
    assert not matched, f"policy pattern {pattern!r} matches non-DLQ name(s): {sorted(matched)}"


def check_init_script(script_text: str) -> None:
    """The init script must declare only the policy -- never a queue or exchange, and
    never a boot-time definitions import -- and must read the operator-configurable
    cap env vars with the documented defaults."""
    assert "exec rabbitmqadmin declare policy" in script_text, "must exec rabbitmqadmin declare policy as the script's final action"
    assert f"--name={POLICY_NAME}" in script_text
    assert POLICY_PATTERN in script_text
    assert "--apply-to=queues" in script_text

    for forbidden in ("rabbitmqadmin declare queue", "rabbitmqadmin declare exchange", "rabbitmqadmin import", "load_definitions ="):
        assert forbidden not in script_text, f"init script must never run {forbidden!r} -- it must only touch the policy"

    for env_var, default in (
        ("RABBITMQ_DLQ_MAX_LENGTH", "50000"),
        ("RABBITMQ_DLQ_MAX_LENGTH_BYTES", "536870912"),
        ("RABBITMQ_DLQ_OVERFLOW", "drop-head"),
    ):
        assert f"${{{env_var}:-{default}}}" in script_text, f"init script must default {env_var} to {default}"

    # The `exec rabbitmqadmin ...` call must be the script's final statement (nothing
    # after it in the file), so it replaces the shell as PID 1 rather than running
    # under a lingering wrapper process.
    tail = script_text[script_text.index("exec rabbitmqadmin declare policy") :].rstrip()
    assert tail.endswith('--definition="$definition"'), "nothing may follow the exec'd rabbitmqadmin invocation"


def render_definition(max_length: int = 50000, max_length_bytes: int = 536870912, overflow: str = "drop-head") -> dict[str, object]:
    """The policy definition body the init script's printf builds, for structural checks."""
    return {"max-length": max_length, "max-length-bytes": max_length_bytes, "overflow": overflow}


def check_compose(compose_text: str) -> None:
    """The broker must run stock (no boot-time definitions import), and the one-shot
    init service must depend on it being healthy, reuse its image (no new third-party
    image), and be wired with the operator-configurable cap env vars."""
    import yaml  # noqa: PLC0415

    compose: dict[str, Any] = yaml.safe_load(compose_text)
    services: dict[str, dict[str, Any]] = compose["services"]

    rabbitmq = services["rabbitmq"]
    assert "entrypoint" not in rabbitmq, "the base rabbitmq service must run the stock image entrypoint"
    for volume in rabbitmq.get("volumes", []):
        assert "definitions" not in volume and "rabbitmq.conf" not in volume, f"rabbitmq service must not load a boot-time definitions file: {volume}"

    assert "rabbitmq-dlq-policy-init" in services, "the DLQ-cap policy must be provisioned by a compose service"
    init_service = services["rabbitmq-dlq-policy-init"]

    assert init_service["image"] == RABBITMQ_IMAGE, "the init service must reuse rabbitmq's own pinned image"
    assert init_service.get("entrypoint") == ["/scripts/rabbitmq-dlq-policy-init.sh"]
    assert init_service.get("restart") == "no", "a one-shot provisioning step must not restart"

    depends_on = init_service.get("depends_on", {})
    assert depends_on.get("rabbitmq", {}).get("condition") == "service_healthy", (
        "the init service must wait for the broker to be healthy before declaring the policy"
    )

    volumes = init_service.get("volumes", [])
    assert "./scripts/rabbitmq-dlq-policy-init.sh:/scripts/rabbitmq-dlq-policy-init.sh:ro" in volumes

    environment = init_service.get("environment", {})
    for env_var in ("RABBITMQ_DLQ_MAX_LENGTH", "RABBITMQ_DLQ_MAX_LENGTH_BYTES", "RABBITMQ_DLQ_OVERFLOW"):
        assert env_var in environment, f"init service must pass {env_var} through to the script"
    for env_var in ("RABBITMQADMIN_TARGET_HOST", "RABBITMQADMIN_USERNAME", "RABBITMQADMIN_PASSWORD"):
        assert env_var in environment, f"init service must configure rabbitmqadmin's {env_var}"


def check_admin_guide(admin_guide_text: str) -> None:
    """The operator-facing DLQ table must stay in sync with the real DLQ names."""
    for name in real_dlq_names():
        assert name in admin_guide_text, f"docs/admin-guide.md is missing DLQ name {name}"
    assert POLICY_PATTERN in admin_guide_text, "docs/admin-guide.md must document the exact policy pattern"


def check_repository(root: Path = ROOT) -> None:
    check_pattern()
    check_init_script((root / "scripts" / "rabbitmq-dlq-policy-init.sh").read_text())
    check_compose((root / "docker-compose.yml").read_text())
    check_admin_guide((root / "docs" / "admin-guide.md").read_text())


if __name__ == "__main__":
    check_repository()
    print("RabbitMQ DLQ policy check passed.")
    sys.exit(0)
