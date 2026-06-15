"""Unit tests for the pluggable cache backends (v0.7 T0).

Covers the three required paths:

* **MemoryBackend** — get/set/TTL/LRU eviction + multi-threaded concurrency.
* **ClickHouseBackend** — mocked ``clickhouse_connect`` client: append/query,
  schema bootstrap, connection-failure degradation, friendly install hint.
* **Degradation** — memory backend time-series returns
  ``requires_clickhouse_persistence``; the factory honors the env var and
  falls back to memory on an unknown value or missing extra.
"""

from __future__ import annotations

import threading
import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from schwab_marketdata_mcp import cache_backend as cb
from schwab_marketdata_mcp.cache_backend import (
    ENV_CACHE_BACKEND,
    ENV_CLICKHOUSE_URL,
    ClickHouseBackend,
    ClickHouseNotInstalledError,
    MemoryBackend,
    get_cache_backend,
    requires_clickhouse_signal,
)

# ===========================================================================
# MemoryBackend
# ===========================================================================


class TestMemoryBackendBasics:
    def test_set_get_round_trip(self) -> None:
        b = MemoryBackend()
        b.set("t", "k", {"v": 1}, 60)
        assert b.get("t", "k") == {"v": 1}

    def test_miss_returns_none(self) -> None:
        assert MemoryBackend().get("t", "missing") is None

    def test_namespaced_by_table(self) -> None:
        b = MemoryBackend()
        b.set("t1", "k", {"v": 1}, 60)
        b.set("t2", "k", {"v": 2}, 60)
        assert b.get("t1", "k") == {"v": 1}
        assert b.get("t2", "k") == {"v": 2}

    def test_overwrite_same_key(self) -> None:
        b = MemoryBackend()
        b.set("t", "k", {"v": 1}, 60)
        b.set("t", "k", {"v": 2}, 60)
        assert b.get("t", "k") == {"v": 2}
        assert b.size() == 1

    def test_value_is_defensively_copied(self) -> None:
        b = MemoryBackend()
        original = {"v": [1]}
        b.set("t", "k", original, 60)
        original["v"].append(2)  # mutate caller's copy after store
        assert b.get("t", "k") == {"v": [1]}
        got = b.get("t", "k")
        assert got is not None
        got["v"].append(99)  # mutate returned copy
        assert b.get("t", "k") == {"v": [1]}

    def test_name(self) -> None:
        assert MemoryBackend().name == "memory"

    def test_clear(self) -> None:
        b = MemoryBackend()
        b.set("t", "k", {"v": 1}, 60)
        b.clear()
        assert b.get("t", "k") is None
        assert b.size() == 0

    def test_size(self) -> None:
        b = MemoryBackend()
        assert b.size() == 0
        b.set("t", "a", {"v": 1}, 60)
        b.set("t", "b", {"v": 2}, 60)
        assert b.size() == 2


class TestMemoryBackendTTL:
    def test_zero_ttl_expires(self) -> None:
        b = MemoryBackend()
        b.set("t", "k", {"v": 1}, 0)
        assert b.get("t", "k") is None
        # Expired entry is evicted, not just hidden.
        assert b.size() == 0

    def test_negative_ttl_clamped_and_expires(self) -> None:
        b = MemoryBackend()
        b.set("t", "k", {"v": 1}, -100)
        assert b.get("t", "k") is None

    def test_ttl_boundary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        b = MemoryBackend()
        base = time.monotonic()
        monkeypatch.setattr(time, "monotonic", lambda: base)
        b.set("t", "k", {"v": 1}, 10)
        # Just before expiry → hit.
        monkeypatch.setattr(time, "monotonic", lambda: base + 9.999)
        assert b.get("t", "k") == {"v": 1}
        # At/after expiry → miss.
        monkeypatch.setattr(time, "monotonic", lambda: base + 10.0)
        assert b.get("t", "k") is None


class TestMemoryBackendLRU:
    def test_eviction_oldest_first(self) -> None:
        b = MemoryBackend(maxsize=2)
        b.set("t", "1", {"v": 1}, 60)
        b.set("t", "2", {"v": 2}, 60)
        b.set("t", "3", {"v": 3}, 60)  # evicts "1"
        assert b.get("t", "1") is None
        assert b.get("t", "2") == {"v": 2}
        assert b.get("t", "3") == {"v": 3}
        assert b.size() == 2

    def test_get_refreshes_recency(self) -> None:
        b = MemoryBackend(maxsize=2)
        b.set("t", "1", {"v": 1}, 60)
        b.set("t", "2", {"v": 2}, 60)
        b.get("t", "1")  # touch "1" → "2" is now LRU
        b.set("t", "3", {"v": 3}, 60)  # evicts "2"
        assert b.get("t", "1") == {"v": 1}
        assert b.get("t", "2") is None
        assert b.get("t", "3") == {"v": 3}

    def test_set_existing_refreshes_recency(self) -> None:
        b = MemoryBackend(maxsize=2)
        b.set("t", "1", {"v": 1}, 60)
        b.set("t", "2", {"v": 2}, 60)
        b.set("t", "1", {"v": 11}, 60)  # re-set "1" → "2" is now LRU
        b.set("t", "3", {"v": 3}, 60)  # evicts "2"
        assert b.get("t", "1") == {"v": 11}
        assert b.get("t", "2") is None

    def test_unbounded_when_maxsize_non_positive(self) -> None:
        b = MemoryBackend(maxsize=0)
        for i in range(100):
            b.set("t", str(i), {"v": i}, 60)
        assert b.size() == 100


class TestMemoryBackendConcurrency:
    def test_concurrent_set_get_thread_safe(self) -> None:
        b = MemoryBackend(maxsize=0)
        errors: list[Exception] = []

        def worker(start: int) -> None:
            try:
                for i in range(start, start + 200):
                    b.set("t", str(i), {"v": i}, 60)
                    b.get("t", str(i))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n * 200,)) for n in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert not errors
        assert b.size() == 8 * 200

    def test_concurrent_eviction_stays_bounded(self) -> None:
        b = MemoryBackend(maxsize=50)

        def worker(start: int) -> None:
            for i in range(start, start + 500):
                b.set("t", str(i), {"v": i}, 60)

        threads = [threading.Thread(target=worker, args=(n * 500,)) for n in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        # Eviction kept the store at/under the cap despite concurrent writers.
        assert b.size() <= 50


class TestMemoryBackendTimeseriesDegradation:
    def test_append_returns_degradation_signal(self) -> None:
        out = MemoryBackend().append_timeseries("iv_history", {"iv": 0.3})
        assert out["status"] == "requires_clickhouse_persistence"
        assert "clickhouse" in out["hint"].lower()

    def test_query_returns_degradation_signal(self) -> None:
        out = MemoryBackend().query_timeseries("iv_history", {"limit": 10})
        assert out["status"] == "requires_clickhouse_persistence"
        assert "hint" in out

    def test_requires_clickhouse_signal_shape(self) -> None:
        sig = requires_clickhouse_signal()
        assert sig["status"] == "requires_clickhouse_persistence"
        assert isinstance(sig["hint"], str)


# ===========================================================================
# ClickHouseBackend (mocked clickhouse_connect — no live ClickHouse)
# ===========================================================================


def _make_mock_client() -> MagicMock:
    client = MagicMock()
    client.command.return_value = None
    client.insert.return_value = None
    result = MagicMock()
    result.result_rows = []
    client.query.return_value = result
    return client


class TestClickHouseBackendInjectedClient:
    def test_schema_bootstrap_on_init(self) -> None:
        client = _make_mock_client()
        ClickHouseBackend(url="clickhouse://x", client=client)
        # Two DDLs: response cache + timeseries.
        assert client.command.call_count == 2

    def test_set_then_get_round_trip(self) -> None:
        client = _make_mock_client()
        b = ClickHouseBackend(url="clickhouse://x", client=client)
        b.set("t", "k", {"v": 1}, 60)
        client.insert.assert_called()
        # Simulate the row coming back from ClickHouse.
        result = MagicMock()
        result.result_rows = [['{"v": 1}']]
        client.query.return_value = result
        assert b.get("t", "k") == {"v": 1}

    def test_get_miss_empty_rows(self) -> None:
        client = _make_mock_client()
        b = ClickHouseBackend(url="clickhouse://x", client=client)
        assert b.get("t", "k") is None

    def test_get_non_dict_payload_returns_none(self) -> None:
        client = _make_mock_client()
        b = ClickHouseBackend(url="clickhouse://x", client=client)
        result = MagicMock()
        result.result_rows = [["[1, 2, 3]"]]  # valid JSON but not a dict
        client.query.return_value = result
        assert b.get("t", "k") is None

    def test_get_query_error_degrades_to_miss(self) -> None:
        client = _make_mock_client()
        b = ClickHouseBackend(url="clickhouse://x", client=client)
        client.query.side_effect = RuntimeError("connection reset")
        assert b.get("t", "k") is None

    def test_set_insert_error_swallowed(self) -> None:
        client = _make_mock_client()
        b = ClickHouseBackend(url="clickhouse://x", client=client)
        client.insert.side_effect = RuntimeError("write failed")
        b.set("t", "k", {"v": 1}, 60)  # must not raise

    def test_append_timeseries_ok(self) -> None:
        client = _make_mock_client()
        b = ClickHouseBackend(url="clickhouse://x", client=client)
        out = b.append_timeseries("iv_history", {"iv": 0.3})
        assert out == {"status": "ok"}
        client.insert.assert_called()

    def test_append_timeseries_error(self) -> None:
        client = _make_mock_client()
        b = ClickHouseBackend(url="clickhouse://x", client=client)
        client.insert.side_effect = RuntimeError("boom")
        out = b.append_timeseries("iv_history", {"iv": 0.3})
        assert out["status"] == "error"
        assert out["error"] == "RuntimeError"

    def test_query_timeseries_ok(self) -> None:
        client = _make_mock_client()
        b = ClickHouseBackend(url="clickhouse://x", client=client)
        result = MagicMock()
        result.result_rows = [['{"iv": 0.3}'], ['{"iv": 0.4}']]
        client.query.return_value = result
        out = b.query_timeseries("iv_history", {"limit": 5})
        assert out["status"] == "ok"
        assert out["rows"] == [{"iv": 0.3}, {"iv": 0.4}]

    def test_query_timeseries_error(self) -> None:
        client = _make_mock_client()
        b = ClickHouseBackend(url="clickhouse://x", client=client)
        client.query.side_effect = RuntimeError("query failed")
        out = b.query_timeseries("iv_history", {})
        assert out["status"] == "error"

    def test_clear(self) -> None:
        client = _make_mock_client()
        b = ClickHouseBackend(url="clickhouse://x", client=client)
        client.command.reset_mock()
        b.clear()
        client.command.assert_called_once()

    def test_clear_error_swallowed(self) -> None:
        client = _make_mock_client()
        b = ClickHouseBackend(url="clickhouse://x", client=client)
        client.command.side_effect = RuntimeError("truncate failed")
        b.clear()  # must not raise

    def test_size(self) -> None:
        client = _make_mock_client()
        b = ClickHouseBackend(url="clickhouse://x", client=client)
        result = MagicMock()
        result.result_rows = [[7]]
        client.query.return_value = result
        assert b.size() == 7

    def test_size_empty(self) -> None:
        client = _make_mock_client()
        b = ClickHouseBackend(url="clickhouse://x", client=client)
        result = MagicMock()
        result.result_rows = []
        client.query.return_value = result
        assert b.size() == 0

    def test_size_error_returns_zero(self) -> None:
        client = _make_mock_client()
        b = ClickHouseBackend(url="clickhouse://x", client=client)
        client.query.side_effect = RuntimeError("count failed")
        assert b.size() == 0

    def test_schema_bootstrap_error_swallowed(self) -> None:
        client = _make_mock_client()
        client.command.side_effect = RuntimeError("ddl failed")
        # Must not raise during construction.
        b = ClickHouseBackend(url="clickhouse://x", client=client)
        assert b.name == "clickhouse"


class TestClickHouseBackendConnect:
    def test_connect_via_module(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A configured URL connects through the (mocked) module."""
        fake_module = MagicMock()
        fake_client = _make_mock_client()
        fake_module.get_client.return_value = fake_client
        monkeypatch.setattr(cb, "_import_clickhouse_connect", lambda: fake_module)
        b = ClickHouseBackend(url="clickhouse://host:9000")
        fake_module.get_client.assert_called_once_with(dsn="clickhouse://host:9000")
        assert b.name == "clickhouse"

    def test_connect_missing_url_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_module = MagicMock()
        monkeypatch.setattr(cb, "_import_clickhouse_connect", lambda: fake_module)
        monkeypatch.delenv(ENV_CLICKHOUSE_URL, raising=False)
        with pytest.raises(ClickHouseNotInstalledError) as exc:
            ClickHouseBackend(url="")
        assert "not set" in str(exc.value.original)

    def test_connect_reads_env_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_module = MagicMock()
        fake_module.get_client.return_value = _make_mock_client()
        monkeypatch.setattr(cb, "_import_clickhouse_connect", lambda: fake_module)
        monkeypatch.setenv(ENV_CLICKHOUSE_URL, "clickhouse://env-host")
        ClickHouseBackend()
        fake_module.get_client.assert_called_once_with(dsn="clickhouse://env-host")

    def test_import_failure_raises_friendly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom() -> Any:
            raise ClickHouseNotInstalledError(ImportError("no module"))

        monkeypatch.setattr(cb, "_import_clickhouse_connect", boom)
        monkeypatch.setenv(ENV_CLICKHOUSE_URL, "clickhouse://x")
        with pytest.raises(ClickHouseNotInstalledError) as exc:
            ClickHouseBackend()
        assert "pip install schwab-marketdata-mcp[clickhouse]" in exc.value.hint


class TestClickHouseNotInstalledError:
    def test_hint_message(self) -> None:
        err = ClickHouseNotInstalledError()
        assert "pip install schwab-marketdata-mcp[clickhouse]" in err.hint
        assert err.original is None

    def test_carries_original(self) -> None:
        original = ImportError("no clickhouse_connect")
        err = ClickHouseNotInstalledError(original)
        assert err.original is original


def test_import_clickhouse_connect_real(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real import helper raises a friendly error when the extra is absent.

    ``clickhouse_connect`` is not installed in the default test env, so the
    real helper exercises the ImportError → ClickHouseNotInstalledError path.
    """
    import builtins

    real_builtin = builtins.__import__

    def fake_builtin(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "clickhouse_connect":
            raise ImportError("simulated missing extra")
        return real_builtin(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_builtin)
    with pytest.raises(ClickHouseNotInstalledError):
        cb._import_clickhouse_connect()


def test_import_clickhouse_connect_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the extra is present, the helper returns the imported module.

    The default test env has no ``clickhouse_connect``, so inject a stub into
    ``sys.modules`` to exercise the success return.
    """
    import sys
    import types

    stub = types.ModuleType("clickhouse_connect")
    monkeypatch.setitem(sys.modules, "clickhouse_connect", stub)
    assert cb._import_clickhouse_connect() is stub


# ===========================================================================
# Factory
# ===========================================================================


class TestFactory:
    def test_default_is_memory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ENV_CACHE_BACKEND, raising=False)
        assert get_cache_backend().name == "memory"

    def test_explicit_memory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_CACHE_BACKEND, "memory")
        assert get_cache_backend().name == "memory"

    @pytest.mark.parametrize("val", ["", "  ", "MEMORY", " Memory "])
    def test_blank_or_cased_falls_back_to_memory(self, monkeypatch: pytest.MonkeyPatch, val: str) -> None:
        monkeypatch.setenv(ENV_CACHE_BACKEND, val)
        assert get_cache_backend().name == "memory"

    def test_unknown_value_falls_back_to_memory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_CACHE_BACKEND, "sqlite")
        assert get_cache_backend().name == "memory"

    def test_clickhouse_selected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_CACHE_BACKEND, "clickhouse")
        fake_module = MagicMock()
        fake_module.get_client.return_value = _make_mock_client()
        monkeypatch.setattr(cb, "_import_clickhouse_connect", lambda: fake_module)
        monkeypatch.setenv(ENV_CLICKHOUSE_URL, "clickhouse://x")
        backend = get_cache_backend()
        assert backend.name == "clickhouse"

    def test_clickhouse_without_extra_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_CACHE_BACKEND, "clickhouse")
        monkeypatch.setenv(ENV_CLICKHOUSE_URL, "clickhouse://x")

        def boom() -> Any:
            raise ClickHouseNotInstalledError(ImportError("missing"))

        monkeypatch.setattr(cb, "_import_clickhouse_connect", boom)
        with pytest.raises(ClickHouseNotInstalledError):
            get_cache_backend()


# ===========================================================================
# Protocol conformance
# ===========================================================================


def test_backends_satisfy_protocol() -> None:
    from schwab_marketdata_mcp.cache_backend import CacheBackend

    assert isinstance(MemoryBackend(), CacheBackend)
    client = _make_mock_client()
    assert isinstance(ClickHouseBackend(url="clickhouse://x", client=client), CacheBackend)
