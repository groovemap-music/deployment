"""Regression tests for the end-to-end canonical media and identifier assertion.

ADR 0007 is delivered only when a release event carrying the canonical `media` block
reaches both stores (gm-deployment-989.2), and ADR 0011 only when the `identifiers` and
`companies` blocks and the country on that same event reach the alias table, the graph, and
the lookup endpoint (gm-deployment-xd8.1). `just smoke-media` is that proof, and it is a
versioned script rather than a manual runbook step so both claims can be re-made on demand.

The assertion itself starts containers, so it stays outside `just check` and CI. What is
checkable without containers, and pinned here:

- the release inputs are the producers' contract fixtures promoted verbatim, with their
  provenance recorded, so the smoke stack asserts against the shape the producers publish
  rather than a locally invented payload;
- the fixture-to-event translation only rewrites the identity fields the stores constrain
  and adds the country the contract fixture omits, and never either canonical block;
- the identifier probe list in full, and every probe in it scoped to an id the event
  carries — a `provider_aliases` count or a `CREDITED_TO` traversal that named no id would
  be answered by whatever a reused volume already held;
- the lookup probe sending the value as printed, so the endpoint's own normalisation is
  what is under test rather than this script's;
- every probe reports a failure as a failure, and a failed run exits non-zero;
- the disposable overlay cannot collide with an operator's environment: its own project
  container names, its own subnet, and no published port but the loopback broker and API
  endpoints;
- the recipe exists, renders, and is documented, and neither `just check` nor CI runs it;
- the runbook records which images have to be released and reviewed before the identifier
  probes can pass at all.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import itertools
import json
import os
import subprocess
import sys
import urllib.error
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
OVERLAY = REPO_ROOT / "docker-compose.media-smoke.yml"

# Services the assertion starts, directly or through a declared dependency. Each must run
# under a Compose-generated name so a smoke run cannot collide with a live environment.
SMOKE_SERVICES = (
    "rabbitmq",
    "postgres",
    "neo4j",
    "victoria-metrics",
    "victoria-traces",
    "otel-collector",
    "schema-init",
    "tableinator",
    "brainztableinator",
    "graphinator",
    "brainzgraphinator",
    "api",
)

# The internal images the smoke stack runs. Their platform is named per service so the
# emulation an operator may need stays off the broker and both stores.
PLATFORM_PINNED_SERVICES = ("schema-init", "tableinator", "brainztableinator", "graphinator", "brainzgraphinator", "api")

# The two endpoints the run needs from the host, and the only two the overlay may publish:
# the broker it publishes fixture events onto, and the API the barcode lookup resolves
# through. Both on loopback.
PUBLISHED_PORTS = [
    "127.0.0.1:${SMOKE_MEDIA_RABBITMQ_PORT:-15673}:15672",
    "127.0.0.1:${SMOKE_MEDIA_API_PORT:-18005}:8004",
]


class ComposeLoader(yaml.SafeLoader):
    """Read a Compose file that carries merge tags, without asking Compose to render it."""


def _compose_tag(loader: yaml.SafeLoader, node: yaml.Node) -> Any:
    """Resolve `!override` and `!reset` to the value they leave behind after a merge."""
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


def _load_module() -> Any:
    """Import the asserter by path — `scripts/` is a script directory, not a package."""
    spec = importlib.util.spec_from_file_location("smoke_media", REPO_ROOT / "scripts" / "smoke_media.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before execution so the module's dataclasses can resolve their own module.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


smoke_media = _load_module()


class FakeStack:
    """A stand-in for the live stack that replays canned store answers."""

    def __init__(self, sql: dict[str, str] | None = None, cypher: dict[str, str] | None = None) -> None:
        self._sql = sql or {}
        self._cypher = cypher or {}
        self.sql_queries: list[str] = []
        self.cypher_queries: list[str] = []

    def psql(self, sql: str) -> str:
        self.sql_queries.append(sql)
        return self._sql.get(sql, "")

    def cypher(self, query: str) -> str:
        self.cypher_queries.append(query)
        return self._cypher.get(query, "")


class FakeApi:
    """A stand-in for the stack's catalog API that replays one canned answer."""

    def __init__(self, answer: Any = None) -> None:
        self._answer = answer
        self.paths: list[str] = []

    def request(self, method: str, path: str, **_kwargs: Any) -> Any:
        self.paths.append(path)
        if isinstance(self._answer, Exception):
            raise self._answer
        if self._answer is None:
            raise smoke_media.SmokeError(f"{method} {path} could not reach the stack's API: Connection refused")
        return self._answer


def discogs_smoke_event() -> Any:
    """Return the Discogs event the smoke publishes, from the promoted fixture."""
    return smoke_media.discogs_event(smoke_media.load_fixture("discogs-releases.data.json"))


def graph_answers(release_id: str = smoke_media.DISCOGS_RELEASE_ID) -> dict[str, str]:
    """Return the Cypher answers a stack that persisted this run's write would give."""
    return {
        f"MATCH (r:Release {{id: '{release_id}'}}) RETURN r.media_families AS value": '["vinyl"]',
        "MATCH (f:MediaFamily {name: 'vinyl'}) RETURN count(f) AS value": "1",
        "MATCH (m:Medium {id: 'vinyl_12'}) RETURN count(m) AS value": "1",
        "MATCH (:Medium {id: 'vinyl_12'})-[:IN_FAMILY]->(f:MediaFamily) RETURN count(f) AS value": "1",
        f"MATCH (:Release {{id: '{release_id}'}})-[:ISSUED_ON {{source: 'discogs'}}]->(:Medium {{id: 'vinyl_12'}}) RETURN count(*) AS value": "1",
    }


def store_answers(release_id: str = smoke_media.DISCOGS_RELEASE_ID) -> dict[str, str]:
    """Return the PostgreSQL answers a stack that persisted this run's write would give.

    These are canned dictionary keys matched against what the probes ask for; nothing here
    reaches a database.
    """
    return {
        f"SELECT media IS NOT NULL FROM releases WHERE data_id = '{release_id}'": "t",  # noqa: S608
        f"SELECT media->'families' FROM releases WHERE data_id = '{release_id}'": '["vinyl"]',  # noqa: S608
    }


def test_release_inputs_are_promoted_producer_fixtures_with_recorded_provenance() -> None:
    provenance = json.loads((REPO_ROOT / "config" / "provenance.json").read_text())
    promoted = {
        "media-smoke/discogs-releases.data.json": "groovemap-music/discogs-ingestion",
        "media-smoke/musicbrainz-releases.data.json": "groovemap-music/musicbrainz-ingestion",
    }
    for relative_path, owner in promoted.items():
        record = provenance[relative_path]
        assert record["owner"] == owner
        assert record["source_path"] == "contracts/catalog-events/v1/fixtures/" + Path(relative_path).name
        digest = hashlib.sha256((REPO_ROOT / "config" / relative_path).read_bytes()).hexdigest()
        assert digest == record["promoted_sha256"]
        # Promoted verbatim: a locally edited fixture would no longer prove anything about
        # what the producers publish.
        assert digest == record["source_sha256"]


def test_promoted_fixtures_carry_the_canonical_media_block() -> None:
    discogs = smoke_media.load_fixture("discogs-releases.data.json")
    musicbrainz = smoke_media.load_fixture("musicbrainz-releases.data.json")

    assert discogs["formats"], "the Discogs fixture must keep the raw provider formats alongside the canonical block"
    assert musicbrainz["media_raw"], "the MusicBrainz fixture must keep the raw medium list alongside the canonical block"
    for fixture in (discogs, musicbrainz):
        assert fixture["media"]["taxonomy_version"] == "1"
        assert smoke_media.media_families(fixture) == ["vinyl"]
        assert smoke_media.media_medium_ids(fixture) == ["vinyl_12"]


def test_events_rewrite_only_the_identity_fields_the_stores_constrain() -> None:
    discogs_fixture = smoke_media.load_fixture("discogs-releases.data.json")
    musicbrainz_fixture = smoke_media.load_fixture("musicbrainz-releases.data.json")

    discogs = smoke_media.discogs_event(discogs_fixture)
    musicbrainz = smoke_media.musicbrainz_event(musicbrainz_fixture)

    assert discogs["media"] == discogs_fixture["media"]
    assert musicbrainz["media"] == musicbrainz_fixture["media"]
    assert discogs["type"] == "data" and musicbrainz["type"] == "data"

    # `releases.data_id` is a VARCHAR, `musicbrainz.releases.mbid` is a UUID, and
    # `musicbrainz.releases.discogs_release_id` is a BIGINT that the MusicBrainz enricher
    # also uses to find the release node the Discogs enricher created.
    assert discogs["id"] == smoke_media.DISCOGS_RELEASE_ID
    assert uuid.UUID(str(musicbrainz["id"]))
    assert musicbrainz["discogs_release_id"] == int(smoke_media.DISCOGS_RELEASE_ID)


def test_event_digest_is_non_empty_stable_and_excludes_itself() -> None:
    fixture = smoke_media.load_fixture("discogs-releases.data.json")
    assert fixture["sha256"] == "", "the published fixture carries no digest of its own"

    event = smoke_media.discogs_event(fixture)
    assert len(event["sha256"]) == 64
    assert smoke_media.discogs_event(fixture)["sha256"] == event["sha256"]
    # The enrichers skip a record whose digest matches the stored one, so the digest must
    # be computed over the payload without its own field or it could never be reproduced.
    assert smoke_media.payload_sha256(event) == event["sha256"]


def test_store_literals_are_refused_unless_they_are_identifier_shaped() -> None:
    assert smoke_media.quote_literal("vinyl_12") == "'vinyl_12'"
    with pytest.raises(smoke_media.SmokeError):
        smoke_media.quote_literal("999000001'; DROP TABLE releases; --")


def test_probes_report_a_missing_row_and_a_wrong_family_list_as_failures() -> None:
    event = smoke_media.discogs_event(smoke_media.load_fixture("discogs-releases.data.json"))
    empty = smoke_media.discogs_probes(FakeStack(), event)
    results = [probe() for probe in empty]

    assert not any(result.passed for result in results)
    assert smoke_media.exit_code(results) == 1
    assert "no releases row" in results[0].detail

    key = "SELECT media->'families' FROM releases WHERE data_id = '999000001'"
    wrong = smoke_media.discogs_probes(FakeStack(sql={key: '["optical"]'}), event)[1]()
    assert not wrong.passed
    assert '["optical"]' in wrong.detail and '["vinyl"]' in wrong.detail


def test_every_graph_probe_is_scoped_to_an_id_the_event_carries() -> None:
    event = discogs_smoke_event()
    stack = FakeStack()
    for probe in smoke_media.discogs_probes(stack, event):
        probe()

    assert stack.cypher_queries, "the Discogs probes must ask the graph something"
    # An unscoped count is answered by whatever a reused volume already held, so a run
    # whose own write never landed would still report a pass.
    scoping_ids = {str(event["id"]), *smoke_media.media_families(event), *smoke_media.media_medium_ids(event)}
    for query in stack.cypher_queries:
        assert "MATCH (m:Medium) RETURN" not in query, f"unscoped Medium count: {query}"
        assert "MATCH (f:MediaFamily) RETURN" not in query, f"unscoped MediaFamily count: {query}"
        assert any(f"'{scope}'" in query for scope in scoping_ids), f"a graph probe named no id from the event: {query}"

    assert "MATCH (m:Medium {id: 'vinyl_12'}) RETURN count(m) AS value" in stack.cypher_queries
    assert "MATCH (f:MediaFamily {name: 'vinyl'}) RETURN count(f) AS value" in stack.cypher_queries


def test_a_reused_volume_holding_only_unrelated_media_fails_the_run() -> None:
    event = discogs_smoke_event()
    # A previous, unrelated run left a CD medium and its family behind, and this run wrote
    # nothing. The old unscoped counts were satisfied by exactly this state.
    leftovers = {
        "MATCH (m:Medium) RETURN count(m) AS value": "7",
        "MATCH (f:MediaFamily) RETURN count(f) AS value": "3",
        "MATCH (m:Medium {id: 'cd'}) RETURN count(m) AS value": "1",
    }
    stack = FakeStack(sql=store_answers(), cypher=leftovers)
    results = [probe() for probe in smoke_media.discogs_probes(stack, event)]

    assert smoke_media.exit_code(results) == 1
    failed = {result.name for result in results if not result.passed}
    assert "neo4j Medium vinyl_12 node" in failed
    assert "neo4j MediaFamily vinyl node" in failed
    assert "neo4j Release media_families" in failed


def test_every_discogs_probe_passes_when_the_run_actually_wrote() -> None:
    event = discogs_smoke_event()
    stack = FakeStack(sql=store_answers(), cypher=graph_answers())
    results = [probe() for probe in smoke_media.discogs_probes(stack, event)]

    assert smoke_media.exit_code(results) == 0, smoke_media.render(results)


# ---------------------------------------------------------------------------
# ADR 0011: identifier aliases, manufacturing credits, country, and lookup
# ---------------------------------------------------------------------------

# The values the promoted fixture carries, spelled out rather than derived, so a fixture
# that silently loses its barcode or its pressing plant fails here instead of quietly
# shrinking the assertion.
PRINTED_BARCODE = "5 012394 144777"
NORMALIZED_BARCODE = "5012394144777"
MATRIX_INSCRIPTION = "PB 41447-A2 UTOPIA MS"
PRESSING_COMPANY_ID = "12345"
PRESSING_COMPANY_NAME = "Damont"

# The probe list, in full. A deletion or a rename here is a change to what the run claims
# to have proved, so it is a fixture rather than an implementation detail.
IDENTIFIER_PROBE_NAMES = [
    "postgres barcode alias row",
    "postgres barcode alias native id",
    "neo4j CREDITED_TO Damont",
    "neo4j Release country",
    "api lookup barcode",
]

LOOKUP_PATH = "/api/lookup/barcode/5%20012394%20144777"


def lookup_answer(**overrides: Any) -> Any:
    """Return the lookup answer a stack that resolved this run's barcode would give."""
    payload = {
        "provider": "barcode",
        "value": PRINTED_BARCODE,
        "normalized": NORMALIZED_BARCODE,
        "gm_id": "018f2c0e-0000-7000-8000-00000000c0de",
        "releases": [{"id": smoke_media.DISCOGS_RELEASE_ID, "title": "Contract Release"}],
    }
    payload.update(overrides)
    return smoke_media.Response(200, json.dumps(payload), "application/json")


def identifier_store_answers(release_id: str = smoke_media.DISCOGS_RELEASE_ID) -> dict[str, str]:
    """Return the PostgreSQL answers a stack that minted this run's alias would give."""
    stack = FakeStack()
    probes = smoke_media.identifier_probes(stack, FakeApi(lookup_answer()), discogs_smoke_event())
    for probe in probes:
        probe()
    assert len(stack.sql_queries) == 2, stack.sql_queries
    assert release_id in stack.sql_queries[1]
    return {stack.sql_queries[0]: "1", stack.sql_queries[1]: "t"}


def identifier_graph_answers() -> dict[str, str]:
    """Return the Cypher answers a stack that persisted this run's writes would give."""
    stack = FakeStack()
    for probe in smoke_media.identifier_probes(stack, FakeApi(lookup_answer()), discogs_smoke_event()):
        probe()
    credited, country = stack.cypher_queries
    return {credited: f'"{PRESSING_COMPANY_NAME}"', country: f'"{smoke_media.DISCOGS_RELEASE_COUNTRY}"'}


def identifier_results(
    sql: dict[str, str] | None = None,
    cypher: dict[str, str] | None = None,
    answer: Any = None,
) -> dict[str, Any]:
    """Return every identifier probe's result by name, against canned store and API answers."""
    stack = FakeStack(sql=sql, cypher=cypher)
    api = FakeApi(lookup_answer() if answer is None else answer)
    return {result.name: result for result in (probe() for probe in smoke_media.identifier_probes(stack, api, discogs_smoke_event()))}


def passing_identifier_results(**overrides: Any) -> dict[str, Any]:
    """Return the probe results for a run whose writes all landed, before any override."""
    return identifier_results(sql=identifier_store_answers(), cypher=identifier_graph_answers(), **overrides)


def test_promoted_fixture_carries_the_identifiers_and_companies_blocks() -> None:
    fixture = smoke_media.load_fixture("discogs-releases.data.json")

    # One barcode and one matrix inscription: the two alias-bearing identifier types the
    # assertion is built on, the first resolved through lookup and the second carried so
    # the block the loader reads is the one the producer publishes.
    assert smoke_media.alias_external_id(fixture, smoke_media.BARCODE_PROVIDER) == NORMALIZED_BARCODE
    assert smoke_media.alias_external_id(fixture, smoke_media.MATRIX_PROVIDER) == MATRIX_INSCRIPTION
    assert smoke_media.identifier_value(fixture, smoke_media.BARCODE_PROVIDER) == PRINTED_BARCODE

    # The normalization is the producer's, not this repository's: a barcode's alias is its
    # digits, so the grouping spaces a sleeve prints are gone by the time it is a key.
    assert PRINTED_BARCODE != NORMALIZED_BARCODE
    assert NORMALIZED_BARCODE.isdigit()

    credit = smoke_media.pressing_credit(fixture)
    assert credit["name"] == PRESSING_COMPANY_NAME
    assert credit["role_category"] == smoke_media.PRESSING_ROLE_CATEGORY
    assert smoke_media.company_id(credit) == PRESSING_COMPANY_ID


def test_an_absent_alias_or_credit_is_an_error_rather_than_a_hollow_probe() -> None:
    # A probe built on an alias the block never minted would assert against an empty string
    # and pass for the wrong reason, so the accessors refuse instead of returning one.
    empty: dict[str, Any] = {"identifiers": {"aliases": [], "items": []}, "companies": {"items": []}}
    with pytest.raises(smoke_media.SmokeError, match="mints no barcode alias"):
        smoke_media.alias_external_id(empty, smoke_media.BARCODE_PROVIDER)
    with pytest.raises(smoke_media.SmokeError, match="no barcode identifier"):
        smoke_media.identifier_value(empty, smoke_media.BARCODE_PROVIDER)
    with pytest.raises(smoke_media.SmokeError, match="no pressing credit"):
        smoke_media.pressing_credit(empty)

    # A company the source gave no usable Discogs id is keyed on its name upstream, which
    # is not an identity this run can inline into a Cypher literal.
    for unusable in (None, 0, True, "12345"):
        with pytest.raises(smoke_media.SmokeError, match="no usable Discogs id"):
            smoke_media.company_id({"discogs_id": unusable, "name": PRESSING_COMPANY_NAME})


def test_the_published_event_carries_the_country_the_contract_fixture_omits() -> None:
    fixture = smoke_media.load_fixture("discogs-releases.data.json")
    event = smoke_media.discogs_event(fixture)

    # Country is a raw Discogs passthrough rather than a canonical block, so the producer's
    # representative fixture documents no country and the enricher would have nothing to
    # project onto `Release.country` unless the event builder supplied one.
    assert "country" not in fixture
    assert event["country"] == smoke_media.DISCOGS_RELEASE_COUNTRY

    # Both blocks travel verbatim, exactly as the media block does.
    assert event["identifiers"] == fixture["identifiers"]
    assert event["companies"] == fixture["companies"]

    # The digest is taken after the country is set, or the enrichers' hash gate would see a
    # value that does not describe the payload they were handed.
    assert smoke_media.payload_sha256(event) == event["sha256"]


def test_identifier_probe_list_is_the_one_adr_0011_names() -> None:
    results = passing_identifier_results()

    assert list(results) == IDENTIFIER_PROBE_NAMES
    assert all(result.passed for result in results.values()), [result.detail for result in results.values()]


def test_every_identifier_probe_is_scoped_to_an_id_the_event_carries() -> None:
    event = discogs_smoke_event()
    stack = FakeStack()
    api = FakeApi(lookup_answer())
    for probe in smoke_media.identifier_probes(stack, api, event):
        probe()

    # An unscoped `count(*)` on `provider_aliases`, or a `CREDITED_TO` traversal from no
    # particular release, is answered by whatever a reused volume already held — so a run
    # whose own writes never landed would still report a pass.
    scoping_ids = {str(event["id"]), NORMALIZED_BARCODE, PRESSING_COMPANY_ID}
    for query in stack.sql_queries + stack.cypher_queries:
        assert "WHERE" in query or "{" in query, f"an unfiltered store query: {query}"
        assert any(f"'{scope}'" in query for scope in scoping_ids), f"a store probe named no id from the event: {query}"

    assert stack.sql_queries[0].startswith("SELECT count(*) FROM provider_aliases WHERE ")
    assert "entity_kind = 'release'" in stack.sql_queries[0] and "valid_to IS NULL" in stack.sql_queries[0]
    assert "a.native_id = r.gm_item_id" in stack.sql_queries[1] and "r.gm_item_id IS NOT NULL" in stack.sql_queries[1]
    assert "[:CREDITED_TO {role_category: 'pressing', source: 'discogs'}]" in stack.cypher_queries[0]
    assert api.paths == [LOOKUP_PATH]


def test_the_lookup_probe_sends_the_value_as_printed_not_as_normalized() -> None:
    api = FakeApi(lookup_answer())
    for probe in smoke_media.identifier_probes(FakeStack(), api, discogs_smoke_event()):
        probe()

    # ADR 0011 has the endpoint normalise with the namespace's own rule. Sending the already
    # normalized value would leave that rule untested and the probe would pass against an
    # endpoint that did no normalisation at all.
    assert NORMALIZED_BARCODE not in api.paths[0]
    assert api.paths[0] == LOOKUP_PATH


def test_identifier_probes_all_fail_against_a_store_that_holds_nothing() -> None:
    results = identifier_results(answer=smoke_media.Response(404, json.dumps({"detail": "Not found"}), "application/json"))

    assert list(results) == IDENTIFIER_PROBE_NAMES
    assert not any(result.passed for result in results.values())
    assert smoke_media.exit_code(list(results.values())) == 1
    assert "expected 1" in results["postgres barcode alias row"].detail
    assert "no barcode alias for" in results["postgres barcode alias native id"].detail
    assert "nothing" in results["neo4j Release country"].detail
    assert "404" in results["api lookup barcode"].detail


def test_a_second_valid_alias_row_is_a_failure_not_a_pass() -> None:
    sql = identifier_store_answers()
    duplicate = dict(sql)
    duplicate[next(iter(sql))] = "2"
    results = identifier_results(sql=duplicate, cypher=identifier_graph_answers())

    # Uniqueness over (provider, entity_kind, external_id) among currently valid rows is
    # what makes the lookup exact, so a second row is the constraint having failed.
    assert not results["postgres barcode alias row"].passed
    assert "-> 2, expected 1" in results["postgres barcode alias row"].detail


def test_an_alias_attached_to_another_release_fails_the_native_id_probe() -> None:
    sql = identifier_store_answers()
    elsewhere = dict(sql)
    native_id_query = next(query for query in sql if "gm_item_id" in query)
    elsewhere[native_id_query] = "f"
    results = identifier_results(sql=elsewhere, cypher=identifier_graph_answers())

    # A minted row proves the loader read the block; only the native id proves it attached
    # the alias to the release this run published.
    assert results["postgres barcode alias row"].passed
    assert not results["postgres barcode alias native id"].passed
    assert "-> f" in results["postgres barcode alias native id"].detail


def test_a_credit_to_the_wrong_company_or_no_company_fails() -> None:
    graph = identifier_graph_answers()
    credited_query = next(query for query in graph if "CREDITED_TO" in query)

    wrong = identifier_results(sql=identifier_store_answers(), cypher=graph | {credited_query: '"Utopia Studios"'})
    assert not wrong["neo4j CREDITED_TO Damont"].passed
    assert "Utopia Studios" in wrong["neo4j CREDITED_TO Damont"].detail and PRESSING_COMPANY_NAME in wrong["neo4j CREDITED_TO Damont"].detail

    missing = identifier_results(sql=identifier_store_answers(), cypher={key: value for key, value in graph.items() if key != credited_query})
    assert not missing["neo4j CREDITED_TO Damont"].passed
    assert "nothing" in missing["neo4j CREDITED_TO Damont"].detail


def test_a_release_node_with_the_wrong_country_fails() -> None:
    graph = identifier_graph_answers()
    country_query = next(query for query in graph if "r.country" in query)
    results = identifier_results(sql=identifier_store_answers(), cypher=graph | {country_query: '"US"'})

    assert not results["neo4j Release country"].passed
    assert '"US"' in results["neo4j Release country"].detail and '"UK"' in results["neo4j Release country"].detail


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (smoke_media.Response(200, "<html>502</html>", "text/html"), "expected a JSON body"),
        (smoke_media.Response(200, json.dumps(["not", "an", "object"]), "application/json"), "expected an object"),
        (smoke_media.Response(500, "boom", "text/plain"), "expected 200"),
    ],
)
def test_a_lookup_answer_that_is_not_a_resolution_is_a_failed_probe(answer: Any, expected: str) -> None:
    results = passing_identifier_results(answer=answer)

    assert not results["api lookup barcode"].passed
    assert expected in results["api lookup barcode"].detail
    # The other four still hold: a wrong answer from the API is reported as itself rather
    # than taken as evidence about the stores.
    assert all(result.passed for name, result in results.items() if name != "api lookup barcode")


@pytest.mark.parametrize(
    "overrides",
    [
        {"normalized": PRINTED_BARCODE},
        {"gm_id": None},
        {"releases": []},
        {"releases": [{"id": "999000999"}]},
    ],
)
def test_a_lookup_that_resolves_to_the_wrong_thing_fails(overrides: dict[str, Any]) -> None:
    results = passing_identifier_results(answer=lookup_answer(**overrides))

    assert not results["api lookup barcode"].passed
    assert smoke_media.DISCOGS_RELEASE_ID in results["api lookup barcode"].detail


def test_an_api_that_is_not_up_yet_is_a_failed_probe_rather_than_an_aborted_run() -> None:
    # The stack publishes the API without waiting on its healthcheck, so a refused
    # connection during the first evaluation is a not-yet. `wait_for` re-evaluates every
    # probe until the deadline, which it could not do if the probe raised.
    results = passing_identifier_results(answer=smoke_media.SmokeError("GET /api/lookup could not reach the stack's API: Connection refused"))

    assert not results["api lookup barcode"].passed
    assert "Connection refused" in results["api lookup barcode"].detail


def test_a_lookup_answer_is_read_for_the_release_in_whatever_shape_it_names_it() -> None:
    # The endpoint ships in wave 3 and no promoted contract in this repository pins its
    # per-release shape, so the probe reads the id rather than guessing a field layout.
    assert smoke_media._release_ids([{"id": 1}, {"data_id": "2"}, "3", None]) == {"1", "2", "3"}
    assert smoke_media._release_ids("not a list") == set()
    assert smoke_media._release_ids(None) == set()


def test_graph_release_media_families_must_match_the_event() -> None:
    event = discogs_smoke_event()
    query = f"MATCH (r:Release {{id: '{smoke_media.DISCOGS_RELEASE_ID}'}}) RETURN r.media_families AS value"

    def graph_families_probe(answer: str) -> Any:
        stack = FakeStack(sql=store_answers(), cypher=graph_answers() | {query: answer})
        by_name = {probe().name: probe() for probe in smoke_media.discogs_probes(stack, event)}
        return by_name["neo4j Release media_families"]

    assert graph_families_probe('["vinyl"]').passed

    wrong = graph_families_probe('["optical"]')
    assert not wrong.passed
    assert '["optical"]' in wrong.detail and '["vinyl"]' in wrong.detail

    # The enricher not having written the property at all is a failure, not an absence the
    # report can shrug at: it is exactly what a no-write run looks like.
    missing = graph_families_probe("")
    assert not missing.passed
    assert "nothing" in missing.detail

    # A value cypher-shell rendered in some other shape is reported verbatim rather than
    # crashing the probe on a JSON parse.
    unparseable = graph_families_probe("[vinyl]")
    assert not unparseable.passed
    assert "[vinyl]" in unparseable.detail


def test_report_names_every_assertion_and_exits_zero_only_when_all_hold() -> None:
    passing = [smoke_media.Check("postgres discogs releases.media", True, "detail")]
    failing = [*passing, smoke_media.Check("neo4j Medium nodes", False, "count(:Medium) -> 0")]

    assert smoke_media.exit_code(passing) == 0
    assert smoke_media.exit_code(failing) == 1
    assert smoke_media.exit_code([]) == 1, "a run that asserted nothing has proved nothing"

    report = smoke_media.render(failing)
    assert "PASS  postgres discogs releases.media" in report
    assert "FAIL  neo4j Medium nodes" in report
    assert "1/2 media assertions passed" in report


def offline_stack(exec_output: str = "", consumers: int = 0, routed: bool = True) -> Any:
    """Return a live Stack whose container and broker calls answer from canned values."""
    stack = smoke_media.Stack("groovemap-media-smoke-test", ["docker-compose.yml"], 15673)
    stack.compose_exec = lambda *_args, **_kwargs: exec_output
    stack.queue_consumers = lambda *_args, **_kwargs: consumers
    stack._management = lambda *_args, **_kwargs: {"routed": routed}
    return stack


def test_compose_project_directory_is_opt_in_and_shared_by_stack_invocations() -> None:
    default = smoke_media.Stack("isolated", ["/issue/docker-compose.yml"], env_file="/issue/.env")
    assert "--project-directory" not in default.compose_argv()

    shared = smoke_media.Stack("isolated", ["/issue/docker-compose.yml"], env_file="/issue/.env", project_directory="/shared")
    assert shared.compose_argv() == [
        "docker",
        "compose",
        "--project-name",
        "isolated",
        "--project-directory",
        "/shared",
        "--env-file",
        "/issue/.env",
        "-f",
        "/issue/docker-compose.yml",
    ]


def test_store_output_is_read_as_a_single_scalar_or_nothing() -> None:
    assert offline_stack(exec_output="value\n1\n").cypher("RETURN 1 AS value") == "1"
    # cypher-shell prints the header even when the match found nothing.
    assert offline_stack(exec_output="value\n").cypher("RETURN 1 AS value") == ""
    assert offline_stack(exec_output=" t \n").psql("SELECT true") == "t"


def test_publishing_to_an_unbound_exchange_is_an_error_not_a_silent_pass() -> None:
    stack = offline_stack(routed=False)
    # A fanout exchange discards a message that reaches no queue, so an unrouted publish
    # would leave the assertions timing out against an empty store for no visible reason.
    with pytest.raises(smoke_media.SmokeError, match="routed it to no queue"):
        stack.publish(smoke_media.DISCOGS_EXCHANGE, {"type": "data"})

    assert offline_stack(routed=True).publish(smoke_media.DISCOGS_EXCHANGE, {"type": "data"}) is None


def test_a_stack_whose_consumers_never_bind_fails_before_it_publishes() -> None:
    with pytest.raises(smoke_media.SmokeError, match="never gained a consumer"):
        smoke_media.wait_for_consumers(offline_stack(consumers=0), deadline=0.0)

    smoke_media.wait_for_consumers(offline_stack(consumers=1), deadline=0.0)


def test_waiting_returns_the_last_evaluation_rather_than_hanging() -> None:
    failing = smoke_media.Check("neo4j Medium nodes", False, "count(:Medium) -> 0")
    results = smoke_media.wait_for([lambda: failing], deadline=0.0)

    assert results == [failing]
    assert smoke_media.exit_code(results) == 1


def test_contract_queues_cover_both_loaders_and_both_enrichers() -> None:
    assert set(smoke_media.CONSUMER_QUEUES) == {
        "groovemap-discogs-tableinator-releases",
        "groovemap-discogs-graphinator-releases",
        "groovemap-musicbrainz-brainztableinator-releases",
        "groovemap-musicbrainz-brainzgraphinator-releases",
    }
    assert smoke_media.DISCOGS_EXCHANGE == "groovemap-discogs-releases"
    assert smoke_media.MUSICBRAINZ_EXCHANGE == "groovemap-musicbrainz-releases"


def test_overlay_isolates_the_run_from_an_operator_environment() -> None:
    overlay = yaml.load(OVERLAY.read_text(), Loader=ComposeLoader)  # noqa: S506
    services = overlay["services"]

    for name in SMOKE_SERVICES:
        assert services[name]["container_name"] is None, f"{name} must run under a Compose project name, not the fixed one"

    published = [port for service in services.values() for port in service.get("ports", [])]
    assert published == PUBLISHED_PORTS, "only the loopback broker and API endpoints may be published"
    assert all(port.startswith("127.0.0.1:") for port in published), "a smoke endpoint must never leave loopback"

    base = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    base_subnet = base["networks"]["groovemap"]["ipam"]["config"][0]["subnet"]
    overlay_subnet = overlay["networks"]["groovemap"]["ipam"]["config"][0]["subnet"]
    assert base_subnet not in overlay_subnet, "the disposable run must not ask for the base stack's subnet"

    for name in PLATFORM_PINNED_SERVICES:
        assert services[name]["platform"] == "${SMOKE_MEDIA_SERVICE_PLATFORM:-linux/amd64}"


class FakeHttpResponse:
    """The parts of an `http.client.HTTPResponse` the client reads."""

    def __init__(self, status: int, body: str, media_type: str) -> None:
        self.status = status
        self._body = body
        self.headers = SimpleNamespace(get_content_type=lambda: media_type)

    def read(self) -> bytes:
        return self._body.encode()

    def __enter__(self) -> FakeHttpResponse:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def test_the_api_client_carries_the_body_and_the_token_and_keeps_every_status(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[Any] = []

    def urlopen(request: Any, timeout: float = 0.0) -> FakeHttpResponse:  # noqa: ARG001
        sent.append(request)
        return FakeHttpResponse(201, '{"ok": true}', "application/json")

    monkeypatch.setattr(smoke_media.urllib.request, "urlopen", urlopen)
    client = smoke_media.ApiClient("http://127.0.0.1:18005/")
    response = client.request("POST", "/api/lookup/barcode/1", body={"a": 1}, token="smoke-token")

    assert response == smoke_media.Response(201, '{"ok": true}', "application/json")
    assert response.json() == {"ok": True}
    assert sent[0].full_url == "http://127.0.0.1:18005/api/lookup/barcode/1", "the trailing slash on the base URL must not double"
    assert sent[0].data == b'{"a": 1}'
    assert sent[0].get_header("Authorization") == "Bearer smoke-token"
    assert sent[0].get_header("Content-type") == "application/json"


def test_the_api_client_reports_a_refusal_as_a_status_and_a_transport_failure_as_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    refused = urllib.error.HTTPError("http://127.0.0.1:18005/api/lookup/barcode/1", 404, "Not Found", None, io.BytesIO(b"nope"))  # type: ignore[arg-type]
    monkeypatch.setattr(smoke_media.urllib.request, "urlopen", lambda *_a, **_k: (_ for _ in ()).throw(refused))

    # A wrong status is a fact the run wants to report as a failed assertion, not an
    # exception that loses it. A stack that cannot be reached at all is the opposite.
    assert smoke_media.ApiClient("http://127.0.0.1:18005").request("GET", "/api/lookup/barcode/1").status == 404

    monkeypatch.setattr(smoke_media.urllib.request, "urlopen", lambda *_a, **_k: (_ for _ in ()).throw(urllib.error.URLError("Connection refused")))
    with pytest.raises(smoke_media.SmokeError, match="could not reach the stack's API"):
        smoke_media.ApiClient("http://127.0.0.1:18005").request("GET", "/api/lookup/barcode/1")


def test_the_identifier_probes_are_offered_only_where_they_can_actually_run() -> None:
    event = discogs_smoke_event()
    media_only = [probe().name for probe in smoke_media.discogs_probes(FakeStack(), event)]

    # `discogs_probes` is what the released-fixture smoke replays against the packaged
    # extractor fixture, on a stack with no API. A probe that quietly became a no-op there
    # would be worse than one that is not offered, so the two lists stay separate.
    assert not set(media_only) & set(IDENTIFIER_PROBE_NAMES)
    assert all(name.startswith(("postgres ", "neo4j ")) for name in media_only)


def test_the_published_run_refuses_to_start_without_the_api_it_asserts_against() -> None:
    argv = [
        "--project",
        "groovemap-media-smoke-test",
        "--compose-file",
        "docker-compose.yml",
        "--broker-port",
        "15673",
    ]

    # A run that could not ask the API would report a tidy set of store assertions and
    # silently drop the hop that makes the alias worth minting.
    assert smoke_media.parse_args(argv).api_port is None
    with pytest.raises(smoke_media.SmokeError, match="--api-port is required"):
        smoke_media.main(argv)

    # The extractor path replays a packaged fixture on a stack with no API, so it neither
    # needs the port nor offers the identifier probes.
    assert smoke_media.parse_args([*argv, "--extractor-service", "extractor-discogs"]).extractor_service == "extractor-discogs"


def test_smoke_script_starts_the_api_and_hands_the_run_its_loopback_port() -> None:
    script = (REPO_ROOT / "scripts" / "smoke-media.sh").read_text()

    assert "services=(tableinator brainztableinator graphinator brainzgraphinator api)" in script
    assert '--api-port "$api_port"' in script
    assert 'api_port="${SMOKE_MEDIA_API_PORT:-18005}"' in script, "the adapter and the overlay must default to one port"
    assert 'project_directory="${SMOKE_MEDIA_PROJECT_DIRECTORY:-}"' in script
    assert 'compose+=(--project-directory "$project_directory")' in script
    assert 'smoke_args+=(--project-directory "$project_directory")' in script


def test_maintenance_guide_records_the_images_the_identifier_probes_need() -> None:
    maintenance = (REPO_ROOT / "docs" / "maintenance.md").read_text()

    # The probes assert behaviour no released image has yet: until the loader mints the
    # alias, the enricher writes the credit and the country, and the API answers the
    # lookup, a failing `just smoke-media` is a missing image rather than a broken stack.
    assert "0011" in maintenance, "the runbook must name the record the probes assert"
    for image in ("DISCOGS_SQL_LOADER_IMAGE", "DISCOGS_GRAPH_ENRICHER_IMAGE", "CATALOG_API_IMAGE"):
        assert image in maintenance
    assert "just smoke-media" in maintenance


def test_recipe_exists_renders_and_stays_out_of_the_credential_free_gate() -> None:
    justfile = (REPO_ROOT / "Justfile").read_text()
    assert "smoke-media:\n    bash scripts/smoke-media.sh" in justfile
    gate = next(line for line in justfile.splitlines() if line.startswith("check:"))
    assert gate == "check: source-check typecheck test", "the credential-free gate must not start containers"

    render = (REPO_ROOT / "scripts" / "check-compose.sh").read_text()
    assert "check_compose docker-compose.yml docker-compose.media-smoke.yml" in render

    workflows = REPO_ROOT / ".github" / "workflows"
    for workflow in workflows.glob("*.yml"):
        assert "smoke-media" not in workflow.read_text(), f"{workflow.name} must not start containers"


def test_smoke_script_requires_operator_supplied_digest_pinned_images() -> None:
    script = (REPO_ROOT / "scripts" / "smoke-media.sh").read_text()

    assert "REPLACE_WITH" in script, "the .env.example placeholder must be refused"
    assert "@sha256:1111111111111111111111111111111111111111111111111111111111111111" in script, "validation-only digests must be refused"
    assert "@sha256:[0-9a-f]{64}$" in script, "every image variable must be digest-pinned"
    assert "trap cleanup EXIT" in script and "down --volumes --remove-orphans" in script, "the run must destroy its own stack"
    assert "/Users/" not in script and "/home/" not in script, "no host-specific path may be committed"


DIGEST_PINNED_IMAGE = "GRAPHINATOR_IMAGE=ghcr.io/groovemap-music/discogs-graph-enricher@sha256:" + "a" * 64
_RUN_COUNTER = itertools.count()


def run_env_gate(tmp_path: Path, env_body: str | None) -> subprocess.CompletedProcess[str]:
    """Run the smoke script far enough to see its .env gate decide, and no further.

    `docker` is stubbed with a binary that logs its arguments and fails, so a run that
    reaches the stack at all is distinguishable from one the gate stopped, and neither
    starts a container.
    """
    # Each call gets its own directory: a `None` body must mean the file is absent, even
    # when an earlier call in the same test already wrote one.
    tmp_path = tmp_path / f"run-{next(_RUN_COUNTER)}"
    tmp_path.mkdir()

    docker = tmp_path / "docker"
    docker_log = tmp_path / "docker.log"
    docker.write_text('#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >>"$DOCKER_LOG"\nexit 1\n')
    docker.chmod(0o755)

    env_file = tmp_path / "smoke.env"
    if env_body is not None:
        env_file.write_text(env_body)

    completed = subprocess.run(
        ["/bin/bash", str(REPO_ROOT / "scripts" / "smoke-media.sh")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=os.environ
        | {
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "DOCKER_LOG": str(docker_log),
            "SMOKE_MEDIA_ENV_FILE": str(env_file),
        },
    )
    completed.stdout = docker_log.read_text() if docker_log.exists() else ""
    return completed


def test_env_gate_refuses_an_env_that_pins_no_image_at_all(tmp_path: Path) -> None:
    # The gate is a loop over the *_IMAGE lines. With none present the loop body never
    # runs, so before this the gate passed having inspected nothing and the assertion went
    # on to prove whatever images happened to be resolvable.
    result = run_env_gate(tmp_path, "SMOKE_MEDIA_TIMEOUT=300\n# no image is pinned here\n")

    assert result.returncode == 2
    assert "declares no *_IMAGE assignment" in result.stderr
    assert result.stdout == "", "the gate must decide before any container command runs"


def test_env_gate_refuses_an_empty_and_a_missing_env_file(tmp_path: Path) -> None:
    empty = run_env_gate(tmp_path, "")
    assert empty.returncode == 2
    assert "declares no *_IMAGE assignment" in empty.stderr

    missing = run_env_gate(tmp_path, None)
    assert missing.returncode == 2
    assert "is missing" in missing.stderr


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("REPLACE_WITH_DIGEST", "placeholder"),
        ("ghcr.io/groovemap-music/discogs-graph-enricher@sha256:" + "1" * 64, "validation.env"),
        ("ghcr.io/groovemap-music/discogs-graph-enricher:v0.2.0", "manifest digest"),
    ],
)
def test_env_gate_refuses_an_image_that_is_not_an_approved_digest(tmp_path: Path, value: str, expected: str) -> None:
    result = run_env_gate(tmp_path, f"GRAPHINATOR_IMAGE={value}\n")

    assert result.returncode == 2
    assert expected in result.stderr
    assert result.stdout == "", "the gate must decide before any container command runs"


def test_env_gate_admits_a_digest_pinned_image(tmp_path: Path) -> None:
    result = run_env_gate(tmp_path, DIGEST_PINNED_IMAGE + "\n")

    # The stub `docker` fails, so the run still ends non-zero — but it ended at the stack,
    # not at the gate, which is what proves a valid .env is admitted.
    assert "smoke-media:" not in result.stderr
    assert "up -d" in result.stdout


def test_testing_guide_documents_the_media_assertion() -> None:
    guide = (REPO_ROOT / "docs" / "testing-guide.md").read_text()

    assert "`just smoke-media`" in guide
    assert "groovemap-media-smoke" in guide, "the guide must name the project the teardown removes"
    assert "config/media-smoke" in guide, "the guide must name the promoted fixtures the run publishes"


def test_product_docs_describe_media_rather_than_a_vinyl_only_feature() -> None:
    architecture = (REPO_ROOT / "docs" / "architecture.md").read_text()
    usage = (REPO_ROOT / "docs" / "usage-examples.md").read_text()

    for document in (architecture, usage):
        assert "Vinyl Archaeology" not in document, "ADR 0007 renames the time-travel endpoints to a media-neutral name"
        assert "time-travel" in document.casefold() or "time travel" in document.casefold()

    for entity in ("Medium", "MediaFamily", "IN_FAMILY", "ISSUED_ON", "media->'families'"):
        assert entity in architecture, f"architecture.md must document {entity}"
    assert "musicbrainz.releases.media" in architecture

    for query in ("media->'families' @> ", "ISSUED_ON", "MediaFamily"):
        assert query in usage, f"usage-examples.md must show a {query} query"
