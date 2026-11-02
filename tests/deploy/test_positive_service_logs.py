"""Contracts for positive smoke-log capture and permission-denied scanning."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_positive_service_logs.py"
SPEC = importlib.util.spec_from_file_location("check_positive_service_logs", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
logs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(logs)


def _container(service: str, suffix: str, *, oneoff: bool = False) -> dict[str, Any]:
    return {
        "Id": suffix * 64,
        "Config": {
            "Labels": {
                "com.docker.compose.service": service,
                "com.docker.compose.oneoff": "True" if oneoff else "False",
            }
        },
    }


def test_container_inventory_uses_inspected_service_ids_and_skips_oneoffs() -> None:
    inspect = [
        _container("api", "a"),
        _container("schema-init", "b"),
        _container("extractor-discogs", "c", oneoff=True),
    ]

    assert logs.service_containers(inspect) == [("api", "a" * 64), ("schema-init", "b" * 64)]


def test_capture_scans_and_saves_only_inspected_positive_service_logs(tmp_path: Path) -> None:
    inspect_path = tmp_path / "inspect.json"
    inspect_path.write_text(json.dumps([_container("api", "a"), _container("probe", "b", oneoff=True)]))
    calls: list[list[str]] = []

    def successful_logs(command: list[str], *, capture_output: bool, text: bool, check: bool) -> subprocess.CompletedProcess[str]:
        assert (capture_output, text, check) == (True, True, False)
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "api started\n", "")

    captured = logs.capture_and_check(inspect_path, tmp_path / "evidence", runner=successful_logs)

    assert calls == [["docker", "logs", "--timestamps", "a" * 64]]
    assert [path.name for path in captured] == [f"api-{'a' * 12}.log"]
    assert captured[0].read_text() == "api started\n"
    assert "PASS scanned 1" in (tmp_path / "evidence" / "permission-log-check.txt").read_text()


@pytest.mark.parametrize("line", ["Permission denied opening /logs/api.log", "PermissionError: /logs is read-only"])
def test_permission_failures_fail_and_retain_the_actual_log(tmp_path: Path, line: str) -> None:
    inspect_path = tmp_path / "inspect.json"
    inspect_path.write_text(json.dumps([_container("api", "a")]))

    def log_with_permission_failure(command: list[str], *, capture_output: bool, text: bool, check: bool) -> subprocess.CompletedProcess[str]:
        assert (capture_output, text, check) == (True, True, False)
        return subprocess.CompletedProcess(command, 0, f"service booted\n{line}\n", "")

    with pytest.raises(logs.LogCheckError, match="positive service log scan found failures"):
        logs.capture_and_check(inspect_path, tmp_path / "evidence", runner=log_with_permission_failure)

    evidence_log = tmp_path / "evidence" / f"api-{'a' * 12}.log"
    assert line in evidence_log.read_text()
    report = (tmp_path / "evidence" / "permission-log-check.txt").read_text()
    assert "FAIL positive service log scan" in report
    assert "api" in report and evidence_log.name in report


def test_docker_logs_failure_is_reported_without_fabricating_success(tmp_path: Path) -> None:
    inspect_path = tmp_path / "inspect.json"
    inspect_path.write_text(json.dumps([_container("neo4j", "d")]))

    def failed_logs(command: list[str], *, capture_output: bool, text: bool, check: bool) -> subprocess.CompletedProcess[str]:
        assert (capture_output, text, check) == (True, True, False)
        return subprocess.CompletedProcess(command, 1, "", "daemon error")

    with pytest.raises(logs.LogCheckError, match="positive service log scan found failures"):
        logs.capture_and_check(inspect_path, tmp_path / "evidence", runner=failed_logs)

    assert "docker logs exited 1" in (tmp_path / "evidence" / "permission-log-check.txt").read_text()


def test_invalid_or_empty_inspect_snapshots_fail_closed(tmp_path: Path) -> None:
    inspect_path = tmp_path / "inspect.json"
    inspect_path.write_text("[]")

    with pytest.raises(logs.LogCheckError, match="no positive Compose service containers"):
        logs.capture_and_check(inspect_path, tmp_path / "evidence")
