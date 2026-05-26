"""FastMCP server entry point — 15 outward-facing tools.

Plan §3.2.3 — the **first** thing the module does is harden stdio so that
no stray ``print`` from schwab-py / httpx pollutes the JSON-RPC stream.

We deliberately:

* **DO NOT** ``os.dup2(2, 1)`` — that would redirect MCP SDK's own JSON-RPC
  output to stderr and break the protocol.
* **DO NOT** assign ``sys.stdout = sys.stderr`` — same reason.
* **DO** monkey-patch ``builtins.print`` so default ``file`` is ``sys.stderr``.
* **DO** install a :class:`RotatingFileHandler` writing to
  ``${XDG_STATE_HOME}/schwab-marketdata-mcp/logs/server.log``.
* **DO** force ``httpx`` / ``httpcore`` / ``schwab`` loggers to ``WARNING``.
* **DO** install the global :class:`RedactBearerFilter` on the root logger.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 0) Stdio hardening — must run BEFORE we import anything that might log /
#    print at import time (schwab, httpx, etc).  Plan §3.2.3.
# ---------------------------------------------------------------------------
import builtins
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


def _harden_stdio() -> None:
    """Install the print + logging mitigations described in plan §3.2.3."""
    # 1) builtins.print → stderr by default.  We do **not** touch sys.stdout
    #    so the MCP SDK can still drive JSON-RPC frames on fd 1.
    _orig_print = builtins.print

    def _safe_print(*args: Any, file: Any = None, **kwargs: Any) -> None:
        _orig_print(*args, file=file or sys.stderr, **kwargs)

    builtins.print = _safe_print

    # 2) Logging - RotatingFileHandler + StreamHandler(stderr).
    from . import _platform

    state_root = _platform.state_root()
    log_dir: Path | None = state_root / "schwab-marketdata-mcp" / "logs"
    try:
        assert log_dir is not None
        log_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    except OSError:
        log_dir = None

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_dir is not None:
        try:
            file_handler = RotatingFileHandler(
                log_dir / "server.log",
                maxBytes=10 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
            file_handler.setFormatter(
                logging.Formatter('{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":%(message)r}')
            )
            handlers.append(file_handler)
        except OSError:
            pass

    level = os.environ.get("LOG_LEVEL", "WARNING").upper()
    logging.basicConfig(
        handlers=handlers,
        level=getattr(logging, level, logging.WARNING),
        format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":%(message)r}',
        force=True,
    )
    # 3) Force noisy upstream loggers to WARNING.
    for noisy in ("httpx", "httpcore", "schwab"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # 4) Global Bearer redact filter.
    from .errors import RedactBearerFilter

    redact = RedactBearerFilter()
    for handler in logging.getLogger().handlers:
        handler.addFilter(redact)


_harden_stdio()


# ---------------------------------------------------------------------------
# 0b) Load .env from the current working directory before any business
#     import reads SCHWAB_APP_KEY / SCHWAB_APP_SECRET / etc.  Host-injected
#     env vars (Cursor mcp.json ``envFile``, Claude Desktop wrappers, plain
#     shell exports) still win because ``override=False``.  See plan §3.3.
# ---------------------------------------------------------------------------
from .bootstrap import bootstrap_dotenv  # noqa: E402

bootstrap_dotenv()


# ---------------------------------------------------------------------------
# Imports after hardening
# ---------------------------------------------------------------------------

from collections.abc import Awaitable, Callable  # noqa: E402
from typing import Final  # noqa: E402

from mcp.server.fastmcp import FastMCP  # noqa: E402

from . import __version__ as SERVER_VERSION  # noqa: E402
from .errors import (  # noqa: E402
    SchwabAuthError,
    SchwabError,
    SchwabRateLimitError,
    SchwabTransientError,
    SchwabValidationError,
)
from .models import (  # noqa: E402
    supported_tool_names,
    validate_tool_input,
)
from .tools import (  # noqa: E402
    instruments,
    markets,
    meta,
    movers,
    options,
    price_history,
    quotes,
    streaming,
)

log = logging.getLogger("schwab_marketdata_mcp.server")

mcp = FastMCP(
    name="schwab-marketdata-mcp",
    instructions=(
        "Schwab Market Data Production tools.  Read-only — no Trader API.  "
        "If a tool returns SchwabAuthError(reason='refresh_token_expired'), "
        "tell the user to run "
        "`uv run python -m schwab_marketdata_mcp.auth login_flow`."
    ),
)

# FastMCP ctor (mcp SDK 1.27.x) does not expose a ``version=`` kwarg, so the
# underlying lowlevel ``Server.version`` defaults to ``None`` and the
# ``initialize`` response falls back to ``importlib.metadata.version("mcp")``
# (the framework version, e.g. 1.27.1).  Inject the project release tag
# directly on the lowlevel server so ``serverInfo.version`` reflects this
# package's ``__version__`` (e.g. 0.3.0).
mcp._mcp_server.version = SERVER_VERSION

SUPPORTED_TOOLS: Final[list[str]] = supported_tool_names()


def _err_to_dict(exc: SchwabError) -> dict[str, Any]:
    """Convert any Schwab* exception to a structured JSON-safe dict."""
    if isinstance(exc, SchwabAuthError):
        return {
            "error": "SchwabAuthError",
            "reason": exc.reason,
            "expires_in_seconds": exc.expires_in_seconds,
            "hint": exc.hint,
        }
    if isinstance(exc, SchwabRateLimitError):
        return {
            "error": "SchwabRateLimitError",
            "retry_after_seconds": exc.retry_after_seconds,
            "current_window_used": exc.current_window_used,
        }
    if isinstance(exc, SchwabTransientError):
        return {
            "error": "SchwabTransientError",
            "status_code": exc.status_code,
            "attempt": exc.attempt,
            "hint": exc.hint,
        }
    if isinstance(exc, SchwabValidationError):
        return {
            "error": "SchwabValidationError",
            "field": exc.field,
            "reason": exc.reason,
        }
    return {"error": exc.__class__.__name__, "message": str(exc)}


async def _run_tool(
    raw: dict[str, Any],
    tool_name: str,
    impl: Callable[[Any], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    """Validate ``raw`` against the tool's input schema, then delegate to ``impl``."""
    try:
        validated = validate_tool_input(tool_name, raw)
        return await impl(validated)
    except SchwabError as exc:
        return _err_to_dict(exc)


# ---------------------------------------------------------------------------
# 15 tools — each is a thin wrapper that validates → delegates → catches.
# ---------------------------------------------------------------------------


@mcp.tool(
    name="get_quote",
    description="Get a single quote by symbol (stock, ETF, index $XYZ, or OSI option).",
)
async def get_quote(symbol: str, fields: list[str] | None = None) -> dict[str, Any]:
    return await _run_tool(
        {"symbol": symbol, "fields": fields},
        "get_quote",
        lambda v: quotes.get_quote_impl(v),
    )


@mcp.tool(
    name="get_quotes",
    description="Batch quotes (max 50 symbols per request to keep the JSON-RPC frame manageable).",
)
async def get_quotes_(  # name suffix avoids shadowing models.GetQuotesInput
    symbols: list[str],
    fields: list[str] | None = None,
    indicative: bool | None = None,
) -> dict[str, Any]:
    return await _run_tool(
        {"symbols": symbols, "fields": fields, "indicative": indicative},
        "get_quotes",
        lambda v: quotes.get_quotes_impl(v),
    )


@mcp.tool(
    name="get_price_history",
    description=(
        "Historical OHLC + volume.  Schwab silently 400s on illegal "
        "(period_type, period, frequency_type, frequency) combos — this tool "
        "rejects them up-front via Pydantic validation."
    ),
)
async def get_price_history_(
    symbol: str,
    period_type: str | None = None,
    period: str | None = None,
    frequency_type: str | None = None,
    frequency: str | None = None,
    start_datetime: str | None = None,
    end_datetime: str | None = None,
    need_extended_hours_data: bool | None = None,
    need_previous_close: bool | None = None,
) -> dict[str, Any]:
    return await _run_tool(
        {
            "symbol": symbol,
            "period_type": period_type,
            "period": period,
            "frequency_type": frequency_type,
            "frequency": frequency,
            "start_datetime": start_datetime,
            "end_datetime": end_datetime,
            "need_extended_hours_data": need_extended_hours_data,
            "need_previous_close": need_previous_close,
        },
        "get_price_history",
        lambda v: price_history.get_price_history_impl(v),
    )


@mcp.tool(
    name="get_option_chain",
    description="Full option chain for an underlying.  See plan §3.1 for the parameter matrix.",
)
async def get_option_chain_(
    symbol: str,
    contract_type: str | None = None,
    strike_count: int | None = None,
    include_underlying_quote: bool | None = None,
    strategy: str | None = None,
    interval: float | None = None,
    strike: float | None = None,
    strike_range: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    volatility: float | None = None,
    underlying_price: float | None = None,
    interest_rate: float | None = None,
    days_to_expiration: int | None = None,
    exp_month: str | None = None,
    option_type: str | None = None,
    entitlement: str | None = None,
) -> dict[str, Any]:
    return await _run_tool(
        {
            "symbol": symbol,
            "contract_type": contract_type,
            "strike_count": strike_count,
            "include_underlying_quote": include_underlying_quote,
            "strategy": strategy,
            "interval": interval,
            "strike": strike,
            "strike_range": strike_range,
            "from_date": from_date,
            "to_date": to_date,
            "volatility": volatility,
            "underlying_price": underlying_price,
            "interest_rate": interest_rate,
            "days_to_expiration": days_to_expiration,
            "exp_month": exp_month,
            "option_type": option_type,
            "entitlement": entitlement,
        },
        "get_option_chain",
        lambda v: options.get_option_chain_impl(v),
    )


@mcp.tool(
    name="get_option_expiration_chain",
    description="Available expiration dates for an underlying's options.",
)
async def get_option_expiration_chain_(symbol: str) -> dict[str, Any]:
    return await _run_tool(
        {"symbol": symbol},
        "get_option_expiration_chain",
        lambda v: options.get_option_expiration_chain_impl(v),
    )


@mcp.tool(
    name="get_market_hours",
    description="Market hours for one or more markets (EQUITY/OPTION/BOND/FUTURE/FOREX).",
)
async def get_market_hours_(markets_list: list[str], date: str | None = None) -> dict[str, Any]:
    return await _run_tool(
        {"markets": markets_list, "date": date},
        "get_market_hours",
        lambda v: markets.get_market_hours_impl(v),
    )


@mcp.tool(
    name="get_market_hour_single",
    description="Market hours for a single market_id (EQUITY/OPTION/BOND/FUTURE/FOREX).",
)
async def get_market_hour_single_(market_id: str, date: str | None = None) -> dict[str, Any]:
    return await _run_tool(
        {"market_id": market_id, "date": date},
        "get_market_hour_single",
        lambda v: markets.get_market_hour_single_impl(v),
    )


@mcp.tool(
    name="get_movers",
    description="Top movers for an index (e.g. NASDAQ, $DJI, $SPX).",
)
async def get_movers_(index: str, sort_order: str | None = None, frequency: str | None = None) -> dict[str, Any]:
    return await _run_tool(
        {"index": index, "sort_order": sort_order, "frequency": frequency},
        "get_movers",
        lambda v: movers.get_movers_impl(v),
    )


@mcp.tool(
    name="search_instruments",
    description="Search instruments by symbol/regex/description (projection enum).",
)
async def search_instruments_(symbols: list[str], projection: str) -> dict[str, Any]:
    return await _run_tool(
        {"symbols": symbols, "projection": projection},
        "search_instruments",
        lambda v: instruments.search_instruments_impl(v),
    )


@mcp.tool(
    name="get_instrument_by_cusip",
    description="Look up an instrument by 9-character CUSIP.",
)
async def get_instrument_by_cusip_(cusip: str) -> dict[str, Any]:
    return await _run_tool(
        {"cusip": cusip},
        "get_instrument_by_cusip",
        lambda v: instruments.get_instrument_by_cusip_impl(v),
    )


@mcp.tool(
    name="health_check",
    description=(
        "Local health check (offline-safe; never calls Schwab).  Returns "
        "token age, expiry estimate, recent error count, rate-limit budget."
    ),
)
async def health_check() -> dict[str, Any]:
    try:
        return await meta.health_check_impl()
    except SchwabError as exc:
        return _err_to_dict(exc)


@mcp.tool(
    name="get_server_info",
    description=(
        "Local server-info (offline-safe).  Returns server_version, "
        "mcp_sdk_version, schwab_py_version, supported_tools."
    ),
)
async def get_server_info() -> dict[str, Any]:
    return await meta.get_server_info_impl(server_version=SERVER_VERSION)


@mcp.tool(
    name="get_cache_stats",
    description=(
        "Local DuckDB cache health (offline-safe; never calls Schwab). "
        "Returns db_path, enabled flag, size_mb, rows_per_table, "
        "expired_rows, hit_rate_24h, hits_24h, misses_24h so the agent "
        "can reason about cache effectiveness before bypassing."
    ),
)
async def get_cache_stats() -> dict[str, Any]:
    try:
        return await meta.get_cache_stats_impl()
    except SchwabError as exc:
        return _err_to_dict(exc)


@mcp.tool(
    name="get_iv_percentile",
    description=(
        "Compute the current ATM IV percentile rank for an underlying "
        "vs. N days of cached history (default 252 ≈ 1 trading year). "
        "Buckets: '30d' / '60d' / '90d'.  When refresh=False (default) "
        "this serves only from the local DuckDB iv_history table — "
        "ideal for batch / dashboard reads.  When refresh=True the "
        "tool first pulls a fresh option chain via get_option_chain, "
        "writes a snapshot to option_chain_snapshots, and aggregates "
        "today's ATM IV before computing the rank.  When sample_count "
        "< 30 the percentile_rank is returned as null with a warning "
        "so the caller does not over-interpret a tiny sample."
    ),
)
async def get_iv_percentile_(
    underlying: str,
    expiry_bucket: str = "30d",
    lookback_days: int = 252,
    refresh: bool = False,
) -> dict[str, Any]:
    return await _run_tool(
        {
            "underlying": underlying,
            "expiry_bucket": expiry_bucket,
            "lookback_days": lookback_days,
            "refresh": refresh,
        },
        "get_iv_percentile",
        lambda v: options.get_iv_percentile_impl(v),
    )


@mcp.tool(
    name="get_streaming_snapshot",
    description=(
        "Experimental: open a Schwab Streamer WebSocket, collect messages "
        "for the requested duration (default 2s, hard-bounded 500ms-10s), "
        "then disconnect and return per-symbol snapshots. service options: "
        "LEVELONE_EQUITIES (real-time bid/ask/last/volume) or CHART_EQUITY "
        "(real-time 1-minute candles). Use sparingly - long-running "
        "subscriptions remain out of scope; see plan section 10."
    ),
)
async def get_streaming_snapshot_(
    symbols: list[str],
    service: str,
    duration_ms: int | None = None,
) -> dict[str, Any]:
    return await _run_tool(
        {"symbols": symbols, "service": service, "duration_ms": duration_ms},
        "get_streaming_snapshot",
        lambda v: streaming.get_streaming_snapshot_impl(v),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """``schwab-marketdata-mcp`` console-script entry."""
    log.info('{"event":"server_start","version":"%s"}', SERVER_VERSION)
    mcp.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = ["SUPPORTED_TOOLS", "main", "mcp"]
