# Platform Targeting Configuration

This document explains how the deployment repository handles CPU architectures. It builds no images and installs no
service packages, so platform targeting here is about which image manifests an environment promotes and which
architecture each container runs on.

## Internal GrooveMap images

Each source repository builds and publishes its own image. The released internal images currently target
`linux/amd64`. When promoting a release, resolve the GHCR manifest digest for the environment's architecture (see
[Promote a service release](maintenance.md#promote-a-service-release)), and re-resolve it for any platform other than
the one the release workflow published.

## Third-party images

Database, broker, cache, exporter, and telemetry images in `docker-compose.yml` must be pinned to the **multi-arch
index digest of a platform-neutral tag**, so every engine pulls its native build. `scripts/check-images.py`
(`PLATFORM_TAG_SUFFIX`) rejects a tag that names one architecture, such as `0.162.0-386` or `v1.2.3-arm64`: that tag
resolves to a single-platform manifest whose digest runs on that platform only. Variant suffixes such as `-alpine` or
`-community` are not platforms and stay valid.

The check exists because Dependabot once proposed a single-platform `-386` collector tag; the pinned `linux/386`
binary segfaulted at startup on an arm64 engine.

## Disposable smoke stacks

On a workstation whose architecture differs from the released images (for example Apple silicon), the smoke overlays
pin only the internal application services to their released platform, so Docker emulates them while the broker,
stores, and cache stay host-native:

| Overlay | Internal-service platform |
| --- | --- |
| `docker-compose.smoke.yml` (`just smoke-infra`, `just smoke-released`) | `linux/amd64` |
| `docker-compose.media-smoke.yml` (`just smoke-media`) | `SMOKE_MEDIA_SERVICE_PLATFORM`, default `linux/amd64` |
| `docker-compose.erasure-smoke.yml` (`just smoke-erasure`) | `SMOKE_ERASURE_SERVICE_PLATFORM`, default `linux/amd64` |
| `docker-compose.released-fixture-smoke.yml` (`just smoke-released-fixture`) | `SMOKE_RELEASED_FIXTURE_SERVICE_PLATFORM`, default `linux/amd64` |

Set the variable to the architecture an environment actually promotes.

## Validation toolchain

`just check` runs on the host. `.mise.toml` pins Python, uv, just, and gitleaks, and `uv.lock` is a universal lock for
the validation dependencies in `pyproject.toml`; it carries no `[tool.uv] environments` restriction because nothing in
this repository is installed into an image.

## References

- [Docker multi-platform images](https://docs.docker.com/build/building/multi-platform/)
- [Container image standards](dockerfile-standards.md)
