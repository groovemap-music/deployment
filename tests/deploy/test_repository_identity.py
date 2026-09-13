"""Repository naming and documentation boundary checks."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

import yaml


ROOT = Path(__file__).resolve().parents[2]
TEXT_SUFFIXES = {".md", ".py", ".sh", ".toml", ".yml", ".yaml", ".json"}


def _markdown_files() -> list[Path]:
    return [ROOT / "README.md", *(ROOT / "docs").glob("*.md"), *(ROOT / "tests" / "perftest").glob("*.md")]


def _heading_anchors(path: Path) -> set[str]:
    """Return the GitHub-style anchors declared by one Markdown file."""
    anchors: set[str] = set()
    counts: dict[str, int] = {}
    for heading in re.findall(r"^#{1,6}\s+(.+?)\s*#*\s*$", path.read_text(), re.MULTILINE):
        plain = re.sub(r"\[([^]]+)]\([^)]+\)", r"\1", heading).replace("`", "")
        slug = re.sub(r"[^\w\- ]", "", plain.casefold(), flags=re.ASCII)
        slug = re.sub(r"\s+", "-", slug.strip())
        duplicate = counts.get(slug, 0)
        counts[slug] = duplicate + 1
        anchors.add(f"{slug}-{duplicate}" if duplicate else slug)
    return anchors


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


def test_repository_local_markdown_links_resolve() -> None:
    problems: list[str] = []
    for source in _markdown_files():
        for raw_target in re.findall(r"!?\[[^]]*]\(([^)]+)\)", source.read_text()):
            target = unquote(raw_target.split(maxsplit=1)[0].strip("<>"))
            if re.match(r"(?:https?|mailto):", target):
                continue
            path_text, separator, fragment = target.partition("#")
            destination = (source.parent / path_text).resolve() if path_text else source
            if not destination.exists():
                problems.append(f"{source.relative_to(ROOT)} -> {target} (missing file)")
            elif separator and destination.suffix == ".md" and fragment not in _heading_anchors(destination):
                problems.append(f"{source.relative_to(ROOT)} -> {target} (missing heading)")
    assert not problems, "broken repository-local links:\n" + "\n".join(problems)


def test_architecture_and_lifecycle_diagrams_are_mermaid() -> None:
    markdown = _markdown_files()
    diagrams = 0
    for path in markdown:
        for language, body in re.findall(r"```([^\n]*)\n(.*?)```", path.read_text(), re.DOTALL):
            if re.search(r"^(?:graph|flowchart|stateDiagram(?:-v2)?)\b", body, re.MULTILINE):
                diagrams += 1
                assert language.strip() == "mermaid", f"{path.relative_to(ROOT)} has a non-Mermaid diagram"
    assert diagrams > 0

    architecture = (ROOT / "docs/architecture.md").read_text()
    assert "stateDiagram-v2" in architecture


def test_observability_conceptual_diagrams_match_the_collector_topology() -> None:
    document = (ROOT / "docs" / "observability.md").read_text()
    architecture, pipeline = re.findall(r"```mermaid\n(.*?)```", document, re.DOTALL)[:2]

    for token in (
        'APPS -->|"OTLP/HTTP :4318"| COLLECTOR',
        'COLLECTOR -->|"Prometheus remote write"| METRICS',
        'COLLECTOR -->|"OTLP/HTTP traces"| TRACES',
        'METRICS -->|"Prometheus query API"| GRAFANA',
        'TRACES -->|"Tempo query API"| GRAFANA',
    ):
        assert token in architecture

    for token in (
        "SPANMETRICS --> METRICS_LIMITER",
        "METRICS_LIMITER --> METRICS_BATCH --> REMOTE_WRITE --> VICTORIA_METRICS",
        "TRACES_BATCH --> SPANMETRICS",
        'TRACES_BATCH -->|"otlphttp/victoria_traces"| VICTORIA_TRACES',
    ):
        assert token in pipeline


def test_architecture_lists_every_named_volume() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    architecture = (ROOT / "docs" / "architecture.md").read_text()
    for volume in compose["volumes"]:
        assert f"`{volume}`" in architecture, f"docs/architecture.md does not name volume {volume}"


def test_image_guide_matches_every_compose_image_identity() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    guide = (ROOT / "docs" / "dockerfile-standards.md").read_text()
    for service_name, service in compose["services"].items():
        image = service["image"]
        if image.startswith("${"):
            variable = image.removeprefix("${").split(":?", maxsplit=1)[0]
            assert f"| `{service_name}` |" in guide
            assert f"| `{variable}` |" in guide
        else:
            tag = image.split("@sha256:", maxsplit=1)[0]
            assert f"| `{service_name}` | `{tag}` |" in guide


def test_quick_start_lists_only_the_published_application_and_backend_ports() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    quick_start = (ROOT / "docs" / "quick-start.md").read_text()
    published = {str(port).split(":", maxsplit=1)[0] for service in compose["services"].values() for port in service.get("ports", [])}
    for port in published:
        assert f"localhost:{port}" in quick_start, f"docs/quick-start.md does not list published port {port}"

    for internal_port in (8000, 8001, 8002, 8008, 8009, 8010, 8011):
        assert f"localhost:{internal_port}" not in quick_start
