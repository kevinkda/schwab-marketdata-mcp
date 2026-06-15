"""Pluggable cache backends for schwab-marketdata-mcp (v0.7 T0 template).

This module defines the storage abstraction the cache layer delegates to:

* :class:`CacheBackend` — the runtime-checkable Protocol every backend
  implements (response-cache ``get`` / ``set`` plus derived-analysis
  ``append_timeseries`` / ``query_timeseries``).
* :class:`MemoryBackend` — the **default**, zero-external-dependency
  backend.  An in-process LRU + per-entry TTL store built on stdlib
  ``OrderedDict`` guarded by a lightweight ``threading.Lock``.  All
  operations are pure in-memory dict mutations — the lock is held only for
  microseconds and never wraps I/O, so it does not serialise an asyncio
  event loop the way the old single-connection DuckDB + global ``RLock``
  did.  Time-series operations return a structured
  ``requires_clickhouse_persistence`` degradation signal rather than
  raising — derived analysis that needs history degrades gracefully, core
  tools are unaffected.
* :class:`ClickHouseBackend` — the **opt-in** backend.  Enabled only when
  ``pip install schwab-marketdata-mcp[clickhouse]`` is present *and*
  ``SCHWAB_CLICKHOUSE_URL`` is configured.  ``clickhouse_connect`` is
  imported lazily so a default install never pays for it; a missing import
  raises :class:`ClickHouseNotInstalledError` with a friendly hint to
  install the extra.
* :func:`get_cache_backend` — the factory.  Selects the backend from
  ``SCHWAB_CACHE_BACKEND`` (``memory`` | ``clickhouse``, default
  ``memory``).

Design constraints (v0.7-roadmap §4, route A + open-source constraint):

* L0 default: in-process memory LRU, concurrency-safe, non-blocking, zero
  file locks, zero external dependencies.
* L1 optional: ClickHouse for derived-analysis history (true concurrent
  read/write).
* No-ClickHouse degradation: time-series queries return
  ``{"status": "requires_clickhouse_persistence", "hint": ...}`` — core
  tools see zero difference.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from typing import Any, Final, Protocol, runtime_checkable

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_MEMORY_MAXSIZE",
    "ENV_CACHE_BACKEND",
    "ENV_CLICKHOUSE_URL",
    "REQUIRES_CLICKHOUSE_HINT",
    "CacheBackend",
    "ClickHouseBackend",
    "ClickHouseNotInstalledError",
    "MemoryBackend",
    "get_cache_backend",
    "requires_clickhouse_signal",
]

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ENV_CACHE_BACKEND: Final[str] = "SCHWAB_CACHE_BACKEND"
ENV_CLICKHOUSE_URL: Final[str] = "SCHWAB_CLICKHOUSE_URL"

DEFAULT_MEMORY_MAXSIZE: Final[int] = 2048

REQUIRES_CLICKHOUSE_HINT: Final[str] = (
    "Derived-analysis history requires the ClickHouse backend. "
    "Install it with `pip install schwab-marketdata-mcp[clickhouse]` and set "
    "SCHWAB_CLICKHOUSE_URL plus SCHWAB_CACHE_BACKEND=clickhouse. "
    "Core tools work unchanged without it."
)


def requires_clickhouse_signal() -> dict[str, Any]:
    """Structured degradation payload returned when history is unavailable.

    Returned by :class:`MemoryBackend` time-series methods so derived
    analysis (IV percentile / P&L trend) degrades gracefully instead of
    raising.  Core response-cache tools never see this.
    """
    return {"status": "requires_clickhouse_persistence", "hint": REQUIRES_CLICKHOUSE_HINT}


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class CacheBackend(Protocol):
    """Storage abstraction the cache layer delegates to.

    Two concerns (v0.7-roadmap §4.0):

    * **Response cache** (``get`` / ``set``) — reduce duplicate upstream
      API calls.  Every backend supports this.
    * **Derived-analysis history** (``append_timeseries`` /
      ``query_timeseries``) — persist time series for IV percentile /
      P&L trend.  Only the ClickHouse backend persists; the memory
      backend returns a ``requires_clickhouse_persistence`` signal.
    """

    name: str

    def get(self, table: str, key: str) -> dict[str, Any] | None:
        """Return a fresh (non-expired) cached row, or ``None`` on miss."""
        ...

    def set(self, table: str, key: str, value: dict[str, Any], ttl_seconds: int) -> None:
        """Store *value* under (table, key) with a TTL.  Best-effort."""
        ...

    def append_timeseries(self, series: str, row: dict[str, Any]) -> dict[str, Any]:
        """Append one row to a derived-analysis time series.

        Returns a status payload — ``{"status": "ok"}`` when persisted or
        a ``requires_clickhouse_persistence`` signal when the backend has
        no durable time-series store.
        """
        ...

    def query_timeseries(self, series: str, predicate: dict[str, Any]) -> dict[str, Any]:
        """Query a derived-analysis time series.

        Returns ``{"status": "ok", "rows": [...]}`` when persisted or a
        ``requires_clickhouse_persistence`` signal otherwise.
        """
        ...

    def clear(self) -> None:
        """Drop all response-cache state.  Test/maintenance convenience."""
        ...

    def size(self) -> int:
        """Number of live response-cache entries (best-effort)."""
        ...


# ---------------------------------------------------------------------------
# Memory backend (L0 default — zero external dependency)
# ---------------------------------------------------------------------------


class MemoryBackend:
    """In-process LRU + per-entry TTL response cache (default backend).

    Thread-safe via a short-held ``threading.Lock`` that only ever wraps
    dict mutations (no I/O), so it never blocks an asyncio event loop the
    way the old DuckDB single-connection + global ``RLock`` did.  LRU
    eviction caps memory at *maxsize* live entries.

    Time-series methods return a ``requires_clickhouse_persistence``
    degradation signal — the memory backend keeps no durable history.
    """

    name = "memory"

    def __init__(self, maxsize: int = DEFAULT_MEMORY_MAXSIZE) -> None:
        # maxsize <= 0 means "unbounded"; we still keep insertion order.
        self._maxsize = maxsize
        self._lock = threading.Lock()
        # key -> (value, expires_at_monotonic)
        self._store: OrderedDict[str, tuple[dict[str, Any], float]] = OrderedDict()

    @staticmethod
    def _composite_key(table: str, key: str) -> str:
        return f"{table}\x1f{key}"

    def get(self, table: str, key: str) -> dict[str, Any] | None:
        composite = self._composite_key(table, key)
        now = time.monotonic()
        with self._lock:
            entry = self._store.get(composite)
            if entry is None:
                return None
            value, expires_at = entry
            if now >= expires_at:
                # Expired — evict and miss.
                del self._store[composite]
                return None
            # LRU touch: mark most-recently-used.
            self._store.move_to_end(composite)
            # Deep copy so callers cannot mutate cached state (nested too).
            return copy.deepcopy(value)

    def set(self, table: str, key: str, value: dict[str, Any], ttl_seconds: int) -> None:
        composite = self._composite_key(table, key)
        expires_at = time.monotonic() + max(0, ttl_seconds)
        stored = copy.deepcopy(value)
        with self._lock:
            if composite in self._store:
                self._store.move_to_end(composite)
            self._store[composite] = (stored, expires_at)
            self._evict_if_needed()

    def _evict_if_needed(self) -> None:
        # Caller holds the lock.
        if self._maxsize <= 0:
            return
        while len(self._store) > self._maxsize:
            self._store.popitem(last=False)

    def append_timeseries(self, series: str, row: dict[str, Any]) -> dict[str, Any]:
        del series, row
        return requires_clickhouse_signal()

    def query_timeseries(self, series: str, predicate: dict[str, Any]) -> dict[str, Any]:
        del series, predicate
        return requires_clickhouse_signal()

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._store)


# ---------------------------------------------------------------------------
# ClickHouse backend (L1 opt-in — requires the `clickhouse` extra)
# ---------------------------------------------------------------------------


class ClickHouseNotInstalledError(RuntimeError):
    """Raised when the ClickHouse backend is requested without the extra.

    Carries a friendly ``hint`` instructing the user to install the
    optional dependency.
    """

    def __init__(self, original: Exception | None = None) -> None:
        self.hint = (
            "ClickHouse backend requested but `clickhouse-connect` is not "
            "installed. Install the optional extra with "
            "`pip install schwab-marketdata-mcp[clickhouse]`."
        )
        super().__init__(self.hint)
        self.original = original


def _import_clickhouse_connect() -> Any:
    """Lazily import ``clickhouse_connect`` with a friendly failure.

    Kept as a module-level function (not an inline import) so tests can
    monkeypatch it to inject a mock client without a live ClickHouse.
    """
    try:
        import clickhouse_connect
    except ImportError as exc:
        raise ClickHouseNotInstalledError(exc) from exc
    return clickhouse_connect


class ClickHouseBackend:
    """ClickHouse-backed cache + derived-analysis history store (opt-in).

    Enabled only when the ``clickhouse`` extra is installed and
    ``SCHWAB_CLICKHOUSE_URL`` is configured.  Connection / query
    failures degrade best-effort (response cache misses, time-series
    returns a degradation signal) so a flaky ClickHouse never breaks a
    core tool.
    """

    name = "clickhouse"

    _RESPONSE_TABLE: Final[str] = "schwab_md_response_cache"
    _TIMESERIES_TABLE: Final[str] = "schwab_md_timeseries"

    def __init__(self, url: str | None = None, *, client: Any | None = None) -> None:
        self._url = url if url is not None else os.environ.get(ENV_CLICKHOUSE_URL, "")
        self._lock = threading.Lock()
        # ``Any`` (not ``Any | None``): after construction the client is always
        # set — either injected (tests) or created via ``_connect``. The
        # connection-failure path raises, so a live instance never holds None.
        self._client: Any = client if client is not None else self._connect()
        self._ensure_schema()

    def _connect(self) -> Any:
        module = _import_clickhouse_connect()
        if not self._url:
            raise ClickHouseNotInstalledError(
                RuntimeError(f"{ENV_CLICKHOUSE_URL} is not set"),
            )
        return module.get_client(dsn=self._url)

    def _ensure_schema(self) -> None:
        # Table names are module-private constants, never user input; row
        # values are bound via ClickHouse query parameters below.
        ddl_response = (
            f"CREATE TABLE IF NOT EXISTS {self._RESPONSE_TABLE} "
            "(table_name String, cache_key String, raw_json String, "
            "fetched_at DateTime DEFAULT now(), ttl_seconds UInt32) "
            "ENGINE = MergeTree ORDER BY (table_name, cache_key)"
        )
        ddl_timeseries = (
            f"CREATE TABLE IF NOT EXISTS {self._TIMESERIES_TABLE} "
            "(series String, ts DateTime DEFAULT now(), payload String) "
            "ENGINE = MergeTree ORDER BY (series, ts)"
        )
        try:
            self._client.command(ddl_response)
            self._client.command(ddl_timeseries)
        except Exception as exc:
            log.debug("clickhouse schema bootstrap failed (best-effort): %s", exc)

    def get(self, table: str, key: str) -> dict[str, Any] | None:
        try:
            with self._lock:
                result = self._client.query(
                    f"SELECT raw_json FROM {self._RESPONSE_TABLE} "  # noqa: S608
                    "WHERE table_name = {t:String} AND cache_key = {k:String} "
                    "AND fetched_at + INTERVAL ttl_seconds SECOND >= now() "
                    "ORDER BY fetched_at DESC LIMIT 1",
                    parameters={"t": table, "k": key},
                )
            rows = getattr(result, "result_rows", None) or []
            if not rows:
                return None
            loaded = json.loads(rows[0][0])
        except Exception:
            return None
        return loaded if isinstance(loaded, dict) else None

    def set(self, table: str, key: str, value: dict[str, Any], ttl_seconds: int) -> None:
        try:
            with self._lock:
                self._client.insert(
                    self._RESPONSE_TABLE,
                    [[table, key, json.dumps(value, default=str), max(0, ttl_seconds)]],
                    column_names=["table_name", "cache_key", "raw_json", "ttl_seconds"],
                )
        except Exception as exc:
            log.debug("clickhouse set failed (best-effort): %s", exc)

    def append_timeseries(self, series: str, row: dict[str, Any]) -> dict[str, Any]:
        try:
            with self._lock:
                self._client.insert(
                    self._TIMESERIES_TABLE,
                    [[series, json.dumps(row, default=str)]],
                    column_names=["series", "payload"],
                )
        except Exception as exc:
            return {"status": "error", "error": type(exc).__name__}
        return {"status": "ok"}

    def query_timeseries(self, series: str, predicate: dict[str, Any]) -> dict[str, Any]:
        limit = int(predicate.get("limit", 1000))
        try:
            with self._lock:
                result = self._client.query(
                    f"SELECT payload FROM {self._TIMESERIES_TABLE} "  # noqa: S608
                    "WHERE series = {s:String} ORDER BY ts ASC LIMIT {n:UInt32}",
                    parameters={"s": series, "n": limit},
                )
            rows = getattr(result, "result_rows", None) or []
            parsed = [json.loads(r[0]) for r in rows]
        except Exception as exc:
            return {"status": "error", "error": type(exc).__name__}
        return {"status": "ok", "rows": parsed}

    def clear(self) -> None:
        try:
            with self._lock:
                self._client.command(f"TRUNCATE TABLE IF EXISTS {self._RESPONSE_TABLE}")
        except Exception as exc:
            log.debug("clickhouse clear failed (best-effort): %s", exc)

    def size(self) -> int:
        try:
            with self._lock:
                result = self._client.query(f"SELECT count() FROM {self._RESPONSE_TABLE}")  # noqa: S608
            rows = getattr(result, "result_rows", None) or []
            return int(rows[0][0]) if rows else 0
        except Exception:
            return 0


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def _resolve_backend_name() -> str:
    raw = os.environ.get(ENV_CACHE_BACKEND, "").strip().lower()
    return raw or "memory"


def get_cache_backend() -> CacheBackend:
    """Construct the configured cache backend (default ``memory``).

    Honors ``SCHWAB_CACHE_BACKEND`` (``memory`` | ``clickhouse``).
    Any unrecognised value falls back to ``memory`` — the zero-dependency
    default that keeps the server working out of the box.
    """
    name = _resolve_backend_name()
    if name == "clickhouse":
        return ClickHouseBackend()
    return MemoryBackend()
