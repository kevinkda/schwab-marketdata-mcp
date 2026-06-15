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
    # Reset the DuckDB cache singleton so it re-opens under the new XDG path.
    from schwab_marketdata_mcp import cache as _cache_mod

    _cache_mod.reset_cache_singleton()
    try:
        yield state
    finally:
        _cache_mod.reset_cache_singleton()


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def use_fake_backend(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("SCHWAB_MOCK_BACKEND", "fixtures")
    monkeypatch.setenv("SCHWAB_MOCK_FIXTURES_DIR", str(FIXTURES_DIR))
    monkeypatch.setenv("SCHWAB_MOCK_SCENARIO", "normal")
    # Reset the cached client between tests
    from schwab_marketdata_mcp import cache
    from schwab_marketdata_mcp.tools import _runtime as rt

    rt.reset_client_cache()
    cache.reset_cache_singleton()
    yield
    rt.reset_client_cache()
    cache.reset_cache_singleton()


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


def make_clickhouse_cache() -> tuple[Any, Any]:
    """Build a ``Cache`` backed by a mocked ClickHouse client (no live CH).

    Returns ``(cache, mock_client)``.  Every ``insert`` succeeds, so derived
    history durably persists; ``query`` returns canned rows the test sets via
    ``mock_client.query.return_value.result_rows``.  Used by every test that
    needs the analytics (snapshot / iv_history / candle) read-back paths.
    """
    from unittest.mock import MagicMock

    from schwab_marketdata_mcp.cache import Cache
    from schwab_marketdata_mcp.cache_backend import ClickHouseBackend

    client = MagicMock()
    client.command.return_value = None
    client.insert.return_value = None
    result = MagicMock()
    result.result_rows = []
    client.query.return_value = result
    return Cache(backend=ClickHouseBackend(url="clickhouse://x", client=client)), client


def clickhouse_inserted_rows(client: Any, *, series: str | None = None) -> list[dict[str, Any]]:
    """Decode JSON payloads inserted into the timeseries table by the backend.

    ``ClickHouseBackend.append_timeseries`` inserts ``[[series, json_payload]]``;
    ``set`` inserts response-cache rows (4-col) which we skip here.  When
    ``series`` is given, only rows for that series are returned.
    """
    rows: list[dict[str, Any]] = []
    for call in client.insert.call_args_list:
        args = call.args
        if len(args) < 2:
            continue
        data = args[1]
        col_names = call.kwargs.get("column_names") or []
        if "payload" not in col_names:
            continue  # response-cache insert, not a timeseries row
        for entry in data:
            if series is not None and entry[0] != series:
                continue
            rows.append(json.loads(entry[1]))
    return rows


def seed_clickhouse_query_rows(client: Any, rows: list[dict[str, Any]]) -> None:
    """Make the mock ClickHouse client's ``query`` return *rows* as payloads.

    Mirrors ``ClickHouseBackend.query_timeseries`` which reads a single
    ``payload`` column per row.
    """
    from unittest.mock import MagicMock

    result = MagicMock()
    result.result_rows = [[json.dumps(r)] for r in rows]
    client.query.return_value = result


def make_stateful_clickhouse_cache() -> Any:
    """Build a ``Cache`` over a *stateful* fake ClickHouse client.

    The fake stores timeseries rows in-process and serves them back through
    ``query`` filtered by series, so analytics round-trips (snapshot →
    aggregate_atm_iv → iv_history → get_iv_percentile_rank, and candle OLAP)
    work end-to-end without a live ClickHouse.  Honours the real
    ``ClickHouseBackend`` SQL parameter contract: it inspects the bound
    ``{s:String}`` series name from ``parameters`` to scope the read.
    """
    from schwab_marketdata_mcp.cache import Cache
    from schwab_marketdata_mcp.cache_backend import ClickHouseBackend

    class _StatefulClient:
        def __init__(self) -> None:
            self.timeseries: list[tuple[str, str]] = []  # (series, payload_json)
            self.response: dict[tuple[str, str], str] = {}  # (table, key) -> raw_json

        def command(self, *_a: Any, **_k: Any) -> None:
            return None

        def insert(self, table: str, data: list[list[Any]], *, column_names: list[str]) -> None:
            if "payload" in column_names:
                for row in data:
                    self.timeseries.append((row[0], row[1]))
            else:
                # response cache: [table_name, cache_key, raw_json, ttl]
                for row in data:
                    self.response[(row[0], row[1])] = row[2]

        def query(self, sql: str, *, parameters: dict[str, Any] | None = None) -> Any:
            from unittest.mock import MagicMock

            params = parameters or {}
            result = MagicMock()
            if "FROM schwab_md_timeseries" in sql and "payload" in sql:
                series = params.get("s")
                limit = int(params.get("n", 1000))
                payloads = [p for (s, p) in self.timeseries if s == series][:limit]
                result.result_rows = [[p] for p in payloads]
            elif "FROM schwab_md_response_cache" in sql and "raw_json" in sql:
                key = (params.get("t"), params.get("k"))
                raw = self.response.get(key)
                result.result_rows = [[raw]] if raw is not None else []
            elif "count()" in sql:
                result.result_rows = [[len(self.response)]]
            else:
                result.result_rows = []
            return result

    client = _StatefulClient()
    return Cache(backend=ClickHouseBackend(url="clickhouse://x", client=client))


def make_stateful_clickhouse_cache_with_client() -> Any:
    """Like :func:`make_stateful_clickhouse_cache` but also returns the fake
    client so tests can inspect inserts or plant timeseries rows directly."""
    cache = make_stateful_clickhouse_cache()
    return cache, cache.backend._client


@pytest.fixture(autouse=True)
def _no_real_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hard-block any test from accidentally using real Schwab credentials."""
    for var in ("SCHWAB_APP_KEY", "SCHWAB_APP_SECRET", "SCHWAB_CALLBACK_URL"):
        if os.environ.get(var):
            monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SCHWAB_APP_KEY", "test-key")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "test-secret")
    monkeypatch.setenv("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8182")
    # Pin the rate-limit budget so tests stay deterministic regardless of
    # whatever a developer has in their local .env (uv automatically loads
    # .env when invoking uv run).  Production default is 120/min.
    monkeypatch.delenv("SCHWAB_RATE_LIMIT_PER_MIN", raising=False)
