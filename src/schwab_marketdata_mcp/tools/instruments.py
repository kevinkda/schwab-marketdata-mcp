"""``search_instruments`` and ``get_instrument_by_cusip`` tool impls.

Plan §3.1 — these share a thin underlying dispatcher because Schwab's
``GET /instruments`` and ``GET /instruments/{cusip}`` are two different
HTTP endpoints, but both translate into a single response shape from the
agent's perspective.
"""

from __future__ import annotations

from typing import Any

from ..cache import Cache
from ..models import GetInstrumentByCusipInput, SearchInstrumentsInput
from . import _enums
from ._runtime import call_endpoint


async def search_instruments_impl(args: SearchInstrumentsInput) -> dict[str, Any]:
    proj = _enums.instrument_projection(args.projection)

    async def fetch(client: Any) -> Any:
        return await client.get_instruments(list(args.symbols), proj)

    cache_params = {"kind": "search", "symbols": list(args.symbols), "projection": str(args.projection)}

    def _lookup(cache: Cache) -> dict[str, Any] | None:
        return cache.get_instruments(cache_params)

    def _store(cache: Cache, raw: dict[str, Any]) -> None:
        cache.put_instruments(cache_params, raw)

    return await call_endpoint("search_instruments", fetch, cache_lookup=_lookup, cache_store=_store)


async def get_instrument_by_cusip_impl(
    args: GetInstrumentByCusipInput,
) -> dict[str, Any]:
    async def fetch(client: Any) -> Any:
        return await client.get_instrument_by_cusip(args.cusip)

    cache_params = {"kind": "cusip", "cusip": args.cusip}

    def _lookup(cache: Cache) -> dict[str, Any] | None:
        return cache.get_instruments(cache_params)

    def _store(cache: Cache, raw: dict[str, Any]) -> None:
        cache.put_instruments(cache_params, raw)

    return await call_endpoint(
        "get_instrument_by_cusip",
        fetch,
        cache_lookup=_lookup,
        cache_store=_store,
    )


__all__ = [
    "get_instrument_by_cusip_impl",
    "search_instruments_impl",
]
