"""Contracts for the isolated, real-container /logs tmpfs ownership probe."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "probe_logs_tmpfs.py"
SPEC = importlib.util.spec_from_file_location("probe_logs_tmpfs", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def _service(uid: int = 1001, gid: int = 1002, *, image: str = "example/app:latest") -> dict[str, Any]:
    return {
        "image": image,
        "user": f"{uid}:{gid}",
        "environment": {"LOG_FILE_MAX_BYTES": "20971520", "LOG_FILE_BACKUP_COUNT": "2"},
        "tmpfs": [f"/logs:rw,noexec,nosuid,uid={uid},gid={gid},mode=0750,size=64m"],
    }


def test_inventory_uses_configured_logs_mounts_and_fails_closed_for_file_writers() -> None:
    rendered = {
        "services": {
            "schema-init": _service(),
            "api": _service(),
            "neo4j": {
                "image": "neo4j:2026.10.0-community",
                "tmpfs": ["/logs:rw,noexec,nosuid,uid=7474,gid=7474,mode=0750,size=1g"],
            },
            # Rust extractors send logs to stdout today, but their /logs mounts
            # still need the runtime ownership proof.
            "extractor-discogs": {
                "image": "ghcr.io/groovemap-music/discogs-ingestion@sha256:abc",
                "user": "1001:1002",
                "tmpfs": ["/logs:uid=1001,gid=1002"],
            },
            # A profile omitted from this render must not be probed in this phase.
        }
    }

    assert probe.log_writer_inventory(rendered) == [
        ("schema-init", "1001", "1002"),
        ("api", "1001", "1002"),
        ("neo4j", "7474", "7474"),
        ("extractor-discogs", "1001", "1002"),
    ]


def test_inventory_includes_the_embeddings_filesystem_probe_when_jobs_profile_is_rendered() -> None:
    rendered = {"services": {"embeddings": _service()}}

    assert probe.log_writer_inventory(rendered) == [("embeddings", "1001", "1002")]


def test_inventory_rejects_a_python_writer_without_exactly_one_logs_tmpfs() -> None:
    rendered = {"services": {"api": {**_service(), "tmpfs": []}}}

    with pytest.raises(probe.ProbeError, match="api: expected exactly one tmpfs mount at /logs"):
        probe.log_writer_inventory(rendered)


def test_inventory_rejects_an_old_named_logs_volume() -> None:
    rendered = {
        "services": {
            "api": {
                **_service(),
                "tmpfs": [],
                "volumes": [{"type": "volume", "source": "api_logs", "target": "/logs"}],
            }
        }
    }

    with pytest.raises(probe.ProbeError, match="api: expected exactly one tmpfs mount at /logs"):
        probe.log_writer_inventory(rendered)


def test_inventory_rejects_mismatch_between_tmpfs_and_runtime_uid_gid() -> None:
    rendered = {"services": {"api": _service(uid=1000, gid=1000)}}
    rendered["services"]["api"]["tmpfs"] = ["/logs:uid=1001,gid=1002,size=64m"]

    with pytest.raises(probe.ProbeError, match="do not match runtime"):
        probe.log_writer_inventory(rendered)


def test_probe_command_overrides_entrypoint_and_never_starts_dependencies() -> None:
    [command] = probe.probe_commands(
        ["docker", "compose", "--project-name", "gm-smoke", "--profile", "jobs"],
        [("embeddings", "1001", "1002")],
    )

    assert command[:7] == ["docker", "compose", "--project-name", "gm-smoke", "--profile", "jobs", "run"]
    assert "--rm" in command
    assert "--no-deps" in command
    assert command[command.index("--user") + 1] == "1001:1002"
    assert command[command.index("--entrypoint") + 1] == "/bin/sh"
    assert command[-3:] == ["logs-tmpfs-probe", "1001", "1002"]
    assert "awk" in command[-4]
    assert "mountinfo" in command[-4]
    assert "tmpfs" in command[-4]
    assert 'rm "$probe"' in command[-4]


def test_runtime_probes_execute_each_writer_and_report_only_success(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rendered = tmp_path / "rendered.json"
    rendered.write_text(
        json.dumps(
            {
                "services": {
                    "schema-init": _service(),
                    "neo4j": {"image": "neo4j:2026.10.0-community", "tmpfs": ["/logs:uid=7474,gid=7474,size=1g"]},
                }
            }
        )
    )
    calls: list[list[str]] = []

    def successful_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        assert set(kwargs) == {"capture_output", "text", "check", "env"}
        assert (kwargs["capture_output"], kwargs["text"], kwargs["check"]) == (True, True, False)
        assert "COMPOSE_FILE" not in kwargs["env"] and "COMPOSE_PROFILES" not in kwargs["env"]
        return subprocess.CompletedProcess(command, 0, "", "")

    probe.run_probes(rendered, ["docker", "compose"], runner=successful_run)

    assert [call[call.index("--entrypoint") + 2] for call in calls] == ["schema-init", "neo4j"]
    assert all("--no-deps" in call for call in calls)
    assert "tmpfs" in capsys.readouterr().out


def test_runtime_probe_fails_with_service_context_without_printing_success(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rendered = tmp_path / "rendered.json"
    rendered.write_text(json.dumps({"services": {"api": _service()}}))

    def failed_run(command: list[str], *, capture_output: bool, text: bool, check: bool, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        assert (capture_output, text, check) == (True, True, False)
        assert "COMPOSE_FILE" not in env and "COMPOSE_PROFILES" not in env
        return subprocess.CompletedProcess(command, 1, "", "permission denied")

    with pytest.raises(probe.ProbeError, match=r"(?s)api: /logs tmpfs runtime ownership/write probe failed \(exit 1\).*permission denied"):
        probe.run_probes(rendered, ["docker", "compose"], runner=failed_run)
    assert capsys.readouterr().out == ""


def test_running_services_are_inspected_and_probed_in_place_while_one_shots_use_fresh_entrypoint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rendered = tmp_path / "rendered.json"
    rendered.write_text(
        json.dumps(
            {
                "services": {
                    "api": _service(),
                    "schema-init": {**_service(), "restart": "no"},
                }
            }
        )
    )
    inspected = tmp_path / "inspect.json"
    inspected.write_text(
        json.dumps(
            [
                {
                    "Id": "a" * 64,
                    "Config": {"Labels": {"com.docker.compose.service": "api", "com.docker.compose.oneoff": "False"}},
                    "State": {"Status": "running"},
                    "HostConfig": {"Tmpfs": {"/logs": "rw,noexec,nosuid,uid=1001,gid=1002,mode=0750,size=64m"}},
                    # Docker commonly omits tmpfs mounts from Mounts while
                    # reporting them in HostConfig.Tmpfs.
                    "Mounts": [],
                }
            ]
        )
    )
    calls: list[list[str]] = []

    def successful_run(command: list[str], *, capture_output: bool, text: bool, check: bool, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        assert (capture_output, text, check) == (True, True, False)
        assert "COMPOSE_FILE" not in env and "COMPOSE_PROFILES" not in env
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    probe.run_probes(rendered, ["docker", "compose", "--project-name", "gm-smoke"], inspected, successful_run)

    assert calls[0][:6] == ["docker", "exec", "--user", "1001:1002", "a" * 64, "/bin/sh"]
    assert calls[1][:7] == ["docker", "compose", "--project-name", "gm-smoke", "run", "--rm", "--no-deps"]
    assert calls[1][calls[1].index("--entrypoint") + 1] == "/bin/sh"
    assert "ok api" in capsys.readouterr().out


def test_existing_writer_with_wrong_actual_mount_fails_without_fresh_probe(tmp_path: Path) -> None:
    rendered = tmp_path / "rendered.json"
    rendered.write_text(json.dumps({"services": {"api": _service()}}))
    inspected = tmp_path / "inspect.json"
    inspected.write_text(
        json.dumps(
            [
                {
                    "Id": "b" * 64,
                    "Config": {"Labels": {"com.docker.compose.service": "api", "com.docker.compose.oneoff": "False"}},
                    "State": {"Status": "running"},
                    "HostConfig": {"Tmpfs": {}},
                    "Mounts": [{"Destination": "/logs", "Type": "volume", "RW": True}],
                }
            ]
        )
    )

    def unexpected_run(command: list[str], *, capture_output: bool, text: bool, check: bool, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        assert (capture_output, text, check) == (True, True, False)
        assert "COMPOSE_FILE" not in env and "COMPOSE_PROFILES" not in env
        raise AssertionError(f"must fail the inspected container, not pass via fresh run: {command}")

    with pytest.raises(probe.ProbeError, match=r"HostConfig\.Tmpfs has no /logs entry"):
        probe.run_probes(rendered, ["docker", "compose"], inspected, unexpected_run)


def test_existing_writer_mount_options_must_match_the_compose_render(tmp_path: Path) -> None:
    rendered = tmp_path / "rendered.json"
    rendered.write_text(json.dumps({"services": {"api": _service()}}))
    inspected = tmp_path / "inspect.json"
    inspected.write_text(
        json.dumps(
            [
                {
                    "Id": "c" * 64,
                    "Config": {"Labels": {"com.docker.compose.service": "api", "com.docker.compose.oneoff": "False"}},
                    "State": {"Status": "running"},
                    "HostConfig": {"Tmpfs": {"/logs": "rw,noexec,nosuid,uid=1001,gid=1002,mode=0750,size=32m"}},
                    "Mounts": [{"Destination": "/logs", "Type": "tmpfs", "RW": True}],
                }
            ]
        )
    )

    def unexpected_run(command: list[str], *, capture_output: bool, text: bool, check: bool, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        assert (capture_output, text, check) == (True, True, False)
        assert "COMPOSE_FILE" not in env and "COMPOSE_PROFILES" not in env
        raise AssertionError(f"must fail the inspected container, not pass via fresh run: {command}")

    with pytest.raises(probe.ProbeError, match=r"HostConfig\.Tmpfs size does not match the Compose render"):
        probe.run_probes(rendered, ["docker", "compose"], inspected, unexpected_run)


def test_live_probe_rejects_a_conflicting_non_tmpfs_mount_at_logs() -> None:
    container = {
        "Id": "d" * 64,
        "HostConfig": {"Tmpfs": {"/logs": "rw,noexec,nosuid,uid=1001,gid=1002,mode=0750,size=64m"}},
        "Mounts": [{"Destination": "/logs", "Type": "volume", "RW": True}],
    }

    with pytest.raises(probe.ProbeError, match=r"running container Mounts has a conflicting /logs mount"):
        probe._check_live_mount("api", container, {"uid": "1001", "gid": "1002", "mode": "0750", "size": "64m", "noexec": "", "nosuid": ""})
