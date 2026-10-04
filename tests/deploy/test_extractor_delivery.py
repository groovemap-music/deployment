"""Synthetic delivery evidence never attests published images or production readiness."""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from typing import TYPE_CHECKING, Any

import pytest


if TYPE_CHECKING:
    from pathlib import Path


def _load_delivery() -> Any:
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("extractor_delivery", Path(__file__).resolve().parents[2] / "scripts/extractor_delivery.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


delivery = _load_delivery()


def manifest() -> dict[str, Any]:
    providers = {}
    for provider, (_, revision) in delivery.PROVIDERS.items():
        image = f"ghcr.io/groovemap-music/{provider}-ingestion@sha256:{'a' * 64}"
        proofs: dict[str, dict[str, Any]] = {
            name: {"image": image, "status": "passed", "reference": "synthetic-test-only"} for name in delivery.PROOFS
        }
        proofs["logging"].update({"root_environment": "production", "json_events": 1, "missing_environment": 0, "wrong_environment": 0})
        providers[provider] = {
            "service": f"existing-extractor-{provider}",
            "baseline_image": f"registry.example/legacy-extractor@sha256:{'c' * 64}",
            "baseline_configured_image": "registry.example/legacy-extractor:latest",
            "baseline_runtime": {
                "configured_image": "registry.example/legacy-extractor:latest",
                "repo_digest": f"registry.example/legacy-extractor@sha256:{'c' * 64}",
                "image_id": f"sha256:{'d' * 64}",
                "reference": "synthetic-test-only",
            },
            "image": image,
            "version": "v1.2.3",
            "image_revision": "b" * 40,
            "reviewed_main": revision,
            "platform": "linux/amd64",
            "release_review_reference": "synthetic-test-only",
            "source_ancestry": {
                "image": image,
                "ancestor": revision,
                "descendant": "b" * 40,
                "command": ["git", "merge-base", "--is-ancestor", revision, "b" * 40],
                "exit": 0,
                "reference": "synthetic-test-only",
            },
            "exchange_prefix": f"existing-{provider}",
            "proofs": proofs,
        }
    return {"format_version": 1, "providers": providers, "baseline_reference": "synthetic-test-only", "rollback_reference": "synthetic-test-only"}


def baseline() -> dict[str, Any]:
    services: dict[str, Any] = {"unrelated": {"image": "unrelated:unchanged", "environment": {"OTHER": "unchanged"}}}
    for provider in delivery.PROVIDERS:
        service = manifest()["providers"][provider]["service"]
        services[service] = {
            "image": "registry.example/legacy-extractor:latest",
            "command": ["--source", provider],
            "hostname": f"extractor-{provider}",
            "environment": {
                "ENVIRONMENT": "production",
                "RABBITMQ_PASSWORD_FILE": "/run/secrets/rabbitmq_password",
                "RABBITMQ_USERNAME": "test-only",
                "STARTUP_DELAY": "30",
                "PERIODIC_CHECK_DAYS": "5",
            },
            "volumes": [{"type": "volume", "source": f"{provider}-original-data", "target": f"/{provider}-data", "read_only": False}],
            "networks": {"backend": {"aliases": [f"extractor-{provider}"]}},
            "secrets": [{"source": "rabbitmq_password", "target": "rabbitmq_password"}],
            "healthcheck": {"test": ["CMD", "curl", "-f", "http://localhost:8000/health"]},
            "user": "1000:1000",
        }
    services[manifest()["providers"]["musicbrainz"]["service"]]["environment"].update(
        {"AMQP_EXCHANGE_PREFIX": "ignored", "DISCOGS_HEALTH_URL": "obsolete"}
    )
    return {
        "name": "test-only",
        "services": services,
        "volumes": {"test-volume": {}},
        "networks": {"backend": {}},
        "secrets": {"rabbitmq_password": {}},
    }


def test_two_service_adaptation_preserves_every_other_contract() -> None:
    old = baseline()
    original = copy.deepcopy(old)
    result = delivery.expected_config(old, manifest())
    delivery.validate_candidate(old, result, manifest())
    assert old == original
    assert result["services"]["unrelated"] == old["services"]["unrelated"]
    for provider, (prefix, _) in delivery.PROVIDERS.items():
        service = manifest()["providers"][provider]["service"]
        changed = result["services"][service]
        assert changed["command"] is None
        assert changed["environment"][prefix] == f"existing-{provider}"
        assert changed["environment"]["RABBITMQ_PASSWORD_FILE"] == old["services"][service]["environment"]["RABBITMQ_PASSWORD_FILE"]
        assert "STARTUP_DELAY" not in changed["environment"]
        for key in ("volumes", "networks", "secrets", "healthcheck", "hostname", "user"):
            assert changed[key] == old["services"][service][key]
    assert "AMQP_EXCHANGE_PREFIX" not in result["services"][manifest()["providers"]["musicbrainz"]["service"]]["environment"]
    assert "DISCOGS_HEALTH_URL" not in result["services"][manifest()["providers"]["musicbrainz"]["service"]]["environment"]


@pytest.mark.parametrize("contract", ["volumes", "networks", "secrets", "healthcheck", "hostname", "user"])
def test_candidate_rejects_drift_in_preserved_contracts(contract: str) -> None:
    old = baseline()
    candidate = delivery.expected_config(old, manifest())
    candidate["services"][manifest()["providers"]["discogs"]["service"]][contract] = "drift"
    with pytest.raises(ValueError, match="unapproved contract"):
        delivery.validate_candidate(old, candidate, manifest())


def test_unrelated_service_and_topology_changes_are_rejected() -> None:
    old = baseline()
    candidate = delivery.expected_config(old, manifest())
    candidate["services"]["unrelated"]["image"] = "changed"
    with pytest.raises(ValueError):
        delivery.validate_candidate(old, candidate, manifest())
    candidate = delivery.expected_config(old, manifest())
    candidate["networks"]["new"] = {}
    with pytest.raises(ValueError):
        delivery.validate_candidate(old, candidate, manifest())


@pytest.mark.parametrize(
    ("key", "bad"),
    [
        ("image", "ghcr.io/groovemap-music/discogs-ingestion:latest"),
        ("image", f"ghcr.io/groovemap-music/musicbrainz-ingestion@sha256:{'a' * 64}"),
        ("version", "main"),
        ("image_revision", "short"),
        ("reviewed_main", "b" * 40),
        ("platform", "linux/arm64"),
        ("release_review_reference", ""),
        ("exchange_prefix", ""),
    ],
)
def test_unreviewed_or_wrong_release_is_rejected(key: str, bad: str) -> None:
    value = manifest()
    value["providers"]["discogs"][key] = bad
    with pytest.raises(ValueError):
        delivery.validate_manifest(value)


@pytest.mark.parametrize("proof", sorted(delivery.PROOFS))
def test_every_contract_needs_passed_evidence_for_the_exact_image(proof: str) -> None:
    value = manifest()
    value["providers"]["discogs"]["proofs"][proof]["status"] = "pending"
    with pytest.raises(ValueError):
        delivery.validate_manifest(value)
    value = manifest()
    value["providers"]["discogs"]["proofs"][proof]["image"] = "old-image"
    with pytest.raises(ValueError):
        delivery.validate_manifest(value)


@pytest.mark.parametrize("key,bad", [("root_environment", "development"), ("json_events", 0)])
def test_no_false_logging_attestation(key: str, bad: Any) -> None:
    value = manifest()
    value["providers"]["discogs"]["proofs"]["logging"][key] = bad
    with pytest.raises(ValueError):
        delivery.validate_manifest(value)


def test_compose_override_resets_legacy_commands_and_environment() -> None:
    rendered = delivery.render_overlay(manifest())
    assert rendered.count("command: !reset []") == 2
    assert "--source" not in rendered
    assert "AMQP_EXCHANGE_PREFIX: !reset 'null'" in rendered
    assert "DISCOGS_HEALTH_URL: !reset 'null'" in rendered
    assert "volumes:" not in rendered and "networks:" not in rendered and "secrets:" not in rendered


def test_missing_evidence_and_baseline_are_rejected() -> None:
    value = manifest()
    value["providers"]["discogs"]["proofs"].pop("resume")
    with pytest.raises(ValueError):
        delivery.validate_manifest(value)
    value = manifest()
    value["baseline_reference"] = ""
    with pytest.raises(ValueError):
        delivery.validate_manifest(value)
    with pytest.raises(ValueError, match="missing baseline"):
        delivery.expected_config({"services": {}}, manifest())


def test_cli_prepares_but_never_applies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    release = tmp_path / "release.json"
    config = tmp_path / "private-baseline.json"
    output = tmp_path / "review.yml"
    release.write_text(json.dumps(manifest()))
    config.write_text(json.dumps(baseline()))
    monkeypatch.setattr("sys.argv", ["extractor_delivery", "--manifest", str(release), "--baseline", str(config), "--output", str(output)])
    assert delivery.main() == 0
    assert output.exists()
    assert "no deployment performed" in capsys.readouterr().out
    assert "test-only" not in output.read_text()


def test_cli_does_not_echo_invalid_private_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    private = tmp_path / "private.json"
    private.write_text("PRIVATE_SENTINEL{")
    monkeypatch.setattr("sys.argv", ["extractor_delivery", "--manifest", str(private), "--baseline", str(private)])
    assert delivery.main() == 2
    assert "PRIVATE_SENTINEL" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "key,bad",
    [("image", "other"), ("ancestor", "b" * 40), ("descendant", "c" * 40), ("command", []), ("exit", 1), ("exit", False), ("reference", "")],
)
def test_ancestry_evidence_is_bound_to_exact_source_and_image(key: str, bad: Any) -> None:
    value = manifest()
    value["providers"]["discogs"]["source_ancestry"][key] = bad
    with pytest.raises(ValueError):
        delivery.validate_manifest(value)


def test_real_compose_resolves_reset_overlay_without_contract_drift(tmp_path: Path) -> None:
    import yaml

    original = baseline()
    # This is a synthetic Compose model, not live mounts, usernames, volumes or secrets.
    original["secrets"]["rabbitmq_password"] = {"file": str(tmp_path / "test-only-secret")}
    (tmp_path / "test-only-secret").write_text("synthetic-test-only")
    for provider in delivery.PROVIDERS:
        original["volumes"][f"{provider}-original-data"] = {}
    base = tmp_path / "base.yml"
    override = tmp_path / "override.yml"
    base.write_text(yaml.safe_dump(original))
    override.write_text(delivery.render_overlay(manifest()))
    command = ["docker", "compose", "--project-name", "extractor-delivery-offline-test", "-f", str(base)]
    before = subprocess.run([*command, "config", "--format", "json"], capture_output=True, text=True, check=True)
    after = subprocess.run([*command, "-f", str(override), "config", "--format", "json"], capture_output=True, text=True, check=True)
    delivery.validate_candidate(json.loads(before.stdout), json.loads(after.stdout), manifest())


def test_actual_json_log_parser_reports_counts_without_messages() -> None:
    event = {"environment": "production", "level": "ERROR", "fields": {"message": 'quoted "message"\nline', "environment": "nested"}}
    report = delivery.validate_json_log("startup banner\n" + json.dumps(event), "production")
    assert report == {"json_events": 1, "root_environment": "production", "missing_environment": 0, "wrong_environment": 0}
    assert "message" not in json.dumps(report)


@pytest.mark.parametrize(
    "text",
    [
        "banner only",
        "{broken",
        '{"environment":"development","level":"INFO","fields":{}}',
        '{"level":"INFO","fields":{}}',
        '{"environment":"production"}',
    ],
)
def test_json_log_parser_rejects_empty_malformed_missing_or_wrong_context(text: str) -> None:
    with pytest.raises(ValueError):
        delivery.validate_json_log(text, "production")


def test_unfilled_checked_in_example_cannot_pass() -> None:
    from pathlib import Path

    with pytest.raises(ValueError):
        delivery.validate_manifest(json.loads((Path(__file__).resolve().parents[2] / "config/extractor-delivery.example.json").read_text()))


def test_isolated_probe_has_no_host_mount_network_or_credentials_and_cleans_owned_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    image = manifest()["providers"]["discogs"]["image"]
    revision = "b" * 40
    calls: list[list[str]] = []
    scenarios = iter(["help", "legacy_selector", "production", "default"])
    current = ""
    owner = ""

    def fake_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal current, owner
        calls.append(command)
        arguments = command[3:]
        if arguments[:2] == ["image", "inspect"]:
            metadata = {
                "Architecture": "amd64",
                "Os": "linux",
                "Config": {"Labels": {"org.opencontainers.image.revision": revision}, "Entrypoint": ["discogs-ingestion"], "Cmd": None},
            }
            return subprocess.CompletedProcess(command, 0, json.dumps([metadata]), "")
        if arguments[0] == "create":
            current = next(scenarios)
            owner = arguments[arguments.index("--label") + 1].split("=", 1)[1]
            return subprocess.CompletedProcess(command, 0, "c" * 64 + "\n", "")
        if arguments[:2] == ["container", "inspect"]:
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(
                    [
                        {
                            "Id": "c" * 64,
                            "Config": {"Image": image, "Labels": {"beadhive.probe.owner": owner, "beadhive.probe.task": "gm-deployment-eeo"}},
                        }
                    ]
                ),
                "",
            )
        if arguments[0] == "start":
            text = (
                "discogs-ingestion usage"
                if current == "help"
                else json.dumps(
                    {
                        "environment": "development" if current == "default" else "production",
                        "level": "ERROR",
                        "fields": {"message": "missing synthetic secret"},
                    }
                )
            )
            return subprocess.CompletedProcess(command, 0, text, "")
        if arguments[0] == "inspect":
            return subprocess.CompletedProcess(command, 0, str({"help": 0, "legacy_selector": 2, "production": 1, "default": 1}[current]), "")
        assert arguments == ["rm", "--force", "c" * 64]
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(delivery.subprocess, "run", fake_run)
    report = delivery.isolated_startup_probe(image, "discogs", "test-only", revision)
    assert report["scenarios"]["production"]["root_environment"] == "production"
    assert report["scenarios"]["default"]["root_environment"] == "development"
    creates = [command for command in calls if command[3] == "create"]
    assert len(creates) == 4
    for command in creates:
        assert command[command.index("--network") + 1] == "none"
        assert "--read-only" in command
        assert command[command.index("--cpus") + 1] == "1"
        assert command[command.index("--memory") + 1] == command[command.index("--memory-swap") + 1] == "256m"
        assert command[command.index("--pids-limit") + 1] == "64"
        assert command[command.index("--tmpfs") + 1] == "/tmp:rw,noexec,nosuid,size=16m"  # noqa: S108 -- container-only bounded tmpfs
        assert "--volume" not in command and "--mount" not in command and "--env-file" not in command
        assert "RABBITMQ_PASSWORD_FILE=/run/secrets/missing-probe-only" in command
    assert sum(command[3] == "rm" for command in calls) == 4
    assert not any("pull" in command or "push" in command for command in calls)


def test_isolated_probe_rejects_wrong_inspected_revision_before_create(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps([{"Architecture": "amd64", "Os": "linux", "Config": {"Labels": {"org.opencontainers.image.revision": "c" * 40}}}]),
            "",
        )

    monkeypatch.setattr(delivery.subprocess, "run", fake_run)
    with pytest.raises(ValueError, match="revision mismatch"):
        delivery.isolated_startup_probe(manifest()["providers"]["discogs"]["image"], "discogs", "test-only", "b" * 40)
    assert len(calls) == 1


@pytest.mark.parametrize("unowned", [False, True])
def test_create_timeout_uses_named_ownership_guard_before_cleanup(monkeypatch: pytest.MonkeyPatch, unowned: bool) -> None:
    image = manifest()["providers"]["discogs"]["image"]
    revision = "b" * 40
    calls: list[list[str]] = []
    owner = ""

    def fake_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal owner
        calls.append(command)
        arguments = command[3:]
        if arguments[:2] == ["image", "inspect"]:
            metadata = {
                "Architecture": "amd64",
                "Os": "linux",
                "Config": {"Labels": {"org.opencontainers.image.revision": revision}, "Entrypoint": ["discogs-ingestion"], "Cmd": None},
            }
            return subprocess.CompletedProcess(command, 0, json.dumps([metadata]), "")
        if arguments[0] == "create":
            owner = arguments[arguments.index("--label") + 1].split("=", 1)[1]
            assert arguments[arguments.index("--name") + 1] == f"gm-extractor-log-probe-{owner}"
            raise subprocess.TimeoutExpired(command, 30)
        if arguments[:2] == ["container", "inspect"]:
            assert arguments[2] == f"gm-extractor-log-probe-{owner}"
            labels = {"beadhive.probe.owner": "other" if unowned else owner, "beadhive.probe.task": "gm-deployment-eeo"}
            return subprocess.CompletedProcess(command, 0, json.dumps([{"Id": "c" * 64, "Config": {"Image": image, "Labels": labels}}]), "")
        assert arguments == ["rm", "--force", "c" * 64]
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(delivery.subprocess, "run", fake_run)
    with pytest.raises(ValueError if unowned else subprocess.TimeoutExpired):
        delivery.isolated_startup_probe(image, "discogs", "test-only", revision)
    assert sum(command[3] == "rm" for command in calls) == (0 if unowned else 1)


@pytest.mark.parametrize("name", [None, "", "service with spaces", "../other", 42])
def test_invalid_private_service_mapping_is_rejected(name: Any) -> None:
    evidence = manifest()
    evidence["providers"]["discogs"]["service"] = name
    with pytest.raises(ValueError, match="baseline service"):
        delivery.validate_manifest(evidence)


def test_duplicate_or_missing_baseline_service_mapping_is_rejected() -> None:
    evidence = manifest()
    evidence["providers"]["musicbrainz"]["service"] = evidence["providers"]["discogs"]["service"]
    with pytest.raises(ValueError, match="distinct"):
        delivery.validate_manifest(evidence)
    evidence = manifest()
    evidence["providers"]["discogs"]["service"] = "not-in-baseline"
    with pytest.raises(ValueError, match="missing baseline"):
        delivery.expected_config(baseline(), evidence)


@pytest.mark.parametrize("contract", ["image", "command"])
def test_existing_service_mapping_requires_verified_original_image_and_selector(contract: str) -> None:
    original = baseline()
    name = manifest()["providers"]["discogs"]["service"]
    original["services"][name][contract] = "different-contract"
    with pytest.raises(ValueError, match="original baseline"):
        delivery.expected_config(original, manifest())


def test_mapping_to_unrelated_existing_service_is_rejected() -> None:
    evidence = manifest()
    evidence["providers"]["discogs"]["service"] = "unrelated"
    with pytest.raises(ValueError, match="original baseline"):
        delivery.expected_config(baseline(), evidence)


@pytest.mark.parametrize(
    "key,bad", [("configured_image", "other:latest"), ("repo_digest", "other@sha256:" + "a" * 64), ("image_id", "invalid"), ("reference", "")]
)
def test_original_mutable_reference_requires_bound_inspected_runtime_evidence(key: str, bad: str) -> None:
    record = manifest()
    record["providers"]["discogs"]["baseline_runtime"][key] = bad
    with pytest.raises(ValueError, match="original"):
        delivery.expected_config(baseline(), record)


def test_mutable_original_reference_is_not_rewritten_into_fabricated_resolved_baseline() -> None:
    original = baseline()
    mapping = manifest()["providers"]["discogs"]
    assert original["services"][mapping["service"]]["image"].endswith(":latest")
    candidate = delivery.expected_config(original, manifest())
    assert original["services"][mapping["service"]]["image"] == mapping["baseline_configured_image"]
    assert candidate["services"][mapping["service"]]["image"] == mapping["image"]
    fabricated = copy.deepcopy(original)
    fabricated["services"][mapping["service"]]["image"] = mapping["baseline_image"]
    with pytest.raises(ValueError, match="original baseline"):
        delivery.expected_config(fabricated, manifest())


@pytest.mark.parametrize("step", ["inspect", "remove"])
def test_cleanup_timeout_preserves_owned_reconciliation_identity(monkeypatch: pytest.MonkeyPatch, step: str) -> None:
    image = manifest()["providers"]["discogs"]["image"]
    owner = ""

    def fake_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal owner
        args = command[3:]
        if args[:2] == ["image", "inspect"]:
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(
                    [
                        {
                            "Architecture": "amd64",
                            "Os": "linux",
                            "Config": {"Labels": {"org.opencontainers.image.revision": "b" * 40}, "Entrypoint": ["discogs-ingestion"]},
                        }
                    ]
                ),
                "",
            )
        if args[0] == "create":
            owner = args[args.index("--label") + 1].split("=", 1)[1]
            raise subprocess.TimeoutExpired(command, 30)
        if args[:2] == ["container", "inspect"]:
            if step == "inspect":
                raise subprocess.TimeoutExpired(command, 5)
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(
                    [
                        {
                            "Id": "c" * 64,
                            "Config": {"Image": image, "Labels": {"beadhive.probe.owner": owner, "beadhive.probe.task": "gm-deployment-eeo"}},
                        }
                    ]
                ),
                "",
            )
        assert args == ["rm", "--force", "c" * 64]
        raise subprocess.TimeoutExpired(command, 5)

    monkeypatch.setattr(delivery.subprocess, "run", fake_run)
    with pytest.raises(subprocess.TimeoutExpired) as error:
        delivery.isolated_startup_probe(image, "discogs", "test-only", "b" * 40)
    note = " ".join(error.value.__notes__)
    assert f"name=gm-extractor-log-probe-{owner}" in note
    assert f"owner={owner}" in note
    assert "context=test-only" in note
    assert isinstance(error.value.__context__, subprocess.TimeoutExpired)


def _load_fragment() -> Any:
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "extractor_managed_fragment", Path(__file__).resolve().parents[2] / "scripts/extractor_managed_fragment.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fragment = _load_fragment()


def managed_original() -> bytes:
    # Deliberately preserve unrelated edits, anchors, comments and non-ASCII bytes.
    text = """x-base: &base
  user: '1000:1000'
services:
  unrelated:
    image: unrelated:unchanged # existing user edit
    environment:
      NOTE: café
"""
    for provider in delivery.PROVIDERS:
        text += f"""  existing-extractor-{provider}:
    <<: *base
    image: registry.example/legacy-extractor:latest # retain provenance
    command: ["--source", "{provider}"] # selector
    environment: # original operator annotation
      ENVIRONMENT: production
      STARTUP_DELAY: "30"
      RABBITMQ_PASSWORD_FILE: /run/secrets/test-only
      RETAIN: 'user modification'
    healthcheck:
      test: [CMD, 'true']
"""
        if provider == "musicbrainz":
            # Insert at the second service's environment only.
            index = text.rfind("    healthcheck:")
            text = text[:index] + "      AMQP_EXCHANGE_PREFIX: ignored\n      DISCOGS_HEALTH_URL: obsolete\n" + text[index:]
    return text.encode()


@pytest.mark.parametrize("crlf", [False, True])
def test_managed_patch_preserves_dirty_bytes_and_exact_immutable_rollback(crlf: bool) -> None:
    original = managed_original()
    if crlf:
        original = original.replace(b"\n", b"\r\n")
    value = manifest()
    forward, proof = fragment.render_fragment(original, fragment.digest(original), value)
    rollback, rollback_proof = fragment.render_fragment(original, fragment.digest(original), value, rollback=True)
    expected_rollback = original.replace(b"registry.example/legacy-extractor:latest", value["providers"]["discogs"]["baseline_image"].encode())
    assert rollback == expected_rollback
    for preserved in [
        b"NOTE: caf\xc3\xa9",
        b"RETAIN: 'user modification'",
        b"# retain provenance",
        b"# selector",
        b"# original operator annotation",
        b"<<: *base",
    ]:
        assert forward.count(preserved) == original.count(preserved)
    assert b"STARTUP_DELAY:" not in forward and b"DISCOGS_HEALTH_URL:" not in forward
    assert forward.count(b"command: !reset []") == 2
    assert proof["all_unrelated_bytes_preserved"] and rollback_proof["changed_original_line_spans"] == 2
    assert not proof["compose_equivalence_verified"] and not proof["production_applied"]
    if crlf:
        assert b"\n" not in forward.replace(b"\r\n", b"")


@pytest.mark.parametrize("change", ["hash", "image", "selector", "duplicate", "environment"])
def test_managed_patch_rejects_source_drift_or_ambiguous_mapping(change: str) -> None:
    original = managed_original()
    if change == "image":
        original = original.replace(b"legacy-extractor:latest", b"other:latest", 1)
    elif change == "selector":
        original = original.replace(b'"--source", "discogs"', b'"--source", "musicbrainz"', 1)
    elif change == "duplicate":
        original += b"  existing-extractor-discogs:\n"
    elif change == "environment":
        original = original.replace(b"environment: # original operator annotation", b"environment: {OTHER: unsafe}", 1)
    expected = "0" * 64 if change == "hash" else fragment.digest(original)
    with pytest.raises(ValueError):
        fragment.render_fragment(original, expected, manifest())


def test_real_compose_managed_fragment_matches_full_contract_and_rollback(tmp_path: Path) -> None:
    original = managed_original()
    value = manifest()
    forward, _ = fragment.render_fragment(original, fragment.digest(original), value)
    rollback, _ = fragment.render_fragment(original, fragment.digest(original), value, rollback=True)
    models = []
    for name, content in [("original", original), ("forward", forward), ("rollback", rollback)]:
        path = tmp_path / (name + ".yml")
        path.write_bytes(content)
        command = ["docker", "compose", "--project-name", "extractor-delivery-offline-test", "-f", str(path), "config", "--format", "json"]
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        models.append(json.loads(result.stdout))
    delivery.validate_candidate(models[0], models[1], value)
    expected = copy.deepcopy(models[0])
    for record in value["providers"].values():
        expected["services"][record["service"]]["image"] = record["baseline_image"]
    assert models[2] == expected


def test_managed_cli_refuses_existing_output_without_modifying_any_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = tmp_path / "original.yml"
    original.write_bytes(managed_original())
    value = tmp_path / "manifest.json"
    value.write_text(json.dumps(manifest()))
    rollback = tmp_path / "rollback.yml"
    receipt = tmp_path / "receipt.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "fragment",
            "--original",
            str(original),
            "--expected-sha256",
            fragment.digest(original.read_bytes()),
            "--manifest",
            str(value),
            "--forward-output",
            str(original),
            "--rollback-output",
            str(rollback),
            "--receipt",
            str(receipt),
        ],
    )
    with pytest.raises(ValueError, match="distinct and absent"):
        fragment.main()
    assert original.read_bytes() == managed_original()
    assert not rollback.exists() and not receipt.exists()
