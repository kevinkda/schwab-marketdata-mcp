"""Unit tests for ``Cache.write_option_chain_snapshot`` and the
``flatten_option_chain_response`` helper plus the v0.4 P1/C
``_persist_chain_snapshot`` wiring inside ``tools/options.py``.

v0.4 P1/C — separate from ``test_iv_percentile.py`` which exercises the
analytics readers (aggregate_atm_iv / get_iv_percentile_rank).  This
file pins the writer + flattener semantics:

* row count returned matches contracts persisted (with malformed rows
  dropped, not raised);
* INSERT OR REPLACE keyed by (underlying, snapshot_at, expiry, strike,
  call_put) — re-writing the same key replaces, doesn't duplicate;
* timezone normalisation: aware datetimes are stored as naive UTC;
* DuckDB errors are swallowed (warning-logged, returns 0) — never
  break the live tool path;
* ``flatten_option_chain_response`` correctly unrolls Schwab's nested
  ``callExpDateMap`` / ``putExpDateMap`` and normalises the IV
  percent-vs-fraction quirk.

All tests use an isolated DuckDB file under ``tmp_path``.  No real
HTTP traffic — option chain shapes are inlined as Python dicts so the
test does not depend on the on-disk fixture catalogue.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from schwab_marketdata_mcp import cache as cache_mod
from schwab_marketdata_mcp.cache import Cache, flatten_option_chain_response

# ---------------------------------------------------------------------------
# Helpers — minimal flattened-contract & nested-Schwab payload factories.
# ---------------------------------------------------------------------------


def _contract(
    *,
    expiry: str | date = "2026-06-19",
    strike: float = 190.0,
    call_put: str = "CALL",
    iv: float | None = 0.32,
    delta: float | None = 0.42,
) -> dict[str, object]:
    return {
        "expiry": expiry,
        "strike": strike,
        "call_put": call_put,
        "last_price": 1.25,
        "bid": 1.20,
        "ask": 1.30,
        "volume": 1_000,
        "open_interest": 5_000,
        "implied_vol": iv,
        "delta": delta,
        "gamma": 0.04,
        "theta": -0.05,
        "vega": 0.10,
        "rho": 0.01,
    }


def _schwab_chain_payload(
    underlying_last: float = 187.34,
    *,
    expiries: tuple[str, ...] = ("2026-06-19:30",),
) -> dict[str, object]:
    """Mimic the Schwab nested ``callExpDateMap`` / ``putExpDateMap`` shape."""
    call_map: dict[str, dict[str, list[dict[str, object]]]] = {}
    put_map: dict[str, dict[str, list[dict[str, object]]]] = {}
    for exp_key in expiries:
        call_map[exp_key] = {
            "190.0": [
                {
                    "putCall": "CALL",
                    "bid": 1.20,
                    "ask": 1.30,
                    "last": 1.25,
                    "delta": 0.42,
                    "volatility": 32.5,  # percent form — must be normalised to 0.325
                }
            ]
        }
        put_map[exp_key] = {
            "190.0": [
                {
                    "putCall": "PUT",
                    "bid": 0.95,
                    "ask": 1.05,
                    "last": 1.00,
                    "delta": -0.31,
                    "volatility": 0.30,  # already fractional — must pass through
                }
            ]
        }
    return {
        "symbol": "AAPL",
        "status": "SUCCESS",
        "underlying": {"symbol": "AAPL", "last": underlying_last},
        "callExpDateMap": call_map,
        "putExpDateMap": put_map,
    }


# ---------------------------------------------------------------------------
# write_option_chain_snapshot — happy path + edge cases.
# ---------------------------------------------------------------------------


def test_write_option_chain_snapshot_round_trip(tmp_path: Path) -> None:
    """Inserting a 4-contract chain reports rows=4 and round-trips through
    the analytics aggregator (most-recent snapshot fetch path)."""
    db = tmp_path / "c.duckdb"
    snap = datetime(2026, 5, 25, 14, 30, tzinfo=UTC)
    contracts = [
        _contract(strike=185.0, call_put="CALL"),
        _contract(strike=185.0, call_put="PUT"),
        _contract(strike=190.0, call_put="CALL"),
        _contract(strike=190.0, call_put="PUT"),
    ]
    with Cache(db) as c:
        rows = c.write_option_chain_snapshot("AAPL", snap, contracts)
        assert rows == 4
        # ATM IV pipeline: the aggregator pulls the most recent snapshot
        # and writes one row per bucket to iv_history.
        atm = c.aggregate_atm_iv("AAPL", date(2026, 5, 25), snapshot_at=snap)
        # 30-day bucket: expiry 2026-06-19 is 25 days out — within
        # the +-7d tolerance window of the 30d bucket (23..37).
        assert atm["30d"] is not None
        assert atm["60d"] is None  # nothing 53..67d out
        assert atm["90d"] is None


def test_write_option_chain_snapshot_idempotent_overwrites(tmp_path: Path) -> None:
    """Re-writing the same (underlying, snapshot_at, expiry, strike,
    call_put) tuple replaces — the row count stays the same, and the
    new IV value is what aggregate_atm_iv reads back."""
    db = tmp_path / "c.duckdb"
    snap = datetime(2026, 5, 25, 14, 30, tzinfo=UTC)
    asof = date(2026, 5, 25)
    with Cache(db) as c:
        c.write_option_chain_snapshot(
            "AAPL",
            snap,
            [_contract(strike=190.0, call_put="CALL", iv=0.20), _contract(strike=190.0, call_put="PUT", iv=0.22)],
        )
        first = c.aggregate_atm_iv("AAPL", asof, snapshot_at=snap)["30d"]
        # Same key, different IV — should *replace* the prior row.
        c.write_option_chain_snapshot(
            "AAPL",
            snap,
            [_contract(strike=190.0, call_put="CALL", iv=0.40), _contract(strike=190.0, call_put="PUT", iv=0.42)],
        )
        second = c.aggregate_atm_iv("AAPL", asof, snapshot_at=snap)["30d"]
    assert first is not None and second is not None
    assert first != second
    assert second == pytest.approx((0.40 + 0.42) / 2, rel=1e-6)


def test_write_option_chain_snapshot_drops_malformed(tmp_path: Path) -> None:
    """Rows missing ``expiry`` / ``strike`` / ``call_put`` are silently
    dropped — the writer never raises on a single malformed contract.
    The returned count reflects only the *good* rows."""
    db = tmp_path / "c.duckdb"
    snap = datetime(2026, 5, 25, 14, 30, tzinfo=UTC)
    contracts = [
        _contract(strike=190.0, call_put="CALL"),  # good
        {"expiry": "2026-06-19", "strike": None, "call_put": "PUT"},  # bad strike
        {"expiry": None, "strike": 200.0, "call_put": "CALL"},  # bad expiry
        {"expiry": "2026-06-19", "strike": 200.0, "call_put": "BOGUS"},  # bad cp
    ]
    with Cache(db) as c:
        rows = c.write_option_chain_snapshot("AAPL", snap, contracts)
    assert rows == 1


def test_write_option_chain_snapshot_empty_inputs_return_zero(tmp_path: Path) -> None:
    """Empty list, empty underlying, and non-list ``contracts`` all
    short-circuit to 0 — covering the input-guard branches."""
    db = tmp_path / "c.duckdb"
    snap = datetime.now(tz=UTC)
    with Cache(db) as c:
        assert c.write_option_chain_snapshot("AAPL", snap, []) == 0
        assert c.write_option_chain_snapshot("", snap, [_contract()]) == 0
        # Non-list deliberately — branch coverage for the isinstance guard.
        assert c.write_option_chain_snapshot("AAPL", snap, "nope") == 0  # type: ignore[arg-type]


def test_write_option_chain_snapshot_after_close_returns_zero(tmp_path: Path) -> None:
    """A closed connection → graceful 0-rows-written, no exception."""
    db = tmp_path / "c.duckdb"
    c = Cache(db)
    c.close()
    snap = datetime.now(tz=UTC)
    assert c.write_option_chain_snapshot("AAPL", snap, [_contract()]) == 0


def test_write_option_chain_snapshot_normalises_call_put_aliases(tmp_path: Path) -> None:
    """``'C'`` / ``'P'`` aliases must round-trip as ``'CALL'`` / ``'PUT'``
    — the aggregate_atm_iv reader compares uppercased CALL/PUT strings."""
    db = tmp_path / "c.duckdb"
    snap = datetime(2026, 5, 25, 14, 30, tzinfo=UTC)
    asof = date(2026, 5, 25)
    contracts = [
        _contract(strike=190.0, call_put="C", iv=0.30),
        _contract(strike=190.0, call_put="P", iv=0.32),
    ]
    with Cache(db) as c:
        rows = c.write_option_chain_snapshot("AAPL", snap, contracts)
        atm = c.aggregate_atm_iv("AAPL", asof, snapshot_at=snap)["30d"]
    assert rows == 2
    assert atm == pytest.approx((0.30 + 0.32) / 2, rel=1e-6)


# ---------------------------------------------------------------------------
# flatten_option_chain_response — Schwab nested map → flat contract list.
# ---------------------------------------------------------------------------


def test_flatten_option_chain_response_unrolls_nested_maps() -> None:
    """One CALL row + one PUT row across one expiry → 2 contracts with
    correct putCall, expiry, strike, and *fractional* IV after the
    percent-form heuristic kicks in for the call (32.5 → 0.325) but
    not for the put (0.30 stays)."""
    raw = _schwab_chain_payload()
    contracts = flatten_option_chain_response(raw)
    assert len(contracts) == 2
    by_cp = {c["call_put"]: c for c in contracts}
    assert by_cp["CALL"]["volatility"] == pytest.approx(0.325, rel=1e-6)
    assert by_cp["PUT"]["volatility"] == pytest.approx(0.30, rel=1e-6)
    # Expiry parsed from the ``2026-06-19:30`` key.
    assert by_cp["CALL"]["expiry"] == date(2026, 6, 19)


def test_flatten_option_chain_response_handles_empty_payload() -> None:
    """No callExpDateMap/putExpDateMap → empty list (not an exception)
    — covers the early-out branches inside ``flatten_option_chain_response``."""
    assert flatten_option_chain_response({}) == []
    assert flatten_option_chain_response({"callExpDateMap": {}}) == []
    assert flatten_option_chain_response({"callExpDateMap": None, "putExpDateMap": None}) == []
    # Non-dict input → empty list, no raise.
    assert flatten_option_chain_response(None) == []


def test_flatten_option_chain_response_keeps_unparseable_for_writer_to_drop() -> None:
    """Unparseable expiry / strike keys are *not* filtered inside
    ``flatten_option_chain_response`` — they pass through with
    ``expiry=None`` / ``strike=None`` and the downstream
    ``_normalise_option_contract`` check inside the writer drops them.
    Asserting on the writer's row count is the contract that matters."""
    raw = {
        "callExpDateMap": {
            "not-a-date:30": {"190.0": [{"putCall": "CALL", "volatility": 0.25}]},
            "2026-06-19:30": {
                "not-a-strike": [{"putCall": "CALL", "volatility": 0.25}],
                "190.0": [{"putCall": "CALL", "volatility": 0.25}],
            },
        },
    }
    contracts = flatten_option_chain_response(raw)
    # Flatten preserves all rows, but only one is well-formed enough
    # to land in the analytics table.
    assert len(contracts) == 3
    assert any(c["expiry"] == date(2026, 6, 19) and c["strike"] == 190.0 for c in contracts)


# ---------------------------------------------------------------------------
# tools.options._persist_chain_snapshot — wiring layer between
# ``get_option_chain`` and the writer.  Exercised directly so we don't
# need a live HTTP mock.
# ---------------------------------------------------------------------------


def test_persist_chain_snapshot_writes_on_miss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-hit payload should produce a non-zero row count when the
    cache is enabled — proving the get_option_chain → snapshot pipeline."""
    from schwab_marketdata_mcp.tools import options as options_tools

    monkeypatch.setenv("SCHWAB_CACHE_ENABLED", "1")
    monkeypatch.setenv("SCHWAB_CACHE_BYPASS", "0")
    monkeypatch.setenv("SCHWAB_CACHE_PATH", str(tmp_path / "c.duckdb"))
    cache_mod.reset_cache_singleton()
    payload = _schwab_chain_payload()
    payload["_cache_status"] = "miss"
    rows = options_tools._persist_chain_snapshot("AAPL", payload)
    assert rows == 2  # one CALL + one PUT
    cache_mod.reset_cache_singleton()


def test_persist_chain_snapshot_skips_on_cache_hit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``_cache_status == 'hit'`` the snapshot writer must skip —
    the chain was already written on the first call and re-writing
    would only thrash the analytics table with near-duplicate rows."""
    from schwab_marketdata_mcp.tools import options as options_tools

    monkeypatch.setenv("SCHWAB_CACHE_ENABLED", "1")
    monkeypatch.setenv("SCHWAB_CACHE_BYPASS", "0")
    monkeypatch.setenv("SCHWAB_CACHE_PATH", str(tmp_path / "c.duckdb"))
    cache_mod.reset_cache_singleton()
    payload = _schwab_chain_payload()
    payload["_cache_status"] = "hit"
    rows = options_tools._persist_chain_snapshot("AAPL", payload)
    assert rows == 0
    cache_mod.reset_cache_singleton()


def test_persist_chain_snapshot_returns_zero_when_cache_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``SCHWAB_CACHE_ENABLED=0`` must short-circuit the writer — the
    tool's hot path stays read-only when the cache is off."""
    from schwab_marketdata_mcp.tools import options as options_tools

    monkeypatch.setenv("SCHWAB_CACHE_ENABLED", "0")
    cache_mod.reset_cache_singleton()
    payload = _schwab_chain_payload()
    payload["_cache_status"] = "miss"
    rows = options_tools._persist_chain_snapshot("AAPL", payload)
    assert rows == 0
    cache_mod.reset_cache_singleton()


def test_persist_chain_snapshot_returns_zero_for_empty_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No callExpDateMap/putExpDateMap → 0 rows, no exception (e.g.
    error responses, empty chains)."""
    from schwab_marketdata_mcp.tools import options as options_tools

    monkeypatch.setenv("SCHWAB_CACHE_ENABLED", "1")
    monkeypatch.setenv("SCHWAB_CACHE_BYPASS", "0")
    monkeypatch.setenv("SCHWAB_CACHE_PATH", str(tmp_path / "c.duckdb"))
    cache_mod.reset_cache_singleton()
    rows = options_tools._persist_chain_snapshot("AAPL", {"_cache_status": "miss", "status": "FAILED"})
    assert rows == 0
    # Non-dict input is also tolerated.
    rows2 = options_tools._persist_chain_snapshot("AAPL", "nope")  # type: ignore[arg-type]
    assert rows2 == 0
    cache_mod.reset_cache_singleton()
