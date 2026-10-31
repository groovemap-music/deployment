"""The groovemap-homelab agent skill and the plugin manifests that make the repository installable.

`/plugin marketplace add groovemap-music/deployment` reads .claude-plugin/marketplace.json, and
`/plugin install groovemap-homelab@groovemap` installs the plugin whose root is the repository,
so the skill is skills/groovemap-homelab/SKILL.md (agentskills.io format). These tests keep the
manifests, the skill's frontmatter and the hard rules it states in step with the repository the
skill drives.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml

from tests.deploy.test_deploy import helpers
from tests.deploy.test_repository_identity import _heading_anchors


ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = ROOT / "skills" / "groovemap-homelab"
SKILL = SKILL_DIR / "SKILL.md"
PLUGIN = ROOT / ".claude-plugin" / "plugin.json"
MARKETPLACE = ROOT / ".claude-plugin" / "marketplace.json"
JUST = shutil.which("just") or "just"
GIT = shutil.which("git") or "git"
BASH = shutil.which("bash") or "/bin/bash"


def _frontmatter() -> tuple[dict[str, Any], str]:
    text = SKILL.read_text()
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    assert match, "SKILL.md must start with YAML frontmatter between --- lines"
    loaded: dict[str, Any] = yaml.safe_load(match[1])
    return loaded, match[2]


def _skill_text() -> str:
    return "\n".join(path.read_text() for path in sorted(SKILL_DIR.rglob("*.md")))


# --- packaging -------------------------------------------------------------------------


def test_marketplace_lists_the_plugin_at_the_repository_root() -> None:
    marketplace = json.loads(MARKETPLACE.read_text())
    assert marketplace["name"] == "groovemap"
    assert marketplace["owner"]["name"]
    [entry] = marketplace["plugins"]
    assert entry["name"] == json.loads(PLUGIN.read_text())["name"] == "groovemap-homelab"
    assert entry["source"] == "./"
    assert entry["description"]


def test_plugin_manifest_is_unversioned_so_installs_follow_the_commit() -> None:
    plugin = json.loads(PLUGIN.read_text())
    assert plugin["name"] == "groovemap-homelab"
    assert plugin["description"]
    assert plugin["repository"] == "https://github.com/groovemap-music/deployment"
    # The repository is intentionally unversioned. Without a version, Claude Code records the
    # git commit as the plugin's version, so every new commit reaches `/plugin update`; a fixed
    # version string would freeze installs at whatever commit first carried it.
    assert "version" not in plugin
    # Components are discovered from the default layout: skills/<name>/SKILL.md.
    assert not {"skills", "commands", "agents", "hooks", "mcpServers"} & plugin.keys()


def test_plugin_paths_are_tracked_not_ignored() -> None:
    for path in (PLUGIN, MARKETPLACE, SKILL):
        ignored = subprocess.run([GIT, "check-ignore", "-q", str(path)], cwd=ROOT, check=False)
        assert ignored.returncode == 1, f"{path.relative_to(ROOT)} is git-ignored"


# --- SKILL.md frontmatter (agentskills.io) ---------------------------------------------


def test_skill_frontmatter_follows_the_agent_skills_format() -> None:
    frontmatter, body = _frontmatter()
    assert set(frontmatter) <= {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
    name = frontmatter["name"]
    assert name == SKILL_DIR.name == "groovemap-homelab"
    assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)
    assert len(name) <= 64
    description = frontmatter["description"]
    assert isinstance(description, str)
    assert 0 < len(description) <= 1024
    assert "GrooveMap" in description
    assert "homelab" in description
    assert len(frontmatter["compatibility"]) <= 500
    assert all(isinstance(value, str) for value in frontmatter["metadata"].values())
    assert frontmatter["license"] == "MIT"
    # Progressive disclosure: a concise entry point, details in references/.
    assert len(body.splitlines()) < 500


# --- what the skill says ---------------------------------------------------------------


def test_skill_states_the_hard_rules_verbatim() -> None:
    _, body = _frontmatter()
    for rule in (
        "Never start the MusicBrainz profile before the gate reports complete.",
        "Never use image tags or `latest`.",
        "Never deploy the base file alone as production.",
        "GrooveMap's images are linux/amd64 only.",
        "Don't publish data-tier ports to the LAN or the internet.",
        "Keep `secrets/` and `.env` out of git.",
        "Never edit tracked files",
        "Ask before you change anything outside the checkout",
        "Existing deployments keep MusicBrainz.",
    ):
        assert rule in body, rule
    assert "gm-automation-2ik" in body
    assert "-f docker-compose.yml -f docker-compose.prod.yml" in body


def test_skill_delegates_the_non_negotiables_to_existing_recipes() -> None:
    _, body = _frontmatter()
    for recipe in ("just env-init", "just secrets-bootstrap", "just discogs-import-status", "just deploy"):
        assert recipe in body, recipe
    recipes = set(subprocess.run((JUST, "--summary"), cwd=ROOT, check=True, capture_output=True, text=True).stdout.split())
    mentioned = set(re.findall(r"`just ([a-z][a-z-]*)", _skill_text()))
    assert mentioned
    assert mentioned <= recipes, f"the skill names recipes the Justfile lacks: {sorted(mentioned - recipes)}"


def test_skill_uses_only_deploy_options_that_exist() -> None:
    usage = subprocess.run([BASH, str(ROOT / "scripts" / "deploy.sh"), "--help"], cwd=ROOT, check=True, capture_output=True, text=True).stdout
    commands = re.findall(r"`just deploy((?: --?[a-z][a-z-]*)*)`", _skill_text())
    # --yes and --force are also discussed on their own, outside a full command.
    flags = {flag for command in commands for flag in command.split()} | {"--yes", "--force"}
    assert "--no-wait" in flags
    for flag in flags:
        assert re.search(rf"(?<![\w-]){re.escape(flag)}\b", usage), flag


def test_skill_names_only_real_port_and_bind_variables() -> None:
    known = {variable for variable, *_ in helpers.PORTS} | {variable for variable, _ in helpers.BIND_ADDRESSES.values()}
    named = set(re.findall(r"\b[A-Z][A-Z0-9_]*_(?:HOST_PORT|BIND_ADDRESS)\b", _skill_text()))
    assert named <= known, sorted(named - known)
    assert {variable for variable, *_ in helpers.PORTS} <= named, "the port table in references/discovery.md must list every published port"


def test_skill_lists_what_the_override_check_rejects() -> None:
    override = (SKILL_DIR / "references" / "override.md").read_text()
    for key, _ in helpers.OVERRIDE_FIXED:
        assert f"`{key}`" in override, key
    for service in helpers.DATA_TIER:
        assert service in override, service


def test_skill_markdown_links_resolve() -> None:
    problems = []
    for source in sorted(SKILL_DIR.rglob("*.md")):
        for raw_target in re.findall(r"\[[^]]*]\(([^)]+)\)", source.read_text()):
            if re.match(r"(?:https?|mailto):", raw_target):
                continue
            path_text, _, fragment = raw_target.partition("#")
            destination = (source.parent / path_text).resolve() if path_text else source
            if not destination.exists():
                problems.append(f"{source.relative_to(ROOT)} -> {raw_target} (missing file)")
            elif fragment and fragment not in _heading_anchors(destination):
                problems.append(f"{source.relative_to(ROOT)} -> {raw_target} (missing heading)")
    assert not problems, "\n".join(problems)


def test_example_overrides_are_rendered_and_checked_by_just_check() -> None:
    examples = sorted((SKILL_DIR / "assets").glob("override-*.yml"))
    assert {path.name for path in examples} >= {"override-traefik.yml", "override-proxy-network.yml", "override-storage.yml"}
    for example in examples:
        assert example.read_text().startswith("---\n"), example.name
    check = (ROOT / "scripts" / "check-compose.sh").read_text()
    assert "skills/groovemap-homelab/assets/override-*.yml" in check
    assert "override-check" in check


# --- reusing existing PostgreSQL and Valkey ---------------------------------------------

REUSE_QUESTION = (
    "This homelab already has PostgreSQL and Valkey. Would you prefer GrooveMap to reuse compatible existing services, or run dedicated instances?"
)
EXTERNAL_STORES_BEADS = (
    "gm-deployment-e1uh",
    "gm-python-libraries-3sm",
    "gm-catalog-api-rchs",
    "gm-analytics-engine-bah5",
    "gm-database-schema-2aty",
    "gm-catalog-api-vd7n",
    "gm-analytics-engine-m0sz",
)
SHARED_SERVICES = SKILL_DIR / "references" / "shared-services.md"
HOMELAB_DOC = ROOT / "docs" / "homelab.md"
CONFIGURATION_DOC = ROOT / "docs" / "configuration.md"


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("> ", ""))


def test_skill_asks_the_reuse_question_verbatim() -> None:
    _, body = _frontmatter()
    assert REUSE_QUESTION in _flat(body)
    assert "only when PostgreSQL and Valkey already exist" in body
    assert "Discovery is never authorization" in _flat(body)


def test_skill_recommends_dedicated_until_the_capability_floor_is_met() -> None:
    _, body = _frontmatter()
    flat = _flat(body)
    for dimension in ("compatibility", "isolation", "capacity", "operational impact"):
        assert dimension in flat, dimension
    assert "Reuse is refused until the capability floors are met" in flat
    assert "EXTERNAL_STORE_FLOORS" in flat
    assert "Keep the deployment stopped" in flat
    assert "dedicated is the recommendation" in flat
    # Once a floor is met the operator provisions and writes the secrets; the agent sets only .env.
    assert "you never run a write on a shared server and never see or print a credential" in flat
    assert "`GROOVEMAP_*` and `EXTERNAL_*` values in `.env`" in flat


def test_discovery_is_read_only_and_never_shows_credentials() -> None:
    discovery = _flat((SKILL_DIR / "references" / "discovery.md").read_text())
    for phrase in (
        "Never print",
        "never run anything that writes",
        "Needs the operator",
        "pool_mode",
        "maxmemory-policy",
        "aof_enabled",
        "max_connections",
        "pg_available_extensions",
    ):
        assert phrase in discovery, phrase
    assert "Report auth as on or off and never its value" in discovery
    _, body = _frontmatter()
    assert "Never display credentials or modify an existing service." in body
    # Every inspect command in the discovery page selects fields, because a bare one prints Config.Env.
    for command in re.findall(r"docker inspect [^\n]+", discovery):
        assert "--format" in command or "without --format" in command or "Config.Env" in command, command


def test_shared_services_states_the_status_and_every_requirement() -> None:
    page = _flat(SHARED_SERVICES.read_text())
    assert "supported once the capability floors are met" in page
    assert "refuses it today" in page
    for answer in (
        "docker compose exec postgres",
        "store-check-postgres",
        "local-stores",
        "POSTGRES_PREPROVISIONED=true",
        "CREATE DATABASE",
        "CREATE ROLE",
        "CREATE EXTENSION",
        "VALKEY_KEY_PREFIX",
        "only through the supported external-stores mode",
        "EXTERNAL_STORE_FLOORS",
        "unreleased",
    ):
        assert answer in page, answer
    for requirement in (
        "PostgreSQL 18",
        "dedicated role",
        "connection budget",
        "session pool mode only",
        "pg_try_advisory_lock",
        "noeviction",
        "AOF",
        "is not a security boundary",
    ):
        assert requirement in page.replace("**", ""), requirement
    assert "Never `FLUSHDB`, `FLUSHALL` or `CONFIG SET`" in page


def test_external_store_beads_are_named_consistently() -> None:
    shared = SHARED_SERVICES.read_text()
    homelab = HOMELAB_DOC.read_text()
    for bead in EXTERNAL_STORES_BEADS:
        assert bead in shared, bead
        assert bead in homelab, bead
    for source in (SHARED_SERVICES, SKILL, SKILL_DIR / "references" / "override.md"):
        text = source.read_text()
        mentioned = set(re.findall(r"gm-(?:deployment|python-libraries|catalog-api|analytics-engine|database-schema)-[0-9a-z]+", text))
        assert mentioned <= set(EXTERNAL_STORES_BEADS), (source.name, sorted(mentioned - set(EXTERNAL_STORES_BEADS)))
    for mentioning in (_frontmatter()[1], HOMELAB_DOC.read_text(), CONFIGURATION_DOC.read_text()):
        assert "gm-deployment-e1uh" in mentioning


def test_docs_state_the_reuse_status_and_the_unchanged_approval_rules() -> None:
    homelab = _flat(HOMELAB_DOC.read_text())
    assert REUSE_QUESTION in homelab
    assert "Status: supported once the capability floors are met, which they are not yet." in homelab.replace("**", "")
    assert "neither is a security boundary" in homelab
    assert "A PgBouncer must pool by session" in homelab
    assert "explicit approval" in homelab
    assert "An override may still never rewire" in homelab
    configuration = _flat(CONFIGURATION_DOC.read_text())
    assert "homelab.md#existing-postgresql-and-valkey" in configuration
    assert "Reuse needs PgBouncer in session mode only" in configuration


def test_docs_give_the_external_stores_procedure_and_what_still_blocks_it() -> None:
    homelab = _flat(HOMELAB_DOC.read_text())
    configuration = _flat(CONFIGURATION_DOC.read_text())
    shared = _flat(SHARED_SERVICES.read_text())
    for page in (homelab, configuration, shared):
        for name in (
            "GROOVEMAP_POSTGRES",
            "GROOVEMAP_VALKEY",
            "EXTERNAL_POSTGRES_HOST",
            "EXTERNAL_VALKEY_KEY_PREFIX",
            "secrets/postgres_password.txt",
        ):
            assert name in page, name
    for page in (homelab, shared):
        assert "gm-database-schema-2aty" in page
        assert "merged, not released or promoted" in page
    for setting in ("noeviction", "AOF", "PostgreSQL 18", "pg_trgm", "60 usable connections", "session"):
        assert setting in homelab, setting
    assert "store-check-postgres" in configuration and "store-check-valkey" in configuration
    assert "is a data migration" in homelab
    assert "postgres-exporter" in homelab  # what the operator takes over
    maintenance = _flat((ROOT / "docs" / "maintenance.md").read_text())
    assert "belong to its operator" in maintenance


def test_docs_cover_transport_security_and_bare_compose_in_external_mode() -> None:
    homelab = _flat(HOMELAB_DOC.read_text())
    configuration = _flat(CONFIGURATION_DOC.read_text())
    shared = _flat(SHARED_SERVICES.read_text())
    _, skill = _frontmatter()
    for page in (homelab, configuration, shared):
        for name in (
            "EXTERNAL_POSTGRES_ALLOW_PLAINTEXT",
            "EXTERNAL_VALKEY_ALLOW_PLAINTEXT",
            "secrets/postgres_ca.crt",
            "secrets/valkey_ca.crt",
            "COMPOSE_FILE",
        ):
            assert name in page, name
    for page in (homelab, configuration):
        assert "EXTERNAL_POSTGRES_SSLMODE" in page and "verify-full" in page and "EXTERNAL_VALKEY_TLS" in page
    assert "Cleartext is refused off this machine." in homelab.replace("**", "")
    assert "Bare Compose uses the same files." in homelab.replace("**", "")
    assert "Anyone on the network path could read the password" in homelab
    assert "`COMPOSE_FILE` in `.env`" in _flat(skill) and "Never edit or remove that line" in _flat(skill)
    assert "Never set an `EXTERNAL_*_ALLOW_PLAINTEXT` opt-out on your own" in _flat(skill)
