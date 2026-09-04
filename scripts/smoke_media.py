"""Assert the ADR 0007 canonical media block end to end on a disposable smoke stack.

`scripts/smoke-media.sh` brings up the schema initializer, the broker, both stores, both
SQL loaders, and both graph enrichers, then runs this module. The extractors never run: a
smoke stack has no dumps to download, so the release events come from the producers'
contract fixtures promoted verbatim into `config/media-smoke/` (`config/provenance.json`
records the owning repository, commit, and upstream digest of each).

The module publishes those fixtures as contract-shaped `data` events onto the durable
fanout exchanges the producers own, waits for the four consumers to persist them, and then
asserts the canonical block survived the whole path:

- PostgreSQL `releases.media` is populated for the Discogs fixture and its `families`
  match the fixture's block;
- PostgreSQL `musicbrainz.releases.media` is populated for the MusicBrainz fixture and its
  `families` match the fixture's block;
- Neo4j carries `Medium` and `MediaFamily` nodes joined by `IN_FAMILY`, and the Discogs
  release is joined to its medium by `ISSUED_ON {source: 'discogs'}`;
- the MusicBrainz enricher's `ISSUED_ON {source: 'musicbrainz'}` edge reaches the same
  release, which is what proves both catalogs' media reconcile onto one release node.

Identity fields are the one thing the fixtures cannot supply as published. The stores
constrain them: `musicbrainz.releases.mbid` is a UUID, `musicbrainz.releases`
`discogs_release_id` is a BIGINT, and the MusicBrainz enricher matches a release by that
Discogs identifier. The fixtures' documentation ids (`contract-discogs-releases`,
`contract-musicbrainz-releases`) satisfy none of that, so the run overrides `id` and
`discogs_release_id` with fixed, reserved values and leaves every media field untouched.

Every assertion is polled to a deadline rather than checked once, because both loaders and
both enrichers batch their writes behind a flush interval.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "config" / "media-smoke"

# Reserved smoke identities. They are deliberately outside any real catalog range so a
# smoke run can never be mistaken for, or collide with, promoted production data.
DISCOGS_RELEASE_ID = "999000001"
MUSICBRAINZ_RELEASE_MBID = "f0f0f0f0-0000-4000-8000-000000009001"

# The producers own these names; the promoted contracts pin them. `runtime_identifiers`
# in each producer's contract.json is the source of truth reproduced here.
DISCOGS_EXCHANGE = "groovemap-discogs-releases"
MUSICBRAINZ_EXCHANGE = "groovemap-musicbrainz-releases"
CONSUMER_QUEUES = (
    "groovemap-discogs-tableinator-releases",
    "groovemap-discogs-graphinator-releases",
    "groovemap-musicbrainz-brainztableinator-releases",
    "groovemap-musicbrainz-brainzgraphinator-releases",
)

# Non-production development credentials from docker-compose.yml. The smoke stack is
# disposable and unpublished; an operator's real environment uses file-backed secrets.
BROKER_USERNAME = "groovemap"
BROKER_PASSWORD = "groovemap"  # noqa: S105
POSTGRES_USER = "groovemap"
POSTGRES_DATABASE = "groovemap"
NEO4J_USERNAME = "neo4j"
NEO4J_PASSWORD = "groovemap"  # noqa: S105

SAFE_LITERAL = re.compile(r"\A[0-9a-zA-Z_-]+\Z")


class SmokeError(RuntimeError):
    """A smoke run could not reach the point where its assertions become meaningful."""


@dataclass(frozen=True)
class Check:
    """One asserted fact about the canonical media block, and how it turned out."""

    name: str
    passed: bool
    detail: str


def quote_literal(value: str) -> str:
    """Return a value safe to inline into SQL and Cypher, refusing anything that is not.

    The only literals this module inlines are identifiers it chose itself and medium and
    family ids from a promoted fixture, so refusing every other shape is cheaper and safer
    than threading parameters through two container CLIs.
    """
    if not SAFE_LITERAL.match(value):
        raise SmokeError(f"refusing to inline an unexpected literal into a store query: {value!r}")
    return f"'{value}'"


def payload_sha256(payload: dict[str, Any]) -> str:
    """Return the digest a producer would carry for this payload.

    The producers hash the record without its own `sha256` field. The graph enrichers
    compare the value against the release node's stored hash to skip unchanged records, so
    an event needs a stable, non-empty digest to behave like a real one.
    """
    without_hash = {key: value for key, value in payload.items() if key != "sha256"}
    canonical = json.dumps(without_hash, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def load_fixture(name: str) -> dict[str, Any]:
    """Load one promoted producer contract fixture."""
    fixture: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    if fixture.get("type") != "data":
        raise SmokeError(f"{name} is not a contract `data` event")
    if not isinstance(fixture.get("media"), dict):
        raise SmokeError(f"{name} carries no canonical media block")
    return fixture


def discogs_event(fixture: dict[str, Any], release_id: str = DISCOGS_RELEASE_ID) -> dict[str, Any]:
    """Return the Discogs release event to publish, in the contract's envelope."""
    event = dict(fixture)
    event["id"] = release_id
    event["sha256"] = payload_sha256(event)
    return event


def musicbrainz_event(
    fixture: dict[str, Any],
    mbid: str = MUSICBRAINZ_RELEASE_MBID,
    discogs_release_id: str = DISCOGS_RELEASE_ID,
) -> dict[str, Any]:
    """Return the MusicBrainz release event to publish, in the contract's envelope.

    `discogs_release_id` is what lets the MusicBrainz enricher find the release node the
    Discogs enricher already created, so both catalogs' media land on one release. It is a
    BIGINT column in `musicbrainz.releases`, so it travels as a JSON number.
    """
    event = dict(fixture)
    event["id"] = mbid
    event["discogs_release_id"] = int(discogs_release_id)
    event["sha256"] = payload_sha256(event)
    return event


def media_families(event: dict[str, Any]) -> list[str]:
    """Return the canonical family ids the event's media block asserts."""
    families: list[str] = list(event["media"]["families"])
    return families


def media_medium_ids(event: dict[str, Any]) -> list[str]:
    """Return the canonical medium ids the event's media block asserts."""
    return sorted({str(item["medium"]) for item in event["media"]["items"]})


def render(checks: Sequence[Check]) -> str:
    """Render the assertion report an operator records for the run."""
    width = max((len(check.name) for check in checks), default=0)
    lines = [f"{'PASS' if check.passed else 'FAIL'}  {check.name.ljust(width)}  {check.detail}" for check in checks]
    failed = [check for check in checks if not check.passed]
    lines.append("")
    lines.append(f"{len(checks) - len(failed)}/{len(checks)} media assertions passed")
    return "\n".join(lines)


def exit_code(checks: Sequence[Check]) -> int:
    """Return 0 only when every asserted fact held."""
    return 0 if checks and all(check.passed for check in checks) else 1


def run(command: Sequence[str], stdin: str | None = None, timeout: float = 120.0) -> str:
    """Run a container-side command and return its stdout, raising on a non-zero exit."""
    completed = subprocess.run(  # noqa: S603
        list(command),
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise SmokeError(f"{' '.join(command)} failed with exit {completed.returncode}: {completed.stderr.strip()}")
    return completed.stdout


class Stack:
    """The disposable smoke stack, addressed through Compose and the management API."""

    def __init__(self, project: str, compose_files: Sequence[str], broker_port: int) -> None:
        self.project = project
        self.compose_files = list(compose_files)
        self.broker_url = f"http://127.0.0.1:{broker_port}/api"

    def compose_exec(self, service: str, command: Sequence[str], timeout: float = 120.0) -> str:
        """Run a command inside one of the stack's containers."""
        argv = ["docker", "compose", "--project-name", self.project]
        for compose_file in self.compose_files:
            argv += ["-f", compose_file]
        argv += ["exec", "-T", service, *command]
        return run(argv, timeout=timeout)

    def psql(self, sql: str) -> str:
        """Return the single scalar a PostgreSQL query yields, or the empty string."""
        output = self.compose_exec(
            "postgres",
            ["psql", "-U", POSTGRES_USER, "-d", POSTGRES_DATABASE, "-v", "ON_ERROR_STOP=1", "-tAqc", sql],
        )
        return output.strip()

    def cypher(self, query: str) -> str:
        """Return the single scalar a Cypher query yields, or the empty string."""
        output = self.compose_exec(
            "neo4j",
            ["cypher-shell", "-u", NEO4J_USERNAME, "-p", NEO4J_PASSWORD, "--format", "plain", query],
        )
        rows = [line.strip() for line in output.splitlines() if line.strip()]
        return rows[-1] if len(rows) > 1 else ""

    def _management(self, path: str, payload: dict[str, Any] | None = None) -> Any:
        credentials = base64.b64encode(f"{BROKER_USERNAME}:{BROKER_PASSWORD}".encode()).decode()
        body = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(  # noqa: S310
            f"{self.broker_url}/{path}",
            data=body,
            method="GET" if body is None else "POST",
            headers={"Authorization": f"Basic {credentials}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return json.loads(response.read().decode())

    def queue_consumers(self, queue: str) -> int:
        """Return how many consumers are attached to a queue, or 0 while it is absent."""
        try:
            queue_state = self._management(f"queues/%2F/{queue}")
        except urllib.error.HTTPError:
            return 0
        return int(queue_state.get("consumers", 0))

    def publish(self, exchange: str, event: dict[str, Any]) -> None:
        """Publish one contract event onto a producer's durable fanout exchange."""
        result = self._management(
            f"exchanges/%2F/{exchange}/publish",
            {
                "properties": {"content_type": "application/json", "delivery_mode": 2},
                "routing_key": "",
                "payload": json.dumps(event),
                "payload_encoding": "string",
            },
        )
        if not result.get("routed"):
            raise SmokeError(f"{exchange} accepted the event but routed it to no queue; the consumers have not bound yet")


def wait_for_consumers(stack: Stack, deadline: float) -> None:
    """Block until every contract queue exists with a live consumer.

    A fanout exchange drops a message that reaches no bound queue, so publishing before the
    loaders and enrichers have declared and bound their queues would silently prove nothing.
    """
    pending = [queue for queue in CONSUMER_QUEUES if stack.queue_consumers(queue) < 1]
    while pending and time.monotonic() < deadline:
        time.sleep(2.0)
        pending = [queue for queue in pending if stack.queue_consumers(queue) < 1]
    if pending:
        raise SmokeError(f"queues never gained a consumer before the deadline: {', '.join(pending)}")


def wait_for(checks: Iterable[Callable[[], Check]], deadline: float) -> list[Check]:
    """Re-evaluate assertions until they all hold or the deadline passes.

    Both loaders and both enrichers batch behind a flush interval, so a single fixture
    event becomes visible some seconds after it is consumed rather than immediately.
    """
    probes = list(checks)
    results = [probe() for probe in probes]
    while not all(result.passed for result in results) and time.monotonic() < deadline:
        time.sleep(3.0)
        results = [probe() for probe in probes]
    return results


def _media_present(stack: Stack, name: str, table: str, key_column: str, key: str) -> Callable[[], Check]:
    """Return a probe asserting a release-shaped table carries a canonical media block."""

    def probe() -> Check:
        value = stack.psql(f"SELECT media IS NOT NULL FROM {table} WHERE {key_column} = {key}")  # noqa: S608
        if value == "":
            return Check(name, False, f"no {table} row for {key}")
        return Check(name, value == "t", f"{table}.media IS NOT NULL -> {value}")

    return probe


def _families_match(stack: Stack, name: str, table: str, key_column: str, key: str, expected: list[str]) -> Callable[[], Check]:
    """Return a probe asserting a stored media block's families match the fixture's."""

    def probe() -> Check:
        value = stack.psql(f"SELECT media->'families' FROM {table} WHERE {key_column} = {key}")  # noqa: S608
        actual = json.loads(value) if value else None
        return Check(name, actual == expected, f"{table}.media->'families' = {json.dumps(actual)}, fixture asserts {json.dumps(expected)}")

    return probe


def _counts(stack: Stack, name: str, query: str, description: str) -> Callable[[], Check]:
    """Return a probe asserting a Cypher count is greater than zero."""

    def probe() -> Check:
        value = stack.cypher(query)
        count = int(value) if value.isdigit() else 0
        return Check(name, count > 0, f"{description} -> {count}")

    return probe


def discogs_probes(stack: Stack, event: dict[str, Any]) -> list[Callable[[], Check]]:
    """Return every assertion the Discogs fixture alone must satisfy.

    These are asserted before the MusicBrainz fixture is published, because the MusicBrainz
    enricher creates no release: it only matches one the Discogs enricher already put in the
    graph (ADR 0007, "Storage").
    """
    release_id = quote_literal(str(event["id"]))
    probes = [
        _media_present(stack, "postgres discogs releases.media", "releases", "data_id", release_id),
        _families_match(stack, "postgres discogs media families", "releases", "data_id", release_id, media_families(event)),
        _counts(stack, "neo4j Medium nodes", "MATCH (m:Medium) RETURN count(m) AS value", "count(:Medium)"),
        _counts(stack, "neo4j MediaFamily nodes", "MATCH (f:MediaFamily) RETURN count(f) AS value", "count(:MediaFamily)"),
    ]
    for medium in media_medium_ids(event):
        literal = quote_literal(medium)
        probes.append(
            _counts(
                stack,
                f"neo4j {medium} IN_FAMILY",
                f"MATCH (:Medium {{id: {literal}}})-[:IN_FAMILY]->(f:MediaFamily) RETURN count(f) AS value",
                f"count((:Medium {{id: {medium}}})-[:IN_FAMILY]->(:MediaFamily))",
            )
        )
        probes.append(
            _counts(
                stack,
                f"neo4j ISSUED_ON discogs {medium}",
                f"MATCH (:Release {{id: {release_id}}})-[:ISSUED_ON {{source: 'discogs'}}]->(:Medium {{id: {literal}}}) RETURN count(*) AS value",
                f"count((:Release {{id: {event['id']}}})-[:ISSUED_ON {{source: 'discogs'}}]->(:Medium {{id: {medium}}}))",
            )
        )
    return probes


def musicbrainz_probes(stack: Stack, event: dict[str, Any], discogs_release_id: str = DISCOGS_RELEASE_ID) -> list[Callable[[], Check]]:
    """Return every assertion the MusicBrainz fixture must satisfy once published."""
    mbid = quote_literal(str(event["id"]))
    release_id = quote_literal(discogs_release_id)
    probes = [
        _media_present(stack, "postgres musicbrainz releases.media", "musicbrainz.releases", "mbid", mbid),
        _families_match(stack, "postgres musicbrainz media families", "musicbrainz.releases", "mbid", mbid, media_families(event)),
    ]
    for medium in media_medium_ids(event):
        literal = quote_literal(medium)
        probes.append(
            _counts(
                stack,
                f"neo4j ISSUED_ON musicbrainz {medium}",
                f"MATCH (:Release {{id: {release_id}}})-[:ISSUED_ON {{source: 'musicbrainz'}}]->(:Medium {{id: {literal}}}) RETURN count(*) AS value",
                f"count((:Release {{id: {discogs_release_id}}})-[:ISSUED_ON {{source: 'musicbrainz'}}]->(:Medium {{id: {medium}}}))",
            )
        )
    return probes


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the arguments `scripts/smoke-media.sh` passes in."""
    parser = argparse.ArgumentParser(description="Assert the ADR 0007 canonical media block end to end.")
    parser.add_argument("--project", required=True, help="Compose project name of the disposable smoke stack")
    parser.add_argument("--compose-file", action="append", required=True, dest="compose_files", help="Compose file, repeatable and order-significant")
    parser.add_argument("--broker-port", type=int, required=True, help="Published loopback port of the RabbitMQ management API")
    parser.add_argument("--timeout", type=float, default=300.0, help="Seconds to wait for each stage before failing")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Publish the fixture events, wait for both stores, and report every assertion."""
    args = parse_args(argv)
    stack = Stack(args.project, args.compose_files, args.broker_port)

    discogs = discogs_event(load_fixture("discogs-releases.data.json"))
    musicbrainz = musicbrainz_event(load_fixture("musicbrainz-releases.data.json"))

    print(f"waiting for {len(CONSUMER_QUEUES)} contract queues to bind a consumer")
    wait_for_consumers(stack, time.monotonic() + args.timeout)

    # The Discogs release goes first and is asserted before the MusicBrainz one is
    # published: the MusicBrainz enricher creates no release, it only matches one that the
    # Discogs enricher already put in the graph (ADR 0007, "Storage").
    print(f"publishing the promoted Discogs fixture as release {discogs['id']} onto {DISCOGS_EXCHANGE}")
    stack.publish(DISCOGS_EXCHANGE, discogs)
    discogs_results = wait_for(discogs_probes(stack, discogs), time.monotonic() + args.timeout)
    if not all(result.passed for result in discogs_results):
        print(render(discogs_results))
        return 1

    print(f"publishing the promoted MusicBrainz fixture as release {musicbrainz['id']} onto {MUSICBRAINZ_EXCHANGE}")
    stack.publish(MUSICBRAINZ_EXCHANGE, musicbrainz)
    musicbrainz_results = wait_for(musicbrainz_probes(stack, musicbrainz, str(discogs["id"])), time.monotonic() + args.timeout)

    results = discogs_results + musicbrainz_results
    print(render(results))
    return exit_code(results)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SmokeError as error:
        print(f"smoke-media: {error}", file=sys.stderr)
        sys.exit(1)
