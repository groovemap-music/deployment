"""Prepare a host-native owned smoke bundle; never execute Docker or certify runtime proof."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import lzma
import re
import tarfile
import uuid
from pathlib import Path
from typing import Any

import yaml


LEGACY_REVISION = "2d680d4c8bc68b2d4084a885608ea4b9c949def4"
SCHEMAS = {
    "postgres_schema.py": "e2760f1f2cca370375aaf3ba138a809c7fa0c4624eeddb0c44a1766d892d491b",
    "neo4j_schema.py": "2a79bd02a1be0e1c65d3b829a80d5ce1076ccaf776aa9a68f2e70a0fa0623fb1",
}
# Limits include every declared service even where runtime steps will be sequential.
LIMITS = {
    "broker": (0.5, 512, 64),
    "postgres": (1.0, 1024, 128),
    "neo4j": (1.0, 1792, 128),
    "fixture-http": (0.25, 96, 32),
    "sql-consumer": (1.0, 512, 64),
    "graph-consumer": (1.0, 512, 64),
    "observer": (0.5, 96, 32),
    "producer": (1.0, 512, 64),
    "schema-sql": (0.25, 128, 32),
    "schema-graph": (0.25, 128, 32),
}
DATA_VOLUMES = {"broker": 64, "postgres": 256, "neo4j": 128, "producer": 16, "captures": 16, "neo4j-conf": 4}
LEGACY_DIGESTS = {
    "discogs-sql": "89250fd96b8b31596e5943f695d5610dfc1c126fe1c9a92e879d8506cea86c6e",
    "discogs-graph": "2bfcbda0720858586f456b5ec45cc5d11f6477315e268c11a825315c39d31503",
    "musicbrainz-sql": "e5457ef0229c58c4bc9fbe60f2cc53ebc28ad09702447dff07ffbbcd7cb3eab5",
    "musicbrainz-graph": "2f072f55b7d03c1cd68988169854d2c937c2a293686913a8a8d2d3647d989197",
}
MB_VERSION = "20000101-000000"
MB_IDS = {entity: f"f0f0f0f0-0000-4000-8000-{i:012d}" for i, entity in enumerate(["artist", "label", "release-group", "release"], 1)}


def require(value: bool, message: str) -> None:
    if not value:
        raise ValueError(message)


def immutable(image: Any) -> bool:
    return isinstance(image, str) and bool(re.fullmatch(r"[a-zA-Z0-9./_-]+@sha256:[0-9a-f]{64}", image))


def validate_inputs(context: dict[str, Any], images: dict[str, Any]) -> tuple[str, str]:
    """Check private input structure; externally verified approval remains separate."""
    require(context.get("bead") == "gm-deployment-eeo", "wrong task context")
    require(context.get("actor") == "dev/gm-extractor-log-delivery", "wrong actor context")
    selected = context["context"]
    require(selected.get("architecture") == "linux/amd64", "native AMD64 context required")
    owned = Path(selected["owned_root"])
    require(owned.is_absolute() and bool(re.fullmatch(r"gm-eeo-smoke-[a-f0-9]{12}", owned.name)), "unique owned root required")
    require(Path(selected["exclusive_lock"]) == owned.parent / "host-resource.lock", "existing host-resource lock required")
    budget = context["budgets"]
    require(budget.get("providers_sequential") is True and budget.get("no_swap_growth") is True, "sequential no-swap context required")
    require(budget.get("aggregate_cpu_quota_max") == 8 and budget.get("aggregate_memory_and_swap_max_gib") == 6, "approved compute budget mismatch")
    require(budget.get("owned_disk_max_gib") == 8 and budget.get("overall_timeout_seconds") == 900, "approved storage/deadline mismatch")
    require(set(images["producers"]) == {"discogs", "musicbrainz"}, "both released producers required")
    for provider, item in images["producers"].items():
        require(immutable(item.get("image")) and item["image"].startswith(f"ghcr.io/groovemap-music/{provider}-ingestion@"), "wrong producer image")
        require(item.get("platform") == "linux/amd64", "producer platform proof required")
        require(bool(re.fullmatch(r"[0-9a-f]{40}", item.get("revision", ""))), "producer OCI revision required")
        require(bool(item.get("verified_provenance_reference")), "external OCI/SLSA/source ancestry review reference required")
    require(set(images["consumers"]) == {"discogs-sql", "discogs-graph", "musicbrainz-sql", "musicbrainz-graph"}, "exact legacy consumers required")
    for role, item in images["consumers"].items():
        require(
            immutable(item.get("image")) and item["image"].endswith("@sha256:" + LEGACY_DIGESTS[role]) and item.get("revision") == LEGACY_REVISION,
            "deployed legacy consumer identity required",
        )
    require(set(images.get("prefixes", {})) == {"discogs", "musicbrainz"}, "verified provider prefixes required")
    require(all(isinstance(value, str) and bool(value) for value in images["prefixes"].values()), "empty provider prefix")
    require(set(context["approved_auxiliary_pins"]) == {"rabbitmq", "postgres", "neo4j"}, "exact auxiliary pin roles required")
    require(all(immutable(image) for image in context["approved_auxiliary_pins"].values()), "immutable auxiliary pins required")
    return str(owned), owned.name


def resource_proof() -> dict[str, Any]:
    """Count quotas plus independently charged persistent tmpfs and Docker log caps."""
    cpu = sum(item[0] for item in LIMITS.values())
    memory = sum(item[1] for item in LIMITS.values())
    # Count volume tmpfs AGAIN conservatively, rather than relying on cgroup charging behavior.
    volume_memory = sum(DATA_VOLUMES.values())
    require(cpu + 1 <= 8 and memory + volume_memory + 256 <= 6144, "aggregate runtime budget exceeded")
    return {
        "aggregate_cpu_quota": cpu,
        "host_control_cpu_reservation": 1,
        "conservative_total_cpu": cpu + 1,
        "container_memory_and_swap_mib": memory,
        "named_tmpfs_extra_conservative_mib": volume_memory,
        "host_control_rss_reservation_mib": 256,
        "conservative_total_memory_mib": memory + volume_memory + 256,
        "owned_storage_mib_max": memory + volume_memory + 10 + 1,
        "docker_log_mib_max": 10,
        "fixture_bundle_mib_max": 1,
        "shared_image_pull_footprint": "Separate root evidence required; not included in owned fixture/storage budget",
        "runtime_configuration_verified": False,
    }


def fixture_bytes() -> dict[str, bytes]:
    """Create tiny synthetic input, never copied production data or claimed expected output."""
    result: dict[str, bytes] = {}
    xml = {
        "artists": "<artists><artist><id>999000001</id><name>Owned Synthetic Artist</name></artist></artists>",
        "labels": "<labels><label><id>999000002</id><name>Owned Synthetic Label</name></label></labels>",
        "masters": '<masters><master id="999000003"><title>Owned Synthetic Master</title><main_release>999000004</main_release><year>2000</year></master></masters>',
        "releases": '<releases><release id="999000004"><title>Owned Synthetic Release</title><country>UK</country><released>2000-01-01</released><artists><artist><id>999000001</id><name>Owned Synthetic Artist</name></artist></artists><labels><label name="Owned Synthetic Label" catno="SMOKE-1" id="999000002"/></labels><master_id>999000003</master_id><formats><format name="Vinyl" qty="1"><descriptions><description>LP</description></descriptions></format></formats><identifiers><identifier type="Barcode" value="9990000000001"/></identifiers></release></releases>',
    }
    for i, (entity, body) in enumerate(xml.items(), 1):
        # Independent versions keep one-shot completed markers from suppressing the next entity.
        version = f"2000010{i}"
        filename = f"discogs_{version}_{entity}.xml.gz"
        compressed = gzip.compress(body.encode(), mtime=0)
        result[f"discogs/{filename}"] = compressed
        manifest = {
            "contract": "groovemap.discogs-extractor-smoke",
            "version": 1,
            "scope": "single_file",
            "input": {
                "path": filename,
                "media_type": "application/gzip",
                "sha256": hashlib.sha256(compressed).hexdigest(),
                "data_type": entity,
                "records": 1,
            },
        }
        result[f"discogs/manifest-{entity}.json"] = json.dumps(manifest).encode()
    result["http/index.html"] = f'<a href="{MB_VERSION}/">Synthetic version</a>'.encode()
    sums = []
    for entity, mbid in MB_IDS.items():
        native = {
            "artist": ("artist", "999000001"),
            "label": ("label", "999000002"),
            "release-group": ("master", "999000003"),
            "release": ("release", "999000004"),
        }[entity]
        row: dict[str, Any] = {
            "id": mbid,
            "name": f"Owned Synthetic {entity}",
            "relations": [{"type": "discogs", "target-type": "url", "url": {"resource": f"https://www.discogs.com/{native[0]}/{native[1]}"}}],
        }
        if entity in {"release", "release-group"}:
            row["title"] = row.pop("name")
        if entity == "release":
            row.update(
                {
                    "status": "Official",
                    "barcode": "9990000000001",
                    "release-group": {"id": MB_IDS["release-group"]},
                    "media": [{"format": "CD", "position": 1, "track-count": 1}],
                }
            )
        elif entity == "release-group":
            row.update({"primary-type": "Album", "first-release-date": "2000-01-01"})
        elif entity == "label":
            row.update({"type": "Original Production", "label-code": 77})
        rows = [row]
        if entity == "artist":
            row.update({"type": "Person", "gender": "Other"})
            target_id = "f0f0f0f0-0000-4000-8000-000000000005"
            row["relations"].append(
                {
                    "type": "member of band",
                    "direction": "forward",
                    "target-type": "artist",
                    "artist": {"id": target_id, "name": "Owned Synthetic Group"},
                }
            )
            rows.append(
                {
                    "id": target_id,
                    "name": "Owned Synthetic Group",
                    "type": "Group",
                    "relations": [{"type": "discogs", "target-type": "url", "url": {"resource": "https://www.discogs.com/artist/999000005"}}],
                }
            )
        content = ("\n".join(json.dumps(item) for item in rows) + "\n").encode()
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            info = tarfile.TarInfo(f"mbdump/{entity}")
            info.size = len(content)
            info.mtime = 0
            archive.addfile(info, io.BytesIO(content))
        compressed = lzma.compress(stream.getvalue())
        filename = f"{entity}.tar.xz"
        result[f"http/{MB_VERSION}/{filename}"] = compressed
        sums.append(f"{hashlib.sha256(compressed).hexdigest()}  {filename}")
    result[f"http/{MB_VERSION}/SHA256SUMS"] = ("\n".join(sums) + "\n").encode()
    require(sum(map(len, result.values())) <= 1024**2, "synthetic input byte cap exceeded")
    return result


def compose_model(context: dict[str, Any], images: dict[str, Any], provider: str) -> dict[str, Any]:
    """Render only a disposable lane, without external networks or production mounts."""
    owned, project = validate_inputs(context, images)
    require(provider in {"discogs", "musicbrainz"}, "unknown provider")
    resource_proof()
    labels = {"beadhive.probe.task": "gm-deployment-eeo", "beadhive.probe.owner": project}
    services: dict[str, Any] = {}
    environment = {
        "ENVIRONMENT": "production",
        "RABBITMQ_HOST": "broker",
        "RABBITMQ_USERNAME": "synthetic-smoke",
        "RABBITMQ_PASSWORD_FILE": "/fixtures/synthetic-password",
        "POSTGRES_HOST": "postgres",
        "POSTGRES_USERNAME": "synthetic-smoke",
        "POSTGRES_PASSWORD_FILE": "/fixtures/synthetic-password",
        "POSTGRES_DATABASE": "synthetic_smoke",
        "POSTGRES_POOL_MIN_SIZE": "1",
        "POSTGRES_POOL_MAX_SIZE": "2",
        "NEO4J_HOST": "neo4j",
        "NEO4J_USERNAME": "neo4j",
        "NEO4J_PASSWORD_FILE": "/fixtures/synthetic-password",
        "STARTUP_DELAY": "0",
        "CONSUMER_CANCEL_DELAY": "0",
        "POSTGRES_BATCH_SIZE": "1",
        "POSTGRES_BATCH_FLUSH_INTERVAL": "1",
        "NEO4J_BATCH_SIZE": "1",
        "NEO4J_BATCH_FLUSH_INTERVAL": "1",
        "OTEL_SDK_DISABLED": "true",
        "OTEL_METRICS_EXPORTER": "none",
        "OTEL_TRACES_EXPORTER": "none",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "",
    }
    pins = context["approved_auxiliary_pins"]
    runtime = images["consumers"][f"{provider}-sql"]["image"]
    for role, (cpus, memory, pids) in LIMITS.items():
        image = {
            "broker": pins["rabbitmq"],
            "postgres": pins["postgres"],
            "neo4j": pins["neo4j"],
            "producer": images["producers"][provider]["image"],
            "graph-consumer": images["consumers"][f"{provider}-graph"]["image"],
            "schema-graph": images["consumers"][f"{provider}-graph"]["image"],
        }.get(role, runtime)
        services[role] = {
            "image": image,
            "platform": "linux/amd64",
            "container_name": f"{project}-{role}",
            "labels": labels,
            "cpus": cpus,
            "mem_limit": f"{memory}m",
            "memswap_limit": f"{memory}m",
            "pids_limit": pids,
            "shm_size": "16m",
            "read_only": True,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "networks": ["isolated"],
            "restart": "no",
            "tmpfs": ["/tmp:rw,noexec,nosuid,size=8m"],
            "logging": {"driver": "json-file", "options": {"max-size": "1m", "max-file": "1"}},
            "volumes": [{"type": "bind", "source": owned + "/bundle", "target": "/fixtures", "read_only": True}],
        }
        if role not in {"broker", "postgres", "neo4j"}:
            services[role].update(user="1000:1000", environment=environment.copy())
            services[role]["tmpfs"].append("/logs:rw,noexec,nosuid,size=4m,uid=1000,gid=1000")
    services["broker"]["user"] = "999:999"
    services["broker"]["environment"] = {
        "RABBITMQ_DEFAULT_USER": "synthetic-smoke",
        "RABBITMQ_CONFIG_FILE": "/fixtures/rabbitmq",
        "RABBITMQ_SERVER_ADDITIONAL_ERL_ARGS": "+S 1:1 +SDcpu 1 +SDio 1",
        "RABBITMQ_CTL_ERL_ARGS": "+S 1:1 +SDcpu 1 +SDio 1",
    }
    services["broker"]["volumes"].append({"type": "volume", "source": "broker", "target": "/var/lib/rabbitmq"})
    services["broker"]["tmpfs"].append("/var/log/rabbitmq:rw,noexec,nosuid,size=4m,uid=999,gid=999")
    services["postgres"]["environment"] = {
        "POSTGRES_USER": "synthetic-smoke",
        "POSTGRES_PASSWORD_FILE": "/fixtures/synthetic-password",
        "POSTGRES_DB": "synthetic_smoke",
        "PGDATA": "/var/lib/postgresql/18/docker",
    }
    services["postgres"]["volumes"].append({"type": "volume", "source": "postgres", "target": "/var/lib/postgresql"})
    services["postgres"]["tmpfs"].append("/var/run/postgresql:rw,noexec,nosuid,size=4m,mode=1777")
    services["postgres"]["command"] = ["postgres", "-c", "max_wal_size=128MB", "-c", "min_wal_size=32MB", "-c", "max_connections=20"]
    services["neo4j"]["user"] = "7474:7474"
    services["neo4j"]["environment"] = {
        "NEO4J_AUTH_FILE": "/fixtures/synthetic-neo4j-auth",
        "NEO4J_server_http_enabled": "false",
        "NEO4J_server_https_enabled": "false",
        "NEO4J_server_bolt_enabled": "true",
        "JAVA_TOOL_OPTIONS": "-Djna.tmpdir=/var/lib/neo4j/native-tmp",
        "NEO4J_server_memory_heap_initial__size": "256m",
        "NEO4J_server_memory_heap_max__size": "256m",
        "NEO4J_server_memory_pagecache_size": "128m",
    }
    services["neo4j"]["volumes"].append({"type": "volume", "source": "neo4j", "target": "/data"})
    services["neo4j"]["volumes"].append({"type": "volume", "source": "neo4j-conf", "target": "/var/lib/neo4j/conf"})
    services["neo4j"]["tmpfs"].extend(
        [f"{path}:rw,noexec,nosuid,size=4m,uid=7474,gid=7474" for path in ["/logs", "/plugins", "/import", "/var/lib/neo4j/run"]]
    )
    services["neo4j"]["tmpfs"].append("/var/lib/neo4j/native-tmp:rw,exec,nosuid,nodev,size=4m,uid=7474,gid=7474,mode=700")
    # Image init routines may drop privileges or initialize empty owned store paths.
    for role in ("postgres",):
        services[role]["cap_add"] = ["CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"]
    services["fixture-http"].update(entrypoint=["/app/.venv/bin/python"], command=["-m", "http.server", "8000", "--directory", "/fixtures/http"])
    for role in ["schema-sql", "schema-graph", "observer"]:
        services[role]["entrypoint"] = ["/app/.venv/bin/python"]
        services[role]["command"] = ["/fixtures/driver.py", role, provider]
    for role in ["observer", "sql-consumer", "graph-consumer"]:
        services[role]["volumes"].append({"type": "volume", "source": "captures", "target": "/captures", "read_only": role != "observer"})
    for role in ["observer", "producer", "sql-consumer", "graph-consumer"]:
        services[role]["environment"].update({f"{provider.upper()}_EXCHANGE_PREFIX": images["prefixes"][provider]})
    data_target = "/discogs-data" if provider == "discogs" else "/musicbrainz-data"
    for role in ["producer", "fixture-http"]:
        # The HTTP keeper holds the bounded volume mount across producer stop/restart.
        services[role]["volumes"].append({"type": "volume", "source": "producer", "target": data_target})
    services["producer"]["environment"].update(
        {
            f"{provider.upper()}_ROOT": data_target,
            f"{provider.upper()}_EXCHANGE_PREFIX": images["prefixes"][provider],
            "FAILURE_COOLDOWN_SECS": "0",
            "MAX_WORKERS": "1",
            "BATCH_SIZE": "1",
        }
    )
    if provider == "musicbrainz":
        services["producer"]["environment"]["MUSICBRAINZ_DUMP_URL"] = "http://fixture-http:8000/"
    else:
        services["producer"]["command"] = ["--local-manifest", "/fixtures/discogs/manifest-releases.json"]
    return {
        "name": project,
        "services": services,
        "networks": {"isolated": {"internal": True, "labels": labels}},
        "volumes": {
            role: {"labels": labels, "driver": "local", "driver_opts": {"type": "tmpfs", "device": "tmpfs", "o": f"size={size}m,mode=1777"}}
            for role, size in DATA_VOLUMES.items()
        },
    }


def synthetic_password() -> str:
    """Keep URI-escaping sentinels within the pinned Neo4j AUTH grammar."""
    return "smoke-@:-" + uuid.uuid4().hex


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--images", type=Path, required=True)
    parser.add_argument("--legacy-schemas", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    context = json.loads(args.context.read_text())
    images = json.loads(args.images.read_text())
    validate_inputs(context, images)
    require(not args.output.exists(), "output must be absent; do not overwrite a reviewed bundle")
    sources = {name: (args.legacy_schemas / name).read_bytes() for name in SCHEMAS}
    require(all(hashlib.sha256(value).hexdigest() == SCHEMAS[name] for name, value in sources.items()), "exact legacy schema source mismatch")
    # No Docker call occurs here, including image inspection or runtime capability checks.
    args.output.mkdir(parents=True)
    for name, content in fixture_bytes().items():
        path = args.output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    for name, content in sources.items():
        (args.output / name).write_bytes(content)
    # These credentials are synthetic and generated only in the untracked owned bundle.
    secret = synthetic_password()
    for name, value in {
        "synthetic-password": secret,
        "synthetic-neo4j-auth": "neo4j/" + secret,
        "rabbitmq.conf": "default_user = synthetic-smoke\ndefault_pass = " + secret + "\n",
    }.items():
        path = args.output / name
        path.write_text(value + "\n")
        path.chmod(0o444)
    (args.output / "driver.py").write_bytes(Path(__file__).with_name("extractor_smoke_driver.py").read_bytes())
    for name in ["extractor_delivery.py", "extractor_smoke_prepare.py", "extractor_smoke_driver.py", "extractor_smoke_run.py"]:
        (args.output / name).write_bytes(Path(__file__).with_name(name).read_bytes())
    for provider in ["discogs", "musicbrainz"]:
        (args.output / f"compose-{provider}.yml").write_text(yaml.safe_dump(compose_model(context, images, provider), sort_keys=False))
    (args.output / "resource-proof.json").write_text(json.dumps(resource_proof(), indent=2) + "\n")
    print("Prepared synthetic bundle only; image capability, schema driver, ownership and runtime proof require independent review before execution.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
