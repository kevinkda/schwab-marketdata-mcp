"""``get_option_chain`` and ``get_option_expiration_chain`` tool impls."""

from __future__ import annotations

from typing import Any

from ..cache import Cache
from ..models import GetOptionChainInput, GetOptionExpirationChainInput
from . import _enums
from ._runtime import call_endpoint


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


__all__ = ["get_option_chain_impl", "get_option_expiration_chain_impl"]
