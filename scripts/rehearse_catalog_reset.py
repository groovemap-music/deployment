#!/usr/bin/env python3
"""A full rehearsal of docs/catalog-reset.md on a disposable stack: `just rehearse-catalog-reset`.

It builds lux's shape on real Docker, loads a small dump through the released extractor and
loaders, and then runs scripts/catalog_reset.py, the same tool and stages the runbook gives the
operator, through reset, forced re-ingest, rollback, a second reset and finish.

- A shared PostgreSQL 18 with TLS (config/homelab-smoke/compose.yml) holding GrooveMap's database
  beside two neighbour databases and their role. A neighbour session stays open throughout.
- The Lux Compose file set: base, prod, external-postgres and an override, in its own project.
- rehearsal_discogs_mirror.py as data.discogs.com, so the real download path runs.

Everything is removed on every exit, by Compose label, and nothing of another project is touched.
It refuses to start where groovemap-* containers exist, because the stack's names are fixed. Each
wait has a deadline and a timeout is a failure. Images are linux/amd64, so an arm64 host runs them
under emulation. It starts containers, so it is never part of `just check`.

Writes REPORT.json and logs to --evidence (kept) and prints PASS/FAIL lines.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from smoke_homelab import provisioning_sql  # noqa: E402


VERSION = "20261101"
ADMIN_EMAIL = "rehearsal-admin@example.com"
PG_CLIENTS = (
    "api",
    "brainztableinator",
    "dashboard",
    "embeddings",
    "insights",
    "schema-init",
    "similar-artists",
    "store-check-postgres",
    "tableinator",
)
GROOVEMAP_IMAGES = ("schema-init", "api", "dashboard", "explore", "insights", "extractor-discogs", "tableinator", "graphinator")
PUBLISHED = ("rabbitmq", "neo4j", "valkey", "api", "dashboard", "explore")
MONITORING = ("grafana", "victoria-metrics", "victoria-traces", "cadvisor", "node-exporter")
CONSUMERS = ("tableinator", "graphinator")
APPLICATIONS = ("api", "dashboard", "explore")
REPORT_SERVICES = ("schema-init", "api", "tableinator", "graphinator", "extractor-discogs", "insights", "neo4j", "rabbitmq", "valkey")
MIRROR_IMAGE = "python:3.14.7-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6"


class RehearsalError(RuntimeError):
    """An assertion or a deadline failed."""


# --- the small dump -------------------------------------------------------------------------


def tool(*argv: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    """A best-effort command for setup checks and teardown: never raises on a nonzero exit."""
    return subprocess.run(argv, capture_output=True, text=True, check=False, timeout=timeout)  # noqa: S603


def _people(index: int) -> str:
    return f"<artist><id>{index}</id><name>Rehearsal Artist {index}</name><anv></anv><join></join><role></role><tracks></tracks></artist>"


def dump_files(artists: int = 80, labels: int = 24, masters: int = 40, releases: int = 160) -> dict[str, bytes]:
    """Four monthly files in Discogs' shape. Deterministic: the same bytes on every run."""
    genres = ("Electronic", "Rock", "Jazz")
    styles = ("Techno", "House", "Ambient", "Krautrock", "Bop")
    artist_rows = []
    for index in range(1, artists + 1):
        member = f'<members><id>{index + 1}</id><name id="{index + 1}">Rehearsal Artist {index + 1}</name></members>' if index % 10 == 0 else ""
        alias = f'<aliases><name id="{index - 1}">Rehearsal Artist {index - 1}</name></aliases>' if index % 7 == 0 else ""
        artist_rows.append(
            f"<artist><id>{index}</id><name>Rehearsal Artist {index}</name><realname>Real Name {index}</realname>"
            f"<profile>Profile {index}</profile><data_quality>Correct</data_quality>"
            f"<namevariations><name>R. Artist {index}</name></namevariations>{alias}{member}</artist>"
        )
    label_rows = []
    for index in range(1, labels + 1):
        parent = f'<parentLabel id="{index - 1}">Rehearsal Label {index - 1}</parentLabel>' if index % 6 == 0 else ""
        label_rows.append(
            f"<label><id>{index}</id><name>Rehearsal Label {index}</name><contactinfo>c{index}</contactinfo>"
            f"<profile>Label {index}</profile><data_quality>Correct</data_quality>{parent}</label>"
        )
    master_rows = []
    for index in range(1, masters + 1):
        master_rows.append(
            f'<master id="{index}"><main_release>{index}</main_release><artists>{_people((index % artists) + 1)}</artists>'
            f"<genres><genre>{genres[index % 3]}</genre></genres><styles><style>{styles[index % 5]}</style></styles>"
            f"<year>{1970 + index % 50}</year><title>Rehearsal Master {index}</title><data_quality>Correct</data_quality></master>"
        )
    release_rows = []
    for index in range(1, releases + 1):
        master = f'<master_id is_main_release="{"true" if index <= masters else "false"}">{(index % masters) + 1}</master_id>'
        release_rows.append(
            f'<release id="{index}" status="Accepted"><artists>{_people((index % artists) + 1)}</artists>'
            f"<title>Rehearsal Release {index}</title>"
            f'<labels><label name="Rehearsal Label {(index % labels) + 1}" catno="RH-{index}" id="{(index % labels) + 1}"/></labels>'
            f"<extraartists><artist><id>{((index + 3) % artists) + 1}</id><name>Rehearsal Artist {((index + 3) % artists) + 1}</name>"
            f"<anv></anv><join></join><role>Producer</role><tracks></tracks></artist></extraartists>"
            f'<formats><format name="Vinyl" qty="1" text=""><descriptions><description>LP</description></descriptions></format></formats>'
            f"<genres><genre>{genres[index % 3]}</genre></genres><styles><style>{styles[index % 5]}</style></styles>"
            f"<country>UK</country><released>{1970 + index % 50}-01-01</released><data_quality>Correct</data_quality>{master}"
            f"<tracklist><track><position>A1</position><title>Track {index}</title><duration>3:30</duration></track></tracklist>"
            f'<identifiers><identifier type="Barcode" value="{9990000000000 + index}"/></identifiers>'
            f"<companies><company><id>{(index % 12) + 1}</id><name>Rehearsal Pressing {(index % 12) + 1}</name><catno></catno>"
            f"<entity_type>17</entity_type><entity_type_name>Pressed By</entity_type_name></company></companies></release>"
        )
    bodies = {
        "artists": "<artists>" + "".join(artist_rows) + "</artists>",
        "labels": "<labels>" + "".join(label_rows) + "</labels>",
        "masters": "<masters>" + "".join(master_rows) + "</masters>",
        "releases": "<releases>" + "".join(release_rows) + "</releases>",
    }
    files = {f"discogs_{VERSION}_{kind}.xml.gz": gzip.compress(body.encode(), mtime=0) for kind, body in bodies.items()}
    checksums = "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in sorted(files.items()))
    files[f"discogs_{VERSION}_CHECKSUM.txt"] = checksums.encode()
    return files


# --- the override ---------------------------------------------------------------------------


def override(network: str, subnet: str, ca: str) -> str:
    """Lux's override in miniature, plus the mirror. Generated, so it can name this run's network."""
    lines = ["services:"]
    services: dict[str, list[str]] = {}
    for service in PUBLISHED:
        services.setdefault(service, []).append("    ports: !override []")
    for service in GROOVEMAP_IMAGES:
        services.setdefault(service, []).append("    platform: linux/amd64")
    for service in PG_CLIENTS:
        services.setdefault(service, []).append("    networks:\n      homelab: {}\n      groovemap: {}")
    for service in MONITORING:
        body = "    deploy:\n      replicas: 0"
        if service == "grafana":
            body += "\n    depends_on: !reset {}"
        services.setdefault(service, []).append(body)
    services.setdefault("otel-collector", []).append("    depends_on: !reset {}")
    services.setdefault("api", []).append(
        '    environment:\n      IDENTITY_AUTO_REATTACH_INTERVAL: "10"\n      IDENTITY_LEDGER_REFRESH_INTERVAL: "30"'
    )
    services.setdefault("extractor-discogs", []).append(
        f"    environment:\n      SSL_CERT_FILE: /rehearsal/tls/mirror-ca.crt\n    volumes:\n      - {ca}:/rehearsal/tls:ro"
    )
    for name, bodies in services.items():
        lines.append(f"  {name}:")
        lines += bodies
    lines += [
        "  discogs-mirror:",
        f"    image: {MIRROR_IMAGE}",
        "    container_name: groovemap-rehearsal-mirror",
        '    entrypoint: ["python3", "/rehearsal/mirror.py", "/rehearsal/dump", "/rehearsal/tls/mirror.crt", "/rehearsal/tls/mirror.key"]',
        "    volumes:",
        f"      - {ca}:/rehearsal/tls:ro",
        f"      - {Path(ca).parent}/dump:/rehearsal/dump:ro",
        f"      - {Path(ca).parent}/mirror.py:/rehearsal/mirror.py:ro",
        "    networks:",
        "      groovemap:",
        "        aliases:",
        "          - data.discogs.com",
        "    restart: unless-stopped",
        "",
        "networks:",
        "  homelab:",
        "    external: true",
        f"    name: {network}",
        "  groovemap:",
        "    ipam: !override",
        "      config:",
        f'        - subnet: "{subnet}"',
        "",
    ]
    return "\n".join(lines)


# --- the run --------------------------------------------------------------------------------


class Rehearsal:
    def __init__(self, evidence: Path, keep_going_seconds: int) -> None:
        self.run_id = f"{int(time.time())}-{os.getpid()}"
        self.project = f"gm-reset-rehearsal-{self.run_id}"
        self.pg_project = f"{self.project}-pg"
        self.network = f"{self.project}-homelab"
        self.evidence = evidence
        self.deadline = time.monotonic() + keep_going_seconds
        # Under the home directory: Docker on a VM (Colima) bind-mounts only that.
        cache = Path.home() / ".cache"
        cache.mkdir(exist_ok=True)
        self.work = Path(tempfile.mkdtemp(prefix="gm-reset-rehearsal.", dir=cache))
        self.stack = self.work / "stack"
        self.tls = self.work / "tls"
        self.report: dict[str, Any] = {"run": self.run_id, "checks": [], "timings_s": {}, "stages_s": {}}
        self.admin_password = secrets.token_urlsafe(24)
        self.pg_password = secrets.token_urlsafe(24)
        self.pg_container = ""
        self.state = self.work / "state"
        self.pg_env = {
            **os.environ,
            "SMOKE_HOMELAB_TLS_DIR": str(self.tls),
            "SMOKE_HOMELAB_ADMIN_PASSWORD_FILE": str(self.work / "admin-password"),
            "SMOKE_HOMELAB_NETWORK": self.network,
            "SMOKE_HOMELAB_MONITORING_NETWORK": f"{self.project}-monitoring",
        }
        self.tool_env = {**os.environ, "CATALOG_RESET_ADMIN_USER": "homelab_admin"}
        for name in ("COMPOSE_FILE", "COMPOSE_PROFILES", "COMPOSE_PROJECT_NAME"):
            self.tool_env.pop(name, None)

    def say(self, text: str) -> None:
        print(f"\n==> {text}", flush=True)

    def check(self, condition: bool, text: str) -> None:
        self.report["checks"].append({"ok": bool(condition), "check": text})
        print(f"{'PASS' if condition else 'FAIL'} {text}", flush=True)
        self.flush_report()
        if not condition:
            raise RehearsalError(text)

    def flush_report(self) -> None:
        (self.evidence / "REPORT.json").write_text(json.dumps(self.report, indent=2) + "\n")

    def run(
        self,
        argv: list[str],
        *,
        timeout: int = 300,
        cwd: Path | None = None,
        stdin: str | None = None,
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise RehearsalError("the rehearsal's overall deadline passed")
        try:
            result = subprocess.run(  # noqa: S603
                argv,
                cwd=cwd or self.stack,
                input=stdin,
                text=True,
                capture_output=True,
                timeout=min(timeout, remaining),
                check=False,
                env=env or self.tool_env,
            )
        except subprocess.TimeoutExpired as error:
            raise RehearsalError(f"timed out: {' '.join(argv[:8])}") from error
        if check and result.returncode != 0:
            raise RehearsalError(f"exit {result.returncode}: {' '.join(argv[:10])}\n{result.stdout[-1500:]}\n{result.stderr[-1500:]}")
        return result

    def compose(self, *args: str, timeout: int = 600, check: bool = True) -> subprocess.CompletedProcess[str]:
        return self.run(["docker", "compose", *args], timeout=timeout, check=check)

    def admin_sql(self, database: str, sql: str, user: str = "homelab_admin") -> str:
        return self.run(
            ["docker", "exec", "-i", self.pg_container, "psql", "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", "-U", user, "-d", database, "-c", sql]
        ).stdout.strip()

    def cypher(self, query: str) -> str:
        script = 'cypher-shell -u neo4j -p "$(cat /run/secrets/neo4j_password)" --format plain "$1"'
        return self.compose("exec", "-T", "neo4j", "sh", "-c", script, "sh", query, timeout=120).stdout.strip()

    def reset(self, stage: str, *extra: str, apply: bool = True, check: bool = True, state: Path | None = None) -> subprocess.CompletedProcess[str]:
        # The floor is checked on the day; this rehearses whichever set is promoted now (see the runbook).
        argv = [sys.executable, "scripts/catalog_reset.py", stage, "--admin-container", self.pg_container, "--min-free-gb", "1"]
        if stage == "preflight":
            argv.append("--rehearsal-skip-release-floor")
        if state:
            argv += ["--state-dir", str(state)]
        if apply:
            argv.append("--apply")
        started = time.monotonic()
        result = self.run([*argv, *extra], timeout=2400, check=False)
        elapsed = round(time.monotonic() - started, 1)
        self.report["stages_s"].setdefault(stage, []).append(elapsed)
        (self.evidence / "stages.log").open("a").write(f"\n### {stage} ({elapsed} s, exit {result.returncode})\n{result.stdout}{result.stderr}")
        if check and result.returncode != 0:
            raise RehearsalError(f"catalog-reset {stage} exit {result.returncode}\n{result.stdout[-2000:]}\n{result.stderr[-2000:]}")
        return result

    def wait(self, what: str, probe: Any, seconds: int, interval: float = 5) -> None:
        deadline = min(time.monotonic() + seconds, self.deadline)
        while time.monotonic() < deadline:
            if probe():
                return
            time.sleep(interval)
        raise RehearsalError(f"deadline: {what} after {seconds}s")

    def gate(self, seconds: int = 900) -> None:
        argv = [
            sys.executable,
            "scripts/discogs_import_status.py",
            "--prod",
            "-f",
            "docker-compose.override.yml",
            "--wait",
            "--interval",
            "10",
            "--timeout",
            str(seconds),
        ]
        result = self.run(argv, timeout=seconds + 60, check=False)
        (self.evidence / "gate.log").open("a").write(result.stdout + result.stderr)
        self.check(result.returncode == 0, f"gate exit {result.returncode} (0 is complete)")

    def api(self, method: str, path: str, *, body: dict[str, Any] | None = None, token: str = "") -> tuple[int, str]:
        code = (
            "import json,os,sys,urllib.request,urllib.error\n"
            "r=urllib.request.Request('http://localhost:8004'+sys.argv[2],method=sys.argv[1],data=sys.stdin.buffer.read() or None,"
            "headers={'Content-Type':'application/json',**({'Authorization':'Bearer '+os.environ['T']} if os.environ.get('T') else {})})\n"
            "try:\n  s=urllib.request.urlopen(r,timeout=30); print(s.status); print(s.read().decode())\n"
            "except urllib.error.HTTPError as e:\n  print(e.code); print(e.read().decode())\n"
        )
        result = self.run(
            ["docker", "exec", "-i", "-e", f"T={token}", "groovemap-api", "python", "-c", code, method, path],
            stdin=json.dumps(body) if body else "",
            timeout=60,
        )
        status, _, text = result.stdout.partition("\n")
        return int(status), text

    def login(self) -> str:
        status, text = self.api("POST", "/api/admin/auth/login", body={"email": ADMIN_EMAIL, "password": self.admin_password})
        self.check(status == 200, f"the kept admin signs in with its original password (HTTP {status})")
        return str(json.loads(text)["access_token"])

    # --- construction ---

    def build(self) -> None:
        self.say("Building the shared PostgreSQL and the stack")
        listed = tool("docker", "ps", "-a", "--format", "{{.Names}}", timeout=30).stdout.split()
        taken = [
            name for name in listed if re.fullmatch(r"groovemap-(postgres|neo4j|rabbitmq|valkey|api|tableinator|graphinator|extractor-discogs)", name)
        ]
        if taken:
            raise RehearsalError(f"these fixed container names are in use: {taken}")
        self.stack.mkdir()
        self.tls.mkdir()
        files = self.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=ROOT).stdout.splitlines()
        for name in filter(None, files):
            source = ROOT / name
            if source.is_file():
                target = self.stack / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        openssl = lambda *args: self.run(["openssl", *args], cwd=self.tls)  # noqa: E731
        openssl(
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "2",
            "-subj",
            "/CN=rehearsal-ca",
            "-keyout",
            "mirror-ca.key",
            "-out",
            "mirror-ca.crt",
        )
        for name, host in (("server", "homelab-postgres"), ("mirror", "data.discogs.com")):
            extension = self.tls / f"{name}.ext"
            extension.write_text(f"subjectAltName=DNS:{host}\n")
            openssl("req", "-newkey", "rsa:2048", "-nodes", "-subj", f"/CN={host}", "-keyout", f"{name}.key", "-out", f"{name}.csr")
            openssl(
                "x509",
                "-req",
                "-in",
                f"{name}.csr",
                "-CA",
                "mirror-ca.crt",
                "-CAkey",
                "mirror-ca.key",
                "-CAcreateserial",
                "-days",
                "2",
                "-extfile",
                str(extension),
                "-out",
                f"{name}.crt",
            )
        (self.tls / "mirror-ca.key").unlink()
        for path in self.tls.iterdir():
            path.chmod(0o644)
        (self.work / "admin-password").write_text(secrets.token_urlsafe(24))
        dump = self.work / "dump"
        dump.mkdir()
        for name, data in dump_files().items():
            (dump / name).write_bytes(data)
        shutil.copy2(ROOT / "scripts" / "rehearsal_discogs_mirror.py", self.work / "mirror.py")
        pg = ["docker", "compose", "-p", self.pg_project, "-f", "config/homelab-smoke/compose.yml"]
        self.run([*pg, "up", "-d", "--wait", "--quiet-pull", "homelab-postgres"], timeout=300, env=self.pg_env)
        self.pg_container = self.run([*pg, "ps", "--format", "{{.Name}}", "homelab-postgres"], env=self.pg_env).stdout.strip()
        provision = provisioning_sql((self.stack / "docs" / "homelab.md").read_text(), self.pg_password)
        self.run(
            ["docker", "exec", "-i", self.pg_container, "psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-U", "homelab_admin", "-d", "postgres"],
            stdin=provision,
        )
        # Lux's database also has homelab's narrower access list (its docs/guides/groovemap.md).
        self.admin_sql("postgres", "REVOKE ALL ON DATABASE groovemap FROM PUBLIC")
        self.admin_sql("groovemap", "REVOKE ALL ON SCHEMA public FROM PUBLIC")
        self.admin_sql("groovemap", "GRANT USAGE, CREATE ON SCHEMA public TO groovemap")
        neighbours = (
            "CREATE ROLE neighbour LOGIN PASSWORD 'neighbour';"
            "CREATE DATABASE phaze_neighbour OWNER neighbour;"
            "CREATE DATABASE synapse_neighbour OWNER neighbour TEMPLATE template0 LOCALE 'C';"
        )
        for statement in neighbours.split(";"):
            if statement:
                self.admin_sql("postgres", statement)
        for database in ("phaze_neighbour", "synapse_neighbour"):
            self.admin_sql(
                database, "CREATE TABLE IF NOT EXISTS public.neighbour_rows AS SELECT g AS id, md5(g::text) AS body FROM generate_series(1, 5000) g"
            )
        self.run(
            [
                "docker",
                "exec",
                "-d",
                "-e",
                "PGAPPNAME=rehearsal-neighbour",
                self.pg_container,
                "psql",
                "-U",
                "neighbour",
                "-d",
                "phaze_neighbour",
                "-c",
                "SELECT pg_sleep(86400)",
            ]
        )
        env_lines = self.run([sys.executable, "scripts/env-init.py"], timeout=60)
        (self.evidence / "env-init.log").write_text(env_lines.stdout)
        subnet = f"172.{31 + (os.getpid() % 3)}.0.0/16"
        (self.stack / "docker-compose.override.yml").write_text(override(self.network, subnet, str(self.tls)))
        settings = {
            "COMPOSE_PROJECT_NAME": self.project,
            "COMPOSE_FILE": "docker-compose.yml:docker-compose.prod.yml:docker-compose.external-postgres.yml:docker-compose.override.yml",
            "APP_BASE_URL": "http://localhost:8006",
            "UID": str(os.getuid()),
            "GID": str(os.getgid()),
            "GROOVEMAP_POSTGRES": "external",
            "EXTERNAL_POSTGRES_HOST": "homelab-postgres",
            "EXTERNAL_POSTGRES_DATABASE": "groovemap",
            "EXTERNAL_POSTGRES_SSLMODE": "verify-full",
            "NEO4J_HEAP_SIZE": "768m",
            "NEO4J_PAGECACHE_SIZE": "256m",
            "NEO4J_TRANSACTION_MEMORY_MAX": "512m",
        }
        environment = self.stack / ".env"
        kept = [line for line in environment.read_text().splitlines() if line.split("=", 1)[0] not in settings]
        environment.write_text("\n".join([*kept, *(f"{key}={value}" for key, value in settings.items())]) + "\n")
        secrets_dir = self.stack / "secrets"
        secrets_dir.mkdir(mode=0o700, exist_ok=True)
        for name, value in (("postgres_username.txt", "groovemap"), ("postgres_password.txt", self.pg_password)):
            path = secrets_dir / name
            path.write_text(value)
            path.chmod(0o600)
        shutil.copy2(self.tls / "mirror-ca.crt", secrets_dir / "postgres_ca.crt")
        self.run(["bash", "scripts/create-secrets.sh"], timeout=60)
        # What this rehearsal vouches for: `preflight --rehearsal-report` checks both on the day.
        self.report["commit"] = self.run(["git", "rev-parse", "HEAD"], cwd=ROOT).stdout.strip()
        self.report["dirty"] = self.run(["git", "status", "--porcelain"], cwd=ROOT).stdout.strip() != ""
        services = json.loads(self.compose("config", "--format", "json", timeout=60).stdout)["services"]
        self.report["images"] = {service: services[service]["image"] for service in REPORT_SERVICES}
        # A VM's file sharing (Colima virtiofs) does not show the host owner to the container, so a
        # 0600 bind mount is unreadable as the same UID. Throwaway values in a throwaway copy.
        if sys.platform == "darwin":
            secrets_dir.chmod(0o755)
            for path in secrets_dir.iterdir():
                path.chmod(0o644)

    # --- phases ---

    def neighbours(self) -> dict[str, str]:
        hashes = {}
        for database in ("phaze_neighbour", "synapse_neighbour"):
            dump = self.run(["docker", "exec", self.pg_container, "pg_dump", "-U", "homelab_admin", "-d", database]).stdout
            body = "\n".join(line for line in dump.splitlines() if not line.startswith(("\\restrict", "\\unrestrict")))
            hashes[database] = hashlib.sha256(body.encode()).hexdigest()
        hashes["session"] = self.admin_sql(
            "postgres", "SELECT pid, backend_start FROM pg_stat_activity WHERE application_name = 'rehearsal-neighbour'"
        )
        return hashes

    def catalog(self) -> dict[str, str]:
        sql = "SELECT count(*), md5(coalesce(string_agg(id::text, ',' ORDER BY id), '')) FROM public.catalog_items"
        return {
            "catalog_items": self.admin_sql("groovemap", sql),
            "artists": self.admin_sql("groovemap", "SELECT count(*) FROM public.artists"),
            "labels": self.admin_sql("groovemap", "SELECT count(*) FROM public.labels"),
            "masters": self.admin_sql("groovemap", "SELECT count(*) FROM public.masters"),
            "releases": self.admin_sql("groovemap", "SELECT count(*) FROM public.releases"),
            "provider_aliases": self.admin_sql("groovemap", "SELECT count(*) FROM public.provider_aliases"),
            "graph_person": self.admin_sql("groovemap", "SELECT count(*) FROM graph.person"),
            "neo4j_nodes": self.cypher("MATCH (n) RETURN labels(n)[0], count(*) ORDER BY labels(n)[0]"),
            "neo4j_gm_ids": self.cypher("MATCH (n:Release) WHERE n.gm_id IS NOT NULL RETURN count(n)").split()[-1],
            "neo4j_first_gm_id": self.cypher("MATCH (n:Release {id: '1'}) RETURN n.gm_id").split()[-1],
            "database_oid": self.admin_sql("postgres", "SELECT oid FROM pg_database WHERE datname = 'groovemap'"),
        }

    def accounts(self) -> str:
        return self.admin_sql("groovemap", "SELECT id, email, hashed_password, is_active, is_admin, created_at FROM public.users ORDER BY id")

    def mirror_downloads(self) -> dict[str, int]:
        log = self.run(["docker", "logs", "groovemap-rehearsal-mirror"], check=False).stdout
        counts = {"data_files": 0, "checksum": 0, "listings": 0}
        for line in log.splitlines():
            if "download=" not in line:
                counts["listings"] += 1
            elif "CHECKSUM" in line:
                counts["checksum"] += 1
            else:
                counts["data_files"] += 1
        return counts

    def maintenance_runs(self) -> int:
        return self.compose("logs", "--no-color", "graphinator", timeout=60).stdout.count("Post-import maintenance complete")

    def wait_maintenance(self, runs: int) -> None:
        """The graph's post-import pass is detached from the gate, and it writes checkpoint nodes."""
        self.wait(f"post-import maintenance run {runs}", lambda: self.maintenance_runs() >= runs, 300)

    def marker(self) -> str:
        return self.compose("exec", "-T", "extractor-discogs", "sh", "-c", f"cat /discogs-data/.extraction_status_{VERSION}.json", timeout=30).stdout

    def first_load(self) -> None:
        self.say("First load through the released extractor and loaders, from the mirror")
        started = time.monotonic()
        self.compose("up", "-d", "discogs-mirror", timeout=120)
        # Consumers before the extractor, as the runbook starts them: a publish with no queue bound
        # fails the run with 312 NO_ROUTE.
        for group in (CONSUMERS, APPLICATIONS, ("extractor-discogs",)):
            result = self.compose("up", "-d", "--wait", "--wait-timeout", "900", "--quiet-pull", *group, timeout=1500, check=False)
            if result.returncode != 0:
                (self.evidence / "first-up.log").write_text(result.stdout + result.stderr)
                raise RehearsalError(f"first up failed on {group}:\n{result.stderr[-1500:]}")
        self.gate(900)
        self.report["timings_s"]["first_load"] = round(time.monotonic() - started, 1)
        self.check(self.mirror_downloads()["data_files"] == 4, "the first load downloaded all four dump files from the mirror")
        self.run(
            ["docker", "exec", "-e", f"ADMIN_PASSWORD={self.admin_password}", "groovemap-api", "catalog-admin-setup", "--email", ADMIN_EMAIL],
            timeout=60,
        )
        self.wait(
            "the identity watcher's handled marker",
            lambda: self.admin_sql("groovemap", "SELECT count(*) FROM public.admin_audit_log WHERE action = 'identity.reattach.auto'") == "1",
            180,
        )
        token = self.login()
        status, _ = self.api("POST", "/api/admin/auth/logout", token=token)
        self.check(status in (200, 204), f"signing out wrote a real revocation key (HTTP {status})")
        rows = self.admin_sql("groovemap", "SELECT id = '6d3c1f0a-5e1b-4c7a-9a4f-1d0e1d3a7a11'::uuid, is_active FROM public.users ORDER BY 1")
        self.check(rows.splitlines() == ["f|t", "t|f"], "two accounts, as on lux: one active admin and the inactive system actor")
        actions = self.admin_sql("groovemap", "SELECT action, count(*) FROM public.admin_audit_log GROUP BY 1 ORDER BY 1")
        self.report["audit_before"] = actions
        self.check("identity.reattach.auto|1" in actions, "the handled marker for this dump is in the audit log, as on lux")

    def seed_stale_state(self) -> None:
        self.say("Seeding what the reset must clear")
        valkey = 'VALKEYCLI_AUTH="$(cat /run/secrets/valkey_password)"; export VALKEYCLI_AUTH; exec valkey-cli --no-auth-warning'
        for command in (
            "SET groovemap:explore:artist:Rehearsal stale",
            "SET groovemap:insights:generation 7",
            "SET unprefixed:stale 1",
            "SET groovemap:password_changed:rehearsal 123 EX 3600",
        ):
            self.compose("exec", "-T", "valkey", "sh", "-c", f"{valkey} {command}", timeout=30)
        publish = (
            'export RABBITMQADMIN_USERNAME="$(cat /run/secrets/rabbitmq_username)" RABBITMQADMIN_PASSWORD="$(cat /run/secrets/rabbitmq_password)"; '
            "exec rabbitmqadmin --non-interactive publish message --routing-key groovemap-discogs-tableinator-releases.dlq --payload stale"
        )
        self.compose("exec", "-T", "rabbitmq", "sh", "-c", publish, timeout=60)
        depth = self.compose(
            "exec", "-T", "rabbitmq", "rabbitmqctl", "-q", "list_queues", "--no-table-headers", "name", "messages", timeout=60
        ).stdout
        self.report["queues_before"] = depth
        self.check(re.search(r"tableinator-releases\.dlq\s+1", depth) is not None, "one dead letter sits in a DLQ")

    def negative_cases(self) -> None:
        self.say("Cases that must stop the reset")
        self.admin_sql("groovemap", "INSERT INTO public.app_config (key, value) VALUES ('rehearsal', 'x')")
        result = self.reset("preflight", apply=False, check=False, state=self.state)
        self.check(result.returncode == 1 and "public.app_config" in result.stderr, "preflight stops on a user-owned table it does not carry across")
        self.admin_sql("groovemap", "DELETE FROM public.app_config")
        self.reset("preflight", apply=False, state=self.state)
        self.check(True, "preflight passes on the populated stack")

    def reset_forward(self, state: Path, *, with_session_check: bool) -> None:
        self.reset("stop", state=state)
        self.reset("capture", state=state)
        left = self.compose("ps", "--all", "--quiet", "tableinator", "graphinator", "api", "extractor-discogs", timeout=30).stdout.strip()
        self.check(left == "", "no writer container remains for a daemon restart to bring back")
        if with_session_check:
            holder = [
                "docker",
                "exec",
                "-d",
                "-e",
                "PGAPPNAME=rehearsal-straggler",
                "-e",
                f"PGPASSWORD={self.pg_password}",
                self.pg_container,
                "psql",
                "-h",
                "127.0.0.1",
                "-U",
                "groovemap",
                "-d",
                "groovemap",
                "-c",
                "SELECT pg_sleep(600)",
            ]
            self.run(holder)
            self.wait(
                "the straggler session",
                lambda: self.admin_sql("postgres", "SELECT count(*) FROM pg_stat_activity WHERE application_name = 'rehearsal-straggler'") == "1",
                30,
                1,
            )
            result = self.reset("reset-postgres", state=state, check=False)
            self.check(
                result.returncode == 1 and "still on groovemap" in result.stderr,
                "reset-postgres stops on a session still on groovemap and terminates nothing",
            )
            names = self.admin_sql("postgres", "SELECT string_agg(datname, ',' ORDER BY datname) FROM pg_database WHERE datname LIKE 'groovemap%'")
            self.check(names == "groovemap", "nothing was renamed")
            self.run(["docker", "exec", self.pg_container, "pkill", "-f", "rehearsal-straggler|pg_sleep\\(600\\)"], check=False)
            self.admin_sql("postgres", "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name = 'rehearsal-straggler'")
            self.wait(
                "the straggler to go",
                lambda: self.admin_sql("postgres", "SELECT count(*) FROM pg_stat_activity WHERE application_name = 'rehearsal-straggler'") == "0",
                30,
                1,
            )
        for stage in ("reset-postgres", "reset-neo4j", "reset-rabbitmq", "reset-valkey", "bootstrap"):
            self.reset(stage, state=state)
        if with_session_check:
            self.reset("reset-neo4j", state=state)
            self.check(self.cypher("MATCH (n) RETURN count(n)").split()[-1] == "0", "re-running reset-neo4j after bootstrap changed nothing")
            refused = self.reset("capture", state=state, check=False)
            self.check(refused.returncode == 1 and "already holds a capture" in refused.stderr, "a second capture is refused")
            refused = self.reset("finish", state=state, check=False)
            self.check(refused.returncode == 1 and "no complete Discogs load" in refused.stderr, "finish refuses before the load is complete")

    def forced_reingest(self) -> None:
        downloads = self.mirror_downloads()
        self.reset("start", state=self.state)
        token = self.login()
        started = time.monotonic()
        status, text = self.api("POST", "/api/admin/extractions/trigger", body={"source": "discogs"}, token=token)
        self.check(status == 202, f"the forced run was accepted through the admin endpoint (HTTP {status}: {text[:120]})")
        self.gate(900)
        self.report["timings_s"].setdefault("reingest", []).append(round(time.monotonic() - started, 1))
        after = self.mirror_downloads()
        self.report["mirror"] = {"before": downloads, "after": after}
        self.check(
            after["data_files"] == downloads["data_files"],
            f"the forced run downloaded no dump file ({after['data_files']} data-file requests before and after)",
        )
        self.check(after["checksum"] > downloads["checksum"], "the forced run did re-verify against the published CHECKSUM")

    def after_load(self) -> None:
        self.say("After the gate: the ledger poll back on, then insights and the rarity run")
        off = self.compose("exec", "-T", "api", "printenv", "IDENTITY_LEDGER_REFRESH_ENABLED", timeout=30).stdout.strip()
        self.check(off == "false", "the ledger poll stayed off for the load")
        refreshed = self.admin_sql("groovemap", "SELECT count(*) FROM public.identity_ledger_refresh_state")
        self.check(refreshed == "1", "the watcher's own refresh built the ledger with the poll off")
        self.reset("after-load", state=self.state)
        sql = "SELECT status FROM insights.computation_log WHERE insight_type = 'release_rarity' ORDER BY started_at DESC LIMIT 1"
        self.wait("release_rarity to finish", lambda: self.admin_sql("groovemap", sql) in ("completed", "failed", "interrupted"), 600)
        status = self.admin_sql("groovemap", sql)
        rows = self.admin_sql("groovemap", "SELECT count(*) FROM insights.release_rarity")
        self.report["rarity"] = {"status": status, "rows": rows}
        self.check(status == "completed" and int(rows) > 0, f"rarity completed with {rows} rows")

    def execute(self) -> None:
        self.build()
        self.first_load()
        self.wait_maintenance(1)
        self.seed_stale_state()
        before_neighbours = self.neighbours()
        before_accounts = self.accounts()
        before = self.catalog()
        self.report["before"] = before
        before_marker = self.marker()
        self.state.mkdir(mode=0o700)
        self.negative_cases()
        self.say("Reset, in the runbook's order")
        self.reset_forward(self.state, with_session_check=True)
        self.check(True, "bootstrap verified the restored accounts and the empty catalog (catalog-reset exit 0)")
        keys = self.compose(
            "exec",
            "-T",
            "valkey",
            "sh",
            "-c",
            'VALKEYCLI_AUTH="$(cat /run/secrets/valkey_password)"; export VALKEYCLI_AUTH; exec valkey-cli --no-auth-warning --scan',
            timeout=30,
        ).stdout.split()
        self.report["valkey_after_reset"] = sorted(key.split(":", 2)[1] if key.count(":") >= 2 else key for key in keys)
        self.check(not [key for key in keys if "explore" in key or "generation" in key or "unprefixed" in key], "catalog caches are gone from Valkey")
        self.check(
            any("password_changed" in key for key in keys) and any("revoked:jti" in key for key in keys), "session revocations survive in Valkey"
        )
        depth = self.compose(
            "exec", "-T", "rabbitmq", "rabbitmqctl", "-q", "list_queues", "--no-table-headers", "name", "messages", timeout=60
        ).stdout
        self.check(all(line.split()[-1] == "0" for line in depth.splitlines() if line.strip()), "every queue is empty, dead letters included")
        self.check(self.accounts() == before_accounts, "both accounts are byte-identical after the reset, password hash included")
        self.say("Forced re-ingest")
        runs = self.maintenance_runs()
        self.forced_reingest()
        self.wait_maintenance(runs + 1)
        self.wait(
            "the identity watcher to run on the re-ingest",
            lambda: self.admin_sql("groovemap", "SELECT count(*) FROM public.admin_audit_log WHERE action = 'identity.reattach.auto'") == "1",
            180,
        )
        self.check(True, "the identity watcher ran again on the same dump version, so its marker was not carried over")
        self.wait("gm_id projection", lambda: self.catalog()["neo4j_gm_ids"] == before["neo4j_gm_ids"], 180)
        after = self.catalog()
        self.report["after"] = after
        for key in ("artists", "labels", "masters", "releases", "provider_aliases", "graph_person", "neo4j_nodes", "neo4j_gm_ids"):
            self.check(after[key] == before[key], f"{key}: {before[key]} before, {after[key]} after")
        self.check(
            after["catalog_items"].split("|")[0] == before["catalog_items"].split("|")[0],
            f"catalog_items count {before['catalog_items'].split('|')[0]} unchanged",
        )
        self.check(after["catalog_items"].split("|")[1] != before["catalog_items"].split("|")[1], "every catalog id set is new: ids were re-minted")
        self.check(after["neo4j_first_gm_id"] != before["neo4j_first_gm_id"], "Neo4j carries the new gm_id")
        self.check(after["database_oid"] != before["database_oid"], "groovemap is a new database")
        self.check(self.accounts() == before_accounts, "both accounts are still byte-identical after the re-ingest")
        self.login()
        self.after_load()
        self.check(self.neighbours() == before_neighbours, "neighbour databases are byte-identical and their session never dropped")
        self.say("Rollback")
        self.reset("rollback", state=self.state)
        (self.state / "done-rollback").unlink()
        self.reset("rollback", state=self.state)
        self.check(self.state.joinpath("done-rollback").exists(), "a rollback run again through the graph copy finished")
        self.compose(
            "up",
            "-d",
            "--no-deps",
            "--wait",
            "--wait-timeout",
            "600",
            "tableinator",
            "graphinator",
            "api",
            "dashboard",
            "explore",
            "extractor-discogs",
            timeout=660,
        )
        restored = self.catalog()
        self.check(restored == before, "rollback restored the old catalog: same database, same ids, same gm_ids, same counts")
        self.check(self.marker() == before_marker, "rollback restored the extractor's marker, so no restart resumes the forced run")
        logs = self.compose("logs", "--no-color", "--tail", "60", "extractor-discogs", timeout=30).stdout
        self.check("already processed, skipping" in logs, "the extractor skips the version after the rollback")
        self.gate(120)
        self.login()
        self.check(self.neighbours() == before_neighbours, "neighbours still untouched after the rollback")
        self.say("Finishing the rolled-back attempt, then a second reset through finish")
        self.reset("finish", state=self.state)
        gone = self.admin_sql("postgres", "SELECT string_agg(datname, ',' ORDER BY datname) FROM pg_database WHERE datname LIKE 'groovemap%'")
        self.check(gone == "groovemap", "finish removed the failed attempt and nothing else")
        self.interrupted_rollback(before)
        self.state = self.work / "state3"
        self.state.mkdir(mode=0o700)
        self.reset("preflight", apply=False, state=self.state)
        self.reset_forward(self.state, with_session_check=False)
        self.forced_reingest()
        refused = self.reset("finish", state=self.state, apply=False)
        self.check(refused.returncode == 0, "finish dry run")
        self.reset("finish", state=self.state)
        gone = self.admin_sql("postgres", "SELECT string_agg(datname, ',' ORDER BY datname) FROM pg_database WHERE datname LIKE 'groovemap%'")
        self.check(gone == "groovemap", "the second reset's finish left only groovemap")
        volumes = self.run(["docker", "volume", "ls", "-q", "--filter", f"name={self.project}_neo4j_data_pre_reset"]).stdout.strip()
        self.check(volumes == "", "the retained graph volume is gone")
        self.check(self.neighbours() == before_neighbours, "neighbours untouched at the very end")
        self.report["result"] = "PASS"

    def interrupted_rollback(self, before: dict[str, str]) -> None:
        """Simulated: a hand-made partial retained volume with no copy recorded, then a re-run with its record removed."""
        self.say("Rollback with a partial retained volume and no verified copy")
        self.state = self.work / "state2"
        self.state.mkdir(mode=0o700)
        self.reset("preflight", apply=False, state=self.state)
        self.reset("stop", state=self.state)
        self.reset("capture", state=self.state)
        self.reset("reset-postgres", state=self.state)
        retained = f"{self.project}_neo4j_data_pre_reset"
        self.run(["docker", "volume", "create", "--label", "groovemap.catalog-reset=retained", retained])
        self.run(["docker", "run", "--rm", "--network", "none", "-v", f"{retained}:/v", "alpine:3", "touch", "/v/partial-copy"])
        self.reset("rollback", state=self.state)
        self.check(self.catalog() == before, "rollback kept the intact graph and ignored the partial copy")
        (self.state / "done-rollback").unlink()
        self.reset("rollback", state=self.state)
        self.check(self.catalog() == before, "a rollback that stopped after the rename ran again and changed nothing")
        self.compose("up", "-d", "--no-deps", "--wait", "--wait-timeout", "600", *CONSUMERS, *APPLICATIONS, "extractor-discogs", timeout=660)
        self.gate(120)
        self.reset("finish", state=self.state)

    def diagnose(self) -> None:
        logs = self.evidence / "logs"
        logs.mkdir(exist_ok=True)
        label = f"label=com.docker.compose.project={self.project}"
        for name in tool("docker", "ps", "-a", "--filter", label, "--format", "{{.Names}}", timeout=30).stdout.split():
            result = tool("docker", "logs", "--timestamps", "--tail", "300", name, timeout=30)
            (logs / f"{name}.log").write_text(result.stdout + result.stderr)

    def teardown(self) -> None:
        self.say("Removing everything labelled with this run's projects")
        for project in (self.project, self.pg_project):
            label = f"label=com.docker.compose.project={project}"
            containers = tool("docker", "ps", "-aq", "--filter", label, timeout=30).stdout.split()
            if containers:
                tool("docker", "rm", "-f", "-v", *containers, timeout=180)
            networks = tool("docker", "network", "ls", "-q", "--filter", label, timeout=30).stdout.split()
            if networks:
                tool("docker", "network", "rm", *networks)
            volumes = tool("docker", "volume", "ls", "-q", "--filter", label, timeout=30).stdout.split()
            if volumes:
                tool("docker", "volume", "rm", "-f", *volumes, timeout=120)
        tool("docker", "volume", "rm", "-f", f"{self.project}_neo4j_data_pre_reset")
        tool("chmod", "-R", "u+rwX", str(self.work))
        shutil.rmtree(self.work, ignore_errors=True)


def stop_on_signal(signum: int, _frame: object) -> None:
    raise RehearsalError(f"stopped by signal {signum}")


def main() -> int:
    # Background jobs ignore SIGINT, so SIGTERM must also reach the teardown in `finally`.
    signal.signal(signal.SIGTERM, stop_on_signal)
    signal.signal(signal.SIGINT, stop_on_signal)
    parser = argparse.ArgumentParser(prog="rehearse-catalog-reset", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--evidence", required=True, help="directory for REPORT.json and logs; kept after the run")
    parser.add_argument("--deadline", type=int, default=3600, help="overall deadline in seconds; passing it is a failure")
    args = parser.parse_args()
    evidence = Path(args.evidence).resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    rehearsal = Rehearsal(evidence, args.deadline)
    started = time.monotonic()
    status = 1
    try:
        rehearsal.execute()
        status = 0
    except (RehearsalError, subprocess.SubprocessError, OSError, ValueError) as error:
        rehearsal.report["result"] = "FAIL"
        rehearsal.report["error"] = str(error)[-3000:]
        print(f"FAILED: {error}", file=sys.stderr)
        rehearsal.diagnose()
    finally:
        rehearsal.report["timings_s"]["total"] = round(time.monotonic() - started, 1)
        rehearsal.flush_report()
        rehearsal.teardown()
    print(f"rehearse-catalog-reset: {rehearsal.report.get('result')}; evidence: {evidence}")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
