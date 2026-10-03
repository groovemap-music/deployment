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
with timestamp-only normalization. Its packaged expected stream omits run-level
`extraction_complete`; the actual one-shot processing function can emit that broadcast
to all Discogs data types. Capture and assert the actual completion frames rather than
assuming the expected fixture stream lists every frame. The packaged lane does not
prove MusicBrainz extraction or exact legacy consumers. Add those isolated proofs before
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
reconcile its labels if Docker was unavailable during cleanup. Cleanup errors and
timeouts retain the exact task name, owner and context for this reconciliation;
never blindly delete
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
the exact `baseline_configured_image` from the unmodified resolved configuration
and the inspected original immutable `baseline_image` for rollback. A separate
`baseline_runtime` record binds that configured reference to its actual image ID,
registry RepoDigest and durable inspection evidence. An image ID is not assumed
to equal a registry manifest digest. The resolved service must match the original
configured reference and its original `--source <provider>` selector. Mapping an unrelated
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


## Preparing the host-native synthetic lane

`scripts/extractor_smoke_prepare.py` prepares fixtures and disposable Compose models
from the root-selected private context and reviewed image metadata. It does not call
Docker or execute a smoke. It verifies exact legacy schema hashes and consumer digests,
counts the quotas of all ten service roles even for sequential steps, and conservatively
counts named tmpfs capacity in addition to every container memory cap. Prepared models
have only an internal network, no published ports, read-only roots, capped logs and
synthetic fixtures. A pinned legacy Python consumer is proposed as the HTTP runtime;
its executable/module and every image-declared VOLUME path still need actual inspection.

The host-native `scripts/extractor_smoke_run.py` uses the selected host's existing
`sudo -n docker` executables, checks its hostname and acquires the existing exclusive
lock without creating a replacement. It reserves the final 120 seconds of the overall
900-second deadline for ownership-verified cleanup. It runs the bounded startup
CLI/negative-secret/logging probes through that same executor, probes the exact legacy
Python runtime without mounts or networking, inspects actual caps and all declared
image volumes, initializes empty stores from the exact legacy schemas, and runs both
providers sequentially. No local Docker context is created or changed.

The intralane driver checks exact sentinel/count/completion frames and legacy store
fields/relationships. MusicBrainz graph fixtures seed only synthetic Discogs nodes so
that all-skipped enrichment cannot pass. The keeper holds the bounded producer tmpfs
volume across restart. Completed and partial marker scenarios assert queue, frame,
checksum, count and original-start-time continuity before any compatibility result.
Observer callback failures create a durable failed flag and stop acceptance. Every
runtime assertion still needs execution against the exact approved images; offline
regressions and preparation do not provide those results.

Root supplies the reviewed context, actual image/provenance metadata, exact legacy
schema sources, and an independent review reference for the final runner. Prepare an
absent local bundle with `extractor_smoke_prepare.py --context ... --images ...
--legacy-schemas ... --output ...`, then root coordinates copying that reviewed bundle
under the selected owned root. Host-native execution uses `extractor_smoke_run.py
--context ... --images ... --bundle <selected-owned-root>/bundle --review-reference ...`.
The reference inputs are structural and do not authenticate approval by themselves.
Only after root has verified the real evidence and independent review may these
commands create resources. No generated bundle or execution result belongs in git.

The isolated auxiliary model uses the pinned images' supported nonroot users: RabbitMQ 999:999 and Neo4j 7474:7474. Neo4j's existing 4 MiB copied-up configuration volume mounts at its actual writable configuration directory; its run directory gets a 4 MiB tmpfs. Its image root remains read-only. RabbitMQ server and control processes share the official user's cookie, and both use one normal, one dirty CPU, and one dirty I/O scheduler to fit the existing CPU/PID caps rather than the host's CPU count. These are resource and startup corrections; the earlier broker failure cause remains unproven because its logs were not retained. Future auxiliary exits fail promptly and preserve bounded, credential-redacted task-owned state/log tails before guarded cleanup.

Neo4j JNA extraction uses only `/var/lib/neo4j/native-tmp`, an owned 4 MiB tmpfs with execute permission, mode 0700, and UID/GID 7474. `JAVA_TOOL_OPTIONS=-Djna.tmpdir=...` covers both admin initialization and the server without replacing its existing JVM configuration. Generic `/tmp` remains non-executable. This implements the [Neo4j documented JNA temporary-directory requirement](https://neo4j.com/docs/operations-manual/current/configuration/file-locations/); actual retry evidence is still required. Failure diagnostics cover every known role and have a 15-second budget while reserving at least 90 seconds of the overall deadline for cleanup.

Graph schema DDL starts only after authenticated driver connectivity and a successful `RETURN 1` query against the owned empty graph. A 60-second readiness budget retries only socket connection refusal preserved in the pinned driver's exception cause chain; authentication, protocol/configuration failures, and per-attempt timeouts fail directly. DDL is invoked once after readiness and partial DDL errors remain failures. The host's 780-second work and 900-second total deadlines still apply.

The disposable Neo4j auxiliary enables authenticated Bolt only. Its HTTP and HTTPS connectors are explicitly disabled using the [supported connector settings](https://neo4j.com/docs/operations-manual/current/configuration/connectors/). The lane's legacy consumers and assertions use Bolt; no web API or Browser compatibility is claimed. This avoids extracting unused Browser static content into the intentionally small temporary filesystem, which the actual failed run showed exhausting space. Production connector configuration is untouched; server failures still fail readiness immediately.

Each transient capture queue is exclusive to its observer connection and automatically deleted when that connection closes, following [RabbitMQ queue ownership semantics](https://www.rabbitmq.com/docs/queues) and [aio-pika declaration options](https://docs.aio-pika.com/apidoc.html). The observer remains alive throughout producer completion and restart tests, and its capture file lives in the owned bounded capture volume. Broker deprecated-feature settings and the actual producer/consumer topology are unchanged. Observer exit fails readiness immediately and preserves the existing bounded diagnostics before cleanup.

SQL verification follows the exact legacy consumer normalization: the synthetic master year is persisted as integer 2000 and the release date derives integer year 2000. Every other captured field and relationship must remain equal, with normalized year type/value independently asserted. Different single-file versions remain unchanged; prior stale-purge safety refusals are recorded without treating them as the sole failure cause. Failed SQL/graph verifier subprocesses now preserve bounded first and latest redacted errors in the owned host directory before retry or cleanup.
