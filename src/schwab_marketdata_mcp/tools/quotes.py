"""``get_quote`` and ``get_quotes`` tool implementations."""

from __future__ import annotations

from typing import Any

from ..cache import Cache
from ..models import GetQuoteInput, GetQuotesInput
from . import _enums
from ._runtime import call_endpoint


async def get_quote_impl(args: GetQuoteInput) -> dict[str, Any]:
    fields = _enums.quote_fields(args.fields)

    async def fetch(client: Any) -> Any:
        return await client.get_quote(args.symbol, fields=fields)

    def _lookup(cache: Cache) -> dict[str, Any] | None:
        return cache.get_quote(args.symbol)

    def _store(cache: Cache, raw: dict[str, Any]) -> None:
        cache.put_quote(args.symbol, raw)

    return await call_endpoint("get_quote", fetch, cache_lookup=_lookup, cache_store=_store)


async def get_quotes_impl(args: GetQuotesInput) -> dict[str, Any]:
    fields = _enums.quote_fields(args.fields)

    async def fetch(client: Any) -> Any:
        return await client.get_quotes(
            list(args.symbols),
            fields=fields,
            indicative=args.indicative,
        )

    return await call_endpoint("get_quotes", fetch)


__all__ = ["get_quote_impl", "get_quotes_impl"]
