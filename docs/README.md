# Deployment documentation

These guides are for operators and contributors of the GrooveMap whole-stack
deployment: the Compose topology, production hardening, secrets, image
promotion, and stack-level validation. Service behavior is documented in each
service's own source repository. New here? Read the quick start first, then the
architecture overview.

## Start here

- [Run GrooveMap in your homelab](homelab.md): prerequisites, sizing, the one-command install, the staged first load, ports, reverse proxy, upgrades.
- [Quick start](quick-start.md)
- [Architecture](architecture.md)
- [Configuration](configuration.md)
- [Administration](admin-guide.md)
- [Troubleshooting](troubleshooting.md)

## Operations and assurance

- [Maintenance](maintenance.md)
- [October 2026 operations console rollout](rollout-2026-10-operations-console.md): releasing and deploying the console redesign, the broker-stats retirement, source-routed triggers and role-based health ports.
- [Monitoring](monitoring.md)
- [Observability](observability.md)
- [Database resilience](database-resilience.md)
- [Graph backend continuity and rollback](graph-backend-continuity.md)
- [Docker security](docker-security.md)
- [Dockerfile standards](dockerfile-standards.md)
- [Transactional email live verification](transactional-email-verification.md)
- [Testing](testing-guide.md)
- [Performance](performance-guide.md)
- [Platform targeting](platform-targeting.md)
- [Usage examples](usage-examples.md)
- [Source-history provenance](extraction.md)
- [Extractor delivery](extractor-delivery.md): two-service compatibility, image provenance, adaptation and rollback.
- [Explore and Insights delivery](health-service-delivery.md): replacing only those two services with source-owner images, with guarded preparation and rollback.
