"""Regression tests for false-positive frame evidence and fail-closed owned cleanup."""

from __future__ import annotations

import ast
import asyncio
import copy
import errno
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest


def load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[2] / "scripts" / f"{name}.py")
    assert spec and spec.loader
    result = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = result
    spec.loader.exec_module(result)
    return result


driver = load("extractor_smoke_driver")
runner = load("extractor_smoke_run")


def frames(provider: str) -> list[dict[str, Any]]:
    result = []
    mapping = driver.expected_ids(provider)
    for entity, identities in mapping.items():
        for identity in sorted(identities):
            if provider == "discogs":
                field = "name" if entity in {"artists", "labels"} else "title"
                event: dict[str, Any] = {
                    field: "Owned Synthetic " + {"artists": "Artist", "labels": "Label", "masters": "Master", "releases": "Release"}[entity]
                }
            else:
                suffix = {"artists": "artist", "labels": "label", "release-groups": "master", "releases": "release"}[entity]
                native = "masters" if entity == "release-groups" else entity
                event = {
                    **driver.expected_mb_fields(entity, identity),
                    f"discogs_{suffix}_id": 999000005 if identity.endswith("000005") else int(driver.DISCOGS_IDS[native]),
                }
            event.update(type="data", id=identity, sha256="a" * 64)
            if entity == "releases":
                event["media"] = {"taxonomy_version": "1"}
                if provider == "musicbrainz":
                    event.update(release_group_mbid=driver.MB_IDS["release-groups"], status="Official")
            result.append({"entity": entity, "event": event, "persistent": True})
        result.append(
            {"entity": entity, "persistent": True, "event": {"type": "file_complete", "data_type": entity, "total_processed": len(identities)}}
        )
        versions = (
            [(f"2000010{i}", {name: 1}) for i, name in enumerate(mapping, 1)]
            if provider == "discogs"
            else [("20000101-000000", {name: len(ids) for name, ids in mapping.items()})]
        )
        for version, counts in versions:
            result.append(
                {
                    "entity": entity,
                    "persistent": True,
                    "event": {
                        "type": "extraction_complete",
                        "version": version,
                        "record_counts": counts,
                        "timestamp": "2000-01-01T00:00:00Z",
                        "started_at": "2000-01-01T00:00:00Z",
                    },
                }
            )
    return result


@pytest.mark.parametrize("provider", ["discogs", "musicbrainz"])
def test_synthetic_complete_frame_matrix_is_structurally_valid(provider: str) -> None:
    assert driver.validate_frames(provider, frames(provider))["actual_completion"] is True


@pytest.mark.parametrize("provider", ["discogs", "musicbrainz"])
@pytest.mark.parametrize("kind", ["data", "file_complete", "extraction_complete"])
def test_missing_or_duplicate_actual_frame_cannot_pass(provider: str, kind: str) -> None:
    original = frames(provider)
    index = next(i for i, frame in enumerate(original) if frame["event"]["type"] == kind)
    missing = copy.deepcopy(original)
    missing.pop(index)
    duplicate = copy.deepcopy(original)
    duplicate.append(copy.deepcopy(original[index]))
    for invalid in [missing, duplicate]:
        with pytest.raises(ValueError):
            driver.validate_frames(provider, invalid)


@pytest.mark.parametrize("mutation", ["name", "hash", "id", "file_count", "run_count", "persistent", "relation"])
def test_wrong_sentinel_or_completion_evidence_is_rejected(mutation: str) -> None:
    captured = frames("musicbrainz")
    first = captured[0]
    if mutation == "name":
        first["event"]["name"] = "unrelated record"
    elif mutation == "hash":
        first["event"]["sha256"] = "not-a-hash"
    elif mutation == "id":
        first["event"]["id"] = "wrong-sentinel"
    elif mutation == "persistent":
        first["persistent"] = False
    elif mutation == "relation":
        first["event"]["discogs_artist_id"] = 123
    else:
        event = next(
            frame["event"] for frame in captured if frame["event"]["type"] == ("file_complete" if mutation == "file_count" else "extraction_complete")
        )
        if mutation == "file_count":
            event["total_processed"] += 1
        else:
            event["record_counts"]["artists"] += 1
    with pytest.raises(ValueError):
        driver.validate_frames("musicbrainz", captured)


def bare_lane() -> Any:
    lane = runner.Lane.__new__(runner.Lane)
    lane.project = "gm-eeo-smoke-012345abcdef"
    lane.created = {"container": {}, "network": {}, "volume": {}}
    lane.deadline = runner.time.monotonic() + 900
    return lane


def owned_container(lane: Any) -> dict[str, Any]:
    return {"Id": "a" * 64, "Config": {"Labels": {runner.OWNER_KEY: lane.project, runner.TASK_KEY: "gm-deployment-eeo"}}}


@pytest.mark.parametrize("wrong", ["owner", "task", "changed-id"])
def test_cleanup_never_removes_unverified_or_replaced_container(monkeypatch: pytest.MonkeyPatch, wrong: str) -> None:
    lane = bare_lane()
    name = lane.project + "-producer"
    metadata = owned_container(lane)
    if wrong == "changed-id":
        lane.created["container"][name] = "b" * 64
    else:
        metadata["Config"]["Labels"][runner.OWNER_KEY if wrong == "owner" else runner.TASK_KEY] = "unowned"
    commands = []
    monkeypatch.setattr(lane, "inspect", lambda *_args, **_kwargs: metadata)
    monkeypatch.setattr(lane, "command", lambda argv, **_kwargs: commands.append(argv))
    with pytest.raises(ValueError):
        lane.remove_container(name)
    assert commands == []


def test_cleanup_continues_other_owned_resources_after_one_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    lane = bare_lane()
    monkeypatch.setattr(lane, "names", lambda: {"container": ["first", "second"], "network": [], "volume": []})
    visited = []

    def remove(name: str) -> None:
        visited.append(name)
        if name == "first":
            raise ValueError("ownership not established")

    monkeypatch.setattr(lane, "remove_container", remove)
    with pytest.raises(ValueError, match="container:first"):
        lane.cleanup()
    assert visited == ["first", "second"]


def test_deadline_rejects_new_commands_before_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    lane = bare_lane()
    lane.deadline = 0
    lane.work_deadline = 0
    calls = []
    monkeypatch.setattr(runner.subprocess, "run", lambda *args, **_kwargs: calls.append(args))
    with pytest.raises(ValueError, match="deadline"):
        lane.command(["info"])
    assert calls == []


def test_queue_dead_letters_fail_acceptance(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    lane = bare_lane()
    monkeypatch.setattr(lane, "check_capture_health", lambda: None)
    monkeypatch.setattr(
        lane,
        "command",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 0, json.dumps([{"name": "synthetic.dlq", "messages_ready": 1, "messages_unacknowledged": 0}]), ""
        ),
    )
    with pytest.raises(ValueError, match="dead-lettered"):
        lane.queues_empty()


@pytest.mark.asyncio
async def test_observer_callback_failure_is_durable_and_stops_capture(tmp_path: Path) -> None:
    import asyncio

    stop = asyncio.Event()
    failure = tmp_path / "failed"

    async def body(_message: Any, _entity: str) -> None:
        raise ValueError("bad actual synthetic frame")

    await driver.consume_owned(body, object(), "artists", stop, failure)
    assert stop.is_set()
    assert failure.read_text() == "owned synthetic callback assertion failed\n"


def partial_scenario() -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any], dict[str, Any]]:
    release = next(frame["event"] for frame in frames("musicbrainz") if frame["entity"] == "releases" and frame["event"]["type"] == "data")
    delta = [
        {"entity": "releases", "persistent": True, "event": release},
        {"entity": "releases", "persistent": True, "event": {"type": "file_complete", "total_processed": 1}},
    ]
    for entity in driver.ENTITIES["musicbrainz"]:
        delta.append(
            {
                "entity": entity,
                "persistent": True,
                "event": {
                    "type": "extraction_complete",
                    "version": "20000101-000000",
                    "started_at": "2000-01-01T00:00:00Z",
                    "record_counts": {"artists": 2, "labels": 1, "release-groups": 1, "releases": 1},
                },
            }
        )
    partial: dict[str, Any] = {
        "current_version": "20000101-000000",
        "processing_phase": {
            "started_at": "2000-01-01T00:00:00+00:00",
            "files_processed": 3,
            "records_extracted": 4,
            "progress_by_file": {
                "artist.jsonl.xz": {"status": "completed", "records_extracted": 2, "source_checksum": "a" * 64},
                "release.jsonl.xz": {"status": "pending", "records_extracted": 0, "source_checksum": "b" * 64},
            },
        },
    }
    final = copy.deepcopy(partial)
    final["summary"] = {"overall_status": "completed"}
    final["processing_phase"].update(files_processed=4, records_extracted=5)
    final["processing_phase"]["progress_by_file"]["release.jsonl.xz"].update(status="completed", records_extracted=1)
    return delta, release, partial, final


def test_partial_resume_requires_exact_delta_counts_hash_start_and_completed_progress() -> None:
    driver.validate_resume_delta("musicbrainz", *partial_scenario())


@pytest.mark.parametrize(
    "failure",
    ["duplicate-data", "missing-completion", "wrong-counts", "reset-start", "changed-hash", "changed-completed-progress", "wrong-final-counter"],
)
def test_partial_resume_false_positives_fail_closed(failure: str) -> None:
    delta, release, partial, final = partial_scenario()
    if failure == "duplicate-data":
        delta.append(copy.deepcopy(delta[0]))
    elif failure == "missing-completion":
        delta.pop()
    elif failure == "wrong-counts":
        delta[-1]["event"]["record_counts"]["artists"] = 0
    elif failure == "reset-start":
        delta[-1]["event"]["started_at"] = "2000-02-01T00:00:00Z"
    elif failure == "changed-hash":
        delta[0] = copy.deepcopy(delta[0])
        delta[0]["event"]["sha256"] = "b" * 64
    elif failure == "changed-completed-progress":
        final["processing_phase"]["progress_by_file"]["artist.jsonl.xz"]["records_extracted"] = 0
    else:
        final["processing_phase"]["records_extracted"] = 10
    with pytest.raises(ValueError):
        driver.validate_resume_delta("musicbrainz", delta, release, partial, final)


def test_host_executor_cleanup_remains_available_after_work_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    delivery = load("extractor_delivery")
    lane = bare_lane()
    lane.executor = ["/usr/bin/sudo", "-n", "/usr/bin/docker"]
    lane.work_deadline, lane.deadline = 780, 900
    clock = 0.0
    owner = ""
    image = "ghcr.io/groovemap-music/discogs-ingestion@sha256:" + "a" * 64
    called = []
    monkeypatch.setattr(runner.time, "monotonic", lambda: clock)

    def execute(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal clock, owner
        assert command[:3] == lane.executor
        args = command[3:]
        called.append(args)
        if args[:2] == ["image", "inspect"]:
            value = {
                "Architecture": "amd64",
                "Os": "linux",
                "Config": {"Labels": {"org.opencontainers.image.revision": "b" * 40}, "Entrypoint": ["discogs-ingestion"]},
            }
            return subprocess.CompletedProcess(command, 0, json.dumps([value]), "")
        if args[0] == "create":
            owner = args[args.index("--label") + 1].split("=", 1)[1]
            clock = 781
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        assert kwargs["timeout"] <= 5
        if args[:2] == ["container", "inspect"]:
            value = {
                "Id": "c" * 64,
                "Config": {"Image": image, "Labels": {"beadhive.probe.owner": owner, "beadhive.probe.task": "gm-deployment-eeo"}},
            }
            return subprocess.CompletedProcess(command, 0, json.dumps([value]), "")
        assert args == ["rm", "--force", "c" * 64]
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(runner.subprocess, "run", execute)
    with pytest.raises(subprocess.TimeoutExpired):
        delivery.isolated_startup_probe(image, "discogs", "remote-lux-sudo-docker", "b" * 40, executor=lane.command)
    assert called[-1] == ["rm", "--force", "c" * 64]
    clock = 900
    with pytest.raises(ValueError, match="deadline"):
        lane.command(["rm", "--force", "c" * 64], cleanup=True)


def test_wrong_prefixed_mb_name_rejected() -> None:
    captured = frames("musicbrainz")
    captured[0]["event"]["name"] = "Owned Synthetic Wrong"
    with pytest.raises(ValueError, match="sentinel fields"):
        driver.validate_frames("musicbrainz", captured)


@pytest.mark.parametrize("entity", ["artists", "labels", "release-groups", "releases"])
def test_missing_non_null_legacy_graph_projection_rejected(entity: str) -> None:
    event = next(frame["event"] for frame in frames("musicbrainz") if frame["entity"] == entity and frame["event"]["type"] == "data")
    with pytest.raises(ValueError, match="projection lost"):
        driver.validate_mb_graph(entity, event, {"mbid": event["id"]})


@pytest.mark.parametrize(
    "name",
    ["extractor_delivery.py", "extractor_managed_fragment.py", "extractor_smoke_run.py", "extractor_smoke_driver.py", "extractor_smoke_prepare.py"],
)
def test_remote_helpers_parse_on_python313(name: str) -> None:
    source = (Path(__file__).resolve().parents[2] / "scripts" / name).read_text()
    ast.parse(source, filename=name, feature_version=(3, 13))


def test_python313_parser_rejects_bare_multi_exception() -> None:
    with pytest.raises(SyntaxError):
        ast.parse("try: pass\nexcept ValueError, OSError: pass\n", feature_version=(3, 13))


def test_auxiliary_exit_fails_before_readiness_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    lane = bare_lane()
    metadata = owned_container(lane)
    metadata["State"] = {"Running": False, "ExitCode": 1}
    monkeypatch.setattr(lane, "owned_identity", lambda *_args, **_kwargs: metadata)
    with pytest.raises(ValueError, match="auxiliary exited: broker"):
        lane.require_auxiliary_running()


def test_failure_diagnostics_redacts_secret_and_connection_before_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    lane = bare_lane()
    lane.bundle = tmp_path / "bundle"
    lane.bundle.mkdir()
    (lane.bundle / "synthetic-password").write_text("synthetic-sensitive")
    lane.owned = str(tmp_path)
    lane.provider = "discogs"
    lane.context = {"context": {"name": "host-native"}}
    metadata = owned_container(lane)
    metadata["State"] = {"Running": False, "ExitCode": 1, "OOMKilled": False}
    monkeypatch.setattr(lane, "owned_identity", lambda *_args, **kwargs: metadata if kwargs["cleanup"] else None)

    def command(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert argv == ["logs", "--tail", "40", metadata["Id"]] and kwargs["cleanup"] is True
        return subprocess.CompletedProcess(argv, 0, "error synthetic-sensitive amqp://user:secret@broker bad", "readonly filesystem")

    monkeypatch.setattr(lane, "command", command)
    lane.failure_diagnostics()
    text = (tmp_path / "failure-diagnostics.json").read_text()
    assert "synthetic-sensitive" not in text and "user:secret" not in text
    assert "readonly filesystem" in text and '"exit_code": 1' in text
    for role in ["schema-sql", "schema-graph", "observer", "sql-consumer", "graph-consumer", "producer"]:
        assert lane.project + "-" + role in text


def test_failure_diagnostics_never_reads_logs_for_unowned_resource(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lane = bare_lane()
    lane.bundle = tmp_path
    lane.owned = str(tmp_path)
    lane.provider = "discogs"
    lane.context = {"context": {"name": "host-native"}}
    (tmp_path / "synthetic-password").write_text("synthetic-sensitive")

    def reject(*_args: Any, **_kwargs: Any) -> None:
        raise ValueError("unowned")

    monkeypatch.setattr(lane, "owned_identity", reject)
    monkeypatch.setattr(lane, "command", lambda *_args, **_kwargs: pytest.fail("unowned logs read"))
    lane.failure_diagnostics()
    assert "unavailable" in (tmp_path / "failure-diagnostics.json").read_text()


def test_diagnostics_reserve_cleanup_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lane = bare_lane()
    lane.bundle = tmp_path
    lane.owned = str(tmp_path)
    lane.provider = "discogs"
    lane.context = {"context": {"name": "host-native"}}
    lane.deadline = runner.time.monotonic() + 89
    (tmp_path / "synthetic-password").write_text("synthetic-sensitive")
    monkeypatch.setattr(lane, "owned_identity", lambda *_args, **_kwargs: pytest.fail("cleanup reservation consumed"))
    lane.failure_diagnostics()
    assert "unavailable" in (tmp_path / "failure-diagnostics.json").read_text()


class StartupUnavailable(Exception):
    pass


class ReadyGraph:
    def __init__(self, failures: list[Exception]) -> None:
        self.failures = failures
        self.calls: list[str] = []

    async def verify_connectivity(self) -> None:
        self.calls.append("connect")
        if self.failures:
            raise self.failures.pop(0)

    def session(self) -> Any:
        return self

    async def __aenter__(self) -> Any:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        pass

    async def run(self, query: str) -> Any:
        assert query == "RETURN 1 AS ready"
        self.calls.append("authenticated-query")
        return self

    async def single(self) -> dict[str, int]:
        return {"ready": 1}


def test_graph_readiness_retries_only_wrapped_socket_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    wrapped = StartupUnavailable("socket unavailable")
    wrapped.__cause__ = ConnectionRefusedError(errno.ECONNREFUSED, "not yet listening")
    graph = ReadyGraph([wrapped])

    async def immediate(_seconds: float) -> None:
        pass

    monkeypatch.setattr(driver.asyncio, "sleep", immediate)
    asyncio.run(driver.await_graph_ready(graph, StartupUnavailable))
    assert graph.calls == ["connect", "connect", "authenticated-query"]


@pytest.mark.parametrize("error", [ValueError("authentication rejected"), StartupUnavailable("unsupported Bolt configuration")])
def test_graph_readiness_does_not_retry_auth_or_configuration(error: Exception) -> None:
    graph = ReadyGraph([error])
    with pytest.raises(type(error)):
        asyncio.run(driver.await_graph_ready(graph, StartupUnavailable))
    assert graph.calls == ["connect"]


def test_graph_readiness_attempt_has_finite_timeout() -> None:
    class HungGraph(ReadyGraph):
        async def verify_connectivity(self) -> None:
            await asyncio.sleep(60)

    with pytest.raises(TimeoutError):
        asyncio.run(driver.await_graph_ready(HungGraph([]), StartupUnavailable, seconds=0.01))


def test_capture_queue_is_exclusive_to_observer_connection() -> None:
    class StrictChannel:
        async def declare_queue(self, name: str, *, durable: bool = False, exclusive: bool = False, auto_delete: bool = False) -> dict[str, Any]:
            if not durable and not exclusive:
                raise ValueError("transient nonexclusive queue disallowed")
            return {"name": name, "durable": durable, "exclusive": exclusive, "auto_delete": auto_delete}

    channel = StrictChannel()
    with pytest.raises(ValueError, match="nonexclusive"):
        asyncio.run(channel.declare_queue("owned-capture-artists", durable=False, auto_delete=False))
    queue = asyncio.run(driver.capture_queue(channel, "artists"))
    assert queue == {"name": "owned-capture-artists", "durable": False, "exclusive": True, "auto_delete": True}


def test_observer_exit_fails_before_ready_file_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    lane = bare_lane()
    metadata = owned_container(lane)
    metadata["State"] = {"Running": False, "ExitCode": 1}
    monkeypatch.setattr(lane, "owned_identity", lambda *_args, **_kwargs: metadata)
    monkeypatch.setattr(lane, "command", lambda *_args, **_kwargs: pytest.fail("dead observer readiness retried"))
    with pytest.raises(ValueError, match="observer exited"):
        lane.observer_ready()


@pytest.mark.parametrize("year", [None, "2000", 1999])
def test_sql_year_contract_does_not_accept_missing_raw_or_wrong_year(year: Any) -> None:
    event = {"id": "999000003", "sha256": "a" * 64, "year": "2000", "title": "Owned Synthetic Master"}
    with pytest.raises(ValueError, match="normalized year"):
        driver.validate_discogs_sql_payload("masters", event, {"year": year, "title": event["title"]})


def test_sql_normalization_preserves_all_other_fields_and_relationships() -> None:
    event = {"id": "999000004", "sha256": "a" * 64, "released": "2000-01-01", "title": "Owned Synthetic Release", "artists": [{"id": "999000001"}]}
    persisted = {"year": 2000, "released": event["released"], "title": event["title"], "artists": event["artists"]}
    driver.validate_discogs_sql_payload("releases", event, persisted)
    persisted["artists"] = []
    with pytest.raises(ValueError, match="relationships lost"):
        driver.validate_discogs_sql_payload("releases", event, persisted)
    driver.validate_discogs_sql_payload("masters", {"year": "2000", "title": "sentinel"}, {"year": 2000, "title": "sentinel"})


def test_verifier_errors_preserve_first_and_latest_bounded_redacted_causes(tmp_path: Path) -> None:
    import subprocess

    lane = bare_lane()
    lane.owned = str(tmp_path)
    lane.bundle = tmp_path
    (tmp_path / "synthetic-password").write_text("synthetic-sensitive")
    lane.record_verifier_failure("verify-sql", subprocess.CompletedProcess([], 1, "", "first synthetic-sensitive amqp://user:password@broker"))
    for _ in range(3):
        lane.record_verifier_failure("verify-sql", subprocess.CompletedProcess([], 2, "", "x" * 20000))
    text = (tmp_path / "verifier-errors.json").read_text()
    record = json.loads(text)["verify-sql"]
    assert record["first"]["exit_code"] == 1 and record["latest"]["exit_code"] == 2 and record["attempts"] == 4
    assert "synthetic-sensitive" not in text and "user:password" not in text
    assert len(record["latest"]["redacted_tail"]) <= 8192 and len(text) < 18000
