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
| `groovemap.insights.computation.duration` | `groovemap_insights_computation_duration_seconds` | histogram (s) | `computation`, `outcome` |
| `groovemap.insights.last_success` | `groovemap_insights_last_success_seconds` | gauge (unix s) | `computation` |
| `groovemap.schema_init.duration` | `groovemap_schema_init_duration_seconds` | histogram (s) | `store`, `outcome` |
| `groovemap.console.websocket.connections` | `groovemap_console_websocket_connections` | up-down counter | — |
| `groovemap.console.poll.duration` | `groovemap_console_poll_duration_seconds` | histogram (s) | `target`, `outcome` |
| `groovemap.mcp.tool.calls` | `groovemap_mcp_tool_calls_total` | counter | `tool`, `outcome` |
| `groovemap.mcp.tool.duration` | `groovemap_mcp_tool_duration_seconds` | histogram (s) | `tool` |

### Metrics that are deliberately absent

Neo4j Community Edition has no Prometheus endpoint. There is no Neo4j exporter
in this stack, and Neo4j health is observed through the application-side
`db.client.operation.duration` series and the container healthcheck.

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
