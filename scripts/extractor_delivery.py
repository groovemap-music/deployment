"""Prepare and check a two-extractor adaptation; never apply it to a live host."""

from __future__ import annotations

import argparse
import copy
import json
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any

import yaml


PROVIDERS = {
    "discogs": ("DISCOGS_EXCHANGE_PREFIX", "e95c0a5db6b8721fecbddf78ea54ed4b17263f01"),
    "musicbrainz": ("MUSICBRAINZ_EXCHANGE_PREFIX", "737eac71a9039efa9ade93de194f33cec50c5a35"),
}
SHA = re.compile(r"[0-9a-f]{40}\Z")
VERSION = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+\Z")
PROOFS = {"cli", "secret_file", "mount_permissions", "routing", "consumer_contract", "resume", "logging"}


def require(condition: bool, message: str) -> None:
    """Reject incomplete operator evidence without printing configuration values."""
    if not condition:
        raise ValueError(message)


def validate_manifest(manifest: dict[str, Any]) -> None:
    """Validate the reviewed release/evidence references, not their human approval itself."""
    require(manifest.get("format_version") == 1, "unsupported delivery manifest version")
    require(isinstance(manifest.get("providers"), dict), "provider mapping required")
    require(set(manifest["providers"]) == set(PROVIDERS), "manifest must name exactly the two providers")
    service_names: set[str] = set()
    for provider, (_, source_main) in PROVIDERS.items():
        record = manifest["providers"][provider]
        require(isinstance(record, dict), f"{provider}: release record required")
        service = record.get("service")
        require(
            isinstance(service, str) and bool(re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", service)),
            f"{provider}: exact baseline service name required",
        )
        require(service not in service_names, "provider service mappings must be distinct")
        service_names.add(service)
        original_image = record.get("baseline_image", "")
        require(
            isinstance(original_image, str) and bool(re.fullmatch(r"[a-zA-Z0-9./_-]+@sha256:[0-9a-f]{64}", original_image)),
            f"{provider}: immutable original baseline image required",
        )
        configured = record.get("baseline_configured_image")
        require(isinstance(configured, str) and bool(configured), f"{provider}: original configured image reference required")
        runtime = record.get("baseline_runtime", {})
        require(isinstance(runtime, dict), f"{provider}: original runtime identity record required")
        require(runtime.get("configured_image") == configured, f"{provider}: original runtime configured reference mismatch")
        require(runtime.get("repo_digest") == original_image, f"{provider}: inspected original registry digest mismatch")
        require(
            isinstance(runtime.get("image_id"), str) and bool(re.fullmatch(r"sha256:[0-9a-f]{64}", runtime["image_id"])),
            f"{provider}: inspected original image ID required",
        )
        require(bool(runtime.get("reference")), f"{provider}: original runtime identity evidence required")
        image = record.get("image", "")
        require(isinstance(image, str), f"{provider}: immutable image string required")
        require(
            bool(re.fullmatch(rf"ghcr\.io/groovemap-music/{provider}-ingestion@sha256:[0-9a-f]{{64}}", image)),
            f"{provider}: wrong owner or mutable image",
        )
        require(isinstance(record.get("version"), str), f"{provider}: version string required")
        require(bool(VERSION.fullmatch(record["version"])), f"{provider}: versioned source-owner release required")
        require(isinstance(record.get("image_revision"), str), f"{provider}: OCI revision string required")
        require(bool(SHA.fullmatch(record["image_revision"])), f"{provider}: full OCI source revision required")
        require(record.get("reviewed_main") == source_main, f"{provider}: reviewed logging source prerequisite mismatch")
        require(record.get("platform") == "linux/amd64", f"{provider}: target platform evidence required")
        require(bool(record.get("release_review_reference")), f"{provider}: independent image release review reference required")
        ancestry = record.get("source_ancestry", {})
        require(isinstance(ancestry, dict), f"{provider}: trusted source ancestry record required")
        require(ancestry.get("image") == image, f"{provider}: ancestry evidence image mismatch")
        require(ancestry.get("ancestor") == source_main, f"{provider}: ancestry evidence source mismatch")
        require(ancestry.get("descendant") == record["image_revision"], f"{provider}: ancestry evidence OCI revision mismatch")
        require(
            ancestry.get("command") == ["git", "merge-base", "--is-ancestor", source_main, record["image_revision"]],
            f"{provider}: ancestry command mismatch",
        )
        require(type(ancestry.get("exit")) is int and ancestry["exit"] == 0, f"{provider}: source is not verified as image ancestor")
        require(bool(ancestry.get("reference")), f"{provider}: ancestry command result reference required")
        require(
            isinstance(record.get("exchange_prefix"), str) and bool(record["exchange_prefix"]), f"{provider}: verified live exchange prefix required"
        )
        proofs = record.get("proofs", {})
        require(isinstance(proofs, dict), f"{provider}: compatibility proof mapping required")
        require(set(proofs) == PROOFS, f"{provider}: exact compatibility evidence categories required")
        for name, proof in proofs.items():
            require(isinstance(proof, dict), f"{provider}/{name}: proof record required")
            require(proof.get("image") == image, f"{provider}/{name}: evidence must identify the selected immutable image")
            require(proof.get("status") == "passed", f"{provider}/{name}: compatibility proof not passed")
            require(bool(proof.get("reference")), f"{provider}/{name}: durable evidence reference required")
        logging = proofs["logging"]
        require(
            logging.get("missing_environment") == 0 and logging.get("wrong_environment") == 0,
            f"{provider}: incomplete/mixed logging context evidence",
        )
        require(logging.get("root_environment") == "production", f"{provider}: actual root production JSON evidence required")
        require(type(logging.get("json_events")) is int and logging["json_events"] > 0, f"{provider}: no JSON events observed")
    require(bool(manifest.get("baseline_reference")), "durable live baseline reference required")
    require(bool(manifest.get("rollback_reference")), "durable rollback reference required")


def adaptation(manifest: dict[str, Any]) -> dict[str, Any]:
    """Return a minimal override for review; preserve all unspecified live contracts."""
    validate_manifest(manifest)
    services = {}
    for provider, (prefix_key, _) in PROVIDERS.items():
        record = manifest["providers"][provider]
        environment: dict[str, Any] = {"ENVIRONMENT": "production", prefix_key: record["exchange_prefix"], "STARTUP_DELAY": None}
        if provider == "musicbrainz":
            environment.update({"AMQP_EXCHANGE_PREFIX": None, "DISCOGS_HEALTH_URL": None})
        services[record["service"]] = {"image": record["image"], "command": [], "environment": environment}
    return {"services": services}


def expected_config(baseline: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    """Model the intended resolved config; no credential or mount values leave this function."""
    overlay = adaptation(manifest)
    result = copy.deepcopy(baseline)
    for provider, record in manifest["providers"].items():
        service = record["service"]
        changes = overlay["services"][service]
        require(service in result.get("services", {}), f"missing baseline service {service}")
        target = result["services"][service]
        require(target.get("image") == record["baseline_configured_image"], f"{provider}: original baseline image mismatch")
        require(target.get("command") == ["--source", provider], f"{provider}: original baseline provider selector mismatch")
        require("build" not in target, f"{service}: baseline must consume an immutable image")
        target["image"] = changes["image"]
        target["command"] = None
        require(isinstance(target.get("environment"), dict), f"{service}: resolved environment mapping required")
        for key, value in changes["environment"].items():
            if value is None:
                target["environment"].pop(key, None)
            else:
                target["environment"][key] = value
    return result


def validate_candidate(baseline: dict[str, Any], candidate: dict[str, Any], manifest: dict[str, Any]) -> None:
    """Require a byte-value-equivalent resolved configuration outside the narrow adaptation."""
    expected = expected_config(baseline, manifest)
    require(candidate == expected, "candidate changes an unapproved contract (inspect private configs locally; values are not printed)")


class Reset:
    """Represent Compose's !reset tag, needed to remove inherited obsolete settings."""

    def __init__(self, value: Any) -> None:
        self.value = value


class ComposeDumper(yaml.SafeDumper):
    """Emit only the minimal reviewable override, including Compose merge tags."""


def represent_reset(dumper: yaml.SafeDumper, value: Reset) -> yaml.Node:
    node = dumper.represent_data(value.value)
    node.tag = "!reset"
    return node


ComposeDumper.add_representer(Reset, represent_reset)


def render_overlay(manifest: dict[str, Any]) -> str:
    """Render an override that does not retain old --source arguments or obsolete env keys."""
    overlay = adaptation(manifest)
    for service in overlay["services"].values():
        service["command"] = Reset([])
        for key, value in service["environment"].items():
            if value is None:
                service["environment"][key] = Reset(None)
    return yaml.dump(overlay, Dumper=ComposeDumper, sort_keys=False)


def validate_json_log(text: str, environment: str) -> dict[str, Any]:
    """Check emitted JSON without returning raw messages; image identity is verified separately."""
    events = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        value = json.loads(stripped)
        require(isinstance(value, dict), "structured event must be an object")
        require(value.get("environment") == environment, "missing or wrong root environment")
        require(isinstance(value.get("fields"), dict) and isinstance(value.get("level"), str), "expected actual Rust event fields")
        events += 1
    require(events > 0, "no structured JSON events")
    return {"json_events": events, "root_environment": environment, "missing_environment": 0, "wrong_environment": 0}


def isolated_startup_probe(image: str, provider: str, context: str, revision: str, executor: Any = None) -> dict[str, Any]:
    """Probe an already-pulled approved image without network, host mounts or production credentials."""
    require(provider in PROVIDERS, "unknown provider")
    require(bool(re.fullmatch(rf"ghcr\.io/groovemap-music/{provider}-ingestion@sha256:[0-9a-f]{{64}}", image)), "immutable provider image required")
    require(bool(re.fullmatch(r"[a-zA-Z0-9_.-]+", context)), "explicit Docker context required")
    require(bool(SHA.fullmatch(revision)), "verified OCI revision required")
    docker = ["docker", "--context", context]

    def execute(arguments: list[str], timeout: int = 30, cleanup: bool = False) -> subprocess.CompletedProcess[str]:
        if executor is not None:
            return executor(arguments, timeout, cleanup=cleanup)  # type: ignore[no-any-return]
        # All commands address Docker with explicit argv; no shell or private environment is forwarded.
        return subprocess.run([*docker, *arguments], capture_output=True, text=True, timeout=timeout, check=False)  # noqa: S603

    inspection = execute(["image", "inspect", image])
    require(inspection.returncode == 0, "approved image must already be pulled in selected context")
    metadata = json.loads(inspection.stdout)[0]
    require(metadata.get("Architecture") == "amd64" and metadata.get("Os") == "linux", "wrong inspected image platform")
    require(metadata.get("Config", {}).get("Labels", {}).get("org.opencontainers.image.revision") == revision, "inspected OCI revision mismatch")
    require(metadata.get("Config", {}).get("Entrypoint") == [f"{provider}-ingestion"], "unexpected provider image entrypoint")
    require(not metadata.get("Config", {}).get("Cmd"), "unexpected provider image default command")
    scenarios: dict[str, Any] = {}
    for name, arguments, environment in [
        ("help", ["--help"], None),
        ("legacy_selector", ["--source", provider], None),
        ("production", [], "production"),
        ("default", [], None),
    ]:
        # Invalid synthetic secret makes config fail before acquisition. Never use a host secret or dump mount.
        owner = uuid.uuid4().hex
        container_name = f"gm-extractor-log-probe-{owner}"
        command = [
            "create",
            "--name",
            container_name,
            "--label",
            f"beadhive.probe.owner={owner}",
            "--label",
            "beadhive.probe.task=gm-deployment-eeo",
            "--cpus",
            "1",
            "--memory",
            "256m",
            "--memory-swap",
            "256m",
            "--pids-limit",
            "64",
            "--log-driver",
            "json-file",
            "--log-opt",
            "max-size=1m",
            "--log-opt",
            "max-file=1",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            "1000:1000",
            "--platform",
            "linux/amd64",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=16m",
            "--env",
            "RABBITMQ_PASSWORD_FILE=/run/secrets/missing-probe-only",
            "--env",
            "OTEL_METRICS_EXPORTER=none",
            "--env",
            "OTEL_TRACES_EXPORTER=none",
            "--env",
            "OTEL_EXPORTER_OTLP_ENDPOINT=",
        ]
        if environment is not None:
            command.extend(["--env", f"ENVIRONMENT={environment}"])
        container: str | None = None
        try:
            created = execute([*command, image, *arguments])
            require(created.returncode == 0, "could not create isolated probe container")
            candidate_id = created.stdout.strip()
            require(bool(re.fullmatch(r"[0-9a-f]{64}", candidate_id)), "invalid created container identity")
            container = candidate_id
            started = execute(["start", "--attach", container])
            state = execute(["inspect", "--format", "{{.State.ExitCode}}", container])
            require(state.returncode == 0, "cannot inspect owned probe exit status")
            exit_code = int(state.stdout.strip())
            if name == "help":
                require(exit_code == 0 and f"{provider}-ingestion" in started.stdout and "--source" not in started.stdout, "provider CLI mismatch")
                scenarios[name] = {"exit": exit_code, "passed": True}
            elif name == "legacy_selector":
                require(exit_code == 2, "legacy selector must be rejected by provider CLI")
                scenarios[name] = {"exit": exit_code, "passed": True}
            else:
                require(exit_code == 1, "missing synthetic secret must fail before acquisition")
                scenarios[name] = {"exit": exit_code, **validate_json_log(started.stdout, environment or "development")}
        except subprocess.TimeoutExpired as error:
            error.add_note(f"Task-owned probe name={container_name} owner={owner} context={context}; inspect ownership before any later cleanup")
            raise
        finally:
            # Even timeout/error before create returns an ID uses the unique ownership label.
            try:
                inspected = execute(["container", "inspect", container_name], timeout=5, cleanup=True)
                if inspected.returncode == 0:
                    owned = json.loads(inspected.stdout)[0]
                    config = owned.get("Config", {})
                    owned_id = owned.get("Id", "")
                    require(
                        config.get("Labels", {}).get("beadhive.probe.owner") == owner
                        and config.get("Labels", {}).get("beadhive.probe.task") == "gm-deployment-eeo"
                        and config.get("Image") == image,
                        "probe cleanup ownership is not verified; no container removed",
                    )
                    require(bool(re.fullmatch(r"[0-9a-f]{64}", owned_id)), "invalid inspected probe identity")
                    require(container is None or container == owned_id, "probe cleanup identity mismatch; no container removed")
                    removed = execute(["rm", "--force", owned_id], timeout=5, cleanup=True)
                    require(removed.returncode == 0, "owned isolated probe cleanup failed")
                else:
                    require(container is None, "owned probe disappeared or cleanup could not inspect it")
            except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as cleanup_error:
                cleanup_error.add_note(
                    f"Task-owned cleanup unresolved name={container_name} owner={owner} context={context}; inspect ownership before reconciliation"
                )
                raise
    return {
        "image": image,
        "provider": provider,
        "image_revision": revision,
        "platform": "linux/amd64",
        "scenarios": scenarios,
        "limitations": "CLI/negative secret-file/startup JSON only; no valid-secret broker, legacy consumer or resume acceptance.",
    }


def load_json(path: Path) -> dict[str, Any]:
    """Load private operator inputs without echoing their contents."""
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("input must be a JSON object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--probe-image", help="explicit isolated startup probe, never a deployment")
    parser.add_argument("--provider", choices=tuple(PROVIDERS))
    parser.add_argument("--docker-context")
    parser.add_argument("--image-revision")
    parser.add_argument("--inspect-log", type=Path, help="check private captured JSON logs; reports only aggregate counts")
    parser.add_argument("--expected-environment", choices=("production", "development"), default="production")
    parser.add_argument("--baseline", type=Path, help="private resolved baseline Compose JSON")
    parser.add_argument("--candidate", type=Path, help="private resolved candidate Compose JSON to compare")
    parser.add_argument("--output", type=Path, help="write a reviewed override; never apply it")
    args = parser.parse_args()
    try:
        if args.probe_image:
            require(bool(args.provider and args.docker_context and args.image_revision), "provider, context and verified image revision required")
            print(json.dumps(isolated_startup_probe(args.probe_image, args.provider, args.docker_context, args.image_revision)))
            return 0
        if args.inspect_log:
            print(json.dumps(validate_json_log(args.inspect_log.read_text(), args.expected_environment)))
            return 0
        require(isinstance(args.manifest, Path) and isinstance(args.baseline, Path), "manifest and private baseline required")
        manifest = load_json(args.manifest)
        baseline = load_json(args.baseline)
        expected_config(baseline, manifest)
        if args.candidate:
            validate_candidate(baseline, load_json(args.candidate), manifest)
        if args.output:
            args.output.write_text(render_overlay(manifest))
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        # Parse and filesystem failures can contain private paths or JSON input snippets.
        print(f"delivery preflight rejected ({type(error).__name__}); inspect inputs locally")
        for note in getattr(error, "__notes__", []):
            print(note)
        return 2
    print("delivery inputs validated; no deployment performed; independent review and root coordination remain required")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
