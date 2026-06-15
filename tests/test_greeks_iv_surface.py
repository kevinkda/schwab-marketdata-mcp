"""Unit tests for v0.6 T1 marketdata-chain features.

Covers:

* **E1 — ``get_option_greeks_summary_impl``**: net Greeks aggregation
  from a live chain.  Three scenarios per the sprint contract:

  - net-value correctness (open-interest weighted + equal weighted),
  - empty / single-contract / missing-field boundaries,
  - expiry filtering and the OI-unavailable equal-weight fallback.

  The chain fetch is monkeypatched at ``get_option_chain_impl`` so no
  real Schwab traffic occurs; one fixtures-backed integration test also
  exercises the live ``get_option_chain`` path via ``FakeSchwabClient``.

* **E6 — ``get_iv_surface_impl`` + ``Cache.get_iv_surface``**: the ATM
  IV term-structure surface across the 30d/60d/90d buckets.

  - with ClickHouse history (stateful fake backend), per-bucket ranks
    are populated and ``total_sample_count`` is correct,
  - without persistence (cache disabled / memory backend) the tool
    degrades to ``requires_clickhouse_persistence`` rather than raising.

No real Schwab traffic and no live ClickHouse — everything is mocked.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from schwab_marketdata_mcp import cache as cache_mod
from schwab_marketdata_mcp.cache import Cache
from schwab_marketdata_mcp.cache_backend import MemoryBackend
from schwab_marketdata_mcp.models import (
    GetIvSurfaceInput,
    GetOptionGreeksSummaryInput,
)
from schwab_marketdata_mcp.tools import _runtime as rt
from schwab_marketdata_mcp.tools import options
from schwab_marketdata_mcp.tools.options import (
    get_iv_surface_impl,
    get_option_greeks_summary_impl,
)
from tests.conftest import make_stateful_clickhouse_cache

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Chain payload builder — mirrors Schwab's callExpDateMap / putExpDateMap.
# ---------------------------------------------------------------------------


def _chain_payload(contracts_by_side: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Build a minimal Schwab option-chain response.

    ``contracts_by_side`` maps ``"CALL"``/``"PUT"`` to a list of contract
    dicts.  Each contract dict supports ``expiry`` (``YYYY-MM-DD``),
    ``strike``, and any Greek / ``openInterest`` keys, which are nested
    under the ``{expiry:dte}`` → ``{strike}`` → ``[contract]`` shape that
    :func:`flatten_option_chain_response` expects.
    """
    payload: dict[str, Any] = {"symbol": "TEST", "status": "SUCCESS"}
    for side, key in (("CALL", "callExpDateMap"), ("PUT", "putExpDateMap")):
        exp_map: dict[str, Any] = {}
        for c in contracts_by_side.get(side, []):
            exp = str(c["expiry"])
            strike = float(c["strike"])
            entry = {k: v for k, v in c.items() if k not in ("expiry", "strike")}
            entry.setdefault("putCall", side)
            exp_map.setdefault(f"{exp}:7", {}).setdefault(str(strike), []).append(entry)
        payload[key] = exp_map
    return payload


def _patch_chain(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> None:
    async def _fake_chain(_args: Any) -> dict[str, Any]:
        return payload

    monkeypatch.setattr(options, "get_option_chain_impl", _fake_chain)


# ===========================================================================
# E1 — Greeks summary: net-value correctness
# ===========================================================================


async def test_greeks_open_interest_weighting_net_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two calls with differing OI → OI-weighted net delta is the
    open-interest-weighted mean, not the simple mean."""
    payload = _chain_payload(
        {
            "CALL": [
                {"expiry": "2026-06-19", "strike": 100.0, "delta": 0.6, "gamma": 0.02, "openInterest": 300},
                {"expiry": "2026-06-19", "strike": 110.0, "delta": 0.2, "gamma": 0.04, "openInterest": 100},
            ]
        }
    )
    _patch_chain(monkeypatch, payload)
    args = GetOptionGreeksSummaryInput(underlying="AAPL", weighting="open_interest")
    out = await get_option_greeks_summary_impl(args)
    # OI-weighted delta = (0.6*300 + 0.2*100) / 400 = 0.5
    assert out["net"]["delta"] == pytest.approx(0.5)
    # OI-weighted gamma = (0.02*300 + 0.04*100) / 400 = 0.025
    assert out["net"]["gamma"] == pytest.approx(0.025)
    assert out["weighting"] == "open_interest"
    assert out["contract_count"] == 2
    assert out["warning"] is None
    # by_side / by_expiry mirror the same single-side aggregate.
    assert out["by_side"]["CALL"]["delta"] == pytest.approx(0.5)
    assert out["by_side"]["PUT"]["delta"] is None
    assert out["by_expiry"]["2026-06-19"]["delta"] == pytest.approx(0.5)


async def test_greeks_equal_weighting_net_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """``weighting='equal'`` → simple mean across contracts regardless of OI."""
    payload = _chain_payload(
        {
            "CALL": [
                {"expiry": "2026-06-19", "strike": 100.0, "delta": 0.6, "openInterest": 300},
                {"expiry": "2026-06-19", "strike": 110.0, "delta": 0.2, "openInterest": 100},
            ],
            "PUT": [
                {"expiry": "2026-06-19", "strike": 100.0, "delta": -0.4, "openInterest": 50},
            ],
        }
    )
    _patch_chain(monkeypatch, payload)
    args = GetOptionGreeksSummaryInput(underlying="AAPL", weighting="equal")
    out = await get_option_greeks_summary_impl(args)
    # equal-weight delta over all 3 contracts = (0.6 + 0.2 - 0.4) / 3
    assert out["net"]["delta"] == pytest.approx((0.6 + 0.2 - 0.4) / 3, abs=1e-6)
    assert out["by_side"]["CALL"]["delta"] == pytest.approx(0.4)
    assert out["by_side"]["PUT"]["delta"] == pytest.approx(-0.4)
    assert out["weighting"] == "equal"


# ===========================================================================
# E1 — Greeks summary: boundaries (empty / single / missing fields)
# ===========================================================================


async def test_greeks_empty_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty chain → contract_count 0 and every net Greek is None."""
    _patch_chain(monkeypatch, _chain_payload({}))
    out = await get_option_greeks_summary_impl(GetOptionGreeksSummaryInput(underlying="AAPL"))
    assert out["contract_count"] == 0
    assert out["net"] == {"delta": None, "gamma": None, "theta": None, "vega": None, "rho": None}
    assert out["by_side"]["CALL"]["delta"] is None
    assert out["by_expiry"] == {}


async def test_greeks_single_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """Single contract → net equals that contract's Greeks."""
    payload = _chain_payload(
        {"CALL": [{"expiry": "2026-06-19", "strike": 100.0, "delta": 0.55, "vega": 0.12, "openInterest": 10}]}
    )
    _patch_chain(monkeypatch, payload)
    out = await get_option_greeks_summary_impl(GetOptionGreeksSummaryInput(underlying="AAPL"))
    assert out["contract_count"] == 1
    assert out["net"]["delta"] == pytest.approx(0.55)
    assert out["net"]["vega"] == pytest.approx(0.12)
    # theta/gamma/rho were never reported → None.
    assert out["net"]["theta"] is None
    assert out["net"]["gamma"] is None
    assert out["net"]["rho"] is None


async def test_greeks_missing_oi_falls_back_to_equal(monkeypatch: pytest.MonkeyPatch) -> None:
    """OI-weighted request but NO contract carries open interest → the
    tool transparently equal-weights and emits the fallback warning."""
    payload = _chain_payload(
        {
            "CALL": [
                {"expiry": "2026-06-19", "strike": 100.0, "delta": 0.6},
                {"expiry": "2026-06-19", "strike": 110.0, "delta": 0.2},
            ]
        }
    )
    _patch_chain(monkeypatch, payload)
    out = await get_option_greeks_summary_impl(
        GetOptionGreeksSummaryInput(underlying="AAPL", weighting="open_interest")
    )
    # No OI anywhere → equal weighting → simple mean (0.4).
    assert out["net"]["delta"] == pytest.approx(0.4)
    assert out["weighting"] == "equal"
    assert out["requested_weighting"] == "open_interest"
    assert "open_interest_unavailable_equal_weighted" in (out["warning"] or [])


async def test_greeks_expiry_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    """``expiry`` restricts the aggregation to one expiration."""
    payload = _chain_payload(
        {
            "CALL": [
                {"expiry": "2026-06-19", "strike": 100.0, "delta": 0.6, "openInterest": 100},
                {"expiry": "2026-09-18", "strike": 100.0, "delta": 0.3, "openInterest": 100},
            ]
        }
    )
    _patch_chain(monkeypatch, payload)
    out = await get_option_greeks_summary_impl(
        GetOptionGreeksSummaryInput(underlying="AAPL", expiry=datetime(2026, 6, 19, tzinfo=UTC))
    )
    assert out["expiry_filter"] == "2026-06-19"
    assert out["contract_count"] == 1
    assert out["net"]["delta"] == pytest.approx(0.6)
    assert list(out["by_expiry"].keys()) == ["2026-06-19"]


async def test_greeks_non_finite_greek_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """NaN / non-numeric Greeks are dropped from the aggregation."""
    payload = _chain_payload(
        {
            "CALL": [
                {"expiry": "2026-06-19", "strike": 100.0, "delta": float("nan"), "openInterest": 100},
                {"expiry": "2026-06-19", "strike": 110.0, "delta": "not-a-number", "openInterest": 100},
            ]
        }
    )
    _patch_chain(monkeypatch, payload)
    out = await get_option_greeks_summary_impl(GetOptionGreeksSummaryInput(underlying="AAPL"))
    # Both deltas unusable → net delta None even though contracts exist.
    assert out["contract_count"] == 2
    assert out["net"]["delta"] is None


async def test_greeks_oi_weighted_bucket_all_zero_oi_uses_mean(monkeypatch: pytest.MonkeyPatch) -> None:
    """A mixed chain where one expiry has OI and another has all-zero OI:
    the global has_oi flag stays True (so OI weighting is kept), but the
    zero-OI bucket degrades to a simple mean instead of dividing by zero."""
    payload = _chain_payload(
        {
            "CALL": [
                {"expiry": "2026-06-19", "strike": 100.0, "delta": 0.6, "openInterest": 100},
                {"expiry": "2026-09-18", "strike": 100.0, "delta": 0.2, "openInterest": 0},
                {"expiry": "2026-09-18", "strike": 110.0, "delta": 0.4, "openInterest": 0},
            ]
        }
    )
    _patch_chain(monkeypatch, payload)
    out = await get_option_greeks_summary_impl(
        GetOptionGreeksSummaryInput(underlying="AAPL", weighting="open_interest")
    )
    assert out["weighting"] == "open_interest"  # global OI present → kept
    # Sep bucket has zero total weight → simple mean (0.3).
    assert out["by_expiry"]["2026-09-18"]["delta"] == pytest.approx(0.3)
    assert out["by_expiry"]["2026-06-19"]["delta"] == pytest.approx(0.6)


async def test_greeks_live_chain_via_fixtures_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Integration: drive the *real* get_option_chain flow through the
    in-process FakeSchwabClient (fixtures backend) — no real API, no
    monkeypatched chain.  The fixture chain has a single delta-only CALL
    so net delta is that value and other Greeks are None."""
    monkeypatch.setenv("SCHWAB_MOCK_BACKEND", "fixtures")
    monkeypatch.setenv(
        "SCHWAB_MOCK_FIXTURES_DIR", str(__import__("pathlib").Path(__file__).resolve().parent / "fixtures")
    )
    monkeypatch.setenv("SCHWAB_MOCK_SCENARIO", "normal")
    monkeypatch.setenv("SCHWAB_MAX_RETRIES", "0")
    rt.reset_client_cache()
    try:
        out = await get_option_greeks_summary_impl(GetOptionGreeksSummaryInput(underlying="AAPL"))
    finally:
        rt.reset_client_cache()
    assert out["underlying"] == "AAPL"
    assert out["contract_count"] >= 1
    # The fixture only carries delta → net delta is set, others None.
    assert out["net"]["delta"] is not None
    assert out["net"]["gamma"] is None


async def test_greeks_unparseable_expiry_dropped_from_by_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    """A contract whose expiry key does not parse to a date still counts
    toward net/by_side but is excluded from by_expiry (no date bucket)."""
    payload = _chain_payload({"CALL": [{"expiry": "not-a-date", "strike": 100.0, "delta": 0.5, "openInterest": 100}]})
    _patch_chain(monkeypatch, payload)
    out = await get_option_greeks_summary_impl(GetOptionGreeksSummaryInput(underlying="AAPL"))
    assert out["contract_count"] == 1
    assert out["net"]["delta"] == pytest.approx(0.5)
    # Unparseable expiry → not present in by_expiry.
    assert out["by_expiry"] == {}


# ===========================================================================
# E6 — IV surface: with ClickHouse history
# ===========================================================================


def _seed_iv(cache: Cache, underlying: str, bucket: str, rows: list[tuple[date, float, int]]) -> None:
    for asof, iv, n in rows:
        cache._upsert_iv_history(underlying, asof, bucket, iv, n)


async def test_iv_surface_with_clickhouse_history(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stateful CH fake → all three buckets populate and total_sample_count
    is the sum across buckets."""
    monkeypatch.setenv("SCHWAB_CACHE_ENABLED", "1")
    cache = make_stateful_clickhouse_cache()
    monkeypatch.setattr(options, "get_cache", lambda: cache)

    base = date(2026, 5, 25)
    for bucket in ("30d", "60d", "90d"):
        rows = [(base - timedelta(days=40 - i), 0.10 + 0.001 * i, 50) for i in range(40)]
        _seed_iv(cache, "AAPL", bucket, rows)

    out = await get_iv_surface_impl(GetIvSurfaceInput(underlying="AAPL", lookback_days=252))
    assert set(out["buckets"].keys()) == {"30d", "60d", "90d"}
    for bucket in ("30d", "60d", "90d"):
        b = out["buckets"][bucket]
        assert b["sample_count"] == 40
        assert b["percentile_rank"] is not None
        assert b["current_iv"] is not None
    assert out["total_sample_count"] == 120
    assert out["warning"] is None
    assert out["lookback_days"] == 252


async def test_iv_surface_partial_history(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the 30d bucket has rows → that bucket populates, the others
    stay empty, and total_sample_count reflects just the 30d rows."""
    monkeypatch.setenv("SCHWAB_CACHE_ENABLED", "1")
    cache = make_stateful_clickhouse_cache()
    monkeypatch.setattr(options, "get_cache", lambda: cache)
    base = date(2026, 5, 25)
    _seed_iv(cache, "AAPL", "30d", [(base - timedelta(days=i), 0.20 + 0.001 * i, 10) for i in range(5)])

    out = await get_iv_surface_impl(GetIvSurfaceInput(underlying="AAPL"))
    assert out["buckets"]["30d"]["sample_count"] == 5
    assert out["buckets"]["60d"]["sample_count"] == 0
    assert out["buckets"]["90d"]["sample_count"] == 0
    assert out["total_sample_count"] == 5
    # total > 0 → no persistence-requirement warning.
    assert out["warning"] is None


# ===========================================================================
# E6 — IV surface: degraded (no persistence)
# ===========================================================================


async def test_iv_surface_cache_disabled_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    """``SCHWAB_CACHE_ENABLED=0`` → empty buckets, both cache_disabled and
    requires_clickhouse_persistence warnings, never raises."""
    monkeypatch.setenv("SCHWAB_CACHE_ENABLED", "0")
    cache_mod.reset_cache_singleton()
    out = await get_iv_surface_impl(GetIvSurfaceInput(underlying="AAPL"))
    assert out["total_sample_count"] == 0
    assert set(out["buckets"].keys()) == {"30d", "60d", "90d"}
    for bucket in ("30d", "60d", "90d"):
        assert out["buckets"][bucket]["sample_count"] == 0
        assert out["buckets"][bucket]["expiry_bucket"] == bucket
    assert "cache_disabled" in (out["warning"] or [])
    assert "requires_clickhouse_persistence" in (out["warning"] or [])


async def test_iv_surface_memory_backend_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    """Memory backend keeps no durable history → empty surface flagged
    requires_clickhouse_persistence (but not cache_disabled)."""
    monkeypatch.setenv("SCHWAB_CACHE_ENABLED", "1")
    cache = Cache(backend=MemoryBackend())
    monkeypatch.setattr(options, "get_cache", lambda: cache)
    out = await get_iv_surface_impl(GetIvSurfaceInput(underlying="AAPL"))
    assert out["total_sample_count"] == 0
    assert "requires_clickhouse_persistence" in (out["warning"] or [])
    assert "cache_disabled" not in (out["warning"] or [])


async def test_cache_get_iv_surface_invalid_underlying_raises() -> None:
    """``Cache.get_iv_surface`` rejects an empty underlying."""
    cache = Cache(backend=MemoryBackend())
    with pytest.raises(ValueError):
        cache.get_iv_surface("")
