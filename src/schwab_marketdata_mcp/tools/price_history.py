"""``get_price_history`` tool implementation."""

from __future__ import annotations

from typing import Any

from ..models import GetPriceHistoryInput
from . import _enums
from ._runtime import call_endpoint


async def get_price_history_impl(args: GetPriceHistoryInput) -> dict[str, Any]:
    pt = _enums.period_type(args.period_type)
    p = _enums.period(args.period)
    ft = _enums.frequency_type(args.frequency_type)
    f = _enums.frequency(args.frequency)

    async def fetch(client: Any) -> Any:
        return await client.get_price_history(
            args.symbol,
            period_type=pt,
            period=p,
            frequency_type=ft,
            frequency=f,
            start_datetime=args.start_datetime,
            end_datetime=args.end_datetime,
            need_extended_hours_data=args.need_extended_hours_data,
            need_previous_close=args.need_previous_close,
        )

    return await call_endpoint("get_price_history", fetch)


__all__ = ["get_price_history_impl"]
