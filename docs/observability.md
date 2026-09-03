# Observability

This document is the canonical reference for GrooveMap metrics: the backend
that stores them, the conventions every service instruments against, and the
metric catalog dashboards are allowed to reference.

[`docs/monitoring.md`](monitoring.md) covers operating a running environment
(health checks, logs, incident snapshots). This document covers telemetry.

## Architecture

```text
  application services                backend                    UI
  ────────────────────                ───────                    ──

  api, extractor-*, ...  ──OTLP/HTTP──▶ otel-collector ──remote-write──▶ prometheus ──▶ grafana
                            :4318          :4317 :4318                     :9090          :3000
                                             │  ▲
  rabbitmq :15692                            │  │
  postgres-exporter :9187  ◀──── prometheus receiver (scrape)
  redis-exporter :9121                       │
  otel-collector :8888     ◀─────────────────┘
```

Two collection paths meet in one collector:

- **Application metrics are pushed.** Every GrooveMap service exports OTLP over
  HTTP/protobuf to `http://otel-collector:4318`. No application service exposes
  a Prometheus scrape endpoint for its own OTEL metrics.
- **Infrastructure metrics are scraped.** RabbitMQ, PostgreSQL, and Redis have
  no OTLP support, so the collector's Prometheus receiver scrapes their
  exporters and folds those series into the same pipeline.

The collector then remote-writes everything to Prometheus, which runs purely as
a remote-write receiver and scrapes nothing itself. Grafana reads Prometheus and
is provisioned entirely from files in this repository.

The existing JSON `/metrics` endpoints on the Rust extractors are unrelated to
this pipeline. They are part of the ADR-0005 HTTP contract that the operations
console reads, and they stay as they are.

## Ports

| Service | Port | Published | Purpose |
| --- | ---: | --- | --- |
| `otel-collector` | 4317 | no | OTLP/gRPC ingest (enabled for future use) |
| `otel-collector` | 4318 | no | OTLP/HTTP-protobuf ingest — the org standard |
| `otel-collector` | 8888 | no | Collector self-metrics, scraped by the collector |
| `otel-collector` | 13133 | no | `health_check` extension liveness endpoint |
| `prometheus` | 9090 | dev only | UI, API, and the remote-write receiver |
| `grafana` | 3000 | yes | Dashboards |

In production the Prometheus publish is replaced with a loopback binding
(`127.0.0.1:9090:9090`). Prometheus has no authentication of its own and its
API can delete series, so reach it through Grafana or an SSH tunnel.

## Backend services

All three images are digest pinned like every other third-party image in this
repository; `scripts/check-images.py` enforces that.

| Service | Image | Config | State |
| --- | --- | --- | --- |
| `otel-collector` | `otel/opentelemetry-collector-contrib` | `config/otel-collector.yaml` (read-only mount) | stateless |
| `prometheus` | `prom/prometheus` | `config/prometheus.yml` (read-only mount) | `prometheus_data` volume, 15d retention |
| `grafana` | `grafana/grafana` | `config/grafana/` (read-only mount) | `grafana_data` volume |

### Collector pipeline

```text
otlp ─▶ memory_limiter ─▶ batch ─▶ prometheusremotewrite ─▶ http://prometheus:9090/api/v1/write
```

`memory_limiter` runs first so back-pressure is applied before batching
allocates. `resource_to_telemetry_conversion` is enabled, which promotes the
OTEL resource attributes (`service.name`, `service.namespace`,
`deployment.environment.name`, `service.version`) to Prometheus labels.

The collector image is distroless, so its container healthcheck re-validates the
mounted config with the collector's own binary rather than calling an HTTP
endpoint. The operator-facing liveness probe is
`http://otel-collector:13133/`, reachable from any other container on the
`groovemap` network.

### Service environment

Every internal-image service is handed the same three variables. Two are shared
through the `x-otel-env` anchor in `docker-compose.yml`; `OTEL_SERVICE_NAME`
differs per service and is set on the service itself.

| Variable | Value |
| --- | --- |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://otel-collector:4318` |
| `OTEL_SERVICE_NAME` | the service's own compose key |
| `OTEL_RESOURCE_ATTRIBUTES` | `service.namespace=groovemap,deployment.environment.name=dev` (`prod` in the production overlay) |

These eleven services are wired:

| Service | `OTEL_SERVICE_NAME` |
| --- | --- |
| `schema-init` | `schema-init` |
| `api` | `api` |
| `extractor-discogs` | `extractor-discogs` |
| `extractor-musicbrainz` | `extractor-musicbrainz` |
| `graphinator` | `graphinator` |
| `brainzgraphinator` | `brainzgraphinator` |
| `tableinator` | `tableinator` |
| `brainztableinator` | `brainztableinator` |
| `dashboard` | `dashboard` |
| `explore` | `explore` |
| `insights` | `insights` |

Each of them also declares `depends_on: otel-collector` with condition
`service_started` — never `service_healthy`. Telemetry must not be able to hold
an application down. A service that starts before the collector is ready loses
at most its first export interval.

`OTEL_METRICS_EXPORTER` and `OTEL_METRIC_EXPORT_INTERVAL` are left at their SDK
defaults (`otlp`, 15000 ms). Setting `OTEL_METRICS_EXPORTER=none` on a single
service is the supported way to mute it without touching its code.

### Infrastructure exporters

RabbitMQ, PostgreSQL, and Redis speak no OTLP, so the collector's Prometheus
receiver scrapes them. None of these ports is published to the host: exporters
expose server internals and have no authentication of their own.

| Scrape job | Target | Source |
| --- | --- | --- |
| `rabbitmq` | `rabbitmq:15692` | `rabbitmq_prometheus` plugin, enabled through `config/rabbitmq-enabled-plugins` |
| `postgres-exporter` | `postgres-exporter:9187` | `prometheuscommunity/postgres-exporter` |
| `redis-exporter` | `redis-exporter:9121` | `oliver006/redis_exporter` |
| `otel-collector` | `otel-collector:8888` | the collector's own telemetry |

Job names deliberately match the docker-compose service keys, which is what the
dashboards filter on.

Both exporters take their credentials the same way the application services do:
literal values in development, and `_FILE`-backed Docker secrets
(`postgres_username`, `postgres_password`, `redis_password`) in production.
They reuse the existing secrets — no new credential is introduced.

`redis-exporter` has no container healthcheck. Its image is distroless and
ships neither a shell nor an HTTP client, so no probe can run inside it; a
failed exporter surfaces as a stale `redis-exporter` job on the infrastructure
dashboard.

Neo4j Community Edition has no Prometheus endpoint, so there is no Neo4j
exporter. Neo4j is observed through the application-side
`db.client.operation.duration` series and its container healthcheck.

### Grafana access

Development enables anonymous `Viewer` access so the dashboards open without a
login. Production disables it and sources the admin password from the
`grafana_admin_password` Docker secret, created by
`bash scripts/create-secrets.sh`.

## Conventions

These are the program-wide conventions from the `gm-deployment-gxr` epic. Every
GrooveMap service molecule instruments against them.

### Transport

- Every service **pushes** metrics over OTLP/HTTP-protobuf to the collector:
  `OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318`. HTTP/protobuf, not
  gRPC: no `grpcio`/`tonic` native dependency, and it works for one-shot jobs
  and stdio processes.
- No service exposes a Prometheus `/metrics` scrape endpoint for its own OTEL
  metrics.
- Standard env vars only, read by the SDK: `OTEL_EXPORTER_OTLP_ENDPOINT`,
  `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES`, `OTEL_METRICS_EXPORTER`
  (`otlp`|`none`), `OTEL_METRIC_EXPORT_INTERVAL` (default 15000 ms). No
  GrooveMap-specific telemetry env vars.
- When `OTEL_EXPORTER_OTLP_ENDPOINT` is unset or `OTEL_METRICS_EXPORTER=none`
  the bootstrap installs a no-op `MeterProvider`. Telemetry must **never** fail
  startup, block the event loop, or raise into application code; exporter errors
  are logged once at `WARNING`.
- Cumulative temporality (Prometheus-compatible). Explicit-bucket histograms in
  seconds.
- One-shot processes (`schema-init`, CLIs) must `force_flush` and `shutdown` the
  provider on exit so the last export lands.

### Resource attributes

- `service.name` = the docker-compose service key (`api`, `extractor-discogs`,
  `extractor-musicbrainz`, `graphinator`, `brainzgraphinator`, `tableinator`,
  `brainztableinator`, `dashboard`, `explore`, `insights`, `schema-init`,
  `mcp-server`). Set via `OTEL_SERVICE_NAME` in compose; the code default is the
  package's canonical name.
- `service.namespace=groovemap` and `deployment.environment.name=<dev|prod>` via
  `OTEL_RESOURCE_ATTRIBUTES` in compose.
- `service.version` = the package version (`importlib.metadata` /
  `CARGO_PKG_VERSION`), set by the bootstrap.

### Naming and cardinality

Metrics are declared with OTEL dot-names. Prometheus sees dots as underscores
plus unit suffixes, so `groovemap.pipeline.messages` becomes
`groovemap_pipeline_messages_total` and `http.server.request.duration` becomes
`http_server_request_duration_seconds_bucket`.

Use an OTEL semantic convention wherever one exists. Attribute values are
**low-cardinality only** — never put ids, file names, or free text in
attributes.

## Metric catalog

This is the canonical catalog. `scripts/check-dashboards.py` refuses any
dashboard that references a metric outside it, so a metric must land here before
a panel can use it.

The Prometheus column gives the base name. Histograms additionally expose
`_bucket`, `_sum`, and `_count` series; that expansion is understood by the
dashboard checker and is not repeated per row.

### Semantic-convention metrics

| OTEL name | Prometheus name | Kind | Attributes |
| --- | --- | --- | --- |
| `http.server.request.duration` | `http_server_request_duration_seconds` | histogram (s) | `http.request.method`, `http.route`, `http.response.status_code` |
| `http.client.request.duration` | `http_client_request_duration_seconds` | histogram (s) | `http.request.method`, `server.address`, `http.response.status_code` |
| `db.client.operation.duration` | `db_client_operation_duration_seconds` | histogram (s) | `db.system.name` (`postgresql`\|`neo4j`\|`redis`), `db.operation.name`, `error.type`? |
| `messaging.client.consumed.messages` | `messaging_client_consumed_messages_total` | counter | `messaging.system=rabbitmq`, `messaging.destination.name`, `messaging.operation.name`, `error.type`? |
| `messaging.client.sent.messages` | `messaging_client_sent_messages_total` | counter | as above |
| `messaging.client.operation.duration` | `messaging_client_operation_duration_seconds` | histogram (s) | as above |

### Pipeline metrics

Shared across every consumer service with the same shape; attribute sets are
closed.

| OTEL name | Prometheus name | Kind | Attributes |
| --- | --- | --- | --- |
| `groovemap.pipeline.messages` | `groovemap_pipeline_messages_total` | counter | `source` (`discogs`\|`musicbrainz`), `entity`, `outcome` (`processed`\|`skipped`\|`failed`) |
| `groovemap.pipeline.message.duration` | `groovemap_pipeline_message_duration_seconds` | histogram (s) | `source`, `entity` |
| `groovemap.pipeline.batch.size` | `groovemap_pipeline_batch_size` | histogram (`{items}`) | `store` (`neo4j`\|`postgresql`), `entity` |
| `groovemap.pipeline.batch.flush.duration` | `groovemap_pipeline_batch_flush_duration_seconds` | histogram (s) | `store`, `entity`, `outcome` |
| `groovemap.pipeline.consumers.active` | `groovemap_pipeline_consumers_active` | up-down counter | `source` |
| `groovemap.pipeline.reconnects` | `groovemap_pipeline_reconnects_total` | counter | `system` (`rabbitmq`\|`neo4j`\|`postgresql`\|`redis`) |
| `groovemap.pipeline.circuit_breaker.state` | `groovemap_pipeline_circuit_breaker_state` | gauge (0 closed, 1 half-open, 2 open) | `system` |

### Extraction metrics

| OTEL name | Prometheus name | Kind | Attributes |
| --- | --- | --- | --- |
| `groovemap.extraction.records` | `groovemap_extraction_records_total` | counter | `source`, `entity` |
| `groovemap.extraction.files` | `groovemap_extraction_files_total` | counter | `source`, `outcome` (`completed`\|`skipped`\|`failed`) |
| `groovemap.extraction.file.progress` | `groovemap_extraction_file_progress_ratio` | gauge (ratio 0..1) | `source`, `entity` |
| `groovemap.extraction.download.bytes` | `groovemap_extraction_download_bytes_total` | counter (By) | `source` |
| `groovemap.extraction.publish.confirm.duration` | `groovemap_extraction_publish_confirm_duration_seconds` | histogram (s) | `source` |
| `groovemap.extraction.errors` | `groovemap_extraction_errors_total` | counter | `source`, `stage` (`download`\|`parse`\|`publish`) |

### Service metrics

| OTEL name | Prometheus name | Kind | Attributes |
| --- | --- | --- | --- |
| `groovemap.api.sync.duration` | `groovemap_api_sync_duration_seconds` | histogram (s) | `outcome` |
| `groovemap.api.cache` | `groovemap_api_cache_total` | counter | `outcome` (`hit`\|`miss`), `cache` |
| `groovemap.api.nlq.requests` | `groovemap_api_nlq_requests_total` | counter | `outcome` |
| `groovemap.explore.proxy.duration` | `groovemap_explore_proxy_duration_seconds` | histogram (s) | `http.route`, `outcome` |
| `groovemap.insights.computation.duration` | `groovemap_insights_computation_duration_seconds` | histogram (s) | `computation`, `outcome` |
| `groovemap.insights.last_success` | `groovemap_insights_last_success_seconds` | gauge (unix s) | `computation` |
| `groovemap.schema_init.duration` | `groovemap_schema_init_duration_seconds` | histogram (s) | `store`, `outcome` |
| `groovemap.console.websocket.connections` | `groovemap_console_websocket_connections` | up-down counter | — |
| `groovemap.console.poll.duration` | `groovemap_console_poll_duration_seconds` | histogram (s) | `target`, `outcome` |
| `groovemap.mcp.tool.calls` | `groovemap_mcp_tool_calls_total` | counter | `tool`, `outcome` |
| `groovemap.mcp.tool.duration` | `groovemap_mcp_tool_duration_seconds` | histogram (s) | `tool` |

`groovemap.explore.proxy.duration` overlaps `http.client.request.duration` on
purpose. `graph-explorer` proxies `/api/*` to `catalog-api` with
`client.send(req, stream=True)`, so the semantic-convention client metric stops
at the response headers and says nothing about the time spent draining a
streamed SSE body. The domain metric times the whole proxied request, which is
what an operator watching a slow stream needs. `http.route` carries the
templated `/api/{path:path}`, matching what the FastAPI instrumentation reports
for the same request, and `outcome` is one of `success`, `timeout`, or
`upstream_error`.

### Metrics that are deliberately absent

Neo4j Community Edition has no Prometheus endpoint. There is no Neo4j exporter
in this stack, and Neo4j health is observed through the application-side
`db.client.operation.duration` series and the container healthcheck.

## Dashboards

Dashboards are code. They live in `config/grafana/dashboards`, Grafana loads
them read-only from a bind mount, and nothing is ever edited in the Grafana UI —
an edit there is not saved back to the repository and is reverted on the next
restart.

| Dashboard | uid | Covers |
| --- | --- | --- |
| Pipeline overview | `groovemap-pipeline-overview` | Extractor publish rate, per-queue depth and consumers, per-service processed and failed rates, end-to-end lag proxy |
| Ingestion | `groovemap-ingestion` | Download bytes, file progress, records per second per entity, publish confirm p50/p95, reconnects, errors by stage |
| Consumers | `groovemap-consumers` | Messages per second by entity and outcome, message duration p95, batch size and flush latency by store, database operation duration, circuit breaker state, active consumers |
| API services | `groovemap-api-services` | RED per service and route, sync duration, cache hit ratio, NLQ outcomes, insights computation duration and staleness |
| Infrastructure | `groovemap-infrastructure` | RabbitMQ, PostgreSQL, and Redis exporter panels, plus collector points received, exported, and dropped |

The uids are stable and part of the contract: links and bookmarks depend on
them, so rename a title freely but never a uid.

### Provisioning

| File | Role |
| --- | --- |
| `config/grafana/provisioning/datasources/prometheus.yaml` | The Prometheus datasource, uid `prometheus`, not editable in the UI |
| `config/grafana/provisioning/dashboards/groovemap.yaml` | Loads `/var/lib/grafana/dashboards` into the `GrooveMap` folder |

Every panel resolves its datasource through the `${DS_PROMETHEUS}` template
variable rather than naming a uid. A dashboard that pins a literal uid works on
the machine it was exported from and nowhere else, so the checker rejects one.

### Adding a dashboard

1. Add a JSON file to `config/grafana/dashboards`. Give it a `uid` that starts
   with `groovemap-`, a unique `title`, and a `schemaVersion`.
2. Define a `DS_PROMETHEUS` datasource template variable, and set every panel's
   and every target's datasource to `{"type": "prometheus", "uid": "${DS_PROMETHEUS}"}`.
3. Add a `service`, `source`, or `queue` template variable so the dashboard can
   be narrowed to one thing.
4. Write PromQL against the **Prometheus** names in the catalog above, not the
   OTEL dot-names. Prefer `rate()` and `histogram_quantile()` over recording
   rules; this stack has no recording or alerting rules.
5. Run `uv run python scripts/check-dashboards.py`. It is part of
   `just source-check`, so `just check` runs it too.

The checker fails the build when a dashboard does not parse, is missing a `uid`,
`title`, or `schemaVersion`, duplicates another dashboard's uid or title, pins a
datasource uid instead of using the variable, has no queries, or references a
metric that is in neither the catalog above nor its exporter/collector
allowlist. Adding a metric to a service means adding it to the catalog first —
that is the ordering the gate enforces.

If a panel needs a new exporter or collector metric, add the exact name to
`EXPORTER_METRICS` in `scripts/check-dashboards.py`. The allowlist is explicit
rather than prefix-matched so a typo in `rabbitmq_queue_messages_ready` still
fails.

## Verification

Provisioning a dashboard proves nothing on its own: a panel whose metric never
arrives renders an empty graph, not an error. This runbook is the end-to-end
check that real service images push metrics that land on the provisioned
dashboards. Run it against the local Docker Compose stack. Never run it against
a live environment without operator approval.

The whole run takes a few minutes. Every command below is copy-pasteable from
the repository root.

### 1. Point `.env` at images that carry the telemetry work

`docker-compose.yml` requires a digest-pinned reference for each of the eleven
internal services, and `scripts/check-images.py` enforces that for every value
committed to the repository. `.env` is not committed, so fill it from
`.env.example` with the released digests you want to exercise:

```bash
cp .env.example .env
# Resolve each approved release tag to its registry digest, then paste it in.
docker buildx imagetools inspect ghcr.io/groovemap-music/catalog-api:<tag> \
  --format '{{ .Manifest.Digest }}'
```

Only images built after their repository adopted `common.telemetry` (Python) or
the `telemetry` module (Rust) export anything. An older image starts fine and
stays silent, which is exactly the failure this runbook is designed to catch.

### 2. Bring the stack up

```bash
just smoke
```

`just smoke` is `docker compose up -d --wait` followed by `docker compose ps`.
It brings up RabbitMQ, PostgreSQL, Neo4j, Redis, the two exporters, the
collector, Prometheus, Grafana, and every application service.

`--wait` blocks on container health. The Python service images ship no `curl`,
while the compose healthchecks for those services invoke `curl`, so on a stack
built from those images `--wait` never converges even though every service is
running and exporting. When that happens, start without the gate and read the
image's own healthcheck instead:

```bash
docker compose up -d
docker compose ps --format '{{.Service}}\t{{.State}}\t{{.Status}}'
```

### 3. Confirm the collector is receiving data points

The collector's self-metrics on `:8888` are the first place a break shows.
`accepted` climbing means services are pushing; `refused` climbing means the
collector is rejecting them. The port is not published, so read it from another
container on the `groovemap` network:

```bash
docker compose exec prometheus \
  wget -qO- http://otel-collector:8888/metrics | grep -E '^otelcol_(receiver|exporter)_'
```

Expect non-zero `otelcol_receiver_accepted_metric_points_total` for both the
`otlp` receiver (the application push path) and the `prometheus` receiver (the
infrastructure scrape path), a matching
`otelcol_exporter_sent_metric_points_total` for `prometheusremotewrite`, and
zero on the `refused` and `send_failed` counters.

The collector's liveness endpoint answers on the same network:

```bash
docker compose exec prometheus wget -qO- http://otel-collector:13133/
```

### 4. Confirm every expected service reached Prometheus

`service.name` is promoted to the `service_name` Prometheus label by the
collector's `resource_to_telemetry_conversion`, so the label's values are the
roll call of everything that exported:

```bash
curl -s 'http://localhost:9090/api/v1/label/service_name/values'
```

Expect the eleven compose service keys — `api`, `extractor-discogs`,
`extractor-musicbrainz`, `graphinator`, `brainzgraphinator`, `tableinator`,
`brainztableinator`, `dashboard`, `explore`, `insights`, `schema-init` — plus
four values that come from the scrape jobs rather than from a push:
`rabbitmq`, `postgres-exporter`, `redis-exporter`, and `otelcol-contrib`.

Two details are easy to misread. `schema-init` runs once and exits, so it
appears only because the one-shot bootstrap flushes on shutdown; its absence
means the flush regressed. And the collector's own scrape job is labelled
`otelcol-contrib`, not `otel-collector`, because `service.name` from the
collector's own telemetry wins over the job name — the three exporter jobs do
match their compose service keys.

A service that is missing here is either running an image without the telemetry
work, or failing its telemetry bootstrap. The bootstrap never raises, so check
the logs for the warning rather than for a crash:

```bash
docker compose logs <service> | grep -i 'OpenTelemetry'
```

### 5. Query one metric per dashboard

Each of these is a real panel query from the dashboard named above it, with the
template variables resolved. A `result` array with at least one entry means the
panel has data.

```bash
# Pipeline overview — queue depth from the RabbitMQ scrape job
curl -sG 'http://localhost:9090/api/v1/query' \
  --data-urlencode 'query=sum by (queue) (rabbitmq_queue_messages_ready)'

# Ingestion — bytes pulled by the extractors
curl -sG 'http://localhost:9090/api/v1/query' \
  --data-urlencode 'query=sum by (source) (groovemap_extraction_download_bytes_total)'

# Consumers — database call latency from the consumer services
curl -sG 'http://localhost:9090/api/v1/query' \
  --data-urlencode 'query=histogram_quantile(0.95, sum by (le, db_system_name) (rate(db_client_operation_duration_seconds_bucket[5m])))'

# API services — request rate per service
curl -sG 'http://localhost:9090/api/v1/query' \
  --data-urlencode 'query=sum by (service_name) (rate(http_server_request_duration_seconds_count[5m]))'

# Infrastructure — every scrape target reporting up
curl -sG 'http://localhost:9090/api/v1/query' \
  --data-urlencode 'query=up'
```

`tests/deploy/test_observability_runbook.py` pins these queries against the
catalog above, so a metric renamed in the catalog fails the build here rather
than silently emptying a panel.

Panels stay empty when the workload that feeds them has not run, and that is
not a fault. A stack that has only just started, with no dump files to extract
and no messages in flight, leaves the pipeline and extraction panels blank
while the infrastructure, API, and schema panels fill immediately. Drive the
pipeline before concluding a panel is broken.

### 6. Confirm Grafana has the five dashboards

Development enables anonymous `Viewer` access, so `http://localhost:3000` opens
the dashboards in a browser without a login. The search API is the scriptable
form of the same check:

```bash
curl -s 'http://localhost:3000/api/search?type=dash-db' \
  | python3 -c 'import json,sys; [print(d["uid"], "|", d["title"]) for d in json.load(sys.stdin)]'
```

Expect exactly five rows, one per uid: `groovemap-pipeline-overview`,
`groovemap-ingestion`, `groovemap-consumers`, `groovemap-api-services`,
`groovemap-infrastructure`.

An `Unauthorized` body instead of that list means anonymous access is not in
effect — always on the production overlay, which disables it, and also on a
development stack whose Grafana cannot read its own database. Authenticate from
the environment rather than putting a credential on the command line:

```bash
GRAFANA_AUTH="admin:$(cat secrets/grafana_admin_password)"
curl -s --user "${GRAFANA_AUTH}" 'http://localhost:3000/api/search?type=dash-db'
```

If that is `Unauthorized` too, the problem is Grafana's own state rather than
the credential; check `docker compose logs grafana` and the `grafana_data`
volume before suspecting the provisioning.

A dashboard missing from an otherwise good listing, when the file does exist in
`config/grafana/dashboards`, means the provisioning mount or the provider file
is wrong, not the dashboard.

### 7. Tear the stack down

```bash
docker compose down -v
```

`-v` removes the volumes this run created, including `prometheus_data` and
`grafana_data`. Leave it off to keep the collected series for a second look.

## Rollout

The program rolls out in three stages, which are cross-hive and therefore not
expressible as bead dependencies:

1. `python-libraries` (`common.telemetry`), this repository (backend and env
   wiring), `design` (ADR), and `catalog-ingestion` (Rust telemetry module) run
   in parallel.
2. After `python-libraries` merges, every Python service bumps its
   `groovemap-runtime` rev to that commit and adopts `common.telemetry`.
   `discogs-ingestion` and `musicbrainz-ingestion` port the
   `catalog-ingestion` module.
3. Dashboards and end-to-end verification run last, against released images.

Until stage 2 lands, the collector accepts connections but no application series
arrive. The backend and the infrastructure exporters are still fully useful on
their own.
