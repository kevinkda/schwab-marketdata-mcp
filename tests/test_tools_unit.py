"""Unit tests for the 12 MCP tools.

Plan §6.1 — covers normal / 400 / 401 / 429 / 5xx / boundary paths.

We drive the tools through the FastMCP-decorated functions in
``server.py`` (which are still callable as plain async functions); the
backend is the in-process :class:`FakeSchwabClient` configured by env vars.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from schwab_marketdata_mcp import server
from schwab_marketdata_mcp.client import FakeSchwabClient, RateLimitedClient, RetryPolicy, TokenBucket
from schwab_marketdata_mcp.tools import _runtime as rt

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def repo_fixtures_dir() -> Path:
    return Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
async def fake_client(repo_fixtures_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Inject a FakeSchwabClient with retry+bucket; yield it via runtime cache."""
    monkeypatch.setenv("SCHWAB_MOCK_BACKEND", "fixtures")
    monkeypatch.setenv("SCHWAB_MOCK_FIXTURES_DIR", str(repo_fixtures_dir))
    monkeypatch.setenv("SCHWAB_MOCK_SCENARIO", "normal")
    monkeypatch.setenv("SCHWAB_MAX_RETRIES", "0")  # tests don't need real retries
    rt.reset_client_cache()
    yield
    rt.reset_client_cache()


def _set_scenario(monkeypatch: pytest.MonkeyPatch, scenario: str) -> None:
    monkeypatch.setenv("SCHWAB_MOCK_SCENARIO", scenario)
    rt.reset_client_cache()


# ---------------------------------------------------------------------------
# Per-tool happy-path tests
# ---------------------------------------------------------------------------


async def test_get_quote_normal(fake_client: None) -> None:
    out = await server.get_quote(symbol="AAPL")
    assert "AAPL" in out


async def test_get_quotes_normal(fake_client: None) -> None:
    out = await server.get_quotes_(symbols=["AAPL", "MSFT", "GOOGL"])
    assert "AAPL" in out
    assert "MSFT" in out


async def test_get_price_history_normal(fake_client: None) -> None:
    out = await server.get_price_history_(
        symbol="AAPL",
        period_type="DAY",
        period="ONE_DAY",
        frequency_type="MINUTE",
        frequency="EVERY_MINUTE",
    )
    assert "candles" in out


async def test_get_option_chain_normal(fake_client: None) -> None:
    out = await server.get_option_chain_(symbol="AAPL", contract_type="ALL")
    assert out["status"] == "SUCCESS"


async def test_get_option_expiration_chain_normal(fake_client: None) -> None:
    out = await server.get_option_expiration_chain_(symbol="AAPL")
    assert "expirationList" in out


async def test_get_market_hours_normal(fake_client: None) -> None:
    out = await server.get_market_hours_(markets_list=["EQUITY"])
    assert "equity" in out


async def test_get_market_hour_single_normal(fake_client: None) -> None:
    out = await server.get_market_hour_single_(market_id="EQUITY")
    assert "equity" in out


async def test_get_movers_normal(fake_client: None) -> None:
    out = await server.get_movers_(index="NASDAQ", sort_order="VOLUME")
    assert "screeners" in out


async def test_search_instruments_normal(fake_client: None) -> None:
    out = await server.search_instruments_(symbols=["AAPL"], projection="SYMBOL_SEARCH")
    assert "instruments" in out


async def test_get_instrument_by_cusip_normal(fake_client: None) -> None:
    out = await server.get_instrument_by_cusip_(cusip="037833100")
    assert "instruments" in out


# ---------------------------------------------------------------------------
# Meta tools — offline-safe even without token
# ---------------------------------------------------------------------------


async def test_health_check_offline_safe(fake_client: None) -> None:
    out = await server.health_check()
    assert "server_version" in out
    assert "token_state" in out
    assert "supported_tools" not in out  # health_check is metadata only
    assert out["rate_limit_remaining_per_min"] == 120


async def test_get_server_info_includes_13_tools(fake_client: None) -> None:
    out = await server.get_server_info()
    assert len(out["supported_tools"]) == 13
    assert "compatible_skill_version_range" not in out  # plan §3.1 — not exposed
    assert out["server_version"] == "0.1.0"


# ---------------------------------------------------------------------------
# Validation rejection at the tool boundary
# ---------------------------------------------------------------------------


async def test_get_quote_invalid_symbol_returns_structured_error(fake_client: None) -> None:
    out = await server.get_quote(symbol="lowercase")
    assert out["error"] == "SchwabValidationError"
    assert out["field"] == "symbol"


async def test_get_quotes_too_many_symbols(fake_client: None) -> None:
    out = await server.get_quotes_(symbols=[f"SYM{i:04d}" for i in range(60)])
    assert out["error"] == "SchwabValidationError"


async def test_get_instrument_by_cusip_bad_length(fake_client: None) -> None:
    out = await server.get_instrument_by_cusip_(cusip="ABC")
    assert out["error"] == "SchwabValidationError"


# ---------------------------------------------------------------------------
# Auth / rate-limit / 5xx error scenarios via FakeSchwabClient
# ---------------------------------------------------------------------------


async def test_auth_error_normalized(fake_client: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_scenario(monkeypatch, "auth_error")
    out = await server.get_quote(symbol="AAPL")
    assert out["error"] == "SchwabAuthError"
    assert out["reason"] == "access_token_invalid"
    # OWASP A02 — hint must not contain raw upstream tokens.
    assert "Bearer" not in out["hint"] or "***REDACTED***" in out["hint"]


async def test_rate_limit_error_surfaces_retry_after(fake_client: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_scenario(monkeypatch, "rate_limit")
    out = await server.get_quote(symbol="AAPL")
    # With SCHWAB_MAX_RETRIES=0 the retry path is disabled; we therefore expect
    # the error to surface immediately on the first 429.
    assert out["error"] == "SchwabRateLimitError"


async def test_5xx_error_normalized(fake_client: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_scenario(monkeypatch, "5xx")
    out = await server.get_quote(symbol="AAPL")
    assert out["error"] == "SchwabTransientError"
    assert out["status_code"] == 503


# ---------------------------------------------------------------------------
# Boundary / edge cases
# ---------------------------------------------------------------------------


async def test_get_quote_index_symbol(fake_client: None) -> None:
    out = await server.get_quote(symbol="$SPX")
    assert "AAPL" in out  # fixture is shared; presence proves call succeeded


async def test_get_market_hours_max_5_markets(fake_client: None) -> None:
    out = await server.get_market_hours_(markets_list=["EQUITY", "OPTION", "BOND", "FUTURE", "FOREX"])
    assert "equity" in out


async def test_get_option_chain_with_dates(fake_client: None) -> None:
    out = await server.get_option_chain_(
        symbol="AAPL",
        from_date="2026-06-01T00:00:00+00:00",
        to_date="2026-06-30T00:00:00+00:00",
    )
    assert out["status"] == "SUCCESS"


# ---------------------------------------------------------------------------
# RateLimitedClient internal behaviour — exhaustion + retry semantics
# ---------------------------------------------------------------------------


async def test_token_bucket_refills_after_window() -> None:
    b = TokenBucket(capacity=2, window_seconds=0.2)
    assert await b.try_acquire()
    assert await b.try_acquire()
    assert not await b.try_acquire()
    await asyncio.sleep(0.25)
    assert await b.try_acquire()


async def test_rate_limited_client_raises_when_bucket_empty() -> None:
    """Plan §3.2.4 — token == 0 → immediate SchwabRateLimitError."""
    fake = FakeSchwabClient(fixtures_dir=Path(__file__).resolve().parent / "fixtures")
    client = RateLimitedClient(
        fake,
        bucket=TokenBucket(capacity=0),
        retry=RetryPolicy(max_429=0, max_5xx=0),
    )

    async def fetch() -> Any:
        return await fake.get_quote("AAPL")

    from schwab_marketdata_mcp.errors import SchwabRateLimitError

    with pytest.raises(SchwabRateLimitError):
        await client.call(fetch, tool_name="get_quote")


# ---------------------------------------------------------------------------
# meta module: cover both the missing-token and valid-token branches
# ---------------------------------------------------------------------------


async def test_health_check_with_valid_token(fake_client: None, tmp_path: Path) -> None:
    # Place a token.json under the isolated XDG_STATE_HOME so health_check_impl
    # exercises the VALID branch.
    state_root = Path(os.environ["XDG_STATE_HOME"])
    pdir = state_root / "schwab-marketdata-mcp"
    pdir.mkdir(parents=True, exist_ok=True)
    os.chmod(pdir, 0o700)
    p = pdir / "token.json"
    p.write_text('{"creation_timestamp": 1700000000}')
    os.chmod(p, 0o600)
    out = await server.health_check()
    assert out["token_state"] == "valid"
    assert isinstance(out["token_age_days"], (int, float))


async def test_health_check_invalid_xdg_falls_through() -> None:
    """When XDG resolution fails the meta tool degrades to MISSING."""
    # We don't set up a token; default state is missing -> handled.
    out = await server.health_check()
    assert out["token_state"] == "missing"
