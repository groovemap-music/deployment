"""Regression tests for gm-deployment-ap8.2 — the erasure and export assertion.

ADR 0010 makes two claims deployment cannot otherwise check: that an erasure leaves
nothing keyed to the account in any store, and that the export a user is handed is
well-formed NDJSON in a documented order. `just smoke-erasure` is that proof, and it is a
versioned script rather than a runbook step so both claims can be re-made on demand.

The assertion itself starts containers, so it stays outside `just check` and CI. What is
checkable without containers, and pinned here:

- the probe list, in full — a deletion assertion is only as good as the set of places it
  looked, so the set is a fixture rather than an implementation detail;
- every probe naming this run's own user id or subject id, because an unscoped `count(*)
  = 0` is a statement about the store rather than about the erased account, and an
  unscoped presence count is satisfied by any row a reused volume held;
- every absence being paired with a presence asserted before the erasure, so an absence
  cannot pass because the arrangement silently never happened;
- the exit code: a failed probe, and a run that asserted nothing, both exit non-zero;
- the NDJSON parse — one JSON object per line, documented kinds, documented order, at
  least one event and one impression line;
- the disposable overlay's isolation from an operator's environment;
- the recipe existing and rendering, and neither `just check` nor CI running it.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.deploy.test_media_smoke import ComposeLoader


REPO_ROOT = Path(__file__).resolve().parents[2]
OVERLAY = REPO_ROOT / "docker-compose.erasure-smoke.yml"

# Services the assertion starts, directly or through a declared dependency. Each must run
# under a Compose-generated name so a smoke run cannot collide with a live environment.
SMOKE_SERVICES = (
    "postgres",
    "neo4j",
    "redis",
    "victoria-metrics",
    "victoria-traces",
    "otel-collector",
    "schema-init",
    "api",
)

PLATFORM_PINNED_SERVICES = ("schema-init", "api")

USER_ID = "3f1d2c4a-0000-4000-8000-00000000abcd"
SUBJECT_ID = "9a8b7c6d-0000-4000-8000-0000000012ef"


def _load_module() -> Any:
    """Import the asserter by path — `scripts/` is a script directory, not a package.

    Its own `import smoke_media` resolves the same way it does at runtime, where the
    interpreter puts the script's directory first on the path.
    """
    scripts = str(REPO_ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    spec = importlib.util.spec_from_file_location("smoke_erasure", REPO_ROOT / "scripts" / "smoke_erasure.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before execution so the module's dataclasses can resolve their own module.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


smoke_erasure = _load_module()


class FakeStack:
    """A stand-in for the live stack that replays canned store answers and records asks."""

    def __init__(
        self,
        sql: dict[str, str] | None = None,
        cypher: dict[str, str] | None = None,
        redis: dict[str, str] | None = None,
    ) -> None:
        self._sql = sql or {}
        self._cypher = cypher or {}
        self._redis = redis or {}
        self.sql_queries: list[str] = []
        self.cypher_queries: list[str] = []
        self.redis_commands: list[list[str]] = []

    def psql(self, sql: str) -> str:
        self.sql_queries.append(sql)
        return self._sql.get(sql, "")

    def cypher(self, query: str) -> str:
        self.cypher_queries.append(query)
        return self._cypher.get(query, "")

    def compose_exec(self, service: str, command: list[str], timeout: float = 120.0) -> str:  # noqa: ARG002
        assert service == "redis", f"only the cache is reached with compose exec, not {service}"
        assert command[0] == "redis-cli"
        self.redis_commands.append(list(command[1:]))
        return self._redis.get(" ".join(command[1:]), "")

    def becomes(self, sql: dict[str, str], cypher: dict[str, str], redis: dict[str, str]) -> None:
        """Replay a different set of answers from here on, as an erasure makes a store do."""
        self._sql, self._cypher, self._redis = sql, cypher, redis


def counted(table: str, predicate: str) -> str:
    """Return the counting query a probe over this table and scope will ask for.

    The fixtures are keyed by the statement the asserter renders, so they go through the
    same renderer rather than repeating its SQL — what the tests below pin is the scope and
    the table each probe asks about, which they read back off the recorded queries.
    """
    return str(smoke_erasure.count_sql(table, predicate))


def valued(expression: str, table: str, predicate: str) -> str:
    """Return the single-value query a probe over this column and scope will ask for."""
    return str(smoke_erasure.value_sql(expression, table, predicate))


def arranged_answers(user_id: str = USER_ID, subject_id: str = SUBJECT_ID) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Return the store answers a stack holding this account's data would give."""
    by_subject = f"subject_id = '{subject_id}'"
    by_user = f"user_id = '{user_id}'"
    sql = {
        counted("activity.events", by_subject): "4",
        counted("activity.impressions", by_subject): "2",
        counted("activity.user_subjects", by_user): "1",
        counted("user_collections", by_user): "1",
        counted("owned_copies", by_user): "1",
        counted("observations", by_user): "1",
        valued("subject_id", "activity.user_subjects", by_user): subject_id,
    }
    cypher = {f"MATCH (u:User {{id: '{user_id}'}}) RETURN count(u) AS value": "1"}
    redis = {
        f"--scan --pattern recommend:*{user_id}*": f"recommend:explore:{user_id}:artist:999000101:2\n",
        f"EXISTS snapshot:usercount:{user_id}": "1",
    }
    return sql, cypher, redis


def erased_answers(user_id: str = USER_ID, subject_id: str = SUBJECT_ID) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Return the store answers a stack whose erasure actually ran would give."""
    by_subject = f"subject_id = '{subject_id}'"
    by_user = f"user_id = '{user_id}'"
    by_id = f"id = '{user_id}'"
    sql = {
        counted("activity.events", by_subject): "0",
        counted("activity.impressions", by_subject): "0",
        counted("activity.user_subjects", by_user): "0",
        counted("activity.erasures", by_subject): "1",
        counted("user_collections", by_user): "0",
        counted("owned_copies", by_user): "0",
        counted("observations", by_user): "0",
        valued("left(email, 7)", "users", by_id): "erased+",
        valued("is_active", "users", by_id): "f",
    }
    cypher = {f"MATCH (u:User {{id: '{user_id}'}}) RETURN count(u) AS value": "0"}
    redis = {f"EXISTS snapshot:usercount:{user_id}": "0"}
    return sql, cypher, redis


def export_body(kinds: list[str] | None = None) -> str:
    """Return an export body carrying one line per named kind, in the order given."""
    records = {
        "event": {"event_id": str(uuid.uuid4()), "event_type": "search.query"},
        "impression": {"impression_id": str(uuid.uuid4()), "surface": "recommendation"},
        "collection_item": {"id": str(uuid.uuid4()), "release_id": int(smoke_erasure.COLLECTED_RELEASE_ID)},
        "consent_grant": {"id": str(uuid.uuid4()), "purpose": "product_analytics"},
    }
    ordered = kinds if kinds is not None else ["event", "event", "impression", "collection_item", "consent_grant"]
    return "".join(json.dumps({"kind": kind, "record": records.get(kind, {"id": "1"})}) + "\n" for kind in ordered)


def export_response(body: str | None = None, media_type: str = smoke_erasure.EXPORT_MEDIA_TYPE) -> Any:
    """Return the export response a stack streaming this account's rows would send."""
    return smoke_erasure.Response(200, export_body() if body is None else body, media_type)


def erasure_response(status: int = 202, payload: dict[str, Any] | None = None) -> Any:
    """Return the answer the erasure endpoint gives for a run where every step completed."""
    body = {"erasure_id": str(uuid.uuid4()), "events_deleted": 4, "impressions_deleted": 2, "incomplete": []}
    return smoke_erasure.Response(status, json.dumps(payload if payload is not None else body), "application/json")


def names(probes: list[Any]) -> list[str]:
    """Return the name of every probe in a list, by evaluating it against its stack."""
    return [probe().name for probe in probes]


# ---------------------------------------------------------------------------
# The probe list
# ---------------------------------------------------------------------------


def test_the_absence_probe_list_covers_every_store_the_erasure_touches() -> None:
    probes = smoke_erasure.absence_probes(FakeStack(), USER_ID, SUBJECT_ID)

    assert names(probes) == [
        "postgres events erased",
        "postgres impressions erased",
        "postgres subject link erased",
        "postgres erasure recorded",
        "postgres collection rows erased",
        "postgres owned copies erased",
        "postgres observations erased",
        "postgres users row soft-erased",
        "postgres users row deactivated",
        "neo4j User node erased",
        "redis recommendation keys erased",
        "redis snapshot count erased",
    ]


def test_every_absence_is_paired_with_a_presence_asserted_before_the_erasure() -> None:
    # An absence probe against a store that never held the row passes for the wrong
    # reason. Every store and table the erasure is asserted to have emptied must therefore
    # also be asserted full while the account still exists.
    arranged = names(smoke_erasure.arranged_probes(FakeStack(), USER_ID, SUBJECT_ID))

    assert arranged == [
        "postgres subject has events",
        "postgres subject has impressions",
        "postgres subject link present",
        "postgres collection row present",
        "postgres owned copy present",
        "postgres observation present",
        "neo4j User node present",
        "redis recommendation keys present",
        "redis snapshot count present",
    ]

    # The erasure record is the one absence probe with no pair: it asserts a row appearing
    # rather than disappearing, and it cannot exist before the erasure runs.
    subjects = {
        "events",
        "impressions",
        "subject link",
        "collection",
        "owned cop",
        "observation",
        "neo4j User node",
        "redis recommendation keys",
        "redis snapshot count",
    }
    for subject in subjects:
        assert any(subject in name for name in arranged), f"nothing is asserted present for {subject}"


def test_every_store_probe_is_scoped_to_the_account_this_run_created() -> None:
    # An unscoped `count(*) = 0` is a statement about the whole store: it fails for any
    # unrelated row a reused volume holds, and it would pass against an empty store
    # whatever this run did. Every probe must name this run's own ids.
    for factory in (smoke_erasure.arranged_probes, smoke_erasure.absence_probes):
        stack = FakeStack()
        for probe in factory(stack, USER_ID, SUBJECT_ID):
            probe()

        for query in stack.sql_queries:
            assert "WHERE" in query, f"an unscoped store query: {query}"
            assert USER_ID in query or SUBJECT_ID in query, f"a store probe named neither the user nor the subject: {query}"
        for query in stack.cypher_queries:
            assert "MATCH (u:User) " not in query, f"an unscoped User count: {query}"
            assert USER_ID in query, f"a graph probe named no id from this run: {query}"
        for command in stack.redis_commands:
            assert any(USER_ID in argument for argument in command), f"a cache probe named no id from this run: {command}"


def test_the_activity_probes_are_keyed_to_the_subject_and_the_rest_to_the_user() -> None:
    # ADR 0010 keys the behavioural tables to the pseudonymous subject and everything else
    # to the account, so a probe keyed to the wrong one would be asking an unrelated
    # question of the right table.
    stack = FakeStack()
    for probe in smoke_erasure.absence_probes(stack, USER_ID, SUBJECT_ID):
        probe()

    by_table = {query.split(" FROM ")[1].split(" WHERE ")[0]: query for query in stack.sql_queries if " FROM " in query}
    for table in ("activity.events", "activity.impressions", "activity.erasures"):
        assert f"subject_id = '{SUBJECT_ID}'" in by_table[table]
    for table in ("activity.user_subjects", "user_collections", "owned_copies", "observations", "users"):
        assert f"'{USER_ID}'" in by_table[table] and SUBJECT_ID not in by_table[table]


# ---------------------------------------------------------------------------
# Probe behaviour
# ---------------------------------------------------------------------------


def test_every_absence_probe_passes_only_when_the_erasure_actually_ran() -> None:
    sql, cypher, redis = erased_answers()
    results = [probe() for probe in smoke_erasure.absence_probes(FakeStack(sql, cypher, redis), USER_ID, SUBJECT_ID)]

    assert smoke_erasure.exit_code(results) == 0, smoke_erasure.render(results, "erasure")


def test_a_store_that_still_holds_the_account_fails_the_run() -> None:
    sql, cypher, redis = erased_answers()
    survivors = {
        counted("activity.events", f"subject_id = '{SUBJECT_ID}'"): "4",
        counted("observations", f"user_id = '{USER_ID}'"): "1",
    }
    stack = FakeStack(sql | survivors, cypher | {f"MATCH (u:User {{id: '{USER_ID}'}}) RETURN count(u) AS value": "1"}, redis)
    results = [probe() for probe in smoke_erasure.absence_probes(stack, USER_ID, SUBJECT_ID)]

    assert smoke_erasure.exit_code(results) == 1
    failed = {result.name for result in results if not result.passed}
    assert failed == {"postgres events erased", "postgres observations erased", "neo4j User node erased"}


def test_a_surviving_cache_key_for_the_user_fails_the_run() -> None:
    sql, cypher, _ = erased_answers()
    # A 28-day recommendation cache outliving an erasure means the system still holds
    # something keyed to the user, which ADR 0010 deletes rather than lets expire.
    leftovers = {
        f"--scan --pattern recommend:*{USER_ID}*": f"recommend:enhanced:{USER_ID}\n",
        f"EXISTS snapshot:usercount:{USER_ID}": "1",
    }
    results = [probe() for probe in smoke_erasure.absence_probes(FakeStack(sql, cypher, leftovers), USER_ID, SUBJECT_ID)]

    failed = {result.name for result in results if not result.passed}
    assert failed == {"redis recommendation keys erased", "redis snapshot count erased"}


def test_a_missing_erasure_record_fails_even_though_every_row_is_gone() -> None:
    sql, cypher, redis = erased_answers()
    # The `activity.erasures` row is the durable record that the erasure happened; the
    # event that requested it is deleted along with everything else.
    del sql[counted("activity.erasures", f"subject_id = '{SUBJECT_ID}'")]
    results = [probe() for probe in smoke_erasure.absence_probes(FakeStack(sql, cypher, redis), USER_ID, SUBJECT_ID)]

    assert smoke_erasure.exit_code(results) == 1
    assert [result.name for result in results if not result.passed] == ["postgres erasure recorded"]


def test_a_hard_deleted_or_still_active_users_row_is_a_failure() -> None:
    sql, cypher, redis = erased_answers()
    # The users row is soft-erased in place: two foreign keys to it carry no cascade rule.
    # Its disappearance is as much a failure as its survival unchanged.
    del sql[valued("left(email, 7)", "users", f"id = '{USER_ID}'")]
    still_active = sql | {valued("is_active", "users", f"id = '{USER_ID}'"): "t"}
    results = [probe() for probe in smoke_erasure.absence_probes(FakeStack(still_active, cypher, redis), USER_ID, SUBJECT_ID)]

    failed = {result.name: result for result in results if not result.passed}
    assert set(failed) == {"postgres users row soft-erased", "postgres users row deactivated"}
    assert "nothing" in failed["postgres users row soft-erased"].detail


def test_an_arrangement_that_never_landed_fails_before_anything_is_erased() -> None:
    results = [probe() for probe in smoke_erasure.arranged_probes(FakeStack(), USER_ID, SUBJECT_ID)]

    assert smoke_erasure.exit_code(results) == 1
    assert not any(result.passed for result in results)


def test_every_arranged_probe_passes_once_the_account_has_data_everywhere() -> None:
    results = [probe() for probe in smoke_erasure.arranged_probes(FakeStack(*arranged_answers()), USER_ID, SUBJECT_ID)]

    assert smoke_erasure.exit_code(results) == 0, smoke_erasure.render(results, "erasure")


def test_the_report_names_the_erasure_run_and_exits_zero_only_when_every_check_holds() -> None:
    passing = [smoke_erasure.Check("postgres events erased", True, "events for subject -> 0")]
    failing = [*passing, smoke_erasure.Check("neo4j User node erased", False, "count(:User) -> 1")]

    assert smoke_erasure.exit_code(passing) == 0
    assert smoke_erasure.exit_code(failing) == 1
    assert smoke_erasure.exit_code([]) == 1, "a run that asserted nothing has proved nothing"

    report = smoke_erasure.render(failing, "erasure")
    assert "PASS  postgres events erased" in report
    assert "FAIL  neo4j User node erased" in report
    assert "1/2 erasure assertions passed" in report


# ---------------------------------------------------------------------------
# The export
# ---------------------------------------------------------------------------


def test_the_export_is_parsed_one_json_object_per_line() -> None:
    lines = smoke_erasure.parse_export(export_body())

    assert [line.kind for line in lines] == ["event", "event", "impression", "collection_item", "consent_grant"]
    assert lines[0].record["event_type"] == "search.query"
    assert smoke_erasure.parse_export("") == [], "an empty body parses to no lines rather than raising"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ('{"kind": "event", "record": {}}\n\n', "blank"),
        ('{"kind": "event", "record": {}\n', "not JSON"),
        ("[1, 2, 3]\n", "not a JSON object"),
        ('{"kind": "event"}\n', "is not shaped"),
        ('{"kind": "event", "record": []}\n', "is not shaped"),
        ('{"kind": "playlist", "record": {}}\n', "does not name"),
    ],
)
def test_an_export_that_is_not_well_formed_ndjson_is_refused(body: str, expected: str) -> None:
    # "It downloaded" is not the claim ADR 0010 makes about the export. Anything that is
    # not one documented object per line stops the run rather than being tolerated.
    with pytest.raises(smoke_erasure.SmokeError, match=expected):
        smoke_erasure.parse_export(body)


def test_the_documented_kind_order_is_the_one_the_export_endpoint_streams() -> None:
    # docs/identity-and-activity.md "Export" names these eight sections in this order, and
    # two exports of unchanged data are only byte-identical because the order is stable.
    assert smoke_erasure.EXPORT_KINDS == (
        "event",
        "impression",
        "collection_item",
        "wantlist_item",
        "owned_copy",
        "observation",
        "collection_snapshot",
        "consent_grant",
    )


def test_a_section_that_comes_back_out_of_order_fails_the_export_probe() -> None:
    shuffled = export_body(["impression", "event"])
    lines = smoke_erasure.parse_export(shuffled)

    assert smoke_erasure.out_of_order_kind(lines) == "event"
    results = [probe() for probe in smoke_erasure.export_probes(export_response(shuffled), lines)]
    by_name = {result.name: result for result in results}

    assert not by_name["export kinds are in the documented order"].passed
    assert "'event'" in by_name["export kinds are in the documented order"].detail
    # Repeats within one section are the normal case and are not an ordering failure.
    assert smoke_erasure.out_of_order_kind(smoke_erasure.parse_export(export_body())) is None


def test_the_export_probes_require_an_event_an_impression_and_this_run_s_own_rows() -> None:
    response = export_response()
    results = [probe() for probe in smoke_erasure.export_probes(response, smoke_erasure.parse_export(response.body))]

    assert names(smoke_erasure.export_probes(response, [])) == [
        "export is NDJSON",
        "export kinds are in the documented order",
        "export carries the subject's events",
        "export carries the subject's impressions",
        "export carries this run's search event",
        "export carries this run's collection row",
    ]
    assert smoke_erasure.exit_code(results) == 0, smoke_erasure.render(results, "export")


@pytest.mark.parametrize(
    ("kinds", "failed"),
    [
        (["impression", "collection_item"], {"export carries the subject's events", "export carries this run's search event"}),
        (["event", "collection_item"], {"export carries the subject's impressions"}),
        (["event", "impression"], {"export carries this run's collection row"}),
    ],
)
def test_an_export_missing_a_section_this_run_produced_fails(kinds: list[str], failed: set[str]) -> None:
    body = export_body(kinds)
    results = [probe() for probe in smoke_erasure.export_probes(export_response(body), smoke_erasure.parse_export(body))]

    assert {result.name for result in results if not result.passed} == failed


def test_an_export_served_as_something_other_than_ndjson_fails() -> None:
    body = export_body()
    wrong = export_response(body, media_type="application/json")
    results = [probe() for probe in smoke_erasure.export_probes(wrong, smoke_erasure.parse_export(body))]

    by_name = {result.name: result for result in results}
    assert not by_name["export is NDJSON"].passed
    assert "application/x-ndjson" in by_name["export is NDJSON"].detail


# ---------------------------------------------------------------------------
# The erasure's own answer
# ---------------------------------------------------------------------------


def test_the_erasure_response_probes_read_the_partial_failure_shape() -> None:
    results = [probe() for probe in smoke_erasure.erasure_probes(erasure_response())]
    assert names(smoke_erasure.erasure_probes(erasure_response())) == [
        "erasure accepted",
        "erasure completed every store step",
        "erasure counted the subject's events",
    ]
    assert smoke_erasure.exit_code(results) == 0


def test_a_store_step_reported_incomplete_fails_the_run() -> None:
    # The relational half commits first, so ADR 0010 reports a failed Neo4j or Redis step
    # in `incomplete` rather than hiding it. A run that ignored that list would call an
    # erasure complete while a store still held the user.
    reported = erasure_response(payload={"erasure_id": "x", "events_deleted": 4, "incomplete": ["Neo4j deletion failed: ServiceUnavailable"]})
    results = [probe() for probe in smoke_erasure.erasure_probes(reported)]

    failed = {result.name: result for result in results if not result.passed}
    assert set(failed) == {"erasure completed every store step"}
    assert "ServiceUnavailable" in failed["erasure completed every store step"].detail


def test_a_refused_erasure_fails_every_answer_probe_without_parsing_a_body() -> None:
    refused = smoke_erasure.Response(401, "Incorrect password", "text/plain")
    results = [probe() for probe in smoke_erasure.erasure_probes(refused)]

    assert smoke_erasure.exit_code(results) == 1
    assert {result.name for result in results if not result.passed} == {
        "erasure accepted",
        "erasure completed every store step",
        "erasure counted the subject's events",
    }


def test_an_erasure_that_deleted_no_event_is_a_failure() -> None:
    nothing = erasure_response(payload={"erasure_id": "x", "events_deleted": 0, "incomplete": []})
    results = [probe() for probe in smoke_erasure.erasure_probes(nothing)]

    assert {result.name for result in results if not result.passed} == {"erasure counted the subject's events"}


# ---------------------------------------------------------------------------
# The whole run, offline
# ---------------------------------------------------------------------------


class FakeApi:
    """The stack's API, replaying canned answers and recording the calls the run made."""

    def __init__(self, answers: dict[tuple[str, str], Any] | None = None, on_erasure: Any = None) -> None:
        self.answers = answers or {}
        self.on_erasure = on_erasure
        self.calls: list[tuple[str, str]] = []
        self.password: str | None = None

    def request(self, method: str, path: str, *, body: Any = None, token: str | None = None) -> Any:  # noqa: ARG002
        self.calls.append((method, path))
        if path == "/api/user/erasure":
            self.password = (body or {}).get("password")
            if self.on_erasure is not None:
                self.on_erasure()
        for (want_method, want_path), response in self.answers.items():
            if want_method == method and path.startswith(want_path):
                return response
        return smoke_erasure.Response(200, "{}", "application/json")


def api_answers(export: Any = None, erasure: Any = None) -> dict[tuple[str, str], Any]:
    """Return the answers a working stack gives to every call the run makes."""
    return {
        ("POST", "/api/auth/register"): smoke_erasure.Response(201, json.dumps({"message": "Registration processed"}), "application/json"),
        ("POST", "/api/auth/login"): smoke_erasure.Response(200, json.dumps({"access_token": "smoke-token", "expires_in": 3600}), "application/json"),
        ("GET", "/api/auth/me"): smoke_erasure.Response(200, json.dumps({"id": USER_ID, "email": "smoke@smoke.invalid"}), "application/json"),
        ("GET", "/api/user/export"): export if export is not None else export_response(),
        ("POST", "/api/user/erasure"): erasure if erasure is not None else erasure_response(),
    }


def whole_run(stack: Any, api: Any) -> list[Any]:
    """Drive the whole assertion with no deadline to wait out."""
    return list(smoke_erasure.run_smoke(stack, api, timeout=0.0))


def test_the_whole_run_passes_against_a_stack_that_exported_and_then_erased() -> None:
    # Every store answers "the account is here" while the run arranges and exports, and
    # "the account is gone" from the erasure onwards — which is the only sequence of
    # answers a stack that actually erased the account could give.
    stack = FakeStack(*arranged_answers())
    api = FakeApi(api_answers(), on_erasure=lambda: stack.becomes(*erased_answers()))
    results = whole_run(stack, api)

    assert smoke_erasure.exit_code(results) == 0, smoke_erasure.render(results, "erasure")
    assert len(results) == 30, "every arranged, export, answer, and absence probe is reported"
    assert ("GET", "/api/search?q=groovemap%20smoke") in api.calls
    assert ("GET", f"/api/recommend/explore/artist/{smoke_erasure.ARTIST_ID}?hops=2&limit=10") in api.calls


def test_the_run_stops_before_erasing_when_the_arrangement_never_landed() -> None:
    # Nothing was written to any store, so every absence would pass for the wrong reason.
    stack = FakeStack({valued("subject_id", "activity.user_subjects", f"user_id = '{USER_ID}'"): SUBJECT_ID})
    api = FakeApi(api_answers())
    results = whole_run(stack, api)

    assert smoke_erasure.exit_code(results) == 1
    assert len(results) == 9, "the run reports the arrangement it could not make, and stops"
    assert ("POST", "/api/user/erasure") not in api.calls
    assert ("GET", "/api/user/export") not in api.calls


def test_the_run_stops_after_a_refused_erasure_rather_than_reporting_absences() -> None:
    api = FakeApi(api_answers(erasure=smoke_erasure.Response(401, "Incorrect password", "text/plain")))
    results = whole_run(FakeStack(*arranged_answers()), api)

    assert smoke_erasure.exit_code(results) == 1
    assert len(results) == 18, "arranged and export checks plus the three answer checks, and no absence"
    assert not any(result.name.endswith("erased") for result in results)


def test_the_erasure_is_re_authenticated_with_the_password_the_run_registered() -> None:
    # ADR 0010 re-authenticates the erasure rather than taking it on the bearer token
    # alone, so a run that sent no password would never exercise that path.
    api = FakeApi(api_answers())
    whole_run(FakeStack(*arranged_answers()), api)

    assert api.password, "the erasure must present the account's own password"
    assert len(api.password) >= 16


def test_a_run_whose_account_has_no_subject_refuses_to_continue() -> None:
    with pytest.raises(smoke_erasure.SmokeError, match="no activity subject"):
        smoke_erasure.subject_of(FakeStack(), USER_ID)


@pytest.mark.parametrize(
    ("answers", "expected"),
    [
        ({("POST", "/api/auth/register"): smoke_erasure.Response(500, "boom", "text/plain")}, "registration answered 500"),
        ({("POST", "/api/auth/login"): smoke_erasure.Response(200, "{}", "application/json")}, "no access token"),
        ({("GET", "/api/auth/me"): smoke_erasure.Response(200, "{}", "application/json")}, "no id"),
    ],
)
def test_a_stack_that_cannot_make_the_account_stops_instead_of_asserting(answers: dict[tuple[str, str], Any], expected: str) -> None:
    api = FakeApi(api_answers() | answers)

    with pytest.raises(smoke_erasure.SmokeError, match=expected):
        whole_run(FakeStack(), api)


def test_the_run_creates_its_own_catalog_neighbourhood_and_user_subgraph() -> None:
    stack = FakeStack(*arranged_answers())
    smoke_erasure.arrange_stores(stack, USER_ID)

    written = "\n".join(stack.cypher_queries)
    assert f"MERGE (u:User {{id: '{USER_ID}'}})" in written, "the node the erasure must detach has to exist first"
    assert "COLLECTED" in written, "a bare node would be detached by a plain DELETE too"
    # The candidate the recommendation records an impression against needs a native id, or
    # ADR 0010 counts it and records nothing.
    aliases = [query for query in stack.sql_queries if "provider_aliases" in query]
    assert any(f"'artist', '{smoke_erasure.NEIGHBOUR_ARTIST_ID}'" in query for query in aliases)
    assert any(f"'label', '{smoke_erasure.LABEL_ID}'" in query for query in aliases)

    for table in ("catalog_items", "artifacts", "user_collections", "owned_copies", "observations"):
        assert any(f"INSERT INTO {table} " in query for query in stack.sql_queries), f"nothing was written to {table}"
    assert stack.redis_commands[-1][:2] == ["SET", f"snapshot:usercount:{USER_ID}"]


def test_the_reserved_smoke_identities_stay_outside_any_real_catalog_range() -> None:
    # The media smoke reserves 999000001 for the same reason: a smoke run must never be
    # mistaken for, or collide with, promoted production data.
    for identity in (smoke_erasure.ARTIST_ID, smoke_erasure.NEIGHBOUR_ARTIST_ID, smoke_erasure.LABEL_ID, *smoke_erasure.RELEASE_IDS):
        assert identity.startswith("999000")
    assert smoke_erasure.SMOKE_EMAIL_DOMAIN.endswith(".invalid"), "the account's address must never be deliverable"
    assert smoke_erasure.ERASED_EMAIL_PREFIX == "erased+"


def test_the_account_credentials_are_minted_per_run_rather_than_committed() -> None:
    assert smoke_erasure.smoke_email() != smoke_erasure.smoke_email()
    assert smoke_erasure.smoke_password() != smoke_erasure.smoke_password()
    assert len(smoke_erasure.smoke_password()) >= 24


def test_both_published_consent_purposes_are_granted_before_anything_is_recorded() -> None:
    # `consent_purposes` is snapshotted onto every activity row at write time, so granting
    # after the fact would leave the exported rows saying the user had permitted nothing.
    assert smoke_erasure.CONSENT_PURPOSES == ("product_analytics", "model_training")
    api = FakeApi()
    smoke_erasure.grant_consent(api, "token")

    assert api.calls == [("PUT", "/api/user/consent/product_analytics"), ("PUT", "/api/user/consent/model_training")]


def test_a_store_literal_that_is_not_identifier_shaped_is_refused() -> None:
    assert smoke_erasure.quote_literal(USER_ID) == f"'{USER_ID}'"
    with pytest.raises(smoke_erasure.SmokeError):
        smoke_erasure.quote_literal("' OR true; DROP TABLE users; --")


def test_an_unreachable_api_is_an_error_rather_than_a_silent_pass() -> None:
    client = smoke_erasure.ApiClient("http://127.0.0.1:1")

    with pytest.raises(smoke_erasure.SmokeError, match="could not reach the stack's API"):
        client.request("GET", "/api/auth/me")


def test_a_non_json_answer_is_reported_rather_than_crashing_the_run() -> None:
    with pytest.raises(smoke_erasure.SmokeError, match="expected a JSON body"):
        smoke_erasure.Response(200, "<html>502</html>", "text/html").json()


def test_the_driver_takes_the_stack_it_is_pointed_at() -> None:
    args = smoke_erasure.parse_args(
        [
            "--project",
            "groovemap-erasure-smoke",
            "--compose-file",
            "docker-compose.yml",
            "--compose-file",
            "docker-compose.erasure-smoke.yml",
            "--api-port",
            "18004",
            "--env-file",
            ".env",
        ]
    )

    assert args.compose_files == ["docker-compose.yml", "docker-compose.erasure-smoke.yml"]
    assert args.api_port == 18004
    assert args.timeout == 300.0


# ---------------------------------------------------------------------------
# The overlay, the recipe, and the gate
# ---------------------------------------------------------------------------


def test_overlay_isolates_the_run_from_an_operator_environment() -> None:
    overlay = yaml.load(OVERLAY.read_text(), Loader=ComposeLoader)  # noqa: S506
    services = overlay["services"]

    for name in SMOKE_SERVICES:
        assert services[name]["container_name"] is None, f"{name} must run under a Compose project name, not the fixed one"

    published = [port for service in services.values() for port in service.get("ports", [])]
    assert published == ["127.0.0.1:${SMOKE_ERASURE_API_PORT:-18004}:8004"], "only the loopback API endpoint may be published"

    base = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    base_subnet = base["networks"]["groovemap"]["ipam"]["config"][0]["subnet"]
    overlay_subnet = overlay["networks"]["groovemap"]["ipam"]["config"][0]["subnet"]
    assert base_subnet not in overlay_subnet, "the disposable run must not ask for the base stack's subnet"

    for name in PLATFORM_PINNED_SERVICES:
        assert services[name]["platform"] == "${SMOKE_ERASURE_SERVICE_PLATFORM:-linux/amd64}"


def test_the_overlay_does_not_reuse_another_disposable_run_s_subnet_or_port() -> None:
    # Two smoke runs on one workstation must be able to coexist; a shared subnet or a
    # shared loopback port would make the second one fail for reasons of its own.
    subnets = set()
    for overlay_path in REPO_ROOT.glob("docker-compose.*-smoke.yml"):
        overlay = yaml.load(overlay_path.read_text(), Loader=ComposeLoader)  # noqa: S506
        subnet = overlay.get("networks", {}).get("groovemap", {}).get("ipam", {}).get("config", [{}])[0].get("subnet")
        assert subnet not in subnets, f"{overlay_path.name} reuses a subnet another disposable overlay asks for"
        subnets.add(subnet)

    media = (REPO_ROOT / "docker-compose.media-smoke.yml").read_text()
    assert "18004" not in media


def test_recipe_exists_renders_and_stays_out_of_the_credential_free_gate() -> None:
    justfile = (REPO_ROOT / "Justfile").read_text()
    assert "smoke-erasure:\n    bash scripts/smoke-erasure.sh" in justfile
    gate = next(line for line in justfile.splitlines() if line.startswith("check:"))
    assert gate == "check: source-check typecheck test", "the credential-free gate must not start containers"
    for recipe in ("source-check:", "test:", "typecheck:", "_compose-check:", "_source-analysis:"):
        body = justfile.split(recipe)[1].split("\n\n")[0]
        assert "smoke-erasure" not in body, f"{recipe} must not start containers"

    render = (REPO_ROOT / "scripts" / "check-compose.sh").read_text()
    assert "check_compose docker-compose.yml docker-compose.erasure-smoke.yml" in render

    workflows = REPO_ROOT / ".github" / "workflows"
    for workflow in workflows.glob("*.yml"):
        assert "smoke-erasure" not in workflow.read_text(), f"{workflow.name} must not start containers"
    assert list(workflows.glob("*.yml")), "the workflow directory must actually have been read"


def test_smoke_script_requires_operator_supplied_digest_pinned_images() -> None:
    script = (REPO_ROOT / "scripts" / "smoke-erasure.sh").read_text()

    assert "REPLACE_WITH" in script, "the .env.example placeholder must be refused"
    assert "@sha256:1111111111111111111111111111111111111111111111111111111111111111" in script, "validation-only digests must be refused"
    assert "@sha256:[0-9a-f]{64}$" in script, "every image variable must be digest-pinned"
    assert "trap cleanup EXIT" in script and "down --volumes --remove-orphans" in script, "the run must destroy its own stack"
    assert "/Users/" not in script and "/home/" not in script, "no host-specific path may be committed"


def run_env_gate(tmp_path: Path, env_body: str | None) -> subprocess.CompletedProcess[str]:
    """Run the smoke script far enough to see its .env gate decide, and no further.

    `docker` is stubbed with a binary that logs its arguments and fails, so a run that
    reaches the stack at all is distinguishable from one the gate stopped, and neither
    starts a container.
    """
    docker = tmp_path / "docker"
    docker_log = tmp_path / "docker.log"
    docker.write_text('#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >>"$DOCKER_LOG"\nexit 1\n')
    docker.chmod(0o755)

    env_file = tmp_path / "smoke.env"
    if env_body is not None:
        env_file.write_text(env_body)

    completed = subprocess.run(
        ["/bin/bash", str(REPO_ROOT / "scripts" / "smoke-erasure.sh")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=os.environ | {"PATH": f"{tmp_path}:{os.environ['PATH']}", "DOCKER_LOG": str(docker_log), "SMOKE_ERASURE_ENV_FILE": str(env_file)},
    )
    completed.stdout = docker_log.read_text() if docker_log.exists() else ""
    return completed


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (None, "is missing"),
        ("", "declares no *_IMAGE assignment"),
        ("SMOKE_ERASURE_TIMEOUT=300\n", "declares no *_IMAGE assignment"),
        ("CATALOG_API_IMAGE=REPLACE_WITH_DIGEST\n", "placeholder"),
        ("CATALOG_API_IMAGE=ghcr.io/groovemap-music/catalog-api@sha256:" + "1" * 64 + "\n", "validation.env"),
        ("CATALOG_API_IMAGE=ghcr.io/groovemap-music/catalog-api:v0.2.0\n", "manifest digest"),
    ],
)
def test_env_gate_refuses_anything_but_an_approved_digest(tmp_path: Path, body: str | None, expected: str) -> None:
    result = run_env_gate(tmp_path, body)

    assert result.returncode == 2
    assert expected in result.stderr
    assert result.stdout == "", "the gate must decide before any container command runs"


def test_env_gate_admits_a_digest_pinned_image(tmp_path: Path) -> None:
    result = run_env_gate(tmp_path, "CATALOG_API_IMAGE=ghcr.io/groovemap-music/catalog-api@sha256:" + "a" * 64 + "\n")

    # The stub `docker` fails, so the run still ends non-zero — but it ended at the stack,
    # not at the gate, which is what proves a valid .env is admitted.
    assert "smoke-erasure:" not in result.stderr
    assert "up -d --wait api" in result.stdout
