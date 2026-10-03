# Delivering extractor images without changing the remaining stack

This runbook addresses `gm-deployment-eeo`: promote only the two existing extractor
services to their independently released provider images. It does not replace the
whole-stack deployment, databases, consumers, secrets or data volumes. The deployment
repository consumes images; release publication belongs to each source repository.

## Required evidence

Keep operational configurations and collected runtime evidence private and untracked.
The release manifest records both immutable image references, versioned releases,
OCI revisions and `linux/amd64` support. Before using it, the independent reviewer must
verify the actual registry/tag/OCI evidence and the source ancestry command against
the source-owned release. The expected reviewed main commits are recorded in
`scripts/extractor_delivery.py`. A release commit may descend from those mains.

`source_ancestry` binds the exact image reference, ancestor, OCI descendant revision,
`git merge-base --is-ancestor <ancestor> <descendant>` command, its zero exit status and
durable result reference. The tool checks this record's consistency. It does **not**
authenticate a reference, fetch registry metadata, run the ancestry command, or grant
approval. A fabricated record, a synthetic test fixture, or a nonempty reference is
never provenance acceptance. The root operator supplies verified records and the
independent reviewer checks them before image delivery.

Every compatibility proof must identify that same immutable image, have a passed
result and a durable reference: CLI, secret-file support, mount permissions, routing,
legacy consumer contracts, persisted resume, and emitted root production JSON. The
logging result must contain a positive count of actual JSON events. These are
external evidence inputs, not assertions that the offline unit tests exercised an
image or a live host. The checked-in example intentionally cannot pass preflight.

## Compatibility smoke before the live adaptation

Use disposable containers, broker queues, stores and synthetic producer-owned fixtures.
Never attach existing production data/log volumes, secret files or broker networks to
this lane. Pin the producer and the four unchanged legacy consumer images by digest;
record their inspected revisions. A test against newer consumer images does not prove
compatibility with the currently deployed consumers.

For both actual provider digests, establish:

1. `--help` exposes the provider binary and the legacy `--source` selector is rejected.
   Inspect the image's entrypoint and default command; resetting the Compose command
   must invoke its provider entrypoint without hidden arguments.
2. A deliberately missing synthetic `RABBITMQ_PASSWORD_FILE` fails before broker
   acquisition and emits valid JSON with `environment=production`. Repeat without
   `ENVIRONMENT` to prove the development fallback. Use only synthetic credentials.
   Separately demonstrate a readable synthetic secret file, with percent-encoded
   credentials, against the disposable broker.
3. Explicit prefixes retain the existing exchanges and their durable fanout/empty-key
   semantics, mandatory publishing, publisher confirmation and persistent JSON frames.
4. Producer-owned normalized events, hashes and completion messages are accepted by the
   exact legacy consumer builds. Capture assertions about stored synthetic records and
   acknowledgements; do not replay or sample production messages.
5. A representative synthetic marker loads and resumes completed progress without
   force-reprocessing. Compare its schema with sanitized production marker metadata;
   never overwrite the real marker or run the fixture against the real dump root.

The existing Discogs packaged `LOCAL_MANIFEST` lane covers `data` and `file_complete`,
with timestamp-only normalization. It does not cover run-level `extraction_complete`,
MusicBrainz extraction, or exact legacy consumers. Add those isolated proofs before
recording their categories as passed. MusicBrainz needs an isolated synthetic dump
endpoint/JSONL acquisition harness or equivalent actual-image test; a production dump
is not a logging fixture. Keep external acquisition disabled in the smoke lane.

After root coordinates selection, review and pull of the versioned immutable image,
its bounded startup probe can run in an explicitly selected Docker context:

```bash
uv run python scripts/extractor_delivery.py \
  --probe-image 'ghcr.io/groovemap-music/discogs-ingestion@sha256:<approved-digest>' \
  --provider discogs --docker-context <selected-context> \
  --image-revision <verified-release-OCI-revision>
```

Repeat for MusicBrainz. This helper does not pull or publish images. It checks local
image platform, OCI revision, entrypoint and empty default command, then creates only
its own four network-isolated, read-only, mount-free containers. It removes only the
container IDs verified against its unique task name/owner labels, including cleanup
when create times out before returning an ID. An unverified container is never removed.
CPU is capped at 1, memory and memory+swap at 256 MiB, PIDs at 64, and tmpfs at 16 MiB.
A timed-out create can still finish late: retain the reported task name/owner and
reconcile its labels if Docker was unavailable during cleanup; never blindly delete
a matching name. It checks `--help`, rejection of
`--source`, and production/default JSON under deliberately missing synthetic secrets.
Its report explicitly excludes valid-secret broker, consumer and resume acceptance.
No synthetic unit-test report substitutes for executing this against the actual digest.

For already captured private JSON output, `--inspect-log <private-log-file>` reports
only event/context counts. Bind that result to the separately verified running image;
this parser does not authenticate a container or image by itself.

## Prepare and compare the narrow override

The root operator captures the original resolved Compose configuration privately,
using the authoritative existing entrypoint and project. Preserve the old extractor
digest, its availability, original commands, mounts, network identities and health
configuration as the rollback baseline. Capture current extraction status before
choosing the recreation time; `healthy` alone does not mean ingestion is idle.

Map each provider to its exact existing resolved Compose service in the private
manifest `service` field. The mappings must be distinct and present in the baseline;
no public default invents or renames operational services. Each mapping also records
the verified original immutable `baseline_image`; the resolved service must match
that digest and its original `--source <provider>` selector. Mapping an unrelated
existing service is rejected before rendering or comparing any candidate.

Fill a private copy of `config/extractor-delivery.example.json` only from the reviewed
image and compatibility evidence. Use the actual broker prefixes confirmed by exchange
and binding listings. The current deployment's prefixes differ from the independent
images' default prefixes; do not infer them from an obsolete `AMQP_EXCHANGE_PREFIX`.

```bash
uv run python scripts/extractor_delivery.py \
  --manifest /private/path/reviewed-delivery.json \
  --baseline /private/path/original-compose.json \
  --output /private/path/review-extractor-override.yml
```

The override resets only the legacy commands and ineffective startup/cross-provider
settings, selects the two immutable images, retains root production logging, and sets
explicit provider exchange prefixes. It inherits mounts, users, secrets, configs,
health settings, aliases and every unrelated service. It does not set new usernames,
passwords, vhosts or volumes. Compose resolves `!reset []` to `command: null`; the
read-only real-Compose regression verifies that behavior.

Resolve the candidate with exactly the same project, base files and private operator
environment, appending this reviewed override last. Do not execute `up` yet. Then:

```bash
uv run python scripts/extractor_delivery.py \
  --manifest /private/path/reviewed-delivery.json \
  --baseline /private/path/original-compose.json \
  --candidate /private/path/candidate-compose.json
```

The comparator requires exact resolved value equality outside the intended changes,
including all unrelated services and top-level networks, volumes, secrets and configs.
It rejects extra drift without printing private values. Review any rejected difference
locally; do not relax the comparison to accommodate unrelated migration.

## Coordinated apply and rollback

After independent adaptation/image review, root coordinates the already authorized
live operation using the existing authoritative project, base files and approved
override. Address only the two exact existing service names from the private
manifest, with no dependency recreation. Do not apply the
standalone whole-stack Compose, change storage ownership, purge queues, remove volumes,
change force-reprocessing, or upgrade databases as part of this delivery.

Verify the actual running digests, numeric user/mount access, network identities,
health/progress, existing consumer bindings and fresh JSON from both recreated
containers. Every observed structured event must have root `environment=production`.
Record sanitized counts, image identities and acceptance, not raw messages, secrets or
private dump contents. Source gates and unit-test results alone cannot certify this.

If health, routing, logging or resumed progress regresses, restore the old two-service
configuration and immutable digest with the same volumes/secrets, and verify health and
progress again. Do not roll back by deleting marker files or reprocessing all records.
Only after actual live acceptance and required repository review/checks can this
bead and its original production-logging parent acceptance be considered complete.
