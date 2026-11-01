"""Regression tests for `just env-init`, which generates .env from the reviewed release digests.

Every write goes to a pytest tmp_path, never the repository's own .env.
"""

from __future__ import annotations

import importlib.util
import re
import stat
from pathlib import Path
from typing import TYPE_CHECKING

import pytest


if TYPE_CHECKING:
    from types import ModuleType


ROOT = Path(__file__).resolve().parents[2]
IMAGE_LINE = re.compile(r"^[A-Z0-9_]+_IMAGE=", re.MULTILINE)
# The same shape and digest checks scripts/smoke-released-stack.sh applies to each image.
RELEASED_REFERENCE = re.compile(r"ghcr\.io/groovemap-music/[a-z0-9-]+@sha256:[0-9a-f]{64}")
VALIDATION_DIGEST = "1" * 64


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


env_init = _load("env_init", ROOT / "scripts" / "env-init.py")
image_policy = _load("check_images", ROOT / "scripts" / "check-images.py")


def _template_image_variables() -> list[str]:
    return re.findall(r"^([A-Z0-9_]+_IMAGE)=", (ROOT / ".env.example").read_text(), re.MULTILINE)


def test_render_fills_the_image_placeholder_and_keeps_every_other_line() -> None:
    template = (
        "# a comment\n"
        "UID=1000\n"
        "DATABASE_SCHEMA_IMAGE=ghcr.io/groovemap-music/database-schema@sha256:REPLACE_WITH_64_HEX_CHARACTERS\n"
        "DISCOGS_ROOT=/discogs-data\n"
    )
    rendered = env_init.render_env(template, {"DATABASE_SCHEMA_IMAGE": "a" * 64})
    assert rendered == (
        f"# a comment\nUID=1000\nDATABASE_SCHEMA_IMAGE=ghcr.io/groovemap-music/database-schema@sha256:{'a' * 64}\nDISCOGS_ROOT=/discogs-data\n"
    )


def test_rendered_template_differs_from_the_example_only_in_image_digests() -> None:
    template = (ROOT / ".env.example").read_text()
    rendered = env_init.render_env(template, image_policy.RELEASED_IMAGE_DIGESTS)

    assert "REPLACE_WITH_64_HEX_CHARACTERS" not in rendered
    before_lines = template.splitlines()
    after_lines = rendered.splitlines()
    assert len(before_lines) == len(after_lines)
    for before, after in zip(before_lines, after_lines, strict=True):
        if IMAGE_LINE.match(before):
            assert after.split("@sha256:")[0] == before.split("@sha256:")[0]
        else:
            assert after == before


def test_every_image_variable_in_the_example_has_a_reviewed_digest() -> None:
    variables = _template_image_variables()
    assert len(variables) == 11
    assert set(variables) <= set(image_policy.RELEASED_IMAGE_DIGESTS)


def test_render_refuses_an_image_without_a_reviewed_digest() -> None:
    template = "ANALYTICS_ENGINE_IMAGE=ghcr.io/groovemap-music/analytics-engine@sha256:REPLACE_WITH_64_HEX_CHARACTERS\n"
    with pytest.raises(ValueError, match=r"no reviewed release digest.*ANALYTICS_ENGINE_IMAGE"):
        env_init.render_env(template, {})


def test_generated_env_passes_the_released_stack_validation() -> None:
    rendered = env_init.render_env((ROOT / ".env.example").read_text(), image_policy.RELEASED_IMAGE_DIGESTS)
    values = dict(re.findall(r"^([A-Z0-9_]+_IMAGE)=(\S+)$", rendered, re.MULTILINE))

    assert set(values) == set(image_policy.RELEASED_IMAGE_DIGESTS)
    for variable, reference in values.items():
        assert RELEASED_REFERENCE.fullmatch(reference), variable
        assert VALIDATION_DIGEST not in reference, variable
        assert reference.endswith(f"@sha256:{image_policy.RELEASED_IMAGE_DIGESTS[variable]}"), variable


def test_write_creates_a_missing_env_without_a_backup(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    assert env_init.write_env(target, "NEW=1\n", force=False) is None
    assert target.read_text() == "NEW=1\n"


def test_write_refuses_to_overwrite_an_existing_env_without_force(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    target.write_text("KEEP=me\n")
    with pytest.raises(FileExistsError, match="--force"):
        env_init.write_env(target, "NEW=1\n", force=False)
    assert target.read_text() == "KEEP=me\n"


def test_force_keeps_the_previous_env_as_a_timestamped_backup(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    target.write_text("OLD=1\n")
    backup = env_init.write_env(target, "NEW=2\n", force=True)
    assert backup is not None
    assert re.fullmatch(r"\.env\.\d{8}T\d{12}Z\.bak", backup.name), backup.name
    assert backup.read_text() == "OLD=1\n"
    assert target.read_text() == "NEW=2\n"


def test_just_recipe_runs_the_generator_with_forwarded_arguments() -> None:
    justfile = (ROOT / "Justfile").read_text()
    assert "env-init *args:\n    {{operator_python}} scripts/env-init.py {{args}}" in justfile


# --- upgrading an existing .env in place -------------------------------------------------

REPO = "ghcr.io/groovemap-music/catalog-api"
TEMPLATE_TEXT = f"# template\nCATALOG_API_IMAGE={REPO}@sha256:REPLACE_WITH_64_HEX_CHARACTERS\nWEB_BIND_ADDRESS=0.0.0.0\nNEW_KEY=fresh\n"
OPERATOR_ENV = (
    "# mine\n"
    f"CATALOG_API_IMAGE={REPO}@sha256:{'0' * 64}\n"
    "WEB_BIND_ADDRESS=10.0.0.5\n"
    "WEB_HOST_PORT=8081   # remapped\n"
    "\n"
    "# Neo4j sizing written by just deploy for about 16 GB of memory (docs/configuration.md). Edit freely; deploy will not rewrite it.\n"
    "NEO4J_HEAP_SIZE=6g\n"
    "COMPOSE_PROFILES=musicbrainz\n"
)


def _upgrade(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_text: str | None, *args: str, mode: int = 0o600) -> Path:
    (tmp_path / ".env.example").write_text(TEMPLATE_TEXT)
    target = tmp_path / ".env"
    if env_text is not None:
        target.write_text(env_text)
        target.chmod(mode)
    monkeypatch.setattr(env_init, "TEMPLATE", tmp_path / ".env.example")
    monkeypatch.setattr(env_init, "TARGET", target)
    monkeypatch.setattr(env_init, "load_release_digests", lambda: {"CATALOG_API_IMAGE": "a" * 64})
    assert env_init.main(list(args)) == 0
    return target


def _backups(tmp_path: Path) -> list[Path]:
    return sorted(tmp_path.glob(".env.*.bak"))


def test_upgrade_refreshes_digests_and_keeps_every_other_line_byte_for_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    target = _upgrade(tmp_path, monkeypatch, OPERATOR_ENV)
    text = target.read_text()

    assert f"CATALOG_API_IMAGE={REPO}@sha256:{'a' * 64}\n" in text
    kept = OPERATOR_ENV.replace("0" * 64, "a" * 64)
    assert text.startswith(kept)
    for line in ("WEB_BIND_ADDRESS=10.0.0.5", "WEB_HOST_PORT=8081   # remapped", "COMPOSE_PROFILES=musicbrainz", "NEO4J_HEAP_SIZE=6g"):
        assert line in text.splitlines()
    assert "# Neo4j sizing written by just deploy for about 16 GB" in text
    out = capsys.readouterr().out
    assert "CATALOG_API_IMAGE" in out
    assert "NEW_KEY" in out


def test_upgrade_appends_missing_template_keys_as_new(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    text = _upgrade(tmp_path, monkeypatch, OPERATOR_ENV).read_text()
    assert text.endswith("# Added by just env-init: new in .env.example since this file was written\nNEW_KEY=fresh\n")
    assert text.count("WEB_BIND_ADDRESS=") == 1


def test_upgrade_keeps_a_timestamped_backup_of_the_previous_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _upgrade(tmp_path, monkeypatch, OPERATOR_ENV)
    (backup,) = _backups(tmp_path)
    assert re.fullmatch(r"\.env\.\d{8}T\d{12}Z\.bak", backup.name)
    assert backup.read_text() == OPERATOR_ENV


def test_upgrade_is_a_no_op_when_the_file_is_current(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    target = _upgrade(tmp_path, monkeypatch, OPERATOR_ENV)
    for backup in _backups(tmp_path):
        backup.unlink()
    upgraded = target.read_text()
    capsys.readouterr()

    assert env_init.main([]) == 0
    assert target.read_text() == upgraded
    assert _backups(tmp_path) == []
    assert "already up to date" in capsys.readouterr().out


def test_upgrade_adds_a_missing_image_line_with_its_digest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = _upgrade(tmp_path, monkeypatch, "WEB_BIND_ADDRESS=10.0.0.5\nNEW_KEY=mine\n")
    text = target.read_text()
    assert f"CATALOG_API_IMAGE={REPO}@sha256:{'a' * 64}\n" in text
    assert "NEW_KEY=mine\n" in text
    assert "NEW_KEY=fresh" not in text


def test_force_regenerates_from_the_template_and_warns_about_dropped_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    target = _upgrade(tmp_path, monkeypatch, OPERATOR_ENV, "--force")
    assert target.read_text() == env_init.render_env(TEMPLATE_TEXT, {"CATALOG_API_IMAGE": "a" * 64})
    assert _backups(tmp_path)[0].read_text() == OPERATOR_ENV
    err = capsys.readouterr().err
    assert "WEB_BIND_ADDRESS" in err
    assert "WEB_HOST_PORT" in err
    assert "COMPOSE_PROFILES" in err
    assert "NEO4J_HEAP_SIZE" in err
    assert "CATALOG_API_IMAGE" not in err


def test_a_missing_env_is_created_from_the_template(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = _upgrade(tmp_path, monkeypatch, None)
    assert target.read_text() == env_init.render_env(TEMPLATE_TEXT, {"CATALOG_API_IMAGE": "a" * 64})
    assert _backups(tmp_path) == []


# --- file modes: .env can hold secrets, so a new file is private and a rewrite keeps its mode ----


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.usefixtures("permissive_umask")
def test_a_fresh_env_is_created_private_even_under_a_permissive_umask(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = _upgrade(tmp_path, monkeypatch, None)
    assert _mode(target) == 0o600
    assert not list(tmp_path.glob(".env.*.tmp"))


@pytest.mark.usefixtures("permissive_umask")
@pytest.mark.parametrize("mode", [0o600, 0o640])
def test_refresh_keeps_the_env_mode_and_the_backup_gets_the_same_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: int) -> None:
    target = _upgrade(tmp_path, monkeypatch, OPERATOR_ENV, mode=mode)
    (backup,) = _backups(tmp_path)
    assert _mode(target) == mode
    assert _mode(backup) == mode
    assert not list(tmp_path.glob(".env.*.tmp"))


@pytest.mark.usefixtures("permissive_umask")
def test_force_keeps_the_env_mode_and_the_backup_gets_the_same_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = _upgrade(tmp_path, monkeypatch, OPERATOR_ENV, "--force", mode=0o640)
    (backup,) = _backups(tmp_path)
    assert _mode(target) == 0o640
    assert _mode(backup) == 0o640
    assert not list(tmp_path.glob(".env.*.tmp"))


@pytest.mark.usefixtures("permissive_umask")
def test_no_op_leaves_the_env_mode_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    target = _upgrade(tmp_path, monkeypatch, OPERATOR_ENV)
    for backup in _backups(tmp_path):
        backup.unlink()
    capsys.readouterr()

    assert env_init.main([]) == 0
    assert _mode(target) == 0o600
    assert _backups(tmp_path) == []
