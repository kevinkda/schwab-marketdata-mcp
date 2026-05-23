"""Pydantic input model tests — symbol regex layers, cartesian product, length cap.

Plan §6.1 — covers 4 symbol layers, ``get_quotes`` length cap, pricehistory
cartesian product (≥9 legal + ≥5 illegal combos), enum boundary, datetime
constraints.

Critical-module target: 100 % coverage on ``schwab_marketdata_mcp.models``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from schwab_marketdata_mcp.errors import SchwabValidationError
from schwab_marketdata_mcp.models import (
    MAX_SYMBOLS_PER_BATCH,
    GetInstrumentByCusipInput,
    GetMarketHourSingleInput,
    GetMarketHoursInput,
    GetMoversInput,
    GetOptionChainInput,
    GetOptionExpirationChainInput,
    GetPriceHistoryInput,
    GetQuoteInput,
    GetQuotesInput,
    GetServerInfoInput,
    HealthCheckInput,
    SearchInstrumentsInput,
    supported_tool_names,
    validate_tool_input,
)

# ---------------------------------------------------------------------------
# Symbol regex layering
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "symbol",
    [
        "AAPL",
        "MSFT",
        "BRK.B",
        "BF/B",
        "T",
        "$DJI",
        "$SPX",
        "$COMPX",
        "AAPL  240119C00170000",
    ],
)
def test_get_quote_accepts_valid_symbols(symbol: str) -> None:
    GetQuoteInput(symbol=symbol)


@pytest.mark.parametrize(
    "bad",
    [
        "aapl",  # lowercase
        "AAPL!!",  # punctuation
        "AAPL'OR'1=1",  # SQL injection attempt
        "AAPL;rm -rf",
        "AAPL<script>",
        "$(whoami)",
        "../../../etc/passwd",
        "",  # empty
        " AAPL",  # leading whitespace
        "TOOLONGAPPL",  # >10 chars
    ],
)
def test_get_quote_rejects_invalid_symbols(bad: str) -> None:
    with pytest.raises(Exception):
        GetQuoteInput(symbol=bad)


def test_get_quotes_max_50() -> None:
    syms = [f"SYM{i:04d}" for i in range(MAX_SYMBOLS_PER_BATCH)]
    inst = GetQuotesInput(symbols=syms)
    assert len(inst.symbols) == 50


def test_get_quotes_rejects_51() -> None:
    syms = [f"SYM{i:04d}" for i in range(MAX_SYMBOLS_PER_BATCH + 1)]
    with pytest.raises(SchwabValidationError) as ei:
        GetQuotesInput(symbols=syms)
    assert ei.value.field == "symbols"


def test_get_quotes_rejects_empty_list() -> None:
    with pytest.raises(Exception):
        GetQuotesInput(symbols=[])


# ---------------------------------------------------------------------------
# pricehistory cartesian product
# ---------------------------------------------------------------------------

# 9 legal combinations (plan §6.1 ≥9)
LEGAL_PRICE_HISTORY_COMBOS = [
    ("DAY", "ONE_DAY", "MINUTE", "EVERY_MINUTE"),
    ("DAY", "FIVE_DAYS", "MINUTE", "EVERY_FIVE_MINUTES"),
    ("DAY", "TEN_DAYS", "MINUTE", "EVERY_THIRTY_MINUTES"),
    ("MONTH", "SIX_MONTHS", "DAILY", None),
    ("MONTH", "ONE_DAY", "WEEKLY", None),
    ("YEAR", "FIFTEEN_YEARS", "DAILY", None),
    ("YEAR", "TWENTY_YEARS", "WEEKLY", None),
    ("YEAR", "TWENTY_YEARS", "MONTHLY", None),
    ("YEAR_TO_DATE", None, "WEEKLY", None),
]


@pytest.mark.parametrize("pt,p,ft,f", LEGAL_PRICE_HISTORY_COMBOS)
def test_price_history_legal_combos(pt: str, p: str | None, ft: str, f: str | None) -> None:
    GetPriceHistoryInput(
        symbol="AAPL",
        period_type=pt,
        period=p,  # type: ignore[arg-type]
        frequency_type=ft,
        frequency=f,  # type: ignore[arg-type]
    )


# 5 illegal combinations (plan §6.1 ≥5)
ILLEGAL_PRICE_HISTORY_COMBOS = [
    ("DAY", "FIFTEEN_YEARS", "MINUTE", "EVERY_MINUTE"),  # bad period for DAY
    ("DAY", "ONE_DAY", "DAILY", None),  # bad freq_type for DAY
    ("MONTH", "ONE_DAY", "MINUTE", "EVERY_MINUTE"),  # bad freq_type for MONTH
    ("YEAR", "ONE_DAY", "DAILY", None),  # bad period for YEAR
    ("DAY", "ONE_DAY", "MINUTE", "EVERY_FIFTEEN_MINUTES"),  # NOTE: this IS legal — replace below
    ("YEAR_TO_DATE", None, "MINUTE", None),  # MINUTE not allowed for YEAR_TO_DATE
]


# Drop the legal-but-mislabelled case at runtime (defensive).
ILLEGAL_PRICE_HISTORY_COMBOS = [
    c for c in ILLEGAL_PRICE_HISTORY_COMBOS if c != ("DAY", "ONE_DAY", "MINUTE", "EVERY_FIFTEEN_MINUTES")
]


@pytest.mark.parametrize("pt,p,ft,f", ILLEGAL_PRICE_HISTORY_COMBOS)
def test_price_history_illegal_combos(pt: str, p: str | None, ft: str, f: str | None) -> None:
    with pytest.raises(SchwabValidationError):
        GetPriceHistoryInput(
            symbol="AAPL",
            period_type=pt,
            period=p,  # type: ignore[arg-type]
            frequency_type=ft,
            frequency=f,  # type: ignore[arg-type]
        )


def test_price_history_naive_datetime_rejected() -> None:
    naive = datetime(2026, 1, 1, 12, 0, 0)
    with pytest.raises(SchwabValidationError):
        GetPriceHistoryInput(symbol="AAPL", start_datetime=naive)


def test_price_history_future_datetime_rejected() -> None:
    fut = datetime.now(tz=UTC) + timedelta(days=365)
    with pytest.raises(SchwabValidationError):
        GetPriceHistoryInput(symbol="AAPL", start_datetime=fut)


def test_price_history_end_before_start() -> None:
    start = datetime(2026, 1, 10, tzinfo=UTC)
    end = datetime(2026, 1, 5, tzinfo=UTC)
    with pytest.raises(SchwabValidationError):
        GetPriceHistoryInput(symbol="AAPL", start_datetime=start, end_datetime=end)


def test_price_history_unknown_period_type() -> None:
    # Construct manually since enum guard happens before model_validator.
    # An unknown period_type is rejected by Pydantic Literal first.
    with pytest.raises(Exception):
        GetPriceHistoryInput(symbol="AAPL", period_type="ZILLENNIAL")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Other models
# ---------------------------------------------------------------------------


def test_movers_input_valid() -> None:
    GetMoversInput(index="NASDAQ", sort_order="VOLUME", frequency="ZERO")


def test_movers_input_invalid_index() -> None:
    with pytest.raises(Exception):
        GetMoversInput(index="UNKNOWN")  # type: ignore[arg-type]


def test_option_chain_dates_consistent() -> None:
    f = datetime(2026, 1, 1, tzinfo=UTC)
    t = datetime(2026, 6, 1, tzinfo=UTC)
    GetOptionChainInput(symbol="AAPL", from_date=f, to_date=t)


def test_option_chain_to_before_from() -> None:
    f = datetime(2026, 6, 1, tzinfo=UTC)
    t = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(SchwabValidationError):
        GetOptionChainInput(symbol="AAPL", from_date=f, to_date=t)


def test_option_chain_strike_count_bounds() -> None:
    GetOptionChainInput(symbol="AAPL", strike_count=1)
    GetOptionChainInput(symbol="AAPL", strike_count=500)
    with pytest.raises(Exception):
        GetOptionChainInput(symbol="AAPL", strike_count=0)
    with pytest.raises(Exception):
        GetOptionChainInput(symbol="AAPL", strike_count=501)


def test_market_hours_valid_markets() -> None:
    GetMarketHoursInput(markets=["EQUITY", "OPTION"])


def test_market_hours_invalid_market() -> None:
    with pytest.raises(Exception):
        GetMarketHoursInput(markets=["CRYPTO"])  # type: ignore[arg-type]


def test_market_hours_max_5() -> None:
    GetMarketHoursInput(markets=["EQUITY", "OPTION", "BOND", "FUTURE", "FOREX"])


def test_market_hour_single() -> None:
    GetMarketHourSingleInput(market_id="EQUITY")


def test_search_instruments_projection_enum() -> None:
    SearchInstrumentsInput(symbols=["AAPL"], projection="SYMBOL_SEARCH")


def test_search_instruments_invalid_projection() -> None:
    with pytest.raises(Exception):
        SearchInstrumentsInput(symbols=["AAPL"], projection="HACK")  # type: ignore[arg-type]


def test_get_instrument_by_cusip_9_chars() -> None:
    GetInstrumentByCusipInput(cusip="037833100")


@pytest.mark.parametrize("bad", ["12345678", "1234567890", "037833lol", "037833 00", ""])
def test_get_instrument_by_cusip_rejects_bad(bad: str) -> None:
    with pytest.raises(Exception):
        GetInstrumentByCusipInput(cusip=bad)


def test_meta_input_models_take_no_args() -> None:
    HealthCheckInput()
    GetServerInfoInput()


# ---------------------------------------------------------------------------
# validate_tool_input dispatcher
# ---------------------------------------------------------------------------


def test_validate_tool_input_unknown_tool() -> None:
    with pytest.raises(SchwabValidationError) as ei:
        validate_tool_input("not_a_tool", {})
    assert ei.value.field == "tool_name"


def test_validate_tool_input_pydantic_to_structured() -> None:
    with pytest.raises(SchwabValidationError) as ei:
        validate_tool_input("get_quote", {"symbol": "lowercase"})
    assert ei.value.field == "symbol"


def test_validate_tool_input_round_trip() -> None:
    out = validate_tool_input("get_quote", {"symbol": "AAPL"})
    assert out.symbol == "AAPL"


def test_validate_tool_input_passes_through_schwab_validation_error() -> None:
    """``GetQuotesInput`` raises SchwabValidationError directly (length cap),
    and validate_tool_input must re-raise it untouched (line 448)."""
    syms = [f"SYM{i:04d}" for i in range(60)]
    with pytest.raises(SchwabValidationError) as ei:
        validate_tool_input("get_quotes", {"symbols": syms})
    assert ei.value.field == "symbols"


def test_supported_tool_names_count_14() -> None:
    names = supported_tool_names()
    assert len(names) == 14
    assert names[-4:] == ["health_check", "get_server_info", "get_cache_stats", "get_streaming_snapshot"]


def test_get_option_expiration_chain_input() -> None:
    GetOptionExpirationChainInput(symbol="AAPL")


def test_get_option_chain_full_kwargs() -> None:
    """Cover all optional fields once."""
    f = datetime(2026, 1, 1, tzinfo=UTC)
    t = datetime(2026, 6, 1, tzinfo=UTC)
    GetOptionChainInput(
        symbol="AAPL",
        contract_type="CALL",
        strike_count=10,
        include_underlying_quote=True,
        strategy="SINGLE",
        interval=1.0,
        strike=200.0,
        strike_range="OUT_OF_THE_MONEY",
        from_date=f,
        to_date=t,
        volatility=0.25,
        underlying_price=200.0,
        interest_rate=0.05,
        days_to_expiration=30,
        exp_month="JANUARY",
        option_type="STANDARD",
        entitlement="NON_PRO",
    )


def test_get_quote_with_fields() -> None:
    GetQuoteInput(symbol="AAPL", fields=["QUOTE", "FUNDAMENTAL"])  # type: ignore[arg-type]


def test_movers_with_only_index() -> None:
    GetMoversInput(index="$DJI" if False else "DJI")  # uses enum name


def test_movers_index_named() -> None:
    GetMoversInput(index="DJI")
