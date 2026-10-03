"""Offline tests for disposable smoke preparation, never actual image acceptance."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import lzma
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import pytest


def _load() -> Any:
    spec = importlib.util.spec_from_file_location(
        "extractor_smoke_prepare", Path(__file__).resolve().parents[2] / "scripts/extractor_smoke_prepare.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


prepare = _load()


def context() -> dict[str, Any]:
    return {
        "bead": "gm-deployment-eeo",
        "actor": "dev/gm-extractor-log-delivery",
        "context": {"architecture": "linux/amd64", "owned_root": "/owned/gm-eeo-smoke-012345abcdef", "exclusive_lock": "/owned/host-resource.lock"},
        "budgets": {
            "providers_sequential": True,
            "no_swap_growth": True,
            "aggregate_cpu_quota_max": 8,
            "aggregate_memory_and_swap_max_gib": 6,
            "owned_disk_max_gib": 8,
            "overall_timeout_seconds": 900,
        },
        "approved_auxiliary_pins": {role: f"registry.example/{role}@sha256:{'a' * 64}" for role in ["rabbitmq", "postgres", "neo4j"]},
    }


def images() -> dict[str, Any]:
    return {
        "producers": {
            provider: {
                "image": f"ghcr.io/groovemap-music/{provider}-ingestion@sha256:{'b' * 64}",
                "revision": "c" * 40,
                "platform": "linux/amd64",
                "verified_provenance_reference": "synthetic-structural-reference-not-approval",
            }
            for provider in ["discogs", "musicbrainz"]
        },
        "consumers": {
            role: {"image": f"registry.example/{role}@sha256:{digest}", "revision": prepare.LEGACY_REVISION}
            for role, digest in prepare.LEGACY_DIGESTS.items()
        },
        "prefixes": {provider: f"synthetic-{provider}" for provider in ["discogs", "musicbrainz"]},
    }


def test_aggregate_bounds_count_all_services_and_extra_volume_memory() -> None:
    proof = prepare.resource_proof()
    assert proof["conservative_total_cpu"] == 7.75
    assert proof["conservative_total_memory_mib"] == 6052
    assert proof["owned_storage_mib_max"] < 8192
    assert proof["runtime_configuration_verified"] is False


@pytest.mark.parametrize("provider", ["discogs", "musicbrainz"])
def test_lane_has_no_external_network_ports_or_production_mounts(provider: str) -> None:
    model = prepare.compose_model(context(), images(), provider)
    assert model["networks"]["isolated"]["internal"] is True
    assert set(model["services"]) == set(prepare.LIMITS)
    for service in model["services"].values():
        assert "ports" not in service and "build" not in service
        assert service["read_only"] is True
        assert service["mem_limit"] == service["memswap_limit"]
        assert service["labels"]["beadhive.probe.task"] == "gm-deployment-eeo"
        for mount in service["volumes"]:
            if mount["type"] == "bind":
                assert mount["source"] == "/owned/gm-eeo-smoke-012345abcdef/bundle"
                assert mount["read_only"] is True
    for volume in model["volumes"].values():
        assert volume["driver_opts"]["type"] == "tmpfs"
        assert "size=" in volume["driver_opts"]["o"]
    for role in ["producer", "fixture-http"]:
        assert any(mount.get("source") == "producer" for mount in model["services"][role]["volumes"])


def test_http_uses_exact_legacy_python_and_producer_uses_local_endpoint() -> None:
    model = prepare.compose_model(context(), images(), "musicbrainz")
    assert model["services"]["fixture-http"]["image"] == images()["consumers"]["musicbrainz-sql"]["image"]
    assert model["services"]["fixture-http"]["entrypoint"] == ["/app/.venv/bin/python"]
    assert model["services"]["producer"]["environment"]["MUSICBRAINZ_DUMP_URL"] == "http://fixture-http:8000/"


@pytest.mark.parametrize(
    "field,value", [("platform", "linux/arm64"), ("image", "mutable:latest"), ("verified_provenance_reference", ""), ("revision", "short")]
)
def test_missing_or_invalid_producer_metadata_fails_closed(field: str, value: str) -> None:
    metadata = images()
    metadata["producers"]["discogs"][field] = value
    with pytest.raises(ValueError):
        prepare.validate_inputs(context(), metadata)


def test_unrelated_legacy_digest_and_wrong_root_or_lock_fail_closed() -> None:
    metadata = images()
    metadata["consumers"]["discogs-sql"]["image"] = f"registry.example/other@sha256:{'a' * 64}"
    with pytest.raises(ValueError):
        prepare.validate_inputs(context(), metadata)
    for key, bad in [("owned_root", "/"), ("exclusive_lock", "/unrelated.lock")]:
        config = context()
        config["context"][key] = bad
        with pytest.raises(ValueError):
            prepare.validate_inputs(config, images())


def test_resource_budget_is_not_silently_relaxed() -> None:
    config = context()
    config["budgets"]["aggregate_memory_and_swap_max_gib"] = 12
    with pytest.raises(ValueError):
        prepare.validate_inputs(config, images())


def test_mb_archives_match_checksum_index_paths_and_synthetic_identities() -> None:
    files = prepare.fixture_bytes()
    assert sum(map(len, files.values())) < 1024**2
    sums = files[f"http/{prepare.MB_VERSION}/SHA256SUMS"].decode().splitlines()
    assert len(sums) == 4
    for entry in sums:
        checksum, name = entry.split()
        compressed = files[f"http/{prepare.MB_VERSION}/{name}"]
        assert hashlib.sha256(compressed).hexdigest() == checksum
        with tarfile.open(fileobj=io.BytesIO(lzma.decompress(compressed))) as archive:
            assert len(archive.getmembers()) == 1
            member = archive.getmembers()[0]
            assert member.name == "mbdump/" + name.removesuffix(".tar.xz")
            stream = archive.extractfile(member)
            assert stream is not None
            records = [json.loads(line) for line in stream.read().decode().splitlines()]
            assert len(records) == (2 if name.startswith("artist.") else 1)
            assert all(record["id"] in {*prepare.MB_IDS.values(), "f0f0f0f0-0000-4000-8000-000000000005"} for record in records)


def test_discogs_manifest_is_checksum_bound_single_file_without_claimed_output() -> None:
    files = prepare.fixture_bytes()
    for entity in ["artists", "labels", "masters", "releases"]:
        manifest = json.loads(files[f"discogs/manifest-{entity}.json"])
        assert manifest["scope"] == "single_file"
        body = files["discogs/" + manifest["input"]["path"]]
        assert hashlib.sha256(body).hexdigest() == manifest["input"]["sha256"]
        assert "expected_event_stream" not in manifest
    assert files == prepare.fixture_bytes()


def test_missing_prefix_mapping_is_rejected() -> None:
    metadata = copy.deepcopy(images())
    metadata["prefixes"].pop("discogs")
    with pytest.raises(ValueError):
        prepare.validate_inputs(context(), metadata)


@pytest.mark.parametrize("provider", ["discogs", "musicbrainz"])
def test_consumers_and_producer_use_same_explicit_verified_prefix(provider: str) -> None:
    metadata = images()
    model = prepare.compose_model(context(), metadata, provider)
    for role in ["producer", "observer", "sql-consumer", "graph-consumer"]:
        assert model["services"][role]["environment"][f"{provider.upper()}_EXCHANGE_PREFIX"] == metadata["prefixes"][provider]


def test_auxiliaries_use_official_nonroot_users_with_bounded_writable_paths() -> None:
    model = prepare.compose_model(context(), images(), "discogs")
    broker, graph = model["services"]["broker"], model["services"]["neo4j"]
    assert broker["user"] == "999:999"
    assert broker["environment"]["RABBITMQ_SERVER_ADDITIONAL_ERL_ARGS"] == "+S 1:1 +SDcpu 1 +SDio 1"
    assert broker["environment"]["RABBITMQ_CTL_ERL_ARGS"] == "+S 1:1 +SDcpu 1 +SDio 1"
    assert graph["user"] == "7474:7474"
    for service in [broker, graph]:
        assert service["read_only"] is True and service["cap_drop"] == ["ALL"]
        assert "cap_add" not in service
    assert any(v["target"] == "/var/lib/neo4j/conf" and v["source"] == "neo4j-conf" for v in graph["volumes"])
    assert not any(v["target"] == "/conf" for v in graph["volumes"])
    assert "/var/lib/neo4j/run:rw,noexec,nosuid,size=4m,uid=7474,gid=7474" in graph["tmpfs"]


@pytest.mark.parametrize("auth,expected", [("neo4j/smoke-@:/-invalid", 1), ("neo4j/smoke-@:-valid-long-password", 0)])
def test_pinned_neo4j_auth_bash_grammar(auth: str, expected: int) -> None:
    # Exact authentication grammar from the pinned auxiliary entrypoint inspection.
    code = '[[ "$1" =~ ^([^/]+)\\/([^/]+)/?([tT][rR][uU][eE])?$ ]]'
    result = subprocess.run(["/bin/bash", "-c", code, "auth-grammar", auth], capture_output=True, check=False)
    assert result.returncode == expected


def test_generated_password_is_neo4j_valid_and_still_requires_uri_escaping() -> None:
    secret = prepare.synthetic_password()
    assert len(secret) >= 8 and "/" not in secret and "@" in secret and ":" in secret
    code = '[[ "$1" =~ ^([^/]+)\\/([^/]+)/?([tT][rR][uU][eE])?$ ]]'
    result = subprocess.run(["/bin/bash", "-c", code, "auth-grammar", "neo4j/" + secret], capture_output=True, check=False)
    assert result.returncode == 0
    uri = "amqp://synthetic-smoke:" + quote(secret, safe="") + "@broker:5672/%2F"
    assert urlsplit(uri).hostname == "broker" and unquote(urlsplit(uri).password or "") == secret


def test_jna_executable_path_is_dedicated_bounded_and_nonroot_only() -> None:
    model = prepare.compose_model(context(), images(), "discogs")
    graph = model["services"]["neo4j"]
    assert graph["environment"]["JAVA_TOOL_OPTIONS"] == "-Djna.tmpdir=/var/lib/neo4j/native-tmp"
    assert "NEO4J_server_jvm_additional" not in graph["environment"]
    assert graph["read_only"] is True and graph["cap_drop"] == ["ALL"]
    executable = [mount for mount in graph["tmpfs"] if "exec" in mount.split(":", 1)[1].split(",")]
    assert executable == ["/var/lib/neo4j/native-tmp:rw,exec,nosuid,nodev,size=4m,uid=7474,gid=7474,mode=700"]
    assert "/tmp:rw,noexec,nosuid,size=8m" in graph["tmpfs"]  # noqa: S108 -- asserted container-only bounded mount
    assert all(
        not any("exec" in mount.split(":", 1)[1].split(",") for mount in service["tmpfs"])
        for role, service in model["services"].items()
        if role != "neo4j"
    )
    assert prepare.resource_proof()["conservative_total_memory_mib"] == 6052


def test_empty_graph_auxiliary_serves_authenticated_bolt_only() -> None:
    graph = prepare.compose_model(context(), images(), "discogs")["services"]["neo4j"]
    assert graph["environment"]["NEO4J_server_http_enabled"] == "false"
    assert graph["environment"]["NEO4J_server_https_enabled"] == "false"
    assert graph["environment"]["NEO4J_server_bolt_enabled"] == "true"
    assert graph["environment"]["NEO4J_AUTH_FILE"] == "/fixtures/synthetic-neo4j-auth"
    assert graph["read_only"] is True and graph["mem_limit"] == "1792m"
    assert graph["cap_drop"] == ["ALL"] and "cap_add" not in graph
    assert prepare.resource_proof()["conservative_total_memory_mib"] == 6052
