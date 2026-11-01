"""The collector configuration as the collector itself would resolve it, for the tests that read it.

config/otel-collector.yaml takes its deployment-specific values from `${env:VAR:-default}`
references, which the collector expands from its container environment before parsing. This
expands them the same way (the variable when it is set, else the default) and parses the result,
so a test can read the effective configuration of the local stack (no variable set, the base and
production files) or of an overlay (the variables its render gives the otel-collector service).
`$$`, the collector's escape for a literal `$`, is left as it is, exactly as the raw YAML has it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml


if TYPE_CHECKING:
    from collections.abc import Mapping


COLLECTOR_CONFIG = Path(__file__).resolve().parents[2] / "config" / "otel-collector.yaml"
ENV_REFERENCE = re.compile(r"(?<!\$)\$\{env:(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::-(?P<default>[^}]*))?\}")


def references(text: str | None = None) -> dict[str, str | None]:
    """Every `${env:...}` reference in the configuration: variable -> its default (None without one)."""
    source = COLLECTOR_CONFIG.read_text() if text is None else text
    return {match["name"]: match["default"] for match in ENV_REFERENCE.finditer(source)}


def expand(text: str, environ: Mapping[str, str]) -> str:
    """Substitute every reference: the variable when set (even to ""), else the default, else ""."""

    def value(match: re.Match[str]) -> str:
        name = match["name"]
        return environ[name] if name in environ else (match["default"] or "")

    return ENV_REFERENCE.sub(value, text)


def effective(environ: Mapping[str, str] | None = None, text: str | None = None) -> dict[str, Any]:
    """The parsed configuration with `environ` as the collector's container environment ({}: the local stack)."""
    source = COLLECTOR_CONFIG.read_text() if text is None else text
    loaded: dict[str, Any] = yaml.safe_load(expand(source, environ or {}))
    return loaded
