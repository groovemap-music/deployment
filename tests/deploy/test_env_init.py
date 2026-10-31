"""Regression tests for `just env-init`, which generates .env from the reviewed release digests.

Every write goes to a pytest tmp_path, never the repository's own .env.
"""

from __future__ import annotations

import importlib.util
import re
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
    assert "env-init *args:\n    uv run python scripts/env-init.py {{args}}" in justfile
