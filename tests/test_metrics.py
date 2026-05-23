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
