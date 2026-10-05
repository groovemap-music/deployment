# Reviewed Explore and Insights delivery

This procedure replaces only the existing Explore and Insights services with independently
released source-owner images. It does not migrate the stack, launch embeddings, change databases,
or touch the already-delivered extractor images. The delivery incident stays open until actual
production acceptance succeeds. Preparation and synthetic tests are not that acceptance.

## Evidence before preparing a candidate

Capture a fresh private resolved Compose baseline and the exact bytes of the authoritative
included managed fragment. Retain its SHA256, current operator dirty-file inventory, service and
unrelated container IDs, mounted-file metadata, network identities and immutable rollback images.
Never publish resolved configuration, credentials, model data or private values in this repository.
Old inventory receipts establish history, not the current host state.

The private JSON manifest has `format_version: 1`, `baseline_reference` and exactly two entries
under `services`: `explore` and `insights`. Each entry contains the actual mapped `service`,
`baseline_configured_image`, inspected immutable `rollback_image`, approved immutable source-owner
`image`, `platform: linux/amd64`, full source `revision`, versioned `version`, and references named
`release_reference`, `attestation_reference`, `ancestry_reference`, `independent_review_reference`.
Root must independently verify those references, release checksums, OCI labels and trusted signed
provenance against the exact published digest and source revision. The preparation tool requires
references but cannot establish their truth or grant approval.

Each entry's `proofs` names exactly `cli`, `environment`, `mounts`, `network`, `health`, `proxy`,
`model_data`, `logging`, `rollback`. Every proof binds `image`, actual `status: passed` and a durable
private `reference`. Logging additionally records `environment: production`, source-owner
`service` (`graph-explorer` or `analytics-engine`), positive `events`, zero `missing_fields` and
zero `wrong_fields`. Populate these only from bounded isolated tests of the actual published image.
Do not copy fixture evidence into an operational manifest.

Explore smoke must cover root/assets, actual Catalog API request compatibility, repeated query
parameters, forwarded identity, buffered proxy and NLQ SSE. Insights smoke must establish secret
file access without exposing contents, API internal authentication, actual PostgreSQL/cache
contracts, existing insight/model compatibility and preserved schedule parameters. Prevent isolated
smoke from scheduling writes against production; use reviewed disposable inputs or read-only
credentials. Never run an embeddings job as part of delivery. Neither unit logging tests nor a
healthy HTTP endpoint establish these broader contracts.

## Guarded preparation

The helper rejects any baseline or direct fragment containing `STARTUP_DELAY`, including zero.
An independently reviewed preservation or adaptation contract is required before that behavior
can be changed. Existing dependency health ordering remains unchanged. Explicit legacy command/entrypoint overrides fail closed and require an independently
reviewed adaptation. Production environment is explicit. Healthchecks use the image's installed
Python because the owner images have no curl dependency. All other fields are retained.

```sh
uv run python scripts/health_service_delivery.py \
  --original "$PRIVATE_MANAGED_FRAGMENT" --expected-sha256 "$VERIFIED_SOURCE_SHA256" \
  --manifest "$PRIVATE_REVIEWED_MANIFEST" \
  --forward-output "$PRIVATE_FORWARD_CANDIDATE" \
  --rollback-output "$PRIVATE_ROLLBACK_CANDIDATE" \
  --receipt "$PRIVATE_PREPARATION_RECEIPT"
```

Outputs must be distinct absent files; the input is never opened for writing. Forward preparation
changes only the two images, `ENVIRONMENT` and healthcheck test. It does not remove startup delay.
Rollback preparation retains every original service contract, replacing only mutable old images
with inspected immutable rollback references. It is rendered from the original bytes, not from the
forward candidate. Comments, CRLF and unrelated operator edits remain byte-for-byte intact.

Resolve each candidate through the actual full private include tree without modifying the live
fragment. Compare the resolved models using `validate_candidate(baseline, candidate, manifest)`
and `rollback=True` for rollback. This comparison includes every top-level and service field,
including mounts, secrets, networks, model inputs, dependencies and prior extractor pins. A
successful render does not claim Compose equivalence; its receipt explicitly leaves that false.
Root independently reviews the exact image-bound manifest, adaptation and runner before any
production write. Recheck the source hash immediately before applying the approved candidate;
stop on drift, do not overwrite or discard operator edits.

## Root-owned live acceptance

Recreate only the mapped Explore and Insights services using the reviewed immutable images.
Capture both application and dedicated health endpoints, actual structured background health-server
events, bounded common logging samples with zero missing/wrong required fields, real proxy and
analytics/data/model compatibility, unchanged secret/mount/network identities and unrelated
container IDs. Verify already-approved extractor pins remain unchanged. On a failed acceptance,
use the independently tested immutable rollback with the original configuration contract, then
verify recovery; retain both failure and rollback evidence.

Normal repository checks, independent pristine review, attributed child/epic integration and
verified origin/main push remain required. No preparation receipt, release success or stored
`in_progress` state substitutes for actual completed delivery and incident acceptance.
