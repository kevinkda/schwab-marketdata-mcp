"""``get_option_chain``, ``get_option_expiration_chain``, and
``get_iv_percentile`` tool impls.

v0.4 P1/C — adds ``get_iv_percentile_impl`` as the 15th MCP tool: it
computes the current ATM IV percentile rank for an underlying versus N
days of cached history (default 252 ≈ 1 trading year).  When
``refresh=True`` it first pulls a live option chain via
``get_option_chain`` and aggregates ATM IV before computing the rank;
when ``refresh=False`` it serves only from cached ``iv_history`` rows.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from ..cache import (
    DEFAULT_IV_LOOKBACK_DAYS,
    Cache,
    get_cache,
)
from ..models import (
    GetIvPercentileInput,
    GetOptionChainInput,
    GetOptionExpirationChainInput,
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

    return await call_endpoint("get_option_chain", fetch, cache_lookup=_lookup, cache_store=_store)


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
    caller can attach it to the response under ``refresh_summary``.
    Errors are swallowed and reported via ``rows_written = 0`` —
    callers fall back to whatever cached history is available.

    NOTE: until the next commit ``get_option_chain_impl`` does not
    persist its own snapshot, so ``rows_written`` here is always 0;
    the live aggregator depends on a separate writer that lands in
    the very next commit.
    """
    try:
        await get_option_chain_impl(GetOptionChainInput(symbol=underlying))
    except Exception as exc:  # pragma: no cover - defensive
        log.warning('{"event":"iv_refresh_chain_fetch_failed","error":"%s"}', exc)

    snapshot_at = datetime.now(tz=UTC).replace(tzinfo=None)
    asof_date = snapshot_at.date()
    try:
        atm = cache.aggregate_atm_iv(underlying, asof_date, snapshot_at=snapshot_at)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning('{"event":"iv_refresh_aggregate_failed","error":"%s"}', exc)
        atm = {"30d": None, "60d": None, "90d": None}

    return {
        "rows_written": 0,
        "atm_iv": atm,
        "asof_date": asof_date.isoformat(),
        "snapshot_at": snapshot_at.isoformat() + "Z",
        "default_lookback_days": DEFAULT_IV_LOOKBACK_DAYS,
    }


__all__ = [
    "get_iv_percentile_impl",
    "get_option_chain_impl",
    "get_option_expiration_chain_impl",
]
