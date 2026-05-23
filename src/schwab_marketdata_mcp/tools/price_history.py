"""``get_price_history`` tool implementation."""

from __future__ import annotations

from typing import Any

from ..cache import Cache
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

    cache_params: dict[str, Any] = {
        "symbol": args.symbol,
        "period_type": str(args.period_type) if args.period_type is not None else None,
        "frequency_type": str(args.frequency_type) if args.frequency_type is not None else None,
        "frequency": _frequency_to_int(args.frequency),
        "start_datetime": args.start_datetime,
        "end_datetime": args.end_datetime,
    }

    def _lookup(cache: Cache) -> dict[str, Any] | None:
        return cache.get_price_history(cache_params)

    def _store(cache: Cache, raw: dict[str, Any]) -> None:
        cache.put_price_history(cache_params, raw)

    return await call_endpoint("get_price_history", fetch, cache_lookup=_lookup, cache_store=_store)


def _frequency_to_int(freq: Any) -> int | None:
    """Map the schwab-py-style frequency literal/enum to its minute count."""
    if freq is None:
        return None
    name = str(freq)
    mapping = {
        "EVERY_MINUTE": 1,
        "EVERY_FIVE_MINUTES": 5,
        "EVERY_TEN_MINUTES": 10,
        "EVERY_FIFTEEN_MINUTES": 15,
        "EVERY_THIRTY_MINUTES": 30,
    }
    return mapping.get(name)


__all__ = ["get_price_history_impl"]
