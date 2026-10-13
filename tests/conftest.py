"""Shared pytest fixtures.

No test in this suite touches the network. Every fixture is built from
deterministic, in-memory objects or the saved raw fixtures under
``src/rotd/sources/fixtures/``.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from rotd.config import Config  # noqa: E402
from rotd.util import UTC  # noqa: E402


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Hard-fail any socket use so a test can never reach the network."""
    import socket

    def _blocked(*_a, **_k):  # noqa: ANN002, ANN003
        raise RuntimeError("network access is forbidden in unit tests")

    monkeypatch.setattr(socket.socket, "connect", _blocked, raising=False)
    monkeypatch.setattr(socket, "create_connection", _blocked, raising=False)


@pytest.fixture()
def cfg(tmp_path: Path) -> Config:
    """A config with no secrets and no live file, using only registry defaults."""
    return Config(path=tmp_path / "rotd.env")


@pytest.fixture()
def fixtures_dir() -> Path:
    return SRC / "rotd" / "sources" / "fixtures"


@pytest.fixture()
def now() -> datetime:
    """A fixed, mid-session UTC instant (2026-10-13 14:30 UTC = 10:30 ET)."""
    return datetime(2026, 10, 13, 14, 30, 0, tzinfo=UTC)
