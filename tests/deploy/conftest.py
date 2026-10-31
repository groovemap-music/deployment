"""Fixtures shared by the deploy-time tests."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest


if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def permissive_umask() -> Iterator[None]:
    """Run with a umask that would let a plain write leave a new file group- and world-readable."""
    previous = os.umask(0o000)
    try:
        yield
    finally:
        os.umask(previous)
