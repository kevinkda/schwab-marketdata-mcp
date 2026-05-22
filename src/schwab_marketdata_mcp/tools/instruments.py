"""``search_instruments`` and ``get_instrument_by_cusip`` tool impls.

Plan §3.1 — these share a thin underlying dispatcher because Schwab's
``GET /instruments`` and ``GET /instruments/{cusip}`` are two different
HTTP endpoints, but both translate into a single response shape from the
agent's perspective.
"""

from __future__ import annotations

from typing import Any

from ..models import GetInstrumentByCusipInput, SearchInstrumentsInput
from . import _enums
from ._runtime import call_endpoint


async def _instruments_impl(
    *,
    tool_name: str,
    fetch: Any,
) -> dict[str, Any]:
    return await call_endpoint(tool_name, fetch)


async def search_instruments_impl(args: SearchInstrumentsInput) -> dict[str, Any]:
    proj = _enums.instrument_projection(args.projection)

    async def fetch(client: Any) -> Any:
        return await client.get_instruments(list(args.symbols), proj)

    return await _instruments_impl(tool_name="search_instruments", fetch=fetch)


async def get_instrument_by_cusip_impl(
    args: GetInstrumentByCusipInput,
) -> dict[str, Any]:
    async def fetch(client: Any) -> Any:
        return await client.get_instrument_by_cusip(args.cusip)

    return await _instruments_impl(tool_name="get_instrument_by_cusip", fetch=fetch)


__all__ = [
    "get_instrument_by_cusip_impl",
    "search_instruments_impl",
]
