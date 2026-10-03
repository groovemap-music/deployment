"""Run synthetic-only assertions inside owned pinned legacy Python probe containers."""

from __future__ import annotations

import argparse
import asyncio
import errno
import importlib
import importlib.util
import json
import os
import re
import signal
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote


ENTITIES = {"discogs": ["artists", "labels", "masters", "releases"], "musicbrainz": ["artists", "labels", "release-groups", "releases"]}
DISCOGS_IDS = {"artists": "999000001", "labels": "999000002", "masters": "999000003", "releases": "999000004"}
MB_IDS = {entity: f"f0f0f0f0-0000-4000-8000-{i:012d}" for i, entity in enumerate(["artists", "labels", "release-groups", "releases"], 1)}
LABELS = {"artists": "Artist", "labels": "Label", "masters": "Master", "releases": "Release", "release-groups": "Master"}


def require(value: bool, message: str) -> None:
    if not value:
        raise ValueError(message)


def password() -> str:
    return Path("/fixtures/synthetic-password").read_text().strip()


def load_schema(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, Path("/fixtures") / f"{name}.py")
    require(spec is not None and spec.loader is not None, "missing reviewed exact schema")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def expected_ids(provider: str) -> dict[str, set[str]]:
    result = {key: {value} for key, value in (DISCOGS_IDS if provider == "discogs" else MB_IDS).items()}
    if provider == "musicbrainz":
        result["artists"].add("f0f0f0f0-0000-4000-8000-000000000005")
    return result


async def sql_connection() -> Any:
    psycopg = importlib.import_module("psycopg")
    return await psycopg.AsyncConnection.connect(host="postgres", dbname="synthetic_smoke", user="synthetic-smoke", password=password())


def graph_driver() -> Any:
    neo4j = importlib.import_module("neo4j")
    return neo4j.AsyncGraphDatabase.driver("bolt://neo4j:7687", auth=("neo4j", password()))


async def schema_sql() -> None:
    common = importlib.import_module("common")
    pool = common.AsyncPostgreSQLPool(
        connection_params={"host": "postgres", "port": 5432, "dbname": "synthetic_smoke", "user": "synthetic-smoke", "password": password()},
        max_connections=2,
        min_connections=1,
        max_retries=2,
        health_check_interval=30,
    )
    await pool.initialize()
    try:
        require(await load_schema("postgres_schema").create_postgres_schema(pool) == 0, "partial SQL schema initialization")
    finally:
        await pool.close()


def connection_refused(error: BaseException) -> bool:
    """Recognize the pinned driver's preserved socket refusal cause only."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, OSError) and current.errno == errno.ECONNREFUSED:
            return True
        current = current.__cause__
    return False


async def await_graph_ready(driver: Any, unavailable: Any, seconds: float = 60) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while True:
        remaining = deadline - loop.time()
        require(remaining > 0, "bounded authenticated graph readiness exhausted")
        try:
            async with asyncio.timeout(min(5, remaining)):
                await driver.verify_connectivity()
                async with driver.session() as session:
                    result = await session.run("RETURN 1 AS ready")
                    record = await result.single()
                    require(record is not None and record["ready"] == 1, "authenticated graph readiness query failed")
            return
        except unavailable as error:
            if not connection_refused(error):
                raise
            await asyncio.sleep(min(1, max(0, deadline - loop.time())))


async def schema_graph(provider: str) -> None:
    driver = graph_driver()
    try:
        await await_graph_ready(driver, importlib.import_module("neo4j.exceptions").ServiceUnavailable)
        require(await load_schema("neo4j_schema").create_neo4j_schema(driver) == 0, "partial graph schema initialization")
        if provider == "musicbrainz":
            # Legacy MB enrichers require an existing Discogs match; empty-store skips are not acceptance.
            async with driver.session() as session:
                for entity, label in LABELS.items():
                    if entity == "release-groups":
                        continue
                    result = await session.run(
                        f"CREATE (n:{label} {{id: $id, name: 'Owned synthetic seed', sha256: 'synthetic-seed'}})", id=DISCOGS_IDS[entity]
                    )
                    await result.consume()
                result = await session.run("CREATE (:Artist {id: '999000005', name: 'Owned Synthetic Group', sha256: 'synthetic-seed'})")
                await result.consume()
    finally:
        await driver.close()


async def consume_owned(body: Any, message: Any, data_type: str, stop: asyncio.Event, failure: Path) -> None:
    try:
        await body(message, data_type)
    except (ValueError, OSError, KeyError, TypeError):
        failure.write_text("owned synthetic callback assertion failed\n")
        stop.set()


def same_time(left: str, right: str) -> bool:
    return datetime.fromisoformat(left.replace("Z", "+00:00")) == datetime.fromisoformat(right.replace("Z", "+00:00"))


def validate_resume_delta(
    provider: str, delta: list[dict[str, Any]], release: dict[str, Any], partial: dict[str, Any], final: dict[str, Any]
) -> None:
    require(len(delta) == 6, "partial resume frame count mismatch")
    require(all(frame["persistent"] is True for frame in delta), "partial resume nonpersistent frame")
    repeated = [frame for frame in delta if frame["event"]["type"] == "data"]
    require(
        len(repeated) == 1 and repeated[0]["entity"] == "releases" and repeated[0]["event"] == release,
        "partial resume reprocessed completed data or changed hash/fields",
    )
    files = [frame for frame in delta if frame["event"]["type"] == "file_complete"]
    require(
        len(files) == 1 and files[0]["entity"] == "releases" and files[0]["event"]["total_processed"] == 1, "partial resume file completion mismatch"
    )
    completions = [frame for frame in delta if frame["event"]["type"] == "extraction_complete"]
    require(
        len(completions) == 4 and {frame["entity"] for frame in completions} == set(ENTITIES[provider]), "partial run completion missing/duplicate"
    )
    expected = {"releases": 1} if provider == "discogs" else {key: len(ids) for key, ids in expected_ids(provider).items()}
    for frame in completions:
        require(frame["event"]["record_counts"] == expected, "partial completion lost persisted record counts")
        require(frame["event"]["version"] == partial["current_version"], "partial completion changed version")
        require(same_time(frame["event"]["started_at"], partial["processing_phase"]["started_at"]), "partial completion reset original start time")
    require(final["summary"]["overall_status"] == "completed", "partial marker incomplete")
    require(same_time(final["processing_phase"]["started_at"], partial["processing_phase"]["started_at"]), "partial marker reset original start time")
    pending_file = "discogs_20000104_releases.xml.gz" if provider == "discogs" else "release.jsonl.xz"
    for filename, progress in partial["processing_phase"]["progress_by_file"].items():
        actual = final["processing_phase"]["progress_by_file"][filename]
        if filename != pending_file:
            require(actual == progress, "partial resume changed completed file progress")
        else:
            require(actual["status"] == "completed" and actual["records_extracted"] == 1, "pending release did not finish exactly once")
            require(actual.get("source_checksum") == progress.get("source_checksum"), "partial resume changed source byte provenance")
    require(final["processing_phase"]["files_processed"] == partial["processing_phase"]["files_processed"] + 1, "partial file counter mismatch")
    require(final["processing_phase"]["records_extracted"] == partial["processing_phase"]["records_extracted"] + 1, "partial record counter mismatch")


async def capture_queue(channel: Any, entity: str) -> Any:
    # Connection ownership matches the observer lifetime; no deprecated broker opt-in.
    return await channel.declare_queue(f"owned-capture-{entity}", durable=False, exclusive=True, auto_delete=True)


async def observe(provider: str) -> None:
    aio = importlib.import_module("aio_pika")
    prefix = os.environ[f"{provider.upper()}_EXCHANGE_PREFIX"]
    connection = await aio.connect_robust(f"amqp://synthetic-smoke:{quote(password(), safe='')}@broker:5672/%2F")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, stop.set)
    output = Path("/captures/events.ndjson")
    total = 0
    async with connection:
        channel = await connection.channel()
        await channel.set_qos(prefetch_count=1)
        for entity in ENTITIES[provider]:
            exchange = await channel.declare_exchange(f"{prefix}-{entity}", aio.ExchangeType.FANOUT, durable=True, auto_delete=False)
            queue = await capture_queue(channel, entity)
            await queue.bind(exchange, routing_key="")

            async def consume_body(message: Any, data_type: str = entity) -> None:
                nonlocal total
                event = json.loads(message.body)
                require(event.get("type") in {"data", "file_complete", "extraction_complete"}, "unexpected actual event envelope")
                require(message.delivery_mode == aio.DeliveryMode.PERSISTENT, "actual frame is not persistent")
                require(total < 128, "owned capture event cap exceeded")
                body = (
                    json.dumps(
                        {"entity": data_type, "event": event, "persistent": True, "traceparent_present": "traceparent" in (message.headers or {})}
                    )
                    + "\n"
                )
                require(output.stat().st_size + len(body.encode()) <= 128 * 1024, "owned capture byte cap exceeded")
                with output.open("a") as stream:
                    stream.write(body)
                total += 1
                await message.ack()

            async def consume(message: Any, data_type: str = entity, body: Any = consume_body) -> None:
                await consume_owned(body, message, data_type, stop, Path("/captures/failed"))

            await queue.consume(consume)
        output.touch()
        Path("/captures/ready").write_text("capture bindings ready\n")
        await stop.wait()
        require(not Path("/captures/failed").exists(), "observer callback failed")


def events() -> list[dict[str, Any]]:
    baseline = Path("/captures/baseline.ndjson")
    path = baseline if baseline.is_file() else Path("/captures/events.ndjson")
    return [json.loads(line) for line in path.read_text().splitlines()]


def expected_mb_fields(entity: str, identity: str) -> dict[str, Any]:
    """Independent normalized sentinels, including non-null legacy projections."""
    if entity == "artists":
        return (
            {"name": "Owned Synthetic Group", "mb_type": "Group"}
            if identity.endswith("000005")
            else {"name": "Owned Synthetic artist", "mb_type": "Person", "gender": "Other"}
        )
    if entity == "labels":
        return {"name": "Owned Synthetic label", "mb_type": "Original Production", "label_code": 77}
    if entity == "release-groups":
        return {"name": "Owned Synthetic release-group", "mb_type": "Album", "first_release_date": "2000-01-01"}
    return {"name": "Owned Synthetic release", "status": "Official", "barcode": "9990000000001", "release_group_mbid": MB_IDS["release-groups"]}


def validate_mb_graph(entity: str, event: dict[str, Any], properties: dict[str, Any]) -> None:
    require(properties.get("mbid") == event["id"], "legacy MB graph skipped enrichment")
    fields = {
        "artists": {"mb_type": "mb_type", "gender": "mb_gender"},
        "labels": {"mb_type": "mb_type", "label_code": "mb_label_code"},
        "release-groups": {"mb_type": "mb_type", "first_release_date": "mb_first_release_date"},
        "releases": {"status": "mb_status", "barcode": "mb_barcode", "release_group_mbid": "mb_release_group_mbid"},
    }[entity]
    for source, target in fields.items():
        if source in expected_mb_fields(entity, event["id"]):
            require(properties.get(target) == expected_mb_fields(entity, event["id"])[source], "legacy MB graph non-null projection lost")


def validate_frames(provider: str, captured: list[dict[str, Any]]) -> dict[str, Any]:
    require(bool(captured) and len(captured) <= 128, "missing or unbounded producer frames")
    expected = expected_ids(provider)
    data: dict[str, list[dict[str, Any]]] = {entity: [] for entity in ENTITIES[provider]}
    files: dict[str, list[dict[str, Any]]] = {entity: [] for entity in ENTITIES[provider]}
    complete: dict[str, list[dict[str, Any]]] = {entity: [] for entity in ENTITIES[provider]}
    for frame in captured:
        entity, event = frame["entity"], frame["event"]
        require(entity in ENTITIES[provider] and frame["persistent"] is True, "invalid captured routing/property")
        require(event["type"] in {"data", "file_complete", "extraction_complete"}, "unknown captured envelope")
        if event["type"] == "data":
            require(
                isinstance(event.get("id"), str) and isinstance(event.get("sha256"), str) and bool(re.fullmatch(r"[a-f0-9]{64}", event["sha256"])),
                "missing data id/hash",
            )
            data[entity].append(event)
        elif event["type"] == "file_complete":
            require(
                event.get("data_type") == entity and type(event.get("total_processed")) is int and event["total_processed"] == len(expected[entity]),
                "file completion count mismatch",
            )
            files[entity].append(event)
        else:
            require(all(key in event for key in ["version", "timestamp", "started_at", "record_counts"]), "run completion contract mismatch")
            complete[entity].append(event)
    for entity in ENTITIES[provider]:
        require(
            len(data[entity]) == len(expected[entity]) and {item["id"] for item in data[entity]} == expected[entity],
            "missing/duplicate/wrong synthetic data",
        )
        require(len(files[entity]) == 1, "missing/duplicate file completion")
        if provider == "discogs":
            expected_versions = {f"2000010{i}": name for i, name in enumerate(ENTITIES[provider], 1)}
            require(
                len(complete[entity]) == 4 and {item["version"] for item in complete[entity]} == set(expected_versions),
                "missing/duplicate run completion",
            )
            for item in complete[entity]:
                require(item["record_counts"] == {expected_versions[item["version"]]: 1}, "wrong run completion record counts")
        else:
            require(len(complete[entity]) == 1 and complete[entity][0]["version"] == "20000101-000000", "missing/duplicate run completion")
            require(
                complete[entity][0]["record_counts"] == {key: len(value) for key, value in expected.items()}, "wrong run completion record counts"
            )
        for item in data[entity]:
            if provider == "discogs":
                field = "name" if entity in {"artists", "labels"} else "title"
                require(
                    item.get(field)
                    == "Owned Synthetic " + {"artists": "Artist", "labels": "Label", "masters": "Master", "releases": "Release"}[entity],
                    "wrong independently expected normalized field",
                )
            else:
                native = "masters" if entity == "release-groups" else entity
                suffix = {"artists": "artist", "labels": "label", "release-groups": "master", "releases": "release"}[entity]
                target = 999000005 if item["id"].endswith("000005") else int(DISCOGS_IDS[native])
                require(item.get(f"discogs_{suffix}_id") == target, "wrong normalized Discogs mapping")
                require(
                    all(item.get(key) == value for key, value in expected_mb_fields(entity, item["id"]).items()), "wrong normalized sentinel fields"
                )
    release = data["releases"][0]
    require(isinstance(release.get("media"), dict) and release["media"].get("taxonomy_version") == "1", "actual normalized media not emitted")
    if provider == "musicbrainz":
        require(
            release.get("release_group_mbid") == MB_IDS["release-groups"] and release.get("status") == "Official",
            "normalized release relation/status lost",
        )
    return {"data": data, "json_frames": len(captured), "entities": sorted(data), "actual_completion": True}


def validate_discogs_sql_payload(entity: str, event: dict[str, Any], persisted: dict[str, Any]) -> None:
    expected = {key: value for key, value in event.items() if key not in {"type", "id", "sha256"}}
    # Exact pinned legacy normalizer parses master year and derives release year.
    if entity == "masters":
        require(event.get("year") in {"2000", 2000}, "wrong independent master year sentinel")
        expected["year"] = 2000
    elif entity == "releases":
        require(event.get("released") == "2000-01-01", "wrong independent release date sentinel")
        expected["year"] = 2000
    if entity in {"masters", "releases"}:
        require(type(persisted.get("year")) is int and persisted["year"] == 2000, "legacy SQL normalized year lost")
    require(all(persisted.get(key) == value for key, value in expected.items()), "SQL expected sentinel fields/relationships lost")


async def verify_sql(provider: str) -> dict[str, Any]:
    result = validate_frames(provider, events())
    sql = importlib.import_module("psycopg.sql")
    connection = await sql_connection()
    try:
        async with connection.cursor() as cursor:
            for entity, entity_events in result["data"].items():
                for event in entity_events:
                    if provider == "discogs":
                        await cursor.execute(sql.SQL("SELECT hash, data FROM {} WHERE data_id=%s").format(sql.Identifier(entity)), (event["id"],))
                        row = await cursor.fetchone()
                        require(row is not None and row[0] == event["sha256"], "legacy SQL id/hash persistence mismatch")
                        validate_discogs_sql_payload(entity, event, row[1])
                    else:
                        table = entity.replace("-", "_")
                        await cursor.execute(
                            sql.SQL("SELECT data FROM {} WHERE mbid=%s").format(sql.Identifier("musicbrainz", table)), (event["id"],)
                        )
                        row = await cursor.fetchone()
                        require(row is not None, "legacy MB SQL sentinel absent")
                        require(
                            all(row[0].get(key) == value for key, value in event.items() if key not in {"type", "id", "sha256"}),
                            "MB SQL expected sentinel fields/relationships lost",
                        )
    finally:
        await connection.close()
    return {"entities": result["entities"], "frames": result["json_frames"], "actual_completion": True, "legacy_sql": "passed"}


async def verify_graph(provider: str) -> dict[str, Any]:
    result = validate_frames(provider, events())
    driver = graph_driver()
    try:
        async with driver.session() as session:
            for entity, entity_events in result["data"].items():
                for event in entity_events:
                    label = LABELS[entity]
                    node_id = (
                        event["id"]
                        if provider == "discogs"
                        else ("999000005" if event["id"].endswith("000005") else DISCOGS_IDS["masters" if entity == "release-groups" else entity])
                    )
                    query = await session.run(f"MATCH (n:{label} {{id: $id}}) RETURN properties(n) AS p", id=node_id)
                    record = await query.single()
                    require(record is not None, "legacy graph sentinel absent")
                    properties = record["p"]
                    if provider == "discogs":
                        require(properties.get("sha256") == event["sha256"], "legacy graph hash mismatch")
                        field = "name" if entity in {"artists", "labels"} else "title"
                        require(properties.get(field) == event[field], "legacy graph expected sentinel field lost")
                    else:
                        validate_mb_graph(entity, event, properties)
            if provider == "discogs":
                for edge, label, target in [("BY", "Artist", "999000001"), ("ON", "Label", "999000002"), ("DERIVED_FROM", "Master", "999000003")]:
                    query = await session.run(
                        f"MATCH (:Release {{id: '999000004'}})-[:{edge}]->(:{label} {{id: $target}}) RETURN count(*) AS n", target=target
                    )
                    record = await query.single()
                    require(record is not None and record["n"] == 1, "legacy Discogs relationship lost")
            else:
                query = await session.run(
                    "MATCH (:Artist {id: '999000001'})-[r:MEMBER_OF]->(:Artist {id: '999000005'}) WHERE r.source='musicbrainz' RETURN count(r) AS n"
                )
                record = await query.single()
                require(record is not None and record["n"] == 1, "legacy MB artist relationship lost")
    finally:
        await driver.close()
    return {
        "entities": result["entities"],
        "legacy_graph": "passed",
        "note": "Legacy projections asserted; no claim that older graph consumers project every newly normalized extra block",
    }


async def main_async() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=["schema-sql", "schema-graph", "observer", "verify-sql", "verify-graph"])
    parser.add_argument("provider", choices=tuple(ENTITIES))
    args = parser.parse_args()
    result: Any = None
    if args.role == "schema-sql":
        await schema_sql()
    elif args.role == "schema-graph":
        await schema_graph(args.provider)
    elif args.role == "observer":
        await observe(args.provider)
    elif args.role == "verify-sql":
        result = await verify_sql(args.provider)
    else:
        result = await verify_graph(args.provider)
    print(json.dumps({"role": args.role, "provider": args.provider, "result": result, "passed": True}))


if __name__ == "__main__":
    asyncio.run(main_async())
