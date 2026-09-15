"""Assert the ADR 0010 erasure and export boundary on a disposable smoke stack.

The deletion claim in ADR 0010 is only true if every store can be shown empty of a real
account afterwards, and the export claim is only true if the body a user is handed
actually parses. This run makes both claims checkable: it registers an account against the
disposable stack's own API, grants both consent purposes, gives the account something to
export in all three stores, takes the export and parses it line by line, then erases the
account and reads PostgreSQL, Neo4j, and Redis directly to assert that nothing keyed to
that user or its subject survived.

Two things make the absence assertions mean something. Every probe is scoped to the id
this run created — never a bare ``count(*)`` a leftover row from another run could satisfy
in reverse — and every store is asserted to hold the data *before* the erasure, so an
absence probe cannot pass because the arrangement silently never happened.

The stack lifecycle stays in the operator-approved ``smoke-erasure.sh`` adapter; the
Compose, PostgreSQL, and Neo4j plumbing, the loopback API client, the ``Check`` record, and
the report are reused from ``smoke_media`` so both assertions report a run the same way.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

# `ApiClient` and `Response` come from `smoke_media` beside `Stack`: both smokes drive the
# same API over the same published loopback port, so the transport is shared plumbing
# rather than this run's own. Importing them by name re-exports them here, which is where a
# reader of the erasure assertion expects to find the client it uses.
import smoke_media
from smoke_media import ApiClient, Check, Response, SmokeError, exit_code, quote_literal, render, wait_for


if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


# Reserved smoke identities for the catalog neighbourhood the recommendation request
# traverses. They are deliberately outside any real catalog range, exactly as the media
# smoke's are, so a smoke run can never be mistaken for or collide with promoted data.
ARTIST_ID = "999000101"
NEIGHBOUR_ARTIST_ID = "999000102"
LABEL_ID = "999000301"
RELEASE_IDS = ("999000201", "999000202", "999000203")
COLLECTED_RELEASE_ID = RELEASE_IDS[0]
GENRE_NAME = "GrooveMapSmokeGenre"

# The account this run creates. The domain is reserved by RFC 2606 so the address can
# never be delivered to, and it is deliberately not the `invalid.groovemap` domain erasure
# rewrites an address to — the soft-erase probe distinguishes the two.
SMOKE_EMAIL_DOMAIN = "smoke.invalid"

# The erasure rewrites `users.email` to `erased+<id>@invalid.groovemap`.
ERASED_EMAIL_PREFIX = "erased+"

# Both purposes the published vocabulary carries. Granting both before anything is recorded
# is what puts a non-empty `consent_purposes` on every row the run then writes.
CONSENT_PURPOSES = ("product_analytics", "model_training")

# The export's section order, from docs/identity-and-activity.md "Export". A line outside
# this set, or a section that comes back out of order, is a failed assertion rather than
# something the parser tolerates.
EXPORT_KINDS = (
    "event",
    "impression",
    "collection_item",
    "wantlist_item",
    "owned_copy",
    "observation",
    "collection_snapshot",
    "consent_grant",
)

EXPORT_MEDIA_TYPE = "application/x-ndjson"

# The per-user Redis keys the erasure sweeps. The recommendation pattern is deliberately
# wider than the two shapes `RecommendCache.invalidate_user` knows: it is still scoped to
# this user's id, and a surviving key under some third `recommend:` shape is exactly the
# kind of residue this assertion exists to catch.
RECOMMEND_KEY_PATTERN = "recommend:*{user_id}*"
SNAPSHOT_USER_COUNT_KEY = "snapshot:usercount:{user_id}"

# Non-production development credentials from docker-compose.yml, the same ones the media
# smoke uses. The smoke stack is disposable and unpublished.
POSTGRES_USER = "groovemap"
POSTGRES_DATABASE = "groovemap"


@dataclass(frozen=True)
class ExportLine:
    """One NDJSON line of the export: a kind, and the row it carries."""

    kind: str
    record: dict[str, Any]


def expect(response: Response, status: int, what: str) -> Response:
    """Return the response, or refuse to continue when it is not the one that was needed.

    The arrangement steps use this rather than a probe: a run whose account was never
    created has not disproved anything about erasure, so it stops instead of reporting a
    tidy row of absences that were absent all along.
    """
    if response.status != status:
        raise SmokeError(f"{what} answered {response.status}, expected {status}: {response.body[:200]}")
    return response


# ---------------------------------------------------------------------------
# Store access
# ---------------------------------------------------------------------------


def redis_cli(stack: smoke_media.Stack, command: Sequence[str]) -> str:
    """Run one redis-cli command inside the stack's cache container."""
    return stack.compose_exec("redis", ["redis-cli", *command]).strip()


def redis_keys(stack: smoke_media.Stack, pattern: str) -> list[str]:
    """Return the keys matching a pattern, scanned cursor-wise rather than with KEYS."""
    return [line.strip() for line in redis_cli(stack, ["--scan", "--pattern", pattern]).splitlines() if line.strip()]


def count_sql(table: str, predicate: str) -> str:
    """Return a scoped counting query.

    Rendering every statement through these three helpers keeps the argument for inlining
    in one place rather than at two dozen call sites: the only values that reach a
    predicate are ids this run minted, and `quote_literal` has already refused to pass
    anything that is not identifier-shaped.
    """
    return f"SELECT count(*) FROM {table} WHERE {predicate}"  # noqa: S608


def value_sql(expression: str, table: str, predicate: str) -> str:
    """Return a scoped single-value query."""
    return f"SELECT {expression} FROM {table} WHERE {predicate}"  # noqa: S608


def insert_sql(table: str, columns: str, values: str) -> str:
    """Return one INSERT for a row this run reserved for itself."""
    return f"INSERT INTO {table} ({columns}) VALUES ({values})"  # noqa: S608


def sql_count(stack: smoke_media.Stack, sql: str) -> int:
    """Return a counting query's answer, treating an empty result as zero."""
    value = stack.psql(sql)
    return int(value) if value.lstrip("-").isdigit() else 0


def graph_count(stack: smoke_media.Stack, query: str) -> int:
    """Return a Cypher count, treating an empty result as zero."""
    value = stack.cypher(query)
    return int(value) if value.lstrip("-").isdigit() else 0


# ---------------------------------------------------------------------------
# Arrangement
# ---------------------------------------------------------------------------


def smoke_email() -> str:
    """Return an address for this run alone, so no run can collide with another's account."""
    return f"smoke-erasure-{secrets.token_hex(8)}@{SMOKE_EMAIL_DOMAIN}"


def smoke_password() -> str:
    """Return a fresh password for this run's throwaway account.

    Generated rather than fixed: the value is presented again to re-authenticate the
    erasure, and a committed credential would be a credential whatever the account.
    """
    return secrets.token_urlsafe(24)


def register(api: ApiClient, email: str, password: str) -> None:
    """Create the throwaway account the whole assertion is about."""
    expect(api.request("POST", "/api/auth/register", body={"email": email, "password": password}), 201, "registration")


def login(api: ApiClient, email: str, password: str) -> str:
    """Log the account in and return its bearer token."""
    response = expect(api.request("POST", "/api/auth/login", body={"email": email, "password": password}), 200, "login")
    token = response.json().get("access_token")
    if not isinstance(token, str) or not token:
        raise SmokeError("login returned no access token, so nothing downstream can be authenticated")
    return token


def user_identity(api: ApiClient, token: str) -> str:
    """Return the account's own id, read back from the API rather than inferred.

    Every store probe downstream is scoped by this id, and taking it from the service that
    minted it means the probes name the row the service believes it wrote.
    """
    response = expect(api.request("GET", "/api/auth/me", token=token), 200, "the current-user lookup")
    user_id = response.json().get("id")
    if not isinstance(user_id, str):
        raise SmokeError("the current-user lookup returned no id, so no probe could be scoped to the account")
    return str(uuid.UUID(user_id))


def grant_consent(api: ApiClient, token: str) -> None:
    """Grant both published purposes, so every row this run writes carries them."""
    for purpose in CONSENT_PURPOSES:
        expect(
            api.request("PUT", f"/api/user/consent/{purpose}", body={"granted": True}, token=token),
            200,
            f"the {purpose} consent grant",
        )


def catalog_graph_statements() -> tuple[str, ...]:
    """Return the reserved catalog neighbourhood the recommendation request traverses.

    Small and entirely this run's own: one artist with three releases on one label in one
    genre, and a second artist sharing the first release. Two hops from the first artist
    therefore discover the second artist and the label, which is what gives the
    recommendation a candidate to record an impression against.
    """
    statements = [
        f"MERGE (a:Artist {{id: '{ARTIST_ID}'}}) SET a.name = 'GrooveMap Smoke Artist'",
        f"MERGE (a:Artist {{id: '{NEIGHBOUR_ARTIST_ID}'}}) SET a.name = 'GrooveMap Smoke Neighbour'",
        f"MERGE (l:Label {{id: '{LABEL_ID}'}}) SET l.name = 'GrooveMap Smoke Label'",
        f"MERGE (g:Genre {{name: '{GENRE_NAME}'}})",
    ]
    for release_id in RELEASE_IDS:
        statements += [
            f"MERGE (r:Release {{id: '{release_id}'}}) SET r.title = 'GrooveMap Smoke Release {release_id}'",
            f"MATCH (r:Release {{id: '{release_id}'}}), (a:Artist {{id: '{ARTIST_ID}'}}) MERGE (r)-[:BY]->(a)",
            f"MATCH (r:Release {{id: '{release_id}'}}), (l:Label {{id: '{LABEL_ID}'}}) MERGE (r)-[:ON]->(l)",
            f"MATCH (r:Release {{id: '{release_id}'}}), (g:Genre {{name: '{GENRE_NAME}'}}) MERGE (r)-[:IS]->(g)",
        ]
    statements.append(f"MATCH (r:Release {{id: '{COLLECTED_RELEASE_ID}'}}), (a:Artist {{id: '{NEIGHBOUR_ARTIST_ID}'}}) MERGE (r)-[:BY]->(a)")
    return tuple(statements)


def user_graph_statements(user_id: str) -> tuple[str, ...]:
    """Return the user's own subgraph — the node and edge the erasure must detach.

    Nothing in the HTTP flow this run drives creates a `:User` node; the collection sync
    does. Writing it here is what makes the Neo4j absence probe a statement about the
    erasure rather than about a node that was never there.
    """
    literal = quote_literal(user_id)
    return (
        f"MERGE (u:User {{id: {literal}}})",
        f"MATCH (u:User {{id: {literal}}}), (r:Release {{id: '{COLLECTED_RELEASE_ID}'}}) MERGE (u)-[:COLLECTED]->(r)",
    )


def user_row_statements(user_id: str) -> tuple[str, ...]:
    """Return the user-owned relational rows the erasure must delete.

    A collection row, the owned copy minted against it, and one observation about that
    copy: the three tables the acceptance names, in the dependency order the schema
    requires. Each id is fresh per run so two runs never contend for the same row.
    """
    user = quote_literal(user_id)
    item = quote_literal(str(uuid.uuid4()))
    artifact = quote_literal(str(uuid.uuid4()))
    collection_row = quote_literal(str(uuid.uuid4()))
    owned_copy = quote_literal(str(uuid.uuid4()))
    return (
        insert_sql("catalog_items", "id, kind", f"{item}, 'release'"),
        insert_sql("artifacts", "id, item_id", f"{artifact}, {item}"),
        insert_sql(
            "user_collections",
            "id, user_id, release_id, title, artist",
            f"{collection_row}, {user}, {COLLECTED_RELEASE_ID}, 'GrooveMap Smoke Release', 'GrooveMap Smoke Artist'",
        ),
        insert_sql(
            "owned_copies", "id, user_id, artifact_id, item_id, collection_row_id", f"{owned_copy}, {user}, {artifact}, {item}, {collection_row}"
        ),
        insert_sql(
            "observations", "user_id, owned_copy_id, kind, value, source", f"{user}, {owned_copy}, 'matrix', 'GrooveMap smoke inscription', 'user'"
        ),
    )


def alias_statements() -> tuple[str, ...]:
    """Return the native-identity aliases the recommendation's candidates need.

    ADR 0010 keys an impression on the ADR 0009 native id, so a candidate the alias table
    does not carry is counted and skipped rather than recorded. Without these two rows the
    recommendation request would serve a list and write no impression at all.
    """
    statements = []
    for kind, external_id in (("artist", NEIGHBOUR_ARTIST_ID), ("label", LABEL_ID)):
        native_id = quote_literal(str(uuid.uuid4()))
        alias = insert_sql("provider_aliases", "provider, entity_kind, external_id, native_id", f"'discogs', '{kind}', '{external_id}', {native_id}")
        statements += [
            insert_sql("catalog_items", "id, kind", f"{native_id}, '{kind}'"),
            f"{alias} ON CONFLICT DO NOTHING",
        ]
    return tuple(statements)


def seed_cache(stack: smoke_media.Stack, user_id: str) -> None:
    """Write the per-user snapshot counter the erasure must delete.

    The collection sync writes this key; nothing in the HTTP flow here does, so the run
    writes it for the same reason it writes the `:User` node.
    """
    redis_cli(stack, ["SET", SNAPSHOT_USER_COUNT_KEY.format(user_id=user_id), "1"])


def arrange_stores(stack: smoke_media.Stack, user_id: str) -> None:
    """Put the account's data into all three stores, ahead of the export and the erasure."""
    for statement in (*catalog_graph_statements(), *user_graph_statements(user_id)):
        stack.cypher(statement)
    for statement in (*alias_statements(), *user_row_statements(user_id)):
        stack.psql(statement)
    seed_cache(stack, user_id)


def subject_of(stack: smoke_media.Stack, user_id: str) -> str:
    """Return the pseudonymous subject the activity rows are keyed to.

    Read before the erasure, because the erasure deletes the row that maps the two: after
    it runs there is no way left to ask which subject the account had, which is the whole
    point of deleting it.
    """
    subject = stack.psql(value_sql("subject_id", "activity.user_subjects", f"user_id = {quote_literal(user_id)}"))
    if not subject:
        raise SmokeError("the account has no activity subject, so no activity was ever recorded against it")
    return str(uuid.UUID(subject))


def record_activity(api: ApiClient, token: str) -> None:
    """Make one search and one recommendation request, so events and impressions exist.

    The search records `search.query` against the account. The personalized traversal is
    the recommendation surface whose cached body is keyed by the user, so it both records
    impressions and leaves the `recommend:explore:<user>:…` key the erasure must sweep.
    """
    expect(api.request("GET", "/api/search?q=groovemap%20smoke", token=token), 200, "the recorded search")
    expect(
        api.request("GET", f"/api/recommend/explore/artist/{ARTIST_ID}?hops=2&limit=10", token=token),
        200,
        "the recorded recommendation",
    )


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------


def _sql_rows(stack: smoke_media.Stack, name: str, table: str, predicate: str, expected: int, description: str) -> Callable[[], Check]:
    """Return a probe asserting a scoped row count is exactly what it must be."""

    def probe() -> Check:
        count = sql_count(stack, count_sql(table, predicate))
        return Check(name, count == expected, f"{description} -> {count}, expected {expected}")

    return probe


def _sql_some(stack: smoke_media.Stack, name: str, table: str, predicate: str, description: str) -> Callable[[], Check]:
    """Return a probe asserting a scoped row count is greater than zero."""

    def probe() -> Check:
        count = sql_count(stack, count_sql(table, predicate))
        return Check(name, count > 0, f"{description} -> {count}, expected at least 1")

    return probe


def _sql_value(
    stack: smoke_media.Stack, name: str, expression: str, table: str, predicate: str, expected: str, description: str
) -> Callable[[], Check]:
    """Return a probe asserting a scoped scalar is exactly what it must be."""

    def probe() -> Check:
        value = stack.psql(value_sql(expression, table, predicate))
        rendered = value if value else "nothing"
        return Check(name, value == expected, f"{description} -> {rendered}, expected {expected}")

    return probe


def _graph_nodes(stack: smoke_media.Stack, name: str, query: str, expected: int, description: str) -> Callable[[], Check]:
    """Return a probe asserting a scoped Cypher count is exactly what it must be."""

    def probe() -> Check:
        count = graph_count(stack, query)
        return Check(name, count == expected, f"{description} -> {count}, expected {expected}")

    return probe


def _redis_matches(stack: smoke_media.Stack, name: str, pattern: str, expected: str) -> Callable[[], Check]:
    """Return a probe asserting how many keys scoped to the user a pattern still matches."""

    def probe() -> Check:
        keys = redis_keys(stack, pattern)
        passed = len(keys) == 0 if expected == "none" else len(keys) > 0
        return Check(name, passed, f"keys matching {pattern} -> {len(keys)}, expected {expected}")

    return probe


def _redis_key(stack: smoke_media.Stack, name: str, key: str, expected: int) -> Callable[[], Check]:
    """Return a probe asserting whether one user-scoped key exists."""

    def probe() -> Check:
        value = redis_cli(stack, ["EXISTS", key])
        exists = int(value) if value.isdigit() else 0
        return Check(name, exists == expected, f"EXISTS {key} -> {exists}, expected {expected}")

    return probe


def arranged_probes(stack: smoke_media.Stack, user_id: str, subject_id: str) -> list[Callable[[], Check]]:
    """Return the assertions that the account actually has data in all three stores.

    These run before the export and the erasure and are not a formality. An absence probe
    against a store that never held the row passes for the wrong reason, so every absence
    asserted below is paired with a presence asserted here.
    """
    user = quote_literal(user_id)
    subject = quote_literal(subject_id)
    by_subject = f"subject_id = {subject}"
    by_user = f"user_id = {user}"
    return [
        _sql_some(stack, "postgres subject has events", "activity.events", by_subject, f"events for subject {subject_id}"),
        _sql_some(stack, "postgres subject has impressions", "activity.impressions", by_subject, f"impressions for subject {subject_id}"),
        _sql_rows(stack, "postgres subject link present", "activity.user_subjects", by_user, 1, f"user_subjects for {user_id}"),
        _sql_rows(stack, "postgres collection row present", "user_collections", by_user, 1, f"user_collections for {user_id}"),
        _sql_rows(stack, "postgres owned copy present", "owned_copies", by_user, 1, f"owned_copies for {user_id}"),
        _sql_rows(stack, "postgres observation present", "observations", by_user, 1, f"observations for {user_id}"),
        _graph_nodes(
            stack, "neo4j User node present", f"MATCH (u:User {{id: {user}}}) RETURN count(u) AS value", 1, f"count(:User {{id: {user_id}}})"
        ),
        _redis_matches(stack, "redis recommendation keys present", RECOMMEND_KEY_PATTERN.format(user_id=user_id), "at least 1"),
        _redis_key(stack, "redis snapshot count present", SNAPSHOT_USER_COUNT_KEY.format(user_id=user_id), 1),
    ]


def absence_probes(stack: smoke_media.Stack, user_id: str, subject_id: str) -> list[Callable[[], Check]]:
    """Return every assertion the erasure must satisfy across all three stores.

    Each one names this run's own user id or subject id. An unscoped `count(*) = 0` would
    be a statement about the whole store rather than about this account, and would fail for
    any unrelated row a reused volume happened to hold.
    """
    user = quote_literal(user_id)
    subject = quote_literal(subject_id)
    by_subject = f"subject_id = {subject}"
    by_user = f"user_id = {user}"
    return [
        _sql_rows(stack, "postgres events erased", "activity.events", by_subject, 0, f"events for subject {subject_id}"),
        _sql_rows(stack, "postgres impressions erased", "activity.impressions", by_subject, 0, f"impressions for subject {subject_id}"),
        _sql_rows(stack, "postgres subject link erased", "activity.user_subjects", by_user, 0, f"user_subjects for {user_id}"),
        _sql_rows(stack, "postgres erasure recorded", "activity.erasures", by_subject, 1, f"erasures for subject {subject_id}"),
        _sql_rows(stack, "postgres collection rows erased", "user_collections", by_user, 0, f"user_collections for {user_id}"),
        _sql_rows(stack, "postgres owned copies erased", "owned_copies", by_user, 0, f"owned_copies for {user_id}"),
        _sql_rows(stack, "postgres observations erased", "observations", by_user, 0, f"observations for {user_id}"),
        _sql_value(
            stack,
            "postgres users row soft-erased",
            f"left(email, {len(ERASED_EMAIL_PREFIX)})",
            "users",
            f"id = {user}",
            ERASED_EMAIL_PREFIX,
            f"users.email prefix for {user_id}",
        ),
        _sql_value(stack, "postgres users row deactivated", "is_active", "users", f"id = {user}", "f", f"users.is_active for {user_id}"),
        _graph_nodes(
            stack, "neo4j User node erased", f"MATCH (u:User {{id: {user}}}) RETURN count(u) AS value", 0, f"count(:User {{id: {user_id}}})"
        ),
        _redis_matches(stack, "redis recommendation keys erased", RECOMMEND_KEY_PATTERN.format(user_id=user_id), "none"),
        _redis_key(stack, "redis snapshot count erased", SNAPSHOT_USER_COUNT_KEY.format(user_id=user_id), 0),
    ]


# ---------------------------------------------------------------------------
# The export
# ---------------------------------------------------------------------------


def parse_export(body: str) -> list[ExportLine]:
    """Parse the export as NDJSON, refusing anything that is not one object per line.

    Strict on purpose. The export is the file a user is handed, and "it downloaded" is not
    the claim ADR 0010 makes about it — the claim is that every line is a JSON object
    carrying a documented kind and its record.
    """
    parsed = []
    for number, raw in enumerate(body.splitlines(), start=1):
        if not raw.strip():
            raise SmokeError(f"export line {number} is blank; NDJSON carries exactly one JSON object per line")
        try:
            line = json.loads(raw)
        except json.JSONDecodeError as error:
            raise SmokeError(f"export line {number} is not JSON: {error}") from error
        if not isinstance(line, dict):
            raise SmokeError(f"export line {number} is a {type(line).__name__}, not a JSON object")
        kind = line.get("kind")
        record = line.get("record")
        if not isinstance(kind, str) or not isinstance(record, dict):
            raise SmokeError(f'export line {number} is not shaped {{"kind": …, "record": {{…}}}}: {raw[:120]}')
        if kind not in EXPORT_KINDS:
            raise SmokeError(f"export line {number} carries kind {kind!r}, which the documented export does not name")
        parsed.append(ExportLine(kind, record))
    return parsed


def out_of_order_kind(lines: Sequence[ExportLine]) -> str | None:
    """Return the first kind that came after a later section, or None when the order holds."""
    position = {kind: index for index, kind in enumerate(EXPORT_KINDS)}
    highest = -1
    for line in lines:
        index = position[line.kind]
        if index < highest:
            return line.kind
        highest = index
    return None


def export_probes(response: Response, lines: Sequence[ExportLine]) -> list[Callable[[], Check]]:
    """Return every assertion the export body must satisfy.

    The content probes are scoped the same way the store probes are: the event and the
    collection row named here are ones this run produced, so an export that streamed
    somebody else's rows — or none — cannot satisfy them.
    """
    kinds = [line.kind for line in lines]

    def media_type() -> Check:
        return Check(
            "export is NDJSON",
            response.media_type == EXPORT_MEDIA_TYPE,
            f"Content-Type -> {response.media_type or 'nothing'}, expected {EXPORT_MEDIA_TYPE}",
        )

    def kind_order() -> Check:
        offender = out_of_order_kind(lines)
        detail = f"{len(lines)} line(s) in {', '.join(EXPORT_KINDS)} order"
        return Check(
            "export kinds are in the documented order", offender is None, detail if offender is None else f"{offender!r} came after a later section"
        )

    def has_events() -> Check:
        count = kinds.count("event")
        return Check("export carries the subject's events", count > 0, f"event line(s) -> {count}, expected at least 1")

    def has_impressions() -> Check:
        count = kinds.count("impression")
        return Check("export carries the subject's impressions", count > 0, f"impression line(s) -> {count}, expected at least 1")

    def has_search_event() -> Check:
        recorded = [line for line in lines if line.kind == "event" and line.record.get("event_type") == "search.query"]
        return Check("export carries this run's search event", bool(recorded), f"search.query event line(s) -> {len(recorded)}, expected at least 1")

    def has_collection_row() -> Check:
        owned = [line for line in lines if line.kind == "collection_item" and str(line.record.get("release_id")) == COLLECTED_RELEASE_ID]
        return Check(
            "export carries this run's collection row",
            bool(owned),
            f"collection_item line(s) for release {COLLECTED_RELEASE_ID} -> {len(owned)}, expected at least 1",
        )

    return [media_type, kind_order, has_events, has_impressions, has_search_event, has_collection_row]


def erasure_probes(response: Response) -> list[Callable[[], Check]]:
    """Return what the erasure's own answer must say before any store is read.

    ADR 0010 reports a Neo4j or Redis step that did not complete in `incomplete` rather
    than hiding it, so a non-empty list is a failed assertion here even though the
    relational half committed.
    """

    def payload() -> dict[str, Any]:
        if response.status != 202:
            return {}
        parsed = response.json()
        return parsed if isinstance(parsed, dict) else {}

    def accepted() -> Check:
        return Check("erasure accepted", response.status == 202, f"POST /api/user/erasure -> {response.status}, expected 202")

    def complete() -> Check:
        incomplete = payload().get("incomplete")
        return Check("erasure completed every store step", incomplete == [], f"incomplete -> {json.dumps(incomplete)}, expected []")

    def counted() -> Check:
        deleted = payload().get("events_deleted")
        counted_ok = isinstance(deleted, int) and deleted > 0
        return Check("erasure counted the subject's events", counted_ok, f"events_deleted -> {json.dumps(deleted)}, expected at least 1")

    return [accepted, complete, counted]


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def run_smoke(stack: smoke_media.Stack, api: ApiClient, timeout: float = 300.0) -> list[Check]:
    """Arrange an account with data in every store, export it, erase it, and assert absence."""
    email = smoke_email()
    password = smoke_password()

    register(api, email, password)
    token = login(api, email, password)
    user_id = user_identity(api, token)
    print(f"arranging the disposable account {user_id}")
    grant_consent(api, token)
    arrange_stores(stack, user_id)
    record_activity(api, token)
    subject_id = subject_of(stack, user_id)

    arranged = wait_for(arranged_probes(stack, user_id, subject_id), time.monotonic() + timeout)
    if not all(check.passed for check in arranged):
        # Stop here deliberately. Absence proves nothing about an erasure when the store
        # never held the row, so a failed arrangement is reported as itself.
        return arranged

    print(f"exporting and then erasing subject {subject_id}")
    export = expect(api.request("GET", "/api/user/export", token=token), 200, "the export")
    lines = parse_export(export.body)
    export_results = [probe() for probe in export_probes(export, lines)]

    erasure = api.request("POST", "/api/user/erasure", body={"password": password}, token=token)
    erasure_results = [probe() for probe in erasure_probes(erasure)]
    if erasure.status != 202:
        return arranged + export_results + erasure_results

    absence = [probe() for probe in absence_probes(stack, user_id, subject_id)]
    return arranged + export_results + erasure_results + absence


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the arguments `scripts/smoke-erasure.sh` passes in."""
    parser = argparse.ArgumentParser(description="Assert the ADR 0010 erasure and export boundary end to end.")
    parser.add_argument("--project", required=True, help="Compose project name of the disposable smoke stack")
    parser.add_argument("--compose-file", action="append", required=True, dest="compose_files", help="Compose file, repeatable and order-significant")
    parser.add_argument("--api-port", type=int, required=True, help="Published loopback port of the catalog API")
    parser.add_argument("--env-file", help="Environment file used to resolve digest-pinned Compose images")
    parser.add_argument("--timeout", type=float, default=300.0, help="Seconds to wait for each stage before failing")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the whole assertion against the disposable stack and report every check."""
    args = parse_args(argv)
    # The erasure stack runs no broker: the run drives the API rather than publishing
    # catalog events, so the Stack's management endpoint is never addressed.
    stack = smoke_media.Stack(args.project, args.compose_files, env_file=args.env_file)
    api = ApiClient(f"http://127.0.0.1:{args.api_port}", timeout=min(args.timeout, 120.0))

    results = run_smoke(stack, api, timeout=args.timeout)
    print(render(results, "erasure"))
    return exit_code(results)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SmokeError as error:
        print(f"smoke-erasure: {error}", file=sys.stderr)
        sys.exit(1)
