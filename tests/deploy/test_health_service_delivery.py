"""Credential-free fixture tests; these never constitute actual image or live proof."""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
from typing import Any

import pytest
import yaml


def _load() -> Any:
    spec = importlib.util.spec_from_file_location(
        "health_service_delivery", Path(__file__).resolve().parents[2] / "scripts/health_service_delivery.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


delivery = _load()
PROOFS = delivery.PROOFS
SERVICES = delivery.SERVICES
digest = delivery.digest
expected_config = delivery.expected_config
render_fragment = delivery.render_fragment
validate_candidate = delivery.validate_candidate
validate_manifest = delivery.validate_manifest


@pytest.fixture
def manifest() -> dict[str, Any]:
    records: dict[str, Any] = {}
    for role, (owner, _) in SERVICES.items():
        image = f"ghcr.io/groovemap-music/{owner}@sha256:" + "a" * 64
        records[role] = {
            "service": f"legacy-{role}",
            "image": image,
            "baseline_configured_image": f"legacy/{role}:latest",
            "rollback_image": f"legacy/{role}@sha256:" + "b" * 64,
            "platform": "linux/amd64",
            "revision": "c" * 40,
            "version": "v0.2.0",
            "release_reference": "fixture-only",
            "attestation_reference": "fixture-only",
            "ancestry_reference": "fixture-only",
            "independent_review_reference": "fixture-only",
            "proofs": {name: {"image": image, "status": "passed", "reference": "fixture-only"} for name in PROOFS},
        }
        records[role]["proofs"]["logging"].update(environment="production", service=owner, events=1, missing_fields=0, wrong_fields=0)
    return {"format_version": 1, "baseline_reference": "fixture-only", "services": records}


@pytest.fixture
def fragment() -> bytes:
    return b"""# retained operator comment\r
services:\r
  legacy-explore:\r
    image: legacy/explore:latest # keep image comment\r
    environment:\r
      API_BASE_URL: http://api:8004\r
    healthcheck:\r
      test: [CMD, curl, -f, http://localhost:8007/health]\r
      interval: 30s\r
    volumes: [existing_logs:/logs]\r
  legacy-insights:\r
    image: legacy/insights:latest\r
    environment:\r
      ENVIRONMENT: development\r
      POSTGRES_PASSWORD_FILE: /run/secrets/postgres_password\r
    healthcheck:\r
      test: [CMD, curl, -f, http://localhost:8009/health]\r
    networks: [existing_private]\r
  unrelated:\r
    image: preserved@sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff\r
    command: [retain, operator, changes]\r
"""


def test_forward_preserves_unrelated_bytes_and_resolved_contracts(fragment: bytes, manifest: dict[str, Any]) -> None:
    candidate, receipt = render_fragment(fragment, digest(fragment), manifest)
    assert b"# retained operator comment\r\n" in candidate
    assert b"# keep image comment\r\n" in candidate
    assert candidate.split(b"  unrelated:", 1)[1] == fragment.split(b"  unrelated:", 1)[1]
    baseline = yaml.safe_load(fragment)
    validate_candidate(baseline, yaml.safe_load(candidate), manifest)
    assert receipt["production_applied"] is False
    assert receipt["compose_equivalence_verified"] is False
    assert b"STARTUP_DELAY" not in candidate
    assert b"POSTGRES_PASSWORD_FILE: /run/secrets/postgres_password" in candidate
    assert b"existing_logs:/logs" in candidate


def test_rollback_restores_original_contract_with_immutable_images(fragment: bytes, manifest: dict[str, Any]) -> None:
    candidate, _ = render_fragment(fragment, digest(fragment), manifest, rollback=True)
    validate_candidate(yaml.safe_load(fragment), yaml.safe_load(candidate), manifest, rollback=True)
    assert yaml.safe_load(candidate)["services"]["legacy-explore"]["environment"] == yaml.safe_load(fragment)["services"]["legacy-explore"]["environment"]
    assert b"ENVIRONMENT: development" in candidate
    assert b"[CMD, curl" in candidate


@pytest.mark.parametrize("mutation", ["mutable", "foreign", "missing-proof", "wrong-image", "wrong-logging", "wrong-platform"])
def test_rejects_unaccepted_image_evidence(manifest: dict[str, Any], mutation: str) -> None:
    record = manifest["services"]["explore"]
    if mutation == "mutable":
        record["image"] = "ghcr.io/groovemap-music/graph-explorer:v0.2.2"
    elif mutation == "foreign":
        record["image"] = record["image"].replace("groovemap-music", "foreign")
    elif mutation == "missing-proof":
        del record["proofs"]["model_data"]
    elif mutation == "wrong-image":
        record["proofs"]["health"]["image"] = "other"
    elif mutation == "wrong-logging":
        record["proofs"]["logging"]["missing_fields"] = 1
    else:
        record["platform"] = "linux/arm64"
    with pytest.raises(ValueError):
        validate_manifest(manifest)


def test_drift_rejected_before_any_patch(fragment: bytes, manifest: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="source hash drift"):
        render_fragment(fragment + b"# owner edit\n", digest(fragment), manifest)


def test_rejects_ambiguous_fields_and_legacy_command(fragment: bytes, manifest: dict[str, Any]) -> None:
    for extra in (b"    image: unexpected\r\n", b"    command: [legacy, startup]\r\n"):
        changed = fragment.replace(b"  legacy-explore:\r\n", b"  legacy-explore:\r\n" + extra)
        with pytest.raises(ValueError):
            render_fragment(changed, digest(changed), manifest)


def test_rejects_unrelated_resolved_change(fragment: bytes, manifest: dict[str, Any]) -> None:
    baseline = yaml.safe_load(fragment)
    candidate = expected_config(baseline, manifest)
    candidate["services"]["legacy-insights"]["networks"] = ["different"]
    with pytest.raises(ValueError, match="unapproved resolved"):
        validate_candidate(baseline, candidate, manifest)
    assert baseline == yaml.safe_load(fragment)
    altered = copy.deepcopy(manifest)
    altered["services"]["insights"]["service"] = "legacy-explore"
    with pytest.raises(ValueError, match="duplicate"):
        validate_manifest(altered)


@pytest.mark.parametrize("delay", [0, 10, "0", "10"])
@pytest.mark.parametrize("rollback", [False, True])
def test_resolved_startup_delay_fails_closed(
    fragment: bytes, manifest: dict[str, Any], delay: int | str, rollback: bool
) -> None:
    baseline = yaml.safe_load(fragment)
    baseline["services"]["legacy-insights"]["environment"]["STARTUP_DELAY"] = delay
    original = copy.deepcopy(baseline)
    with pytest.raises(ValueError, match="startup delay requires independently reviewed"):
        expected_config(baseline, manifest, rollback=rollback)
    assert baseline == original


@pytest.mark.parametrize("delay", [b"0", b"10"])
@pytest.mark.parametrize("rollback", [False, True])
def test_direct_fragment_startup_delay_fails_closed(
    fragment: bytes, manifest: dict[str, Any], delay: bytes, rollback: bool
) -> None:
    changed = fragment.replace(
        b"      ENVIRONMENT: development\r\n",
        b"      ENVIRONMENT: development\r\n      STARTUP_DELAY: " + delay + b"\r\n",
    )
    with pytest.raises(ValueError, match="startup delay requires independently reviewed"):
        render_fragment(changed, digest(changed), manifest, rollback=rollback)
    assert b"STARTUP_DELAY: " + delay + b"\r\n" in changed


def test_forward_preserves_all_other_environment_values(fragment: bytes, manifest: dict[str, Any]) -> None:
    baseline = yaml.safe_load(fragment)
    candidate = expected_config(baseline, manifest)
    for record in manifest["services"].values():
        name = record["service"]
        wanted = dict(baseline["services"][name]["environment"], ENVIRONMENT="production")
        assert candidate["services"][name]["environment"] == wanted
    assert baseline == yaml.safe_load(fragment)
