"""``get_quote`` and ``get_quotes`` tool implementations."""

from __future__ import annotations

from typing import Any

from ..models import GetQuoteInput, GetQuotesInput
from . import _enums
from ._runtime import call_endpoint


async def get_quote_impl(args: GetQuoteInput) -> dict[str, Any]:
    fields = _enums.quote_fields(args.fields)

    async def fetch(client: Any) -> Any:
        return await client.get_quote(args.symbol, fields=fields)

    return await call_endpoint("get_quote", fetch)


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
