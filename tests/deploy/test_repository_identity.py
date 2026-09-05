"""Repository naming and documentation boundary checks."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TEXT_SUFFIXES = {".md", ".py", ".sh", ".toml", ".yml", ".yaml", ".json"}


def _active_text_files() -> list[Path]:
    roots = [ROOT / "README.md", ROOT / "AGENTS.md", ROOT / "docs", ROOT / "scripts", ROOT / "tests", ROOT / "config"]
    paths: list[Path] = []
    for root in roots:
        if root.is_file():
            paths.append(root)
        else:
            paths.extend(path for path in root.rglob("*") if path.is_file() and path.suffix in TEXT_SUFFIXES)
    paths.extend(ROOT.glob("docker-compose*.yml"))
    return paths


def test_retired_product_name_is_absent_from_active_files() -> None:
    retired_product_name = "discogs" + "ography"
    offenders = [str(path.relative_to(ROOT)) for path in _active_text_files() if retired_product_name in path.read_text()]
    assert not offenders, f"retired product branding remains in active files: {offenders}"


def test_readme_links_repository_local_details() -> None:
    readme = (ROOT / "README.md").read_text()
    for link in (
        "docs/README.md",
        "docs/quick-start.md",
        "docs/dockerfile-standards.md",
        "docs/architecture.md",
    ):
        assert f"]({link})" in readme


def test_architecture_and_lifecycle_diagrams_are_mermaid() -> None:
    markdown = [ROOT / "README.md", *(ROOT / "docs").glob("*.md")]
    diagrams = 0
    for path in markdown:
        for language, body in re.findall(r"```([^\n]*)\n(.*?)```", path.read_text(), re.DOTALL):
            if re.search(r"^(?:graph|flowchart|stateDiagram(?:-v2)?)\b", body, re.MULTILINE):
                diagrams += 1
                assert language.strip() == "mermaid", f"{path.relative_to(ROOT)} has a non-Mermaid diagram"
    assert diagrams > 0

    architecture = (ROOT / "docs/architecture.md").read_text()
    assert "stateDiagram-v2" in architecture
