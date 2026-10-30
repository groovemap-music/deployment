# Database Resilience Documentation

This document is for operators who need to know how GrooveMap services behave when PostgreSQL, Neo4j, or RabbitMQ
becomes unavailable, for example during nightly maintenance windows, and how to test that behavior.

## Overview

The Python services use the shared resilient connection classes from
[`python-libraries`](https://github.com/groovemap-music/python-libraries) (`common.db_resilience`,
`common.postgres_resilient`, `common.neo4j_resilient`, `common.rabbitmq_resilient`), which automatically handle:

- Nightly database maintenance windows
- Temporary network issues
- Database restarts
- Connection timeouts
- Service unavailability

## Key Features

### 1. Circuit Breaker Pattern

Each database connection uses a circuit breaker to prevent cascading failures:

- **Closed State**: Normal operation, all requests pass through
- **Open State**: After 3 consecutive failures (the PostgreSQL, Neo4j, and RabbitMQ connection classes; the shared
  default is 5), rejects requests immediately
- **Half-Open State**: After 30 seconds (the shared default is 60), allows one test request

```python
# Circuit breaker configuration used by the PostgreSQL, Neo4j, and RabbitMQ classes
failure_threshold: 3  # Number of failures before opening
recovery_timeout: 30  # Seconds before trying half-open
```

### 2. Exponential Backoff

Failed connections retry with exponential backoff:

```yaml
# Backoff configuration
initial_delay: 0.5-1.0    # Initial retry delay (seconds): 0.5 PostgreSQL, 1.0 Neo4j and RabbitMQ
max_delay: 30-60         # Maximum retry delay: 30 PostgreSQL and Neo4j, 60 RabbitMQ
exponential_base: 2.0    # Delay multiplier
jitter: 0-25%            # Random jitter to prevent thundering herd
```

### 3. Connection Health Monitoring

#### PostgreSQL

- `AsyncPostgreSQLPool` (class default 2-20 connections); each service passes its own budget-aware bounds, overridable
  with `POSTGRES_POOL_MIN_SIZE` / `POSTGRES_POOL_MAX_SIZE`: 2-12 for both SQL loaders, 2-8 for the API, 1-4 for Insights
- Health checks every 30 seconds
- Automatic removal of unhealthy connections
- Maintains minimum connection pool size

#### Neo4j

- Driver-level connection pooling (max 50 connections)
- Built-in keep-alive mechanism
- Session-level health checks
- Automatic reconnection on SessionExpired

#### RabbitMQ

- Robust connections with automatic recovery
- Heartbeat monitoring (600 seconds)
- Channel-level recovery
- Publisher confirmations on the extractors' publishing channels

### 4. Message Durability

During database outages:

1. **Messages remain in RabbitMQ** (persistent storage)
1. **Batch-mode consumers** (Graphinator, Brainzgraphinator, and Tableinator with `NEO4J_BATCH_MODE` /
   `POSTGRES_BATCH_MODE` at its default `true`) keep failed batches in process memory and retry them with backoff, so
   the broker's delivery count is not spent
1. **Per-message consumers** (Brainztableinator, which has no batch mode, and any consumer with batch mode off) **requeue** with `nack(requeue=True)`. Their main queues are quorum queues with
   `x-delivery-limit: 20`, so `common.outage_backoff.OutageBackoff` holds each delivery for a growing delay (2 seconds,
   capped at 60) before the requeue. That buys roughly 20 minutes of outage tolerance per message; a longer outage
   dead-letters the message to its DLQ, from which it must be recovered by re-extraction
1. **Idempotency prevents duplicates** using SHA256 hashes

## Service-Specific Implementation

### Extractors (Rust)

- `extractor-discogs` and `extractor-musicbrainz` are Rust services owned by `discogs-ingestion` and
  `musicbrainz-ingestion`; they do not use the Python classes above
- Retry the AMQP connection with exponential backoff (1 second, capped at 30)
- Publish with publisher confirms and treat an unconfirmed or returned message as a failure

### Graphinator Service (Neo4j)

- Uses `AsyncResilientNeo4jDriver` with automatic reconnection and `AsyncResilientRabbitMQ` for consuming
- Handles `ServiceUnavailable` and `SessionExpired` exceptions
- Requeues messages on connection failures

### Tableinator Service (PostgreSQL)

- Uses `AsyncPostgreSQLPool` with health monitoring and `AsyncResilientRabbitMQ` for consuming
- Connection pool with min/max bounds (2-12 by default; see `POSTGRES_POOL_MIN_SIZE` / `POSTGRES_POOL_MAX_SIZE` in [Configuration Guide](configuration.md))
- Handles `InterfaceError` and `OperationalError`
- Automatic connection recycling

### Dashboard Service

- Uses `AsyncResilientNeo4jDriver` and `AsyncResilientPostgreSQL`; it holds no RabbitMQ connection and reads queue
  metrics from `rabbitmq_prometheus` instead
- Async implementations for non-blocking operations
- Graceful degradation when services unavailable

### Brainzgraphinator Service (Neo4j)

- Uses `AsyncResilientNeo4jDriver` with automatic reconnection
- Enriches existing Neo4j nodes with MusicBrainz metadata
- Handles `ServiceUnavailable` and `SessionExpired` exceptions
- Requeues messages on connection failures

### Brainztableinator Service (PostgreSQL)

- Uses `AsyncPostgreSQLPool` with health monitoring
- Stores MusicBrainz data in `musicbrainz` PostgreSQL schema
- Connection pool with min/max bounds (2-12 by default; see `POSTGRES_POOL_MIN_SIZE` / `POSTGRES_POOL_MAX_SIZE` in [Configuration Guide](configuration.md)); RabbitMQ prefetch (channel-global QoS) is coupled to the pool max instead of using batch flushing
- Handles `InterfaceError` and `OperationalError`

## Configuration

### Environment Variables

No changes required to existing environment variables. The resilient connections use the same configuration:

```bash
# Neo4j
NEO4J_HOST=neo4j
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=groovemap

# PostgreSQL
POSTGRES_HOST=postgres
POSTGRES_DATABASE=groovemap
POSTGRES_USERNAME=groovemap
POSTGRES_PASSWORD=groovemap

# RabbitMQ
RABBITMQ_HOST=rabbitmq
RABBITMQ_USERNAME=groovemap
RABBITMQ_PASSWORD=groovemap
```

### Tuning Parameters

The following parameters are set in `python-libraries` code; only the pool bounds are environment-configurable
(`POSTGRES_POOL_MIN_SIZE` / `POSTGRES_POOL_MAX_SIZE`):

```python
# Circuit Breaker
failure_threshold = 3  # Failures before circuit opens
recovery_timeout = 30  # Seconds before recovery attempt

# Retry Settings
max_retries = 5  # Maximum connection attempts
initial_delay = 1.0  # Initial retry delay (0.5 for PostgreSQL)
max_delay = 60.0  # Maximum retry delay (30.0 for PostgreSQL and Neo4j)

# Connection Pools (AsyncPostgreSQLPool class defaults; services pass their own bounds)
postgres_min_connections = 2
postgres_max_connections = 20
postgres_health_check_interval = 30

# Neo4j Settings
neo4j_max_connection_lifetime = 1800  # 30 minutes
neo4j_max_connection_pool_size = 50
neo4j_connection_acquisition_timeout = 60.0
```

## Behavior During Maintenance

When databases undergo nightly maintenance:

1. **Connection Detection**: Services detect connection loss within seconds
1. **Circuit Breaker Opens**: After 3 failures, prevents cascade
1. **Message Queuing**: New messages remain in RabbitMQ
1. **Exponential Backoff**: Retry attempts with increasing delays
1. **Recovery**: When database returns, connections automatically restore
1. **Message Processing**: Queued messages process in order
1. **Idempotency**: Duplicate prevention via SHA256 hashes

## Monitoring

### Health Endpoints

Each application exposes health data, but only three probes are published by
the base Compose file:

- Dashboard: `http://localhost:8003/health`
- API: `http://localhost:8005/health`
- Explore: `http://localhost:8007/health`

Health ports are role-based: ingestors (both extractors) use 8000, graph
enrichers (Graphinator, Brainzgraphinator) use 8001, and SQL loaders
(Tableinator, Brainztableinator) use 8002. Insights uses 8009. All of these are
internal-only. Use `docker compose ps` for their Compose health status; those
ports are not mapped to localhost.

### Logging

Enhanced logging for connection events:

```
🔄 <name>: Creating new connection (attempt 1/5)
⚠️ <name>: Connection attempt 1 failed: Connection refused. Retrying in 1.2 seconds...
🔄 <name>: Creating new connection (attempt 2/5)
✅ <name>: Connection established successfully
❌ <name>: Circuit breaker OPEN after 3 failures
🔄 <name>: Circuit breaker entering HALF_OPEN state
✅ <name>: Circuit breaker reset to CLOSED
```

### Metrics

The `operations-console` dashboard exposes operator-facing JSON at `/api/metrics`
for its monitoring UI. This is not a Prometheus scrape endpoint.

## Testing Database Outages

To test the resilience features:

### 1. Stop a Database

```bash
# Stop Neo4j
docker compose stop neo4j

# Stop PostgreSQL
docker compose stop postgres

# Stop RabbitMQ
docker compose stop rabbitmq
```

### 2. Observe Service Behavior

Watch the logs to see connection failures and circuit breaker activation:

```bash
docker compose logs -f graphinator
docker compose logs -f tableinator
```

### 3. Restart Database

```bash
# Restart the stopped service
docker compose start neo4j
docker compose start postgres
docker compose start rabbitmq
```

### 4. Verify Recovery

- Services should automatically reconnect
- Queued messages should process
- No data should be lost

## Best Practices

1. **Don't Panic**: Services handle outages automatically
1. **Monitor Logs**: Watch for extended outage warnings
1. **Check Queues**: Monitor RabbitMQ queue depths during outages
1. **Verify Recovery**: Ensure message processing resumes after recovery
1. **Test Regularly**: Simulate outages in non-production environments

## Troubleshooting

### Services Not Recovering

If services don't recover after database restart:

1. Check circuit breaker state in logs
1. Verify database is fully started and accepting connections
1. Restart affected service if needed: `docker compose restart [service]`

### Messages Not Processing

If messages remain queued after recovery:

1. Check service health endpoints
1. Verify database connectivity manually
1. Look for poison messages causing repeated failures
1. Check dead letter queues for poison messages (each consumer has its own DLQ)

### Performance Issues

If services are slow after recovery:

1. Check for message backlog in RabbitMQ
1. Monitor database connection pool usage
1. Consider increasing prefetch counts temporarily
1. Watch for circuit breaker flapping (rapid open/close)
