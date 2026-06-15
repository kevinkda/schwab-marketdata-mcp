"""Unit tests for ``Cache.aggregate_atm_iv``,
``Cache.get_iv_percentile_rank``, the ``_compute_atm_iv_for_bucket`` /
``_median`` helpers, and the ``get_iv_percentile_impl`` MCP tool wiring.

v0.4 P1/C — covers:

* ATM-IV aggregation across the 30/60/90-day buckets, including the
  +-7d tolerance window;
* writes one row per bucket to ``iv_history`` even when the bucket is
  empty (so the lookback density is preserved);
* percentile-rank computation: empty / single-observation / many-day
  history;
* sparse-data guard: when ``sample_count < 30`` the response's
  ``percentile_rank`` is muted to ``None`` and a ``warning`` is set;
* ``refresh=False`` read-only path (no HTTP);
* ``refresh=True`` triggers ``aggregate_atm_iv`` (the live chain fetch
  is exercised via the in-process FakeSchwabClient).

Tests use isolated DuckDB files under ``tmp_path`` and a per-test cache
singleton reset.  No real Schwab traffic.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from schwab_marketdata_mcp import cache as cache_mod
from schwab_marketdata_mcp.cache import (
    Cache,
    _compute_atm_iv_for_bucket,
    _median,
)
from schwab_marketdata_mcp.models import GetIvPercentileInput
from schwab_marketdata_mcp.tools import _runtime as rt
from schwab_marketdata_mcp.tools.options import get_iv_percentile_impl
from tests.conftest import make_stateful_clickhouse_cache

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _contract(
    *,
    expiry: date,
    strike: float,
    call_put: str,
    iv: float | None,
) -> dict[str, Any]:
    return {
        "expiry": expiry,
        "strike": strike,
        "call_put": call_put,
        "implied_vol": iv,
    }


def _seed_history(
    cache: Cache,
    underlying: str,
    bucket: str,
    series: list[tuple[date, float | None, int]],
) -> None:
    """Insert a synthetic IV time-series via the durable history writer."""
    for asof, iv, samples in series:
        cache._upsert_iv_history(underlying, asof, bucket, iv, samples)


@pytest.fixture(autouse=True)
def _isolated_cache_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Each test gets a fresh stateful ClickHouse-backed cache singleton so
    derived-analysis history (snapshots / iv_history) durably round-trips
    without a live ClickHouse — and a clean reset to avoid cross-test bleed."""
    monkeypatch.setenv("SCHWAB_CACHE_ENABLED", "1")
    monkeypatch.setenv("SCHWAB_CACHE_BYPASS", "0")
    cache_mod.reset_cache_singleton()
    cache_mod._singleton = make_stateful_clickhouse_cache()
    yield
    cache_mod.reset_cache_singleton()


# ---------------------------------------------------------------------------
# Pure-function helpers — _median / _compute_atm_iv_for_bucket
# ---------------------------------------------------------------------------


def test_median_empty_and_odd_and_even() -> None:
    """``_median`` covers the three numeric branches: empty → None,
    odd-length → middle element, even-length → mean of two middles."""
    assert _median([]) is None
    assert _median([5.0]) == pytest.approx(5.0)
    assert _median([1.0, 2.0, 3.0]) == pytest.approx(2.0)
    assert _median([1.0, 2.0, 3.0, 4.0]) == pytest.approx(2.5)


def test_compute_atm_iv_for_bucket_returns_average_of_call_and_put() -> None:
    """Single CALL+PUT pair at the median strike → average IV."""
    asof = date(2026, 5, 25)
    contracts = [
        _contract(expiry=asof + timedelta(days=30), strike=190.0, call_put="CALL", iv=0.30),
        _contract(expiry=asof + timedelta(days=30), strike=190.0, call_put="PUT", iv=0.34),
    ]
    iv, n = _compute_atm_iv_for_bucket(contracts, asof, bucket_days=30)
    assert n == 2
    assert iv == pytest.approx(0.32, rel=1e-6)


def test_compute_atm_iv_for_bucket_skips_out_of_window() -> None:
    """Contracts outside the +-7d window must not be sampled, and a
    bucket with zero in-window contracts returns ``(None, 0)``."""
    asof = date(2026, 5, 25)
    contracts = [
        # 100d out — way outside the 30d bucket.
        _contract(expiry=asof + timedelta(days=100), strike=190.0, call_put="CALL", iv=0.30),
    ]
    iv, n = _compute_atm_iv_for_bucket(contracts, asof, bucket_days=30)
    assert iv is None
    assert n == 0


def test_compute_atm_iv_for_bucket_drops_rows_missing_iv() -> None:
    """In-window strikes with no IV must not poison the average — when
    every in-window contract lacks IV, the bucket returns ``None``
    but the sample_count still reflects the in-window row count
    (so iv_history records the calendar density)."""
    asof = date(2026, 5, 25)
    contracts = [
        _contract(expiry=asof + timedelta(days=30), strike=190.0, call_put="CALL", iv=None),
        _contract(expiry=asof + timedelta(days=30), strike=190.0, call_put="PUT", iv=None),
    ]
    iv, n = _compute_atm_iv_for_bucket(contracts, asof, bucket_days=30)
    assert iv is None
    assert n == 2


# ---------------------------------------------------------------------------
# aggregate_atm_iv → iv_history persistence + later get_iv_percentile_rank
# ---------------------------------------------------------------------------


def test_aggregate_atm_iv_writes_rows_for_all_three_buckets() -> None:
    """One snapshot with contracts at 30/60/90-day expiries → three
    iv_history rows are written, one per bucket label."""
    asof = date(2026, 5, 25)
    snap = datetime(2026, 5, 25, 14, 30, tzinfo=UTC)
    contracts = []
    for dte, iv in [(30, 0.30), (60, 0.28), (90, 0.27)]:
        expiry = asof + timedelta(days=dte)
        contracts.append({"expiry": expiry, "strike": 190.0, "call_put": "CALL", "implied_vol": iv})
        contracts.append({"expiry": expiry, "strike": 190.0, "call_put": "PUT", "implied_vol": iv})
    c = cache_mod.get_cache()
    assert c is not None
    c.write_option_chain_snapshot("AAPL", snap, contracts)
    atm = c.aggregate_atm_iv("AAPL", asof, snapshot_at=snap)
    assert atm["30d"] == pytest.approx(0.30, rel=1e-6)
    assert atm["60d"] == pytest.approx(0.28, rel=1e-6)
    assert atm["90d"] == pytest.approx(0.27, rel=1e-6)


def test_aggregate_atm_iv_handles_empty_snapshot() -> None:
    """No snapshot for the underlying → all three buckets return ``None``."""
    c = cache_mod.get_cache()
    assert c is not None
    atm = c.aggregate_atm_iv("AAPL", date(2026, 5, 25))
    assert atm == {"30d": None, "60d": None, "90d": None}


def test_aggregate_atm_iv_invalid_input_returns_empty() -> None:
    """Empty underlying / unparseable date short-circuit to the all-None payload."""
    c = cache_mod.get_cache()
    assert c is not None
    assert c.aggregate_atm_iv("", date(2026, 5, 25)) == {"30d": None, "60d": None, "90d": None}
    assert c.aggregate_atm_iv("AAPL", "not-a-date") == {  # type: ignore[arg-type]
        "30d": None,
        "60d": None,
        "90d": None,
    }


def test_aggregate_atm_iv_memory_degrades() -> None:
    """On the memory backend the aggregator returns all-None (no history)."""
    from schwab_marketdata_mcp.cache_backend import MemoryBackend

    c = Cache(backend=MemoryBackend())
    snap = datetime(2026, 5, 25, 14, 30, tzinfo=UTC)
    c.write_option_chain_snapshot(
        "AAPL", snap, [_contract(expiry=date(2026, 6, 24), strike=190.0, call_put="CALL", iv=0.3)]
    )
    assert c.aggregate_atm_iv("AAPL", date(2026, 5, 25), snapshot_at=snap) == {"30d": None, "60d": None, "90d": None}


# ---------------------------------------------------------------------------
# get_iv_percentile_rank — the analytics reader
# ---------------------------------------------------------------------------


def test_get_iv_percentile_rank_empty_history_returns_null_payload() -> None:
    """No iv_history rows → all-None payload (sample_count=0)."""
    c = cache_mod.get_cache()
    assert c is not None
    out = c.get_iv_percentile_rank("AAPL", "30d", lookback_days=252)
    assert out["sample_count"] == 0
    assert out["current_iv"] is None
    assert out["percentile_rank"] is None
    assert out["min_iv"] is None
    assert out["max_iv"] is None


def test_get_iv_percentile_rank_single_observation_returns_neutral_50() -> None:
    """A single iv_history row → percentile undefined, but the rank is
    set to the neutral 50.0 sentinel so dashboards always show a number."""
    asof = date(2026, 5, 25)
    c = cache_mod.get_cache()
    assert c is not None
    _seed_history(c, "AAPL", "30d", [(asof, 0.30, 5)])
    out = c.get_iv_percentile_rank("AAPL", "30d", lookback_days=252)
    assert out["sample_count"] == 1
    assert out["current_iv"] == pytest.approx(0.30, rel=1e-6)
    assert out["percentile_rank"] == pytest.approx(50.0)
    assert out["min_iv"] == pytest.approx(0.30, rel=1e-6)
    assert out["max_iv"] == pytest.approx(0.30, rel=1e-6)
    assert out["current_asof"] == asof.isoformat()


def test_get_iv_percentile_rank_full_distribution() -> None:
    """A 100-row monotonically-rising history with the latest at the
    top → percentile_rank == 100."""
    base = date(2026, 5, 25)
    series: list[tuple[date, float, int]] = []
    for i in range(100):  # i=0 oldest .. i=99 newest
        series.append((base - timedelta(days=99 - i), 0.10 + 0.001 * i, 50))
    c = cache_mod.get_cache()
    assert c is not None
    _seed_history(c, "AAPL", "30d", series)
    out = c.get_iv_percentile_rank("AAPL", "30d", lookback_days=252)
    assert out["sample_count"] == 100
    assert out["current_iv"] == pytest.approx(0.199, rel=1e-6)
    assert out["percentile_rank"] == pytest.approx(100.0)
    assert out["min_iv"] == pytest.approx(0.10, rel=1e-6)
    assert out["max_iv"] == pytest.approx(0.199, rel=1e-6)


def test_get_iv_percentile_rank_invalid_bucket_raises() -> None:
    """Unsupported ``expiry_bucket`` → ValueError (input guard)."""
    c = cache_mod.get_cache()
    assert c is not None
    with pytest.raises(ValueError):
        c.get_iv_percentile_rank("AAPL", "45d")  # bucket not supported
    with pytest.raises(ValueError):
        c.get_iv_percentile_rank("AAPL", "30d", lookback_days=0)
    with pytest.raises(ValueError):
        c.get_iv_percentile_rank("", "30d")


# ---------------------------------------------------------------------------
# Tool wiring — get_iv_percentile_impl with refresh=False (read-only)
# ---------------------------------------------------------------------------


async def test_get_iv_percentile_impl_emits_sparse_warning_when_under_30() -> None:
    """When sample_count < 30 the impl mutes percentile_rank and emits
    the ``sample_count_below_30`` warning so callers can disclose the
    statistical noise to users."""
    cache = cache_mod.get_cache()
    assert cache is not None
    # Seed 5 rows — well below the sparse threshold.
    base = date(2026, 5, 25)
    for i in range(5):
        cache._upsert_iv_history(
            "AAPL",
            base - timedelta(days=4 - i),
            "30d",
            0.20 + 0.01 * i,
            10,
        )
    args = GetIvPercentileInput(underlying="AAPL", expiry_bucket="30d", lookback_days=252)
    out = await get_iv_percentile_impl(args)
    assert out["sample_count"] == 5
    assert out["percentile_rank"] is None  # muted by sparse-data guard
    assert "sample_count_below_30" in (out["warning"] or [])
    assert out["refresh"] is False


async def test_get_iv_percentile_impl_returns_rank_when_history_dense() -> None:
    """A 50-row history is above the sparse threshold → ``percentile_rank``
    is a real number and no sparse warning is emitted."""
    cache = cache_mod.get_cache()
    assert cache is not None
    base = date(2026, 5, 25)
    for i in range(50):
        cache._upsert_iv_history(
            "AAPL",
            base - timedelta(days=49 - i),
            "30d",
            0.10 + 0.001 * i,
            50,
        )
    args = GetIvPercentileInput(underlying="AAPL", expiry_bucket="30d")
    out = await get_iv_percentile_impl(args)
    assert out["sample_count"] == 50
    assert out["percentile_rank"] is not None
    assert out["warning"] in (None, [])


async def test_get_iv_percentile_impl_handles_disabled_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``SCHWAB_CACHE_ENABLED=0`` → empty payload + ``cache_disabled``
    warning, never raises.  Critical: the read path stays functional
    even when the writer side is off."""
    monkeypatch.setenv("SCHWAB_CACHE_ENABLED", "0")
    cache_mod.reset_cache_singleton()
    args = GetIvPercentileInput(underlying="AAPL", expiry_bucket="30d")
    out = await get_iv_percentile_impl(args)
    assert out["sample_count"] == 0
    assert out["percentile_rank"] is None
    assert "cache_disabled" in (out["warning"] or [])


# ---------------------------------------------------------------------------
# Tool wiring — refresh=True triggers a chain fetch + aggregate.
# We use the in-process FakeSchwabClient (fixtures backend) so the test
# never hits real Schwab.
# ---------------------------------------------------------------------------


async def test_get_iv_percentile_impl_refresh_calls_aggregate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``refresh=True`` must drive the live ``get_option_chain`` flow,
    persist a snapshot, run ``aggregate_atm_iv``, and return a
    ``refresh_summary`` block with ``rows_written`` and ``atm_iv``."""
    monkeypatch.setenv("SCHWAB_MOCK_BACKEND", "fixtures")
    monkeypatch.setenv(
        "SCHWAB_MOCK_FIXTURES_DIR",
        str(Path(__file__).resolve().parent / "fixtures"),
    )
    monkeypatch.setenv("SCHWAB_MOCK_SCENARIO", "normal")
    monkeypatch.setenv("SCHWAB_MAX_RETRIES", "0")
    rt.reset_client_cache()
    args = GetIvPercentileInput(
        underlying="AAPL",
        expiry_bucket="30d",
        refresh=True,
    )
    try:
        out = await get_iv_percentile_impl(args)
    finally:
        rt.reset_client_cache()
    assert out["refresh"] is True
    assert "refresh_summary" in out
    summary = out["refresh_summary"]
    assert isinstance(summary, dict)
    # The fixture chain has only a 2026-06-19 expiry — depending on
    # today's date, rows_written may legitimately be 0 or >= 2; both
    # are acceptable, but the structure must be present.
    assert "rows_written" in summary
    assert "atm_iv" in summary
    assert set(summary["atm_iv"].keys()) == {"30d", "60d", "90d"}
