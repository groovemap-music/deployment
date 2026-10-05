"""Prepare byte-guarded Explore/Insights candidates; never apply live configuration."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import yaml


SERVICES = {"explore": ("graph-explorer", 8007), "insights": ("analytics-engine", 8009)}
PROOFS = {"cli", "environment", "mounts", "network", "health", "proxy", "model_data", "logging", "rollback"}


def require(condition: bool, message: str) -> None:
    """Fail closed without disclosing private configuration values."""
    if not condition:
        raise ValueError(message)


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def validate_manifest(manifest: dict[str, Any]) -> None:
    """Require image-bound reviewed evidence; references alone do not prove their contents."""
    require(manifest.get("format_version") == 1, "unsupported delivery manifest")
    require(set(manifest.get("services", {})) == set(SERVICES), "exactly Explore and Insights required")
    names: set[str] = set()
    for role, (owner, _) in SERVICES.items():
        record = manifest["services"][role]
        service = record.get("service", "")
        require(bool(re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", service)), "invalid service mapping")
        require(service not in names, "duplicate service mapping")
        names.add(service)
        image = record.get("image", "")
        require(bool(re.fullmatch(rf"ghcr\.io/groovemap-music/{owner}@sha256:[0-9a-f]{{64}}", image)), "wrong owner or mutable image")
        require(bool(re.fullmatch(r"[a-zA-Z0-9./_-]+@sha256:[0-9a-f]{64}", record.get("rollback_image", ""))), "immutable rollback required")
        require(bool(record.get("baseline_configured_image")), "baseline image required")
        require(record.get("platform") == "linux/amd64", "verified AMD64 platform required")
        require(bool(re.fullmatch(r"[0-9a-f]{40}", record.get("revision", ""))), "full source revision required")
        require(bool(re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", record.get("version", ""))), "versioned release required")
        for key in ("release_reference", "attestation_reference", "ancestry_reference", "independent_review_reference"):
            require(bool(record.get(key)), "reviewed release provenance reference missing")
        proofs = record.get("proofs", {})
        require(set(proofs) == PROOFS, "complete compatibility evidence required")
        for proof in proofs.values():
            require(
                proof.get("image") == image and proof.get("status") == "passed" and bool(proof.get("reference")),
                "compatibility proof missing or wrong image",
            )
        logging = proofs["logging"]
        require(logging.get("environment") == "production" and logging.get("service") == owner, "logging identity mismatch")
        require(logging.get("missing_fields") == 0 and logging.get("wrong_fields") == 0, "logging sample incomplete")
        require(type(logging.get("events")) is int and logging["events"] > 0, "actual logging events required")
    require(bool(manifest.get("baseline_reference")), "fresh private baseline reference required")


def healthcheck(port: int) -> list[str]:
    return ["CMD", "/app/.venv/bin/python", "-c", f"import urllib.request; urllib.request.urlopen('http://localhost:{port}/health', timeout=5)"]


def expected_config(baseline: dict[str, Any], manifest: dict[str, Any], *, rollback: bool = False) -> dict[str, Any]:
    """Preserve every resolved contract outside image, production environment and health test."""
    validate_manifest(manifest)
    result = copy.deepcopy(baseline)
    for role, (_, port) in SERVICES.items():
        record = manifest["services"][role]
        service = record["service"]
        require(service in result.get("services", {}), "mapped baseline service absent")
        target = result["services"][service]
        require(target.get("image") == record["baseline_configured_image"], "baseline image drift")
        require("build" not in target, "service must consume a released image")
        require(not target.get("command") and not target.get("entrypoint"), "explicit legacy startup override needs separate reviewed adaptation")
        require(isinstance(target.get("environment"), dict), "resolved environment mapping required")
        require("STARTUP_DELAY" not in target["environment"], "startup delay requires independently reviewed preservation or adaptation")
        require(isinstance(target.get("healthcheck"), dict) and "test" in target["healthcheck"], "baseline healthcheck required")
        target["image"] = record["rollback_image"] if rollback else record["image"]
        if not rollback:
            target["environment"]["ENVIRONMENT"] = "production"
            target["healthcheck"]["test"] = healthcheck(port)
    return result


def validate_candidate(baseline: dict[str, Any], candidate: dict[str, Any], manifest: dict[str, Any], *, rollback: bool = False) -> None:
    require(candidate == expected_config(baseline, manifest, rollback=rollback), "candidate changes an unapproved resolved contract")


def render_fragment(original: bytes, expected_sha: str, manifest: dict[str, Any], *, rollback: bool = False) -> tuple[bytes, dict[str, Any]]:
    """Patch direct mapped fields only; preserve all unrelated bytes including operator edits."""
    require(digest(original) == expected_sha, "source hash drift; preserve operator edits")
    validate_manifest(manifest)
    text = original.decode()
    require("\t" not in text, "ambiguous tab indentation")
    parsed = yaml.safe_load(text)
    require(isinstance(parsed, dict) and isinstance(parsed.get("services"), dict), "service mapping required")
    lines = text.splitlines(keepends=True)
    changes: dict[int, str] = {}
    for role, (_, port) in SERVICES.items():
        record = manifest["services"][role]
        direct = parsed["services"].get(record["service"], {})
        environment = direct.get("environment") if isinstance(direct, dict) else None
        require(isinstance(environment, dict), "direct environment mapping required")
        require("STARTUP_DELAY" not in environment, "startup delay requires independently reviewed preservation or adaptation")
        headers = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == f"  {record['service']}:"]
        require(len(headers) == 1, "missing or duplicated mapped service")
        start = headers[0]
        end = next((i for i in range(start + 1, len(lines)) if re.match(r"^(?:  [A-Za-z0-9_.-]+:|[^\s#])", lines[i])), len(lines))
        fields: dict[str, list[int]] = {}
        for i in range(start + 1, end):
            match = re.match(r"^    ([A-Za-z0-9_.-]+):", lines[i])
            if match:
                fields.setdefault(match[1], []).append(i)
        require(all(len(fields.get(key, [])) == 1 for key in ("image", "environment", "healthcheck")), "ambiguous required direct fields")
        require(
            not fields.get("command") and not fields.get("entrypoint") and not fields.get("build"), "legacy startup/build override requires review"
        )
        image = fields["image"][0]
        require(yaml.safe_load(lines[image].split(":", 1)[1]) == record["baseline_configured_image"], "original image drift")

        def replace(index: int, value: str) -> str:
            line = lines[index]
            ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            body = line.removesuffix(ending) if ending else line
            comment = re.search(r"\s+#.*$", body)
            return body.split(":", 1)[0] + ": " + value + (comment.group() if comment else "") + ending

        changes[image] = replace(image, record["rollback_image"] if rollback else record["image"])
        if rollback:
            continue
        for block, keys in (("environment", {"ENVIRONMENT"}), ("healthcheck", {"test"})):
            index = fields[block][0]
            require(not lines[index].split(":", 1)[1].split("#", 1)[0].strip(), "direct block mapping required")
            stop = next((i for i in range(index + 1, end) if re.match(r"^    [^\s#]", lines[i])), end)
            nested: dict[str, list[int]] = {}
            for i in range(index + 1, stop):
                match = re.match(r"^      ([A-Za-z0-9_]+):", lines[i])
                if match:
                    nested.setdefault(match[1], []).append(i)
            require(all(len(nested.get(key, [])) <= 1 for key in keys), "duplicate edited field")
            if block == "environment":
                if nested.get("ENVIRONMENT"):
                    changes[nested["ENVIRONMENT"][0]] = replace(nested["ENVIRONMENT"][0], "production")
                else:
                    ending = "\r\n" if lines[index].endswith("\r\n") else "\n"
                    changes[index] = lines[index] + f"      ENVIRONMENT: production{ending}"
            else:
                require(len(nested.get("test", [])) == 1, "direct healthcheck test required")
                changes[nested["test"][0]] = replace(nested["test"][0], json.dumps(healthcheck(port)))
    candidate = "".join(changes.get(i, line) for i, line in enumerate(lines)).encode()
    untouched = b"".join(line.encode() for i, line in enumerate(lines) if i not in changes)
    return candidate, {
        "original_sha256": expected_sha,
        "candidate_sha256": digest(candidate),
        "untouched_bytes_sha256": digest(untouched),
        "all_unrelated_bytes_preserved": True,
        "rollback": rollback,
        "compose_equivalence_verified": False,
        "production_applied": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--forward-output", type=Path, required=True)
    parser.add_argument("--rollback-output", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    paths = [args.forward_output, args.rollback_output, args.receipt]
    require(len({p.resolve() for p in paths}) == 3 and not any(p.exists() for p in paths), "outputs must be distinct and absent")
    original = args.original.read_bytes()
    manifest = json.loads(args.manifest.read_text())
    forward, forward_proof = render_fragment(original, args.expected_sha256, manifest)
    rollback, rollback_proof = render_fragment(original, args.expected_sha256, manifest, rollback=True)
    for path, content in [(args.forward_output, forward), (args.rollback_output, rollback)]:
        with path.open("xb") as stream:
            stream.write(content)
    with args.receipt.open("x") as stream:
        json.dump({"forward": forward_proof, "rollback": rollback_proof}, stream, indent=2)
        stream.write("\n")
    print("Candidates prepared; root review and actual Compose equivalence remain required.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
