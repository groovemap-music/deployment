"""Static and offline regression coverage for the released extractor-fixture smoke."""

from __future__ import annotations

import importlib.util
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[2]
OVERLAY = ROOT / "docker-compose.released-fixture-smoke.yml"
SCRIPT = ROOT / "scripts" / "smoke-released-fixture.sh"
RELEASE_DIGEST = "db418bfc97d2d364ac0e64045b492ad8492b500c84f8ce105a04cadd400ee17c"
FIXTURE_MANIFEST = "/usr/share/discogs-ingestion/contracts/extractor-smoke/v1/manifest.json"
EXPECTED_EVENTS = "/usr/share/discogs-ingestion/contracts/extractor-smoke/v1/expected-events.ndjson"


class ComposeLoader(yaml.SafeLoader):
    """Read Compose merge tags as the values they leave after a merge."""


def _compose_tag(loader: yaml.SafeLoader, node: yaml.Node) -> Any:
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    if not isinstance(node, yaml.ScalarNode):
        raise TypeError(f"unexpected Compose merge tag on {type(node).__name__}")
    scalar = loader.construct_scalar(node)
    return None if scalar in ("null", "~", "") else scalar


ComposeLoader.add_constructor("!override", _compose_tag)
ComposeLoader.add_constructor("!reset", _compose_tag)


def _load_smoke_media() -> Any:
    spec = importlib.util.spec_from_file_location("released_fixture_smoke_media", ROOT / "scripts" / "smoke_media.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


smoke_media = _load_smoke_media()


def _released_env() -> str:
    namespace = runpy.run_path(str(ROOT / "scripts" / "check-images.py"))
    registry = namespace["REGISTRY"]
    owners = namespace["IMAGE_OWNERS"]
    digests = namespace["RELEASED_IMAGE_DIGESTS"]
    return "".join(f"{variable}={registry}/{owners[variable]}@sha256:{digest}\n" for variable, digest in digests.items())


def test_reviewed_release_set_promotes_the_fixture_capable_image() -> None:
    namespace = runpy.run_path(str(ROOT / "scripts" / "check-images.py"))
    assert namespace["RELEASED_IMAGE_DIGESTS"]["DISCOGS_INGESTION_IMAGE"] == RELEASE_DIGEST


def test_overlay_selects_the_packaged_v1_manifest_and_isolates_state() -> None:
    overlay = yaml.load(OVERLAY.read_text(), Loader=ComposeLoader)  # noqa: S506
    services = overlay["services"]
    expected_services = {
        "rabbitmq",
        "postgres",
        "neo4j",
        "schema-init",
        "extractor-discogs",
        "tableinator",
        "graphinator",
    }
    assert set(services) == expected_services
    assert all(services[name]["container_name"] is None for name in expected_services)
    assert services["extractor-discogs"]["environment"]["LOCAL_MANIFEST"] == FIXTURE_MANIFEST
    assert services["extractor-discogs"]["environment"]["DATA_QUALITY_RULES"] is None
    assert services["extractor-discogs"]["volumes"] == ["discogs_data:/discogs-data", "extractor_discogs_logs:/logs"]
    assert services["rabbitmq"]["volumes"] == ["rabbitmq_data:/var/lib/rabbitmq"]
    assert [port for service in services.values() for port in service.get("ports", [])] == [
        "127.0.0.1:${SMOKE_RELEASED_FIXTURE_RABBITMQ_PORT:-15674}:15672"
    ]
    assert set(services["schema-init"]["depends_on"]) == {"postgres", "neo4j"}
    assert set(services["tableinator"]["depends_on"]) == {"schema-init", "rabbitmq", "postgres"}
    assert set(services["graphinator"]["depends_on"]) == {"schema-init", "rabbitmq", "neo4j"}


def test_recipe_is_operator_gated_and_statically_rendered() -> None:
    justfile = (ROOT / "Justfile").read_text()
    assert "smoke-released-fixture:\n    bash scripts/smoke-released-fixture.sh" in justfile
    assert "check: source-check typecheck test" in justfile
    assert "check_compose docker-compose.yml docker-compose.released-fixture-smoke.yml" in (ROOT / "scripts" / "check-compose.sh").read_text()
    for workflow in (ROOT / ".github" / "workflows").glob("*.yml"):
        assert "smoke-released-fixture" not in workflow.read_text()


def test_shell_adapter_starts_only_discogs_consumers_and_runs_the_extractor_mode() -> None:
    text = SCRIPT.read_text()
    assert "up -d --wait tableinator graphinator" in text
    assert "--extractor-service extractor-discogs" in text
    assert '--env-file "$env_file"' in text
    assert "trap cleanup EXIT" in text and "down --volumes --remove-orphans" in text


def test_env_gate_accepts_only_the_reviewed_release_set(tmp_path: Path) -> None:
    docker = tmp_path / "docker"
    docker.write_text("#!/usr/bin/env bash\nexit 1\n")
    docker.chmod(0o755)
    env_file = tmp_path / "released.env"
    env_file.write_text(_released_env())

    result = subprocess.run(
        ["/bin/bash", str(SCRIPT)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=os.environ | {"PATH": f"{tmp_path}:{os.environ['PATH']}", "SMOKE_RELEASED_FIXTURE_ENV_FILE": str(env_file)},
        check=False,
    )
    assert "smoke-released-fixture:" not in result.stderr

    env_file.write_text(_released_env().replace(RELEASE_DIGEST, "a" * 64))
    refused = subprocess.run(
        ["/bin/bash", str(SCRIPT)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=os.environ | {"PATH": f"{tmp_path}:{os.environ['PATH']}", "SMOKE_RELEASED_FIXTURE_ENV_FILE": str(env_file)},
        check=False,
    )
    assert refused.returncode == 2
    assert "DISCOGS_INGESTION_IMAGE must promote the reviewed release digest" in refused.stderr


class FixtureStack:
    """Offline stack double for the extractor-driven branch of the assertion."""

    def __init__(self) -> None:
        self.extractor_ran = False

    def queue_consumers(self, _queue: str) -> int:
        return 1

    def compose_run(
        self,
        _service: str,
        command: tuple[str, ...] | list[str] = (),
        *,
        entrypoint: str | None = None,
        timeout: float = 120.0,
    ) -> str:
        del timeout
        if entrypoint == "cat":
            assert list(command) == [EXPECTED_EVENTS]
            return json.dumps(
                {
                    "type": "data",
                    "id": "contract-discogs-releases",
                    "sha256": "4c3c408982a1a170762664d76aee5da1ff9a0191060c522b8152dd9eea6e5cac",
                    "title": "Contract Release",
                    "released": "2000-01-01",
                    "media": {"families": ["vinyl"], "items": [{"medium": "vinyl_12"}]},
                }
            )
        self.extractor_ran = True
        return ""

    def psql(self, sql: str) -> str:
        return '["vinyl"]' if "media->'families'" in sql else "t"

    def cypher(self, query: str) -> str:
        return '["vinyl"]' if "media_families AS value" in query else "1"


def test_extractor_mode_reads_expected_values_from_image_then_asserts_both_stores(monkeypatch: Any) -> None:
    stack = FixtureStack()
    monkeypatch.setattr(smoke_media, "Stack", lambda *_args, **_kwargs: stack)
    result = smoke_media.main(
        [
            "--project",
            "offline",
            "--env-file",
            "released.env",
            "--compose-file",
            "docker-compose.yml",
            "--compose-file",
            "docker-compose.released-fixture-smoke.yml",
            "--broker-port",
            "15674",
            "--timeout",
            "1",
            "--extractor-service",
            "extractor-discogs",
        ]
    )
    assert result == 0
    assert stack.extractor_ran
