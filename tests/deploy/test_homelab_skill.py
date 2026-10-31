"""The groovemap-homelab agent skill and the plugin manifests that make the repository installable.

`/plugin marketplace add groovemap-music/deployment` reads .claude-plugin/marketplace.json, and
`/plugin install groovemap-homelab@groovemap` installs the plugin whose root is the repository,
so the skill is skills/groovemap-homelab/SKILL.md (agentskills.io format). These tests keep the
manifests, the skill's frontmatter and the hard rules it states in step with the repository the
skill drives.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from tests.deploy.test_deploy import helpers
from tests.deploy.test_repository_identity import _heading_anchors


if TYPE_CHECKING:
    import pytest


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


def _load_carry_env() -> Any:
    spec = importlib.util.spec_from_file_location("carry_env", SKILL_DIR / "scripts" / "carry_env.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


carry_env = _load_carry_env()


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


# --- scripts/carry_env.py --------------------------------------------------------------


OLD_TEMPLATE = """# template
CATALOG_API_IMAGE=ghcr.io/groovemap-music/catalog-api@sha256:REPLACE_WITH_64_HEX_CHARACTERS
NEO4J_HEAP_SIZE=2G
GRAFANA_URL=http://localhost:3000
# APP_BASE_URL=
"""
NEW_ENV = """# template
CATALOG_API_IMAGE=ghcr.io/groovemap-music/catalog-api@sha256:new
NEO4J_HEAP_SIZE=4G
GRAFANA_URL=http://localhost:3000
# APP_BASE_URL=
"""
BACKUP = """# template
CATALOG_API_IMAGE=ghcr.io/groovemap-music/catalog-api@sha256:old
NEO4J_HEAP_SIZE=2G
GRAFANA_URL="https://grafana.example.com"
# APP_BASE_URL=
APP_BASE_URL=https://groovemap.example.com
DATA_BIND_ADDRESS=127.0.0.1
COMPOSE_PROFILES=musicbrainz
"""


def test_carry_keeps_host_choices_and_never_the_old_digests() -> None:
    text, carried = carry_env.carry(BACKUP, NEW_ENV, OLD_TEMPLATE, ".env.x.bak")
    values = carry_env.assignments(text)
    assert carried == ["GRAFANA_URL", "APP_BASE_URL", "DATA_BIND_ADDRESS", "COMPOSE_PROFILES"]
    assert values["CATALOG_API_IMAGE"].endswith("@sha256:new")
    # Still the old template's default in the backup, so the new template's default wins.
    assert values["NEO4J_HEAP_SIZE"] == "4G"
    # Replaced in place, quotes kept; new keys appended under a comment naming the backup.
    assert 'GRAFANA_URL="https://grafana.example.com"' in text.splitlines()[3]
    assert "# Carried over from .env.x.bak" in text
    assert text.endswith("COMPOSE_PROFILES=musicbrainz\n")
    assert carry_env.carry(BACKUP, text, OLD_TEMPLATE, ".env.x.bak") == (text, [])


def test_carry_command_reads_the_old_template_from_git(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def git(*args: str) -> None:
        subprocess.run([GIT, "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    (tmp_path / ".env.example").write_text(OLD_TEMPLATE)
    git("add", ".env.example")
    git("commit", "-q", "-m", "old")
    (tmp_path / ".env").write_text(NEW_ENV)
    (tmp_path / ".env.x.bak").write_text(BACKUP)
    assert carry_env.main(["--since", "HEAD", str(tmp_path / ".env.x.bak"), str(tmp_path / ".env")]) == 0
    out = capsys.readouterr().out
    assert "APP_BASE_URL" in out
    assert "example.com" not in out
    assert carry_env.assignments((tmp_path / ".env").read_text())["APP_BASE_URL"] == "https://groovemap.example.com"
    assert carry_env.main(["--since", "HEAD", str(tmp_path / ".env.x.bak"), str(tmp_path / ".env")]) == 0
    assert "nothing to carry" in capsys.readouterr().out
    assert carry_env.main(["--since", "no-such-commit", str(tmp_path / ".env.x.bak"), str(tmp_path / ".env")]) == 1
