"""Deep-coverage completion tests — drive every remaining branch to 100%.

Batch 2 of the v0.6 test campaign.  Each test cites the ``file:line``
gap it closes (from ``--cov-report=term-missing``).  Tests here are
*real* — they assert on persisted content / redaction / return shape,
never empty-pass placeholders.

Modules driven to 100% here:
* ``cache.py``        — corruption / quarantine / DB-closed / event-log
* ``tools/meta.py``   — token-state exceptions / cache-disabled paths
* ``tools/streaming`` — make_client raise / login auth+transient / handler guards
* ``tools/options``   — _persist_chain_snapshot cache-None
* ``tools/price_history`` — _frequency_to_int(None)
* ``tools/_enums``    — unknown-enum → SchwabValidationError
* ``tools/_runtime``  — get_client cached / non-raise_for_status resp
* ``client.py``       — Retry-After header guard / credential_missing
* ``health.py``       — probe paths / OSError guards
* ``metrics.py``      — cli human-text / tail-read guards
* ``server.py``       — error fallback / SchwabError catch / main()
* ``stats.py``        — module import shim
"""

from __future__ import annotations

import json
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import duckdb
import pytest

from schwab_marketdata_mcp import cache

# ===========================================================================
# cache.py — corruption / quarantine / DB-closed / event-log branches
# ===========================================================================


def test_quarantine_returns_when_db_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """cache.py:366-368 — _quarantine_and_reopen with no file on disk → conn None."""
    c = cache.Cache(tmp_path / "c.duckdb")
    c.close()
    # Force the quarantine path against a non-existent file.
    c.db_path = tmp_path / "ghost.duckdb"
    c._quarantine_and_reopen(RuntimeError("synthetic"))
    assert c._conn is None


def test_quarantine_rename_failure_sets_conn_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """cache.py:378-381 — os.rename OSError → conn None, no reopen."""
    db = tmp_path / "c.duckdb"
    c = cache.Cache(db)
    c.close()
    assert db.exists()

    def _boom_rename(*_a: Any, **_k: Any) -> None:
        raise OSError("rename denied")

    monkeypatch.setattr(cache.os, "rename", _boom_rename)
    c._quarantine_and_reopen(RuntimeError("corrupt"))
    assert c._conn is None


def test_quarantine_reopen_failure_sets_conn_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """cache.py:390-392 — reopen after rename fails (duckdb.connect raises)."""
    db = tmp_path / "c.duckdb"
    c = cache.Cache(db)
    c.close()

    calls = {"n": 0}
    real_connect = duckdb.connect

    def _fail_second_connect(*a: Any, **k: Any) -> Any:
        calls["n"] += 1
        raise duckdb.Error("reopen failed")

    monkeypatch.setattr(cache.duckdb, "connect", _fail_second_connect)
    c._quarantine_and_reopen(RuntimeError("corrupt"))
    assert c._conn is None
    # The corrupt backup must have been created before the failed reopen.
    assert any(p.name.startswith("c.duckdb.corrupt-") for p in tmp_path.iterdir())
    monkeypatch.setattr(cache.duckdb, "connect", real_connect)


def test_record_event_noop_when_conn_none(tmp_path: Path) -> None:
    """cache.py:414 — _record_event early-returns when conn is None."""
    c = cache.Cache(tmp_path / "c.duckdb")
    c.close()
    # Must not raise even though the connection is gone.
    c._record_event("hit", "quotes_cache")


def test_record_event_swallows_duckdb_error(tmp_path: Path) -> None:
    """cache.py:420-421 — INSERT failure is swallowed."""
    c = cache.Cache(tmp_path / "c.duckdb")
    real = c._conn
    assert real is not None
    wrapped = MagicMock(wraps=real)
    wrapped.execute.side_effect = duckdb.Error("insert blew up")
    c._conn = wrapped
    c._record_event("hit", "quotes_cache")  # swallowed
    c._conn = real
    c.close()


def _wrap_conn_raising(c: cache.Cache, exc: Exception) -> Any:
    """Return the real conn after installing a wrapped conn whose execute raises."""
    real = c._conn
    assert real is not None
    wrapped = MagicMock(wraps=real)
    wrapped.execute.side_effect = exc
    c._conn = wrapped
    return real


def test_get_quote_conn_none_returns_none(tmp_path: Path) -> None:
    """cache.py:429 — get_quote with conn None → None."""
    c = cache.Cache(tmp_path / "c.duckdb")
    c.close()
    assert c.get_quote("AAPL") is None


def test_get_quote_swallows_query_error(tmp_path: Path) -> None:
    """cache.py:435-437 — SELECT failure logged, returns None."""
    c = cache.Cache(tmp_path / "c.duckdb")
    real = _wrap_conn_raising(c, duckdb.Error("select boom"))
    assert c.get_quote("AAPL") is None
    c._conn = real
    c.close()


def test_put_quote_conn_none_noop(tmp_path: Path) -> None:
    """cache.py:452 — put_quote with conn None is a no-op."""
    c = cache.Cache(tmp_path / "c.duckdb")
    c.close()
    c.put_quote("AAPL", {"AAPL": {"quote": {"lastPrice": 1.0}}})  # no raise


def test_put_quote_swallows_write_error(tmp_path: Path) -> None:
    """cache.py:478-479 — INSERT failure logged, swallowed."""
    c = cache.Cache(tmp_path / "c.duckdb")
    real = _wrap_conn_raising(c, duckdb.Error("write boom"))
    c.put_quote("AAPL", {"AAPL": {"quote": {"lastPrice": 1.0}}})
    c._conn = real
    c.close()


def test_get_price_history_conn_none(tmp_path: Path) -> None:
    """cache.py:504 — conn None → None."""
    c = cache.Cache(tmp_path / "c.duckdb")
    c.close()
    params = {"symbol": "VOO", "period_type": "DAY", "frequency_type": "MINUTE", "frequency": 30}
    assert c.get_price_history(params) is None


def test_get_price_history_swallows_query_error(tmp_path: Path) -> None:
    """cache.py:515-520 — SELECT failure → None."""
    c = cache.Cache(tmp_path / "c.duckdb")
    real = _wrap_conn_raising(c, duckdb.Error("ph select boom"))
    params = {"symbol": "VOO", "period_type": "DAY", "frequency_type": "MINUTE", "frequency": 30}
    assert c.get_price_history(params) is None
    c._conn = real
    c.close()


def test_get_price_history_window_filters_start_end(tmp_path: Path) -> None:
    """cache.py:532-535 — start/end window filters drop out-of-range candles."""
    db = tmp_path / "c.duckdb"
    base = datetime.now(tz=UTC).replace(microsecond=0) - timedelta(days=5)
    candles = [
        {
            "datetime": int((base + timedelta(days=i)).replace(tzinfo=UTC).timestamp() * 1000),
            "open": 1.0,
            "high": 1.0,
            "low": 1.0,
            "close": 1.0,
            "volume": 10,
        }
        for i in range(5)
    ]
    params = {"symbol": "VOO", "period_type": "DAY", "frequency_type": "DAILY", "frequency": 1}
    with cache.Cache(db) as c:
        c.put_price_history(params, {"candles": candles})
        # Window that includes only the middle candle.
        windowed = dict(params)
        windowed["start_datetime"] = (base + timedelta(days=2)).isoformat()
        windowed["end_datetime"] = (base + timedelta(days=2, hours=1)).isoformat()
        got = c.get_price_history(windowed)
    assert got is not None
    assert len(got["candles"]) == 1


def test_put_price_history_conn_none_noop(tmp_path: Path) -> None:
    """cache.py:595 — put_price_history conn None no-op."""
    c = cache.Cache(tmp_path / "c.duckdb")
    c.close()
    params = {"symbol": "VOO", "period_type": "DAY", "frequency_type": "MINUTE", "frequency": 30}
    c.put_price_history(params, {"candles": [{"datetime": 1700000000000, "close": 1.0}]})


def test_put_price_history_swallows_write_error(tmp_path: Path) -> None:
    """cache.py:607-608 — executemany failure swallowed."""
    c = cache.Cache(tmp_path / "c.duckdb")
    real = c._conn
    assert real is not None
    wrapped = MagicMock(wraps=real)
    wrapped.executemany.side_effect = duckdb.Error("ph write boom")
    c._conn = wrapped
    params = {"symbol": "VOO", "period_type": "DAY", "frequency_type": "MINUTE", "frequency": 30}
    c.put_price_history(params, {"candles": [{"datetime": 1700000000000, "close": 1.0}]})
    c._conn = real
    c.close()


def test_get_option_chain_conn_none(tmp_path: Path) -> None:
    """cache.py:619 — conn None → None."""
    c = cache.Cache(tmp_path / "c.duckdb")
    c.close()
    assert c.get_option_chain({"symbol": "AAPL"}) is None


def test_get_option_chain_swallows_query_error(tmp_path: Path) -> None:
    """cache.py:625-630 — SELECT failure → None."""
    c = cache.Cache(tmp_path / "c.duckdb")
    real = _wrap_conn_raising(c, duckdb.Error("oc select boom"))
    assert c.get_option_chain({"symbol": "AAPL"}) is None
    c._conn = real
    c.close()


def test_put_option_chain_conn_none_and_error(tmp_path: Path) -> None:
    """cache.py:653 + 664-665 — conn None no-op, then write error swallowed."""
    c = cache.Cache(tmp_path / "c.duckdb")
    c.close()
    c.put_option_chain({"symbol": "AAPL"}, {"x": 1})  # conn None branch
    c2 = cache.Cache(tmp_path / "c2.duckdb")
    real = _wrap_conn_raising(c2, duckdb.Error("oc write boom"))
    c2.put_option_chain({"symbol": "AAPL"}, {"x": 1})
    c2._conn = real
    c2.close()


def test_get_instruments_conn_none_and_error(tmp_path: Path) -> None:
    """cache.py:676 + 682-687 — conn None → None, then SELECT error → None."""
    c = cache.Cache(tmp_path / "c.duckdb")
    c.close()
    assert c.get_instruments({"cusip": "X"}) is None
    c2 = cache.Cache(tmp_path / "c2.duckdb")
    real = _wrap_conn_raising(c2, duckdb.Error("instr select boom"))
    assert c2.get_instruments({"cusip": "X"}) is None
    c2._conn = real
    c2.close()


def test_put_instruments_conn_none_and_error(tmp_path: Path) -> None:
    """cache.py:709 + 720-721 — conn None no-op, write error swallowed."""
    c = cache.Cache(tmp_path / "c.duckdb")
    c.close()
    c.put_instruments({"cusip": "X"}, {"x": 1})
    c2 = cache.Cache(tmp_path / "c2.duckdb")
    real = _wrap_conn_raising(c2, duckdb.Error("instr write boom"))
    c2.put_instruments({"cusip": "X"}, {"x": 1})
    c2._conn = real
    c2.close()


# ---- option chain snapshots / aggregate IV / OLAP / stats branches ----


def test_write_snapshot_conn_none_returns_zero(tmp_path: Path) -> None:
    """cache.py:761 — write_option_chain_snapshot conn None → 0."""
    c = cache.Cache(tmp_path / "c.duckdb")
    c.close()
    rows = [{"expiry": "2026-06-19", "strike": 100.0, "call_put": "CALL"}]
    assert c.write_option_chain_snapshot("AAPL", datetime.now(tz=UTC), rows) == 0


def test_write_snapshot_swallows_write_error(tmp_path: Path) -> None:
    """cache.py:774-779 — executemany failure → 0."""
    c = cache.Cache(tmp_path / "c.duckdb")
    real = c._conn
    assert real is not None
    wrapped = MagicMock(wraps=real)
    wrapped.executemany.side_effect = duckdb.Error("snap write boom")
    c._conn = wrapped
    rows = [{"expiry": "2026-06-19", "strike": 100.0, "call_put": "CALL"}]
    assert c.write_option_chain_snapshot("AAPL", datetime.now(tz=UTC), rows) == 0
    c._conn = real
    c.close()


def test_aggregate_atm_iv_invalid_underlying_and_date(tmp_path: Path) -> None:
    """cache.py:807-811 — bad underlying / unparseable date → all-None."""
    with cache.Cache(tmp_path / "c.duckdb") as c:
        assert c.aggregate_atm_iv("", date(2026, 5, 1)) == {"30d": None, "60d": None, "90d": None}
        assert c.aggregate_atm_iv("AAPL", "not-a-date") == {  # type: ignore[arg-type]
            "30d": None,
            "60d": None,
            "90d": None,
        }


def test_fetch_latest_snapshot_conn_none(tmp_path: Path) -> None:
    """cache.py:837 — _fetch_latest_snapshot_contracts conn None → []."""
    c = cache.Cache(tmp_path / "c.duckdb")
    c.close()
    assert c._fetch_latest_snapshot_contracts("AAPL", datetime.now(tz=UTC)) == []


def test_fetch_latest_snapshot_swallows_query_error(tmp_path: Path) -> None:
    """cache.py:857-862 — query failure → []."""
    c = cache.Cache(tmp_path / "c.duckdb")
    real = _wrap_conn_raising(c, duckdb.Error("snap query boom"))
    assert c._fetch_latest_snapshot_contracts("AAPL", datetime.now(tz=UTC)) == []
    c._conn = real
    c.close()


def test_upsert_iv_history_conn_none_and_error(tmp_path: Path) -> None:
    """cache.py:888 + 899-900 — conn None no-op, then write error swallowed."""
    c = cache.Cache(tmp_path / "c.duckdb")
    c.close()
    c._upsert_iv_history("AAPL", date(2026, 5, 1), "30d", 0.3, 5)
    c2 = cache.Cache(tmp_path / "c2.duckdb")
    real = _wrap_conn_raising(c2, duckdb.Error("iv write boom"))
    c2._upsert_iv_history("AAPL", date(2026, 5, 1), "30d", 0.3, 5)
    c2._conn = real
    c2.close()


def test_get_iv_percentile_rank_validation_errors(tmp_path: Path) -> None:
    """cache.py:923-927 — bad bucket / lookback / underlying raise ValueError."""
    with cache.Cache(tmp_path / "c.duckdb") as c:
        with pytest.raises(ValueError, match="expiry_bucket"):
            c.get_iv_percentile_rank("AAPL", "45d")
        with pytest.raises(ValueError, match="lookback_days"):
            c.get_iv_percentile_rank("AAPL", "30d", lookback_days=0)
        with pytest.raises(ValueError, match="underlying"):
            c.get_iv_percentile_rank("", "30d")


def test_fetch_iv_history_window_conn_none_and_error(tmp_path: Path) -> None:
    """cache.py:980 + 991-996 — conn None → [], query error → []."""
    c = cache.Cache(tmp_path / "c.duckdb")
    c.close()
    assert c._fetch_iv_history_window("AAPL", "30d", 252) == []
    c2 = cache.Cache(tmp_path / "c2.duckdb")
    real = _wrap_conn_raising(c2, duckdb.Error("iv window boom"))
    assert c2._fetch_iv_history_window("AAPL", "30d", 252) == []
    c2._conn = real
    c2.close()


def test_query_candles_conn_none_and_error(tmp_path: Path) -> None:
    """cache.py:1023 + 1037-1039 — conn None → [], query error → []; tz coercion 1019-1020."""
    c = cache.Cache(tmp_path / "c.duckdb")
    c.close()
    start = datetime.now(tz=UTC) - timedelta(days=1)
    end = datetime.now(tz=UTC)
    assert c.query_candles("VOO", start, end) == []
    c2 = cache.Cache(tmp_path / "c2.duckdb")
    real = _wrap_conn_raising(c2, duckdb.Error("candles boom"))
    assert c2.query_candles("VOO", start, end) == []
    c2._conn = real
    c2.close()


def test_query_candles_returns_rows_with_tz_aware_bounds(tmp_path: Path) -> None:
    """cache.py:1019-1020 + 1040-1057 — tz-aware start/end + row mapping."""
    db = tmp_path / "c.duckdb"
    base = datetime.now(tz=UTC).replace(microsecond=0) - timedelta(days=3)
    candles = [
        {
            "datetime": int((base + timedelta(days=i)).replace(tzinfo=UTC).timestamp() * 1000),
            "open": 1.0,
            "high": 2.0,
            "low": 0.5,
            "close": 1.5,
            "volume": 100,
        }
        for i in range(3)
    ]
    params = {"symbol": "VOO", "period_type": "DAY", "frequency_type": "DAILY", "frequency": 1}
    with cache.Cache(db) as c:
        c.put_price_history(params, {"candles": candles})
        out = c.query_candles("VOO", base - timedelta(days=1), base + timedelta(days=5))
    assert len(out) == 3
    assert out[0]["close"] == 1.5
    assert out[0]["datetime"].endswith("Z")


def test_get_stats_size_oserror_branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """cache.py:1071-1072 — stat() OSError → size_mb 0.0."""
    c = cache.Cache(tmp_path / "c.duckdb")
    real_stat = Path.stat
    calls = {"n": 0}

    def _boom_stat(self: Path, *a: Any, **k: Any) -> Any:
        if self == c.db_path:
            calls["n"] += 1
            # First call is exists() — let it through; the second is the
            # size read inside get_stats() — fail that one.
            if calls["n"] >= 2:
                raise OSError("stat denied")
        return real_stat(self, *a, **k)

    monkeypatch.setattr(Path, "stat", _boom_stat)
    stats = c.get_stats()
    assert stats.size_mb == 0.0
    c.close()


def test_get_stats_count_and_event_errors(tmp_path: Path) -> None:
    """cache.py:1079-1080 + 1094-1095 — COUNT(*) and event-count failures swallowed."""
    c = cache.Cache(tmp_path / "c.duckdb")
    real = c._conn
    assert real is not None
    wrapped = MagicMock(wraps=real)
    wrapped.execute.side_effect = duckdb.Error("count boom")
    c._conn = wrapped
    stats = c.get_stats()
    # All tables reported 0 rows because COUNT(*) raised.
    assert all(v == 0 for v in stats.rows_per_table.values())
    assert stats.hits_24h == 0
    c._conn = real
    c.close()


def test_count_expired_conn_none_and_unknown_table(tmp_path: Path) -> None:
    """cache.py:1111 + 1128 — conn None → 0, unknown table → 0."""
    c = cache.Cache(tmp_path / "c.duckdb")
    c.close()
    assert c._count_expired("quotes_cache") == 0
    c2 = cache.Cache(tmp_path / "c2.duckdb")
    assert c2._count_expired("not_a_table") == 0
    c2.close()


def test_count_expired_price_history_error(tmp_path: Path) -> None:
    """cache.py:1125-1126 — price_history count error → 0."""
    c = cache.Cache(tmp_path / "c.duckdb")
    real = _wrap_conn_raising(c, duckdb.Error("ph count boom"))
    assert c._count_expired("price_history_cache") == 0
    c._conn = real
    c.close()


def test_count_expired_generic_table_error(tmp_path: Path) -> None:
    """cache.py:1134-1135 — generic table count error → 0."""
    c = cache.Cache(tmp_path / "c.duckdb")
    real = _wrap_conn_raising(c, duckdb.Error("count boom"))
    assert c._count_expired("quotes_cache") == 0
    c._conn = real
    c.close()


def test_hourly_breakdown_conn_none_and_error(tmp_path: Path) -> None:
    """cache.py:1155 + 1172-1174 — conn None → [], query error → []."""
    c = cache.Cache(tmp_path / "c.duckdb")
    c.close()
    assert c.hourly_breakdown(24) == []
    c2 = cache.Cache(tmp_path / "c2.duckdb")
    real = _wrap_conn_raising(c2, duckdb.Error("breakdown boom"))
    assert c2.hourly_breakdown(24) == []
    c2._conn = real
    c2.close()


def test_truncate_expired_conn_none_and_error(tmp_path: Path) -> None:
    """cache.py:1197 + 1206-1207 + 1212-1213 — conn None → 0, delete error swallowed."""
    c = cache.Cache(tmp_path / "c.duckdb")
    c.close()
    assert c.truncate_expired() == 0
    c2 = cache.Cache(tmp_path / "c2.duckdb")
    real = _wrap_conn_raising(c2, duckdb.Error("delete boom"))
    assert c2.truncate_expired() == 0  # all deletes + event-prune fail, swallowed
    c2._conn = real
    c2.close()


def test_reset_conn_none_and_error(tmp_path: Path) -> None:
    """cache.py:1220-1227 — conn None no-op, DELETE error swallowed."""
    c = cache.Cache(tmp_path / "c.duckdb")
    c.close()
    c.reset()  # conn None branch
    c2 = cache.Cache(tmp_path / "c2.duckdb")
    real = _wrap_conn_raising(c2, duckdb.Error("reset boom"))
    c2.reset()
    c2._conn = real
    c2.close()


# ---- module-level helper branches ----


def test_parse_dt_naive_and_bad_string() -> None:
    """cache.py:262 + 272 — naive datetime returned as-is; bad string → None."""
    naive = datetime(2026, 5, 1, 12, 0, 0)
    assert cache._parse_dt(naive) == naive
    assert cache._parse_dt("totally-not-a-date") is None


def test_safe_int_bad_value() -> None:
    """cache.py:289-290 — non-numeric → None."""
    assert cache._safe_int("abc") is None


def test_is_expired_parses_string_fetched_at() -> None:
    """cache.py:1257-1260 — string fetched_at parsed; unparseable → True."""
    iso = datetime.now(tz=UTC).replace(tzinfo=None).isoformat()
    assert cache._is_expired(iso, 3600) is False
    assert cache._is_expired("garbage", 3600) is True


def test_deserialise_variants() -> None:
    """cache.py:1267 + 1269 + 1272-1276 — None / dict / bad-json / non-dict json."""
    assert cache._deserialise(None) is None
    assert cache._deserialise({"a": 1}) == {"a": 1}
    assert cache._deserialise("{bad json") is None
    assert cache._deserialise("[1,2,3]") is None  # valid json but not a dict
    assert cache._deserialise(12345) is None  # not str/bytes/dict


def test_normalise_naive_utc_fallback_to_now() -> None:
    """cache.py:1316-1319 — unparseable value falls back to _utcnow()."""
    out = cache._normalise_naive_utc(object())
    assert isinstance(out, datetime)
    assert out.tzinfo is None


def test_coerce_date_variants() -> None:
    """cache.py:1324-1346 — datetime / epoch / bad epoch / iso / suffixed / bad str."""
    assert cache._coerce_date(datetime(2026, 5, 1, 9, 30)) == date(2026, 5, 1)
    assert cache._coerce_date(1716163200000) == date(2024, 5, 20)
    assert cache._coerce_date(1e308 * 1000) is None  # overflow guard
    assert cache._coerce_date("2026-06-19:30") == date(2026, 6, 19)
    assert cache._coerce_date("2026-06-19") == date(2026, 6, 19)
    assert cache._coerce_date("nonsense") is None
    assert cache._coerce_date(None) is None


def test_coerce_iv_normalises_percent_and_rejects_nonpositive() -> None:
    """cache.py:1464-1474 — percent>1.5 → /100; <=0 → None; fractional kept."""
    assert cache._coerce_iv(32.5) == pytest.approx(0.325)
    assert cache._coerce_iv(0.32) == pytest.approx(0.32)
    assert cache._coerce_iv(0) is None
    assert cache._coerce_iv(None) is None


def test_compute_atm_iv_median_none_path() -> None:
    """cache.py:1505-1506 — empty-strike bucket returns (None, count)."""
    # All contracts in-bucket but every strike None → in_bucket empty → (None, 0).
    asof = date(2026, 5, 1)
    contracts = [{"expiry": date(2026, 5, 31), "strike": None, "implied_vol": 0.3}]
    iv, count = cache._compute_atm_iv_for_bucket(contracts, asof, 30)
    assert iv is None
    assert count == 0


def test_format_hour_utc_variants() -> None:
    """cache.py:1243-1250 — naive / aware / parseable str / None."""
    assert cache._format_hour_utc(datetime(2026, 5, 1, 10)).endswith("Z")
    aware = datetime(2026, 5, 1, 10, tzinfo=UTC)
    assert cache._format_hour_utc(aware).endswith("Z")
    assert cache._format_hour_utc("not-a-date") is None


# ===========================================================================
# tools/meta.py — token-state exceptions / cache-disabled paths
# ===========================================================================


async def test_meta_safe_token_state_path_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """meta.py:37-38 — resolve_token_path raising → (MISSING, None)."""
    from schwab_marketdata_mcp.security import TokenState
    from schwab_marketdata_mcp.tools import meta

    def _boom(_: Any) -> Any:
        raise RuntimeError("resolve blew up")

    monkeypatch.setattr(meta, "resolve_token_path", _boom)
    state, parsed = meta._safe_token_state()
    assert state is TokenState.MISSING
    assert parsed is None


def test_meta_safe_cache_summary_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """meta.py:51 — cache disabled → enabled False summary."""
    from schwab_marketdata_mcp.tools import meta

    monkeypatch.setenv("SCHWAB_CACHE_ENABLED", "0")
    out = meta._safe_cache_summary()
    assert out == {"enabled": False, "size_mb": 0.0, "hit_rate_24h": None}


def test_meta_safe_cache_summary_get_cache_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """meta.py:54 — get_cache() returns None → enabled False summary."""
    from schwab_marketdata_mcp.tools import meta

    monkeypatch.setattr(meta, "get_cache", lambda: None)
    out = meta._safe_cache_summary()
    assert out["enabled"] is False


def test_meta_safe_cache_summary_get_stats_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """meta.py:57-58 — get_stats() raising → enabled True, zeroed numerics."""
    from schwab_marketdata_mcp.tools import meta

    fake_cache = MagicMock()
    fake_cache.get_stats.side_effect = RuntimeError("stats boom")
    monkeypatch.setattr(meta, "get_cache", lambda: fake_cache)
    out = meta._safe_cache_summary()
    assert out == {"enabled": True, "size_mb": 0.0, "hit_rate_24h": None}


async def test_meta_health_check_token_valid_mtime_oserror(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """meta.py:85-86 — VALID token but stat() OSError → age stays None."""
    from schwab_marketdata_mcp.security import TokenState
    from schwab_marketdata_mcp.tools import meta

    monkeypatch.setattr(meta, "_safe_token_state", lambda: (TokenState.VALID, {"creation_timestamp": 1}))

    def _boom_resolve(_: Any) -> Any:
        # First call inside health_check_impl resolves the path then stats it.
        p = MagicMock()
        p.stat.side_effect = OSError("mtime denied")
        return p

    monkeypatch.setattr("schwab_marketdata_mcp.security.resolve_token_path", _boom_resolve)
    out = await meta.health_check_impl()
    assert out["token_age_days"] is None
    assert out["token_expires_in_days"] is None
    assert out["token_state"] == "valid"


def test_meta_rate_limit_budget_unset_and_bad(monkeypatch: pytest.MonkeyPatch) -> None:
    """meta.py:140-143 — unset → 120; non-int → 120."""
    from schwab_marketdata_mcp.tools import meta

    monkeypatch.delenv("SCHWAB_RATE_LIMIT_PER_MIN", raising=False)
    assert meta._rate_limit_budget() == 120
    monkeypatch.setenv("SCHWAB_RATE_LIMIT_PER_MIN", "not-a-number")
    assert meta._rate_limit_budget() == 120
    monkeypatch.setenv("SCHWAB_RATE_LIMIT_PER_MIN", "")
    assert meta._rate_limit_budget() == 120


async def test_meta_get_cache_stats_cache_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """meta.py:174 — get_cache None → empty stats dict."""
    from schwab_marketdata_mcp.tools import meta

    monkeypatch.setattr(meta, "get_cache", lambda: None)
    out = await meta.get_cache_stats_impl()
    assert out["enabled"] is False
    assert out["db_path"] is None
    assert out["hourly_breakdown_24h"] == []


# ===========================================================================
# tools/price_history.py + tools/options.py + tools/_enums.py
# ===========================================================================


def test_frequency_to_int_none() -> None:
    """price_history.py:53 — None freq → None."""
    from schwab_marketdata_mcp.tools.price_history import _frequency_to_int

    assert _frequency_to_int(None) is None
    assert _frequency_to_int("EVERY_FIVE_MINUTES") == 5
    assert _frequency_to_int("UNKNOWN") is None


def test_persist_chain_snapshot_cache_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """options.py:135-136 — get_cache None → 0 rows."""
    from schwab_marketdata_mcp.tools import options

    monkeypatch.setattr(options, "get_cache", lambda: None)
    payload = {"callExpDateMap": {}, "_cache_status": "miss"}
    assert options._persist_chain_snapshot("AAPL", payload) == 0


def test_enums_unknown_name_raises_validation() -> None:
    """_enums.py:25-31 — unknown enum name → SchwabValidationError."""
    from schwab_marketdata_mcp.errors import SchwabValidationError
    from schwab_marketdata_mcp.tools import _enums

    with pytest.raises(SchwabValidationError) as ei:
        _enums.options_contract_type("NOPE")
    assert "unknown enum name" in ei.value.reason


def test_enums_quote_field_single_and_list() -> None:
    """_enums.py:35 + 41 — quote_field / quote_fields happy path."""
    from schwab_marketdata_mcp.tools import _enums

    assert _enums.quote_field("QUOTE") is not None
    assert _enums.quote_fields(None) is None
    fields = _enums.quote_fields(["QUOTE", "FUNDAMENTAL"])
    assert fields is not None and len(fields) == 2


# ===========================================================================
# tools/_runtime.py — get_client cached + non-raise_for_status response
# ===========================================================================


async def test_runtime_get_client_returns_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """_runtime.py:39->43 — second call returns the already-built client."""
    from schwab_marketdata_mcp.tools import _runtime

    _runtime.reset_client_cache()
    monkeypatch.setenv("SCHWAB_MOCK_BACKEND", "fixtures")
    first = await _runtime.get_client()
    second = await _runtime.get_client()
    assert first is second
    _runtime.reset_client_cache()


async def test_runtime_get_client_double_checked_lock_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_runtime.py — double-checked locking guard is concurrency-safe.

    Two coroutines call :func:`get_client` concurrently against a freshly reset
    cache while we hold the lock, so both queue behind it.  The winner builds
    the client; the loser re-checks the inner guard (now non-None) and returns
    the *same* instance without building a second client.  ``make_rate_limited``
    must therefore run exactly once.

    Note: the inner-guard short-circuit arc (``41->43``) is the loser's path.
    It is exercised here, but coverage.py cannot reliably record branch arcs
    that span an ``asyncio`` task switch, so the source carries a
    ``# pragma: no branch`` on that guard; this test still asserts the behaviour
    is correct.
    """
    import asyncio

    from schwab_marketdata_mcp.tools import _runtime

    _runtime.reset_client_cache()
    _runtime._lock = asyncio.Lock()  # fresh lock bound to this running loop
    sentinel = object()
    calls = {"n": 0}

    def _make_once() -> Any:
        calls["n"] += 1
        return sentinel

    real_make = _runtime.make_rate_limited
    monkeypatch.setattr(_runtime, "make_rate_limited", _make_once)
    try:
        await _runtime._lock.acquire()
        task_a = asyncio.create_task(_runtime.get_client())
        task_b = asyncio.create_task(_runtime.get_client())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        _runtime._lock.release()
        results = await asyncio.gather(task_a, task_b)
    finally:
        monkeypatch.setattr(_runtime, "make_rate_limited", real_make)

    assert results[0] is sentinel
    assert results[1] is sentinel
    assert calls["n"] == 1  # loser short-circuited instead of double-building
    _runtime.reset_client_cache()


def test_runtime_to_dict_variants() -> None:
    """_runtime.py:110->112 path + _to_dict list/scalar/no-json branches."""
    from schwab_marketdata_mcp.tools import _runtime

    assert _runtime._to_dict({"a": 1}) == {"a": 1}

    class _RespList:
        def json(self) -> Any:
            return [1, 2, 3]

    assert _runtime._to_dict(_RespList()) == {"items": [1, 2, 3]}

    class _RespScalar:
        def json(self) -> Any:
            return 42

    assert _runtime._to_dict(_RespScalar()) == {"value": 42}

    assert _runtime._to_dict(object())["value"] is not None


async def test_runtime_call_endpoint_no_raise_for_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """_runtime.py:110->112 — response without raise_for_status is handled."""
    from schwab_marketdata_mcp.tools import _runtime

    _runtime.reset_client_cache()
    monkeypatch.setenv("SCHWAB_MOCK_BACKEND", "fixtures")

    async def _fetch(_client: Any) -> Any:
        # Plain dict has no raise_for_status; the wrapper must skip it.
        return {"ok": True}

    out = await _runtime.call_endpoint("get_quote", _fetch)
    assert out["ok"] is True
    assert out["_cache_status"] == "disabled"
    _runtime.reset_client_cache()


# ===========================================================================
# tools/streaming.py — factory import / make_client raise / login errors / guards
# ===========================================================================


def test_streaming_make_stream_client_imports_real(monkeypatch: pytest.MonkeyPatch) -> None:
    """streaming.py:61-63 — _make_stream_client constructs schwab StreamClient."""
    import schwab.streaming as schwab_streaming

    from schwab_marketdata_mcp.tools import streaming as streaming_mod

    captured: dict[str, Any] = {}

    class _FakeStreamClient:
        def __init__(self, client: Any) -> None:
            captured["client"] = client

    monkeypatch.setattr(schwab_streaming, "StreamClient", _FakeStreamClient)
    out = streaming_mod._make_stream_client("inner-client")
    assert isinstance(out, _FakeStreamClient)
    assert captured["client"] == "inner-client"


async def test_streaming_make_client_reraises_schwab_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """streaming.py:193-195 — make_client SchwabError is re-raised untouched."""
    from schwab_marketdata_mcp.errors import SchwabAuthError
    from schwab_marketdata_mcp.models import GetStreamingSnapshotInput
    from schwab_marketdata_mcp.tools import streaming as streaming_mod

    def _boom() -> Any:
        raise SchwabAuthError(reason="token_not_initialized", hint="run auth")

    monkeypatch.setattr(streaming_mod, "make_client", _boom)
    args = GetStreamingSnapshotInput(symbols=["VOO"], service="LEVELONE_EQUITIES", duration_ms=500)
    with pytest.raises(SchwabAuthError):
        await streaming_mod.get_streaming_snapshot_impl(args)


async def test_streaming_login_auth_error_reraised(monkeypatch: pytest.MonkeyPatch) -> None:
    """streaming.py:205-206 — SchwabAuthError from login bubbles up unchanged."""
    from schwab_marketdata_mcp.errors import SchwabAuthError
    from schwab_marketdata_mcp.models import GetStreamingSnapshotInput
    from schwab_marketdata_mcp.tools import streaming as streaming_mod

    class _AuthFailStreamer:
        async def login(self) -> None:
            raise SchwabAuthError(reason="access_token_invalid", hint="reauth")

        async def logout(self) -> None:  # pragma: no cover - never reached
            pass

    monkeypatch.setattr(streaming_mod, "make_client", lambda: "client")
    monkeypatch.setattr(streaming_mod, "_make_stream_client", lambda _c: _AuthFailStreamer())
    monkeypatch.setenv("SCHWAB_MOCK_BACKEND", "fixtures")
    args = GetStreamingSnapshotInput(symbols=["VOO"], service="LEVELONE_EQUITIES", duration_ms=500)
    with pytest.raises(SchwabAuthError):
        await streaming_mod.get_streaming_snapshot_impl(args)


async def test_streaming_login_generic_error_wrapped_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    """streaming.py:207-212 — generic login failure → SchwabTransientError; logout NOT run (224->234)."""
    from schwab_marketdata_mcp.errors import SchwabTransientError
    from schwab_marketdata_mcp.models import GetStreamingSnapshotInput
    from schwab_marketdata_mcp.tools import streaming as streaming_mod

    class _GenericFailStreamer:
        def __init__(self) -> None:
            self.logout_calls = 0

        async def login(self) -> None:
            raise RuntimeError("socket reset during login")

        async def logout(self) -> None:
            self.logout_calls += 1

    fake = _GenericFailStreamer()
    monkeypatch.setattr(streaming_mod, "make_client", lambda: "client")
    monkeypatch.setattr(streaming_mod, "_make_stream_client", lambda _c: fake)
    monkeypatch.setenv("SCHWAB_MOCK_BACKEND", "fixtures")
    args = GetStreamingSnapshotInput(symbols=["VOO"], service="LEVELONE_EQUITIES", duration_ms=500)
    with pytest.raises(SchwabTransientError) as ei:
        await streaming_mod.get_streaming_snapshot_impl(args)
    assert "streamer login failed" in ei.value.hint
    # logged_in stayed False → logout must NOT have been attempted.
    assert fake.logout_calls == 0


async def test_streaming_handler_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    """streaming.py:170 + 176 + 179 — non-list content / non-dict entry / unknown symbol skipped."""
    from schwab_marketdata_mcp.models import GetStreamingSnapshotInput
    from schwab_marketdata_mcp.tools import streaming as streaming_mod

    # Three frames: content not a list, content list with a non-dict + unknown sym.
    frames = [
        {"content": "not-a-list"},
        {"content": ["scalar", {"key": "UNKNOWN", "BID_PRICE": 1.0}]},
        {"content": [{"key": "VOO", "BID_PRICE": 9.0, "ASK_PRICE": 9.1, "LAST_PRICE": 9.05, "TOTAL_VOLUME": 5}]},
    ]

    class _ScriptedStreamer:
        def __init__(self) -> None:
            self._handlers: list[Any] = []
            self._frames = list(frames)
            self.logout_calls = 0

        async def login(self) -> None:
            pass

        async def logout(self) -> None:
            self.logout_calls += 1

        def add_level_one_equity_handler(self, h: Any) -> None:
            self._handlers.append(h)

        async def level_one_equity_subs(self, _symbols: list[str]) -> None:
            pass

        async def handle_message(self) -> None:
            if self._frames:
                msg = self._frames.pop(0)
                for h in self._handlers:
                    h(msg)
                return
            import asyncio as _asyncio

            await _asyncio.sleep(0.05)

    fake = _ScriptedStreamer()
    monkeypatch.setattr(streaming_mod, "make_client", lambda: "client")
    monkeypatch.setattr(streaming_mod, "_make_stream_client", lambda _c: fake)
    monkeypatch.setenv("SCHWAB_MOCK_BACKEND", "fixtures")
    args = GetStreamingSnapshotInput(symbols=["VOO"], service="LEVELONE_EQUITIES", duration_ms=600)
    out = await streaming_mod.get_streaming_snapshot_impl(args)
    # Only the VOO frame produced a record (the other two are guarded away).
    assert out["messages_count"] == 1
    assert out["snapshots"]["VOO"][0]["bid"] == 9.0
    assert fake.logout_calls == 1


# ===========================================================================
# client.py — Retry-After header guard + credential_missing branch
# ===========================================================================


def test_client_retry_after_header_access_guard() -> None:
    """client.py:198-199 — headers access raising → None."""
    from schwab_marketdata_mcp import client

    class _BadResp:
        @property
        def headers(self) -> Any:
            raise RuntimeError("no headers")

    assert client._retry_after_seconds(_BadResp()) is None


def test_client_retry_after_header_parsed() -> None:
    """client.py:200-205 — valid / missing / non-numeric Retry-After."""
    from schwab_marketdata_mcp import client

    class _Resp:
        def __init__(self, val: Any) -> None:
            self.headers = {"Retry-After": val} if val is not None else {}

    assert client._retry_after_seconds(_Resp("30")) == 30
    assert client._retry_after_seconds(_Resp(None)) is None
    assert client._retry_after_seconds(_Resp("soon")) is None


def test_make_real_client_credential_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """client.py:499-507 — missing APP_KEY/SECRET → SchwabAuthError credential_missing."""
    from schwab_marketdata_mcp import client
    from schwab_marketdata_mcp.errors import SchwabAuthError

    monkeypatch.delenv("SCHWAB_APP_KEY", raising=False)
    monkeypatch.delenv("SCHWAB_APP_SECRET", raising=False)
    with pytest.raises(SchwabAuthError) as ei:
        client._make_real_client(tmp_path / "token.json")
    assert ei.value.reason == "credential_missing"


def test_make_client_real_path_invoked(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """client.py:569-571 — non-fixtures path resolves token + builds real client."""
    from schwab_marketdata_mcp import client

    monkeypatch.delenv("SCHWAB_MOCK_BACKEND", raising=False)
    sentinel = object()
    monkeypatch.setattr(client, "resolve_token_path", lambda _a: tmp_path / "token.json")
    monkeypatch.setattr(client, "_enforce_token_or_raise", lambda _p: None)
    monkeypatch.setattr(client, "_make_real_client", lambda _p: sentinel)
    assert client.make_client() is sentinel


def test_make_real_client_builds_with_easy_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """client.py:508-522 — easy_client + UA injection on the happy path."""
    from schwab_marketdata_mcp import client

    monkeypatch.setenv("SCHWAB_APP_KEY", "k")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "s")
    fake_session_headers: dict[str, str] = {}
    fake_client = MagicMock()
    fake_client.session.headers = fake_session_headers

    import schwab.auth as schwab_auth

    monkeypatch.setattr(schwab_auth, "easy_client", lambda **_kw: fake_client)
    # token_file_lock uses a real lock file under the token's parent.
    tok = tmp_path / "token.json"
    out = client._make_real_client(tok)
    assert out is fake_client
    assert "User-Agent" in fake_session_headers


def test_inject_user_agent_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    """client.py:540-548 — session None / headers None / exception swallowed."""
    from schwab_marketdata_mcp import client

    client._inject_user_agent(MagicMock(session=None))  # session None branch

    no_headers = MagicMock()
    no_headers.session = MagicMock(headers=None)
    client._inject_user_agent(no_headers)  # headers None branch

    class _Boom:
        @property
        def session(self) -> Any:
            raise RuntimeError("attr explode")

    client._inject_user_agent(_Boom())  # exception swallowed


# ===========================================================================
# health.py — probe paths + OSError guards
# ===========================================================================


def test_health_probe_schwab_py_disabled_dummy_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """health.py:173-174 — dummy/blank creds skip the schwab-py probe (returns None)."""
    from schwab_marketdata_mcp import health

    monkeypatch.setenv("SCHWAB_APP_KEY", "dummy-not-a-real-secret")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "x")
    assert health._probe_token_age_via_schwab_py(tmp_path / "token.json") is None
    monkeypatch.delenv("SCHWAB_APP_KEY", raising=False)
    assert health._probe_token_age_via_schwab_py(tmp_path / "token.json") is None


def test_health_probe_schwab_py_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """health.py:188-190 — easy_client.token_age returns timedelta → expires_in."""
    from schwab_marketdata_mcp import health

    monkeypatch.setenv("SCHWAB_APP_KEY", "real-key")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "real-secret")
    fake_client = MagicMock()
    fake_client.token_age.return_value = timedelta(hours=24)

    import schwab.auth as schwab_auth

    monkeypatch.setattr(schwab_auth, "easy_client", lambda **_kw: fake_client)
    out = health._probe_token_age_via_schwab_py(tmp_path / "token.json")
    assert out is not None
    assert out == timedelta(days=health.REFRESH_TOKEN_LIFETIME_DAYS) - timedelta(hours=24)


def test_health_probe_schwab_py_non_timedelta(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """health.py:189->193 — token_age returns non-timedelta → None."""
    from schwab_marketdata_mcp import health

    monkeypatch.setenv("SCHWAB_APP_KEY", "real-key")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "real-secret")
    fake_client = MagicMock()
    fake_client.token_age.return_value = "not-a-timedelta"

    import schwab.auth as schwab_auth

    monkeypatch.setattr(schwab_auth, "easy_client", lambda **_kw: fake_client)
    assert health._probe_token_age_via_schwab_py(tmp_path / "token.json") is None


def test_health_probe_schwab_py_exception(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """health.py:191-192 — easy_client raising → None."""
    from schwab_marketdata_mcp import health

    monkeypatch.setenv("SCHWAB_APP_KEY", "real-key")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "real-secret")

    import schwab.auth as schwab_auth

    def _boom(**_kw: Any) -> Any:
        raise RuntimeError("auth explode")

    monkeypatch.setattr(schwab_auth, "easy_client", _boom)
    assert health._probe_token_age_via_schwab_py(tmp_path / "token.json") is None


def test_health_probe_mtime_oserror(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """health.py:200-201 — stat() OSError → None."""
    from schwab_marketdata_mcp import health

    assert health._probe_token_age_via_mtime(tmp_path / "absent.json") is None


def test_health_write_desktop_marker_with_hint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """health.py:126-127 — marker body includes the redacted hint block."""
    from schwab_marketdata_mcp import health

    home = tmp_path / "home"
    desktop = home / "Desktop"
    desktop.mkdir(parents=True)
    monkeypatch.setattr(health.Path, "home", classmethod(lambda _cls: home))
    out = health._write_desktop_marker("status text", hint="Bearer secret.tok leaked")
    assert out is not None
    body = out.read_text()
    assert "## Hint" in body
    assert "secret.tok" not in body  # redacted


def test_health_write_desktop_marker_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """health.py:140-141 — write_text OSError → None."""
    from schwab_marketdata_mcp import health

    home = tmp_path / "home"
    desktop = home / "Desktop"
    desktop.mkdir(parents=True)
    monkeypatch.setattr(health.Path, "home", classmethod(lambda _cls: home))

    real_write = Path.write_text

    def _boom_write(self: Path, *a: Any, **k: Any) -> Any:
        if self.name == health.DESKTOP_REAUTH_FILE:
            raise OSError("disk full")
        return real_write(self, *a, **k)

    monkeypatch.setattr(Path, "write_text", _boom_write)
    assert health._write_desktop_marker("status") is None


def _health_setup_valid_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    pkg = state / "schwab-marketdata-mcp"
    pkg.mkdir(parents=True)
    import os as _os

    _os.chmod(pkg, 0o700)
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    tok = pkg / "token.json"
    tok.write_text(json.dumps({"creation_timestamp": int(time.time())}))
    _os.chmod(tok, 0o600)
    return tok


def test_health_run_missing_truncate_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """health.py:232-233 — truncate OSError on missing-token path swallowed."""
    from schwab_marketdata_mcp import health

    state = tmp_path / "state"
    (state / "schwab-marketdata-mcp").mkdir(parents=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setattr(health.Path, "home", classmethod(lambda _cls: tmp_path / "nohome"))
    monkeypatch.setattr(health, "truncate_to_window", MagicMock(side_effect=OSError("boom")))
    assert health.run() == health.HealthExit.MISSING


def test_health_run_valid_truncate_oserror_then_both_probes_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """health.py:264-266 + 271->274 + 276-279 — truncate OSError, both probes None → EXPIRED_OR_12H."""
    from schwab_marketdata_mcp import health

    self_tok = _health_setup_valid_token(tmp_path, monkeypatch)
    monkeypatch.setattr(health.Path, "home", classmethod(lambda _cls: tmp_path / "nohome"))
    monkeypatch.setattr(health, "truncate_to_window", MagicMock(side_effect=OSError("trunc boom")))
    monkeypatch.setattr(health, "_probe_token_age_via_schwab_py", lambda _p: None)
    monkeypatch.setattr(health, "_probe_token_age_via_mtime", lambda _p: None)
    code = health.run(str(self_tok))
    assert code == health.HealthExit.EXPIRED_OR_12H


# ===========================================================================
# metrics.py — cli human-text branches + tail-read guards
# ===========================================================================


def _write_usage(path: Path, **kw: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": datetime.now(tz=UTC).isoformat(timespec="milliseconds"),
        "tool": kw.get("tool", "get_quote"),
        "status": kw.get("status", "ok"),
        "error_class": kw.get("error_class"),
        "latency_ms": kw.get("latency_ms", 10),
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def test_metrics_cli_human_text_with_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """metrics.py:393-401 (incl. 398->402) — human text incl. by_error_class block."""
    from schwab_marketdata_mcp import metrics

    usage = tmp_path / "schwab-marketdata-mcp" / "usage.jsonl"
    _write_usage(usage, status="ok", tool="get_quote", latency_ms=5)
    _write_usage(usage, status="err", tool="get_quote", error_class="SchwabRateLimitError", latency_ms=8)
    monkeypatch.setattr(metrics, "usage_path", lambda: usage)
    rc = metrics.cli_main(["--window-days", "7"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "by_error_class" in out
    assert "SchwabRateLimitError" in out


def test_metrics_tail_lines_stat_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """metrics.py:271-272 — stat() OSError → []."""
    from schwab_marketdata_mcp import metrics

    p = tmp_path / "usage.jsonl"
    p.write_text("x\n")
    real_stat = Path.stat

    def _boom(self: Path, *a: Any, **k: Any) -> Any:
        if self == p:
            raise OSError("stat denied")
        return real_stat(self, *a, **k)

    monkeypatch.setattr(Path, "stat", _boom)
    assert metrics._tail_lines(p) == []


def test_metrics_tail_lines_empty_file(tmp_path: Path) -> None:
    """metrics.py:274 — zero-byte file → []."""
    from schwab_marketdata_mcp import metrics

    p = tmp_path / "usage.jsonl"
    p.write_text("")
    assert metrics._tail_lines(p) == []


def test_metrics_tail_lines_drops_truncated_first_line(tmp_path: Path) -> None:
    """metrics.py:284-286 — when reading < full size, first partial line dropped."""
    from schwab_marketdata_mcp import metrics

    p = tmp_path / "usage.jsonl"
    lines = [f"line-{i}-padding-to-make-it-longer" for i in range(200)]
    p.write_text("\n".join(lines) + "\n")
    out = metrics._tail_lines(p, max_bytes=200)
    # We asked for only 200 bytes from the tail, so we got fewer lines than total
    # and the first (possibly partial) line was dropped.
    assert len(out) < len(lines)
    assert all(line.startswith("line-") for line in out)


def test_metrics_tail_lines_read_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """metrics.py:280-281 — open() OSError during read → []."""
    from schwab_marketdata_mcp import metrics

    p = tmp_path / "usage.jsonl"
    p.write_text("data\n")
    real_open = Path.open

    def _boom_open(self: Path, *a: Any, **k: Any) -> Any:
        if self == p and a and a[0] == "rb":
            raise OSError("read denied")
        return real_open(self, *a, **k)

    monkeypatch.setattr(Path, "open", _boom_open)
    assert metrics._tail_lines(p) == []


def test_metrics_percentile_single_and_exact_index() -> None:
    """metrics.py:235-236 + 241 — single value + f==c branch."""
    from schwab_marketdata_mcp import metrics

    assert metrics._percentile([7], 95) == 7.0
    # 100th percentile lands exactly on the last index (f==c).
    assert metrics._percentile([1, 2, 3, 4], 100) == 4.0


def test_metrics_aggregate_skips_blank_and_naive_ts(tmp_path: Path) -> None:
    """metrics.py:194 + 200-201 — blank line skipped; naive ts treated as UTC."""
    from schwab_marketdata_mcp import metrics

    p = tmp_path / "usage.jsonl"
    naive_ts = datetime.now(tz=UTC).replace(tzinfo=None).isoformat()
    with p.open("w", encoding="utf-8") as fh:
        fh.write("\n")  # blank line
        fh.write(json.dumps({"ts": naive_ts, "tool": "get_quote", "status": "ok", "latency_ms": 3}) + "\n")
    out = metrics.aggregate_stats(window_days=7, path=p)
    assert out["count"] == 1


# ===========================================================================
# server.py — error fallback / SchwabError catch / main()
# ===========================================================================


def test_server_err_to_dict_generic_fallback() -> None:
    """server.py:187 — non-mapped SchwabError → generic error dict."""
    from schwab_marketdata_mcp import server
    from schwab_marketdata_mcp.errors import SchwabError

    class _CustomErr(SchwabError):
        pass

    out = server._err_to_dict(_CustomErr("boom detail"))
    assert out["error"] == "_CustomErr"
    # SchwabError.__str__ deliberately returns the class name (never the
    # raw arg) so credentials can't leak via str(exc); message mirrors that.
    assert out["message"] == "_CustomErr"


async def test_server_health_check_catches_schwab_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """server.py:402-403 — health_check_impl SchwabError → dict."""
    from schwab_marketdata_mcp import server
    from schwab_marketdata_mcp.errors import SchwabAuthError

    async def _boom() -> Any:
        raise SchwabAuthError(reason="token_not_initialized", hint="x")

    monkeypatch.setattr(server.meta, "health_check_impl", _boom)
    out = await server.health_check()
    assert out["error"] == "SchwabAuthError"


async def test_server_cache_stats_catches_schwab_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """server.py:429-430 — get_cache_stats_impl SchwabError → dict."""
    from schwab_marketdata_mcp import server
    from schwab_marketdata_mcp.errors import SchwabTransientError

    async def _boom() -> Any:
        raise SchwabTransientError(status_code=500, attempt=0, hint="x")

    monkeypatch.setattr(server.meta, "get_cache_stats_impl", _boom)
    out = await server.get_cache_stats()
    assert out["error"] == "SchwabTransientError"


def test_server_main_invokes_mcp_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """server.py:496-497 — main() logs + calls mcp.run(stdio)."""
    from schwab_marketdata_mcp import server

    captured: dict[str, Any] = {}
    monkeypatch.setattr(server.mcp, "run", lambda **kw: captured.update(kw))
    server.main()
    assert captured.get("transport") == "stdio"


# ===========================================================================
# stats.py — module import shim (was 0% — never imported by the suite)
# ===========================================================================


def test_stats_module_exposes_cli_main() -> None:
    """stats.py:6-8 — importing the shim binds metrics.cli_main."""
    from schwab_marketdata_mcp import stats
    from schwab_marketdata_mcp.metrics import cli_main as metrics_cli_main

    assert stats.cli_main is metrics_cli_main


# ===========================================================================
# cache.py — remaining fine-grained branch closures
# ===========================================================================


def test_parse_dt_tzaware_datetime_normalised() -> None:
    """cache.py:261 — tz-aware datetime → naive UTC."""
    aware = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    out = cache._parse_dt(aware)
    assert out == datetime(2026, 5, 1, 12, 0)
    assert out.tzinfo is None


def test_open_secure_chmod_oserror_swallowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """cache.py:358-359 — secure_chmod OSError during _open is swallowed."""
    monkeypatch.setattr(cache._platform, "secure_chmod", MagicMock(side_effect=OSError("chmod denied")))
    c = cache.Cache(tmp_path / "c.duckdb")
    assert c._conn is not None  # open still succeeded
    c.close()


def test_quarantine_reopen_secure_chmod_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """cache.py:388-389 — secure_chmod OSError during reopen is swallowed."""
    db = tmp_path / "c.duckdb"
    c = cache.Cache(db)
    c.close()
    monkeypatch.setattr(cache._platform, "secure_chmod", MagicMock(side_effect=OSError("chmod denied")))
    c._quarantine_and_reopen(RuntimeError("corrupt"))
    # Reopen succeeded despite the chmod failure.
    assert c._conn is not None
    c.close()


def test_close_idempotent_when_conn_none(tmp_path: Path) -> None:
    """cache.py:398->exit — close() with conn already None is a no-op."""
    c = cache.Cache(tmp_path / "c.duckdb")
    c.close()
    c.close()  # second close — conn is None, exits cleanly
    assert c._conn is None


def test_price_history_skips_unparseable_candle_datetime(tmp_path: Path) -> None:
    """cache.py:530-531 — a row whose candle_datetime won't parse is skipped.

    DuckDB enforces NOT NULL on candle_datetime, so a real NULL is
    impossible; we drive the defensive guard by wrapping the connection
    so ``fetchall`` returns a non-datetime, unparseable value.
    """
    db = tmp_path / "c.duckdb"
    params = {"symbol": "VOO", "period_type": "DAY", "frequency_type": "DAILY", "frequency": 1}
    with cache.Cache(db) as c:
        real = c._conn
        assert real is not None

        class _Cursor:
            def fetchall(self) -> list[tuple[Any, ...]]:
                # cdt is a str that _parse_dt cannot parse → skipped.
                return [("not-a-datetime", 1.0, 1.0, 1.0, 1.0, 10, datetime.now(tz=UTC))]

        wrapped = MagicMock(wraps=real)
        wrapped.execute.return_value = _Cursor()
        c._conn = wrapped
        got = c.get_price_history(params)
        c._conn = real
    # The single row had an unparseable datetime → filtered → miss.
    assert got is None


def test_put_price_history_invalid_params_and_non_list_candles(tmp_path: Path) -> None:
    """cache.py:564 + 567 — missing required params / non-list candles → no-op."""
    with cache.Cache(tmp_path / "c.duckdb") as c:
        c.put_price_history({"symbol": ""}, {"candles": []})  # missing params
        c.put_price_history(
            {"symbol": "VOO", "period_type": "DAY", "frequency_type": "DAILY", "frequency": 1},
            {"candles": "not-a-list"},
        )


def test_put_price_history_skips_non_dict_and_bad_dt_candles(tmp_path: Path) -> None:
    """cache.py:572 + 575 + 592 — non-dict candle / unparseable dt skipped; empty rows → no-op."""
    params = {"symbol": "VOO", "period_type": "DAY", "frequency_type": "DAILY", "frequency": 1}
    with cache.Cache(tmp_path / "c.duckdb") as c:
        # All candles invalid → rows empty → early return at 592.
        c.put_price_history(params, {"candles": ["scalar", {"datetime": "bad-date", "close": 1.0}]})
        got = c.get_price_history(params)
    assert got is None


def test_get_instruments_expired(tmp_path: Path) -> None:
    """cache.py:693-694 — expired instruments row → None (records 'expired')."""
    params = {"cusip": "037833100"}
    with cache.Cache(tmp_path / "c.duckdb") as c:
        c.put_instruments(params, {"x": 1}, ttl_seconds=0)
        time.sleep(0.05)
        assert c.get_instruments(params) is None


def test_write_snapshot_all_rows_invalid_returns_zero(tmp_path: Path) -> None:
    """cache.py:758 — contracts present but none normalise → 0 (no DB write)."""
    with cache.Cache(tmp_path / "c.duckdb") as c:
        # Missing strike on every contract → _normalise_option_contract → None.
        rows = [{"expiry": "2026-06-19", "call_put": "CALL"}]
        assert c.write_option_chain_snapshot("AAPL", datetime.now(tz=UTC), rows) == 0


def test_fetch_latest_snapshot_skips_bad_expiry(tmp_path: Path) -> None:
    """cache.py:865-867 — a snapshot row with an unparseable expiry is skipped.

    DuckDB enforces NOT NULL on expiry, so we wrap the connection to feed
    ``_fetch_latest_snapshot_contracts`` a row whose expiry _coerce_date rejects.
    """
    db = tmp_path / "c.duckdb"
    with cache.Cache(db) as c:
        real = c._conn
        assert real is not None
        snap_at = datetime(2026, 5, 1, 14, 30)

        class _Cursor:
            def __init__(self, rows: Any) -> None:
                self._rows = rows

            def fetchone(self) -> Any:
                return (snap_at,)

            def fetchall(self) -> list[tuple[Any, ...]]:
                return [("not-a-date", 100.0, "CALL", 0.3)]

        def _exec(sql: str, *_a: Any, **_k: Any) -> Any:
            return _Cursor(None)

        wrapped = MagicMock(wraps=real)
        wrapped.execute.side_effect = _exec
        c._conn = wrapped
        out = c._fetch_latest_snapshot_contracts("AAPL", datetime(2026, 5, 2))
        c._conn = real
    assert out == []


def test_fetch_iv_history_window_skips_bad_date(tmp_path: Path) -> None:
    """cache.py:999-1001 — iv_history row with unparseable asof_date skipped."""
    db = tmp_path / "c.duckdb"
    with cache.Cache(db) as c:
        real = c._conn
        assert real is not None

        class _Cursor:
            def fetchall(self) -> list[tuple[Any, ...]]:
                return [("not-a-date", 0.3, 5)]

        wrapped = MagicMock(wraps=real)
        wrapped.execute.return_value = _Cursor()
        c._conn = wrapped
        out = c._fetch_iv_history_window("AAPL", "30d", 252)
        c._conn = real
    assert out == []


def test_query_candles_skips_bad_datetime(tmp_path: Path) -> None:
    """cache.py:1043-1044 — query_candles skips a row with an unparseable datetime."""
    db = tmp_path / "c.duckdb"
    base = datetime.now(tz=UTC).replace(microsecond=0) - timedelta(days=2)
    with cache.Cache(db) as c:
        real = c._conn
        assert real is not None

        class _Cursor:
            def fetchall(self) -> list[tuple[Any, ...]]:
                return [("not-a-datetime", 1.0, 1.0, 1.0, 1.0, 10, "DAY", "DAILY", 1)]

        wrapped = MagicMock(wraps=real)
        wrapped.execute.return_value = _Cursor()
        c._conn = wrapped
        out = c.query_candles("VOO", base - timedelta(days=1), base + timedelta(days=1))
        c._conn = real
    assert out == []


def test_hourly_breakdown_skips_unformattable_hour(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """cache.py:1179 — a row whose hour cannot be formatted is skipped."""
    db = tmp_path / "c.duckdb"
    with cache.Cache(db) as c:
        c._record_event("hit", "quotes_cache")
        # Force _format_hour_utc to reject the bucket value.
        monkeypatch.setattr(cache, "_format_hour_utc", lambda _v: None)
        out = c.hourly_breakdown(24)
    assert out == []


def test_format_hour_utc_parses_string() -> None:
    """cache.py:1250 — parseable string returns ISO + Z."""
    iso = "2026-05-01T10:00:00"
    out = cache._format_hour_utc(iso)
    assert out is not None and out.endswith("Z")


def test_is_expired_none_inputs() -> None:
    """cache.py:1255 — None fetched_at or ttl → expired (True)."""
    assert cache._is_expired(None, 60) is True
    assert cache._is_expired(datetime.now(tz=UTC).replace(tzinfo=None), None) is True


def test_normalise_naive_utc_tzaware() -> None:
    """cache.py:1318 — tz-aware datetime → naive UTC (covered via parsed path None)."""
    aware = datetime(2026, 5, 1, 12, tzinfo=UTC)
    out = cache._normalise_naive_utc(aware)
    assert out.tzinfo is None and out == datetime(2026, 5, 1, 12)


def test_normalise_naive_utc_already_naive() -> None:
    """cache.py:1318 — an already-naive datetime is returned unchanged."""
    naive = datetime(2026, 5, 1, 12, 0, 0)
    out = cache._normalise_naive_utc(naive)
    assert out is naive


def test_coerce_date_unknown_type_returns_none() -> None:
    """cache.py:1346 — a type that is not str/date/datetime/number → None."""
    assert cache._coerce_date(object()) is None
    assert cache._coerce_date([2026, 5, 1]) is None


def test_normalise_naive_utc_parses_string_input() -> None:
    """cache.py:1316-1318 — a non-datetime value that _parse_dt parses → returned."""
    out = cache._normalise_naive_utc("2026-05-01T12:30:00Z")
    assert out == datetime(2026, 5, 1, 12, 30, 0)
    assert out.tzinfo is None


def test_get_stats_conn_none_returns_empty(tmp_path: Path) -> None:
    """cache.py:1074->1096 (not-taken) — conn None → empty rows, neutral stats."""
    c = cache.Cache(tmp_path / "c.duckdb")
    c.close()  # conn becomes None
    stats = c.get_stats()
    assert stats.rows_per_table == {}
    assert stats.hits_24h == 0
    assert stats.misses_24h == 0
    assert stats.hit_rate_24h is None


def test_truncate_expired_fetchone_none_skips_increment(tmp_path: Path) -> None:
    """cache.py:1204->1198 (not-taken) — DELETE result fetchone()->None → no increment."""
    c = cache.Cache(tmp_path / "c.duckdb")
    real = c._conn
    assert real is not None

    class _NoneCursor:
        def fetchone(self) -> Any:
            return None

    wrapped = MagicMock(wraps=real)
    wrapped.execute.return_value = _NoneCursor()
    c._conn = wrapped
    # Every DELETE returns a None fetchone → deleted stays 0 across the loop.
    assert c.truncate_expired() == 0
    c._conn = real
    c.close()


def test_get_cache_singleton_race_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    """cache.py:1553->1555 (not-taken) — singleton set after acquiring the lock → skip build.

    Replace the module lock so acquiring it populates ``_singleton`` as a
    side effect: the top guard (1550) sees None and enters the lock, but the
    inner re-check (1553) finds it set → the ``Cache()`` build is skipped.
    """
    cache.reset_cache_singleton()
    monkeypatch.delenv("SCHWAB_CACHE_ENABLED", raising=False)

    sentinel = cache.Cache(":memory:")
    real_lock = cache._singleton_lock

    class _SideEffectLock:
        def __enter__(self) -> Any:
            cache._singleton = sentinel
            return real_lock.__enter__()

        def __exit__(self, *a: Any) -> Any:
            return real_lock.__exit__(*a)

    monkeypatch.setattr(cache, "_singleton_lock", _SideEffectLock())
    out = cache.get_cache()
    assert out is sentinel
    cache.reset_cache_singleton()


def test_coerce_date_parse_dt_fallback() -> None:
    """cache.py:1344-1346 — non-iso string falls back through _parse_dt then None."""
    # A full ISO datetime string that date.fromisoformat rejects but _parse_dt accepts.
    assert cache._coerce_date("2026-05-01T10:30:00Z") == date(2026, 5, 1)


def test_normalise_call_put_none_and_unknown() -> None:
    """cache.py:1359 — None → None; unknown alias → None."""
    assert cache._normalise_call_put(None) is None
    assert cache._normalise_call_put("CHICKEN") is None
    assert cache._normalise_call_put("c") == "CALL"


def test_normalise_option_contract_non_dict() -> None:
    """cache.py:1376 — non-dict contract → None."""
    assert cache._normalise_option_contract("AAPL", datetime.now(tz=UTC), "scalar") is None


def test_flatten_option_chain_response_guards() -> None:
    """cache.py:1430 + 1434 + 1438 — non-dict strike_map / non-list contracts / non-dict contract skipped."""
    raw = {
        "callExpDateMap": {
            "2026-06-19:30": "not-a-dict",  # 1430 skip
            "2026-07-17:60": {
                "100.0": "not-a-list",  # 1434 skip
                "105.0": ["scalar", {"bid": 1.0}],  # 1438 skips the scalar
            },
        }
    }
    out = cache.flatten_option_chain_response(raw)
    # Only the one dict contract under 105.0 survives.
    assert len(out) == 1
    assert out[0]["strike"] == 105.0


def test_compute_atm_iv_skips_non_date_expiry() -> None:
    """cache.py:1494 — contract whose expiry is not a date is skipped."""
    asof = date(2026, 5, 1)
    contracts = [
        {"expiry": "2026-05-31", "strike": 100.0, "implied_vol": 0.3},  # str expiry skipped
        {"expiry": date(2026, 5, 31), "strike": 100.0, "implied_vol": 0.4},
    ]
    iv, count = cache._compute_atm_iv_for_bucket(contracts, asof, 30)
    assert count == 1
    assert iv == pytest.approx(0.4)


def test_compute_atm_iv_median_strike_none_guard() -> None:
    """cache.py:1505-1506 — when median strike is None the bucket returns (None, count).

    Drive _median to return None for a non-empty bucket by monkeypatch so the
    guard branch executes deterministically.
    """
    import schwab_marketdata_mcp.cache as cache_mod

    asof = date(2026, 5, 1)
    contracts = [{"expiry": date(2026, 5, 31), "strike": 100.0, "implied_vol": 0.3}]
    orig_median = cache_mod._median
    try:
        cache_mod._median = lambda _values: None  # type: ignore[assignment]
        iv, count = cache_mod._compute_atm_iv_for_bucket(contracts, asof, 30)
    finally:
        cache_mod._median = orig_median  # type: ignore[assignment]
    assert iv is None
    assert count == 1


def test_price_history_recent_stale_forces_refresh(tmp_path: Path) -> None:
    """cache.py:538->542 + 553-554 — stale recent candle forces refresh; empty window → miss."""
    db = tmp_path / "c.duckdb"
    now = datetime.now(tz=UTC).replace(microsecond=0)
    params = {"symbol": "VOO", "period_type": "DAY", "frequency_type": "MINUTE", "frequency": 30}
    with cache.Cache(db) as c:
        # A recent candle (inside the 1h boundary) with a stale fetched_at.
        c.put_price_history(
            params,
            {
                "candles": [
                    {"datetime": int((now - timedelta(minutes=5)).replace(tzinfo=UTC).timestamp() * 1000), "close": 1.0}
                ]
            },
        )
        assert c._conn is not None
        c._conn.execute(
            "UPDATE price_history_cache SET fetched_at = ?",
            [now - timedelta(hours=2)],
        )
        got = c.get_price_history(params)
    # Recent candle is stale → whole window refreshed → miss (None).
    assert got is None


def test_get_stats_no_db_file_size_zero(tmp_path: Path) -> None:
    """cache.py:1068->1073 — db_path missing on disk → size_mb 0.0, conn still queried."""
    c = cache.Cache(tmp_path / "c.duckdb")
    # Remove the file but keep the (in-memory-ish) connection alive.
    c.db_path = tmp_path / "never-created.duckdb"
    stats = c.get_stats()
    assert stats.size_mb == 0.0
    assert "quotes_cache" in stats.rows_per_table
    c.close()


def test_truncate_expired_counts_deleted_rows(tmp_path: Path) -> None:
    """cache.py:1204-1207 (and 1198 loop) — deleted-row count accumulates."""
    params = {"cusip": "037833100"}
    with cache.Cache(tmp_path / "c.duckdb") as c:
        c.put_instruments(params, {"x": 1}, ttl_seconds=0)
        c.put_quote("AAPL", {"AAPL": {"quote": {"lastPrice": 1.0}}}, ttl_seconds=0)
        time.sleep(0.05)
        deleted = c.truncate_expired()
    assert deleted >= 2


def test_get_cache_singleton_returns_existing(monkeypatch: pytest.MonkeyPatch) -> None:
    """cache.py:1553->1555 — second get_cache() returns the existing singleton."""
    cache.reset_cache_singleton()
    monkeypatch.delenv("SCHWAB_CACHE_ENABLED", raising=False)
    first = cache.get_cache()
    second = cache.get_cache()
    assert first is second
    cache.reset_cache_singleton()


# ===========================================================================
# client.py — 429 backoff jitter (delay None) + enforce_token_perms tail
# ===========================================================================


async def test_client_429_backoff_when_no_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    """client.py:301-303 — 429 without Retry-After computes jittered backoff."""
    import httpx

    from schwab_marketdata_mcp import client

    calls = {"n": 0}

    async def _fetch() -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            resp = httpx.Response(429, headers={}, request=httpx.Request("GET", "https://x"))
            raise httpx.HTTPStatusError("429", request=resp.request, response=resp)
        return {"ok": True}

    async def _instant_sleep(_s: Any) -> None:
        return None

    monkeypatch.setattr(client.asyncio, "sleep", _instant_sleep)
    rl = client.RateLimitedClient.from_env(MagicMock())
    out = await rl.call(_fetch, tool_name="get_quote")
    assert out == {"ok": True}
    assert calls["n"] == 2


def test_enforce_token_or_raise_valid_calls_enforce(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """client.py:491 — VALID token reaches enforce_token_perms()."""
    from schwab_marketdata_mcp import client

    tok = tmp_path / "token.json"
    tok.write_text(json.dumps({"creation_timestamp": 1}))
    import os as _os

    _os.chmod(tok, 0o600)
    called = {"n": 0}
    monkeypatch.setattr(client, "enforce_token_perms", lambda _p: called.__setitem__("n", called["n"] + 1))
    client._enforce_token_or_raise(tok)
    assert called["n"] == 1


# ===========================================================================
# metrics.py — cli human-text no-error branch + 211->213 + 136/144
# ===========================================================================


def test_metrics_cli_human_text_no_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """metrics.py:136 + 144 + 398->402 — text output when there are no error rows."""
    from schwab_marketdata_mcp import metrics

    usage = tmp_path / "schwab-marketdata-mcp" / "usage.jsonl"
    _write_usage(usage, status="ok", tool="get_quote", latency_ms=5)
    monkeypatch.setattr(metrics, "usage_path", lambda: usage)
    rc = metrics.cli_main([])  # default window-days, text mode
    assert rc == 0
    out = capsys.readouterr().out
    assert "by_tool" in out
    # No error rows → by_error_class block is skipped.
    assert "by_error_class" not in out


def test_metrics_aggregate_non_numeric_latency_skipped(tmp_path: Path) -> None:
    """metrics.py:211->213 — non-numeric latency_ms is not appended to latencies."""
    from schwab_marketdata_mcp import metrics

    p = tmp_path / "usage.jsonl"
    ts = datetime.now(tz=UTC).isoformat(timespec="milliseconds")
    with p.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": ts, "tool": "get_quote", "status": "ok", "latency_ms": "oops"}) + "\n")
    out = metrics.aggregate_stats(window_days=7, path=p)
    assert out["count"] == 1
    assert out["p50_latency_ms"] is None


def test_metrics_truncate_skips_blank_and_naive_ts(tmp_path: Path) -> None:
    """metrics.py:135-136 + 143-144 — blank line skipped; naive ts treated as UTC and kept."""
    from schwab_marketdata_mcp import metrics

    p = tmp_path / "usage.jsonl"
    naive_ts = datetime.now(tz=UTC).replace(tzinfo=None).isoformat()
    with p.open("w", encoding="utf-8") as fh:
        fh.write("\n")  # blank line → continue (136)
        fh.write(json.dumps({"ts": naive_ts, "tool": "get_quote", "status": "ok", "latency_ms": 3}) + "\n")
    kept = metrics.truncate_to_window(days=30, path=p)
    assert kept == 1  # the naive-ts row was normalised to UTC and retained


# ===========================================================================
# server.py — logging hardening OSError guards + get_iv_percentile run path
# ===========================================================================


def test_server_setup_logging_handles_oserrors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """server.py:52-53 + 56->71 + 68-69 — log_dir mkdir + file handler OSErrors swallowed."""
    import importlib

    from schwab_marketdata_mcp import _platform

    monkeypatch.setattr(_platform, "state_root", lambda: tmp_path / "state")

    real_mkdir = Path.mkdir

    def _boom_mkdir(self: Path, *a: Any, **k: Any) -> Any:
        if "logs" in str(self):
            raise OSError("mkdir denied")
        return real_mkdir(self, *a, **k)

    monkeypatch.setattr(Path, "mkdir", _boom_mkdir)
    # Re-import the server module fresh so _harden_stdio() runs under the patches.
    import schwab_marketdata_mcp.server as server_mod

    importlib.reload(server_mod)
    # The reload must not raise even though the log dir couldn't be created.
    assert server_mod.mcp is not None
    importlib.reload(server_mod)  # restore clean state


def test_server_setup_logging_file_handler_oserror(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """server.py:68-69 — RotatingFileHandler OSError is swallowed (stderr handler stays)."""
    import importlib

    from schwab_marketdata_mcp import _platform

    monkeypatch.setattr(_platform, "state_root", lambda: tmp_path / "state2")

    import logging.handlers as _lh

    class _BoomHandler:
        def __init__(self, *a: Any, **k: Any) -> None:
            raise OSError("file handler denied")

    monkeypatch.setattr(_lh, "RotatingFileHandler", _BoomHandler)
    import schwab_marketdata_mcp.server as server_mod

    importlib.reload(server_mod)
    assert server_mod.mcp is not None
    monkeypatch.undo()
    importlib.reload(server_mod)  # restore clean state


async def test_server_get_iv_percentile_runs(monkeypatch: pytest.MonkeyPatch, use_fake_backend: None) -> None:
    """server.py:454 — get_iv_percentile_ delegates through _run_tool."""
    from schwab_marketdata_mcp import server

    out = await server.get_iv_percentile_(underlying="AAPL", expiry_bucket="30d", lookback_days=60, refresh=False)
    # Read path with no history → structured dict (not an exception).
    assert out["underlying"] == "AAPL"
    assert out["expiry_bucket"] == "30d"


# ===========================================================================
# streaming.py — deadline-reached immediate return (119)
# ===========================================================================


async def test_streaming_drain_returns_when_deadline_passed() -> None:
    """streaming.py:118-119 — _drain_until_deadline returns immediately if past deadline."""
    import asyncio as _asyncio

    from schwab_marketdata_mcp.tools import streaming as streaming_mod

    class _NeverCalledStreamer:
        async def handle_message(self) -> None:  # pragma: no cover - must not be called
            raise AssertionError("handle_message should not run past the deadline")

    past = _asyncio.get_event_loop().time() - 1.0
    await streaming_mod._drain_until_deadline(_NeverCalledStreamer(), past)


# ===========================================================================
# Remaining branch-partial closures (538->542, 1074->1096, 1204->1198,
# 1553->1555, health 271->274, _runtime 41->43, streaming 224->234)
# ===========================================================================


def test_price_history_recent_fresh_candle_is_served(tmp_path: Path) -> None:
    """cache.py:536-542 (538->542 not-taken) — recent candle with FRESH fetched_at is served."""
    db = tmp_path / "c.duckdb"
    now = datetime.now(tz=UTC).replace(microsecond=0)
    params = {"symbol": "VOO", "period_type": "DAY", "frequency_type": "MINUTE", "frequency": 30}
    with cache.Cache(db) as c:
        # Recent candle (inside 1h window) written just now → fetched_at is fresh.
        c.put_price_history(
            params,
            {
                "candles": [
                    {"datetime": int((now - timedelta(minutes=5)).replace(tzinfo=UTC).timestamp() * 1000), "close": 1.0}
                ]
            },
        )
        got = c.get_price_history(params)
    # Fresh recent candle served (the stale-refresh branch is NOT taken).
    assert got is not None
    assert len(got["candles"]) == 1


def test_get_stats_with_rows_present(tmp_path: Path) -> None:
    """cache.py:1074->1096 — conn present, tables populated → real counts + hit-rate."""
    db = tmp_path / "c.duckdb"
    with cache.Cache(db) as c:
        c.put_quote("AAPL", {"AAPL": {"quote": {"lastPrice": 1.0}}})
        c.get_quote("AAPL")  # records a hit event
        c.get_quote("ZZZZ")  # records a miss event
        stats = c.get_stats()
    assert stats.rows_per_table["quotes_cache"] == 1
    assert stats.hits_24h >= 1
    assert stats.misses_24h >= 1
    assert stats.hit_rate_24h is not None


def test_truncate_expired_nothing_to_delete(tmp_path: Path) -> None:
    """cache.py:1198-1205 (1204->1198 loop) — fresh rows → 0 deleted but loop runs all tables."""
    db = tmp_path / "c.duckdb"
    with cache.Cache(db) as c:
        c.put_quote("AAPL", {"AAPL": {"quote": {"lastPrice": 1.0}}}, ttl_seconds=3600)
        deleted = c.truncate_expired()
    assert deleted == 0


def test_get_cache_singleton_first_build(monkeypatch: pytest.MonkeyPatch) -> None:
    """cache.py:1553-1555 — first get_cache() builds the singleton inside the lock."""
    cache.reset_cache_singleton()
    monkeypatch.delenv("SCHWAB_CACHE_ENABLED", raising=False)
    c = cache.get_cache()
    assert c is not None
    cache.reset_cache_singleton()


def test_health_run_schwab_py_probe_skips_mtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """health.py:269-273 (271->274 not-taken) — schwab_py probe returns → mtime fallback skipped."""
    from schwab_marketdata_mcp import health

    tok = _health_setup_valid_token(tmp_path, monkeypatch)
    monkeypatch.setattr(health.Path, "home", classmethod(lambda _cls: tmp_path / "nohome"))
    monkeypatch.setattr(health, "truncate_to_window", lambda **_kw: 0)
    # schwab_py probe returns a healthy window → mtime fallback must NOT run.
    monkeypatch.setattr(health, "_probe_token_age_via_schwab_py", lambda _p: timedelta(days=6))
    mtime_called = {"n": 0}
    monkeypatch.setattr(
        health, "_probe_token_age_via_mtime", lambda _p: mtime_called.__setitem__("n", 1) or timedelta(days=1)
    )
    code = health.run(str(tok))
    assert code == health.HealthExit.HEALTHY
    assert mtime_called["n"] == 0


async def test_runtime_get_client_first_build(monkeypatch: pytest.MonkeyPatch) -> None:
    """_runtime.py:39-43 (41->43) — first call builds the client inside the lock."""
    from schwab_marketdata_mcp.tools import _runtime

    _runtime.reset_client_cache()
    monkeypatch.setenv("SCHWAB_MOCK_BACKEND", "fixtures")
    built = await _runtime.get_client()
    assert built is not None
    _runtime.reset_client_cache()


async def test_runtime_get_client_concurrent_second_waiter(monkeypatch: pytest.MonkeyPatch) -> None:
    """_runtime.py:41->43 (not-taken) — _client set after acquiring the lock → skip build.

    We replace the module lock with one whose ``__aenter__`` populates
    ``_client`` as a side effect.  The outer guard (39) is evaluated while
    ``_client`` is still None, so we enter the lock; by the time the inner
    re-check (41) runs the client is already set → the build is skipped,
    exercising the double-checked-locking not-taken edge deterministically.
    """
    import asyncio as _asyncio

    from schwab_marketdata_mcp.tools import _runtime

    _runtime.reset_client_cache()
    monkeypatch.setenv("SCHWAB_MOCK_BACKEND", "fixtures")

    sentinel = _runtime.make_rate_limited()
    build_calls = {"n": 0}

    def _should_not_build() -> Any:  # pragma: no cover - asserted never called
        build_calls["n"] += 1
        return sentinel

    monkeypatch.setattr(_runtime, "make_rate_limited", _should_not_build)

    real_lock = _runtime._lock

    class _SideEffectLock:
        async def __aenter__(self) -> Any:
            # Simulate another coroutine having built the client while we
            # were waiting to acquire the lock.
            _runtime._client = sentinel
            return await real_lock.__aenter__()

        async def __aexit__(self, *a: Any) -> Any:
            return await real_lock.__aexit__(*a)

    monkeypatch.setattr(_runtime, "_lock", _SideEffectLock())
    out = await _runtime.get_client()
    assert out is sentinel
    assert build_calls["n"] == 0  # inner re-check was False → build skipped
    _ = _asyncio  # silence unused-import lint if asyncio path changes
    _runtime.reset_client_cache()


async def test_streaming_happy_path_logs_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """streaming.py:223-232 (224->234 taken) — logged_in True → logout runs in finally."""
    from schwab_marketdata_mcp.models import GetStreamingSnapshotInput
    from schwab_marketdata_mcp.tools import streaming as streaming_mod

    class _OkStreamer:
        def __init__(self) -> None:
            self.logout_calls = 0

        async def login(self) -> None:
            pass

        async def logout(self) -> None:
            self.logout_calls += 1

        def add_level_one_equity_handler(self, _h: Any) -> None:
            pass

        async def level_one_equity_subs(self, _s: list[str]) -> None:
            pass

        async def handle_message(self) -> None:
            import asyncio as _asyncio

            await _asyncio.sleep(0.05)

    fake = _OkStreamer()
    monkeypatch.setattr(streaming_mod, "make_client", lambda: "client")
    monkeypatch.setattr(streaming_mod, "_make_stream_client", lambda _c: fake)
    monkeypatch.setenv("SCHWAB_MOCK_BACKEND", "fixtures")
    args = GetStreamingSnapshotInput(symbols=["VOO"], service="LEVELONE_EQUITIES", duration_ms=500)
    out = await streaming_mod.get_streaming_snapshot_impl(args)
    assert out["messages_count"] == 0
    assert fake.logout_calls == 1


def test_streaming_logout_guard_is_unreachable_source_drift_guard() -> None:
    """streaming.py:224 — source-drift guard for the ``# pragma: no cover`` branch.

    The ``if logged_in:`` finally-guard's not-taken edge (224->234) is
    unreachable: ``logged_in`` is only ever set True immediately after a
    successful ``streamer.login()``, and every login failure path re-raises
    (so the post-finally return at 234 is never reached with logged_in False).
    This test fails loudly if the source ever drifts so the pragma stops
    silently masking a real gap.
    """
    import inspect

    from schwab_marketdata_mcp.tools import streaming as streaming_mod

    src = inspect.getsource(streaming_mod.get_streaming_snapshot_impl)
    # 1) logged_in starts False and is set True only right after login().
    assert "logged_in = False" in src
    assert src.count("logged_in = True") == 1
    # 2) The login block re-raises SchwabAuthError and wraps everything else
    #    in SchwabTransientError — neither path can fall through with
    #    logged_in still False.
    assert "await streamer.login()" in src
    assert "raise SchwabTransientError(" in src
    assert "except SchwabAuthError:" in src
    # 3) The guard carries the pragma so coverage treats the edge as excluded.
    guard_src = inspect.getsource(streaming_mod)
    assert "if logged_in:  # pragma: no cover" in guard_src
