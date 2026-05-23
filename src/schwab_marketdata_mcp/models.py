"""Pydantic v2 input schemas for every outward-facing tool.

Plan §3.2 / §6.1 / §8.3 — symbol regexes are layered (stock / index / OSI
option / CUSIP), ``get_quotes`` symbol list is bounded at 50, and the
``pricehistory`` cartesian product is validated by a ``model_validator``
because the Schwab endpoint silently returns 400 on illegal combos.

Schwab-py enums are re-exported as the truth for tool ``Literal`` choices to
guarantee any future enum drift is a compile-time / runtime error here, not
silently swallowed by the upstream ``enforce_enums=True`` flag.

Coverage target: **100 %** (see ``CRITICAL_MODULES``).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from schwab.client import Client as _SchwabClient

# ---------------------------------------------------------------------------
# Symbol regex layering (Plan §8.3)
# ---------------------------------------------------------------------------

#: Stock / ETF / common share root, e.g. ``AAPL``, ``BRK.B``, ``BF/B``.
STOCK_SYMBOL_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Z][A-Z0-9.\-/]{0,9}$")

#: Index symbol prefixed with ``$``, e.g. ``$DJI``, ``$SPX``, ``$COMPX``.
INDEX_SYMBOL_RE: Final[re.Pattern[str]] = re.compile(r"^\$[A-Z]{3,5}$")

#: OSI 21-character option symbol, e.g. ``AAPL  240119C00170000``.
OSI_OPTION_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Z]{1,6}\s{0,5}\d{6}[CP]\d{8}$")

#: CUSIP (9-character alphanumeric).
CUSIP_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Z0-9]{9}$")

# Combined "any tradable" pattern used by tools that accept either an
# equity / ETF, an index, or an OSI option contract.  The explicit ``^…$``
# anchors are required because Pydantic v2's ``StringConstraints.pattern``
# semantics are search-style; without them ``"AAPL!!"`` would pass on the
# ``AAPL`` prefix.
ANY_SYMBOL_RE: Final[re.Pattern[str]] = re.compile(
    "^(?:"
    + "|".join(f"(?:{p.pattern.lstrip('^').rstrip('$')})" for p in (STOCK_SYMBOL_RE, INDEX_SYMBOL_RE, OSI_OPTION_RE))
    + ")$"
)

# Hard cap from plan §3.1 — guards against runaway symbol lists blowing the
# stdio JSON-RPC frame budget.
MAX_SYMBOLS_PER_BATCH: Final[int] = 50

# ---------------------------------------------------------------------------
# Streaming snapshot constants (Plan §10 follow-up — bounded streaming model).
# ---------------------------------------------------------------------------

#: Cap on the symbol list for ``get_streaming_snapshot``.  Smaller than the
#: REST batch cap because each symbol expands into many websocket messages
#: and we want a tight upper-bound on connection-lifetime memory.
MAX_STREAMING_SNAPSHOT_SYMBOLS: Final[int] = 20

#: Lower bound for the bounded-snapshot duration.  Below 500 ms the round
#: trip for login / subscribe / first message will rarely complete.
MIN_STREAMING_SNAPSHOT_DURATION_MS: Final[int] = 500

#: Upper bound — keeps the tool call inside MCP request-response semantics
#: (most clients have a 30-second tool timeout; we leave generous headroom).
MAX_STREAMING_SNAPSHOT_DURATION_MS: Final[int] = 10_000

#: Default duration when the caller omits ``duration_ms``.
DEFAULT_STREAMING_SNAPSHOT_DURATION_MS: Final[int] = 2_000

StreamingService = Literal["LEVELONE_EQUITIES", "CHART_EQUITY"]


# ---------------------------------------------------------------------------
# Re-exported schwab-py enums (Plan §3.2 / §3.1)
# ---------------------------------------------------------------------------

QuoteFields = Literal["QUOTE", "FUNDAMENTAL", "EXTENDED", "REFERENCE", "REGULAR"]
MoversIndex = Literal[
    "DJI",
    "COMPX",
    "SPX",
    "NYSE",
    "NASDAQ",
    "OTCBB",
    "INDEX_ALL",
    "EQUITY_ALL",
    "OPTION_ALL",
    "OPTION_PUT",
    "OPTION_CALL",
]
MoversSortOrder = Literal["VOLUME", "TRADES", "PERCENT_CHANGE_UP", "PERCENT_CHANGE_DOWN"]
MoversFrequency = Literal["ZERO", "ONE", "FIVE", "TEN", "THIRTY", "SIXTY"]

PeriodType = Literal["DAY", "MONTH", "YEAR", "YEAR_TO_DATE"]
Period = Literal[
    "ONE_DAY",
    "TWO_DAYS",
    "THREE_DAYS",
    "FOUR_DAYS",
    "FIVE_DAYS",
    "TEN_DAYS",
    "SIX_MONTHS",
    "FIFTEEN_YEARS",
    "TWENTY_YEARS",
]
FrequencyType = Literal["MINUTE", "DAILY", "WEEKLY", "MONTHLY"]
Frequency = Literal[
    "EVERY_MINUTE",
    "EVERY_FIVE_MINUTES",
    "EVERY_TEN_MINUTES",
    "EVERY_FIFTEEN_MINUTES",
    "EVERY_THIRTY_MINUTES",
]

OptionsContractType = Literal["CALL", "PUT", "ALL"]
OptionsStrategy = Literal[
    "SINGLE",
    "ANALYTICAL",
    "COVERED",
    "VERTICAL",
    "CALENDAR",
    "STRANGLE",
    "STRADDLE",
    "BUTTERFLY",
    "CONDOR",
    "DIAGONAL",
    "COLLAR",
    "ROLL",
]
OptionsStrikeRange = Literal[
    "IN_THE_MONEY",
    "NEAR_THE_MONEY",
    "OUT_OF_THE_MONEY",
    "STRIKES_ABOVE_MARKET",
    "STRIKES_BELOW_MARKET",
    "STRIKES_NEAR_MARKET",
    "ALL",
]
OptionsType = Literal["STANDARD", "NON_STANDARD", "ALL"]
OptionsExpirationMonth = Literal[
    "JANUARY",
    "FEBRUARY",
    "MARCH",
    "APRIL",
    "MAY",
    "JUNE",
    "JULY",
    "AUGUST",
    "SEPTEMBER",
    "OCTOBER",
    "NOVEMBER",
    "DECEMBER",
    "ALL",
]
OptionsEntitlement = Literal["PAYING_PRO", "NON_PRO", "NON_PAYING_PRO"]

MarketHoursMarket = Literal["EQUITY", "OPTION", "BOND", "FUTURE", "FOREX"]

InstrumentProjection = Literal[
    "SYMBOL_SEARCH",
    "SYMBOL_REGEX",
    "DESCRIPTION_SEARCH",
    "DESCRIPTION_REGEX",
    "SEARCH",
    "FUNDAMENTAL",
]


# Internal sanity check: keep our local Literals aligned with schwab-py.  If
# this assertion fails after a schwab-py upgrade, the THREAT_MODEL.md
# upgrade-checklist points the reviewer here.
def _assert_enum_alignment() -> None:
    pairs = [
        (QuoteFields, _SchwabClient.Quote.Fields),
        (MoversIndex, _SchwabClient.Movers.Index),
        (MoversSortOrder, _SchwabClient.Movers.SortOrder),
        (MoversFrequency, _SchwabClient.Movers.Frequency),
        (PeriodType, _SchwabClient.PriceHistory.PeriodType),
        (Period, _SchwabClient.PriceHistory.Period),
        (FrequencyType, _SchwabClient.PriceHistory.FrequencyType),
        (Frequency, _SchwabClient.PriceHistory.Frequency),
        (OptionsContractType, _SchwabClient.Options.ContractType),
        (OptionsStrategy, _SchwabClient.Options.Strategy),
        (OptionsStrikeRange, _SchwabClient.Options.StrikeRange),
        (OptionsType, _SchwabClient.Options.Type),
        (OptionsExpirationMonth, _SchwabClient.Options.ExpirationMonth),
        (OptionsEntitlement, _SchwabClient.Options.Entitlement),
        (MarketHoursMarket, _SchwabClient.MarketHours.Market),
        (InstrumentProjection, _SchwabClient.Instrument.Projection),
    ]
    from typing import get_args

    for literal, enum_cls in pairs:
        local = set(get_args(literal))
        # Compare against enum *names* — the wire-format value translation is
        # done by ``client.py`` when invoking schwab-py methods (it accepts
        # enum members directly so we just look up Enum[name]).  Using names
        # keeps our public Literal surface stable across schwab-py value
        # changes (e.g. ``"day"`` → ``"DAY"``) and matches what an LLM agent
        # naturally produces.
        upstream = {e.name for e in enum_cls}
        if local != upstream:  # pragma: no cover - drift is upgrade-time only
            raise RuntimeError(
                f"Enum drift between schwab-py {enum_cls.__qualname__} and "
                f"models.py Literal {literal!r}: "
                f"missing_local={upstream - local}, extra_local={local - upstream}"
            )


_assert_enum_alignment()


# ---------------------------------------------------------------------------
# Reusable string types
# ---------------------------------------------------------------------------

StockSymbol = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=10,
        pattern=STOCK_SYMBOL_RE.pattern,
        strip_whitespace=False,
    ),
]
IndexSymbol = Annotated[
    str,
    StringConstraints(min_length=4, max_length=6, pattern=INDEX_SYMBOL_RE.pattern),
]
OsiOptionSymbol = Annotated[
    str,
    StringConstraints(min_length=15, max_length=21, pattern=OSI_OPTION_RE.pattern),
]
AnyTradableSymbol = Annotated[
    str,
    StringConstraints(min_length=1, max_length=21, pattern=ANY_SYMBOL_RE.pattern),
]
Cusip = Annotated[
    str,
    StringConstraints(min_length=9, max_length=9, pattern=CUSIP_RE.pattern),
]


class _BaseInput(BaseModel):
    """Base model — strict by default; extra=forbid to catch typos."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=False,
        frozen=True,
        validate_default=True,
        use_enum_values=True,
    )


# ---------------------------------------------------------------------------
# Tool input schemas
# ---------------------------------------------------------------------------


class GetQuoteInput(_BaseInput):
    symbol: AnyTradableSymbol
    fields: list[QuoteFields] | None = None


class GetQuotesInput(_BaseInput):
    symbols: list[AnyTradableSymbol] = Field(min_length=1)
    fields: list[QuoteFields] | None = None
    indicative: bool | None = None

    @model_validator(mode="after")
    def _enforce_batch_cap(self) -> GetQuotesInput:
        if len(self.symbols) > MAX_SYMBOLS_PER_BATCH:
            from .errors import SchwabValidationError

            raise SchwabValidationError(
                field="symbols",
                reason=(f"too many symbols: {len(self.symbols)} > {MAX_SYMBOLS_PER_BATCH}; split the request"),
            )
        return self


class GetPriceHistoryInput(_BaseInput):
    """Cartesian-product validation per Schwab docs.

    The product of (period_type, period, frequency_type, frequency) is *not*
    free-form — illegal combinations return HTTP 400 from Schwab.  We
    short-circuit those in :py:meth:`_check_combo`.
    """

    symbol: AnyTradableSymbol
    period_type: PeriodType | None = None
    period: Period | None = None
    frequency_type: FrequencyType | None = None
    frequency: Frequency | None = None
    start_datetime: datetime | None = None
    end_datetime: datetime | None = None
    need_extended_hours_data: bool | None = None
    need_previous_close: bool | None = None

    # Legal subsets distilled from Schwab Market Data v1 API docs.
    _LEGAL: Final[dict[str, dict[str, set[str]]]] = {
        "DAY": {
            "period": {"ONE_DAY", "TWO_DAYS", "THREE_DAYS", "FOUR_DAYS", "FIVE_DAYS", "TEN_DAYS"},
            "frequency_type": {"MINUTE"},
            "frequency": {
                "EVERY_MINUTE",
                "EVERY_FIVE_MINUTES",
                "EVERY_TEN_MINUTES",
                "EVERY_FIFTEEN_MINUTES",
                "EVERY_THIRTY_MINUTES",
            },
        },
        "MONTH": {
            "period": {"ONE_DAY", "TWO_DAYS", "THREE_DAYS", "SIX_MONTHS"},
            "frequency_type": {"DAILY", "WEEKLY"},
            "frequency": set(),
        },
        "YEAR": {
            "period": {"FIFTEEN_YEARS", "TWENTY_YEARS"},
            "frequency_type": {"DAILY", "WEEKLY", "MONTHLY"},
            "frequency": set(),
        },
        "YEAR_TO_DATE": {
            "period": set(),
            "frequency_type": {"DAILY", "WEEKLY"},
            "frequency": set(),
        },
    }

    @model_validator(mode="after")
    def _check_combo(self) -> GetPriceHistoryInput:
        from .errors import SchwabValidationError

        if self.period_type is not None:
            legal = self._LEGAL.get(str(self.period_type))
            if legal is None:  # pragma: no cover - defended by Pydantic Literal
                raise SchwabValidationError(
                    field="period_type",
                    reason=f"unknown period_type {self.period_type!r}",
                )
            if self.period is not None and legal["period"] and str(self.period) not in legal["period"]:
                raise SchwabValidationError(
                    field="period",
                    reason=(
                        f"period={self.period!r} illegal for period_type="
                        f"{self.period_type!r}; allowed={sorted(legal['period'])}"
                    ),
                )
            if self.frequency_type is not None and str(self.frequency_type) not in legal["frequency_type"]:
                raise SchwabValidationError(
                    field="frequency_type",
                    reason=(
                        f"frequency_type={self.frequency_type!r} illegal for "
                        f"period_type={self.period_type!r}; "
                        f"allowed={sorted(legal['frequency_type'])}"
                    ),
                )
            if (
                self.frequency is not None and legal["frequency"] and str(self.frequency) not in legal["frequency"]
            ):  # pragma: no cover - all DAY freqs are valid; unreachable subset
                raise SchwabValidationError(
                    field="frequency",
                    reason=(
                        f"frequency={self.frequency!r} illegal for period_type="
                        f"{self.period_type!r}; allowed={sorted(legal['frequency'])}"
                    ),
                )
        if (
            self.start_datetime is not None
            and self.end_datetime is not None
            and self.end_datetime < self.start_datetime
        ):
            raise SchwabValidationError(
                field="end_datetime",
                reason="end_datetime must be >= start_datetime",
            )
        # Reject naive datetimes — only TZ-aware allowed (Schwab needs ms epoch).
        for fld in ("start_datetime", "end_datetime"):
            val: datetime | None = getattr(self, fld)
            if val is not None and (val.tzinfo is None or val.utcoffset() is None):
                raise SchwabValidationError(
                    field=fld,
                    reason="datetime must be timezone-aware",
                )
            if val is not None and val > datetime.now(tz=UTC):
                raise SchwabValidationError(
                    field=fld,
                    reason="datetime cannot be in the future",
                )
        return self


class GetOptionChainInput(_BaseInput):
    symbol: StockSymbol
    contract_type: OptionsContractType | None = None
    strike_count: int | None = Field(default=None, ge=1, le=500)
    include_underlying_quote: bool | None = None
    strategy: OptionsStrategy | None = None
    interval: float | None = Field(default=None, gt=0)
    strike: float | None = Field(default=None, gt=0)
    strike_range: OptionsStrikeRange | None = None
    from_date: datetime | None = None
    to_date: datetime | None = None
    volatility: float | None = Field(default=None, ge=0)
    underlying_price: float | None = Field(default=None, gt=0)
    interest_rate: float | None = None
    days_to_expiration: int | None = Field(default=None, ge=0, le=3650)
    exp_month: OptionsExpirationMonth | None = None
    option_type: OptionsType | None = None
    entitlement: OptionsEntitlement | None = None

    @model_validator(mode="after")
    def _check_dates(self) -> GetOptionChainInput:
        from .errors import SchwabValidationError

        if self.from_date is not None and self.to_date is not None and self.to_date < self.from_date:
            raise SchwabValidationError(
                field="to_date",
                reason="to_date must be >= from_date",
            )
        return self


class GetOptionExpirationChainInput(_BaseInput):
    symbol: StockSymbol


class GetMarketHoursInput(_BaseInput):
    markets: list[MarketHoursMarket] = Field(min_length=1, max_length=5)
    date: datetime | None = None


class GetMarketHourSingleInput(_BaseInput):
    market_id: MarketHoursMarket
    date: datetime | None = None


class GetMoversInput(_BaseInput):
    index: MoversIndex
    sort_order: MoversSortOrder | None = None
    frequency: MoversFrequency | None = None


class SearchInstrumentsInput(_BaseInput):
    symbols: list[StockSymbol] = Field(min_length=1, max_length=MAX_SYMBOLS_PER_BATCH)
    projection: InstrumentProjection


class GetInstrumentByCusipInput(_BaseInput):
    cusip: Cusip


class HealthCheckInput(_BaseInput):
    """No input — kept for schema parity in `list_tools`."""


class GetServerInfoInput(_BaseInput):
    """No input — kept for schema parity in `list_tools`."""


class GetCacheStatsInput(_BaseInput):
    """No input — local-only meta query for the DuckDB cache."""


class GetStreamingSnapshotInput(_BaseInput):
    """Bounded WebSocket snapshot via ``StreamerClient`` (plan §10 follow-up).

    Opens a Schwab Streamer connection, collects messages for
    ``duration_ms`` (default 2 s, hard-bounded 500 ms - 10 s), then closes.
    Deliberately fits the request-response semantics of MCP rather than
    holding a long-running subscription open across tool calls — the
    long-running model is reserved for a dedicated streaming MCP server
    in v0.3+.
    """

    symbols: list[StockSymbol] = Field(
        min_length=1,
        max_length=MAX_STREAMING_SNAPSHOT_SYMBOLS,
    )
    service: StreamingService
    duration_ms: int | None = None

    @model_validator(mode="after")
    def _check_duration(self) -> GetStreamingSnapshotInput:
        from .errors import SchwabValidationError

        # symbol regex is already enforced by ``StockSymbol``; we only need
        # to police the duration window because Pydantic's ``Field`` does
        # not gracefully express "either None or a bounded int".
        d = self.duration_ms if self.duration_ms is not None else DEFAULT_STREAMING_SNAPSHOT_DURATION_MS
        if not (MIN_STREAMING_SNAPSHOT_DURATION_MS <= d <= MAX_STREAMING_SNAPSHOT_DURATION_MS):
            raise SchwabValidationError(
                field="duration_ms",
                reason=(
                    f"duration_ms must be in "
                    f"[{MIN_STREAMING_SNAPSHOT_DURATION_MS}, "
                    f"{MAX_STREAMING_SNAPSHOT_DURATION_MS}]; got {d}"
                ),
            )
        return self


# ---------------------------------------------------------------------------
# Discriminator-free public API
# ---------------------------------------------------------------------------

#: Mapping from tool name → input model class.  Used by both the server's
#: ``list_tools`` reflection and the unit tests.
TOOL_INPUT_MODELS: Final[dict[str, type[_BaseInput]]] = {
    "get_quote": GetQuoteInput,
    "get_quotes": GetQuotesInput,
    "get_price_history": GetPriceHistoryInput,
    "get_option_chain": GetOptionChainInput,
    "get_option_expiration_chain": GetOptionExpirationChainInput,
    "get_market_hours": GetMarketHoursInput,
    "get_market_hour_single": GetMarketHourSingleInput,
    "get_movers": GetMoversInput,
    "search_instruments": SearchInstrumentsInput,
    "get_instrument_by_cusip": GetInstrumentByCusipInput,
    "health_check": HealthCheckInput,
    "get_server_info": GetServerInfoInput,
    "get_cache_stats": GetCacheStatsInput,
    "get_streaming_snapshot": GetStreamingSnapshotInput,
}


def supported_tool_names() -> list[str]:
    """Return all 14 tool names in deterministic order."""
    return list(TOOL_INPUT_MODELS.keys())


def validate_tool_input(tool_name: str, raw: dict[str, Any]) -> _BaseInput:
    """Validate ``raw`` payload for ``tool_name`` or raise SchwabValidationError."""
    from pydantic import ValidationError

    from .errors import SchwabValidationError

    try:
        model_cls = TOOL_INPUT_MODELS[tool_name]
    except KeyError as exc:
        raise SchwabValidationError(field="tool_name", reason=f"unknown tool {tool_name!r}") from exc
    try:
        return model_cls.model_validate(raw)
    except SchwabValidationError:
        raise
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first["loc"]) or "(root)"
        raise SchwabValidationError(field=loc, reason=first["msg"]) from exc


__all__ = [
    "ANY_SYMBOL_RE",
    "CUSIP_RE",
    "DEFAULT_STREAMING_SNAPSHOT_DURATION_MS",
    "INDEX_SYMBOL_RE",
    "MAX_STREAMING_SNAPSHOT_DURATION_MS",
    "MAX_STREAMING_SNAPSHOT_SYMBOLS",
    "MAX_SYMBOLS_PER_BATCH",
    "MIN_STREAMING_SNAPSHOT_DURATION_MS",
    "OSI_OPTION_RE",
    "STOCK_SYMBOL_RE",
    "TOOL_INPUT_MODELS",
    "AnyTradableSymbol",
    "Cusip",
    "Frequency",
    "FrequencyType",
    "GetCacheStatsInput",
    "GetInstrumentByCusipInput",
    "GetMarketHourSingleInput",
    "GetMarketHoursInput",
    "GetMoversInput",
    "GetOptionChainInput",
    "GetOptionExpirationChainInput",
    "GetPriceHistoryInput",
    "GetQuoteInput",
    "GetQuotesInput",
    "GetServerInfoInput",
    "GetStreamingSnapshotInput",
    "HealthCheckInput",
    "IndexSymbol",
    "InstrumentProjection",
    "MarketHoursMarket",
    "MoversFrequency",
    "MoversIndex",
    "MoversSortOrder",
    "OptionsContractType",
    "OptionsEntitlement",
    "OptionsExpirationMonth",
    "OptionsStrategy",
    "OptionsStrikeRange",
    "OptionsType",
    "OsiOptionSymbol",
    "Period",
    "PeriodType",
    "QuoteFields",
    "SearchInstrumentsInput",
    "StockSymbol",
    "StreamingService",
    "supported_tool_names",
    "validate_tool_input",
]
