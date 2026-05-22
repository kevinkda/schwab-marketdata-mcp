"""``get_movers`` tool implementation."""

from __future__ import annotations

from typing import Any

from ..models import GetMoversInput
from . import _enums
from ._runtime import call_endpoint


async def get_movers_impl(args: GetMoversInput) -> dict[str, Any]:
    idx = _enums.movers_index(args.index)
    so = _enums.movers_sort_order(args.sort_order)
    fr = _enums.movers_frequency(args.frequency)

    async def fetch(client: Any) -> Any:
        return await client.get_movers(idx, sort_order=so, frequency=fr)

    return await call_endpoint("get_movers", fetch)


__all__ = ["get_movers_impl"]
