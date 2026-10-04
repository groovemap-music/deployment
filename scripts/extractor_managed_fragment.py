"""Render guarded managed-fragment candidates; never overwrite an existing file."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import yaml
from extractor_delivery import PROVIDERS, require, validate_manifest


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def scalar(line: str) -> Any:
    return yaml.safe_load(line.split(":", 1)[1])


def replace_value(line: str, value: str) -> str:
    ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
    body = line.removesuffix(ending) if ending else line
    comment = re.search(r"\s+#.*$", body)
    return body.split(":", 1)[0] + ": " + value + (comment.group() if comment else "") + ending


def render_fragment(original: bytes, expected_sha: str, manifest: dict[str, Any], rollback: bool = False) -> tuple[bytes, dict[str, Any]]:
    """Change approved direct fields only, retaining all other original byte spans."""
    require(digest(original) == expected_sha, "managed fragment hash drift; preserve existing edits")
    validate_manifest(manifest)
    text = original.decode("utf-8")
    require("\t" not in text, "ambiguous tab-indented managed fragment")
    lines = text.splitlines(keepends=True)
    replacements: dict[int, str] = {}
    allowed: set[int] = set()
    changed_paths: list[str] = []
    for provider, (prefix_key, _) in PROVIDERS.items():
        record = manifest["providers"][provider]
        service = record["service"]
        headers = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == f"  {service}:"]
        require(len(headers) == 1, "mapped service missing or duplicated in managed fragment")
        start = headers[0]
        end = next((i for i in range(start + 1, len(lines)) if re.match(r"^(?:  [A-Za-z0-9_.-]+:|[^\s#])", lines[i])), len(lines))
        fields: dict[str, list[int]] = {}
        for i in range(start + 1, end):
            match = re.match(r"^    ([A-Za-z0-9_.-]+):", lines[i])
            if match:
                fields.setdefault(match[1], []).append(i)
        require(all(len(fields.get(key, [])) == 1 for key in ["image", "command", "environment"]), "ambiguous required service fields")
        image_index, command_index, environment_index = (fields[key][0] for key in ["image", "command", "environment"])
        require(scalar(lines[image_index]) == record["baseline_configured_image"], "managed original image does not match verified baseline")
        require(scalar(lines[command_index]) == ["--source", provider], "managed original provider selector drift")
        require(not lines[environment_index].split(":", 1)[1].split("#", 1)[0].strip(), "environment must use direct block mapping")
        replacements[image_index] = replace_value(lines[image_index], record["baseline_image"] if rollback else record["image"])
        allowed.add(image_index)
        changed_paths.append(service + ".image")
        if rollback:
            continue
        replacements[command_index] = replace_value(lines[command_index], "!reset []")
        allowed.add(command_index)
        changed_paths.append(service + ".command")
        env_end = next((i for i in range(environment_index + 1, end) if re.match(r"^    [^\s#]", lines[i])), end)
        env_fields: dict[str, list[int]] = {}
        for i in range(environment_index + 1, env_end):
            match = re.match(r"^      ([A-Za-z0-9_]+):", lines[i])
            if match:
                env_fields.setdefault(match[1], []).append(i)
        removed = {"STARTUP_DELAY"}
        if provider == "musicbrainz":
            removed |= {"AMQP_EXCHANGE_PREFIX", "DISCOGS_HEALTH_URL"}
        require(all(len(env_fields.get(key, [])) <= 1 for key in removed | {"ENVIRONMENT", prefix_key}), "duplicated edited environment key")
        require(len(env_fields.get("ENVIRONMENT", [])) == 1, "missing direct deployment environment field")
        for key in removed:
            for index in env_fields.get(key, []):
                replacements[index] = ""
                allowed.add(index)
                changed_paths.append(service + ".environment." + key)
        environment = env_fields["ENVIRONMENT"][0]
        replacements[environment] = replace_value(lines[environment], "production")
        allowed.add(environment)
        if prefix_key in env_fields:
            index = env_fields[prefix_key][0]
            replacements[index] = replace_value(lines[index], json.dumps(record["exchange_prefix"]))
            allowed.add(index)
        else:
            ending = "\r\n" if lines[environment].endswith("\r\n") else "\n"
            replacements[environment] += f"      {prefix_key}: {json.dumps(record['exchange_prefix'])}{ending}"
        changed_paths.extend([service + ".environment.ENVIRONMENT", service + ".environment." + prefix_key])
    require(set(replacements) <= allowed, "patch escaped approved original line spans")
    candidate = "".join(replacements.get(i, line) for i, line in enumerate(lines)).encode("utf-8")
    untouched = b"".join(line.encode("utf-8") for i, line in enumerate(lines) if i not in allowed)
    require(all(replacements.get(i, line) == line for i, line in enumerate(lines) if i not in allowed), "unrelated original bytes changed")
    return candidate, {
        "mode": "immutable rollback" if rollback else "forward",
        "original_sha256": expected_sha,
        "candidate_sha256": digest(candidate),
        "untouched_original_bytes_sha256": digest(untouched),
        "untouched_original_bytes": len(untouched),
        "changed_original_line_spans": len(allowed),
        "edited_paths": sorted(changed_paths),
        "all_unrelated_bytes_preserved": True,
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
    outputs = [args.forward_output, args.rollback_output, args.receipt]
    require(
        len({path.resolve() for path in outputs}) == 3 and not any(path.exists() for path in outputs), "candidate outputs must be distinct and absent"
    )
    original = args.original.read_bytes()
    manifest = json.loads(args.manifest.read_text())
    forward, forward_proof = render_fragment(original, args.expected_sha256, manifest)
    rollback, rollback_proof = render_fragment(original, args.expected_sha256, manifest, rollback=True)
    # Existing files (including the managed input) are never opened for writing.
    for path, content in [(args.forward_output, forward), (args.rollback_output, rollback)]:
        with path.open("xb") as stream:
            stream.write(content)
    with args.receipt.open("x") as stream:
        json.dump({"forward": forward_proof, "rollback": rollback_proof}, stream, indent=2)
        stream.write("\n")
    print("Guarded candidates prepared; actual Compose equivalence and independent root review remain required.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
