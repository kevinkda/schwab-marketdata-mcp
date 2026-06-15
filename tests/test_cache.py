"""``cache.py`` unit tests — pluggable backend (v0.7 T0).

.. versionchanged:: 0.5.0
    DuckDB removed; the cache delegates to a pluggable ``CacheBackend``
    (memory default).  Response cache (quotes / price_history / option_chain
    / instruments) works in-process on the memory backend; derived-analysis
    history (snapshots / iv_history / candle OLAP) is durable only on the
    ClickHouse backend and degrades gracefully on memory.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from schwab_marketdata_mcp import cache
from schwab_marketdata_mcp.cache_backend import MemoryBackend
from tests.conftest import (
    clickhouse_inserted_rows,
    make_clickhouse_cache,
    seed_clickhouse_query_rows,
)


def _mem_cache() -> cache.Cache:
    return cache.Cache(backend=MemoryBackend())


# ---------------------------------------------------------------------------
# Hit / miss / TTL — quotes (response cache, memory backend)
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


def test_get_quote_returns_none_on_empty_cache() -> None:
    with _mem_cache() as c:
        assert c.get_quote("AAPL") is None


def test_put_then_get_quote_round_trip() -> None:
    payload = _quote_payload("AAPL", last=170.5)
    with _mem_cache() as c:
        c.put_quote("AAPL", payload)
        got = c.get_quote("AAPL")
    assert got is not None
    assert got["AAPL"]["quote"]["lastPrice"] == 170.5


def test_quote_expires_after_ttl() -> None:
    with _mem_cache() as c:
        c.put_quote("AAPL", _quote_payload("AAPL"), ttl_seconds=0)
        time.sleep(0.05)
        assert c.get_quote("AAPL") is None


def test_quote_replace_overwrites_existing() -> None:
    with _mem_cache() as c:
        c.put_quote("AAPL", _quote_payload("AAPL", last=100.0))
        c.put_quote("AAPL", _quote_payload("AAPL", last=200.0))
        got = c.get_quote("AAPL")
    assert got is not None and got["AAPL"]["quote"]["lastPrice"] == 200.0


# ---------------------------------------------------------------------------
# price_history — response cache round-trip + OLAP query (ClickHouse)
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


def test_price_history_put_and_get_returns_payload() -> None:
    base = datetime.now(tz=UTC).replace(microsecond=0) - timedelta(days=2)
    raw = {"candles": [_candle(base + timedelta(minutes=30 * i)) for i in range(5)], "empty": False}
    with _mem_cache() as c:
        c.put_price_history(_ph_params(), raw)
        got = c.get_price_history(_ph_params())
    assert got is not None
    assert len(got["candles"]) == 5


def test_price_history_query_candles_window_clickhouse() -> None:
    base = datetime.now(tz=UTC).replace(microsecond=0) - timedelta(days=10)
    raw = {"candles": [_candle(base + timedelta(days=i), close=1.0 + i) for i in range(10)]}
    c, client = make_clickhouse_cache()
    c.put_price_history(_ph_params(), raw)
    # Seed the read path with the candle rows that were appended.
    appended = clickhouse_inserted_rows(client, series="price_history_candles")
    seed_clickhouse_query_rows(client, appended)
    rows = c.query_candles("VOO", base + timedelta(days=2), base + timedelta(days=5))
    assert len(rows) == 4
    assert all(r["period_type"] == "DAY" for r in rows)


def test_query_candles_memory_degrades_to_empty() -> None:
    with _mem_cache() as c:
        assert c.query_candles("VOO", datetime(2020, 1, 1), datetime(2030, 1, 1)) == []


# ---------------------------------------------------------------------------
# option_chain_cache + instruments_cache — hashed key (memory)
# ---------------------------------------------------------------------------


def test_option_chain_put_and_get_round_trip() -> None:
    params = {"symbol": "AAPL", "contract_type": "CALL", "strike_count": 5}
    raw = {"underlyingSymbol": "AAPL", "callExpDateMap": {}}
    with _mem_cache() as c:
        c.put_option_chain(params, raw)
        got = c.get_option_chain(params)
        miss = c.get_option_chain({**params, "contract_type": "PUT"})
    assert got == raw
    assert miss is None


def test_option_chain_expires() -> None:
    with _mem_cache() as c:
        c.put_option_chain({"symbol": "AAPL"}, {"x": 1}, ttl_seconds=0)
        time.sleep(0.05)
        assert c.get_option_chain({"symbol": "AAPL"}) is None


def test_instruments_round_trip() -> None:
    params = {"symbols": ["AAPL", "MSFT"], "projection": "FUNDAMENTAL"}
    raw = {"instruments": [{"symbol": "AAPL"}]}
    with _mem_cache() as c:
        c.put_instruments(params, raw)
        got = c.get_instruments(params)
    assert got == raw


# ---------------------------------------------------------------------------
# Stats / reset / env flags / singleton
# ---------------------------------------------------------------------------


def test_stats_reports_backend_and_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(cache.ENV_CACHE_ENABLED, "true")
    with _mem_cache() as c:
        c.put_quote("AAPL", _quote_payload("AAPL"))
        stats = c.get_stats()
    d = stats.to_dict()
    assert d["backend"] == "memory"
    assert d["entries"] == 1
    assert d["enabled"] is True


def test_stats_size_error_degrades_to_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = MemoryBackend()
    monkeypatch.setattr(backend, "size", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert cache.Cache(backend=backend).get_stats().entries == 0


def test_reset_clears_response_cache() -> None:
    with _mem_cache() as c:
        c.put_quote("AAPL", _quote_payload("AAPL"))
        c.reset()
        assert c.get_quote("AAPL") is None


def test_truncate_expired_returns_zero() -> None:
    with _mem_cache() as c:
        assert c.truncate_expired() == 0


def test_hourly_breakdown_returns_empty() -> None:
    with _mem_cache() as c:
        assert c.hourly_breakdown(hours=24) == []


def test_hourly_breakdown_invalid_hours_raises() -> None:
    with _mem_cache() as c, pytest.raises(ValueError):
        c.hourly_breakdown(hours=0)


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


@pytest.mark.parametrize(
    "raw",
    ["1", "true", "yes", "on", "TRUE", "Yes", "On", " true ", "  1 ", "\tyes\n"],
)
def test_cache_enabled_truthy_matrix(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(cache.ENV_CACHE_ENABLED, raw)
    assert cache.cache_enabled() is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "FALSE", "nope", "2", "", "   "])
def test_cache_enabled_falsy_matrix(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(cache.ENV_CACHE_ENABLED, raw)
    assert cache.cache_enabled() is False


def test_cache_enabled_unset_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(cache.ENV_CACHE_ENABLED, raising=False)
    assert cache.cache_enabled() is False
    cache.reset_cache_singleton()
    assert cache.get_cache() is None


def test_cache_singleton_is_lazy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(cache.ENV_CACHE_ENABLED, "true")
    monkeypatch.delenv("SCHWAB_CACHE_BACKEND", raising=False)
    cache.reset_cache_singleton()
    a = cache.get_cache()
    b = cache.get_cache()
    assert a is b
    assert a is not None and a.backend.name == "memory"
    cache.reset_cache_singleton()


def test_get_cache_init_failure_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(cache.ENV_CACHE_ENABLED, "true")
    cache.reset_cache_singleton()
    monkeypatch.setattr(
        cache,
        "get_cache_backend",
        lambda: (_ for _ in ()).throw(RuntimeError("init boom")),
    )
    assert cache.get_cache() is None
    cache.reset_cache_singleton()


def test_default_backend_is_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCHWAB_CACHE_BACKEND", raising=False)
    assert cache.Cache().backend.name == "memory"


def test_price_history_invalid_params_returns_none() -> None:
    with _mem_cache() as c:
        assert c.get_price_history({"symbol": "VOO"}) is None
        c.put_price_history({"symbol": "VOO"}, {"candles": []})  # no-op, missing keys


def test_concurrent_writes_do_not_lose_rows() -> None:
    c = _mem_cache()
    try:
        n_writers = 4
        writes_per = 25
        errors: list[BaseException] = []

        def writer(prefix: str) -> None:
            try:
                for i in range(writes_per):
                    sym = f"{prefix}{i:03d}"[:10]
                    c.put_quote(sym, _quote_payload(sym))
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(f"S{n}",)) for n in range(n_writers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        assert not errors, f"writer errors: {errors!r}"
        assert c.get_stats().entries == n_writers * writes_per
    finally:
        c.close()


def test_serialise_round_trip_with_default_str() -> None:
    payload = {"AAPL": {"now": datetime(2026, 1, 1, tzinfo=UTC)}}
    s = json.dumps(payload, default=str)
    assert "2026-01-01" in s


# ---------------------------------------------------------------------------
# Integration smoke — call_endpoint wires cache hit/miss into payload
# (memory backend; the response cache works in-process)
# ---------------------------------------------------------------------------


async def test_call_endpoint_records_cache_status_on_miss(
    monkeypatch: pytest.MonkeyPatch,
    use_fake_backend: None,
) -> None:
    del use_fake_backend
    monkeypatch.setenv("SCHWAB_CACHE_ENABLED", "true")
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

    monkeypatch.setenv("SCHWAB_CACHE_ENABLED", "true")
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
    monkeypatch.setenv("SCHWAB_CACHE_ENABLED", "true")
    from schwab_marketdata_mcp import server

    out = await server.get_cache_stats()
    assert out["enabled"] is True
    assert out["backend"] == "memory"
    assert "entries" in out


async def test_health_check_includes_cache_fields(use_fake_backend: None) -> None:
    del use_fake_backend
    from schwab_marketdata_mcp import server

    out = await server.health_check()
    assert "cache_enabled" in out
    assert "cache_backend" in out
    assert "cache_entries" in out
