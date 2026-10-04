"""Execute only a reviewed, host-native, synthetic extractor smoke bundle."""

from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import re
import resource
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast

import yaml


OWNER_KEY = "beadhive.probe.owner"
TASK_KEY = "beadhive.probe.task"


def require(value: bool, message: str) -> None:
    if not value:
        raise ValueError(message)


def load_prepare() -> Any:
    spec = importlib.util.spec_from_file_location("extractor_smoke_prepare", Path(__file__).with_name("extractor_smoke_prepare.py"))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Lane:
    """Enforce one existing host lock, global deadline and known labeled resources."""

    def __init__(self, context: dict[str, Any], images: dict[str, Any], bundle: Path) -> None:
        self.prepare = load_prepare()
        self.owned, self.project = self.prepare.validate_inputs(context, images)
        self.control_rss_guard = True
        self.context = context
        self.images = images
        self.bundle = bundle
        started = time.monotonic()
        self.work_deadline = started + 780
        self.deadline = started + 900
        self.provider = "discogs"
        sudo = shutil.which("sudo")
        docker = shutil.which("docker")
        require(sudo is not None and docker is not None, "trusted host Docker executables missing")
        self.executor = [str(sudo), "-n", str(docker)]
        self.created: dict[str, dict[str, str]] = {"container": {}, "network": {}, "volume": {}}

    def command(self, arguments: list[str], timeout: int = 60, cleanup: bool = False) -> subprocess.CompletedProcess[str]:
        remaining = (self.deadline if cleanup else self.work_deadline) - time.monotonic()
        require(remaining > 0, "global smoke deadline exhausted")
        result = subprocess.run(  # noqa: S603 -- fixed root-selected host-native sudo Docker argv, no shell
            [*self.executor, *arguments],
            capture_output=True,
            text=True,
            timeout=min(5, timeout, remaining) if cleanup else min(timeout, remaining),
            check=False,
        )

        if not cleanup and getattr(self, "control_rss_guard", False):
            divisor = 1024**2 if sys.platform == "darwin" else 1024
            peak_mib = (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss + resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss) / divisor
            require(peak_mib <= 256, "actual host-control RSS reservation exceeded; cleanup required")
        return result

    def compose(self, arguments: list[str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
        return self.command(["compose", "--project-name", self.project, "-f", str(self.bundle / f"compose-{self.provider}.yml"), *arguments], timeout)

    def checked(self, result: subprocess.CompletedProcess[str], operation: str) -> str:
        require(result.returncode == 0, f"owned smoke operation failed: {operation}; private raw output remains local")
        return result.stdout

    def inspect(self, kind: str, name: str, cleanup: bool = False, timeout: int = 60) -> dict[str, Any] | None:
        result = self.command([kind, "inspect", name], cleanup=cleanup, timeout=timeout)
        if result.returncode != 0:
            require("no such" in result.stderr.casefold() or "not found" in result.stderr.casefold(), "cannot establish resource absence/identity")
            return None
        return json.loads(result.stdout)[0]  # type: ignore[no-any-return]

    def owned_identity(self, kind: str, name: str, cleanup: bool = False, timeout: int = 60) -> dict[str, Any] | None:
        metadata = self.inspect(kind, name, cleanup=cleanup, timeout=timeout)
        if metadata is None:
            return None
        labels = metadata.get("Config", {}).get("Labels", {}) if kind == "container" else metadata.get("Labels", {})
        require(labels.get(OWNER_KEY) == self.project and labels.get(TASK_KEY) == "gm-deployment-eeo", f"unverified {kind} ownership; no removal")
        identifier = metadata.get("Id") if kind != "volume" else metadata.get("Name")
        require(isinstance(identifier, str) and bool(identifier), "missing owned resource identity")
        previous = self.created[kind].get(name)
        require(previous is None or previous == identifier, "owned resource identity changed")
        self.created[kind][name] = cast("str", identifier)
        return metadata

    def names(self) -> dict[str, list[str]]:
        return {
            "container": [f"{self.project}-{role}" for role in self.prepare.LIMITS],
            "network": [f"{self.project}_isolated"],
            "volume": [f"{self.project}_{role}" for role in self.prepare.DATA_VOLUMES],
        }

    def precheck(self) -> None:
        expected_host = self.context["context"]["host"].split("@")[-1].split(".")[0]
        require(socket.gethostname().split(".")[0] == expected_host, "wrong host-native execution target")
        require(self.bundle.resolve() == Path(self.owned) / "bundle", "bundle outside exact selected owned root")
        self.checked(self.command(["info", "--format", "{{.Architecture}}"]), "host Docker availability")
        for kind, names in self.names().items():
            for name in names:
                require(self.inspect(kind, name) is None, "owned resource name is already present; do not reuse")
        for provider in ["discogs", "musicbrainz"]:
            model = self.prepare.compose_model(self.context, self.images, provider)
            require(yaml.safe_load((self.bundle / f"compose-{provider}.yml").read_text()) == model, "reviewed model differs from bound inputs")
            for role, service in model["services"].items():
                inspected = self.checked(self.command(["image", "inspect", service["image"]]), "pre-pulled selected image")
                image = json.loads(inspected)[0]
                require(image.get("Architecture") == "amd64" and image.get("Os") == "linux", "inspected image is not native AMD64")
                require(service["image"] in image.get("RepoDigests", []), "selected digest absent from inspected image")
                if role == "producer":
                    require(
                        image["Config"].get("Labels", {}).get("org.opencontainers.image.revision") == self.images["producers"][provider]["revision"],
                        "producer OCI revision mismatch",
                    )
                targets = [mount["target"] for mount in service["volumes"]] + [mount.split(":")[0] for mount in service["tmpfs"]]
                require(
                    all(any(path == target or path.startswith(target + "/") for target in targets) for path in image["Config"].get("Volumes", {})),
                    "image-declared volume is not explicitly bounded",
                )

    def verify_resources(self) -> None:
        model = self.prepare.compose_model(self.context, self.images, self.provider)
        for role, service in model["services"].items():
            metadata = self.owned_identity("container", service["container_name"])
            if metadata is None:
                continue
            require(metadata["Config"].get("Image") == service["image"], "actual container image reference mismatch")
            host = metadata["HostConfig"]
            cpus, memory, pids = self.prepare.LIMITS[role]
            require(host.get("NanoCpus") == int(cpus * 1_000_000_000), "actual CPU quota mismatch")
            require(host.get("Memory") == memory * 1024**2 and host.get("MemorySwap") == memory * 1024**2, "actual memory/swap cap mismatch")
            require(
                host.get("PidsLimit") == pids and host.get("ReadonlyRootfs") is True and host.get("ShmSize") == 16 * 1024**2,
                "actual PID/readonly boundary mismatch",
            )
            require(not host.get("PortBindings"), "unexpected published port")
            allowed = {mount["target"]: mount for mount in service["volumes"]}
            for mount in metadata.get("Mounts", []):
                if mount["Type"] == "tmpfs":
                    continue
                require(mount["Destination"] in allowed, "unexpected actual mount")
                if mount["Type"] == "bind":
                    require(mount["Source"] == str(self.bundle) and not mount["RW"], "unexpected host bind/write access")
                else:
                    require(mount["Name"] == f"{self.project}_{allowed[mount['Destination']]['source']}", "unexpected volume identity")
        for kind in ["network", "volume"]:
            for name in self.names()[kind]:
                metadata = self.owned_identity(kind, name)
                if metadata and kind == "network":
                    require(metadata.get("Internal") is True, "actual network is not internal")
                if metadata and kind == "volume":
                    role = name.removeprefix(self.project + "_")
                    require(
                        metadata.get("Driver") == "local"
                        and metadata.get("Options")
                        == {"type": "tmpfs", "device": "tmpfs", "o": f"size={self.prepare.DATA_VOLUMES[role]}m,mode=1777"},
                        "actual named tmpfs bound mismatch",
                    )

    def wait(self, function: Any, seconds: int = 90) -> Any:
        end = min(self.work_deadline, time.monotonic() + seconds)
        while time.monotonic() < end:
            result = function()
            if result is not None:
                return result
            time.sleep(1)
        raise ValueError("bounded readiness/assertion wait exhausted")

    def role_exit(self, role: str) -> int | None:
        metadata = self.owned_identity("container", f"{self.project}-{role}")
        if metadata and not metadata["State"]["Running"]:
            return int(metadata["State"]["ExitCode"])
        return None

    def start(self, roles: list[str]) -> None:
        self.checked(self.compose(["up", "-d", "--no-deps", *roles]), "start only owned roles")
        self.verify_resources()

    def require_auxiliary_running(self) -> None:
        for role in ["broker", "postgres", "neo4j", "fixture-http"]:
            name = f"{self.project}-{role}"
            metadata = self.owned_identity("container", name)
            require(metadata is not None and metadata["State"]["Running"], f"owned auxiliary exited: {role}")

    def failure_diagnostics(self) -> None:
        """Preserve bounded task-only evidence without disclosing synthetic credentials."""
        secret = (self.bundle / "synthetic-password").read_text().strip()
        records = []
        diagnostics_end = min(time.monotonic() + 15, self.deadline - 90)
        for role in [
            "broker",
            "postgres",
            "neo4j",
            "fixture-http",
            "schema-sql",
            "schema-graph",
            "observer",
            "sql-consumer",
            "graph-consumer",
            "producer",
        ]:
            name = f"{self.project}-{role}"
            try:
                require(time.monotonic() < diagnostics_end, "diagnostic budget reserved for cleanup")
                metadata = self.owned_identity("container", name, cleanup=True, timeout=1)
                if metadata is None:
                    continue
                output = self.command(["logs", "--tail", "40", metadata["Id"]], cleanup=True, timeout=1)
                text = (output.stdout + output.stderr)[-8192:].replace(secret, "[synthetic credential redacted]")
                text = re.sub(r"(?i)(?:amqp|postgresql|bolt|neo4j)://[^\s]+", "[connection redacted]", text)
                state = metadata["State"]
                records.append(
                    {
                        "name": name,
                        "id": metadata["Id"],
                        "running": state["Running"],
                        "exit_code": state["ExitCode"],
                        "oom_killed": state.get("OOMKilled"),
                        "redacted_log_tail": text,
                    }
                )
            except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
                records.append({"name": name, "diagnostics": "unavailable; ownership/deadline guards retained"})
        (Path(self.owned) / "failure-diagnostics.json").write_text(
            json.dumps(
                {"provider": self.provider, "owner": self.project, "context": self.context["context"]["name"], "auxiliaries": records}, indent=2
            )
            + "\n"
        )

    def observer_ready(self) -> bool | None:
        name = f"{self.project}-observer"
        metadata = self.owned_identity("container", name)
        require(metadata is not None and metadata["State"]["Running"], "owned observer exited before readiness")
        return True if self.command(["exec", name, "test", "-f", "/captures/ready"]).returncode == 0 else None

    def record_verifier_failure(self, role: str, result: subprocess.CompletedProcess[str]) -> None:
        require(role in {"verify-sql", "verify-graph"}, "unknown verifier evidence role")
        path = Path(self.owned) / "verifier-errors.json"
        record = json.loads(path.read_text()) if path.is_file() else {}
        secret = (self.bundle / "synthetic-password").read_text().strip()
        text = (result.stdout + result.stderr)[-8192:].replace(secret, "[synthetic credential redacted]")
        text = re.sub(r"(?i)(?:amqp|postgresql|bolt|neo4j)://[^\s]+", "[connection redacted]", text)
        current = {"exit_code": result.returncode, "redacted_tail": text}
        previous = record.setdefault(role, {"first": current, "attempts": 0})
        previous["latest"] = current
        previous["attempts"] += 1
        path.write_text(json.dumps(record, indent=2) + "\n")

    def exec_driver(self, role: str) -> dict[str, Any] | None:
        container_role = "sql-consumer" if role == "verify-sql" else "graph-consumer"
        require(self.owned_identity("container", f"{self.project}-{container_role}") is not None, "owned verifier host absent")
        result = self.command(["exec", f"{self.project}-{container_role}", "/app/.venv/bin/python", "/fixtures/driver.py", role, self.provider])
        if result.returncode != 0:
            self.record_verifier_failure(role, result)
            return None
        return json.loads(result.stdout.strip().splitlines()[-1])  # type: ignore[no-any-return]

    def capture(self) -> list[dict[str, Any]]:
        result = self.checked(
            self.command(
                [
                    "exec",
                    f"{self.project}-observer",
                    "/app/.venv/bin/python",
                    "-c",
                    "from pathlib import Path; print(Path('/captures/events.ndjson').read_text())",
                ]
            ),
            "read only owned synthetic capture",
        )
        return [json.loads(line) for line in result.splitlines() if line]

    def remove_container(self, name: str) -> None:
        metadata = self.owned_identity("container", name, cleanup=True)
        if metadata:
            self.checked(self.command(["rm", "--force", metadata["Id"]], cleanup=True), "remove exact verified owned container")
            self.created["container"].pop(name, None)

    def cleanup(self) -> None:
        failures = []
        for kind, names in self.names().items():
            for name in names:
                try:
                    if kind == "container":
                        self.remove_container(name)
                    else:
                        metadata = self.owned_identity(kind, name, cleanup=True)
                        if metadata:
                            identifier = metadata["Name"] if kind == "volume" else metadata["Id"]
                            self.checked(self.command([kind, "rm", identifier], cleanup=True), "remove verified owned resource")
                            self.created[kind].pop(name, None)
                except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
                    failures.append(f"{kind}:{name}")
        require(not failures, "owned cleanup unresolved: " + ",".join(failures))

    def capability(self, role: str, modules: list[str]) -> dict[str, Any]:
        model = self.prepare.compose_model(self.context, self.images, self.provider)
        service = model["services"][role]
        name = service["container_name"]
        cpus, memory, pids = self.prepare.LIMITS[role]
        code = (
            "import importlib,json; modules="
            + repr(modules)
            + "; [importlib.import_module(m) for m in modules]; print(json.dumps({'modules':modules,'available':True}))"
        )
        try:
            self.checked(
                self.command(
                    [
                        "create",
                        "--name",
                        name,
                        "--label",
                        OWNER_KEY + "=" + self.project,
                        "--label",
                        TASK_KEY + "=gm-deployment-eeo",
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
                        "--cpus",
                        str(cpus),
                        "--memory",
                        str(memory) + "m",
                        "--memory-swap",
                        str(memory) + "m",
                        "--pids-limit",
                        str(pids),
                        "--tmpfs",
                        "/tmp:rw,noexec,nosuid,size=8m",
                        "--tmpfs",
                        "/logs:rw,noexec,nosuid,size=4m,uid=1000,gid=1000",
                        "--entrypoint",
                        "/app/.venv/bin/python",
                        service["image"],
                        "-c",
                        code,
                    ]
                ),
                "create mount-free Python capability probe",
            )
            metadata = self.owned_identity("container", name)
            require(
                metadata is not None and all(mount["Type"] == "tmpfs" for mount in metadata.get("Mounts", [])),
                "capability probe unexpectedly has data mounts",
            )
            output = self.checked(self.command(["start", "--attach", name]), "Python capability probe")
            metadata = self.owned_identity("container", name)
            require(metadata is not None and metadata["State"]["ExitCode"] == 0, "Python executable/module capability failed")
            return json.loads(output.strip().splitlines()[-1])  # type: ignore[no-any-return]
        finally:
            try:
                self.remove_container(name)
            except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
                error.add_note(f"Capability cleanup unresolved name={name} owner={self.project} context={self.context['context']['name']}")
                raise

    def marker_path(self) -> str:
        return (
            "/discogs-data/.extraction_status_20000104.json"
            if self.provider == "discogs"
            else "/musicbrainz-data/20000101-000000/.mb_extraction_status_20000101-000000.json"
        )

    def marker(self) -> dict[str, Any]:
        code = "from pathlib import Path; print(Path(" + repr(self.marker_path()) + ").read_text())"
        output = self.checked(
            self.command(["exec", f"{self.project}-fixture-http", "/app/.venv/bin/python", "-c", code]), "read owned synthetic marker"
        )
        return json.loads(output)  # type: ignore[no-any-return]

    def pending_marker(self) -> dict[str, Any]:
        # Mutate only this owned actual-image synthetic marker, never a production marker.
        filename = "discogs_20000104_releases.xml.gz" if self.provider == "discogs" else "release.jsonl.xz"
        code = (
            "from pathlib import Path; import json; p=Path("
            + repr(self.marker_path())
            + "); m=json.loads(p.read_text()); f=m['processing_phase']['progress_by_file']["
            + repr(filename)
            + "]; m['processing_phase']['records_extracted']-=f['records_extracted']; m['processing_phase']['files_processed']-=1; f.update(status='pending',records_extracted=0,messages_published=0,batches_sent=0,completed_at=None); m['processing_phase']['status']='in_progress'; m['processing_phase']['completed_at']=None; m['publishing_phase']['status']='in_progress'; m['summary']['overall_status']='in_progress'; p.write_text(json.dumps(m)); print(json.dumps(m))"
        )
        output = self.checked(
            self.command(["exec", f"{self.project}-fixture-http", "/app/.venv/bin/python", "-c", code]), "install owned partial marker scenario"
        )
        return json.loads(output)  # type: ignore[no-any-return]

    def check_capture_health(self) -> None:
        require(self.command(["exec", f"{self.project}-observer", "test", "!", "-f", "/captures/failed"]).returncode == 0, "observer callback failed")
        metadata = self.owned_identity("container", f"{self.project}-observer")
        require(metadata is not None and metadata["State"]["Running"], "observer stopped before capture completed")

    def queues_empty(self) -> bool | None:
        self.check_capture_health()
        output = self.checked(
            self.command(
                [
                    "exec",
                    f"{self.project}-broker",
                    "rabbitmqctl",
                    "list_queues",
                    "name",
                    "messages_ready",
                    "messages_unacknowledged",
                    "--formatter=json",
                ]
            ),
            "owned broker queue counters",
        )
        queues = json.loads(output)
        require(isinstance(queues, list) and bool(queues), "no owned queue counter evidence")
        for queue in queues:
            if queue["name"].endswith(".dlq"):
                require(queue["messages_ready"] == 0 and queue["messages_unacknowledged"] == 0, "synthetic frame dead-lettered")
        return True if all(queue["messages_ready"] == 0 and queue["messages_unacknowledged"] == 0 for queue in queues) else None

    def producer_logs(self, delivery: Any) -> dict[str, Any]:
        output = self.checked(self.command(["logs", f"{self.project}-producer"]), "owned actual producer log evidence")
        return delivery.validate_json_log(output, "production")  # type: ignore[no-any-return]

    def run_provider(self, provider: str) -> dict[str, Any]:
        self.provider = provider
        spec = importlib.util.spec_from_file_location("extractor_delivery", Path(__file__).with_name("extractor_delivery.py"))
        assert spec is not None and spec.loader is not None
        delivery = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(delivery)
        producer = self.images["producers"][provider]
        startup = delivery.isolated_startup_probe(
            producer["image"], provider, self.context["context"]["name"], producer["revision"], executor=self.command
        )
        capabilities = [
            self.capability("fixture-http", ["http.server"]),
            self.capability("schema-sql", ["psycopg", "aio_pika", "common"]),
            self.capability("schema-graph", ["neo4j", "common"]),
        ]
        self.start(["broker", "postgres", "neo4j", "fixture-http"])

        def ready() -> bool | None:
            self.require_auxiliary_running()
            probes = [
                ("broker", ["rabbitmq-diagnostics", "-q", "ping"]),
                ("postgres", ["pg_isready", "-U", "synthetic-smoke", "-d", "synthetic_smoke"]),
            ]
            return True if all(self.command(["exec", f"{self.project}-{role}", *args]).returncode == 0 for role, args in probes) else None

        self.wait(ready)
        for role in ["schema-sql", "schema-graph"]:
            self.start([role])
            require(self.wait(lambda current=role: self.role_exit(current)) == 0, "exact schema initialization failed")
        self.start(["sql-consumer", "graph-consumer", "observer"])
        self.wait(self.observer_ready)
        logging = []
        if provider == "discogs":
            for entity in ["artists", "labels", "masters", "releases"]:
                result = self.compose(
                    [
                        "run",
                        "--no-deps",
                        "--name",
                        f"{self.project}-producer",
                        "producer",
                        "--local-manifest",
                        f"/fixtures/discogs/manifest-{entity}.json",
                    ],
                    timeout=120,
                )
                self.verify_resources()
                self.checked(result, "actual Discogs fixture processing")
                logging.append(self.producer_logs(delivery))
                self.remove_container(f"{self.project}-producer")
        else:
            self.start(["producer"])
        sql = self.wait(lambda: self.exec_driver("verify-sql"), 120)
        graph = self.wait(lambda: self.exec_driver("verify-graph"), 120)
        self.wait(self.queues_empty)
        marker_before = self.marker()
        require(marker_before["summary"]["overall_status"] == "completed", "initial synthetic marker incomplete")
        self.checked(
            self.command(
                [
                    "exec",
                    f"{self.project}-observer",
                    "/app/.venv/bin/python",
                    "-c",
                    "from pathlib import Path; Path('/captures/baseline.ndjson').write_bytes(Path('/captures/events.ndjson').read_bytes())",
                ]
            ),
            "freeze actual initial synthetic frame evidence",
        )
        before = self.capture()
        data_before = sum(frame["event"]["type"] == "data" for frame in before)
        if provider == "discogs":
            # Re-run the same completed release marker without deleting or rewriting it.
            self.checked(
                self.compose(
                    [
                        "run",
                        "--no-deps",
                        "--name",
                        f"{self.project}-producer",
                        "producer",
                        "--local-manifest",
                        "/fixtures/discogs/manifest-releases.json",
                    ],
                    timeout=90,
                ),
                "completed Discogs resume",
            )
            self.verify_resources()
        else:
            self.checked(self.command(["restart", f"{self.project}-producer"]), "restart only owned MusicBrainz producer")
        time.sleep(3)
        self.wait(self.queues_empty)
        after = self.capture()
        require(sum(frame["event"]["type"] == "data" for frame in after) == data_before, "completed marker reprocessed synthetic records")
        marker_completed = self.marker()
        for field in ["files_processed", "files_total", "records_extracted", "progress_by_file"]:
            require(
                marker_completed["processing_phase"][field] == marker_before["processing_phase"][field],
                "completed synthetic marker counters/state changed",
            )
        require(marker_completed["summary"]["overall_status"] == "completed", "completed synthetic marker status changed")
        logging.append(self.producer_logs(delivery))
        partial = self.pending_marker()
        if provider == "discogs":
            self.remove_container(f"{self.project}-producer")
            self.checked(
                self.compose(
                    [
                        "run",
                        "--no-deps",
                        "--name",
                        f"{self.project}-producer",
                        "producer",
                        "--local-manifest",
                        "/fixtures/discogs/manifest-releases.json",
                    ],
                    timeout=90,
                ),
                "partial Discogs resume",
            )
            self.verify_resources()
        else:
            self.checked(self.command(["restart", f"{self.project}-producer"]), "partial MusicBrainz resume")
        expected_frames = len(after) + 6
        self.wait(lambda: True if len(self.capture()) >= expected_frames else None, 90)
        self.wait(self.queues_empty)
        final_frames = self.capture()
        delta = final_frames[len(after) :]
        original_release = next(frame["event"] for frame in before if frame["entity"] == "releases" and frame["event"]["type"] == "data")
        final_marker = self.marker()
        spec = importlib.util.spec_from_file_location("extractor_smoke_driver", Path(__file__).with_name("extractor_smoke_driver.py"))
        assert spec is not None and spec.loader is not None
        driver = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(driver)
        driver.validate_resume_delta(provider, delta, original_release, partial, final_marker)
        logging.append(self.producer_logs(delivery))
        sql_after = self.wait(lambda: self.exec_driver("verify-sql"), 30)
        graph_after = self.wait(lambda: self.exec_driver("verify-graph"), 30)
        return {
            "provider": provider,
            "startup": startup,
            "capabilities": capabilities,
            "sql": sql,
            "graph": graph,
            "sql_after_completed_resume": sql_after,
            "graph_after_completed_resume": graph_after,
            "data_frames_before": data_before,
            "data_frames_after_completed_resume": data_before,
            "data_frames_after_partial_resume": data_before + 1,
            "partial_resume_verified": True,
            "actual_owned_queue_counters_empty": True,
            "logging": logging,
            "production_acceptance": False,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--images", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--review-reference", required=True)
    args = parser.parse_args()
    require(bool(args.review_reference), "independent exact runner review reference required")
    context, images = json.loads(args.context.read_text()), json.loads(args.images.read_text())
    lane = Lane(context, images, args.bundle)
    lock_path = Path(context["context"]["exclusive_lock"])
    require(lock_path.is_file(), "existing exclusive host lock missing; do not create a replacement")

    def terminate(_signum: int, _frame: Any) -> None:
        raise RuntimeError("owned smoke interrupted; finally cleanup required")

    signal.signal(signal.SIGTERM, terminate)
    with lock_path.open("r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lane.precheck()
        result = {
            "review_reference": args.review_reference,
            "resource_proof": lane.prepare.resource_proof(),
            "providers": [],
            "partial_resume_verified": False,
            "production_acceptance": False,
        }
        try:
            for provider in ["discogs", "musicbrainz"]:
                result["providers"].append(lane.run_provider(provider))
                lane.cleanup()
        except Exception as error:
            try:
                lane.failure_diagnostics()
            except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
                error.add_note(f"Owned failure diagnostics unavailable owner={lane.project} context={lane.context['context']['name']}")
            raise
        finally:
            lane.cleanup()
        result["partial_resume_verified"] = all(item["partial_resume_verified"] for item in result["providers"])
        result["actual_owned_resource_checks_passed"] = True
        print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
