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
from datetime import UTC, date, datetime, timedelta
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

# v0.4 P1/C — IV percentile materialisation.
# DTE buckets used by ``aggregate_atm_iv`` / ``get_iv_percentile_rank``.
# Stored as VARCHAR ('30d' / '60d' / '90d') so future buckets (e.g. '180d')
# can be added without an ALTER TABLE.
IV_BUCKET_30D_DAYS: Final[int] = 30
IV_BUCKET_60D_DAYS: Final[int] = 60
IV_BUCKET_90D_DAYS: Final[int] = 90
IV_BUCKET_TOLERANCE_DAYS: Final[int] = 7  # how close DTE must be to bucket centre
DEFAULT_IV_LOOKBACK_DAYS: Final[int] = 252  # ~1 trading year

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
    # ----- v0.4 P1/C — structured option chain snapshot + IV history -----
    # Distinct from ``option_chain_cache`` (which is the legacy raw JSON
    # response cache keyed by query-hash).  ``option_chain_snapshots`` is
    # row-normalised so we can compute analytics (ATM IV, Greeks
    # distributions, etc.) directly with SQL.
    """
    CREATE TABLE IF NOT EXISTS option_chain_snapshots (
        underlying VARCHAR NOT NULL,
        snapshot_at TIMESTAMP NOT NULL,
        expiry DATE NOT NULL,
        strike DECIMAL(18,4) NOT NULL,
        call_put VARCHAR(4) NOT NULL,
        last_price DECIMAL(18,4),
        bid DECIMAL(18,4),
        ask DECIMAL(18,4),
        volume BIGINT,
        open_interest BIGINT,
        implied_vol DECIMAL(8,6),
        delta DECIMAL(8,6),
        gamma DECIMAL(8,6),
        theta DECIMAL(8,6),
        vega DECIMAL(8,6),
        rho DECIMAL(8,6),
        raw_json JSON,
        PRIMARY KEY (underlying, snapshot_at, expiry, strike, call_put)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_option_chain_snapshots_underlying_time
        ON option_chain_snapshots(underlying, snapshot_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS iv_history (
        underlying VARCHAR NOT NULL,
        asof_date DATE NOT NULL,
        expiry_bucket VARCHAR NOT NULL,
        atm_iv DECIMAL(8,6),
        sample_count INTEGER,
        PRIMARY KEY (underlying, asof_date, expiry_bucket)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_iv_history_lookup
        ON iv_history(underlying, expiry_bucket, asof_date DESC)
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

    # ----------------------------------------------- option chain snapshots

    def write_option_chain_snapshot(
        self,
        underlying: str,
        snapshot_at: datetime,
        contracts: list[dict[str, Any]],
    ) -> int:
        """Persist a flattened option-chain snapshot to ``option_chain_snapshots``.

        ``contracts`` is a list of dicts; each dict must carry at minimum
        ``expiry`` (date | str | datetime), ``strike`` (numeric), and
        ``call_put`` (``'CALL'`` / ``'PUT'`` / ``'C'`` / ``'P'``).  Any
        Greek / volume / IV field is optional — missing values land as
        ``NULL``.

        Returns the number of rows successfully inserted (0 on error or
        DB unavailable).  Idempotent: re-writing the same
        ``(underlying, snapshot_at, expiry, strike, call_put)`` tuple
        replaces the prior row.
        """
        if not isinstance(underlying, str) or not underlying:
            return 0
        if not isinstance(contracts, list) or not contracts:
            return 0
        snapshot_at_naive = _normalise_naive_utc(snapshot_at)
        rows: list[tuple[Any, ...]] = []
        for contract in contracts:
            row = _normalise_option_contract(underlying, snapshot_at_naive, contract)
            if row is not None:
                rows.append(row)
        if not rows:
            return 0
        with self._lock:
            if self._conn is None:
                return 0
            try:
                self._conn.executemany(
                    """
                    INSERT OR REPLACE INTO option_chain_snapshots (
                        underlying, snapshot_at, expiry, strike, call_put,
                        last_price, bid, ask, volume, open_interest,
                        implied_vol, delta, gamma, theta, vega, rho, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
                self._record_event("write", "option_chain_snapshots")
            except duckdb.Error as exc:
                log.warning(
                    '{"event":"cache_put_failed","table":"option_chain_snapshots","error":"%s"}',
                    exc,
                )
                return 0
        return len(rows)

    def aggregate_atm_iv(
        self,
        underlying: str,
        asof_date: date,
        snapshot_at: datetime | None = None,
    ) -> dict[str, float | None]:
        """Compute ATM IV for the 30/60/90-day buckets and write to ``iv_history``.

        For each bucket the algorithm is:

        1. Pick the most recent snapshot of ``underlying`` at or before
           ``snapshot_at`` (default: end of ``asof_date`` UTC).
        2. Filter contracts whose ``DTE = expiry - asof_date`` falls
           inside ``[bucket_days - tolerance, bucket_days + tolerance]``.
        3. Inside that bucket, pick the strike closest to the
           snapshot's ATM (median strike of the filtered slice — robust
           to missing underlying price).
        4. ATM IV = average of CALL-IV and PUT-IV at that strike.

        Buckets with zero samples produce ``None`` and are still
        written (sample_count = 0) so the lookback window has the
        right calendar density.

        Returns a dict ``{'30d': float|None, '60d': float|None, '90d': float|None}``.
        """
        if not isinstance(underlying, str) or not underlying:
            return {"30d": None, "60d": None, "90d": None}
        asof = _coerce_date(asof_date)
        if asof is None:
            return {"30d": None, "60d": None, "90d": None}
        cutoff = (
            _normalise_naive_utc(snapshot_at)
            if snapshot_at is not None
            else datetime.combine(asof, datetime.max.time())
        )
        contracts = self._fetch_latest_snapshot_contracts(underlying, cutoff)
        result: dict[str, float | None] = {}
        for bucket_days, label in (
            (IV_BUCKET_30D_DAYS, "30d"),
            (IV_BUCKET_60D_DAYS, "60d"),
            (IV_BUCKET_90D_DAYS, "90d"),
        ):
            atm_iv, sample_count = _compute_atm_iv_for_bucket(contracts, asof, bucket_days)
            self._upsert_iv_history(underlying, asof, label, atm_iv, sample_count)
            result[label] = atm_iv
        return result

    def _fetch_latest_snapshot_contracts(
        self,
        underlying: str,
        cutoff: datetime,
    ) -> list[dict[str, Any]]:
        """Return all rows from the most recent snapshot at or before ``cutoff``."""
        with self._lock:
            if self._conn is None:
                return []
            try:
                latest = self._conn.execute(
                    """
                    SELECT MAX(snapshot_at) FROM option_chain_snapshots
                    WHERE underlying = ? AND snapshot_at <= ?
                    """,
                    [underlying, cutoff],
                ).fetchone()
                if latest is None or latest[0] is None:
                    return []
                snapshot_at = latest[0]
                rows = self._conn.execute(
                    """
                    SELECT expiry, strike, call_put, implied_vol
                    FROM option_chain_snapshots
                    WHERE underlying = ? AND snapshot_at = ?
                    """,
                    [underlying, snapshot_at],
                ).fetchall()
            except duckdb.Error as exc:
                log.warning(
                    '{"event":"cache_query_failed","table":"option_chain_snapshots","error":"%s"}',
                    exc,
                )
                return []
        out: list[dict[str, Any]] = []
        for expiry, strike, call_put, iv in rows:
            iso_expiry = _coerce_date(expiry)
            if iso_expiry is None:
                continue
            out.append(
                {
                    "expiry": iso_expiry,
                    "strike": _safe_float(strike),
                    "call_put": str(call_put).upper(),
                    "implied_vol": _safe_float(iv),
                }
            )
        return out

    def _upsert_iv_history(
        self,
        underlying: str,
        asof_date: date,
        bucket: str,
        atm_iv: float | None,
        sample_count: int,
    ) -> None:
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO iv_history
                        (underlying, asof_date, expiry_bucket, atm_iv, sample_count)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    [underlying, asof_date, bucket, atm_iv, int(sample_count)],
                )
                self._record_event("write", "iv_history")
            except duckdb.Error as exc:
                log.warning(
                    '{"event":"cache_put_failed","table":"iv_history","error":"%s"}',
                    exc,
                )

    def get_iv_percentile_rank(
        self,
        underlying: str,
        expiry_bucket: str,
        lookback_days: int = DEFAULT_IV_LOOKBACK_DAYS,
    ) -> dict[str, Any]:
        """Compute the IV percentile rank for ``underlying`` / ``expiry_bucket``.

        ``percentile_rank`` is computed against the historical
        distribution in ``iv_history`` over the past ``lookback_days``
        calendar days, *excluding* the current row.  Returns 0-100 where
        100 means "current IV is at or above every prior observation".

        Returns the structured dict described in the v0.4 P1/C plan.
        Fields default to ``None`` when there is no history.
        """
        bucket = (expiry_bucket or "").lower()
        if bucket not in {"30d", "60d", "90d"}:
            raise ValueError(f"unsupported expiry_bucket {expiry_bucket!r}; expected '30d'|'60d'|'90d'")
        if not isinstance(lookback_days, int) or lookback_days < 1:
            raise ValueError(f"lookback_days must be a positive int; got {lookback_days!r}")
        if not isinstance(underlying, str) or not underlying:
            raise ValueError("underlying must be a non-empty str")

        rows = self._fetch_iv_history_window(underlying, bucket, lookback_days)
        non_null = [r for r in rows if r["atm_iv"] is not None]
        if not non_null:
            return {
                "underlying": underlying,
                "expiry_bucket": bucket,
                "current_iv": None,
                "percentile_rank": None,
                "sample_count": 0,
                "lookback_days": lookback_days,
                "min_iv": None,
                "max_iv": None,
                "median_iv": None,
                "current_asof": None,
            }
        # Most recent first per ORDER BY DESC.
        current_row = non_null[0]
        current_iv = float(current_row["atm_iv"])
        current_asof = current_row["asof_date"]
        history = [float(r["atm_iv"]) for r in non_null[1:]]
        if history:
            below = sum(1 for v in history if v <= current_iv)
            percentile_rank = round(100.0 * below / len(history), 2)
        else:
            # Only one observation in the window; rank undefined,
            # report 50.0 as a neutral middle value rather than None
            # so downstream reasoners can still display a number.
            percentile_rank = 50.0
        ivs = [float(r["atm_iv"]) for r in non_null]
        return {
            "underlying": underlying,
            "expiry_bucket": bucket,
            "current_iv": current_iv,
            "percentile_rank": percentile_rank,
            "sample_count": len(non_null),
            "lookback_days": lookback_days,
            "min_iv": min(ivs),
            "max_iv": max(ivs),
            "median_iv": _median(ivs),
            "current_asof": current_asof.isoformat() if isinstance(current_asof, date) else None,
        }

    def _fetch_iv_history_window(
        self,
        underlying: str,
        bucket: str,
        lookback_days: int,
    ) -> list[dict[str, Any]]:
        cutoff = _utcnow().date() - timedelta(days=lookback_days)
        with self._lock:
            if self._conn is None:
                return []
            try:
                rows = self._conn.execute(
                    """
                    SELECT asof_date, atm_iv, sample_count
                    FROM iv_history
                    WHERE underlying = ? AND expiry_bucket = ? AND asof_date >= ?
                    ORDER BY asof_date DESC
                    """,
                    [underlying, bucket, cutoff],
                ).fetchall()
            except duckdb.Error as exc:
                log.warning(
                    '{"event":"cache_query_failed","table":"iv_history","error":"%s"}',
                    exc,
                )
                return []
        out: list[dict[str, Any]] = []
        for asof, atm_iv, sample_count in rows:
            iso_date = _coerce_date(asof)
            if iso_date is None:
                continue
            out.append(
                {
                    "asof_date": iso_date,
                    "atm_iv": _safe_float(atm_iv),
                    "sample_count": int(sample_count) if sample_count is not None else 0,
                }
            )
        return out

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
# Option-chain snapshot helpers (v0.4 P1/C)
# ---------------------------------------------------------------------------


def _normalise_naive_utc(value: Any) -> datetime:
    """Coerce ``value`` to a naive-UTC datetime; current UTC on failure."""
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(UTC).replace(tzinfo=None)
        return value
    parsed = _parse_dt(value)
    if parsed is not None:
        return parsed
    return _utcnow()


def _coerce_date(value: Any) -> date | None:
    """Coerce a date / datetime / iso-string / epoch to a ``date``."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value) / 1000.0, tz=UTC).date()
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(value, str):
        s = value.strip()
        # ``'2026-06-19:30'`` and ``'2026-06-19'`` are both legitimate
        # Schwab expDateMap key shapes.  Strip the optional DTE suffix.
        if ":" in s:
            s = s.split(":", 1)[0]
        try:
            return date.fromisoformat(s)
        except ValueError:
            parsed = _parse_dt(s)
            return parsed.date() if parsed is not None else None
    return None


_CALL_PUT_NORMALISE: Final[dict[str, str]] = {
    "C": "CALL",
    "P": "PUT",
    "CALL": "CALL",
    "PUT": "PUT",
}


def _normalise_call_put(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip().upper()
    return _CALL_PUT_NORMALISE.get(s)


def _normalise_option_contract(
    underlying: str,
    snapshot_at: datetime,
    contract: Any,
) -> tuple[Any, ...] | None:
    """Convert a single contract dict into a row tuple for the INSERT.

    Returns ``None`` if any required field (expiry / strike / call_put)
    is missing or unparseable — those rows are silently dropped to keep
    the writer best-effort.
    """
    if not isinstance(contract, dict):
        return None
    expiry = _coerce_date(contract.get("expiry") or contract.get("expirationDate"))
    if expiry is None:
        return None
    strike = _safe_float(contract.get("strike") or contract.get("strikePrice"))
    if strike is None:
        return None
    call_put = _normalise_call_put(contract.get("call_put") or contract.get("putCall"))
    if call_put is None:
        return None
    raw = contract.get("raw")
    raw_json = json.dumps(raw if raw is not None else contract, default=str)
    return (
        underlying,
        snapshot_at,
        expiry,
        strike,
        call_put,
        _safe_float(contract.get("last_price") or contract.get("last")),
        _safe_float(contract.get("bid")),
        _safe_float(contract.get("ask")),
        _safe_int(contract.get("volume") or contract.get("totalVolume")),
        _safe_int(contract.get("open_interest") or contract.get("openInterest")),
        _safe_float(contract.get("implied_vol") or contract.get("volatility")),
        _safe_float(contract.get("delta")),
        _safe_float(contract.get("gamma")),
        _safe_float(contract.get("theta")),
        _safe_float(contract.get("vega")),
        _safe_float(contract.get("rho")),
        raw_json,
    )


def flatten_option_chain_response(raw: Any) -> list[dict[str, Any]]:
    """Flatten a Schwab option-chain response into a list of contracts.

    Schwab returns ``callExpDateMap`` / ``putExpDateMap`` dicts shaped
    like ``{ '2026-06-19:30': { '190.0': [ { ...contract... } ] } }``.
    This helper unrolls both maps into a flat list with ``expiry`` /
    ``strike`` / ``call_put`` resolved on every entry, ready for
    :py:meth:`Cache.write_option_chain_snapshot`.

    Best-effort — non-dict shapes return an empty list rather than
    raising, so the cache layer never breaks the live tool path.
    """
    if not isinstance(raw, dict):
        return []
    out: list[dict[str, Any]] = []
    for map_key, default_cp in (("callExpDateMap", "CALL"), ("putExpDateMap", "PUT")):
        exp_map = raw.get(map_key)
        if not isinstance(exp_map, dict):
            continue
        for exp_key, strike_map in exp_map.items():
            if not isinstance(strike_map, dict):
                continue
            expiry = _coerce_date(exp_key)
            for strike_key, contracts in strike_map.items():
                if not isinstance(contracts, list):
                    continue
                strike = _safe_float(strike_key)
                for c in contracts:
                    if not isinstance(c, dict):
                        continue
                    cp = _normalise_call_put(c.get("putCall") or c.get("call_put") or default_cp) or default_cp
                    out.append(
                        {
                            "expiry": expiry,
                            "strike": strike,
                            "call_put": cp,
                            "bid": c.get("bid"),
                            "ask": c.get("ask"),
                            "last": c.get("last") or c.get("lastPrice") or c.get("mark"),
                            "volume": c.get("totalVolume") or c.get("volume"),
                            "openInterest": c.get("openInterest"),
                            "volatility": _coerce_iv(c.get("volatility") or c.get("impliedVolatility")),
                            "delta": c.get("delta"),
                            "gamma": c.get("gamma"),
                            "theta": c.get("theta"),
                            "vega": c.get("vega"),
                            "rho": c.get("rho"),
                            "raw": c,
                        }
                    )
    return out


def _coerce_iv(value: Any) -> float | None:
    """Schwab quotes IV as a percent (e.g. 32.5 = 32.5%); normalise to 0..1."""
    iv = _safe_float(value)
    if iv is None:
        return None
    if iv <= 0:
        return None
    # Heuristic: if the value looks like a percent (>= 1.5), divide by 100.
    # Genuine fractional IVs (e.g. 0.32) are kept as-is.  This matches the
    # convention used by every analytics consumer of ``iv_history``.
    if iv > 1.5:
        return iv / 100.0
    return iv


def _compute_atm_iv_for_bucket(
    contracts: list[dict[str, Any]],
    asof: date,
    bucket_days: int,
) -> tuple[float | None, int]:
    """Return ``(atm_iv, sample_count)`` for the given DTE bucket.

    ``contracts`` is the output of
    :py:meth:`Cache._fetch_latest_snapshot_contracts` — already
    decoded (expiry as ``date``, strike as float, IV as float|None).
    """
    lo = bucket_days - IV_BUCKET_TOLERANCE_DAYS
    hi = bucket_days + IV_BUCKET_TOLERANCE_DAYS
    in_bucket: list[dict[str, Any]] = []
    for c in contracts:
        expiry = c.get("expiry")
        if not isinstance(expiry, date):
            continue
        dte = (expiry - asof).days
        if dte < lo or dte > hi:
            continue
        if c.get("strike") is None:
            continue
        in_bucket.append(c)
    if not in_bucket:
        return None, 0
    strikes = sorted({float(c["strike"]) for c in in_bucket})
    median_strike = _median(strikes)
    if median_strike is None:
        return None, len(in_bucket)
    # Closest strike to the median (most liquid centre).
    atm_strike = min(strikes, key=lambda s: abs(s - median_strike))
    ivs: list[float] = []
    for c in in_bucket:
        if abs(float(c["strike"]) - atm_strike) > 1e-9:
            continue
        iv = c.get("implied_vol")
        if iv is None:
            continue
        ivs.append(float(iv))
    if not ivs:
        return None, len(in_bucket)
    return sum(ivs) / len(ivs), len(in_bucket)


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return float(s[mid])
    return (float(s[mid - 1]) + float(s[mid])) / 2.0


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
    "DEFAULT_IV_LOOKBACK_DAYS",
    "DEFAULT_TTL_INSTRUMENTS_S",
    "DEFAULT_TTL_OPTION_CHAIN_S",
    "DEFAULT_TTL_PRICE_HISTORY_RECENT_S",
    "DEFAULT_TTL_QUOTES_S",
    "ENV_CACHE_BYPASS",
    "ENV_CACHE_ENABLED",
    "IV_BUCKET_30D_DAYS",
    "IV_BUCKET_60D_DAYS",
    "IV_BUCKET_90D_DAYS",
    "IV_BUCKET_TOLERANCE_DAYS",
    "RECENT_CANDLE_BOUNDARY_S",
    "Cache",
    "CacheStats",
    "cache_bypass",
    "cache_enabled",
    "default_db_path",
    "flatten_option_chain_response",
    "get_cache",
    "reset_cache_singleton",
]
