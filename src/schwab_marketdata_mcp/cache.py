"""DuckDB-backed local cache for Schwab Market Data responses.

Plan §2 sprint v0.2 — task #2.

Goals
-----
* Cut repeat Schwab API calls for the same ``(tool, params)`` (rate-limit
  + latency relief).
* Provide a local OLAP engine (DuckDB) the Shakeout playbook v2 can query
  for historical candles without re-hitting Schwab.

Storage
-------
* Single-file DuckDB database under
  ``${XDG_STATE_HOME}/schwab-marketdata-mcp/cache.duckdb``.
* Same parent directory as ``token.json`` / ``usage.jsonl`` (mode 0o700)
  so all secret-adjacent state is co-located and the threat model can
  reason about it as one boundary.
* The DB file itself is chmod'd to ``0o600`` on POSIX (no-op on Windows
  where we rely on inherited ``%LOCALAPPDATA%`` ACLs — see
  ``_platform.secure_chmod`` and docs/THREAT_MODEL.md).

TTL
---
* ``quotes_cache`` — 60 s (price ticks change fast).
* ``price_history_cache`` — historical candles (older than 1 h) are
  treated as immutable; the most-recent 1 h is re-fetched if older
  than 60 s.
* ``option_chain_cache`` — 300 s (chains are heavy; LLM agents tend to
  read them several times per session).
* ``instruments_cache`` — 86 400 s (24 h; instrument metadata is nearly
  static).

Concurrency
-----------
DuckDB owns its own intra-process file lock and uses optimistic
serialisation; we do not add a second layer.  The ``Cache`` class
opens one connection per process and serialises writes via DuckDB's
own transaction guarantees.

Failure mode
------------
**Cache is best-effort.**  Any DuckDB / IO error is caught, logged at
``WARNING`` and the caller falls through to the live API.  A corrupt
DB file is renamed aside (``cache.duckdb.corrupt-<ts>``) and a fresh
one created.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import duckdb

from . import _platform

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_TTL_QUOTES_S: Final[int] = 60
DEFAULT_TTL_PRICE_HISTORY_RECENT_S: Final[int] = 60
RECENT_CANDLE_BOUNDARY_S: Final[int] = 3600  # candles within last hour are "recent"
DEFAULT_TTL_OPTION_CHAIN_S: Final[int] = 300
DEFAULT_TTL_INSTRUMENTS_S: Final[int] = 86_400

CACHE_DB_FILENAME: Final[str] = "cache.duckdb"
CACHE_DIR_NAME: Final[str] = "schwab-marketdata-mcp"

ENV_CACHE_ENABLED: Final[str] = "SCHWAB_CACHE_ENABLED"
ENV_CACHE_BYPASS: Final[str] = "SCHWAB_CACHE_BYPASS"


def _truthy(raw: str | None, *, default: bool) -> bool:
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def cache_enabled() -> bool:
    """Honor ``SCHWAB_CACHE_ENABLED`` (default on)."""
    return _truthy(os.environ.get(ENV_CACHE_ENABLED), default=True)


def cache_bypass() -> bool:
    """Honor ``SCHWAB_CACHE_BYPASS`` (default off — single-call force fresh)."""
    return _truthy(os.environ.get(ENV_CACHE_BYPASS), default=False)


def default_db_path() -> Path:
    """Canonical cache DB path under ``$XDG_STATE_HOME``."""
    return _platform.state_root() / CACHE_DIR_NAME / CACHE_DB_FILENAME


# ---------------------------------------------------------------------------
# Schema (DDL)
# ---------------------------------------------------------------------------

_SCHEMA_DDL: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS quotes_cache (
        symbol VARCHAR PRIMARY KEY,
        fetched_at TIMESTAMP,
        bid DOUBLE,
        ask DOUBLE,
        last DOUBLE,
        volume BIGINT,
        open DOUBLE,
        high DOUBLE,
        low DOUBLE,
        close DOUBLE,
        raw_json JSON,
        ttl_seconds INTEGER DEFAULT 60
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS price_history_cache (
        symbol VARCHAR,
        candle_datetime TIMESTAMP,
        period_type VARCHAR,
        frequency_type VARCHAR,
        frequency INTEGER,
        open DOUBLE,
        high DOUBLE,
        low DOUBLE,
        close DOUBLE,
        volume BIGINT,
        fetched_at TIMESTAMP,
        PRIMARY KEY (symbol, candle_datetime, period_type, frequency_type, frequency)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS option_chain_cache (
        cache_key VARCHAR PRIMARY KEY,
        symbol VARCHAR,
        fetched_at TIMESTAMP,
        raw_json JSON,
        ttl_seconds INTEGER DEFAULT 300
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS instruments_cache (
        cache_key VARCHAR PRIMARY KEY,
        fetched_at TIMESTAMP,
        raw_json JSON,
        ttl_seconds INTEGER DEFAULT 86400
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cache_events (
        ts TIMESTAMP,
        kind VARCHAR,
        table_name VARCHAR
    )
    """,
)


_TABLE_NAMES: Final[tuple[str, ...]] = (
    "quotes_cache",
    "price_history_cache",
    "option_chain_cache",
    "instruments_cache",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(tz=UTC).replace(tzinfo=None)


def _hash_params(params: dict[str, Any]) -> str:
    """Stable cache key for arbitrary param dicts.

    ``json.dumps(sort_keys=True, default=str)`` is enough — every value
    we store is JSON-serialisable in practice (str / int / float / bool
    / None / lists thereof).  ``default=str`` covers stray ``datetime``
    / ``Enum`` instances.
    """
    blob = json.dumps(params, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _parse_dt(value: Any) -> datetime | None:
    """Parse Schwab candle ``datetime`` (epoch ms or iso) into naive UTC."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(UTC).replace(tzinfo=None)
        return value
    if isinstance(value, (int, float)):
        # Schwab uses milliseconds since epoch in price_history responses
        # (https://developer.schwab.com/products/trader-api).
        return datetime.fromtimestamp(value / 1000.0, tz=UTC).replace(tzinfo=None)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC).replace(tzinfo=None)
        except ValueError:
            return None
    return None


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Stats payload
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CacheStats:
    db_path: str
    enabled: bool
    size_mb: float
    rows_per_table: dict[str, int]
    expired_rows: dict[str, int]
    hit_rate_24h: float | None
    hits_24h: int
    misses_24h: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "db_path": self.db_path,
            "enabled": self.enabled,
            "size_mb": round(self.size_mb, 4),
            "rows_per_table": dict(self.rows_per_table),
            "expired_rows": dict(self.expired_rows),
            "hit_rate_24h": self.hit_rate_24h,
            "hits_24h": self.hits_24h,
            "misses_24h": self.misses_24h,
        }


# ---------------------------------------------------------------------------
# Cache class
# ---------------------------------------------------------------------------


class Cache:
    """DuckDB-backed cache.  One instance per process.

    All public methods are best-effort: errors are logged at WARNING and
    the relevant getter returns ``None`` (treated as cache miss) so the
    caller falls through to the live API.  ``put_*`` errors are likewise
    swallowed.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else default_db_path()
        self._lock = threading.RLock()
        self._conn: duckdb.DuckDBPyConnection | None = None
        self._open()

    # ------------------------------------------------------------------ open

    def _ensure_parent(self) -> None:
        parent = self.db_path.parent
        with _platform.restrictive_umask():
            parent.mkdir(parents=True, mode=0o700, exist_ok=True)

    def _open(self) -> None:
        try:
            self._ensure_parent()
            self._conn = duckdb.connect(str(self.db_path))
            for stmt in _SCHEMA_DDL:
                self._conn.execute(stmt)
            # On POSIX, restrict the file we just touched to 0o600.
            try:
                _platform.secure_chmod(self.db_path, 0o600)
            except OSError:
                pass
        except (duckdb.Error, OSError) as exc:
            log.warning('{"event":"cache_open_failed","path":"%s","error":"%s"}', self.db_path, exc)
            # Try to quarantine corrupt DB and re-open fresh.
            self._quarantine_and_reopen(exc)

    def _quarantine_and_reopen(self, original_exc: Exception) -> None:
        if not self.db_path.exists():
            self._conn = None
            return
        ts = int(time.time())
        backup = self.db_path.with_suffix(self.db_path.suffix + f".corrupt-{ts}")
        try:
            os.rename(self.db_path, backup)
            log.warning(
                '{"event":"cache_quarantined","backup":"%s","original_error":"%s"}',
                backup,
                original_exc,
            )
        except OSError as exc:
            log.warning('{"event":"cache_quarantine_failed","error":"%s"}', exc)
            self._conn = None
            return
        try:
            self._conn = duckdb.connect(str(self.db_path))
            for stmt in _SCHEMA_DDL:
                self._conn.execute(stmt)
            try:
                _platform.secure_chmod(self.db_path, 0o600)
            except OSError:
                pass
        except (duckdb.Error, OSError) as exc:
            log.warning('{"event":"cache_reopen_failed","error":"%s"}', exc)
            self._conn = None

    # ---------------------------------------------------------------- close

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                with contextlib.suppress(duckdb.Error):
                    self._conn.close()
                self._conn = None

    def __enter__(self) -> Cache:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        del exc_type, exc, tb
        self.close()

    # ----------------------------------------------------------- event log

    def _record_event(self, kind: str, table: str) -> None:
        if self._conn is None:
            return
        try:
            self._conn.execute(
                "INSERT INTO cache_events (ts, kind, table_name) VALUES (?, ?, ?)",
                [_utcnow(), kind, table],
            )
        except duckdb.Error:
            pass

    # ------------------------------------------------------------- quotes

    def get_quote(self, symbol: str) -> dict[str, Any] | None:
        """Return cached single-quote payload or ``None`` on miss/expire."""
        with self._lock:
            if self._conn is None:
                return None
            try:
                row = self._conn.execute(
                    "SELECT raw_json, fetched_at, ttl_seconds FROM quotes_cache WHERE symbol = ?",
                    [symbol],
                ).fetchone()
            except duckdb.Error as exc:
                log.warning('{"event":"cache_get_failed","table":"quotes_cache","error":"%s"}', exc)
                return None
            if row is None:
                self._record_event("miss", "quotes_cache")
                return None
            raw, fetched_at, ttl = row
            if _is_expired(fetched_at, ttl):
                self._record_event("expired", "quotes_cache")
                return None
            self._record_event("hit", "quotes_cache")
            return _deserialise(raw)

    def put_quote(self, symbol: str, raw: dict[str, Any], *, ttl_seconds: int | None = None) -> None:
        ttl = ttl_seconds if ttl_seconds is not None else DEFAULT_TTL_QUOTES_S
        with self._lock:
            if self._conn is None:
                return
            fields = _extract_quote_fields(symbol, raw)
            try:
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO quotes_cache (
                        symbol, fetched_at, bid, ask, last, volume,
                        open, high, low, close, raw_json, ttl_seconds
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        symbol,
                        _utcnow(),
                        fields["bid"],
                        fields["ask"],
                        fields["last"],
                        fields["volume"],
                        fields["open"],
                        fields["high"],
                        fields["low"],
                        fields["close"],
                        json.dumps(raw, default=str),
                        ttl,
                    ],
                )
                self._record_event("write", "quotes_cache")
            except duckdb.Error as exc:
                log.warning('{"event":"cache_put_failed","table":"quotes_cache","error":"%s"}', exc)

    # ----------------------------------------------------- price history

    def get_price_history(self, params: dict[str, Any]) -> dict[str, Any] | None:
        """Return cached candles for ``(symbol, period_type, frequency_type, frequency)``.

        We do *not* cache by full param hash here — instead we re-query
        the candle rows that fall within the requested ``[start, end]``
        window.  A miss is returned if the most-recent candle is within
        the "recent" boundary (1 h) **and** older than the recent TTL
        (60 s), forcing a refresh.
        """
        symbol = str(params.get("symbol", ""))
        period_type = str(params.get("period_type") or "")
        frequency_type = str(params.get("frequency_type") or "")
        frequency = _safe_int(params.get("frequency"))
        if not symbol or not period_type or not frequency_type or frequency is None:
            return None

        start = _parse_dt(params.get("start_datetime"))
        end = _parse_dt(params.get("end_datetime"))

        with self._lock:
            if self._conn is None:
                return None
            try:
                rows = self._conn.execute(
                    """
                    SELECT candle_datetime, open, high, low, close, volume, fetched_at
                    FROM price_history_cache
                    WHERE symbol = ? AND period_type = ? AND frequency_type = ? AND frequency = ?
                    ORDER BY candle_datetime ASC
                    """,
                    [symbol, period_type, frequency_type, frequency],
                ).fetchall()
            except duckdb.Error as exc:
                log.warning(
                    '{"event":"cache_get_failed","table":"price_history_cache","error":"%s"}',
                    exc,
                )
                return None
            if not rows:
                self._record_event("miss", "price_history_cache")
                return None
            candles: list[dict[str, Any]] = []
            now = _utcnow()
            recent_boundary = now - timedelta(seconds=RECENT_CANDLE_BOUNDARY_S)
            recent_age_max = timedelta(seconds=DEFAULT_TTL_PRICE_HISTORY_RECENT_S)
            for cdt, o, h, lo, c, v, fa in rows:
                cdt_naive = cdt if isinstance(cdt, datetime) else _parse_dt(cdt)
                if cdt_naive is None:
                    continue
                if start is not None and cdt_naive < start:
                    continue
                if end is not None and cdt_naive > end:
                    continue
                if cdt_naive >= recent_boundary:
                    fetched_at = fa if isinstance(fa, datetime) else _parse_dt(fa)
                    if fetched_at is None or (now - fetched_at) > recent_age_max:
                        # Recent candle stale; force refresh of the whole window.
                        self._record_event("expired", "price_history_cache")
                        return None
                candles.append(
                    {
                        "datetime": int(cdt_naive.replace(tzinfo=UTC).timestamp() * 1000),
                        "open": o,
                        "high": h,
                        "low": lo,
                        "close": c,
                        "volume": v,
                    }
                )
            if not candles:
                self._record_event("miss", "price_history_cache")
                return None
            self._record_event("hit", "price_history_cache")
            return {"symbol": symbol, "candles": candles, "empty": False, "_cache_source": "duckdb"}

    def put_price_history(self, params: dict[str, Any], raw: dict[str, Any]) -> None:
        symbol = str(params.get("symbol", ""))
        period_type = str(params.get("period_type") or "")
        frequency_type = str(params.get("frequency_type") or "")
        frequency = _safe_int(params.get("frequency"))
        if not symbol or not period_type or not frequency_type or frequency is None:
            return
        candles = raw.get("candles") if isinstance(raw, dict) else None
        if not isinstance(candles, list):
            return
        now = _utcnow()
        rows: list[tuple[Any, ...]] = []
        for c in candles:
            if not isinstance(c, dict):
                continue
            cdt = _parse_dt(c.get("datetime"))
            if cdt is None:
                continue
            rows.append(
                (
                    symbol,
                    cdt,
                    period_type,
                    frequency_type,
                    frequency,
                    _safe_float(c.get("open")),
                    _safe_float(c.get("high")),
                    _safe_float(c.get("low")),
                    _safe_float(c.get("close")),
                    _safe_int(c.get("volume")),
                    now,
                )
            )
        if not rows:
            return
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.executemany(
                    """
                    INSERT OR REPLACE INTO price_history_cache (
                        symbol, candle_datetime, period_type, frequency_type, frequency,
                        open, high, low, close, volume, fetched_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
                self._record_event("write", "price_history_cache")
            except duckdb.Error as exc:
                log.warning(
                    '{"event":"cache_put_failed","table":"price_history_cache","error":"%s"}',
                    exc,
                )

    # ------------------------------------------------------- option chain

    def get_option_chain(self, params: dict[str, Any]) -> dict[str, Any] | None:
        key = _hash_params(params)
        with self._lock:
            if self._conn is None:
                return None
            try:
                row = self._conn.execute(
                    "SELECT raw_json, fetched_at, ttl_seconds FROM option_chain_cache WHERE cache_key = ?",
                    [key],
                ).fetchone()
            except duckdb.Error as exc:
                log.warning(
                    '{"event":"cache_get_failed","table":"option_chain_cache","error":"%s"}',
                    exc,
                )
                return None
            if row is None:
                self._record_event("miss", "option_chain_cache")
                return None
            raw, fetched_at, ttl = row
            if _is_expired(fetched_at, ttl):
                self._record_event("expired", "option_chain_cache")
                return None
            self._record_event("hit", "option_chain_cache")
            return _deserialise(raw)

    def put_option_chain(
        self,
        params: dict[str, Any],
        raw: dict[str, Any],
        *,
        ttl_seconds: int | None = None,
    ) -> None:
        ttl = ttl_seconds if ttl_seconds is not None else DEFAULT_TTL_OPTION_CHAIN_S
        key = _hash_params(params)
        symbol = str(params.get("symbol", ""))
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO option_chain_cache
                        (cache_key, symbol, fetched_at, raw_json, ttl_seconds)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    [key, symbol, _utcnow(), json.dumps(raw, default=str), ttl],
                )
                self._record_event("write", "option_chain_cache")
            except duckdb.Error as exc:
                log.warning(
                    '{"event":"cache_put_failed","table":"option_chain_cache","error":"%s"}',
                    exc,
                )

    # --------------------------------------------------------- instruments

    def get_instruments(self, params: dict[str, Any]) -> dict[str, Any] | None:
        key = _hash_params(params)
        with self._lock:
            if self._conn is None:
                return None
            try:
                row = self._conn.execute(
                    "SELECT raw_json, fetched_at, ttl_seconds FROM instruments_cache WHERE cache_key = ?",
                    [key],
                ).fetchone()
            except duckdb.Error as exc:
                log.warning(
                    '{"event":"cache_get_failed","table":"instruments_cache","error":"%s"}',
                    exc,
                )
                return None
            if row is None:
                self._record_event("miss", "instruments_cache")
                return None
            raw, fetched_at, ttl = row
            if _is_expired(fetched_at, ttl):
                self._record_event("expired", "instruments_cache")
                return None
            self._record_event("hit", "instruments_cache")
            return _deserialise(raw)

    def put_instruments(
        self,
        params: dict[str, Any],
        raw: dict[str, Any],
        *,
        ttl_seconds: int | None = None,
    ) -> None:
        ttl = ttl_seconds if ttl_seconds is not None else DEFAULT_TTL_INSTRUMENTS_S
        key = _hash_params(params)
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO instruments_cache
                        (cache_key, fetched_at, raw_json, ttl_seconds)
                    VALUES (?, ?, ?, ?)
                    """,
                    [key, _utcnow(), json.dumps(raw, default=str), ttl],
                )
                self._record_event("write", "instruments_cache")
            except duckdb.Error as exc:
                log.warning(
                    '{"event":"cache_put_failed","table":"instruments_cache","error":"%s"}',
                    exc,
                )

    # -------------------------------------------------------- OLAP queries

    def query_candles(self, symbol: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Read-only OLAP query for the Shakeout playbook v2.

        Returns candles ordered by ``candle_datetime`` ascending.  Empty
        list on miss / DB unavailable.
        """
        s = start.astimezone(UTC).replace(tzinfo=None) if start.tzinfo is not None else start
        e = end.astimezone(UTC).replace(tzinfo=None) if end.tzinfo is not None else end
        with self._lock:
            if self._conn is None:
                return []
            try:
                rows = self._conn.execute(
                    """
                    SELECT candle_datetime, open, high, low, close, volume,
                           period_type, frequency_type, frequency
                    FROM price_history_cache
                    WHERE symbol = ?
                      AND candle_datetime >= ?
                      AND candle_datetime <= ?
                    ORDER BY candle_datetime ASC
                    """,
                    [symbol, s, e],
                ).fetchall()
            except duckdb.Error as exc:
                log.warning('{"event":"cache_query_failed","error":"%s"}', exc)
                return []
        out: list[dict[str, Any]] = []
        for cdt, o, h, lo, c, v, pt, ft, fr in rows:
            cdt_naive = cdt if isinstance(cdt, datetime) else _parse_dt(cdt)
            if cdt_naive is None:
                continue
            out.append(
                {
                    "datetime": cdt_naive.isoformat() + "Z",
                    "open": o,
                    "high": h,
                    "low": lo,
                    "close": c,
                    "volume": v,
                    "period_type": pt,
                    "frequency_type": ft,
                    "frequency": fr,
                }
            )
        return out

    # ------------------------------------------------------------- stats

    def get_stats(self) -> CacheStats:
        rows: dict[str, int] = {}
        expired: dict[str, int] = {}
        hits = 0
        misses = 0
        size_mb = 0.0
        if self.db_path.exists():
            try:
                size_mb = self.db_path.stat().st_size / (1024 * 1024)
            except OSError:
                size_mb = 0.0
        with self._lock:
            if self._conn is not None:
                for tbl in _TABLE_NAMES:
                    try:
                        c = self._conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()  # noqa: S608
                        rows[tbl] = int(c[0]) if c else 0
                    except duckdb.Error:
                        rows[tbl] = 0
                    expired[tbl] = self._count_expired(tbl)
                cutoff = _utcnow() - timedelta(hours=24)
                try:
                    h = self._conn.execute(
                        "SELECT COUNT(*) FROM cache_events WHERE kind = 'hit' AND ts >= ?",
                        [cutoff],
                    ).fetchone()
                    hits = int(h[0]) if h else 0
                    m = self._conn.execute(
                        "SELECT COUNT(*) FROM cache_events WHERE kind IN ('miss', 'expired') AND ts >= ?",
                        [cutoff],
                    ).fetchone()
                    misses = int(m[0]) if m else 0
                except duckdb.Error:
                    pass
        total = hits + misses
        hit_rate = (hits / total) if total > 0 else None
        return CacheStats(
            db_path=str(self.db_path),
            enabled=cache_enabled(),
            size_mb=size_mb,
            rows_per_table=rows,
            expired_rows=expired,
            hit_rate_24h=hit_rate,
            hits_24h=hits,
            misses_24h=misses,
        )

    def _count_expired(self, table: str) -> int:
        if self._conn is None:
            return 0
        if table == "price_history_cache":
            # Recent candles only; historical candles are immutable.
            try:
                cutoff = _utcnow() - timedelta(seconds=RECENT_CANDLE_BOUNDARY_S)
                stale = _utcnow() - timedelta(seconds=DEFAULT_TTL_PRICE_HISTORY_RECENT_S)
                row = self._conn.execute(
                    """
                    SELECT COUNT(*) FROM price_history_cache
                    WHERE candle_datetime >= ? AND fetched_at < ?
                    """,
                    [cutoff, stale],
                ).fetchone()
                return int(row[0]) if row else 0
            except duckdb.Error:
                return 0
        if table not in _TABLE_NAMES:
            return 0
        try:
            row = self._conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE fetched_at + INTERVAL (ttl_seconds) SECOND < CURRENT_TIMESTAMP"  # noqa: S608
            ).fetchone()
            return int(row[0]) if row else 0
        except duckdb.Error:
            return 0

    # ------------------------------------------------ hourly breakdown

    def hourly_breakdown(self, hours: int = 24) -> list[dict[str, Any]]:
        """Per-hour cache hit/miss/expired counts for the last ``hours`` hours.

        Returns a list of dicts in chronological order:

            [{"hour_utc": "2026-05-23T10:00:00Z", "hits": 42,
              "misses": 8, "expired": 2}, ...]

        Used by ``get_cache_stats`` and the SLO compliance checker.
        Returns ``[]`` if the DB is unavailable or no events fall in the
        window.  Hours with zero events are *not* emitted (sparse output).
        """
        if hours < 1:
            raise ValueError("hours must be >= 1")
        with self._lock:
            if self._conn is None:
                return []
            cutoff = _utcnow() - timedelta(hours=hours)
            try:
                rows = self._conn.execute(
                    """
                    SELECT
                        DATE_TRUNC('hour', ts) AS hour_utc,
                        SUM(CASE WHEN kind = 'hit' THEN 1 ELSE 0 END) AS hits,
                        SUM(CASE WHEN kind = 'miss' THEN 1 ELSE 0 END) AS misses,
                        SUM(CASE WHEN kind = 'expired' THEN 1 ELSE 0 END) AS expired
                    FROM cache_events
                    WHERE ts >= ?
                    GROUP BY hour_utc
                    ORDER BY hour_utc ASC
                    """,
                    [cutoff],
                ).fetchall()
            except duckdb.Error as exc:
                log.warning('{"event":"cache_hourly_breakdown_failed","error":"%s"}', exc)
                return []
        out: list[dict[str, Any]] = []
        for hour_dt, hits, misses, expired in rows:
            iso = _format_hour_utc(hour_dt)
            if iso is None:
                continue
            out.append(
                {
                    "hour_utc": iso,
                    "hits": int(hits or 0),
                    "misses": int(misses or 0),
                    "expired": int(expired or 0),
                }
            )
        return out

    # -------------------------------------------------- truncate expired

    def truncate_expired(self) -> int:
        """Delete expired rows; return count removed."""
        deleted = 0
        with self._lock:
            if self._conn is None:
                return 0
            for tbl in ("quotes_cache", "option_chain_cache", "instruments_cache"):
                try:
                    res = self._conn.execute(
                        f"DELETE FROM {tbl} WHERE fetched_at + INTERVAL (ttl_seconds) SECOND < CURRENT_TIMESTAMP"  # noqa: S608
                    )
                    n = res.fetchone()
                    if n is not None and n[0] is not None:
                        deleted += int(n[0])
                except duckdb.Error as exc:
                    log.warning('{"event":"cache_truncate_failed","table":"%s","error":"%s"}', tbl, exc)
            # cache_events older than 30 days
            try:
                cutoff = _utcnow() - timedelta(days=30)
                self._conn.execute("DELETE FROM cache_events WHERE ts < ?", [cutoff])
            except duckdb.Error:
                pass
        return deleted

    # ----------------------------------------------------------------- ops

    def reset(self) -> None:
        """Drop all rows.  Test-only convenience; prod code should not call this."""
        with self._lock:
            if self._conn is None:
                return
            for tbl in (*_TABLE_NAMES, "cache_events"):
                try:
                    self._conn.execute(f"DELETE FROM {tbl}")  # noqa: S608
                except duckdb.Error:
                    pass


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _format_hour_utc(value: Any) -> str | None:
    """Format a DuckDB ``DATE_TRUNC('hour', ...)`` result as ISO-8601 UTC.

    DuckDB returns naive ``datetime`` for our naive-UTC ``ts`` column.
    We treat naive values as UTC (matches ``_utcnow``) and emit a
    canonical ``...Z`` suffix so consumers don't have to think about
    timezones.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.isoformat() + "Z"
        return value.astimezone(UTC).replace(tzinfo=None).isoformat() + "Z"
    parsed = _parse_dt(value)
    if parsed is None:
        return None
    return parsed.isoformat() + "Z"


def _is_expired(fetched_at: Any, ttl_seconds: Any) -> bool:
    if fetched_at is None or ttl_seconds is None:
        return True
    if not isinstance(fetched_at, datetime):
        parsed = _parse_dt(fetched_at)
        if parsed is None:
            return True
        fetched_at = parsed
    age = _utcnow() - fetched_at
    return bool(age.total_seconds() > float(ttl_seconds))


def _deserialise(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        loaded = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else None
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if isinstance(loaded, dict):
        return loaded
    return None


def _extract_quote_fields(symbol: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Pull the columnar fields out of Schwab's quote payload.

    Schwab's quote response is a top-level dict keyed by symbol; under
    that key are nested objects like ``quote``, ``regular``, etc.  We
    pluck the most common scalars; everything else is preserved in
    ``raw_json``.  Missing keys → ``None``.
    """
    inner = raw.get(symbol)
    body: dict[str, Any] = inner if isinstance(inner, dict) else raw
    quote_obj = body.get("quote")
    quote: dict[str, Any] = quote_obj if isinstance(quote_obj, dict) else {}
    regular_obj = body.get("regular")
    regular: dict[str, Any] = regular_obj if isinstance(regular_obj, dict) else {}
    return {
        "bid": _safe_float(quote.get("bidPrice") or quote.get("bid")),
        "ask": _safe_float(quote.get("askPrice") or quote.get("ask")),
        "last": _safe_float(quote.get("lastPrice") or quote.get("last") or regular.get("regularMarketLastPrice")),
        "volume": _safe_int(quote.get("totalVolume") or quote.get("volume")),
        "open": _safe_float(quote.get("openPrice") or quote.get("open")),
        "high": _safe_float(quote.get("highPrice") or quote.get("high")),
        "low": _safe_float(quote.get("lowPrice") or quote.get("low")),
        "close": _safe_float(quote.get("closePrice") or quote.get("close")),
    }


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

_singleton: Cache | None = None
_singleton_lock = threading.Lock()


def get_cache() -> Cache | None:
    """Return the process-wide cache, or ``None`` if disabled.

    Honors :func:`cache_enabled` — when the env flag is off this returns
    ``None`` so the caller skips both the get and put paths.
    """
    if not cache_enabled():
        return None
    global _singleton
    if _singleton is not None:
        return _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = Cache()
    return _singleton


def reset_cache_singleton() -> None:
    """Test helper — close the singleton so the next call re-opens it."""
    global _singleton
    with _singleton_lock:
        if _singleton is not None:
            _singleton.close()
            _singleton = None


__all__ = [
    "CACHE_DB_FILENAME",
    "CACHE_DIR_NAME",
    "DEFAULT_TTL_INSTRUMENTS_S",
    "DEFAULT_TTL_OPTION_CHAIN_S",
    "DEFAULT_TTL_PRICE_HISTORY_RECENT_S",
    "DEFAULT_TTL_QUOTES_S",
    "ENV_CACHE_BYPASS",
    "ENV_CACHE_ENABLED",
    "RECENT_CANDLE_BOUNDARY_S",
    "Cache",
    "CacheStats",
    "cache_bypass",
    "cache_enabled",
    "default_db_path",
    "get_cache",
    "reset_cache_singleton",
]
