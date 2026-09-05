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
Series cAdvisor reports for the root cgroup and for containers started outside
this stack carry neither label, so a dashboard filtering on them must use an
`allValue` of `.+` rather than `.*` — a missing label reads as the empty string
in PromQL, and `.*` would quietly match those series too.

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

### Traces

Spans follow the same env-var-only contract as metrics, over the same endpoint.

- `OTEL_TRACES_EXPORTER` is `otlp` or `none`; unset with
  `OTEL_EXPORTER_OTLP_ENDPOINT` set means `otlp`. `OTEL_TRACES_SAMPLER` defaults
  to `parentbased_traceidratio` and `OTEL_TRACES_SAMPLER_ARG` is `1.0` in
  development and `0.1` on the production overlay, so a production trace is
  sampled at the root and every service in the request keeps the same decision.
- Transport is OTLP/HTTP-protobuf to `http://otel-collector:4318`, shared with
  metrics, through a `BatchSpanProcessor`. A no-op `TracerProvider` is installed
  when tracing is disabled. Tracing never fails startup and never raises into
  application code. One-shot processes `force_flush` and `shutdown` the tracer
  provider on exit, next to the meter provider.
- Context propagates as W3C TraceContext. HTTP propagation comes from the
  `fastapi` and `httpx` instrumentors. Across RabbitMQ, the producer span
  injects `traceparent` and `tracestate` into the message headers and the
  consumer span extracts them, so a trace spans the queue.

Span names are low-cardinality, exactly like metric names, because each distinct
name is a distinct span-metric series:

| Kind of work | Span name | Span kind |
| --- | --- | --- |
| HTTP server and client | route-templated, from the instrumentor | `SERVER`, `CLIENT` |
| Database call | `{db.operation.name} {db.system.name}` | `CLIENT` |
| Publish to a queue | `publish {messaging.destination.name}` | `PRODUCER` |
| Consume from a queue | `process {messaging.destination.name}` | `CONSUMER` |
| Batch flush | `flush {store} {entity}` | `INTERNAL` |
| Extractor file | `extract {source} {entity}`, with `download` and `parse` children | `INTERNAL` |
| Domain roots | `insights {computation}`, `mcp.tool {tool}`, `schema_init {store}`, `console.poll {target}`, `api.sync` | `INTERNAL` |

A consumer span is a child of the context extracted from the message, not of the
flush that follows it; a batch flush instead carries span **links** to at most
64 member message spans, which is what keeps a 5000-message flush from
producing a 5000-parent span.

Span attributes use the same closed sets as the metric catalog: never ids, file
names, or free text. Database spans carry `db.system.name` and
`db.operation.name` and never a statement. Messaging spans carry
`messaging.system=rabbitmq`, `messaging.destination.name`, and
`messaging.operation.name`. No span event carries a payload. An error sets the
span status to `ERROR` with `error.type` and nothing else.

Nothing queries VictoriaTraces for a span an operator has not asked for: the
RED numbers on the Traces dashboard come from the span metrics in the catalog
below, and the trace store is read only by the search panel and by the Explore
links out of those panels.

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

### Runtime metrics

Process and language-runtime health for every service, so a consumer that is
slowly leaking memory or a Rust service whose tokio queue is backing up is
visible before it fails. No service emits `system.*` host metrics: the host is
node-exporter's job.

The Python names come from `opentelemetry-instrumentation-system-metrics`,
which `groovemap-runtime` installs with the process-scoped subset only.
`docs/runtime.md` in [`python-libraries`](https://github.com/groovemap-music/python-libraries)
is the source of truth for exactly which instruments the pinned version emits;
the rows below were read from version `0.65b0` of that package and are
reconciled against a running stack by the end-to-end verification.

| OTEL name | Prometheus name | Kind | Attributes |
| --- | --- | --- | --- |
| `process.cpu.time` | `process_cpu_time_seconds_total` | counter (s) | `type` (`user`\|`system`) on Python, `cpu.mode` (`user`\|`system`) on Rust |
| `process.cpu.utilization` | `process_cpu_utilization_ratio` | gauge (ratio 0..1) | — |
| `process.memory.usage` | `process_memory_usage_bytes` | gauge (By, resident) | — |
| `process.memory.virtual` | `process_memory_virtual_bytes` | gauge (By) | — |
| `process.thread.count` | `process_thread_count` | gauge | — |
| `process.open_file_descriptor.count` | `process_open_file_descriptor_count` | gauge | — |
| `process.context_switches` | `process_context_switches_total` | counter | `type` (`voluntary`\|`involuntary`) |
| `cpython.gc.collections` | `cpython_gc_collections_total` | counter (`{collection}`) | `generation` and `cpython.gc.generation`, both (`0`\|`1`\|`2`) |
| `groovemap.runtime.event_loop.lag` | `groovemap_runtime_event_loop_lag_seconds` | histogram (s) | — |
| `groovemap.runtime.tokio.workers` | `groovemap_runtime_tokio_workers` | gauge | — |
| `groovemap.runtime.tokio.alive_tasks` | `groovemap_runtime_tokio_alive_tasks` | gauge | — |
| `groovemap.runtime.tokio.global_queue_depth` | `groovemap_runtime_tokio_global_queue_depth` | gauge | — |

Which services emit which:

- Every Python service emits the `process.*` rows and `cpython.gc.collections`.
  Every Python service with a running event loop also emits
  `groovemap.runtime.event_loop.lag`, sampled once a second by the background
  task `common.telemetry.start_event_loop_monitor()` starts.
- Rust services emit `process.cpu.time`, `process.memory.usage`,
  `process.thread.count`, and `process.open_file_descriptor.count` by reading
  `/proc/self` — silently absent off Linux — plus the three
  `groovemap.runtime.tokio.*` gauges from `tokio::runtime::Handle::metrics()`.
  They do not emit `process.cpu.utilization`, `process.context_switches`, or
  `process.memory.virtual`, so the utilisation panel derives cores-in-use from
  `rate(process_cpu_time_seconds_total[...])` for them.

Two names in the table are version-sensitive and are the first things to check
if a Runtime panel is empty. `cpython.gc.collections` replaced the older
`process.runtime.cpython.gc_count` instrument, which reaches Prometheus as
`process_runtime_cpython_gc_count_bytes_total` (the `By` unit on a collection
count is a bug in the old instrument, not a typo here). And `process.cpu.time`
carries `type` in the instrumentor and `cpu.mode` in current semantic
conventions; the panels deliberately sum over it rather than filter on it, so
the discrepancy costs a legend and nothing else.

### Neo4j metrics

Neo4j Community Edition has no Prometheus endpoint, so `operations-console`
(`dashboard`) emits these observable gauges on its behalf, refreshed at most
once per export interval. Every underlying query is bounded — the count store
or a `SHOW`/`CALL` command — so scraping the graph never costs a scan.

| OTEL name | Prometheus name | Kind | Attributes |
| --- | --- | --- | --- |
| `groovemap.neo4j.up` | `groovemap_neo4j_up` | gauge (0 unreachable, 1 reachable) | — |
| `groovemap.neo4j.nodes` | `groovemap_neo4j_nodes` | gauge | `label` |
| `groovemap.neo4j.relationships` | `groovemap_neo4j_relationships` | gauge | `type` |
| `groovemap.neo4j.transactions.active` | `groovemap_neo4j_transactions_active` | gauge | — |
| `groovemap.neo4j.store.size.bytes` | `groovemap_neo4j_store_size_bytes` | gauge (By) | `store` |

The attribute sets are closed: `operations-console` iterates a fixed list, so a
label or relationship type nobody declared cannot appear as a new series.

- `label` is one of `Artist`, `Genre`, `Label`, `Master`, `MediaFamily`,
  `Medium`, `Person`, `Release`, `Style`, `User` — the ten labels
  `database-schema` puts a uniqueness constraint on.
- `type` is one of the relationship types
  [`docs/architecture.md`](architecture.md) inventories: `BY`, `ON`, `IS`,
  `DERIVED_FROM`, `MEMBER_OF`, `ALIAS_OF`, `SUBLABEL_OF`, `PART_OF`,
  `CREDITED_ON`, `SAME_AS`, `COLLECTED`, `WANTS`, `IN_FAMILY`, `ISSUED_ON`, and
  the MusicBrainz enrichment edges `COLLABORATED_WITH`, `TAUGHT`, `TRIBUTE_TO`,
  `FOUNDED`, `SUPPORTED`, `SUBGROUP_OF`, `RENAMED_TO`. That document owns the
  list; this one does not restate it as a second source of truth.
- `store` is the store name `CALL dbms.queryJmx('org.neo4j:instance=kernel#0,name=Store sizes')`
  reports. That procedure is not available on every Community build; when it
  does not answer, the series is **omitted** rather than reported as zero, so an
  empty store-size panel means "not measurable here", not "no data".

Neo4j *latency* is not in this section: it is already covered by
`db_client_operation_duration_seconds{db_system_name="neo4j"}`, emitted by every
service that talks to the graph.

### Span metrics

RED metrics derived from spans by the collector's `spanmetrics` connector.
**No service emits these** — they exist for every instrumented operation the
moment its spans arrive, which is why they cover operations no hand-written
metric does.

| OTEL name | Prometheus name | Kind | Attributes |
| --- | --- | --- | --- |
| `traces.span.metrics.calls` | `traces_span_metrics_calls_total` | counter | `service.name`, `span.name`, `span.kind`, `status.code` |
| `traces.span.metrics.duration` | `traces_span_metrics_duration_seconds` | histogram (s) | as above |

As Prometheus labels those are `service_name`, `span_name`, `span_kind`, and
`status_code`. `span_kind` is an OTLP enum name (`SPAN_KIND_SERVER`,
`SPAN_KIND_CLIENT`, `SPAN_KIND_PRODUCER`, `SPAN_KIND_CONSUMER`,
`SPAN_KIND_INTERNAL`) and `status_code` likewise (`STATUS_CODE_UNSET`,
`STATUS_CODE_OK`, `STATUS_CODE_ERROR`) — an error ratio therefore filters on
`status_code="STATUS_CODE_ERROR"`, not on `5..`.

The connector's default `collector.instance.id` dimension is excluded in
`config/otel-collector.yaml`: there is one collector in this stack, so it would
only add a constant label to every series.

### Metrics that are deliberately absent

There is no Neo4j exporter in this stack, because Neo4j Community Edition has no
Prometheus endpoint to scrape. Graph health is observed from the application
side instead: the `groovemap.neo4j.*` gauges above, the
`db.client.operation.duration` series, and the container healthcheck.

No service emits `system.*` host metrics. The host is measured once, by
node-exporter, rather than once per container by every service on it.

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
| Runtime | `groovemap-runtime` | Per-service CPU, resident and virtual memory, threads, open file descriptors, GC collections, event-loop lag, and the tokio task and queue gauges |
| Neo4j | `groovemap-neo4j` | Graph reachability, nodes by label, relationships by type, active transactions, store sizes, and client operation latency by service |
| Containers & host | `groovemap-containers` | Per-container CPU, memory working set against limit, network, block I/O, restarts and OOM kills, plus host CPU, memory, load, disk, and filesystems |
| Traces | `groovemap-traces` | RED per service and span name from the span metrics, and a TraceQL search over VictoriaTraces |

Nine dashboards, one page each. The uids are stable and part of the contract: links and bookmarks depend on
them, so rename a title freely but never a uid.

### Provisioning

| File | Role |
| --- | --- |
| `config/grafana/provisioning/datasources/prometheus.yaml` | The Prometheus datasource (uid `prometheus`, VictoriaMetrics behind it) and the Tempo datasource (uid `tempo`, VictoriaTraces behind it), neither editable in the UI |
| `config/grafana/provisioning/dashboards/groovemap.yaml` | Loads `/var/lib/grafana/dashboards` into the `GrooveMap` folder |

Every panel resolves its datasource through the `${DS_PROMETHEUS}` template
variable rather than naming a uid. A dashboard that pins a literal uid works on
the machine it was exported from and nowhere else, so the checker rejects one.

`${DS_TEMPO}` is the one other variable a panel may resolve, and only a panel
that renders traces — a `table`, `traces`, or `nodeGraph` panel. The trace store
answers TraceQL, not PromQL, so a timeseries panel pointed at it draws an empty
graph rather than raising an error; the checker refuses that pairing, and
refuses the raw `tempo` uid exactly as it refuses the raw `prometheus` one.

### Adding a dashboard

1. Add a JSON file to `config/grafana/dashboards`. Give it a `uid` that starts
   with `groovemap-`, a unique `title`, and a `schemaVersion`.
2. Define a `DS_PROMETHEUS` datasource template variable, and set every panel's
   and every target's datasource to `{"type": "prometheus", "uid": "${DS_PROMETHEUS}"}`.
   A dashboard with a trace panel additionally defines `DS_TEMPO` and sets that
   panel's datasource to `{"type": "tempo", "uid": "${DS_TEMPO}"}`.
3. Add a `service`, `source`, or `queue` template variable so the dashboard can
   be narrowed to one thing.
4. Write PromQL against the **Prometheus** names in the catalog above, not the
   OTEL dot-names. Prefer `rate()` and `histogram_quantile()` over recording
   rules; this stack has no recording or alerting rules.
5. Run `uv run python scripts/check-dashboards.py`. It is part of
   `just source-check`, so `just check` runs it too.

The checker fails the build when a dashboard does not parse, is missing a `uid`,
`title`, or `schemaVersion`, duplicates another dashboard's uid or title, pins a
datasource uid instead of using the variable, reaches for `${DS_TEMPO}` from a
panel that cannot render a trace, has no queries, or references a
metric that is in neither the catalog above nor its exporter/collector
allowlist. It lints the provisioned alert rules the same way, against the same
catalog, once `config/grafana/provisioning/alerting/groovemap.yaml` exists. Adding a metric to a service means adding it to the catalog first —
that is the ordering the gate enforces.

If a panel needs a new exporter or collector metric, add the exact name to
`EXPORTER_METRICS` in `scripts/check-dashboards.py`. The allowlist is explicit
rather than prefix-matched so a typo in `rabbitmq_queue_messages_ready` still
fails.

## Alerts

Alert rules are code too. `config/grafana/provisioning/alerting/groovemap.yaml`
provisions them into the `GrooveMap` folder as the evaluation group `groovemap`,
evaluated every `1m`, from the same `config/grafana/provisioning` bind mount
that carries the datasources and the dashboard provider. A rule is a catalogued
query with a threshold on it, so `scripts/check-dashboards.py` lints every
expression against the catalog above exactly as it lints a panel, and
additionally requires each rule to carry a `summary`, a `description` that
states its threshold, a `severity` label, a unique Grafana-legal `uid`, and a
`condition` that names one of its own refIds.

Every rule has the same two-node shape: refId `A` is an instant PromQL query
against the Prometheus datasource, refId `C` is Grafana's built-in `threshold`
expression on `A`, and `condition: C`. Keeping the threshold in the expression
node rather than baking it into the PromQL is what lets the table below quote a
single number per rule.

| Rule | Expression | Threshold | Severity |
| --- | --- | --- | --- |
| `ScrapeTargetDown` | `up` | `< 1` for 5m | critical |
| `CollectorDroppingPoints` | `sum(rate(otelcol_exporter_send_failed_metric_points_total[15m]))` | `> 0` for 10m | critical |
| `ExtractorErrorRateHigh` | failed share of `groovemap_extraction_files_total` per `source`, 30m | `> 10%` for 15m | warning |
| `ConsumerFailureRateHigh` | failed share of `groovemap_pipeline_messages_total` per `service_name`/`source`, 15m | `> 5%` for 10m | critical |
| `CircuitBreakerOpen` | `max by (service_name, system) (groovemap_pipeline_circuit_breaker_state)` | `> 1` (open) for 2m | critical |
| `QueueBacklogGrowing` | `rabbitmq_queue_messages_ready` per `queue`, only while its 30m `delta` is positive | `> 50000` for 30m | warning |
| `ConsumersAbsent` | `sum by (source) (groovemap_pipeline_consumers_active)` | `< 1` for 10m | critical |
| `InsightsStale` | `time() - max by (computation) (groovemap_insights_last_success_seconds)` | `> 86400s` (24h) for 15m | warning |
| `ApiErrorRateHigh` | 5xx share of `http_server_request_duration_seconds_count` per `service_name`, 10m | `> 5%` for 10m | critical |
| `ApiLatencyHigh` | p95 of `http_server_request_duration_seconds_bucket` per `service_name`, 10m | `> 2s` for 10m | warning |
| `PostgresConnectionsNearMax` | `sum(pg_stat_database_numbackends) / max(pg_settings_max_connections)` | `> 80%` for 10m | warning |
| `RedisMemoryHigh` | `redis_memory_used_bytes / redis_memory_max_bytes` | `> 85%` for 15m | warning |
| `RabbitMqMemoryAlarm` | `rabbitmq_process_resident_memory_bytes / rabbitmq_resident_memory_limit_bytes` | `> 90%` for 5m | critical |
| `Neo4jDown` | `min(groovemap_neo4j_up)` | `< 1` for 5m | critical |
| `EventLoopLagHigh` | p99 of `groovemap_runtime_event_loop_lag_seconds_bucket` per `service_name`, 10m | `> 1s` for 10m | warning |
| `ContainerMemoryNearLimit` | `container_memory_working_set_bytes / container_spec_memory_limit_bytes` per compose service, limited containers only | `> 90%` for 10m | warning |
| `HostDiskLow` | `node_filesystem_avail_bytes / node_filesystem_size_bytes`, excluding tmpfs, overlay, and squashfs | `< 10%` free for 15m | warning |

`severity` is `critical` when the pipeline has stopped moving data or is losing
it, and `warning` when a resource is heading somewhere bad but nothing is lost
yet.

### Missing telemetry is reported by three canaries

Grafana's default `noDataState` is `NoData`, which raises a `DatasourceNoData`
alert whenever a rule's query matches nothing. Left on the default, a stack that
is starting up — or one whose extractors have not run yet — raises one of those
for nearly every rule at once, and the operator has to read eight alerts to
learn one fact.

So exactly three rules keep a no-data behaviour, one per source of telemetry:

| Rule | `noDataState` | Speaks for |
| --- | --- | --- |
| `ScrapeTargetDown` | `NoData` | The scrape jobs: rabbitmq, postgres, redis, cadvisor, node-exporter, and the collector's own `:8888`. `up` returning nothing means there is no scrape data at all. |
| `ConsumersAbsent` | `Alerting` | The services that push OTLP. A consumer gauge that stopped arriving is the condition this rule exists to catch, so absence is the alert rather than a separate one. |
| `Neo4jDown` | `NoData` | The graph probe `operations-console` pushes. It is not a scrape target, so `ScrapeTargetDown` never speaks for it. |

Every other rule is `noDataState: OK`. Each one is a threshold on a series that
one of those three already covers, so it sees no data only when its canary is
already firing; staying quiet keeps it from saying the same thing twice. The
ratio and quantile rules additionally resolve to no data whenever there is no
traffic, which is a healthy idle stack, not a fire.

Every rule uses `execErrState: Error`, so a query that genuinely fails is
distinguishable from one that matched nothing.

### Where the alerts go

Nowhere. **No contact point and no notification policy are provisioned**, so a
firing rule appears in Grafana under **Alerting → Alert rules** and in the alert
list on the GrooveMap folder, and nothing is emailed, posted, or paged. That is
deliberate: routing an alert somewhere commits a human to answering it, and this
stack is a local Compose environment whose owner is already looking at it.

To route them when someone is ready to be woken up, add a
`config/grafana/provisioning/alerting/contact-points.yaml` with a
`contactPoints:` block and a `policies:` block naming it as the default
receiver:

```yaml
---
apiVersion: 1

contactPoints:
  - orgId: 1
    name: groovemap-oncall
    receivers:
      - uid: groovemap-oncall-slack
        type: slack
        settings:
          url: $SLACK_WEBHOOK_URL

policies:
  - orgId: 1
    receiver: groovemap-oncall
    group_by: [alertname, severity]
    routes:
      - receiver: groovemap-oncall
        matchers:
          - severity = critical
```

The secret belongs in a Docker secret or an environment variable Grafana
expands, never in the file — `gitleaks` runs over this directory in
`just source-check`. `scripts/check-dashboards.py` asserts today that no contact
point or notification policy is provisioned, so adding one is a deliberate edit
to the gate and its test, not an accident.

### Why an alert rule names `prometheus` and a dashboard does not

A dashboard panel must use the `${DS_PROMETHEUS}` template variable: it is
resolved in the browser against whatever Grafana served the page, so a pinned
uid works on one machine and nowhere else. An alert rule has no browser and no
dashboard. The Grafana scheduler evaluates it server-side and resolves
`datasourceUid` against the provisioned datasource list, where a `${...}` string
is not a variable but an unresolvable uid. So a rule names `prometheus`
directly, which is safe precisely because provisioning pins that uid, and the
lint gate enforces the split: the raw uid is accepted only in the alerting file,
the template variable is rejected there, and both rules are reversed for
dashboards.

### Adding an alert rule

1. Add a rule to the `groovemap` group in
   `config/grafana/provisioning/alerting/groovemap.yaml`. Give it a `uid` that
   starts with `groovemap-`, at most 40 characters of letters, digits, `-`, and
   `_`; a `title` in PascalCase matching the rule names above; and a `for`
   duration long enough that a single scrape gap cannot fire it.
2. Write refId `A` as an instant query (`instant: true`, `range: false`) whose
   PromQL uses only catalogued metric names, and refId `C` as a `threshold`
   expression on `A`. Point `condition` at `C`.
3. Write a `summary` that names what broke and a `description` that begins
   `Threshold:` and states the number, because that description is the whole of
   what an operator sees. Add a `severity` label of `critical` or `warning`.
4. Add a row to the table above and to `EXPECTED_ALERT_SEVERITIES` in
   `tests/deploy/test_dashboards.py`; the rule set is closed on both sides.
5. Run `uv run python scripts/check-dashboards.py`, then `just check`.


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

`--wait` blocks on container health. On the wave-2 images it converges: the
Python service images carry a `python -c urlopen` healthcheck rather than one
that shells out to `curl`, and both extractor images do ship `curl`, which their
compose healthcheck still invokes. That was not true of the wave-1 images, whose
Python healthchecks called a `curl` the image did not contain, so `--wait` hung
on a stack where every service was in fact running and exporting.

A hang is therefore now a real failure rather than a known quirk — but read the
container's own healthcheck before concluding anything, because a service can be
exporting perfectly while its probe is misconfigured:

```bash
docker compose up -d
docker compose ps --format '{{.Service}}\t{{.State}}\t{{.Status}}'
```

**Both extractors start downloading real monthly dumps the moment they start.**
Neither takes a fixture or a size limit: `discogs-ingestion` scrapes the Discogs
dump index, and `musicbrainz-ingestion` its own, then pulls the whole set before
parsing any of it. That is tens of gigabytes, at whatever rate the link allows,
onto the Docker VM's data filesystem. It is what filled the volume and corrupted
the VM during the first execution of this runbook.

Watch the free space for as long as they run, and stop them as soon as the
evidence you came for is in:

```bash
docker run --rm --privileged -v /var/lib/docker:/d:ro alpine df -h /d
docker compose stop extractor-discogs extractor-musicbrainz
```

The extractors are worth starting anyway: they are the only source of the
`groovemap_runtime_tokio_*` gauges, of the Rust `process.*` rows, and of a real
`publish` span. A couple of minutes of running is enough for all three, and the
first entity's records begin publishing as soon as the last file has downloaded.

### 3. Confirm the collector is receiving data points

The collector's self-metrics on `:8888` are the first place a break shows.
`accepted` climbing means services are pushing; `refused` climbing means the
collector is rejecting them. The port is not published, so read it from another
container on the `groovemap` network:

```bash
docker compose exec victoria-metrics \
  wget -qO- http://otel-collector:8888/metrics | grep -E '^otelcol_(receiver|exporter)_'
```

Expect non-zero `otelcol_receiver_accepted_metric_points` for both the `otlp`
receiver (the application push path) and the `prometheus` receiver (the
infrastructure scrape path), a matching `otelcol_exporter_sent_metric_points`
for `prometheusremotewrite`, and zero on the `refused` and `failed` counters.
Once services push spans, `otelcol_receiver_accepted_spans` climbs on the `otlp`
receiver and `otelcol_exporter_sent_spans` on `otlphttp/victoria_traces`.

The names on `:8888` carry **no** `_total` suffix. The suffix is added by the
remote-write translation, so the same counters are
`otelcol_receiver_accepted_metric_points_total` and
`otelcol_exporter_sent_metric_points_total` once they reach VictoriaMetrics —
which is the form the Infrastructure dashboard and the `CollectorDroppingPoints`
rule query, and the form the catalog allowlist names. Grepping `:8888` for the
suffixed name finds nothing and looks exactly like a dead collector.

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

# Runtime — resident memory per service
curl -sG 'http://localhost:8428/api/v1/query' \
  --data-urlencode 'query=sum by (service_name) (process_memory_usage_bytes)'

# Neo4j — node count per label, emitted by operations-console
curl -sG 'http://localhost:8428/api/v1/query' \
  --data-urlencode 'query=sum by (label) (groovemap_neo4j_nodes)'

# Containers & host — CPU seconds per second, per compose service
curl -sG 'http://localhost:8428/api/v1/query' \
  --data-urlencode 'query=sum by (container_label_com_docker_compose_service) (rate(container_cpu_usage_seconds_total[5m]))'

# Traces — span rate per service, derived by the spanmetrics connector
curl -sG 'http://localhost:8428/api/v1/query' \
  --data-urlencode 'query=sum by (service_name) (rate(traces_span_metrics_calls_total[5m]))'
```

`tests/deploy/test_observability_runbook.py` pins these queries against the
catalog above, so a metric renamed in the catalog fails the build here rather
than silently emptying a panel.

Panels stay empty when the workload that feeds them has not run, and that is
not a fault. A stack that has only just started, with no dump files to extract
and no messages in flight, leaves the pipeline and extraction panels blank
while the infrastructure, API, and schema panels fill immediately. Drive the
pipeline before concluding a panel is broken.

### 6. Confirm the wave-2 series in detail

Step 5 proves each dashboard's headline panel can fill. These queries prove the
series wave 2 added are present with the labels the catalog promises, which is
what tells a runtime, Neo4j, span, container, or host panel apart from a panel
whose metric never arrived.

Runtime. The `process.*` instruments come from
`opentelemetry-instrumentation-system-metrics` on Python and from `/proc/self`
on Rust, so the first four answer for every service and the rest split by
language:

```bash
# Every service — CPU seconds, resident and virtual memory, threads, descriptors
curl -sG 'http://localhost:8428/api/v1/query' \
  --data-urlencode 'query=sum by (service_name, type) (rate(process_cpu_time_seconds_total[5m]))'
curl -sG 'http://localhost:8428/api/v1/query' \
  --data-urlencode 'query=count by (service_name) (process_memory_virtual_bytes)'
curl -sG 'http://localhost:8428/api/v1/query' \
  --data-urlencode 'query=sum by (service_name) (process_thread_count)'
curl -sG 'http://localhost:8428/api/v1/query' \
  --data-urlencode 'query=sum by (service_name) (process_open_file_descriptor_count)'

# Python only — utilisation ratio, context switches, GC, and event-loop lag
curl -sG 'http://localhost:8428/api/v1/query' \
  --data-urlencode 'query=sum by (service_name) (process_cpu_utilization_ratio)'
curl -sG 'http://localhost:8428/api/v1/query' \
  --data-urlencode 'query=sum by (service_name, type) (rate(process_context_switches_total[5m]))'
curl -sG 'http://localhost:8428/api/v1/query' \
  --data-urlencode 'query=sum by (service_name, cpython_gc_generation) (rate(cpython_gc_collections_total[5m]))'
curl -sG 'http://localhost:8428/api/v1/query' \
  --data-urlencode 'query=histogram_quantile(0.95, sum by (le, service_name) (rate(groovemap_runtime_event_loop_lag_seconds_bucket[5m])))'

# Rust only — the three tokio gauges, one series per extractor
curl -sG 'http://localhost:8428/api/v1/query' \
  --data-urlencode 'query=sum by (service_name) (groovemap_runtime_tokio_workers)'
curl -sG 'http://localhost:8428/api/v1/query' \
  --data-urlencode 'query=sum by (service_name) (groovemap_runtime_tokio_alive_tasks)'
curl -sG 'http://localhost:8428/api/v1/query' \
  --data-urlencode 'query=sum by (service_name) (groovemap_runtime_tokio_global_queue_depth)'
```

`process_cpu_time_seconds_total` carries `type` on Python and `cpu_mode` on
Rust, which is why the query sums over the label rather than filtering on it. An
empty `cpython_gc_collections_total` with a populated `process_thread_count`
means the service is Rust, not that the instrument broke.

Neo4j. All five gauges come from `operations-console` (`dashboard`); an empty
result for every one of them means that service is not emitting, not that the
graph is down:

```bash
curl -sG 'http://localhost:8428/api/v1/query' \
  --data-urlencode 'query=groovemap_neo4j_up'
curl -sG 'http://localhost:8428/api/v1/query' \
  --data-urlencode 'query=groovemap_neo4j_transactions_active'
curl -sG 'http://localhost:8428/api/v1/query' \
  --data-urlencode 'query=sum by (type) (groovemap_neo4j_relationships)'
curl -sG 'http://localhost:8428/api/v1/query' \
  --data-urlencode 'query=sum by (store) (groovemap_neo4j_store_size_bytes)'
```

`groovemap_neo4j_store_size_bytes` is the one gauge whose absence is not a
fault: `dbms.queryJmx` does not answer on every Community build, and the
convention is to omit the series rather than report a zero.

Span metrics. Nothing emits these — the collector's `spanmetrics` connector
derives them from arriving spans, so a non-empty result is proof the traces
pipeline is carrying data, independent of the trace store:

```bash
curl -sG 'http://localhost:8428/api/v1/query' \
  --data-urlencode 'query=sum by (span_kind) (rate(traces_span_metrics_calls_total[5m]))'
curl -sG 'http://localhost:8428/api/v1/query' \
  --data-urlencode 'query=histogram_quantile(0.95, sum by (le, span_name) (rate(traces_span_metrics_duration_seconds_bucket[5m])))'
```

`span_kind` should show `SPAN_KIND_SERVER`, `SPAN_KIND_CLIENT`,
`SPAN_KIND_PRODUCER`, `SPAN_KIND_CONSUMER`, and `SPAN_KIND_INTERNAL` — the OTLP
enum names, not the short forms.

Containers and host, from the two scrape jobs step 4 added:

```bash
# One row per compose service, from the cadvisor container label
curl -sG 'http://localhost:8428/api/v1/query' \
  --data-urlencode 'query=count by (container_label_com_docker_compose_service) (container_last_seen)'
curl -sG 'http://localhost:8428/api/v1/query' \
  --data-urlencode 'query=sum by (container_label_com_docker_compose_service) (container_memory_working_set_bytes)'

# The host node-exporter measures: on Docker Desktop and Colima this is the VM
curl -sG 'http://localhost:8428/api/v1/query' \
  --data-urlencode 'query=node_load1'
curl -sG 'http://localhost:8428/api/v1/query' \
  --data-urlencode 'query=1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes'
curl -sG 'http://localhost:8428/api/v1/query' \
  --data-urlencode 'query=sum by (mode) (rate(node_cpu_seconds_total[5m]))'
curl -sG 'http://localhost:8428/api/v1/query' \
  --data-urlencode 'query=1 - node_filesystem_avail_bytes / node_filesystem_size_bytes'
```

Scrape targets. `up` is synthesised by the collector's Prometheus receiver, one
series per job, and survives remote write into VictoriaMetrics. Every canary in
the alert rules rests on that, so confirm it directly rather than inferring it
from the exporters' own series:

```bash
curl -sG 'http://localhost:8428/api/v1/query' \
  --data-urlencode 'query=min by (job) (up)'
```

Expect one row per scrape job, each `1`: `cadvisor`, `node-exporter`,
`otelcol-contrib`, `postgres-exporter`, `rabbitmq`, `redis-exporter`. The
collector's own job is labelled `otelcol-contrib` for the reason step 4 gives.

### 7. Confirm one trace spans a publish and a consume

The traces pipeline is only proved end to end when a producer's span and a
consumer's span are in the *same* trace, because that is what the `traceparent`
header carried over AMQP is for. Everything before this step would still pass if
propagation were broken and each service simply started its own trace.

The trace store answers TraceQL rather than PromQL, so this query goes to
VictoriaTraces' Tempo API and not to VictoriaMetrics:

```bash
# Find a trace that contains an extractor's publish span
curl -sG 'http://localhost:10428/select/tempo/api/search' \
  --data-urlencode 'q={name=~"publish .*"}' \
  --data-urlencode 'limit=5'

# Then read the whole trace and list its spans by service and kind
TRACE_ID=<traceID from the search above>
curl -s "http://localhost:10428/select/tempo/api/traces/${TRACE_ID}" \
  | python3 -c '
import collections, json, sys
counts = collections.Counter()
for batch in json.load(sys.stdin)["batches"]:
    service = {a["key"]: list(a["value"].values())[0] for a in batch["resource"]["attributes"]}["service.name"]
    for scope in batch["scopeSpans"]:
        for span in scope["spans"]:
            counts[(service, span["kind"], span["name"])] += 1
for (service, kind, name), n in sorted(counts.items(), key=lambda kv: -kv[1]):
    print("%6d  %-24s %-20s %s" % (n, service, kind, name))
'
```

The spans are counted rather than listed because a real trace is large: the
extractor opens one `extract {source} {entity}` root span per **file**, so every
publish and every downstream consume for that whole file hangs off one root. A
partial artists file produced a trace of 69,760 spans during the 2026-09-05 run.
`kind` comes back as the OTLP enum name (`SPAN_KIND_PRODUCER`), the same string
the span metrics carry, not as the integer the OTLP wire format uses.

A pass is one trace holding a `publish {exchange}` span of kind `PRODUCER` under
an extractor's `service.name`, and at least one `process {queue}` span of kind
`CONSUMER` under a loader's or enricher's. The database `CLIENT` spans and the
`flush {store} {entity}` `INTERNAL` span the consumer opens around its batch
write come along in the same trace, which is the point of the whole exercise.

Two syntax details cost time. VictoriaTraces accepts `{kind=producer}` but not
Tempo's `{span:kind=producer}`, so use the short form. And a `=~` matcher takes
an RE2 regular expression: Grafana's *default* interpolation of a multi-value
dashboard variable is a glob (`{api,insights}`), which RE2 reads as a literal and
which therefore matches nothing. That is why the Traces dashboard's search panel
interpolates `${service:regex}` rather than `$service` — the `regex` format
produces `(api|insights)`, which does match.

### 8. Confirm the alert rules are evaluating

Rules are provisioned read-only from
`config/grafana/provisioning/alerting/groovemap.yaml`. Grafana provisions the
whole file or none of it, so a single malformed rule is a silent loss of all
seventeen:

```bash
curl -s 'http://localhost:3000/api/v1/provisioning/alert-rules' \
  | python3 -c 'import json,sys; print(len(json.load(sys.stdin)), "rules provisioned")'

curl -s 'http://localhost:3000/api/prometheus/grafana/api/v1/rules' \
  | python3 -c '
import json, sys
for group in json.load(sys.stdin)["data"]["groups"]:
    for rule in group["rules"]:
        print("%-32s %-9s health=%s" % (rule["name"], rule["state"], rule["health"]))
'
```

Expect seventeen rules in folder `GrooveMap`, group `groovemap`. `state` is
`inactive` for a healthy stack, `pending` once a condition has held for less than
its `for` duration, and `alerting` after. `health` is `ok` for a rule that
evaluated, `nodata` for one whose query returned nothing — which is the correct
state for `Neo4jDown` and `ScrapeTargetDown` until their series exist, and is why
those two are the rules that declare `noDataState: NoData`.

### 9. Confirm Grafana has all nine dashboards

Development enables anonymous `Viewer` access, so `http://localhost:3000` opens
the dashboards in a browser without a login. The search API is the scriptable
form of the same check:

```bash
curl -s 'http://localhost:3000/api/search?type=dash-db' \
  | python3 -c 'import json,sys; [print(d["uid"], "|", d["title"]) for d in json.load(sys.stdin)]'
```

Expect exactly nine rows, one per uid: `groovemap-pipeline-overview`,
`groovemap-ingestion`, `groovemap-consumers`, `groovemap-api-services`,
`groovemap-infrastructure`, `groovemap-runtime`, `groovemap-neo4j`,
`groovemap-containers`, `groovemap-traces`.

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

### 10. Tear the stack down

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
that turns the partial evidence into a pass. The 2026-09-05 record below is that
re-run: it closes the Grafana listing, the clean pass, and `graphinator`, and it
leaves `mcp-server` open for the same reason.

### Second execution, 2026-09-05

Wave 2's run. It exercised the VictoriaMetrics backend, the VictoriaTraces
pipeline, the runtime, Neo4j, span, container, and host series, and the
seventeen provisioned alert rules, against images built from every service
repository's `main`. It also closes three of the four gaps the 2026-09-03 record
left open.

**Images.** Nothing wave 2 produced is published to GHCR yet, so every image was
built on the workstation from its repository's local `main` and tagged
`<name>:local`; a local, uncommitted `.env` pointed each compose image variable
at that tag. The Python images were built through each repository's
`just image`, whose `prepare-runtime-wheel.sh` step builds the
`groovemap-runtime` wheel from a clean checkout of `python-libraries` at the
revision that repository pins — `455523e` for every service except, before its
wave-2 molecule landed mid-run, `operations-console`. The two Rust images were
built by their own `just image`. The identifiers below are local image IDs, not
registry manifest digests: nothing was pushed, so no manifest digest exists.

| Repository | Commit | Local image ID | Compose service |
| --- | --- | --- | --- |
| `database-schema` | `91cbf6e` | `f09f251fa403` | `schema-init` |
| `catalog-api` | `acdf9be` | `43db9627e2b0` | `api` |
| `graph-explorer` | `9ef18c8` | `06b9263a6175` | `explore` |
| `analytics-engine` | `ec733f2` | `850ee3515037` | `insights` |
| `operations-console` | `b563f10` | `82783d1d7f4d` | `dashboard` |
| `discogs-sql-loader` | `c3fba8f` | `565a10972608` | `tableinator` |
| `musicbrainz-sql-loader` | `8b5f346` | `82e85eed5b07` | `brainztableinator` |
| `discogs-graph-enricher` | `ab23f7b` | `2c4617be0c4c` | `graphinator` |
| `musicbrainz-graph-enricher` | `c6e3514` | `76a54865f518` | `brainzgraphinator` |
| `discogs-ingestion` | `403e70f` | `cc4c6b9e429d` | `extractor-discogs` |
| `musicbrainz-ingestion` | `0aa08e2` | `76bdbd106a43` | `extractor-musicbrainz` |

Every third-party image was the digest-pinned reference already committed in
`docker-compose.yml`, unchanged for this run.

`operations-console` was rebuilt during the run. The stack first came up on
`b5e11d0`, which still pinned `python-libraries` at the pre-wave-2 revision
`41805b6` and emitted no Neo4j gauges, no runtime metrics, and no spans; the
Neo4j dashboard and `Neo4jDown` were unexercisable while it ran. Its wave-2
molecule landed on local `main` as `b563f10` partway through, and the numbers
below are from the rebuilt image. The earlier state is recorded because it is
exactly what a service that has not adopted looks like from the backend: present
and healthy, silent on every wave-2 series.

**`--wait` converges now.** `docker compose up -d --wait` returned `0` in fifteen
seconds with every service healthy. The wave-1 record's `curl` caveat no longer
applies: the Python images carry a `python -c urlopen` healthcheck, and both
extractor images ship the `curl` their compose healthcheck calls.

**`service_name` values observed.** Seventeen, from step 4 — the full roll call
for the first time. Pushed by services: `api`, `brainzgraphinator`,
`brainztableinator`, `dashboard`, `explore`, `extractor-discogs`,
`extractor-musicbrainz`, `graphinator`, `insights`, `schema-init`,
`tableinator` — all eleven wired services. From the scrape jobs: `cadvisor`,
`node-exporter`, `otelcol-contrib`, `postgres-exporter`, `rabbitmq`,
`redis-exporter`.

**`up` is queryable.** `min by (job) (up)` returned `1` for all six scrape jobs
(`cadvisor`, `node-exporter`, `otelcol-contrib`, `postgres-exporter`,
`rabbitmq`, `redis-exporter`). The collector's Prometheus receiver synthesises
the series and remote write carries it into VictoriaMetrics intact, which is
what `ScrapeTargetDown` rests on.

**Runtime series split by language exactly as the catalog says.** The four
`/proc`-derived rows — `process_cpu_time_seconds_total`,
`process_memory_usage_bytes`, `process_thread_count`,
`process_open_file_descriptor_count` — answered for all eleven services.
`cpython_gc_collections_total`, `process_cpu_utilization_ratio`,
`process_memory_virtual_bytes`, and `process_context_switches_total` answered for
the nine Python services only. `groovemap_runtime_event_loop_lag_seconds`
answered for eight: every Python service except `schema-init`, which is one-shot
and has no long-running loop to sample. The three `groovemap_runtime_tokio_*`
gauges answered for the two Rust extractors only.

`cpython_gc_collections_total` arrives carrying **both** `generation` and
`cpython_gc_generation`, not one or the other; the catalog row was corrected to
say so. `process_cpu_time_seconds_total` carries `type` on Python, as the catalog
already noted.

**Neo4j gauges.** `groovemap_neo4j_up`, `groovemap_neo4j_transactions_active`,
`groovemap_neo4j_nodes` (ten series, one per constrained label) and
`groovemap_neo4j_relationships` (twenty-one series, one per relationship type)
all arrived from `dashboard` once it ran the wave-2 image. The closed attribute
sets held: no label or type outside the declared lists appeared.
`groovemap_neo4j_store_size_bytes` was **absent**, which is the documented
correct behaviour — `dbms.queryJmx` does not answer on this Neo4j Community
build, and the series is omitted rather than reported as zero.

**Span metrics and the trace path.** The `spanmetrics` connector produced
`traces_span_metrics_calls_total` and `traces_span_metrics_duration_seconds` for
every instrumented service, with `span_kind` taking all five OTLP enum names —
`SPAN_KIND_INTERNAL`, `SPAN_KIND_CLIENT`, `SPAN_KIND_SERVER`,
`SPAN_KIND_CONSUMER`, `SPAN_KIND_PRODUCER` — and no short forms. The collector
accepted 142,949 spans on its `otlp` receiver with zero refused and zero failed,
and had sent 142,910 of them to `otlphttp/victoria_traces`; the difference was
the batch still in flight.

One trace, `53e1e0be1d203701398e8e7e678a65d4`, spans the whole pipeline:

| Spans | Service | Kind | Name |
| ---: | --- | --- | --- |
| 338 | `extractor-discogs` | `SPAN_KIND_PRODUCER` | `publish groovemap-discogs-artists` |
| 34,175 | `tableinator` | `SPAN_KIND_CONSUMER` | `process artists` |
| 34,175 | `graphinator` | `SPAN_KIND_CONSUMER` | `process discogs-graph-enricher-artists` |
| 450 | `graphinator` | `SPAN_KIND_INTERNAL` | `flush neo4j artist` |
| 450 | `graphinator` | `SPAN_KIND_CLIENT` | `session neo4j` |
| 86 | `tableinator` | `SPAN_KIND_INTERNAL` | `flush postgresql artists` |
| 86 | `tableinator` | `SPAN_KIND_CLIENT` | `session postgresql` |

That is the acceptance in one object: a real extractor `publish` PRODUCER span
and real consumer `process` CONSUMER spans in a single trace, which is only
possible if the `traceparent` header survives the AMQP hop. The database `CLIENT`
spans and the batch `flush` `INTERNAL` spans came along with it.

The trace is also 69,760 spans, from a partial artists file. The extractor opens
one `extract {source} {entity}` root span per **file**, and every publish and
every downstream consume hangs off that one root, so a completed releases file
would put tens of millions of spans under it. That is a design problem rather
than a verification failure, and it is recorded in the not-verified table below.

Two consumers name the same destination differently: `tableinator` opens
`process artists` while `graphinator` opens
`process discogs-graph-enricher-artists`. The convention is
`process {messaging.destination.name}`, so one of the two is naming its queue and
the other its entity. Also a follow-up, not a wave-2 regression.

**Panels with data**, from running every panel's queries against the run window:

| Dashboard | Panels returning series |
| --- | ---: |
| Runtime | 10 of 10 |
| Containers & host | 14 of 14 |
| Consumers | 10 of 10 |
| Neo4j | 9 of 10 |
| Infrastructure | 17 of 20 |
| Pipeline overview | 8 of 10 |
| API services | 8 of 16 |
| Traces | 6 of 7 |
| Ingestion | 5 of 8 |

The Traces dashboard's seventh panel is the TraceQL `Trace search` table, which
queries the Tempo datasource rather than VictoriaMetrics and so cannot be counted
by a PromQL sweep; it was confirmed separately against the Tempo API in step 7.

**Why each panel was empty.** Three different reasons, and only one of them is a
fault.

*The series exist but carry no sample inside the query window.* `Schema
initialisation duration`, `Insights computation duration p95`, and `Cache hit
ratio` all query series that are present in VictoriaMetrics —
`groovemap_schema_init_duration_seconds_bucket` has 48 series and
`groovemap_insights_computation_duration_seconds_bucket` has 112 — but the work
that produced them happened at start-up and a `rate()` over a later window is
empty. Widen the dashboard's time range and they fill. Their metric names are
right, which is exactly what distinguishes them from the group below.

*The workload never ran.* The API sync, NLQ, and explore-proxy panels need calls
nobody made — `groovemap_explore_proxy_duration_seconds` was never recorded at
all. The two MCP panels can never fill here because `mcp-server` is not a compose
service. The ingestion file-progress, files-by-outcome, and errors-by-stage
panels and the two pipeline failure panels need a completed file and a failure,
and this run stopped the extractors mid-file on purpose. `Store sizes` is the
documented Neo4j omission above.

*A real naming defect.* The three PostgreSQL panels on Infrastructure are not a
workload gap, and the wave-1 record's explanation for them — too few samples —
was wrong. Five metric
names those panels query do not exist in VictoriaMetrics under the name the panel
uses. `postgres-exporter` publishes `pg_stat_database_xact_commit`,
`pg_stat_database_xact_rollback`, `pg_stat_database_blks_hit`,
`pg_stat_database_blks_read`, and `pg_stat_database_deadlocks` with no suffix;
the collector's remote-write translation appends `_total` to each, so the series
land as `pg_stat_database_xact_commit_total` and so on. The panels ask for the
unsuffixed name and can therefore never fill. A sixth,
`rabbitmq_global_messages_published_total`, does not exist in RabbitMQ 4 at all —
the publish-side counter is now `rabbitmq_global_messages_received_total`. All
six are recorded as follow-ups; none is a wave-2 regression, and none was
changed here.

**Alert rules.** All seventeen provisioned into folder `GrooveMap`, group
`groovemap`, and all seventeen evaluated with `health=ok`:

| State | Rules |
| --- | --- |
| `firing` | `HostDiskLow` |
| `inactive` | the other sixteen |

`HostDiskLow` firing is real evidence rather than noise: the extractors' dump
downloads took the Docker VM's data filesystem past the rule's threshold, and the
rule walked `inactive` → `pending` → `firing` on its own. `Neo4jDown` sat at
`health=nodata` while `dashboard` ran the pre-wave-2 image and moved to
`health=ok`, state `inactive`, once the rebuilt image was emitting
`groovemap_neo4j_up` — which is the no-data design working as intended.
`ConsumersAbsent` never fired, because every consumer reported
`groovemap_pipeline_consumers_active`.

**Grafana.** `/api/search?type=dash-db` listed exactly nine dashboards, one per
expected uid, and `/api/datasources` listed both `prometheus` (VictoriaMetrics)
and `tempo` (VictoriaTraces). This is the wave-1 gap that the damaged volume
prevented; it is now closed.

**Not verified in this run.**

| Item | Why |
| --- | --- |
| `mcp-server` | Still not a compose service, so this stack cannot exercise it. Its two API-services panels and its `groovemap_mcp_*` series have never been observed anywhere. |
| `groovemap_neo4j_store_size_bytes` | `dbms.queryJmx('org.neo4j:instance=kernel#0,name=Store sizes')` does not answer on this Neo4j Community build, so the gauge is correctly omitted. The `Store sizes` panel has never been seen with data. |
| A completed extraction | Both extractors were stopped mid-file to protect the Docker VM's disk. Nothing here exercised a file-completion path: `groovemap_extraction_files_total`, `groovemap_extraction_errors_total`, the ingestion file-progress and errors-by-stage panels, and the two pipeline failure panels stayed empty. |
| The production overlay | Only the development stack was run. The loopback binds and the `0.1` trace sampler in `docker-compose.prod.yml` are covered by tests and `just config-prod`, not by an execution. |
| Published images | Nothing wave 2 built is on GHCR. This run proves the code, not a release; the digest table in `docs/maintenance.md` is unchanged and still records the wave-1 releases. |
| Grafana's rendering of any panel | Every panel was checked by issuing its query against the datasource, not by loading the dashboard in a browser. A panel whose query returns series can still render wrongly. |

**Follow-ups this run found.** None is a wave-2 regression, and none was changed
by the verification bead, which owns the runbook rather than the dashboards or
the services.

| Follow-up | Evidence |
| --- | --- |
| Five Infrastructure panel queries name `pg_stat_database_xact_commit`, `pg_stat_database_xact_rollback`, `pg_stat_database_blks_hit`, `pg_stat_database_blks_read`, and `pg_stat_database_deadlocks`; remote write stores them with a `_total` suffix. | Three PostgreSQL panels are permanently empty. The suffixed names are present in VictoriaMetrics and the unsuffixed ones are absent. |
| The Infrastructure RabbitMQ publish-rate query names `rabbitmq_global_messages_published_total`, which RabbitMQ 4 does not emit. | The broker's `:15692` endpoint publishes `rabbitmq_global_messages_received_total` and no `_published_` counter at all. |
| An extractor opens one `extract {source} {entity}` root span per file, so one file is one unbounded trace. | 69,760 spans in one trace from a partial artists file; a completed releases file would be orders of magnitude larger. |
| `tableinator` and `graphinator` disagree on the `process {destination}` span name. | `process artists` against `process discogs-graph-enricher-artists` in the same trace. |
| `mcp-server` has telemetry but no place to run it. | Two API-services panels and every `groovemap_mcp_*` series have never been observed on any stack. |
| Neither extractor can be run against a fixture. | Both download the full monthly dump set on start, with no size limit, no fixture mode, and no configurable source URL. Verifying a publish span costs tens of gigabytes. |


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
