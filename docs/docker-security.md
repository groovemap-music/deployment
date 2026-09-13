# Docker Security Configuration

This document describes the security measures implemented in the groovemap Docker deployment.

## Security Features

### 1. Non-Root User Execution

Every source-owned application image is assigned the configurable host UID/GID:

```yaml
user: "${UID:-1000}:${GID:-1000}"
```

- Default: UID=1000, GID=1000
- Customize by setting `UID` and `GID` environment variables
- Matches host user to avoid permission issues with volumes
- Third-party infrastructure images keep their image-defined user; Compose
  does not falsely override a UID their entrypoint may require

### 2. Capability Dropping

All source-owned application containers drop all Linux capabilities. The
collector and metrics/exporter containers do too; cAdvisor then adds back only
`DAC_READ_SEARCH` and `SYSLOG` for host filesystem and OOM observation.

```yaml
cap_drop:
  - ALL
```

This prevents containers from:

- Modifying network configuration
- Loading kernel modules
- Accessing raw sockets
- Other privileged operations

### 3. No New Privileges

Every service except RabbitMQ declares this option to prevent privilege
escalation:

```yaml
security_opt:
  - no-new-privileges:true
```

### 4. Read-Only Root Filesystem

Schema-init, API, every consumer, Dashboard, Explore, and Insights use read-only
root filesystems with an explicit temporary filesystem:

```yaml
read_only: true
tmpfs:
  - /tmp
```

- Prevents malicious writes to the container filesystem
- `/tmp` is mounted as tmpfs for temporary files
- Application data uses explicit volumes
- The two extractor containers are exceptions: they drop all capabilities and
  write their source-specific data/log volumes, but their root filesystems are
  not marked read-only in the current Compose contract

### 5. Health Checks

Health checks match what each image can execute. Application services use HTTP
probes, databases use native clients, Grafana and some exporters use HTTP, and
the distroless collector/trace images validate their own binaries or config.
For example:

```yaml
healthcheck:
  test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
  interval: 30s
  timeout: 10s
  retries: 3
  start_period: 60s
```

### 6. Network Isolation

Services use a dedicated Docker network:

```yaml
networks:
  groovemap:
    driver: bridge
    ipam:
      config:
        - subnet: 172.20.0.0/16
```

### 7. Restart Policies (Production)

Production deployment includes automatic restart policies:

```yaml
deploy:
  restart_policy:
    condition: any
    delay: 5s
    max_attempts: 3
```

## Environment Variables

### Security-Sensitive Variables

These secrets are **never passed as plain environment variables in production**.
Instead, the production overlay mounts them at `/run/secrets/*` and services
read them through the `_FILE` convention. With this repository's local
Compose `file:` provider, the source files remain on the Docker host under the
untracked `secrets/` directory; they are not ephemeral Swarm secrets. See
[Production Secrets Setup](#production-secrets-setup) below.

| Secret                 | `_FILE` env var                     | Plain env var (dev only)       |
| ---------------------- | ----------------------------------- | ------------------------------ |
| RabbitMQ password      | `RABBITMQ_PASSWORD_FILE`            | `RABBITMQ_PASSWORD`            |
| RabbitMQ username      | `RABBITMQ_USERNAME_FILE`            | `RABBITMQ_USERNAME`            |
| PostgreSQL password    | `POSTGRES_PASSWORD_FILE`            | `POSTGRES_PASSWORD`            |
| PostgreSQL username    | `POSTGRES_USERNAME_FILE`            | `POSTGRES_USERNAME`            |
| Neo4j password         | (via entrypoint wrapper)            | `NEO4J_AUTH`                   |
| JWT secret key         | `JWT_SECRET_KEY_FILE`               | `JWT_SECRET_KEY`               |
| Encryption master key  | `ENCRYPTION_MASTER_KEY_FILE`        | `ENCRYPTION_MASTER_KEY`        |
| Resend API key         | `RESEND_API_KEY_FILE`               | `RESEND_API_KEY`               |
| NLQ API key            | `NLQ_API_KEY_FILE`                  | `NLQ_API_KEY`                  |
| Redis password         | `REDIS_PASSWORD_FILE`               | `REDIS_PASSWORD`               |
| Insights internal key  | `INSIGHTS_INTERNAL_SECRET_FILE`     | `INSIGHTS_INTERNAL_SECRET`     |
| Grafana admin password | `GF_SECURITY_ADMIN_PASSWORD__FILE`  | `GF_SECURITY_ADMIN_PASSWORD`   |

The Dashboard's RabbitMQ management-API access reuses the same `RABBITMQ_USERNAME`/`RABBITMQ_PASSWORD` (or `_FILE`) credentials above — there is no separate management-only credential pair.

Plain env vars work in development. The production overlay (`docker-compose.prod.yml`) switches to the `_FILE` convention automatically. The shared secret-loading implementation is owned by [`python-libraries`](https://github.com/groovemap-music/python-libraries).

### User Configuration

Set these to match your host user:

```dotenv
UID=1000
GID=1000
```

Use the output of `id -u` and `id -g` for the values in the untracked `.env`
copied from `.env.example`; `UID` is read-only in common shells.

## Production Secrets Setup

### 8. Runtime Secrets via `docker-compose.prod.yml`

The production overlay mounts secrets at `/run/secrets/<name>`. Secret values
are not embedded in container environment variables or normal
`docker inspect` output, but the backing files are written to the Docker host's
untracked `secrets/` directory. Protect, rotate, and remove those files through
the approved host secret-management process.

**Step 1 — Generate secrets** (idempotent, skips existing files):

```bash
bash scripts/create-secrets.sh
```

This creates `secrets/` (mode `700`) with one file per secret (mode `600`):

| File | Value or generation method |
| --- | --- |
| `jwt_secret_key.txt` | `openssl rand -hex 32` |
| `encryption_master_key.txt` | URL-safe base64 of 32 random bytes |
| `resend_api_key.txt` | Optional Resend API key; empty disables delivery |
| `nlq_api_key.txt` | Optional NLQ provider API key; empty unless enabled |
| `postgres_username.txt` | `groovemap` |
| `postgres_password.txt` | `openssl rand -base64 24` |
| `rabbitmq_username.txt` | `groovemap` |
| `rabbitmq_password.txt` | `openssl rand -base64 24` |
| `neo4j_password.txt` | `openssl rand -base64 24` |
| `redis_password.txt` | `openssl rand -base64 24` |
| `insights_internal_secret.txt` | `openssl rand -hex 32`; shared by API and insights |
| `grafana_admin_password.txt` | `openssl rand -base64 24`; required because production disables anonymous access |

Use `secrets.example/` as a reference for each file's format and generation command.

**Step 2 — Start with production overlay**:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

**Neo4j note**: Neo4j does not natively support the `_FILE` convention. The production overlay overrides Neo4j's entrypoint with `scripts/neo4j-entrypoint.sh`, which reads `/run/secrets/neo4j_password` and sets `NEO4J_AUTH=neo4j/<password>` before delegating to the official Neo4j entrypoint.

**Redis note**: Redis does not natively support the `_FILE` convention either. The production overlay overrides Redis's entrypoint with `scripts/redis-entrypoint.sh`, which reads `/run/secrets/redis_password` and appends `--requirepass <password>` before delegating to the official Redis entrypoint. It also rebinds the published port to `127.0.0.1:6379` instead of the base file's `0.0.0.0:6379`, since Redis is meant to be reached over the internal `groovemap` docker network by api/dashboard/insights, not from the host's public interfaces.

### Running Securely

The commands below change environment state. Run them only after reviewing the
rendered configuration and obtaining approval for the exact target.

**Development**:

```bash
# Copy and configure environment
cp .env.example .env
# Edit .env to set UID/GID

# Run with security features
docker compose up -d
```

**Production**:

```bash
# 1. Generate secrets (first time only — safe to re-run)
just secrets-bootstrap

# 2. Start with production overlay (secrets + restart policies)
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

## Security Best Practices

1. **Regular Updates**

   - Update base images regularly
   - Rebuild containers for security patches
   - Monitor for vulnerabilities with tools like Trivy

1. **Secrets Management**

   - Use `docker-compose.prod.yml` with `scripts/create-secrets.sh` for Docker Compose deployments
   - For Kubernetes, use Kubernetes Secrets or an external secrets operator
   - For cloud deployments, consider AWS Secrets Manager, Azure Key Vault, or HashiCorp Vault
   - Never use default passwords in production
   - Rotate secrets by updating the file in `secrets/` and restarting the affected container

1. **Monitoring**

   - Monitor container logs for suspicious activity
   - Set up alerts for health check failures
   - Track resource usage for anomalies

1. **Network Security**

   - Use TLS for all external connections
   - Restrict exposed ports to minimum required
   - Consider using reverse proxy (nginx, traefik) for services

## Verification

### Check Security Configuration

```bash
# Verify user execution (extractor runs as two services)
docker compose exec extractor-discogs id
docker compose exec extractor-musicbrainz id

# Check capabilities
docker compose exec extractor-discogs capsh --print

# Verify read-only filesystem
docker compose exec extractor-discogs touch /test.txt  # Should fail

# Check security options
docker inspect groovemap-extractor-discogs | jq '.[0].HostConfig.SecurityOpt'
```

### Security Scanning

```bash
# Scan each promoted source-owned extractor image by its exact approved digest;
# the two Compose services do not share an image.
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
  aquasec/trivy image "${DISCOGS_INGESTION_IMAGE:?set the approved digest}"
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
  aquasec/trivy image "${MUSICBRAINZ_INGESTION_IMAGE:?set the approved digest}"

# Check for misconfigurations
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
  aquasec/trivy config .
```

## Troubleshooting

### Permission Issues

If you encounter permission errors:

1. Check UID/GID match your user:

   ```bash
   echo "Host: UID=$(id -u) GID=$(id -g)"
   docker compose exec service id
   ```

1. Fix volume permissions:

   ```bash
   sudo chown -R $(id -u):$(id -g) ./volumes/
   ```

### Read-Only Filesystem Errors

Some applications may need writable directories:

1. Add specific tmpfs mounts:

   ```yaml
   tmpfs:
     - /tmp
     - /run
     - /var/cache
   ```

1. Or use volumes for persistent data:

   ```yaml
   volumes:
     - app_cache:/app/.cache
   ```
