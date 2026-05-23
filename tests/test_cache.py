"""``cache.py`` unit tests — schema + TTL + concurrent + corruption.

Plan v0.2 sprint task #2.  Coverage target: ≥10 tests across hit /
miss / expire / put / OLAP / corrupt / stats / disabled / hash.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from schwab_marketdata_mcp import cache

# ---------------------------------------------------------------------------
# Hit / miss / TTL — quotes_cache
# ---------------------------------------------------------------------------


def _quote_payload(symbol: str = "AAPL", *, last: float = 100.0) -> dict[str, object]:
    return {
        symbol: {
            "quote": {
                "bidPrice": last - 0.05,
                "askPrice": last + 0.05,
                "lastPrice": last,
                "totalVolume": 1_000_000,
                "openPrice": last - 0.5,
                "highPrice": last + 1.0,
                "lowPrice": last - 1.0,
                "closePrice": last - 0.1,
            }
        }
    }


def test_get_quote_returns_none_on_empty_cache(tmp_path: Path) -> None:
    db = tmp_path / "c.duckdb"
    with cache.Cache(db) as c:
        assert c.get_quote("AAPL") is None


def test_put_then_get_quote_round_trip(tmp_path: Path) -> None:
    db = tmp_path / "c.duckdb"
    payload = _quote_payload("AAPL", last=170.5)
    with cache.Cache(db) as c:
        c.put_quote("AAPL", payload)
        got = c.get_quote("AAPL")
    assert got is not None
    assert got["AAPL"]["quote"]["lastPrice"] == 170.5


def test_quote_expires_after_ttl(tmp_path: Path) -> None:
    db = tmp_path / "c.duckdb"
    with cache.Cache(db) as c:
        c.put_quote("AAPL", _quote_payload("AAPL"), ttl_seconds=0)
        # ttl_seconds=0 → any non-zero age expires
        time.sleep(0.05)
        assert c.get_quote("AAPL") is None


def test_quote_replace_overwrites_existing(tmp_path: Path) -> None:
    db = tmp_path / "c.duckdb"
    with cache.Cache(db) as c:
        c.put_quote("AAPL", _quote_payload("AAPL", last=100.0))
        c.put_quote("AAPL", _quote_payload("AAPL", last=200.0))
        got = c.get_quote("AAPL")
    assert got is not None and got["AAPL"]["quote"]["lastPrice"] == 200.0


# ---------------------------------------------------------------------------
# price_history_cache — OLAP-style upsert + window query
# ---------------------------------------------------------------------------


def _candle(ts: datetime, *, close: float = 1.0) -> dict[str, object]:
    return {
        "datetime": int(ts.replace(tzinfo=UTC).timestamp() * 1000),
        "open": close - 0.1,
        "high": close + 0.2,
        "low": close - 0.3,
        "close": close,
        "volume": 1234,
    }


def _ph_params() -> dict[str, object]:
    return {
        "symbol": "VOO",
        "period_type": "DAY",
        "period": "TEN_DAYS",
        "frequency_type": "MINUTE",
        "frequency": 30,
    }


def test_price_history_put_and_get_returns_candles(tmp_path: Path) -> None:
    db = tmp_path / "c.duckdb"
    base = datetime.now(tz=UTC).replace(microsecond=0) - timedelta(days=2)
    raw = {"candles": [_candle(base + timedelta(minutes=30 * i)) for i in range(5)]}
    with cache.Cache(db) as c:
        c.put_price_history(_ph_params(), raw)
        got = c.get_price_history(_ph_params())
    assert got is not None
    assert len(got["candles"]) == 5
    assert got["_cache_source"] == "duckdb"


def test_price_history_recent_candle_forces_refresh(tmp_path: Path) -> None:
    """Candles inside the 1 h recent window with stale fetched_at → miss."""
    db = tmp_path / "c.duckdb"
    now = datetime.now(tz=UTC).replace(microsecond=0)
    raw = {"candles": [_candle(now - timedelta(minutes=5))]}
    with cache.Cache(db) as c:
        c.put_price_history(_ph_params(), raw)
        # Manually backdate fetched_at to simulate stale recent candle.
        assert c._conn is not None
        c._conn.execute(
            "UPDATE price_history_cache SET fetched_at = ?",
            [datetime.now(tz=UTC).replace(tzinfo=None) - timedelta(minutes=5)],
        )
        got = c.get_price_history(_ph_params())
    assert got is None


def test_price_history_query_candles_window(tmp_path: Path) -> None:
    db = tmp_path / "c.duckdb"
    base = datetime.now(tz=UTC).replace(microsecond=0) - timedelta(days=10)
    raw = {"candles": [_candle(base + timedelta(days=i), close=1.0 + i) for i in range(10)]}
    with cache.Cache(db) as c:
        c.put_price_history(_ph_params(), raw)
        rows = c.query_candles("VOO", base + timedelta(days=2), base + timedelta(days=5))
    assert len(rows) == 4
    assert all(r["period_type"] == "DAY" for r in rows)


# ---------------------------------------------------------------------------
# option_chain_cache — hashed key
# ---------------------------------------------------------------------------


def test_option_chain_put_and_get_round_trip(tmp_path: Path) -> None:
    db = tmp_path / "c.duckdb"
    params = {"symbol": "AAPL", "contract_type": "CALL", "strike_count": 5}
    raw = {"underlyingSymbol": "AAPL", "callExpDateMap": {}}
    with cache.Cache(db) as c:
        c.put_option_chain(params, raw)
        got = c.get_option_chain(params)
        # Different params → different hash → miss.
        miss = c.get_option_chain({**params, "contract_type": "PUT"})
    assert got == raw
    assert miss is None


def test_option_chain_expires(tmp_path: Path) -> None:
    db = tmp_path / "c.duckdb"
    with cache.Cache(db) as c:
        c.put_option_chain({"symbol": "AAPL"}, {"x": 1}, ttl_seconds=0)
        time.sleep(0.05)
        assert c.get_option_chain({"symbol": "AAPL"}) is None


# ---------------------------------------------------------------------------
# instruments_cache
# ---------------------------------------------------------------------------


def test_instruments_round_trip(tmp_path: Path) -> None:
    db = tmp_path / "c.duckdb"
    params = {"symbols": ["AAPL", "MSFT"], "projection": "FUNDAMENTAL"}
    raw = {"instruments": [{"symbol": "AAPL"}]}
    with cache.Cache(db) as c:
        c.put_instruments(params, raw)
        got = c.get_instruments(params)
    assert got == raw


# ---------------------------------------------------------------------------
# Stats / truncate / disabled / corrupt
# ---------------------------------------------------------------------------


def test_stats_reflects_writes_and_events(tmp_path: Path) -> None:
    db = tmp_path / "c.duckdb"
    with cache.Cache(db) as c:
        c.put_quote("AAPL", _quote_payload("AAPL"))
        c.get_quote("AAPL")  # hit
        c.get_quote("MSFT")  # miss
        stats = c.get_stats()
    d = stats.to_dict()
    assert d["rows_per_table"]["quotes_cache"] == 1
    assert d["hits_24h"] >= 1
    assert d["misses_24h"] >= 1
    assert d["enabled"] is True
    assert d["size_mb"] > 0


def test_truncate_expired_removes_rows(tmp_path: Path) -> None:
    db = tmp_path / "c.duckdb"
    with cache.Cache(db) as c:
        c.put_quote("AAPL", _quote_payload("AAPL"), ttl_seconds=0)
        c.put_option_chain({"symbol": "AAPL"}, {"x": 1}, ttl_seconds=0)
        time.sleep(0.05)
        n = c.truncate_expired()
        rows = c.get_stats().rows_per_table
    assert n >= 0  # DuckDB DELETE returns row count via fetchone
    assert rows["quotes_cache"] == 0
    assert rows["option_chain_cache"] == 0


def test_disabled_singleton_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(cache.ENV_CACHE_ENABLED, "false")
    cache.reset_cache_singleton()
    assert cache.get_cache() is None
    assert cache.cache_enabled() is False


def test_bypass_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(cache.ENV_CACHE_BYPASS, "1")
    assert cache.cache_bypass() is True
    monkeypatch.delenv(cache.ENV_CACHE_BYPASS, raising=False)
    assert cache.cache_bypass() is False


def test_concurrent_writes_do_not_lose_rows(tmp_path: Path) -> None:
    db = tmp_path / "c.duckdb"
    c = cache.Cache(db)
    try:
        N_WRITERS = 4
        WRITES_PER = 25
        errors: list[BaseException] = []

        def writer(prefix: str) -> None:
            try:
                for i in range(WRITES_PER):
                    c.put_quote(f"{prefix}{i:03d}"[:10], _quote_payload(f"{prefix}{i:03d}"[:10]))
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(f"S{n}",)) for n in range(N_WRITERS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        assert not errors, f"writer errors: {errors!r}"
        rows = c.get_stats().rows_per_table["quotes_cache"]
        assert rows == N_WRITERS * WRITES_PER
    finally:
        c.close()


def test_corrupt_db_is_quarantined_and_reopened(tmp_path: Path) -> None:
    db = tmp_path / "c.duckdb"
    db.write_bytes(b"not a duckdb file at all")
    c = cache.Cache(db)
    try:
        # Reopen path should have produced a working DB.
        c.put_quote("AAPL", _quote_payload("AAPL"))
        got = c.get_quote("AAPL")
    finally:
        c.close()
    assert got is not None
    backups = list(tmp_path.glob("c.duckdb.corrupt-*"))
    assert backups, "expected a quarantined backup file"


def test_default_db_path_under_xdg_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "alt-state"
    state.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    p = cache.default_db_path()
    assert str(p).startswith(str(state))
    assert p.name == cache.CACHE_DB_FILENAME


def test_cache_singleton_is_lazy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state = tmp_path / "alt-state"
    state.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv(cache.ENV_CACHE_ENABLED, "true")
    cache.reset_cache_singleton()
    a = cache.get_cache()
    b = cache.get_cache()
    assert a is b
    cache.reset_cache_singleton()


def test_get_quote_unknown_symbol_records_miss(tmp_path: Path) -> None:
    db = tmp_path / "c.duckdb"
    with cache.Cache(db) as c:
        c.get_quote("NOPE")
        c.get_quote("NOPE")
        stats = c.get_stats()
    assert stats.misses_24h >= 2


def test_price_history_invalid_params_returns_none(tmp_path: Path) -> None:
    db = tmp_path / "c.duckdb"
    with cache.Cache(db) as c:
        # Missing required keys
        assert c.get_price_history({"symbol": "VOO"}) is None
        c.put_price_history({"symbol": "VOO"}, {"candles": []})
    # No write should have happened.
    with cache.Cache(db) as c:
        rows = c.get_stats().rows_per_table["price_history_cache"]
    assert rows == 0


@pytest.mark.posix_only
def test_db_file_has_secure_perms(tmp_path: Path) -> None:
    import stat

    db = tmp_path / "c.duckdb"
    with cache.Cache(db):
        pass
    mode = stat.S_IMODE(db.stat().st_mode)
    assert mode == 0o600


def test_serialise_round_trip_with_default_str() -> None:
    """Ensure datetime / Enum-ish values don't break the JSON encoder."""
    from datetime import UTC, datetime

    payload = {"AAPL": {"now": datetime(2026, 1, 1, tzinfo=UTC)}}
    s = json.dumps(payload, default=str)
    assert "2026-01-01" in s


# ---------------------------------------------------------------------------
# Integration smoke — call_endpoint wires cache hit/miss into payload
# ---------------------------------------------------------------------------


async def test_call_endpoint_records_cache_status_on_miss(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    use_fake_backend: None,
) -> None:
    """First call → miss + write; second call → hit (no API)."""
    del use_fake_backend  # consumed via fixture
    from schwab_marketdata_mcp import server

    out1 = await server.get_quote(symbol="AAPL")
    assert out1.get("_cache_status") == "miss"
    out2 = await server.get_quote(symbol="AAPL")
    assert out2.get("_cache_status") == "hit"


async def test_call_endpoint_bypass_skips_cache(
    monkeypatch: pytest.MonkeyPatch,
    use_fake_backend: None,
) -> None:
    del use_fake_backend
    from schwab_marketdata_mcp import server

    monkeypatch.setenv("SCHWAB_CACHE_BYPASS", "1")
    out = await server.get_quote(symbol="AAPL")
    assert out.get("_cache_status") == "bypass"


async def test_call_endpoint_disabled_skips_cache(
    monkeypatch: pytest.MonkeyPatch,
    use_fake_backend: None,
) -> None:
    del use_fake_backend
    from schwab_marketdata_mcp import server

    monkeypatch.setenv("SCHWAB_CACHE_ENABLED", "false")
    out = await server.get_quote(symbol="AAPL")
    assert out.get("_cache_status") == "disabled"


async def test_get_cache_stats_tool(monkeypatch: pytest.MonkeyPatch, use_fake_backend: None) -> None:
    del use_fake_backend
    from schwab_marketdata_mcp import server

    out = await server.get_cache_stats()
    assert out["enabled"] is True
    assert "rows_per_table" in out
    assert "size_mb" in out


async def test_health_check_includes_cache_fields(use_fake_backend: None) -> None:
    del use_fake_backend
    from schwab_marketdata_mcp import server

    out = await server.health_check()
    assert "cache_enabled" in out
    assert "cache_size_mb" in out
    assert "cache_hit_rate_24h" in out
