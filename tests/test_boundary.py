"""Boundary / edge-case security suite (batch 2).

Tests input-validation boundaries (min/max symbol counts, duration bounds,
lookback bounds), numeric edge values (zero / negative / overflow), empty &
single-element collections, and cache TTL / sparse-data edges.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from schwab_marketdata_mcp import cache
from schwab_marketdata_mcp.cache_backend import MemoryBackend
from schwab_marketdata_mcp.errors import SchwabValidationError
from schwab_marketdata_mcp.models import (
    MAX_STREAMING_SNAPSHOT_SYMBOLS,
    MAX_SYMBOLS_PER_BATCH,
    validate_tool_input,
)
from tests.conftest import make_stateful_clickhouse_cache

# ---------------------------------------------------------------------------
# Symbol-batch boundaries (get_quotes)
# ---------------------------------------------------------------------------


def test_boundary_quotes_single_symbol_ok() -> None:
    """Lower bound: a single symbol is valid."""
    out = validate_tool_input("get_quotes", {"symbols": ["AAPL"]})
    assert out.symbols == ["AAPL"]


def test_boundary_quotes_exactly_max_ok() -> None:
    """Upper bound: exactly MAX symbols is valid."""
    syms = [f"SY{i:03d}" for i in range(MAX_SYMBOLS_PER_BATCH)]
    out = validate_tool_input("get_quotes", {"symbols": syms})
    assert len(out.symbols) == MAX_SYMBOLS_PER_BATCH


def test_boundary_quotes_max_plus_one_rejected() -> None:
    """Just over the upper bound is rejected."""
    syms = [f"SY{i:03d}" for i in range(MAX_SYMBOLS_PER_BATCH + 1)]
    with pytest.raises(SchwabValidationError):
        validate_tool_input("get_quotes", {"symbols": syms})


def test_boundary_quotes_empty_rejected() -> None:
    """Empty symbol list is rejected (min_length=1)."""
    with pytest.raises(SchwabValidationError):
        validate_tool_input("get_quotes", {"symbols": []})


# ---------------------------------------------------------------------------
# Streaming symbol + duration boundaries
# ---------------------------------------------------------------------------


def test_boundary_streaming_exactly_max_symbols_ok() -> None:
    """Streaming accepts exactly its (tighter) symbol cap."""
    syms = [f"SY{i:03d}" for i in range(MAX_STREAMING_SNAPSHOT_SYMBOLS)]
    out = validate_tool_input(
        "get_streaming_snapshot",
        {"symbols": syms, "service": "LEVELONE_EQUITIES"},
    )
    assert len(out.symbols) == MAX_STREAMING_SNAPSHOT_SYMBOLS


def test_boundary_streaming_max_plus_one_rejected() -> None:
    """One past the streaming symbol cap is rejected."""
    syms = [f"SY{i:03d}" for i in range(MAX_STREAMING_SNAPSHOT_SYMBOLS + 1)]
    with pytest.raises(SchwabValidationError):
        validate_tool_input(
            "get_streaming_snapshot",
            {"symbols": syms, "service": "LEVELONE_EQUITIES"},
        )


@pytest.mark.parametrize("dur", [500, 10_000])
def test_boundary_streaming_duration_edges_ok(dur: int) -> None:
    """Exact min (500ms) and max (10s) durations are valid."""
    out = validate_tool_input(
        "get_streaming_snapshot",
        {"symbols": ["VOO"], "service": "LEVELONE_EQUITIES", "duration_ms": dur},
    )
    assert out.duration_ms == dur


@pytest.mark.parametrize("dur", [499, 10_001, 0, -1])
def test_boundary_streaming_duration_out_of_range_rejected(dur: int) -> None:
    """Durations just outside the bounds (and non-positive) are rejected."""
    with pytest.raises(SchwabValidationError):
        validate_tool_input(
            "get_streaming_snapshot",
            {"symbols": ["VOO"], "service": "LEVELONE_EQUITIES", "duration_ms": dur},
        )


# ---------------------------------------------------------------------------
# IV percentile lookback boundaries
# ---------------------------------------------------------------------------


def test_boundary_iv_lookback_minimum_ok() -> None:
    """lookback_days at the documented minimum (30) is valid."""
    out = validate_tool_input(
        "get_iv_percentile",
        {"underlying": "AAPL", "expiry_bucket": "30d", "lookback_days": 30},
    )
    assert out.lookback_days == 30


@pytest.mark.parametrize("bad", [0, 29, -5])
def test_boundary_iv_lookback_below_min_rejected(bad: int) -> None:
    """lookback_days below the minimum is rejected."""
    with pytest.raises(SchwabValidationError):
        validate_tool_input(
            "get_iv_percentile",
            {"underlying": "AAPL", "expiry_bucket": "30d", "lookback_days": bad},
        )


@pytest.mark.parametrize("bucket", ["30d", "60d", "90d"])
def test_boundary_iv_bucket_accepted_values(bucket: str) -> None:
    """All three documented expiry buckets validate."""
    out = validate_tool_input(
        "get_iv_percentile",
        {"underlying": "AAPL", "expiry_bucket": bucket},
    )
    assert out.expiry_bucket == bucket


def test_boundary_iv_bucket_unknown_rejected() -> None:
    """An undocumented bucket is rejected."""
    with pytest.raises(SchwabValidationError):
        validate_tool_input(
            "get_iv_percentile",
            {"underlying": "AAPL", "expiry_bucket": "45d"},
        )


# ---------------------------------------------------------------------------
# Symbol length boundaries
# ---------------------------------------------------------------------------


def test_boundary_symbol_single_char_ok() -> None:
    """A 1-char ticker (e.g. 'F') is valid."""
    from schwab_marketdata_mcp.models import GetQuoteInput

    assert GetQuoteInput(symbol="F").symbol == "F"


def test_boundary_symbol_too_long_rejected() -> None:
    """A symbol beyond the max length is rejected."""
    from schwab_marketdata_mcp.models import GetQuoteInput

    with pytest.raises(Exception):
        GetQuoteInput(symbol="A" * 25)


def test_boundary_symbol_empty_rejected() -> None:
    """An empty symbol string is rejected."""
    from schwab_marketdata_mcp.models import GetQuoteInput

    with pytest.raises(Exception):
        GetQuoteInput(symbol="")


# ---------------------------------------------------------------------------
# Cache TTL edges
# ---------------------------------------------------------------------------


def test_boundary_cache_ttl_zero_expires_immediately() -> None:
    """ttl_seconds=0 means any positive age is expired."""
    import time

    with cache.Cache(backend=MemoryBackend()) as c:
        c.put_quote("AAPL", {"AAPL": {"quote": {"lastPrice": 1.0}}}, ttl_seconds=0)
        time.sleep(0.02)
        assert c.get_quote("AAPL") is None


def test_boundary_cache_ttl_large_keeps_fresh() -> None:
    """A large TTL keeps the entry fresh on immediate read."""
    with cache.Cache(backend=MemoryBackend()) as c:
        c.put_quote("AAPL", {"AAPL": {"quote": {"lastPrice": 1.0}}}, ttl_seconds=10_000)
        assert c.get_quote("AAPL") is not None


def test_boundary_hourly_breakdown_min_hours() -> None:
    """hourly_breakdown rejects hours < 1 (lower-bound guard)."""
    with cache.Cache(backend=MemoryBackend()) as c:
        with pytest.raises(ValueError):
            c.hourly_breakdown(0)


# ---------------------------------------------------------------------------
# IV percentile sparse-data edges (single / empty distribution)
# ---------------------------------------------------------------------------


def test_boundary_iv_rank_empty_history_null_payload() -> None:
    """No history → null percentile payload with sample_count 0."""
    with cache.Cache(backend=MemoryBackend()) as c:
        out = c.get_iv_percentile_rank("AAPL", "30d", 252)
    assert out["sample_count"] == 0
    assert out["percentile_rank"] is None


def test_boundary_iv_rank_single_observation_neutral_50() -> None:
    """A single observation yields the neutral 50.0 rank (no distribution)."""
    with make_stateful_clickhouse_cache() as c:
        c._upsert_iv_history("AAPL", date(2026, 5, 1), "30d", 0.30, 5)
        out = c.get_iv_percentile_rank("AAPL", "30d", 252)
    assert out["sample_count"] == 1
    assert out["percentile_rank"] == 50.0


# ---------------------------------------------------------------------------
# ATM IV bucket numeric edges
# ---------------------------------------------------------------------------


def test_boundary_atm_iv_dte_window_edges() -> None:
    """A contract exactly at the bucket-tolerance edge is included; one past it
    is excluded."""
    asof = date(2026, 5, 1)
    edge_in = asof + timedelta(days=cache.IV_BUCKET_30D_DAYS + cache.IV_BUCKET_TOLERANCE_DAYS)
    edge_out = asof + timedelta(days=cache.IV_BUCKET_30D_DAYS + cache.IV_BUCKET_TOLERANCE_DAYS + 1)
    contracts = [
        {"expiry": edge_in, "strike": 100.0, "implied_vol": 0.30},
        {"expiry": edge_out, "strike": 100.0, "implied_vol": 0.99},
    ]
    iv, count = cache._compute_atm_iv_for_bucket(contracts, asof, cache.IV_BUCKET_30D_DAYS)
    assert count == 1  # only the in-window contract counts
    assert iv == pytest.approx(0.30)


def test_boundary_median_even_and_odd() -> None:
    """Median handles both odd and even-length inputs and empty → None."""
    assert cache._median([]) is None
    assert cache._median([5.0]) == 5.0
    assert cache._median([1.0, 3.0]) == 2.0
    assert cache._median([1.0, 2.0, 3.0]) == 2.0


def test_boundary_coerce_iv_threshold() -> None:
    """IV normalisation flips at the 1.5 heuristic boundary."""
    # Just above 1.5 → treated as percent.
    assert cache._coerce_iv(1.6) == pytest.approx(0.016)
    # 1.5 exactly and below → kept as fractional.
    assert cache._coerce_iv(1.5) == pytest.approx(1.5)
    assert cache._coerce_iv(0.5) == pytest.approx(0.5)
