"""``get_market_hours`` and ``get_market_hour_single`` tool impls.

Note: Schwab Market Data v1 only exposes ``GET /markets`` which accepts a
list.  The "single" tool is a convenience wrapper that requests a 1-element
list and unpacks the response.  Plan §3.1.
"""

from __future__ import annotations

from typing import Any

from ..models import GetMarketHourSingleInput, GetMarketHoursInput
from . import _enums
from ._runtime import call_endpoint


async def get_market_hours_impl(args: GetMarketHoursInput) -> dict[str, Any]:
    enum_markets = _enums.market_hours_markets(list(args.markets))

    async def fetch(client: Any) -> Any:
        return await client.get_market_hours(enum_markets, date=args.date)

    return await call_endpoint("get_market_hours", fetch)


async def get_market_hour_single_impl(args: GetMarketHourSingleInput) -> dict[str, Any]:
    enum_markets = [_enums.market_hours_market(args.market_id)]

    async def fetch(client: Any) -> Any:
        return await client.get_market_hours(enum_markets, date=args.date)

    return await call_endpoint("get_market_hour_single", fetch)


__all__ = ["get_market_hour_single_impl", "get_market_hours_impl"]
