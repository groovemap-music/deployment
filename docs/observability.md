# Observability

This document is the canonical reference for GrooveMap telemetry: the backends
that store metrics and traces, the conventions every service instruments
against, and the metric catalog dashboards are allowed to reference.

[`docs/monitoring.md`](monitoring.md) covers operating a running environment
(health checks, logs, incident snapshots). This document covers telemetry.

## Architecture

```text
  application services                backend                    UI
  ────────────────────                ───────                    ──

                                                  ──remote-write──▶ victoria-metrics ─┐
  api, extractor-*, ...  ──OTLP/HTTP──▶ otel-collector                   :8428          ├▶ grafana
                            :4318          :4317 :4318                                  │   :3000
                                             │  ▲   └─────────OTLP──────▶ victoria-traces ┘
  rabbitmq :15692                            │  │                          :10428
  postgres-exporter :9187  ◀──── prometheus receiver (scrape)
  redis-exporter :9121                       │
  cadvisor :8080                             │
  node-exporter :9100                        │
  otel-collector :8888     ◀─────────────────┘
```

Three collection paths meet in one collector:

- **Application metrics are pushed.** Every GrooveMap service exports OTLP over
  HTTP/protobuf to `http://otel-collector:4318`. No application service exposes
  a Prometheus scrape endpoint for its own OTEL metrics.
- **Infrastructure metrics are scraped.** RabbitMQ, PostgreSQL, and Redis have
  no OTLP support, so the collector's Prometheus receiver scrapes their
  exporters and folds those series into the same pipeline. The container
  runtime and the host kernel are scraped the same way, through cAdvisor and
  node-exporter.
- **Spans are pushed to the same endpoint.** Traces ride the OTLP receiver
  alongside metrics, take their own pipeline, and are written to VictoriaTraces.

The collector remote-writes every metric to VictoriaMetrics and pushes every
span to VictoriaTraces. VictoriaMetrics accepts the Prometheus remote-write
protocol natively and serves the Prometheus query API back, so nothing about
the metrics path other than the hostname changed when Prometheus was retired,
and it needs no configuration file of its own. VictoriaTraces serves the Tempo
query API under `/select/tempo`. Grafana reads both and is provisioned entirely
from files in this repository.

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
| `victoria-metrics` | 8428 | dev only | vmui, the Prometheus query API, and the remote-write receiver |
| `victoria-traces` | 10428 | dev only | OTLP trace ingest and the Tempo query API |
| `cadvisor` | 8080 | no | Per-container metrics, scraped by the collector |
| `node-exporter` | 9100 | no | Host metrics, scraped by the collector |
| `grafana` | 3000 | yes | Dashboards |

In production both Victoria publishes are replaced with loopback bindings
(`127.0.0.1:8428:8428` and `127.0.0.1:10428:10428`). Neither server has
authentication of its own, both accept unauthenticated writes, and the metrics
API can delete series, so reach them through Grafana or an SSH tunnel.

## Backend services

All four images are digest pinned like every other third-party image in this
repository; `scripts/check-images.py` enumerates the exact reference each
service runs and enforces that.

| Service | Image | Config | State |
| --- | --- | --- | --- |
| `otel-collector` | `otel/opentelemetry-collector-contrib` | `config/otel-collector.yaml` (read-only mount) | stateless |
| `victoria-metrics` | `victoriametrics/victoria-metrics` | command-line flags only | `victoria_metrics_data` volume, 15d retention |
| `victoria-traces` | `victoriametrics/victoria-traces` | command-line flags only | `victoria_traces_data` volume, 7d retention |
| `grafana` | `grafana/grafana` | `config/grafana/` (read-only mount) | `grafana_data` volume |

VictoriaMetrics is the organisation's metrics backend, and it is the only TSDB
in this stack. It is not a drop-in that needs coaxing: it accepts Prometheus
remote write with no flag and answers the Prometheus query API on the same
port, which is why the Grafana datasource below keeps `type: prometheus` and
uid `prometheus`, and why all five dashboards were unaffected by the swap. The
`victoriametrics-datasource` plugin is deliberately not used.

The `victoria-traces` image is distroless — no shell, no `wget`, no `curl` —
so its container healthcheck runs the server's own binary with `-version`
rather than calling an HTTP endpoint, the same compromise the collector makes.
`http://victoria-traces:10428/health` is the operator-facing liveness endpoint,
reachable from any other container on the `groovemap` network.

### Collector pipeline

```text
metrics: otlp, prometheus, spanmetrics ─▶ memory_limiter ─▶ batch ─▶ prometheusremotewrite
                                                                    ─▶ http://victoria-metrics:8428/api/v1/write

traces:  otlp ─▶ memory_limiter ─▶ batch ─▶ spanmetrics
                                          ─▶ otlphttp/victoria_traces
                                             ─▶ http://victoria-traces:10428/insert/opentelemetry/v1/traces
```

`memory_limiter` runs first in both pipelines so back-pressure is applied
before batching allocates. `resource_to_telemetry_conversion` is enabled on the
remote-write exporter, which promotes the OTEL resource attributes
(`service.name`, `service.namespace`, `deployment.environment.name`,
`service.version`) to Prometheus labels.

The `spanmetrics` connector sits in both pipelines at once: it is an exporter
of the traces pipeline and a receiver of the metrics pipeline. Every span that
passes through becomes RED metrics — request rate, error rate, duration — with
no service emitting a single extra instrument. It is configured with
`namespace: traces.span.metrics`, a histogram in seconds like every other
GrooveMap histogram, and explicit buckets, because exponential histograms do
not survive remote write intact. Its four built-in dimensions are exactly the
label set the conventions declare, so no extra dimensions are configured;
`collector.instance.id`, added by default, is excluded because one collector
runs here and it would only add a constant label to every series.

| OTEL name | Prometheus name | Kind | Labels |
| --- | --- | --- | --- |
| `traces.span.metrics.calls` | `traces_span_metrics_calls_total` | counter | `service_name`, `span_name`, `span_kind`, `status_code` |
| `traces.span.metrics.duration` | `traces_span_metrics_duration_seconds` | histogram (s) | as above |

The collector image is distroless, so its container healthcheck re-validates the
mounted config with the collector's own binary rather than calling an HTTP
endpoint. The operator-facing liveness probe is
`http://otel-collector:13133/`, reachable from any other container on the
`groovemap` network.

### Service environment

Every internal-image service is handed the same five variables. Four are shared
through the `x-otel-env` anchor in `docker-compose.yml`; `OTEL_SERVICE_NAME`
differs per service and is set on the service itself.

| Variable | Value |
| --- | --- |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://otel-collector:4318` (metrics and traces share it) |
| `OTEL_SERVICE_NAME` | the service's own compose key |
| `OTEL_RESOURCE_ATTRIBUTES` | `service.namespace=groovemap,deployment.environment.name=dev` (`prod` in the production overlay) |
| `OTEL_TRACES_SAMPLER` | `parentbased_traceidratio` in both environments |
| `OTEL_TRACES_SAMPLER_ARG` | `1.0` in dev, `0.1` in the production overlay |

`parentbased_traceidratio` means the sampling decision taken at the edge of a
request is honoured by every downstream service, so a trace is never recorded
only in half of the services it touched. Only the head rate changes between
environments: development keeps every span because the volumes are small and a
dropped span is a debugging dead end, while production keeps a tenth.

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
receiver scrapes them. Neither does the container runtime or the host kernel, so
cAdvisor and node-exporter are scraped the same way. None of these ports is
published to the host: exporters expose server internals and have no
authentication of their own.

| Scrape job | Target | Interval | Source |
| --- | --- | ---: | --- |
| `rabbitmq` | `rabbitmq:15692` | 15s | `rabbitmq_prometheus` plugin, enabled through `config/rabbitmq-enabled-plugins` |
| `postgres-exporter` | `postgres-exporter:9187` | 15s | `prometheuscommunity/postgres-exporter` |
| `redis-exporter` | `redis-exporter:9121` | 15s | `oliver006/redis_exporter` |
| `otel-collector` | `otel-collector:8888` | 15s | the collector's own telemetry |
| `cadvisor` | `cadvisor:8080` | 30s | `gcr.io/cadvisor/cadvisor` — per-container CPU, memory, network, and block I/O |
| `node-exporter` | `node-exporter:9100` | 30s | `prom/node-exporter` — host CPU, memory, load, disk, and filesystems |

Job names deliberately match the docker-compose service keys, which is what the
dashboards filter on.

cAdvisor and node-exporter run at 30s rather than 15s. cAdvisor emits a series
per container per interface and per device, node-exporter one per CPU per mode
and per filesystem, so between them they dominate the sample count; container
and host saturation move over minutes, and half the samples still show it.

`cadvisor` is **not** privileged, and that is a deliberate departure from the
upstream recipe. Measured against a privileged run on the same engine, the
capability set below produces an identical series set for every metric the
Containers & host dashboard plots:

| Grant | Why it is needed |
| --- | --- |
| `cap_drop: ALL` then `cap_add: DAC_READ_SEARCH` | Walks the per-container directories under `/var/lib/docker` whose modes exclude it. Without it the filesystem collector logs a permission error on every housekeeping pass. |
| `cap_add: SYSLOG` | Opens `/dev/kmsg`. |
| `devices: /dev/kmsg:/dev/kmsg:r` | `/dev/kmsg` is the only source of kernel OOM kill messages. Without it cAdvisor logs `Could not configure a source for OOM detection, disabling OOM events` and `container_oom_events_total` never leaves zero — silently, which for a stack that scrapes cAdvisor precisely to catch OOM kills is the worst available failure. `/dev/kmsg` exists on every Linux engine, including the VM behind Docker Desktop, Colima, and Rancher Desktop. |

All four of its mounts (`/`, `/var/run`, `/sys`, `/var/lib/docker`) are
read-only, as is the container's own root filesystem, and `no-new-privileges`
is set. Container labels are not stored wholesale — only
`com.docker.compose.project` and `com.docker.compose.service` are converted into
Prometheus labels, which is what the Containers & host dashboard filters on.

`node-exporter` needs no capability at all. It reads `/proc`, `/sys`, and `/`
through read-only bind mounts under `--path.rootfs=/rootfs`, and keeps
`no-new-privileges`, `cap_drop: ALL`, and `read_only: true`. Its filesystem
collector excludes the `/var/lib/docker` overlay mounts, without which every
container's layer appears as a separate host filesystem.

**On a VM-backed engine — Docker Desktop on macOS or Windows, Colima, Rancher
Desktop — node-exporter reports the Linux VM, not your laptop.** The engine runs
inside that VM, so `node_cpu_seconds_total`, `node_memory_MemTotal_bytes`, and
the filesystem series describe the VM's allotted cores, memory, and disk. That
is the right denominator for the containers on this dashboard, but it is not the
machine's own usage — a laptop at 20% can show a VM at 90%. cAdvisor is
unaffected: the containers it reports are the same containers either way.

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
| Containers & host | `groovemap-containers` | Per-container CPU, memory working set against limit, network, block I/O, restarts and OOM kills, plus host CPU, memory, load, disk, and filesystems |

The uids are stable and part of the contract: links and bookmarks depend on
them, so rename a title freely but never a uid.

### Provisioning

| File | Role |
| --- | --- |
| `config/grafana/provisioning/datasources/prometheus.yaml` | The Prometheus datasource (uid `prometheus`, VictoriaMetrics behind it) and the Tempo datasource (uid `tempo`, VictoriaTraces behind it), neither editable in the UI |
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
It brings up RabbitMQ, PostgreSQL, Neo4j, Redis, the four exporters
(`postgres-exporter`, `redis-exporter`, `cadvisor`, `node-exporter`), the
collector, VictoriaMetrics, VictoriaTraces, Grafana, and every application
service.

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
docker compose exec victoria-metrics \
  wget -qO- http://otel-collector:8888/metrics | grep -E '^otelcol_(receiver|exporter)_'
```

Expect non-zero `otelcol_receiver_accepted_metric_points_total` for both the
`otlp` receiver (the application push path) and the `prometheus` receiver (the
infrastructure scrape path), a matching
`otelcol_exporter_sent_metric_points_total` for `prometheusremotewrite`, and
zero on the `refused` and `send_failed` counters.

The collector's liveness endpoint answers on the same network:

```bash
docker compose exec victoria-metrics wget -qO- http://otel-collector:13133/
```

### 4. Confirm every expected service reached VictoriaMetrics

`service.name` is promoted to the `service_name` Prometheus label by the
collector's `resource_to_telemetry_conversion`, so the label's values are the
roll call of everything that exported:

```bash
curl -s 'http://localhost:8428/api/v1/label/service_name/values'
```

Expect the eleven compose service keys — `api`, `extractor-discogs`,
`extractor-musicbrainz`, `graphinator`, `brainzgraphinator`, `tableinator`,
`brainztableinator`, `dashboard`, `explore`, `insights`, `schema-init` — plus
six values that come from the scrape jobs rather than from a push:
`rabbitmq`, `postgres-exporter`, `redis-exporter`, `cadvisor`, `node-exporter`,
and `otelcol-contrib`.

Two details are easy to misread. `schema-init` runs once and exits, so it
appears only because the one-shot bootstrap flushes on shutdown; its absence
means the flush regressed. And the collector's own scrape job is labelled
`otelcol-contrib`, not `otel-collector`, because `service.name` from the
collector's own telemetry wins over the job name — the five exporter jobs do
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
curl -sG 'http://localhost:8428/api/v1/query' \
  --data-urlencode 'query=sum by (queue) (rabbitmq_queue_messages_ready)'

# Ingestion — bytes pulled by the extractors
curl -sG 'http://localhost:8428/api/v1/query' \
  --data-urlencode 'query=sum by (source) (groovemap_extraction_download_bytes_total)'

# Consumers — database call latency from the consumer services
curl -sG 'http://localhost:8428/api/v1/query' \
  --data-urlencode 'query=histogram_quantile(0.95, sum by (le, db_system_name) (rate(db_client_operation_duration_seconds_bucket[5m])))'

# API services — request rate per service
curl -sG 'http://localhost:8428/api/v1/query' \
  --data-urlencode 'query=sum by (service_name) (rate(http_server_request_duration_seconds_count[5m]))'

# Infrastructure — every scrape target reporting up
curl -sG 'http://localhost:8428/api/v1/query' \
  --data-urlencode 'query=up'

# Containers & host — CPU seconds per second, per compose service
curl -sG 'http://localhost:8428/api/v1/query' \
  --data-urlencode 'query=sum by (container_label_com_docker_compose_service) (rate(container_cpu_usage_seconds_total[5m]))'
```

`tests/deploy/test_observability_runbook.py` pins these queries against the
catalog above, so a metric renamed in the catalog fails the build here rather
than silently emptying a panel.

Panels stay empty when the workload that feeds them has not run, and that is
not a fault. A stack that has only just started, with no dump files to extract
and no messages in flight, leaves the pipeline and extraction panels blank
while the infrastructure, API, and schema panels fill immediately. Drive the
pipeline before concluding a panel is broken.

### 6. Confirm Grafana has every provisioned dashboard

Development enables anonymous `Viewer` access, so `http://localhost:3000` opens
the dashboards in a browser without a login. The search API is the scriptable
form of the same check:

```bash
curl -s 'http://localhost:3000/api/search?type=dash-db' \
  | python3 -c 'import json,sys; [print(d["uid"], "|", d["title"]) for d in json.load(sys.stdin)]'
```

Expect one row per file in `config/grafana/dashboards`, by uid:
`groovemap-pipeline-overview`, `groovemap-ingestion`, `groovemap-consumers`,
`groovemap-api-services`, `groovemap-infrastructure`, `groovemap-containers`.

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

`-v` removes the volumes this run created, including `victoria_metrics_data`,
`victoria_traces_data`, and `grafana_data`. Leave it off to keep the collected series for a second look.

### First execution, 2026-09-03

The runbook above was executed once against locally built images. This record
exists so the next operator knows what a good result looks like, and which
steps have never been executed successfully. Replace it after the first run
against released, digest-pinned images.

**Images.** Built from each repository's `main` at the commit shown, and fed to
compose through a local, uncommitted `.env`. No digest, `.env`, or image
reference from that run is committed here.

| Repository | Commit | Compose service |
| --- | --- | --- |
| `database-schema` | `c201562` | `schema-init` |
| `catalog-api` | `1b742a4` | `api` |
| `graph-explorer` | `3078603` | `explore` |
| `analytics-engine` | `787c836` | `insights` |
| `operations-console` | `ca364dc` | `dashboard` |
| `discogs-sql-loader` | `fa52a26` | `tableinator` |
| `musicbrainz-sql-loader` | `8134b8d` | `brainztableinator` |
| `musicbrainz-graph-enricher` | `d9984ea` | `brainzgraphinator` |
| `discogs-ingestion` | `e1ddf7e` | `extractor-discogs` |
| `musicbrainz-ingestion` | `9a0caee` | `extractor-musicbrainz` |

`discogs-sql-loader` is pinned at `fa52a26` deliberately. Its previous `main`,
`1f9af11`, installed the runtime wheel as `[postgres,rabbitmq]` and shipped an
image with no OTEL SDK at all. That image started normally, logged one warning,
installed the no-op `MeterProvider`, and exported nothing — the exact silent
failure step 4 is designed to catch, and a good argument for reading the
`service_name` roll call rather than trusting a green `docker compose ps`.

Both extractors ran their own repository's image with the compose `--source`
argument removed; neither binary accepts it any more.

**`service_name` values observed.** Fourteen, from step 4. Pushed by services:
`api`, `brainzgraphinator`, `brainztableinator`, `dashboard`, `explore`,
`extractor-discogs`, `extractor-musicbrainz`, `insights`, `schema-init`,
`tableinator`. From the scrape jobs: `otelcol-contrib`, `postgres-exporter`,
`rabbitmq`, `redis-exporter`. Ten of the eleven wired services exported.
`schema-init` appearing at all is the evidence that the one-shot flush on exit
works.

`groovemap.explore.proxy.duration` arrived as
`groovemap_explore_proxy_duration_seconds` with its `_bucket`, `_sum`, and
`_count` series, which is what the new API-services panel charts.

**Panels with data**, from running every panel's first query over the run
window:

| Dashboard | Panels returning series |
| --- | ---: |
| Infrastructure | 16 of 20 |
| API services | 9 of 15 |
| Consumers | 4 of 10 |
| Pipeline overview | 4 of 10 |
| Ingestion | 1 of 8 |

**Why each panel was empty.** Every one is missing workload, not a wrong metric
name. No dump file was processed and no message crossed the broker, which
accounts for all six consumer panels, seven of the eight ingestion panels, and
the six pipeline-overview panels that count processed, failed, or published
records. On API services, the sync, NLQ, and websocket panels need API calls
nobody made, the error-rate panel needs a 5xx nobody provoked, and the two MCP
panels can never fill from this stack because `mcp-server` is not a compose
service. On infrastructure, the three PostgreSQL rate panels and the RabbitMQ
publish rate were empty only because those exporters had fewer than two samples
in the window — the underlying counters were present.

**Not verified in this run.** The host volume filled during the run, which
damaged the Docker VM's data filesystem: ext4 aborted its journal, reads began
returning `EIO`, and `docker images`, `docker logs`, `docker exec`, and
container removal all stopped working. These four items were never completed
and must not be read as passing.

| Item | Why |
| --- | --- |
| Step 6, the Grafana `/api/search` listing | Grafana could not read its own database on the damaged volume and returned `Unauthorized` to both anonymous and credentialed requests. The five dashboards were never confirmed present through the API. |
| One clean pass of steps 1 through 7 | The run was interrupted partway. The evidence above is real but was assembled across a degraded stack, and step 7 could stop containers without removing them. |
| `graphinator` | `discogs-graph-enricher` landed its telemetry at `9cd4303`, after the build set for this run was taken. It was the one wired service with no telemetry at build time and did not export. |
| `mcp-server` | Not a compose service, so this stack cannot exercise it at all. Its telemetry landed at `511845d`. |

Re-running the whole runbook is the way to close these; there is no shortcut
that turns the partial evidence into a pass.

## Rollout

The program rolls out in three stages, which are cross-hive and therefore not
expressible as bead dependencies:

1. `python-libraries` (`common.telemetry`), this repository (backend and env
   wiring), `design` (ADR), and `discogs-ingestion` and `musicbrainz-ingestion`
   (Rust telemetry modules, each source-owned since ADR 0005 retired the
   combined `catalog-ingestion` repository) run in parallel.
2. After `python-libraries` merges, every Python service bumps its
   `groovemap-runtime` rev to that commit and adopts `common.telemetry`.
3. Dashboards and end-to-end verification run last, against released images.

Until stage 2 lands, the collector accepts connections but no application series
arrive. The backend and the infrastructure exporters are still fully useful on
their own.
