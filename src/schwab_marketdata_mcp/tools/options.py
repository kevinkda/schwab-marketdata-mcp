"""``get_option_chain``, ``get_option_expiration_chain``, and
``get_iv_percentile`` tool impls.

v0.4 P1/C
---------

* ``get_option_chain_impl`` now also persists a *flattened* snapshot of
  the chain to ``option_chain_snapshots`` (the v0.4 row-normalised
  table, distinct from the legacy raw-JSON ``option_chain_cache``).
  This is opportunistic — failures never break the live tool path —
  and the response is enriched with ``_cached_rows`` so callers can
  see how many contracts landed in the analytics table.
* ``get_iv_percentile_impl`` is the 15th MCP tool: it computes the
  current ATM IV percentile rank for an underlying versus N days of
  cached history (default 252 ≈ 1 trading year).  When ``refresh=True``
  it first pulls a live option chain and aggregates ATM IV before
  computing the rank; when ``refresh=False`` it serves only from
  cached ``iv_history`` rows.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from ..cache import (
    DEFAULT_IV_LOOKBACK_DAYS,
    Cache,
    cache_bypass,
    cache_enabled,
    flatten_option_chain_response,
    get_cache,
)
from ..models import (
    GetIvPercentileInput,
    GetIvSurfaceInput,
    GetOptionChainInput,
    GetOptionExpirationChainInput,
    GetOptionGreeksSummaryInput,
)
from . import _enums
from ._runtime import call_endpoint

log = logging.getLogger(__name__)

# When the caller did not opt into ``refresh=True`` and the cache layer
# is disabled, the percentile-rank query has no data to read.  Surface
# this as a ``warning`` field rather than raising — the tool stays
# read-only and callers can decide how to react.
_WARN_CACHE_DISABLED = "cache_disabled"
_WARN_SAMPLE_SPARSE = "sample_count_below_30"
_MIN_SAMPLES_FOR_RANK = 30

# v0.6 T1/E1 — Greeks aggregation.  The five first-order/second-order
# Greeks we fold across the chain.
_GREEK_KEYS: tuple[str, ...] = ("delta", "gamma", "theta", "vega", "rho")


async def get_option_chain_impl(args: GetOptionChainInput) -> dict[str, Any]:
    ct = _enums.options_contract_type(args.contract_type)
    strat = _enums.options_strategy(args.strategy)
    sr = _enums.options_strike_range(args.strike_range)
    em = _enums.options_exp_month(args.exp_month)
    ot = _enums.options_type(args.option_type)
    ent = _enums.options_entitlement(args.entitlement)

    async def fetch(client: Any) -> Any:
        return await client.get_option_chain(
            args.symbol,
            contract_type=ct,
            strike_count=args.strike_count,
            include_underlying_quote=args.include_underlying_quote,
            strategy=strat,
            interval=args.interval,
            strike=args.strike,
            strike_range=sr,
            from_date=args.from_date,
            to_date=args.to_date,
            volatility=args.volatility,
            underlying_price=args.underlying_price,
            interest_rate=args.interest_rate,
            days_to_expiration=args.days_to_expiration,
            exp_month=em,
            option_type=ot,
            entitlement=ent,
        )

    cache_params = args.model_dump(mode="json", exclude_none=True)

    def _lookup(cache: Cache) -> dict[str, Any] | None:
        return cache.get_option_chain(cache_params)

    def _store(cache: Cache, raw: dict[str, Any]) -> None:
        cache.put_option_chain(cache_params, raw)

    payload = await call_endpoint(
        "get_option_chain",
        fetch,
        cache_lookup=_lookup,
        cache_store=_store,
    )

    # ----- v0.4 P1/C — persist a row-normalised snapshot for analytics ----
    # ``call_endpoint`` already populates ``_cache_status`` with one of
    # ``hit | miss | bypass | disabled``.  We *augment* the payload with
    # ``_cached_rows`` (number of contracts written to the analytics
    # table on this call).  The legacy ``option_chain_cache`` raw-JSON
    # table is untouched — that's the read-back hit path used by
    # subsequent calls with the same params.
    cached_rows = _persist_chain_snapshot(args.symbol, payload)
    payload["_cached_rows"] = cached_rows
    return payload


def _persist_chain_snapshot(underlying: str, payload: Any) -> int:
    """Write the flattened chain to ``option_chain_snapshots``.

    Returns the number of rows successfully inserted.  Always returns
    ``0`` when:

    * the cache layer is disabled by env var, or
    * ``_cache_status == 'hit'`` (we already wrote that snapshot the
      first time around — re-writing would just thrash the table with
      a near-identical row at a different ``snapshot_at``), or
    * the response carries no ``callExpDateMap`` / ``putExpDateMap``
      (e.g. error response, empty chain).

    All exceptions are swallowed and logged at WARNING — option chain
    is the live tool's hot path and analytics persistence must never
    break it.  See the cache module's failure-mode section.
    """
    if not cache_enabled() or cache_bypass():
        return 0
    if not isinstance(payload, dict):
        return 0
    # Avoid re-writing on cache hits (the snapshot is already there).
    if payload.get("_cache_status") == "hit":
        return 0
    cache = get_cache()
    if cache is None:
        return 0
    try:
        contracts = flatten_option_chain_response(payload)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning('{"event":"option_chain_flatten_failed","error":"%s"}', exc)
        return 0
    if not contracts:
        return 0
    snapshot_at = datetime.now(tz=UTC).replace(tzinfo=None)
    try:
        return cache.write_option_chain_snapshot(underlying, snapshot_at, contracts)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning('{"event":"option_chain_snapshot_write_failed","error":"%s"}', exc)
        return 0


async def get_option_expiration_chain_impl(
    args: GetOptionExpirationChainInput,
) -> dict[str, Any]:
    async def fetch(client: Any) -> Any:
        return await client.get_option_expiration_chain(args.symbol)

    return await call_endpoint("get_option_expiration_chain", fetch)


# ---------------------------------------------------------------------------
# v0.4 P1/C — get_iv_percentile (15th MCP tool)
# ---------------------------------------------------------------------------


async def get_iv_percentile_impl(args: GetIvPercentileInput) -> dict[str, Any]:
    """Return the IV percentile-rank dict described in the v0.4 plan.

    Read path (default, ``refresh=False``):

    * Read the most recent ``iv_history`` rows for
      ``(underlying, expiry_bucket)`` over ``lookback_days`` calendar
      days; compute ``percentile_rank`` of the most-recent ``atm_iv``.
    * If ``cache`` is unavailable or sample count < 30 the response
      includes a non-fatal ``warning`` field and ``percentile_rank``
      is set to ``None``.

    Refresh path (``refresh=True``):

    * Fetch a fresh option chain via the live ``get_option_chain``
      flow (cache-aware), flatten it into ``option_chain_snapshots``,
      run :py:meth:`Cache.aggregate_atm_iv` for today, then compute
      the percentile.
    * On any error the read path's response is returned with the
      ``warning`` field set.
    """
    underlying = args.underlying
    bucket = args.expiry_bucket
    lookback = args.lookback_days

    cache = get_cache()
    warnings: list[str] = []
    refresh_summary: dict[str, Any] | None = None

    if cache is None:
        warnings.append(_WARN_CACHE_DISABLED)
        return _empty_response(underlying, bucket, lookback, warnings)

    if args.refresh:
        refresh_summary = await _refresh_iv_for_underlying(underlying, cache)

    rank = cache.get_iv_percentile_rank(underlying, bucket, lookback)
    sample_count = int(rank.get("sample_count") or 0)
    if sample_count < _MIN_SAMPLES_FOR_RANK:
        # Sparse-data guard: keep the numeric breakdown (min/max/median
        # are still useful) but mute the percentile rank itself so the
        # caller does not over-interpret ``50.0`` from a tiny sample.
        rank["percentile_rank"] = None
        warnings.append(_WARN_SAMPLE_SPARSE)

    rank["refresh"] = bool(args.refresh)
    rank["warning"] = warnings or None
    if refresh_summary is not None:
        rank["refresh_summary"] = refresh_summary
    return rank


def _empty_response(
    underlying: str,
    bucket: str,
    lookback: int,
    warnings: list[str],
) -> dict[str, Any]:
    return {
        "underlying": underlying,
        "expiry_bucket": bucket,
        "current_iv": None,
        "percentile_rank": None,
        "sample_count": 0,
        "lookback_days": lookback,
        "min_iv": None,
        "max_iv": None,
        "median_iv": None,
        "current_asof": None,
        "refresh": False,
        "warning": warnings or None,
    }


async def _refresh_iv_for_underlying(underlying: str, cache: Cache) -> dict[str, Any]:
    """Fetch a fresh chain and aggregate ATM IV for ``underlying``.

    Returns a small summary dict describing what was written so the
    caller can attach it to the response under ``refresh_summary``:

        {
          "rows_written": 412,        # snapshot rows inserted
          "atm_iv": {"30d": 0.32, "60d": 0.28, "90d": 0.27},
          "asof_date": "2026-05-25",
          "snapshot_at": "2026-05-25T14:30:00Z",
        }

    Errors are swallowed and reported via ``rows_written = 0`` —
    callers fall back to whatever cached history is available.
    """
    args = GetOptionChainInput(symbol=underlying)
    try:
        payload = await get_option_chain_impl(args)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning('{"event":"iv_refresh_chain_fetch_failed","error":"%s"}', exc)
        payload = {}

    rows_written = int(payload.get("_cached_rows") or 0) if isinstance(payload, dict) else 0
    snapshot_at = datetime.now(tz=UTC).replace(tzinfo=None)
    asof_date = snapshot_at.date()
    try:
        atm = cache.aggregate_atm_iv(underlying, asof_date, snapshot_at=snapshot_at)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning('{"event":"iv_refresh_aggregate_failed","error":"%s"}', exc)
        atm = {"30d": None, "60d": None, "90d": None}

    return {
        "rows_written": rows_written,
        "atm_iv": atm,
        "asof_date": asof_date.isoformat(),
        "snapshot_at": snapshot_at.isoformat() + "Z",
        "default_lookback_days": DEFAULT_IV_LOOKBACK_DAYS,
    }


# ---------------------------------------------------------------------------
# v0.6 T1/E1 — get_option_greeks_summary (16th MCP tool)
# ---------------------------------------------------------------------------


async def get_option_greeks_summary_impl(
    args: GetOptionGreeksSummaryInput,
) -> dict[str, Any]:
    """Aggregate per-contract Greeks from a live option chain.

    Works **without** ClickHouse: the chain is fetched via the cache-aware
    ``get_option_chain`` flow and the Greeks are computed in-process.  No
    durable history is required.

    The response shape::

        {
          "underlying": "AAPL",
          "expiry_filter": "2026-06-19" | None,
          "weighting": "open_interest" | "equal",
          "contract_count": 412,
          "net": {"delta": .., "gamma": .., "theta": .., "vega": .., "rho": ..},
          "by_side": {"CALL": {...net...}, "PUT": {...net...}},
          "by_expiry": {"2026-06-19": {...net...}, ...},
          "warning": [...] | None,
        }
    """
    underlying = args.underlying
    expiry_filter = args.expiry.date().isoformat() if args.expiry is not None else None

    chain_args = GetOptionChainInput(symbol=underlying)
    payload = await get_option_chain_impl(chain_args)

    contracts = flatten_option_chain_response(payload)
    # Restrict to the requested expiry when supplied.
    if expiry_filter is not None:
        contracts = [c for c in contracts if _contract_expiry_iso(c) == expiry_filter]

    warnings: list[str] = []
    fell_back = _aggregate_greeks(contracts, args.weighting)
    if fell_back.effective_weighting != args.weighting:
        warnings.append("open_interest_unavailable_equal_weighted")

    return {
        "underlying": underlying,
        "expiry_filter": expiry_filter,
        "weighting": fell_back.effective_weighting,
        "requested_weighting": args.weighting,
        "contract_count": len(contracts),
        "net": fell_back.net,
        "by_side": fell_back.by_side,
        "by_expiry": fell_back.by_expiry,
        "warning": warnings or None,
    }


def _contract_expiry_iso(contract: dict[str, Any]) -> str | None:
    expiry = contract.get("expiry")
    if isinstance(expiry, date):
        return expiry.isoformat()
    return None


@dataclass(frozen=True)
class _GreeksAggregate:
    net: dict[str, float | None]
    by_side: dict[str, dict[str, float | None]]
    by_expiry: dict[str, dict[str, float | None]]
    effective_weighting: str


def _aggregate_greeks(
    contracts: list[dict[str, Any]],
    weighting: str,
) -> _GreeksAggregate:
    """Fold a flattened contract list into net Greeks.

    ``open_interest`` weighting falls back to ``equal`` for the *whole*
    summary when no contract carries open interest, so the returned
    ``effective_weighting`` may differ from the requested one.
    """
    has_oi = any(_safe_positive_int(c.get("openInterest")) > 0 for c in contracts)
    effective = "equal" if (weighting == "open_interest" and not has_oi) else weighting

    net = _weighted_net(contracts, effective)
    by_side: dict[str, dict[str, float | None]] = {}
    for side in ("CALL", "PUT"):
        side_contracts = [c for c in contracts if str(c.get("call_put")) == side]
        by_side[side] = _weighted_net(side_contracts, effective)
    by_expiry: dict[str, dict[str, float | None]] = {}
    grouped: dict[str, list[dict[str, Any]]] = {}
    for c in contracts:
        exp = _contract_expiry_iso(c)
        if exp is None:
            continue
        grouped.setdefault(exp, []).append(c)
    by_expiry = {exp: _weighted_net(items, effective) for exp, items in sorted(grouped.items())}
    return _GreeksAggregate(
        net=net,
        by_side=by_side,
        by_expiry=by_expiry,
        effective_weighting=effective,
    )


def _weighted_net(
    contracts: list[dict[str, Any]],
    weighting: str,
) -> dict[str, float | None]:
    """Net (weighted) Greek per key across ``contracts``.

    A contract contributes a Greek only when that Greek parses to a
    finite float.  ``open_interest`` weighting uses each contract's open
    interest (contracts without OI contribute zero weight); ``equal``
    weighting uses a weight of 1 per contract that reports the Greek.
    Returns ``None`` for a Greek when no contract reported it.
    """
    out: dict[str, float | None] = {}
    for key in _GREEK_KEYS:
        weighted_sum = 0.0
        total_weight = 0.0
        seen = False
        for c in contracts:
            value = _safe_finite_float(c.get(key))
            if value is None:
                continue
            seen = True
            weight = float(_safe_positive_int(c.get("openInterest"))) if weighting == "open_interest" else 1.0
            weighted_sum += value * weight
            total_weight += weight
        if not seen:
            out[key] = None
        elif total_weight <= 0:
            # Greek seen but zero total weight (all OI zero in an
            # OI-weighted bucket) — degrade to a simple mean so the value
            # is still meaningful rather than a divide-by-zero.
            vals = [v for v in (_safe_finite_float(c.get(key)) for c in contracts) if v is not None]
            out[key] = round(sum(vals) / len(vals), 6) if vals else None
        else:
            out[key] = round(weighted_sum / total_weight, 6)
    return out


def _safe_positive_int(value: Any) -> int:
    try:
        ivalue = int(value)
    except (TypeError, ValueError):
        return 0
    return ivalue if ivalue > 0 else 0


def _safe_finite_float(value: Any) -> float | None:
    try:
        fvalue = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(fvalue):  # NaN / inf guard
        return None
    return fvalue


# ---------------------------------------------------------------------------
# v0.6 T1/E6 — get_iv_surface (17th MCP tool)
# ---------------------------------------------------------------------------

_WARN_REQUIRES_CH = "requires_clickhouse_persistence"


async def get_iv_surface_impl(args: GetIvSurfaceInput) -> dict[str, Any]:
    """Return the ATM IV term-structure surface across expiry buckets.

    Read-only: depends on durable ``iv_history`` rows that only the
    ClickHouse backend retains.  When the cache is disabled or the memory
    backend yields no history, the tool flags
    ``requires_clickhouse_persistence=True`` and returns empty buckets
    rather than raising.
    """
    underlying = args.underlying
    lookback = args.lookback_days

    cache = get_cache()
    if cache is None:
        return _empty_surface(underlying, lookback, [_WARN_CACHE_DISABLED, _WARN_REQUIRES_CH])

    surface = cache.get_iv_surface(underlying, lookback)
    warnings: list[str] = []
    if int(surface.get("total_sample_count") or 0) == 0:
        warnings.append(_WARN_REQUIRES_CH)
    surface["warning"] = warnings or None
    return surface


def _empty_surface(underlying: str, lookback: int, warnings: list[str]) -> dict[str, Any]:
    empty_bucket = {
        "underlying": underlying,
        "expiry_bucket": None,
        "current_iv": None,
        "percentile_rank": None,
        "sample_count": 0,
        "lookback_days": lookback,
        "min_iv": None,
        "max_iv": None,
        "median_iv": None,
        "current_asof": None,
    }
    return {
        "underlying": underlying,
        "lookback_days": lookback,
        "buckets": {
            "30d": {**empty_bucket, "expiry_bucket": "30d"},
            "60d": {**empty_bucket, "expiry_bucket": "60d"},
            "90d": {**empty_bucket, "expiry_bucket": "90d"},
        },
        "total_sample_count": 0,
        "warning": warnings or None,
    }


__all__ = [
    "get_iv_percentile_impl",
    "get_iv_surface_impl",
    "get_option_chain_impl",
    "get_option_expiration_chain_impl",
    "get_option_greeks_summary_impl",
]
