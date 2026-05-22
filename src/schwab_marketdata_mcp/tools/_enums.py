"""Translate string Literals (used in our public tool API) → schwab-py Enum members.

Plan §3.2 — schwab-py with ``enforce_enums=True`` requires actual
``Enum`` instances, not strings.  Our public Pydantic input models use the
enum **name** as a string Literal (so an LLM agent can produce
``"VOLUME"`` rather than having to know about the schwab-py enum module).
This module bridges between the two.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, TypeVar

from schwab.client import Client as SchwabClient

E = TypeVar("E", bound=Enum)


def _by_name(enum_cls: type[E], name: str | None) -> E | None:
    if name is None:
        return None
    try:
        return enum_cls[name]
    except KeyError as exc:
        from ..errors import SchwabValidationError

        raise SchwabValidationError(
            field=enum_cls.__qualname__.split(".")[-1].lower(),
            reason=f"unknown enum name {name!r}; valid={[m.name for m in enum_cls]}",
        ) from exc


def quote_field(name: str | None) -> SchwabClient.Quote.Fields | None:
    return _by_name(SchwabClient.Quote.Fields, name)


def quote_fields(names: list[Any] | None) -> list[SchwabClient.Quote.Fields] | None:
    if names is None:
        return None
    return [SchwabClient.Quote.Fields[str(n)] for n in names]


def movers_index(name: str) -> SchwabClient.Movers.Index:
    return SchwabClient.Movers.Index[name]


def movers_sort_order(name: str | None) -> SchwabClient.Movers.SortOrder | None:
    return _by_name(SchwabClient.Movers.SortOrder, name)


def movers_frequency(name: str | None) -> SchwabClient.Movers.Frequency | None:
    return _by_name(SchwabClient.Movers.Frequency, name)


def period_type(name: str | None) -> SchwabClient.PriceHistory.PeriodType | None:
    return _by_name(SchwabClient.PriceHistory.PeriodType, name)


def period(name: str | None) -> SchwabClient.PriceHistory.Period | None:
    return _by_name(SchwabClient.PriceHistory.Period, name)


def frequency_type(name: str | None) -> SchwabClient.PriceHistory.FrequencyType | None:
    return _by_name(SchwabClient.PriceHistory.FrequencyType, name)


def frequency(name: str | None) -> SchwabClient.PriceHistory.Frequency | None:
    return _by_name(SchwabClient.PriceHistory.Frequency, name)


def options_contract_type(name: str | None) -> SchwabClient.Options.ContractType | None:
    return _by_name(SchwabClient.Options.ContractType, name)


def options_strategy(name: str | None) -> SchwabClient.Options.Strategy | None:
    return _by_name(SchwabClient.Options.Strategy, name)


def options_strike_range(name: str | None) -> SchwabClient.Options.StrikeRange | None:
    return _by_name(SchwabClient.Options.StrikeRange, name)


def options_type(name: str | None) -> SchwabClient.Options.Type | None:
    return _by_name(SchwabClient.Options.Type, name)


def options_exp_month(name: str | None) -> SchwabClient.Options.ExpirationMonth | None:
    return _by_name(SchwabClient.Options.ExpirationMonth, name)


def options_entitlement(name: str | None) -> SchwabClient.Options.Entitlement | None:
    return _by_name(SchwabClient.Options.Entitlement, name)


def market_hours_market(name: str) -> SchwabClient.MarketHours.Market:
    return SchwabClient.MarketHours.Market[name]


def market_hours_markets(names: list[str]) -> list[SchwabClient.MarketHours.Market]:
    return [SchwabClient.MarketHours.Market[n] for n in names]


def instrument_projection(name: str) -> SchwabClient.Instrument.Projection:
    return SchwabClient.Instrument.Projection[name]


__all__ = [
    "frequency",
    "frequency_type",
    "instrument_projection",
    "market_hours_market",
    "market_hours_markets",
    "movers_frequency",
    "movers_index",
    "movers_sort_order",
    "options_contract_type",
    "options_entitlement",
    "options_exp_month",
    "options_strategy",
    "options_strike_range",
    "options_type",
    "period",
    "period_type",
    "quote_field",
    "quote_fields",
]
