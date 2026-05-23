"""Pytest configuration & shared fixtures.

Plan §6.2 — fixture-backed integration tests; ``SCHWAB_MOCK_BACKEND`` is set
per-test to switch between scenarios.  Unit tests do **not** use this
backend; they use ``respx`` against ``httpx`` directly.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent
FIXTURES_DIR = ROOT / "fixtures"
SEED_DIR = FIXTURES_DIR / "seed"


def pytest_collection_modifyitems(
    config: pytest.Config,  # pytest hook signature requires this parameter
    items: list[pytest.Item],
) -> None:
    """Honor the ``@pytest.mark.posix_only`` marker (Tier A Windows port).

    Tests marked ``posix_only`` rely on POSIX file-permission semantics
    (``stat.S_IMODE(...) == 0o600`` etc.) that NTFS does not faithfully
    emulate.  Skip them when running on Windows.
    """
    del config
    if sys.platform != "win32":
        return
    skip_posix = pytest.mark.skip(reason="POSIX-only file-permission semantics")
    for item in items:
        if "posix_only" in item.keywords:
            item.add_marker(skip_posix)


@pytest.fixture(autouse=True)
def _isolate_xdg_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Each test gets a fresh ``$XDG_STATE_HOME`` so usage.jsonl / token.json
    side-effects don't leak between tests."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    # Also make the test's cwd a clean dir so any accidental relative path is harmless.
    monkeypatch.chdir(tmp_path)
    return state


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def use_fake_backend(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("SCHWAB_MOCK_BACKEND", "fixtures")
    monkeypatch.setenv("SCHWAB_MOCK_FIXTURES_DIR", str(FIXTURES_DIR))
    monkeypatch.setenv("SCHWAB_MOCK_SCENARIO", "normal")
    # Reset the cached client between tests
    from schwab_marketdata_mcp.tools import _runtime as rt

    rt.reset_client_cache()
    yield
    rt.reset_client_cache()


@pytest.fixture
def write_fixture(tmp_path: Path) -> Any:
    """Write a JSON fixture file under ``tmp_path/fixtures/{tool}_{scenario}.json``."""
    fdir = tmp_path / "fixtures"
    fdir.mkdir(exist_ok=True)

    def _write(tool: str, scenario: str, payload: Any) -> Path:
        path = fdir / f"{tool}_{scenario}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    return _write


@pytest.fixture(autouse=True)
def _no_real_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hard-block any test from accidentally using real Schwab credentials."""
    for var in ("SCHWAB_APP_KEY", "SCHWAB_APP_SECRET", "SCHWAB_CALLBACK_URL"):
        if os.environ.get(var):
            monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SCHWAB_APP_KEY", "test-key")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "test-secret")
    monkeypatch.setenv("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8182")
