"""Which stores the stack runs itself, and the one way the deploy gate and its probes reach PostgreSQL.

`GROOVEMAP_POSTGRES` and `GROOVEMAP_VALKEY` (the shell, then .env, as Compose reads them) are
`local`, the default, or `external`. A local store is the stack's own container, addressed exactly
as before. An external store adds its overlay (`docker-compose.external-<store>.yml`) after the
production overlay: that overlay moves the local store into the `local-stores` profile, adds a
`store-check-<store>` one-shot from the same pinned image, and rewires the services to the
external server. Without either variable set to `external` no overlay is added, so the rendered
stack is byte for byte what it was before external stores existed.

Every PostgreSQL query of the deploy gate (scripts/discogs_import_status.py) and of deploy's
MusicBrainz latch probe (scripts/deploy_helpers.py) goes through `psql_argv`:

- local: `docker compose exec -T postgres`, psql over the container's trusted local socket;
- external: `docker compose run --rm --no-deps -T store-check-postgres`, psql from the pinned
  postgres image on the stack network, with the role and password read from the Docker secret
  files inside the container. No credential is ever on a command line.

Either way a failure is the caller's "cannot determine", never a guess. Stdlib only and Python
3.10, like the scripts that load it by path (tests/deploy/test_python310_compat.py).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path


LOCAL = "local"
EXTERNAL = "external"
MODES = (LOCAL, EXTERNAL)
STORES = ("postgres", "valkey")
MODE_VARIABLES = {"postgres": "GROOVEMAP_POSTGRES", "valkey": "GROOVEMAP_VALKEY"}
OVERLAYS = {"postgres": "docker-compose.external-postgres.yml", "valkey": "docker-compose.external-valkey.yml"}
STORE_CHECK_SERVICES = {"postgres": "store-check-postgres", "valkey": "store-check-valkey"}
# Every service that connects to each external store, in the overlay that rewires it. This is the one
# place the list lives: the homelab skill's override example and references are tested against it,
# and the overlays are tested to render exactly this set. A service on a different Docker network
# from the store cannot resolve its host, so an operator who attaches the store to an existing
# network must attach all of them. store-check-<store> is the deploy-time check, and
# store-check-postgres is also where the gate's and the latch probe's psql run (`psql_argv`).
STORE_CLIENTS = {
    "postgres": (
        "api",
        "brainztableinator",
        "dashboard",
        "embeddings",
        "insights",
        "schema-init",
        "store-check-postgres",
        "tableinator",
    ),
    "valkey": ("api", "dashboard", "insights", "store-check-valkey"),
}
# The profile an overlay moves a local store (and its exporter) into; nothing ever enables it.
LOCAL_STORES_PROFILE = "local-stores"
LOCAL_STORE_SERVICES = {"postgres": ("postgres", "postgres-exporter"), "valkey": ("valkey", "valkey-exporter")}
# The container whose presence says this stack has been started: its own postgres, or, with an
# external PostgreSQL, neo4j (which the gate needs as well).
STACK_CONTAINERS = {LOCAL: "groovemap-postgres", EXTERNAL: "groovemap-neo4j"}

# The credentials each external store reads from secrets/, written by the operator (create-secrets.sh
# would otherwise invent a random one that the server does not know).
EXTERNAL_SECRETS = {"postgres": ("postgres_username.txt", "postgres_password.txt"), "valkey": ("valkey_password.txt",)}
# The CA that signs each external server's certificate, mounted at /run/secrets/<name> as PGSSLROOTCERT
# or VALKEY_TLS_CA_FILE. Needed only when that store verifies its server; otherwise the overlay still
# mounts the file, so deploy leaves an empty placeholder that nothing reads.
CA_SECRETS = {"postgres": "postgres_ca.crt", "valkey": "valkey_ca.crt"}

# Transport security. libpq's sslmode: the verify modes check the server against the CA, `require`
# encrypts without checking who answers, and disable/allow/prefer may send everything, password
# included, in the clear.
SSLMODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")
VERIFYING_SSLMODES = ("verify-ca", "verify-full")
PLAINTEXT_SSLMODES = ("disable", "allow", "prefer")
DEFAULT_SSLMODE = "verify-full"
ALLOW_PLAINTEXT = {"postgres": "EXTERNAL_POSTGRES_ALLOW_PLAINTEXT", "valkey": "EXTERNAL_VALKEY_ALLOW_PLAINTEXT"}
PLAINTEXT_RISK = (
    "anyone on the network path between this host and {host} can read the {what} password and every query and answer, and can impersonate the server"
)
# Hosts whose traffic never leaves this machine: loopback, and the Docker host itself as the overlays
# map it (host.docker.internal -> host-gateway). A container name on a shared Docker network is not
# recognized: nothing here can tell that network from one that spans hosts.
LOCAL_HOSTS = frozenset({"host.docker.internal", "localhost", "::1", "[::1]"})
STRICT_TRUE = frozenset({"true", "1", "yes", "on"})
STRICT_FALSE = frozenset({"false", "0", "no", "off"})

# Bare-Compose safety: with an external store, deploy writes COMPOSE_FILE (Compose reads it from .env)
# so `docker compose up/ps/logs/exec` with no -f uses exactly deploy's files, overlays included, and
# can never start the local stores and point the services at them. The marker says deploy owns it.
COMPOSE_FILE_VARIABLE = "COMPOSE_FILE"
COMPOSE_FILE_MARKER = "# COMPOSE_FILE is written by just deploy while a store is external, so bare docker compose uses its files. Do not edit."
# Databases GrooveMap never uses on a shared server: the maintenance database and the templates.
RESERVED_DATABASES = frozenset({"postgres", "template0", "template1"})
DATABASE_NAME = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]{0,62}\Z")
HOST_NAME = re.compile(r"\A[A-Za-z0-9_.:\[\]-]+\Z")
# python-libraries' valkey_key_prefix rule: letters, digits and _-.:, and more than colons.
KEY_PREFIX = re.compile(r"\A[A-Za-z0-9_.:-]+\Z")
VALKEY_MAX_DB = 255

# Run inside the postgres container (local) or the store-check-postgres one-shot (external). The
# production overlay unsets POSTGRES_USER and mounts the username as a secret file; the base file
# sets it directly. Locally psql connects over the socket the official image trusts, so no
# password is involved. The one-shot sets PGHOST, so psql connects over the network and reads the
# password from the secret file into PGPASSWORD. The query is `$1`.
PSQL_SHELL = (
    'u="${POSTGRES_USER:-}"; '
    'if [ -z "$u" ] && [ -n "${POSTGRES_USER_FILE:-}" ]; then u="$(cat "$POSTGRES_USER_FILE")"; fi; '
    'if [ -z "$u" ]; then echo "no PostgreSQL user: neither POSTGRES_USER nor POSTGRES_USER_FILE is set" >&2; exit 2; fi; '
    'if [ -n "${PGHOST:-}" ] && [ -n "${POSTGRES_PASSWORD_FILE:-}" ]; then PGPASSWORD="$(cat "$POSTGRES_PASSWORD_FILE")"; export PGPASSWORD; fi; '
    'exec psql -X -U "$u" -d "${POSTGRES_DB:-groovemap}" -v ON_ERROR_STOP=1 -tAq -c "$1"'
)


ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=(?P<value>.*)$")


def _unquote(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    # Compose's dotenv reader drops an unquoted inline comment that follows whitespace.
    return re.split(r"\s+#", value, maxsplit=1)[0].strip()


def parse_env(text: str) -> dict[str, str]:
    """Read KEY=VALUE assignments the way Compose's .env reader does, ignoring comments."""
    values = {}
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        match = ASSIGNMENT.match(line)
        if match:
            values[match["key"]] = _unquote(match["value"])
    return values


def read_env(path: Path) -> dict[str, str]:
    return parse_env(path.read_text()) if path.exists() else {}


class StoreConfigError(ValueError):
    """The store settings are not ones this deployment can run with."""


@dataclass(frozen=True)
class Stores:
    """Where each store lives: LOCAL (the stack's own container) or EXTERNAL."""

    postgres: str = LOCAL
    valkey: str = LOCAL

    def mode(self, store: str) -> str:
        return self.postgres if store == "postgres" else self.valkey

    @property
    def external(self) -> tuple[str, ...]:
        return tuple(store for store in STORES if self.mode(store) == EXTERNAL)

    @property
    def overlays(self) -> list[str]:
        """The overlay files to add, in order, after the production overlay and before the operator's override."""
        return [OVERLAYS[store] for store in self.external]

    @property
    def stack_container(self) -> str:
        return STACK_CONTAINERS[self.postgres]


def _effective(key: str, env_values: Mapping[str, str], environ: Mapping[str, str]) -> str:
    """Compose's precedence: the shell environment, then .env."""
    return environ.get(key, env_values.get(key, "")).strip()


def read_stores(env_values: Mapping[str, str], environ: Mapping[str, str]) -> Stores:
    """The configured modes; an empty or missing variable is LOCAL. Anything else is refused, never guessed."""
    modes = {}
    for store, variable in MODE_VARIABLES.items():
        value = _effective(variable, env_values, environ) or LOCAL
        if value not in MODES:
            raise StoreConfigError(f"{variable}={value!r} is not a store mode; use local (the stack's own {store}) or external")
        modes[store] = value
    return Stores(**modes)


def is_local_host(host: str) -> bool:
    """True when traffic to `host` from a stack container never leaves this machine (LOCAL_HOSTS or 127.0.0.0/8)."""
    host = host.strip().lower()
    return host in LOCAL_HOSTS or bool(re.fullmatch(r"127(?:\.\d{1,3}){3}", host))


def strict_flag(key: str, env_values: Mapping[str, str], environ: Mapping[str, str], default: bool) -> bool | None:
    """A security flag read strictly (python-libraries' rule): true/1/yes/on, false/0/no/off, unset is `default`, anything else None."""
    value = _effective(key, env_values, environ).lower()
    if not value:
        return default
    return True if value in STRICT_TRUE else False if value in STRICT_FALSE else None


def sslmode(env_values: Mapping[str, str], environ: Mapping[str, str]) -> str:
    return _effective("EXTERNAL_POSTGRES_SSLMODE", env_values, environ) or DEFAULT_SSLMODE


def encrypts(store: str, env_values: Mapping[str, str], environ: Mapping[str, str]) -> bool:
    """Whether the connection to the external `store` is encrypted at all."""
    if store == "postgres":
        return sslmode(env_values, environ) not in PLAINTEXT_SSLMODES
    return strict_flag("EXTERNAL_VALKEY_TLS", env_values, environ, default=True) is True


def verifies(store: str, env_values: Mapping[str, str], environ: Mapping[str, str]) -> bool:
    """Whether the external `store`'s certificate is checked against the CA in CA_SECRETS."""
    if store == "postgres":
        return sslmode(env_values, environ) in VERIFYING_SSLMODES
    return encrypts(store, env_values, environ)


def transport_problems(stores: Stores, env_values: Mapping[str, str], environ: Mapping[str, str]) -> list[str]:
    """Refuse a cleartext connection to a server off this machine unless its ALLOW_PLAINTEXT opt-out is set."""
    problems = []
    if "postgres" in stores.external:
        mode = sslmode(env_values, environ)
        if mode not in SSLMODES:
            problems.append(f"EXTERNAL_POSTGRES_SSLMODE={mode!r} is not a libpq sslmode ({', '.join(SSLMODES)})")
    if "valkey" in stores.external:
        for flag in ("EXTERNAL_VALKEY_TLS", "EXTERNAL_VALKEY_TLS_VERIFY_HOSTNAME"):
            if strict_flag(flag, env_values, environ, default=True) is None:
                problems.append(f"{flag}={_effective(flag, env_values, environ)!r} must be true or false (also 1/0, yes/no, on/off)")
    if problems:
        return problems  # the cleartext rule below needs readable settings
    for store in stores.external:
        flag = ALLOW_PLAINTEXT[store]
        allowed = strict_flag(flag, env_values, environ, default=False)
        if allowed is None:
            problems.append(f"{flag}={_effective(flag, env_values, environ)!r} must be true or false")
            continue
        host = _effective(f"EXTERNAL_{store.upper()}_HOST", env_values, environ)
        if host and not encrypts(store, env_values, environ) and not is_local_host(host) and not allowed:
            what = "PostgreSQL role's" if store == "postgres" else "Valkey"
            setting = f"EXTERNAL_POSTGRES_SSLMODE={sslmode(env_values, environ)}" if store == "postgres" else "EXTERNAL_VALKEY_TLS=false"
            problems.append(
                f"{setting} sends credentials and data to {host} in the clear: {PLAINTEXT_RISK.format(host=host, what=what)}. "
                f"Turn TLS on, or set {flag}=true to accept that risk"
            )
    return problems


def transport_warnings(stores: Stores, env_values: Mapping[str, str], environ: Mapping[str, str]) -> list[str]:
    """What the operator accepted about transport security, said out loud on every run."""
    warnings = []
    for store in stores.external:
        host = _effective(f"EXTERNAL_{store.upper()}_HOST", env_values, environ)
        name = "PostgreSQL" if store == "postgres" else "Valkey"
        if not encrypts(store, env_values, environ) and not is_local_host(host):
            what = "PostgreSQL role's" if store == "postgres" else "Valkey"
            warnings.append(
                f"{name} at {host} is reached in the clear ({ALLOW_PLAINTEXT[store]}=true): {PLAINTEXT_RISK.format(host=host, what=what)}"
            )
        elif store == "postgres" and sslmode(env_values, environ) == "require":
            warnings.append(
                f"PostgreSQL at {host} is encrypted but not verified (EXTERNAL_POSTGRES_SSLMODE=require): a server that impersonates it is not detected"
            )
        elif (
            store == "valkey"
            and encrypts(store, env_values, environ)
            and strict_flag("EXTERNAL_VALKEY_TLS_VERIFY_HOSTNAME", env_values, environ, default=True) is False
        ):
            warnings.append(f"Valkey at {host}: the certificate chain is verified but not the host name (EXTERNAL_VALKEY_TLS_VERIFY_HOSTNAME=false)")
    return warnings


def setting_problems(stores: Stores, env_values: Mapping[str, str], environ: Mapping[str, str]) -> list[str]:
    """What is wrong with the operator's settings for each external store (empty when they are usable)."""
    problems = []

    def get(key: str) -> str:
        return _effective(key, env_values, environ)

    def port(key: str) -> None:
        value = get(key)
        if value and not (value.isdigit() and 0 < int(value) < 65536):
            problems.append(f"{key}={value!r} is not a port number")

    def host(key: str, store: str) -> None:
        value = get(key)
        if not value:
            problems.append(f"{key} is not set; name the host of the existing {store} server")
        elif not HOST_NAME.match(value):
            problems.append(f"{key}={value!r} is not a host name or address")

    if "postgres" in stores.external:
        host("EXTERNAL_POSTGRES_HOST", "PostgreSQL")
        port("EXTERNAL_POSTGRES_PORT")
        database = get("EXTERNAL_POSTGRES_DATABASE") or "groovemap"
        if not DATABASE_NAME.match(database):
            problems.append(f"EXTERNAL_POSTGRES_DATABASE={database!r} is not a plain database name (letters, digits and _)")
        elif database in RESERVED_DATABASES:
            problems.append(f"EXTERNAL_POSTGRES_DATABASE={database!r} is a server database; GrooveMap needs a database of its own")
    if "valkey" in stores.external:
        host("EXTERNAL_VALKEY_HOST", "Valkey")
        port("EXTERNAL_VALKEY_PORT")
        db = get("EXTERNAL_VALKEY_DB") or "0"
        if not (db.isdigit() and int(db) <= VALKEY_MAX_DB):
            problems.append(f"EXTERNAL_VALKEY_DB={db!r} is not a Valkey database number (0-{VALKEY_MAX_DB})")
        prefix = get("EXTERNAL_VALKEY_KEY_PREFIX")
        if not prefix:
            problems.append(
                "EXTERNAL_VALKEY_KEY_PREFIX is not set; every GrooveMap key on a shared Valkey needs a prefix of its own, such as groovemap:"
            )
        elif not KEY_PREFIX.match(prefix) or not prefix.strip(":"):
            problems.append(f"EXTERNAL_VALKEY_KEY_PREFIX={prefix!r} may only use letters, digits and _-.: and must be more than colons")
    return problems + transport_problems(stores, env_values, environ)


def version_tuple(version: str) -> tuple[int, ...] | None:
    """`0.5.0` or `v0.5.0` as (0, 5, 0); None for anything else, `unreleased` included."""
    match = re.fullmatch(r"v?(\d+(?:\.\d+)*)", version.strip())
    return tuple(int(part) for part in match[1].split(".")) if match else None


def floor_problems(
    store: str,
    floors: Mapping[str, Mapping[str, str]],
    released_versions: Mapping[str, str],
    released_digests: Mapping[str, str],
    owners: Mapping[str, str],
    env_values: Mapping[str, str],
    environ: Mapping[str, str],
) -> list[str]:
    """Why an external `store` cannot run on this deployment's images yet (empty when the floor is met).

    A floor is met when the reviewed release set (RELEASED_IMAGE_DIGESTS and the versions beside
    it) pins, for every image the floor names, a version at or above it, and the operator's .env
    runs exactly that reviewed digest. A floor that is not a version (UNRELEASED) is never met.
    """
    problems = []
    for variable, floor in floors.get(store, {}).items():
        repository = owners.get(variable, variable)
        needed = version_tuple(floor)
        released = released_versions.get(variable, "")
        have = version_tuple(released)
        if needed is None:
            problems.append(f"{repository} has no release with external-{store} support yet (capability floor: {floor})")
            continue
        if have is None or have < needed:
            problems.append(
                f"the reviewed release set pins {repository} {released or 'with no version'}, below v{floor}, the first with external-{store} support"
            )
            continue
        pinned = _effective(variable, env_values, environ).rpartition("@sha256:")[2]
        if pinned != released_digests.get(variable):
            problems.append(
                f"{variable} in .env is not the reviewed {repository} v{released}; run `just env-init` to move .env to the reviewed digests"
            )
    return problems


def psql_argv(compose_argv: Sequence[str], sql: str, stores: Stores) -> list[str]:
    """The command that runs one read-only query against the stack's PostgreSQL, wherever it lives."""
    if stores.postgres == EXTERNAL:
        return [*compose_argv, "run", "--rm", "--no-deps", "-T", STORE_CHECK_SERVICES["postgres"], "sh", "-c", PSQL_SHELL, "sh", sql]
    return [*compose_argv, "exec", "-T", "postgres", "sh", "-c", PSQL_SHELL, "sh", sql]
