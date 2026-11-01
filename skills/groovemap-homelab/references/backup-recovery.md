# Backup coverage and secret recovery

Run this after deploy (SKILL.md step 6) and before any upgrade. Everything here is **read-only**:
look, report, and ask the user. Don't change another service's backup configuration, don't
print a secret value, and don't run anything that writes to a shared store.

## 1. Is the data protected? (shared PostgreSQL)

Reusing an existing PostgreSQL (external-stores mode) does **not** put GrooveMap's database
under that server's backups. Backups usually run from an allowlist of databases, and a new
database is not on it. The user's operator policy decides; the skill only checks and reports.

Ask the user where the backup configuration lives (a backup container's environment, a cron job,
a script, a stack file) and read it. Look for:

1. **Protected-database list.** Is GrooveMap's database (`EXTERNAL_POSTGRES_DATABASE` in `.env`,
   default `groovemap`) in it? If it is missing, tell the user the database has **no backup**
   and name the setting to change. They make the change; you don't.
2. **Schema exclusions.** Which schemas does the backup skip? Report them, and keep the
   policy: do not suggest removing an exclusion that the operator chose.
   - An excluded **`musicbrainz`** schema is a valid choice: it is reconstructible. But a
     restore then has **no MusicBrainz data** until the MusicBrainz import is run again. That is
     a long job (the Discogs import alone takes hours to about two days; MusicBrainz sizing is
     unmeasured until `gm-deployment-vc5s`, so give no figure beyond "likely days and
     on the order of 100 GB of disk"). The user should accept that downtime knowingly.
   - Every other GrooveMap schema must be in the backup (see section 3).
3. **Restore verification.** Does the periodic restore test or verification job cover the
   GrooveMap database, or only the databases it was set up with? A backup that has never been
   restored is not evidence. Ask when GrooveMap's database was last restored to a scratch target.
4. **Retention and location.** Backups on the same disk as the data do not survive its loss.

## 2. Dedicated stores

For each store GrooveMap runs itself, the user needs a backup of its volume, taken with the
store's own tool or with the container stopped. Compose volume names: `postgres_data`,
`neo4j_data`, `rabbitmq_data`, `valkey_data`. A bind-mounted relocation (references/storage.md)
changes where the files are, not whether they need backing up. External Valkey: its operator
owns it; ask the same questions as in section 1.

## 3. What can be rebuilt, and what cannot

| Data | Rebuilt from dumps? |
| --- | --- |
| Discogs catalog (PostgreSQL, Neo4j) | Yes, by re-import (hours to about two days) |
| `musicbrainz` schema | Yes, by re-import (long; unmeasured, `gm-deployment-vc5s`) |
| RabbitMQ queues | Yes; in-flight work is re-queued by the import |
| Valkey | Yes; a cache |
| **User accounts and credentials** | **No** |
| **Collections and other user data** | **No** |
| **Identity ledger** | **No** |
| **Admin audit log** | **No** |

Only the last four need the backup to be good. They live in the GrooveMap PostgreSQL database.

## 4. Secrets and the recovery bundle

`just secrets-bootstrap` generates the JWT key, the encryption master key and the datastore
passwords. Without those exact values a restored database or volume cannot be used: sessions,
OAuth tokens and TOTP secrets are unreadable, and the stores reject the passwords.

Run `just recovery-manifest` (read-only; python3 3.10 or newer, uv optional). It prints the
files to capture: `.env`, `docker-compose.override.yml`, the names, mode, owner and a short
SHA-256 fingerprint of each `secrets/` file (never a value), the deployment commit and the
promoted image digests, and which stores need their own backups. Tell the user to put those
files in one bundle and **encrypt** it (SOPS with age, as they already do) before it leaves the
host, and to keep it apart from the database backups. Never put the bundle in the repository.

## Rules

- **Never reset an existing secret.** `just secrets-bootstrap` and `just deploy` skip existing
  files, and `just deploy` stops with exit 3 if one changed anyway. Don't delete a file to
  "regenerate" it, and don't rotate except by the procedure in docs/maintenance.md#secrets.
- **Never expose a value.** Not in output, a commit, a ticket or the chat. Fingerprints only.
- **Never purge persistent keys or volumes after a failed staging.** No `docker compose down -v`,
  `docker volume rm`, or deleting `secrets/` to start clean. Fix the failure and re-run.

## Recovery order

1. Restore `secrets/` (directory 0700, files 0600), `.env` and the override from the
   decrypted bundle **before any container starts** and before `just secrets-bootstrap`.
2. Check out the pinned deployment commit; confirm the digests in `.env` match the bundle.
3. Restore the data stores (the GrooveMap PostgreSQL database first).
4. `just deploy --no-wait`, then repeat the health checks. Run the MusicBrainz re-import if its
   schema was excluded from the backup.
