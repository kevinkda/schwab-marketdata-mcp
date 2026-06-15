"""Pluggable response + derived-analysis cache for Schwab Market Data (v0.7 T0).

.. versionchanged:: 0.5.0
    ⚠️ **BREAKING** — the embedded DuckDB cache is removed.  The cache now
    delegates to a pluggable
    :class:`~schwab_marketdata_mcp.cache_backend.CacheBackend`:

    * **memory** (default) — in-process LRU + TTL response cache, zero
      external dependency, concurrency-safe, non-blocking (no global
      ``RLock``, no file locks).  Derived-analysis history
      (``option_chain_snapshots`` / ``iv_history`` / candle OLAP) keeps **no
      durable store**, so those methods degrade gracefully:
      ``write_option_chain_snapshot`` → ``0`` rows, ``aggregate_atm_iv`` →
      all-``None`` buckets, ``get_iv_percentile_rank`` → empty-history dict,
      ``query_candles`` → ``[]``.  All 15 tools keep working.
    * **clickhouse** (opt-in) — ``pip install schwab-marketdata-mcp[clickhouse]``
      + ``SCHWAB_CLICKHOUSE_URL`` + ``SCHWAB_CACHE_BACKEND=clickhouse`` to
      durably persist the derived-analysis history time series and serve the
      real IV-percentile / ATM-IV / candle-OLAP analytics.

    Selection via ``SCHWAB_CACHE_BACKEND`` (``memory`` | ``clickhouse``,
    default ``memory``).

The legacy per-table public API (``get_quote`` / ``put_quote`` /
``get_price_history`` / ``put_price_history`` / ``get_option_chain`` /
``put_option_chain`` / ``get_instruments`` / ``put_instruments`` /
``write_option_chain_snapshot`` / ``aggregate_atm_iv`` /
``get_iv_percentile_rank`` / ``query_candles`` / ``get_stats`` /
``hourly_breakdown`` / ``truncate_expired`` / ``reset``) is preserved
verbatim so the 15 MCP tools require no changes.

Failure mode: best-effort — every backend swallows storage errors and the
caller falls through to the live API.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final

from .cache_backend import (
    CacheBackend,
    get_cache_backend,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_TTL_QUOTES_S: Final[int] = 60
DEFAULT_TTL_PRICE_HISTORY_RECENT_S: Final[int] = 60
RECENT_CANDLE_BOUNDARY_S: Final[int] = 3600  # candles within last hour are "recent"
DEFAULT_TTL_OPTION_CHAIN_S: Final[int] = 300
DEFAULT_TTL_INSTRUMENTS_S: Final[int] = 86_400

# v0.4 P1/C — IV percentile materialisation buckets.
IV_BUCKET_30D_DAYS: Final[int] = 30
IV_BUCKET_60D_DAYS: Final[int] = 60
IV_BUCKET_90D_DAYS: Final[int] = 90
IV_BUCKET_TOLERANCE_DAYS: Final[int] = 7
DEFAULT_IV_LOOKBACK_DAYS: Final[int] = 252  # ~1 trading year

CACHE_DIR_NAME: Final[str] = "schwab-marketdata-mcp"

ENV_CACHE_ENABLED: Final[str] = "SCHWAB_CACHE_ENABLED"
ENV_CACHE_BYPASS: Final[str] = "SCHWAB_CACHE_BYPASS"

# Response-cache logical tables (delegated to backend.get/set).
_QUOTES_TABLE: Final[str] = "quotes_cache"
_PRICE_HISTORY_TABLE: Final[str] = "price_history_cache"
_OPTION_CHAIN_TABLE: Final[str] = "option_chain_cache"
_INSTRUMENTS_TABLE: Final[str] = "instruments_cache"

_RESPONSE_TABLE_NAMES: Final[tuple[str, ...]] = (
    _QUOTES_TABLE,
    _PRICE_HISTORY_TABLE,
    _OPTION_CHAIN_TABLE,
    _INSTRUMENTS_TABLE,
)

# Derived-analysis time series (delegated to backend.append/query_timeseries).
_SNAPSHOTS_SERIES: Final[str] = "option_chain_snapshots"
_IV_HISTORY_SERIES: Final[str] = "iv_history"
_CANDLES_SERIES: Final[str] = "price_history_candles"

_GLOBAL_KEY: Final[str] = "global"


def _truthy(raw: str | None, *, default: bool) -> bool:
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def cache_enabled() -> bool:
    """Honor ``SCHWAB_CACHE_ENABLED`` (default off — opt-in).

    .. versionchanged:: 0.4.2
        cache now opt-in, default disabled.
    """
    return _truthy(os.environ.get(ENV_CACHE_ENABLED), default=False)


def cache_bypass() -> bool:
    """Honor ``SCHWAB_CACHE_BYPASS`` (default off — single-call force fresh)."""
    return _truthy(os.environ.get(ENV_CACHE_BYPASS), default=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(tz=UTC).replace(tzinfo=None)


def _hash_params(params: dict[str, Any]) -> str:
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


def _extract_quote_fields(symbol: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Pull the columnar fields out of Schwab's quote payload (best-effort)."""
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
# Stats payload
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CacheStats:
    backend: str
    enabled: bool
    entries: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "enabled": self.enabled,
            "entries": self.entries,
        }


# ---------------------------------------------------------------------------
# Cache facade
# ---------------------------------------------------------------------------


class Cache:
    """Backend-agnostic response + derived-analysis cache.  One per process.

    Delegates response-cache storage to :meth:`CacheBackend.get` /
    :meth:`CacheBackend.set` and derived-analysis history to
    :meth:`CacheBackend.append_timeseries` / :meth:`query_timeseries`.  The
    memory backend keeps no durable history, so analytics degrade gracefully
    while the four response-cache tables still work in-process.
    """

    def __init__(self, backend: CacheBackend | None = None) -> None:
        self.backend: CacheBackend = backend if backend is not None else get_cache_backend()
        self._lock = threading.Lock()

    def close(self) -> None:
        return None

    def __enter__(self) -> Cache:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        del exc_type, exc, tb
        self.close()

    # ------------------------------------------------------------- quotes

    def get_quote(self, symbol: str) -> dict[str, Any] | None:
        hit = self.backend.get(_QUOTES_TABLE, symbol)
        return hit.get("raw") if isinstance(hit, dict) else None

    def put_quote(self, symbol: str, raw: dict[str, Any], *, ttl_seconds: int | None = None) -> None:
        ttl = ttl_seconds if ttl_seconds is not None else DEFAULT_TTL_QUOTES_S
        fields = _extract_quote_fields(symbol, raw)
        self.backend.set(_QUOTES_TABLE, symbol, {"raw": raw, "fields": fields}, ttl)

    # ----------------------------------------------------- price history

    def get_price_history(self, params: dict[str, Any]) -> dict[str, Any] | None:
        symbol = str(params.get("symbol", ""))
        period_type = str(params.get("period_type") or "")
        frequency_type = str(params.get("frequency_type") or "")
        frequency = _safe_int(params.get("frequency"))
        if not symbol or not period_type or not frequency_type or frequency is None:
            return None
        key = _hash_params(
            {
                "symbol": symbol,
                "period_type": period_type,
                "frequency_type": frequency_type,
                "frequency": frequency,
            }
        )
        hit = self.backend.get(_PRICE_HISTORY_TABLE, key)
        if not isinstance(hit, dict):
            return None
        return hit.get("raw")

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
        key = _hash_params(
            {
                "symbol": symbol,
                "period_type": period_type,
                "frequency_type": frequency_type,
                "frequency": frequency,
            }
        )
        self.backend.set(_PRICE_HISTORY_TABLE, key, {"raw": raw}, DEFAULT_TTL_PRICE_HISTORY_RECENT_S)
        # Also append normalised candles to the OLAP time series so
        # ``query_candles`` can read historical bars (ClickHouse only).
        self._append_candles(symbol, period_type, frequency_type, frequency, candles)

    def _append_candles(
        self,
        symbol: str,
        period_type: str,
        frequency_type: str,
        frequency: int,
        candles: list[Any],
    ) -> None:
        rows: list[dict[str, Any]] = []
        for c in candles:
            if not isinstance(c, dict):
                continue
            cdt = _parse_dt(c.get("datetime"))
            if cdt is None:
                continue
            rows.append(
                {
                    "symbol": symbol,
                    "candle_datetime": cdt.isoformat(),
                    "period_type": period_type,
                    "frequency_type": frequency_type,
                    "frequency": frequency,
                    "open": _safe_float(c.get("open")),
                    "high": _safe_float(c.get("high")),
                    "low": _safe_float(c.get("low")),
                    "close": _safe_float(c.get("close")),
                    "volume": _safe_int(c.get("volume")),
                }
            )
        if not rows:
            return
        with self._lock:
            for row in rows:
                try:
                    result = self.backend.append_timeseries(_CANDLES_SERIES, row)
                except Exception as exc:  # pragma: no cover - defensive
                    log.debug("append candle failed (best-effort): %s", exc)
                    break
                if result.get("status") != "ok":
                    break

    # ------------------------------------------------------- option chain

    def get_option_chain(self, params: dict[str, Any]) -> dict[str, Any] | None:
        hit = self.backend.get(_OPTION_CHAIN_TABLE, _hash_params(params))
        return hit.get("raw") if isinstance(hit, dict) else None

    def put_option_chain(
        self,
        params: dict[str, Any],
        raw: dict[str, Any],
        *,
        ttl_seconds: int | None = None,
    ) -> None:
        ttl = ttl_seconds if ttl_seconds is not None else DEFAULT_TTL_OPTION_CHAIN_S
        self.backend.set(_OPTION_CHAIN_TABLE, _hash_params(params), {"raw": raw}, ttl)

    # --------------------------------------------------------- instruments

    def get_instruments(self, params: dict[str, Any]) -> dict[str, Any] | None:
        hit = self.backend.get(_INSTRUMENTS_TABLE, _hash_params(params))
        return hit.get("raw") if isinstance(hit, dict) else None

    def put_instruments(
        self,
        params: dict[str, Any],
        raw: dict[str, Any],
        *,
        ttl_seconds: int | None = None,
    ) -> None:
        ttl = ttl_seconds if ttl_seconds is not None else DEFAULT_TTL_INSTRUMENTS_S
        self.backend.set(_INSTRUMENTS_TABLE, _hash_params(params), {"raw": raw}, ttl)

    # ----------------------------------------------- option chain snapshots

    def write_option_chain_snapshot(
        self,
        underlying: str,
        snapshot_at: datetime,
        contracts: list[dict[str, Any]],
    ) -> int:
        """Persist a flattened option-chain snapshot to the analytics series.

        Returns the number of rows durably persisted (``0`` on the memory
        backend, which keeps no history, or on error).  Each contract is
        appended to the ``option_chain_snapshots`` time series.
        """
        if not isinstance(underlying, str) or not underlying:
            return 0
        if not isinstance(contracts, list) or not contracts:
            return 0
        snapshot_at_naive = _normalise_naive_utc(snapshot_at)
        rows: list[dict[str, Any]] = []
        for contract in contracts:
            row = _normalise_option_contract(underlying, snapshot_at_naive, contract)
            if row is not None:
                rows.append(row)
        if not rows:
            return 0
        persisted = 0
        with self._lock:
            for row in rows:
                try:
                    result = self.backend.append_timeseries(_SNAPSHOTS_SERIES, row)
                except Exception as exc:  # pragma: no cover - defensive
                    log.warning("snapshot append failed (best-effort): %s", exc)
                    break
                if result.get("status") == "ok":
                    persisted += 1
                else:
                    break
        return persisted

    def aggregate_atm_iv(
        self,
        underlying: str,
        asof_date: date,
        snapshot_at: datetime | None = None,
    ) -> dict[str, float | None]:
        """Compute ATM IV for the 30/60/90-day buckets and persist to iv_history.

        On the memory backend (no durable snapshots) every bucket is ``None``
        — there is no history to aggregate.  On ClickHouse the latest snapshot
        is read back and the ATM IV computed per bucket, then written to the
        ``iv_history`` series.
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
        """Return decoded contracts from the most recent snapshot at/<= cutoff.

        Reads the ``option_chain_snapshots`` series via the backend; on the
        memory backend this yields the degradation signal → empty list.
        """
        out_all = self._query_series(_SNAPSHOTS_SERIES, limit=100_000)
        rows = [r for r in out_all if r.get("underlying") == underlying]
        if not rows:
            return []
        # Pick the latest snapshot_at at or before cutoff.
        cutoff_iso = cutoff.isoformat()
        eligible = [r for r in rows if str(r.get("snapshot_at") or "") <= cutoff_iso]
        if not eligible:
            return []
        latest_snapshot = max(str(r.get("snapshot_at") or "") for r in eligible)
        latest_rows = [r for r in eligible if str(r.get("snapshot_at") or "") == latest_snapshot]
        out: list[dict[str, Any]] = []
        for r in latest_rows:
            iso_expiry = _coerce_date(r.get("expiry"))
            if iso_expiry is None:
                continue
            out.append(
                {
                    "expiry": iso_expiry,
                    "strike": _safe_float(r.get("strike")),
                    "call_put": str(r.get("call_put") or "").upper(),
                    "implied_vol": _safe_float(r.get("implied_vol")),
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
        row = {
            "underlying": underlying,
            "asof_date": asof_date.isoformat(),
            "expiry_bucket": bucket,
            "atm_iv": atm_iv,
            "sample_count": int(sample_count),
        }
        try:
            self.backend.append_timeseries(_IV_HISTORY_SERIES, row)
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("iv_history append failed (best-effort): %s", exc)

    def get_iv_percentile_rank(
        self,
        underlying: str,
        expiry_bucket: str,
        lookback_days: int = DEFAULT_IV_LOOKBACK_DAYS,
    ) -> dict[str, Any]:
        """Compute the IV percentile rank for ``underlying`` / ``expiry_bucket``.

        Reads the ``iv_history`` series via the backend.  On the memory
        backend (no durable history) this returns the empty-history dict
        (``sample_count = 0``) so the calling tool surfaces a warning.
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
        current_row = non_null[0]
        current_iv = float(current_row["atm_iv"])
        current_asof = current_row["asof_date"]
        history = [float(r["atm_iv"]) for r in non_null[1:]]
        if history:
            below = sum(1 for v in history if v <= current_iv)
            percentile_rank = round(100.0 * below / len(history), 2)
        else:
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
        rows = self._query_series(_IV_HISTORY_SERIES, limit=100_000)
        decoded: list[dict[str, Any]] = []
        for r in rows:
            if r.get("underlying") != underlying or r.get("expiry_bucket") != bucket:
                continue
            iso_date = _coerce_date(r.get("asof_date"))
            if iso_date is None or iso_date < cutoff:
                continue
            decoded.append(
                {
                    "asof_date": iso_date,
                    "atm_iv": _safe_float(r.get("atm_iv")),
                    "sample_count": _safe_int(r.get("sample_count")) or 0,
                }
            )
        # Most recent first (latest append wins for the same asof_date).
        decoded.sort(key=lambda d: d["asof_date"], reverse=True)
        return decoded

    # -------------------------------------------------------- OLAP queries

    def query_candles(self, symbol: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Read-only OLAP query for the Shakeout playbook v2.

        Reads the ``price_history_candles`` series via the backend.  Empty
        list on the memory backend (no durable history) or on miss.
        """
        s = start.astimezone(UTC).replace(tzinfo=None) if start.tzinfo is not None else start
        e = end.astimezone(UTC).replace(tzinfo=None) if end.tzinfo is not None else end
        rows = self._query_series(_CANDLES_SERIES, limit=1_000_000)
        out: list[dict[str, Any]] = []
        for r in rows:
            if r.get("symbol") != symbol:
                continue
            cdt = _parse_dt(r.get("candle_datetime"))
            if cdt is None or cdt < s or cdt > e:
                continue
            out.append(
                {
                    "datetime": cdt.isoformat() + "Z",
                    "open": r.get("open"),
                    "high": r.get("high"),
                    "low": r.get("low"),
                    "close": r.get("close"),
                    "volume": r.get("volume"),
                    "period_type": r.get("period_type"),
                    "frequency_type": r.get("frequency_type"),
                    "frequency": r.get("frequency"),
                }
            )
        out.sort(key=lambda d: d["datetime"])
        return out

    def _query_series(self, series: str, *, limit: int) -> list[dict[str, Any]]:
        """Return decoded rows from a derived-analysis time series.

        Best-effort: a degradation signal (memory backend) or an error
        payload yields an empty list, so analytics degrade gracefully.
        """
        try:
            result = self.backend.query_timeseries(series, {"limit": limit})
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("query_timeseries failed (best-effort): %s", exc)
            return []
        if result.get("status") != "ok":
            return []
        rows = result.get("rows")
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    # ------------------------------------------------------------- stats

    def get_stats(self) -> CacheStats:
        try:
            entries = self.backend.size()
        except Exception:
            entries = 0
        return CacheStats(
            backend=self.backend.name,
            enabled=cache_enabled(),
            entries=entries,
        )

    def hourly_breakdown(self, hours: int = 24) -> list[dict[str, Any]]:
        """Per-hour cache event breakdown.

        .. versionchanged:: 0.5.0
            The DuckDB ``cache_events`` audit table is removed with the
            pluggable-backend swap; the pluggable backends expose a live
            entry count via :meth:`get_stats` instead of a per-hour event
            log.  Returns ``[]`` (no per-hour history) for API compatibility.
        """
        if hours < 1:
            raise ValueError("hours must be >= 1")
        return []

    def truncate_expired(self) -> int:
        """Drop expired response-cache entries; return count removed.

        The memory backend evicts lazily on read, and ClickHouse rows carry
        a TTL filter in the query path, so there is nothing to actively
        delete here — returns ``0``.
        """
        return 0

    def reset(self) -> None:
        """Drop all response-cache state.  Test-only convenience."""
        self.backend.clear()


# ---------------------------------------------------------------------------
# Option-chain snapshot helpers (v0.4 P1/C — unchanged analytics logic)
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
) -> dict[str, Any] | None:
    """Convert a single contract dict into a normalised time-series row.

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
    return {
        "underlying": underlying,
        "snapshot_at": snapshot_at.isoformat(),
        "expiry": expiry.isoformat(),
        "strike": strike,
        "call_put": call_put,
        "last_price": _safe_float(contract.get("last_price") or contract.get("last")),
        "bid": _safe_float(contract.get("bid")),
        "ask": _safe_float(contract.get("ask")),
        "volume": _safe_int(contract.get("volume") or contract.get("totalVolume")),
        "open_interest": _safe_int(contract.get("open_interest") or contract.get("openInterest")),
        "implied_vol": _safe_float(contract.get("implied_vol") or contract.get("volatility")),
        "delta": _safe_float(contract.get("delta")),
        "gamma": _safe_float(contract.get("gamma")),
        "theta": _safe_float(contract.get("theta")),
        "vega": _safe_float(contract.get("vega")),
        "rho": _safe_float(contract.get("rho")),
        "raw_json": raw_json,
    }


def flatten_option_chain_response(raw: Any) -> list[dict[str, Any]]:
    """Flatten a Schwab option-chain response into a list of contracts.

    Best-effort — non-dict shapes return an empty list rather than raising.
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
    if iv > 1.5:
        return iv / 100.0
    return iv


def _compute_atm_iv_for_bucket(
    contracts: list[dict[str, Any]],
    asof: date,
    bucket_days: int,
) -> tuple[float | None, int]:
    """Return ``(atm_iv, sample_count)`` for the given DTE bucket."""
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
    """Return the process-wide cache, or ``None`` if disabled."""
    if not cache_enabled():
        return None
    global _singleton
    if _singleton is not None:
        return _singleton
    with _singleton_lock:
        if _singleton is None:  # pragma: no branch - double-checked lock
            try:
                _singleton = Cache()
            except Exception as exc:  # pragma: no cover - defensive backend init
                log.warning("Cache init failed; running without cache: %s", exc)
                return None
    return _singleton


def reset_cache_singleton() -> None:
    """Test helper — drop the singleton so the next call re-creates it."""
    global _singleton
    with _singleton_lock:
        if _singleton is not None:
            _singleton.close()
            _singleton = None


__all__ = [
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
    "flatten_option_chain_response",
    "get_cache",
    "reset_cache_singleton",
]
