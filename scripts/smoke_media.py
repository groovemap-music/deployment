"""Assert the canonical media block and the identifier path on a disposable smoke stack.

The run publishes promoted producer fixtures, waits for both SQL loaders and
both graph enrichers, and checks each store using reserved run-owned IDs. Those
IDs prevent stale data in a reused volume from satisfying the assertions.
Polling accounts for consumer batch intervals; stack lifecycle remains in the
operator-approved ``smoke-media.sh`` adapter.

Two claims are asserted against the same published event. ADR 0007's canonical ``media``
block reaching both stores is the first. ADR 0011's identifier path is the second: the
release's ``identifiers`` and ``companies`` blocks and its country have to become a
``provider_aliases`` row keyed on the release's native id, a ``CREDITED_TO`` edge to a
``Company`` node, a ``Release.country`` property, and an answer from
``GET /api/lookup/barcode/{value}``. That last hop is why this stack publishes the catalog
API on loopback: the alias is only worth minting if something resolves through it, and the
erasure smoke — the other stack that runs the API — has no broker and no loaders, so it
could only assert a row it inserted itself.
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
import urllib.parse
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

# The release's country. Unlike `media`, `identifiers`, and `companies`, country is not a
# canonical block the producer computes — it is the raw Discogs field carried through
# untouched, which ADR 0011 projects onto `Release.country`. The producer's representative
# contract fixture documents the blocks its mappers produce and so carries no country at
# all, which is why the event builder supplies one here exactly as it supplies the reserved
# release id, rather than this repository editing a promoted fixture.
DISCOGS_RELEASE_COUNTRY = "UK"

# ADR 0011 mints three of the seven identifier types into ADR 0009 alias namespaces. This
# run resolves through `barcode`, the one a person holding the record can read off the
# sleeve, and the only one whose normalized value — digits only — is identifier-shaped
# enough for `quote_literal` to inline. `matrix` rides in the same block and is asserted as
# part of the published event rather than as a store query.
BARCODE_PROVIDER = "barcode"
MATRIX_PROVIDER = "matrix"

# Which canonical identifier type each alias namespace is minted from. The namespace and
# the type are deliberately not the same word for matrix inscriptions, so the lookup probe
# needs the mapping to find the value as printed rather than the value as normalized.
ALIAS_IDENTIFIER_TYPES = {
    BARCODE_PROVIDER: "barcode",
    MATRIX_PROVIDER: "matrix_runout",
    "catalog_number": "catalog_number",
}

# `provider_aliases` is keyed on (provider, entity_kind, external_id) over the currently
# valid row. Every alias an identifiers block mints is a release alias.
ALIAS_ENTITY_KIND = "release"

# The manufacturing credit the CREDITED_TO probe asserts. The vendored company-role
# vocabulary puts a pressing plant under `pressing`, which is the category the
# pressing-chain lens is built on.
PRESSING_ROLE_CATEGORY = "pressing"

# The provenance the Discogs graph enricher stamps on every edge it writes, so a credit a
# second catalog asserts on the same release is distinguishable from this one.
COMPANY_SOURCE = "discogs"

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
DISCOGS_CONSUMER_QUEUES = (
    "groovemap-discogs-tableinator-releases",
    "groovemap-discogs-graphinator-releases",
)
PACKAGED_EXTRACTOR_EXPECTED_EVENTS = "/usr/share/discogs-ingestion/contracts/extractor-smoke/v1/expected-events.ndjson"

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


@dataclass(frozen=True)
class Response:
    """One answer from a disposable stack's API."""

    status: int
    body: str
    media_type: str

    def json(self) -> Any:
        """Return the parsed body, refusing anything that is not JSON."""
        try:
            return json.loads(self.body)
        except json.JSONDecodeError as error:
            raise SmokeError(f"expected a JSON body, got {self.body[:200]!r}") from error


class ApiClient:
    """A disposable stack's catalog API, over the one loopback port its overlay publishes.

    Every method returns the status rather than raising on it, because a wrong status is a
    fact a run wants to report as a failed assertion, not an exception that loses it. Only
    a transport failure raises, and a probe that expects the API to still be starting
    catches it rather than letting it end the run.

    It lives here beside `Stack` rather than in either asserter because both smokes drive
    the same API over the same loopback shape: the erasure run registers and erases an
    account, and the media run resolves a barcode through `GET /api/lookup`.
    """

    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def request(self, method: str, path: str, *, body: Any = None, token: str | None = None) -> Response:
        """Perform one request against the stack's API and return its whole response."""
        headers = {"Accept": "*/*"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(f"{self.base_url}{path}", data=data, method=method, headers=headers)  # noqa: S310
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                return Response(response.status, response.read().decode(), response.headers.get_content_type())
        except urllib.error.HTTPError as error:
            return Response(error.code, error.read().decode(), error.headers.get_content_type() if error.headers else "")
        except urllib.error.URLError as error:
            raise SmokeError(f"{method} {path} could not reach the stack's API: {error.reason}") from error


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


def discogs_event(fixture: dict[str, Any], release_id: str = DISCOGS_RELEASE_ID, country: str = DISCOGS_RELEASE_COUNTRY) -> dict[str, Any]:
    """Return the Discogs release event to publish, in the contract's envelope.

    `country` is added rather than promoted. The producer's contract fixture documents the
    canonical blocks its mappers compute, and country is not one of them — it is a raw
    Discogs field that travels untouched — so the fixture carries none and the graph
    enricher would have nothing to project onto `Release.country`. The digest is taken
    afterwards so it covers the country the same way it covers the blocks.
    """
    event = dict(fixture)
    event["id"] = release_id
    event["country"] = country
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


def alias_external_id(event: dict[str, Any], provider: str) -> str:
    """Return the normalized value the event's identifiers block mints in one namespace.

    The producer derives the aliases at the normalization boundary and publishes them
    beside the items, so this reads the value the loader will key a `provider_aliases` row
    on rather than re-implementing the namespace's normalization rule here. A namespace the
    block mints nothing into is an error: a probe built on an absent alias would assert
    against an empty string and pass for the wrong reason.
    """
    for alias in event["identifiers"]["aliases"]:
        if alias.get("provider") == provider:
            return str(alias["external_id"])
    raise SmokeError(f"the event's identifiers block mints no {provider} alias")


def identifier_value(event: dict[str, Any], provider: str) -> str:
    """Return the identifier as printed on the release for one alias namespace.

    This is what a person reads off a sleeve and types into lookup — grouping spaces and
    all — as opposed to the normalized `external_id` the alias row is keyed on.
    """
    item_type = ALIAS_IDENTIFIER_TYPES[provider]
    for item in event["identifiers"]["items"]:
        if item.get("type") == item_type:
            return str(item["value"])
    raise SmokeError(f"the event's identifiers block carries no {item_type} identifier")


def pressing_credit(event: dict[str, Any]) -> dict[str, Any]:
    """Return the manufacturing credit the `CREDITED_TO` probe asserts.

    One entry, chosen by role category rather than by position, so a fixture that gains or
    reorders credits upstream still names the pressing plant this run asserts.
    """
    for item in event["companies"]["items"]:
        if item.get("role_category") == PRESSING_ROLE_CATEGORY:
            return dict(item)
    raise SmokeError(f"the event's companies block carries no {PRESSING_ROLE_CATEGORY} credit")


def company_id(credit: dict[str, Any]) -> str:
    """Return the `Company.id` the graph enricher keys one companies entry on.

    The Discogs id is the identity whenever the source gives one, stringified: the dump
    states it as element text and the API as a number, and the uniqueness constraint has to
    see one value for both.
    """
    discogs_id = credit.get("discogs_id")
    if not isinstance(discogs_id, int) or isinstance(discogs_id, bool) or discogs_id < 1:
        raise SmokeError(f"the asserted company credit carries no usable Discogs id: {discogs_id!r}")
    return str(discogs_id)


def render(checks: Sequence[Check], subject: str = "media") -> str:
    """Render the assertion report an operator records for the run.

    `subject` names what was asserted, so a second smoke reusing this report reads as its
    own run rather than as a media assertion.
    """
    width = max((len(check.name) for check in checks), default=0)
    lines = [f"{'PASS' if check.passed else 'FAIL'}  {check.name.ljust(width)}  {check.detail}" for check in checks]
    failed = [check for check in checks if not check.passed]
    lines.append("")
    lines.append(f"{len(checks) - len(failed)}/{len(checks)} {subject} assertions passed")
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
    """The disposable smoke stack, addressed through Compose and the management API.

    `broker_port` is optional because a smoke stack need not run a broker at all — the
    erasure assertion drives the API instead of publishing catalog events, and never
    addresses the management endpoint.
    """

    def __init__(self, project: str, compose_files: Sequence[str], broker_port: int = 0, env_file: str | None = None) -> None:
        self.project = project
        self.compose_files = list(compose_files)
        self.env_file = env_file
        self.broker_url = f"http://127.0.0.1:{broker_port}/api"

    def compose_argv(self) -> list[str]:
        """Return the common Compose invocation for this isolated stack."""
        argv = ["docker", "compose", "--project-name", self.project]
        if self.env_file is not None:
            argv += ["--env-file", self.env_file]
        for compose_file in self.compose_files:
            argv += ["-f", compose_file]
        return argv

    def compose_exec(self, service: str, command: Sequence[str], timeout: float = 120.0) -> str:
        """Run a command inside one of the stack's containers."""
        argv = self.compose_argv()
        argv += ["exec", "-T", service, *command]
        return run(argv, timeout=timeout)

    def compose_run(
        self,
        service: str,
        command: Sequence[str] = (),
        *,
        entrypoint: str | None = None,
        timeout: float = 120.0,
    ) -> str:
        """Run one disposable service container without starting its dependencies."""
        argv = [*self.compose_argv(), "run", "--rm", "--no-deps"]
        if entrypoint is not None:
            argv += ["--entrypoint", entrypoint]
        argv += [service, *command]
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


def wait_for_consumers(stack: Stack, deadline: float, queues: Sequence[str] = CONSUMER_QUEUES) -> None:
    """Block until every contract queue exists with a live consumer.

    A fanout exchange drops a message that reaches no bound queue, so publishing before the
    loaders and enrichers have declared and bound their queues would silently prove nothing.
    """
    pending = [queue for queue in queues if stack.queue_consumers(queue) < 1]
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


def _graph_value_matches(stack: Stack, name: str, query: str, expected: Any, subject: str) -> Callable[[], Check]:
    """Return a probe asserting one Cypher answer is the value the event asserts.

    The enricher having written nothing at all is a failure, not an absence the report can
    shrug at: it is exactly what a run whose write never landed looks like.
    """

    def probe() -> Check:
        value = stack.cypher(query)
        try:
            actual = json.loads(value) if value else None
        except json.JSONDecodeError:
            # cypher-shell renders strings and lists of strings as JSON, so anything else
            # is a value worth reporting verbatim rather than discarding as a parse failure.
            actual = value
        rendered = value if value else "nothing"
        return Check(name, actual == expected, f"{subject} = {rendered}, event asserts {json.dumps(expected)}")

    return probe


def _graph_families_match(stack: Stack, name: str, release_id: str, expected: list[str]) -> Callable[[], Check]:
    """Return a probe asserting the release node's `media_families` matches the event's.

    ADR 0007 gives the release node a `media_families` list property alongside the
    `ISSUED_ON` edges, so a filter can skip the traversal. The enricher writes it from the
    same canonical block the SQL loader stores, which makes it the graph-side statement of
    the fact `releases.media->'families'` already asserts in PostgreSQL.
    """
    return _graph_value_matches(
        stack,
        name,
        f"MATCH (r:Release {{id: {release_id}}}) RETURN r.media_families AS value",
        expected,
        f"(:Release {{id: {release_id}}}).media_families",
    )


def _alias_row_minted(stack: Stack, name: str, provider: str, external_id: str) -> Callable[[], Check]:
    """Return a probe asserting exactly one currently valid alias row carries this value.

    Exactly one, not at least one: `provider_aliases` is unique over
    (provider, entity_kind, external_id) among rows with `valid_to IS NULL`, so a second
    row would mean the loader minted a duplicate the constraint was meant to prevent.
    """
    predicate = (
        f"provider = {quote_literal(provider)} "
        f"AND entity_kind = {quote_literal(ALIAS_ENTITY_KIND)} "
        f"AND external_id = {quote_literal(external_id)} "
        "AND valid_to IS NULL"
    )

    def probe() -> Check:
        value = stack.psql(f"SELECT count(*) FROM provider_aliases WHERE {predicate}")  # noqa: S608
        count = int(value) if value.isdigit() else 0
        return Check(name, count == 1, f"count(provider_aliases WHERE provider = {provider}, external_id = {external_id}) -> {count}, expected 1")

    return probe


def _alias_points_at_the_release(stack: Stack, name: str, provider: str, external_id: str, release_key: str) -> Callable[[], Check]:
    """Return a probe asserting the alias resolves to the release this run published.

    A minted row proves the loader read the identifiers block; it does not prove the row
    was attached to the right item. ADR 0009 makes `releases.gm_item_id` the native id the
    Discogs release resolved to, so the alias is only useful if its `native_id` is that
    same id. `gm_item_id IS NOT NULL` is asserted in the same expression because two NULLs
    do not compare equal in SQL and would leave the comparison NULL rather than false.
    """
    predicate = (
        f"a.provider = {quote_literal(provider)} "
        f"AND a.entity_kind = {quote_literal(ALIAS_ENTITY_KIND)} "
        f"AND a.external_id = {quote_literal(external_id)} "
        "AND a.valid_to IS NULL"
    )
    sql = (
        "SELECT a.native_id = r.gm_item_id AND r.gm_item_id IS NOT NULL "  # noqa: S608
        f"FROM provider_aliases a CROSS JOIN releases r WHERE r.data_id = {release_key} AND {predicate}"
    )

    def probe() -> Check:
        value = stack.psql(sql)
        if value == "":
            return Check(name, False, f"no {provider} alias for {external_id} beside a releases row for {release_key}")
        return Check(name, value == "t", f"provider_aliases.native_id = releases.gm_item_id for {release_key} -> {value}")

    return probe


def _credited_to_company(stack: Stack, name: str, release_id: str, credit: dict[str, Any]) -> Callable[[], Check]:
    """Return a probe asserting the release is credited to the company under its role.

    The company id, the role category, and this provider's `source` are all in the match
    pattern, so the traversal proves the edge ADR 0011 specifies rather than any edge; the
    company's name is what comes back, so the node is asserted to be the right one and not
    just a node bearing the right id.
    """
    identity = quote_literal(company_id(credit))
    expected_name = str(credit["name"])
    query = (
        f"MATCH (:Release {{id: {release_id}}})"
        f"-[:CREDITED_TO {{role_category: {quote_literal(PRESSING_ROLE_CATEGORY)}, source: {quote_literal(COMPANY_SOURCE)}}}]->"
        f"(c:Company {{id: {identity}}}) RETURN c.name AS value"
    )
    return _graph_value_matches(
        stack, name, query, expected_name, f"(:Release {{id: {release_id}}})-[:CREDITED_TO]->(:Company {{id: {identity}}}).name"
    )


def _release_country(stack: Stack, name: str, release_id: str, country: str) -> Callable[[], Check]:
    """Return a probe asserting the release node carries the country the event published."""
    return _graph_value_matches(
        stack,
        name,
        f"MATCH (r:Release {{id: {release_id}}}) RETURN r.country AS value",
        country,
        f"(:Release {{id: {release_id}}}).country",
    )


def _lookup_resolves(api: ApiClient, name: str, provider: str, value: str, external_id: str, release_id: str) -> Callable[[], Check]:
    """Return a probe asserting the API resolves the printed identifier to this release.

    This is the hop that makes the alias worth minting: the two store probes above prove a
    row and an edge exist, and only this one proves a person holding the record can get
    back to it. ADR 0011 has the endpoint normalise the value with the namespace's own
    rule, so the probe sends the value **as printed**, spaces and all, and asserts the
    `normalized` field equals the `external_id` the producer derived — sending the already
    normalized value would leave the endpoint's normalisation untested.

    The `releases` entries are read tolerantly. The endpoint ships in wave 3 and its per
    release shape is not pinned by a promoted contract this repository holds, so the probe
    asserts the fact ADR 0011 states — the release comes back — without asserting a field
    layout it would have to guess.
    """
    path = f"/api/lookup/{urllib.parse.quote(provider, safe='')}/{urllib.parse.quote(value, safe='')}"

    def probe() -> Check:
        try:
            response = api.request("GET", path)
        except SmokeError as error:
            # The stack publishes the API without waiting on its healthcheck, so a refused
            # connection is a not-yet rather than a verdict; `wait_for` re-evaluates.
            return Check(name, False, f"GET {path} -> {error}")
        if response.status != 200:
            return Check(name, False, f"GET {path} -> {response.status}, expected 200")
        try:
            payload = response.json()
        except SmokeError as error:
            return Check(name, False, f"GET {path} -> {error}")
        if not isinstance(payload, dict):
            return Check(name, False, f"GET {path} -> {type(payload).__name__}, expected an object")
        normalized = payload.get("normalized")
        resolved = _release_ids(payload.get("releases"))
        matched = normalized == external_id and bool(payload.get("gm_id")) and release_id in resolved
        detail = (
            f"GET {path} -> normalized {json.dumps(normalized)}, gm_id {json.dumps(payload.get('gm_id'))}, releases {json.dumps(sorted(resolved))}"
        )
        return Check(name, matched, f"{detail}; expected normalized {json.dumps(external_id)}, a gm_id, and release {release_id}")

    return probe


def _release_ids(releases: Any) -> set[str]:
    """Return the release identifiers a lookup answer names, in whatever shape it names them."""
    if not isinstance(releases, list):
        return set()
    found: set[str] = set()
    for entry in releases:
        if isinstance(entry, dict):
            found.update(str(entry[key]) for key in ("id", "data_id", "discogs_id", "release_id") if entry.get(key) is not None)
        elif entry is not None:
            found.add(str(entry))
    return found


def identifier_probes(stack: Stack, api: ApiClient, event: dict[str, Any]) -> list[Callable[[], Check]]:
    """Return every assertion ADR 0011's identifier path must satisfy for this event.

    These are separate from `discogs_probes` because they are asserted only on the path
    that publishes the promoted contract fixtures. The packaged extractor fixture the
    released-fixture smoke replays documents the media block, its stack runs no API, and a
    probe that quietly became a no-op there would be worse than one that is not offered.

    Every probe names an id this run's own event carries — the release id, the barcode the
    producer derived, or the credited company's Discogs id — for the same reason the media
    probes do: an unscoped count is answered by whatever a reused volume already held.
    """
    release_id = quote_literal(str(event["id"]))
    barcode = alias_external_id(event, BARCODE_PROVIDER)
    printed_barcode = identifier_value(event, BARCODE_PROVIDER)
    credit = pressing_credit(event)
    return [
        _alias_row_minted(stack, f"postgres {BARCODE_PROVIDER} alias row", BARCODE_PROVIDER, barcode),
        _alias_points_at_the_release(stack, f"postgres {BARCODE_PROVIDER} alias native id", BARCODE_PROVIDER, barcode, release_id),
        _credited_to_company(stack, f"neo4j CREDITED_TO {credit['name']}", release_id, credit),
        _release_country(stack, "neo4j Release country", release_id, str(event["country"])),
        _lookup_resolves(api, f"api lookup {BARCODE_PROVIDER}", BARCODE_PROVIDER, printed_barcode, barcode, str(event["id"])),
    ]


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
        _graph_families_match(stack, "neo4j Release media_families", release_id, media_families(event)),
    ]
    # Every graph probe is scoped to an id this run's own event carries. An unscoped
    # `count(:Medium)` or `count(:MediaFamily)` is satisfied by any node left behind in a
    # reused volume, so a run whose writes never landed would still report a pass.
    for family in media_families(event):
        literal = quote_literal(family)
        probes.append(
            _counts(
                stack,
                f"neo4j MediaFamily {family} node",
                f"MATCH (f:MediaFamily {{name: {literal}}}) RETURN count(f) AS value",
                f"count(:MediaFamily {{name: {family}}})",
            )
        )
    for medium in media_medium_ids(event):
        literal = quote_literal(medium)
        probes.append(
            _counts(
                stack,
                f"neo4j Medium {medium} node",
                f"MATCH (m:Medium {{id: {literal}}}) RETURN count(m) AS value",
                f"count(:Medium {{id: {medium}}})",
            )
        )
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
    parser = argparse.ArgumentParser(description="Assert the ADR 0007 canonical media block and the ADR 0011 identifier path end to end.")
    parser.add_argument("--project", required=True, help="Compose project name of the disposable smoke stack")
    parser.add_argument("--compose-file", action="append", required=True, dest="compose_files", help="Compose file, repeatable and order-significant")
    parser.add_argument("--broker-port", type=int, required=True, help="Published loopback port of the RabbitMQ management API")
    parser.add_argument("--api-port", type=int, help="Published loopback port of the catalog API, required unless --extractor-service is given")
    parser.add_argument("--env-file", help="Environment file used to resolve digest-pinned Compose images")
    parser.add_argument("--extractor-service", help="Run this one-shot extractor instead of publishing promoted event fixtures")
    parser.add_argument("--timeout", type=float, default=300.0, help="Seconds to wait for each stage before failing")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Publish the fixture events, wait for both stores, and report every assertion."""
    args = parse_args(argv)
    stack = Stack(args.project, args.compose_files, args.broker_port, args.env_file)

    if args.extractor_service:
        print(f"waiting for {len(DISCOGS_CONSUMER_QUEUES)} Discogs contract queues to bind a consumer")
        wait_for_consumers(stack, time.monotonic() + args.timeout, DISCOGS_CONSUMER_QUEUES)
        expected_stream = stack.compose_run(
            args.extractor_service,
            [PACKAGED_EXTRACTOR_EXPECTED_EVENTS],
            entrypoint="cat",
        )
        expected_events = [json.loads(line) for line in expected_stream.splitlines() if line]
        data_events = [event for event in expected_events if event.get("type") == "data"]
        if len(data_events) != 1:
            raise SmokeError(f"packaged extractor fixture declared {len(data_events)} data events; expected exactly one")
        discogs = data_events[0]
        if not isinstance(discogs.get("media"), dict):
            raise SmokeError("packaged extractor fixture data event carries no canonical media block")

        print(f"running {args.extractor_service} against its packaged v1 local manifest")
        stack.compose_run(args.extractor_service, timeout=args.timeout)
        results = wait_for(discogs_probes(stack, discogs), time.monotonic() + args.timeout)
        print(render(results))
        return exit_code(results)

    if args.api_port is None:
        # The identifier path ends at the API. A run that could not ask it would report a
        # tidy set of store assertions and silently drop the one hop that makes the alias
        # worth minting, so it refuses to start rather than assert less than it claims.
        raise SmokeError("--api-port is required: the identifier assertion resolves a barcode through the stack's catalog API")
    api = ApiClient(f"http://127.0.0.1:{args.api_port}", timeout=min(args.timeout, 120.0))

    discogs = discogs_event(load_fixture("discogs-releases.data.json"))
    musicbrainz = musicbrainz_event(load_fixture("musicbrainz-releases.data.json"))

    print(f"waiting for {len(CONSUMER_QUEUES)} contract queues to bind a consumer")
    wait_for_consumers(stack, time.monotonic() + args.timeout)

    # The Discogs release goes first and is asserted before the MusicBrainz one is
    # published: the MusicBrainz enricher creates no release, it only matches one that the
    # Discogs enricher already put in the graph (ADR 0007, "Storage").
    print(f"publishing the promoted Discogs fixture as release {discogs['id']} onto {DISCOGS_EXCHANGE}")
    stack.publish(DISCOGS_EXCHANGE, discogs)
    discogs_results = wait_for([*discogs_probes(stack, discogs), *identifier_probes(stack, api, discogs)], time.monotonic() + args.timeout)
    if not all(result.passed for result in discogs_results):
        print(render(discogs_results, "media and identifier"))
        return 1

    print(f"publishing the promoted MusicBrainz fixture as release {musicbrainz['id']} onto {MUSICBRAINZ_EXCHANGE}")
    stack.publish(MUSICBRAINZ_EXCHANGE, musicbrainz)
    musicbrainz_results = wait_for(musicbrainz_probes(stack, musicbrainz, str(discogs["id"])), time.monotonic() + args.timeout)

    results = discogs_results + musicbrainz_results
    print(render(results, "media and identifier"))
    return exit_code(results)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SmokeError as error:
        print(f"smoke-media: {error}", file=sys.stderr)
        sys.exit(1)
