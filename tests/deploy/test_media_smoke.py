"""Regression tests for gm-deployment-989.2 — the end-to-end canonical media assertion.

ADR 0007 is delivered only when a release event carrying the canonical `media` block
reaches both stores. `just smoke-media` is that proof, and it is a versioned script rather
than a manual runbook step so the claim can be re-made on demand.

The assertion itself starts containers, so it stays outside `just check` and CI. What is
checkable without containers, and pinned here:

- the release inputs are the producers' contract fixtures promoted verbatim, with their
  provenance recorded, so the smoke stack asserts against the shape the producers publish
  rather than a locally invented payload;
- the fixture-to-event translation only rewrites the identity fields the stores constrain,
  and never the media block;
- every probe reports a failure as a failure, and a failed run exits non-zero;
- the disposable overlay cannot collide with an operator's environment: its own project
  container names, its own subnet, and no published port but the loopback broker endpoint;
- the recipe exists, renders, and is documented, and neither `just check` nor CI runs it.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import uuid
from pathlib import Path
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
    "prometheus",
    "otel-collector",
    "schema-init",
    "tableinator",
    "brainztableinator",
    "graphinator",
    "brainzgraphinator",
)

# The internal images the smoke stack runs. Their platform is named per service so the
# emulation an operator may need stays off the broker and both stores.
PLATFORM_PINNED_SERVICES = ("schema-init", "tableinator", "brainztableinator", "graphinator", "brainzgraphinator")


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

    def psql(self, sql: str) -> str:
        return self._sql.get(sql, "")

    def cypher(self, query: str) -> str:
        return self._cypher.get(query, "")


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
    assert published == ["127.0.0.1:${SMOKE_MEDIA_RABBITMQ_PORT:-15673}:15672"], "only the loopback broker endpoint may be published"

    base = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    base_subnet = base["networks"]["groovemap"]["ipam"]["config"][0]["subnet"]
    overlay_subnet = overlay["networks"]["groovemap"]["ipam"]["config"][0]["subnet"]
    assert base_subnet not in overlay_subnet, "the disposable run must not ask for the base stack's subnet"

    for name in PLATFORM_PINNED_SERVICES:
        assert services[name]["platform"] == "${SMOKE_MEDIA_SERVICE_PLATFORM:-linux/amd64}"


def test_recipe_exists_renders_and_stays_out_of_the_credential_free_gate() -> None:
    justfile = (REPO_ROOT / "Justfile").read_text()
    assert "smoke-media:\n    bash scripts/smoke-media.sh" in justfile
    gate = next(line for line in justfile.splitlines() if line.startswith("check:"))
    assert gate == "check: source-check typecheck test", "the credential-free gate must not start containers"

    render = (REPO_ROOT / "scripts" / "check-compose.sh").read_text()
    assert "-f docker-compose.yml -f docker-compose.media-smoke.yml config --quiet" in render

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
