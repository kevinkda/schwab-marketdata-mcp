"""``metrics.py`` unit tests — record / aggregate / truncate / cli.

Plan §3.4.3 / §6.4.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from schwab_marketdata_mcp import metrics


@pytest.mark.posix_only
def test_record_creates_file_with_secure_perms(tmp_path: Path) -> None:
    p = tmp_path / "u.jsonl"
    metrics.record(tool="get_quote", status="ok", error_class=None, latency_ms=10, path=p)
    assert p.exists()
    import stat

    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_record_invalid_status_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        metrics.record(
            tool="get_quote",
            status="weird",
            error_class=None,
            latency_ms=10,
            path=tmp_path / "u.jsonl",
        )


def test_record_swallows_oserror(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Failure to write metrics must NOT raise to the caller."""
    bad = tmp_path / "doesnt-exist" / "u.jsonl"
    bad.parent.mkdir()
    bad.parent.chmod(0o000)
    try:
        metrics.record(tool="x", status="ok", error_class=None, latency_ms=1, path=bad)
        out = capsys.readouterr().err
        assert "metrics_write_failed" in out
    finally:
        bad.parent.chmod(0o700)


def test_time_tool_records_ok(tmp_path: Path) -> None:
    p = tmp_path / "u.jsonl"
    with metrics.time_tool("get_quote", path=p):
        pass
    line = p.read_text().strip()
    obj = json.loads(line)
    assert obj["tool"] == "get_quote"
    assert obj["status"] == "ok"


def test_time_tool_records_err_and_reraises(tmp_path: Path) -> None:
    p = tmp_path / "u.jsonl"
    with pytest.raises(RuntimeError, match="boom"), metrics.time_tool("get_quote", path=p):
        raise RuntimeError("boom")
    obj = json.loads(p.read_text().strip())
    assert obj["status"] == "err"
    assert obj["error_class"] == "RuntimeError"


def test_aggregate_stats_empty_path(tmp_path: Path) -> None:
    s = metrics.aggregate_stats(window_days=7, path=tmp_path / "missing.jsonl")
    assert s["count"] == 0
    assert s["p50_latency_ms"] is None


def test_aggregate_stats_filters_window(tmp_path: Path) -> None:
    p = tmp_path / "u.jsonl"
    old_ts = (datetime.now(tz=UTC) - timedelta(days=10)).isoformat(timespec="milliseconds")
    new_ts = datetime.now(tz=UTC).isoformat(timespec="milliseconds")
    with p.open("w") as fh:
        fh.write(json.dumps({"ts": old_ts, "tool": "x", "status": "ok", "error_class": None, "latency_ms": 1}) + "\n")
        fh.write(json.dumps({"ts": new_ts, "tool": "y", "status": "err", "error_class": "X", "latency_ms": 2}) + "\n")
        fh.write("malformed\n")  # exercises the dropped-line branch
    s = metrics.aggregate_stats(window_days=1, path=p)
    assert s["count"] == 1
    assert "y" in s["by_tool"]
    assert "X" in s["by_error_class"]


def test_aggregate_stats_window_invalid(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        metrics.aggregate_stats(window_days=0, path=tmp_path / "u.jsonl")


def test_truncate_to_window(tmp_path: Path) -> None:
    p = tmp_path / "u.jsonl"
    old = (datetime.now(tz=UTC) - timedelta(days=40)).isoformat(timespec="milliseconds")
    new = datetime.now(tz=UTC).isoformat(timespec="milliseconds")
    with p.open("w") as fh:
        fh.write(json.dumps({"ts": old, "tool": "x", "status": "ok"}) + "\n")
        fh.write(json.dumps({"ts": new, "tool": "y", "status": "ok"}) + "\n")
        fh.write("garbage\n")
    kept = metrics.truncate_to_window(days=30, path=p)
    assert kept == 1
    txt = p.read_text()
    assert "y" in txt
    assert "x" not in txt
    assert "garbage" not in txt


def test_truncate_to_window_missing(tmp_path: Path) -> None:
    assert metrics.truncate_to_window(days=30, path=tmp_path / "missing.jsonl") == 0


def test_truncate_to_window_invalid_days(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        metrics.truncate_to_window(days=0, path=tmp_path / "u.jsonl")


def test_recent_error_count_24h(tmp_path: Path) -> None:
    p = tmp_path / "u.jsonl"
    metrics.record(tool="x", status="err", error_class="X", latency_ms=1, path=p)
    metrics.record(tool="x", status="ok", error_class=None, latency_ms=1, path=p)
    assert metrics.recent_error_count_24h(p) == 1


def test_percentile_edges() -> None:
    assert metrics._percentile([], 50) == 0.0
    assert metrics._percentile([42], 95) == 42.0
    with pytest.raises(ValueError):
        metrics._percentile([1, 2], 0)
    with pytest.raises(ValueError):
        metrics._percentile([1, 2], 101)


def test_cli_main_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    p = tmp_path / "state" / "schwab-marketdata-mcp" / "usage.jsonl"
    p.parent.mkdir(parents=True)
    metrics.record(tool="x", status="ok", error_class=None, latency_ms=1, path=p)
    metrics.record(tool="x", status="err", error_class="ER", latency_ms=2, path=p)
    rc = metrics.cli_main(["--window-days", "1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "by_tool" in out
    assert "by_error_class" in out


def test_cli_main_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    rc = metrics.cli_main(["--window-days", "1", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["window_days"] == 1


# ---------------------------------------------------------------------------
# recent_errors_window — Sprint v0.3 task #3 (observability candidate E)
# ---------------------------------------------------------------------------


def _write_usage_line(path: Path, **kwargs: object) -> None:
    payload = {
        "ts": kwargs.get("ts", datetime.now(tz=UTC).isoformat(timespec="milliseconds")),
        "tool": kwargs.get("tool", "get_quote"),
        "status": kwargs.get("status", "ok"),
        "error_class": kwargs.get("error_class"),
        "latency_ms": kwargs.get("latency_ms", 1),
    }
    if "extra" in kwargs and isinstance(kwargs["extra"], dict):
        payload.update(kwargs["extra"])
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload) + "\n")


def test_recent_errors_empty_file(tmp_path: Path) -> None:
    """Missing file returns the empty-baseline shape, never raises."""
    out = metrics.recent_errors_window(window_minutes=5, last_n=5, path=tmp_path / "missing.jsonl")
    assert out["total_calls"] == 0
    assert out["error_count"] == 0
    assert out["error_rate"] == 0.0
    assert out["last_errors"] == []


def test_recent_errors_only_ok(tmp_path: Path) -> None:
    p = tmp_path / "u.jsonl"
    for _ in range(3):
        _write_usage_line(p, status="ok")
    out = metrics.recent_errors_window(window_minutes=5, last_n=5, path=p)
    assert out["total_calls"] == 3
    assert out["error_count"] == 0
    assert out["error_rate"] == 0.0
    assert out["last_errors"] == []


def test_recent_errors_only_err(tmp_path: Path) -> None:
    p = tmp_path / "u.jsonl"
    for _ in range(2):
        _write_usage_line(p, status="err", error_class="SchwabRateLimitError")
    out = metrics.recent_errors_window(window_minutes=5, last_n=5, path=p)
    assert out["total_calls"] == 2
    assert out["error_count"] == 2
    assert out["error_rate"] == 1.0
    assert len(out["last_errors"]) == 2
    assert all(e["error_class"] == "SchwabRateLimitError" for e in out["last_errors"])


def test_recent_errors_mixed_within_window(tmp_path: Path) -> None:
    p = tmp_path / "u.jsonl"
    for _ in range(8):
        _write_usage_line(p, status="ok")
    for _ in range(2):
        _write_usage_line(p, status="err", error_class="X")
    out = metrics.recent_errors_window(window_minutes=5, last_n=5, path=p)
    assert out["total_calls"] == 10
    assert out["error_count"] == 2
    assert abs(out["error_rate"] - 0.2) < 1e-9


def test_recent_errors_excludes_outside_window(tmp_path: Path) -> None:
    p = tmp_path / "u.jsonl"
    old_ts = (datetime.now(tz=UTC) - timedelta(minutes=30)).isoformat(timespec="milliseconds")
    new_ts = datetime.now(tz=UTC).isoformat(timespec="milliseconds")
    _write_usage_line(p, ts=old_ts, status="err", error_class="OldErr")
    _write_usage_line(p, ts=old_ts, status="ok")
    _write_usage_line(p, ts=new_ts, status="err", error_class="NewErr")
    _write_usage_line(p, ts=new_ts, status="ok")
    out = metrics.recent_errors_window(window_minutes=5, last_n=5, path=p)
    assert out["total_calls"] == 2
    assert out["error_count"] == 1
    classes = [e["error_class"] for e in out["last_errors"]]
    assert "NewErr" in classes
    assert "OldErr" not in classes


def test_recent_errors_last_n_limits(tmp_path: Path) -> None:
    p = tmp_path / "u.jsonl"
    for i in range(10):
        _write_usage_line(p, status="err", error_class=f"E{i}")
    out = metrics.recent_errors_window(window_minutes=5, last_n=3, path=p)
    assert out["error_count"] == 10
    assert len(out["last_errors"]) == 3
    # Should be the *last* 3, in chronological order.
    assert [e["error_class"] for e in out["last_errors"]] == ["E7", "E8", "E9"]


def test_recent_errors_invalid_window_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        metrics.recent_errors_window(window_minutes=0, last_n=5, path=tmp_path / "u.jsonl")
    with pytest.raises(ValueError):
        metrics.recent_errors_window(window_minutes=5, last_n=-1, path=tmp_path / "u.jsonl")


def test_recent_errors_skips_malformed_lines(tmp_path: Path) -> None:
    p = tmp_path / "u.jsonl"
    _write_usage_line(p, status="ok")
    with p.open("a", encoding="utf-8") as fh:
        fh.write("not-json\n")
        fh.write("\n")  # blank
        fh.write(json.dumps({"no_ts": "field"}) + "\n")
    _write_usage_line(p, status="err", error_class="X")
    out = metrics.recent_errors_window(window_minutes=5, last_n=5, path=p)
    assert out["total_calls"] == 2
    assert out["error_count"] == 1


def test_recent_errors_no_pii_leak(tmp_path: Path) -> None:
    """``last_errors`` must NEVER include error message / token / PII fields.

    Even if a future writer adds a ``message`` or ``access_token`` column to
    usage.jsonl rows, the sliding-window helper must surface only metadata
    (``ts``, ``tool``, ``error_class``).
    """
    p = tmp_path / "u.jsonl"
    _write_usage_line(
        p,
        status="err",
        error_class="SchwabAuthError",
        extra={
            "message": "401 Unauthorized: Bearer abc.def.ghi",
            "access_token": "Bearer abc.def.ghi",
            "account_number": "1234567890",
            "user_email": "victim@example.com",
        },
    )
    out = metrics.recent_errors_window(window_minutes=5, last_n=5, path=p)
    assert out["error_count"] == 1
    assert len(out["last_errors"]) == 1
    last = out["last_errors"][0]
    # Allowed fields only.
    assert set(last.keys()) == {"ts", "tool", "error_class"}
    blob = json.dumps(last)
    forbidden_substrings = (
        "Bearer",
        "access_token",
        "account_number",
        "user_email",
        "victim@example.com",
        "abc.def.ghi",
        "401 Unauthorized",
    )
    for needle in forbidden_substrings:
        assert needle not in blob, f"PII/secret leaked into last_errors: {needle!r}"


def test_recent_errors_tail_read_handles_large_file(tmp_path: Path) -> None:
    """Smoke-test the tail-read optimisation: 5000 rows → still scans only the recent ones."""
    p = tmp_path / "u.jsonl"
    old_ts = (datetime.now(tz=UTC) - timedelta(hours=2)).isoformat(timespec="milliseconds")
    # Write a lot of old rows + a handful of fresh rows.
    with p.open("w", encoding="utf-8") as fh:
        for _ in range(5000):
            fh.write(json.dumps({"ts": old_ts, "tool": "x", "status": "ok", "error_class": None}) + "\n")
        fresh = datetime.now(tz=UTC).isoformat(timespec="milliseconds")
        fh.write(json.dumps({"ts": fresh, "tool": "y", "status": "err", "error_class": "Z"}) + "\n")
    out = metrics.recent_errors_window(window_minutes=5, last_n=5, path=p)
    # The old rows are outside the window, regardless of whether they were
    # read or skipped via tail truncation.
    assert out["total_calls"] == 1
    assert out["error_count"] == 1
    assert out["last_errors"][0]["error_class"] == "Z"


def test_recent_errors_last_n_zero_returns_empty_list(tmp_path: Path) -> None:
    """``last_n=0`` short-circuits to an empty list even if errors exist."""
    p = tmp_path / "u.jsonl"
    _write_usage_line(p, status="err", error_class="X")
    _write_usage_line(p, status="err", error_class="Y")
    out = metrics.recent_errors_window(window_minutes=5, last_n=0, path=p)
    assert out["error_count"] == 2
    assert out["last_errors"] == []


def test_recent_errors_handles_naive_timestamps(tmp_path: Path) -> None:
    """A row with a naive ``ts`` (no tz info) must be treated as UTC, not skipped."""
    p = tmp_path / "u.jsonl"
    naive = datetime.now(tz=UTC).replace(tzinfo=None).isoformat(timespec="milliseconds")
    _write_usage_line(p, ts=naive, status="ok")
    _write_usage_line(p, ts=naive, status="err", error_class="NaiveErr")
    out = metrics.recent_errors_window(window_minutes=5, last_n=5, path=p)
    assert out["total_calls"] == 2
    assert out["error_count"] == 1
    assert out["last_errors"][0]["error_class"] == "NaiveErr"


def test_tail_lines_handles_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``_tail_lines`` swallows OSError and returns []."""
    p = tmp_path / "u.jsonl"
    p.write_text("hello\n")

    real_open = Path.open

    def boom(self: Path, *a: object, **kw: object) -> object:
        if self == p:
            raise OSError("simulated read failure")
        return real_open(self, *a, **kw)

    monkeypatch.setattr(Path, "open", boom)
    assert metrics._tail_lines(p) == []
