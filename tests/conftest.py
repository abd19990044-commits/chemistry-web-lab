"""Shared pytest fixtures for the orca_engine test suite."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from orca_engine.models import JobData
from orca_engine.parser import OrcaParser

DATA_DIR = Path(__file__).parent / "data"


def parse_fixture(name: str) -> list[JobData]:
    """Parse one fixture file from ``tests/data``.

    Args:
        name: File name inside the fixture directory.

    Returns:
        Parsed job records.
    """

    path = DATA_DIR / name
    with path.open("r", encoding="utf-8") as stream:
        return OrcaParser(stream, source_name=str(path)).parse()


@pytest.fixture
def data_dir() -> Path:
    """Return the fixture data directory."""

    return DATA_DIR


@pytest.fixture
def reaction_dir(tmp_path: Path) -> Iterator[Path]:
    """Return a directory holding only the balanced reaction fixtures."""

    for name in ("rxn_h2.out", "rxn_o.out", "rxn_h2o.out"):
        (tmp_path / name).write_text(
            (DATA_DIR / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    yield tmp_path
