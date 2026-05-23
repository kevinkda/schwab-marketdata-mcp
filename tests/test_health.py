"""``health.py`` unit tests — exit code matrix + side-channels."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from schwab_marketdata_mcp import health

# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------


def test_classify_table() -> None:
    assert health.classify(timedelta(0)) == health.HealthExit.EXPIRED_OR_12H
    assert health.classify(timedelta(hours=11)) == health.HealthExit.EXPIRED_OR_12H
    assert health.classify(timedelta(hours=23)) == health.HealthExit.EXPIRES_24H
    assert health.classify(timedelta(hours=25)) == health.HealthExit.HEALTHY
    assert health.classify(timedelta(seconds=-1)) == health.HealthExit.EXPIRED_OR_12H


def test_compute_expires_in_now_zero_age() -> None:
    now = datetime.now(tz=UTC).timestamp()
    out = health.compute_expires_in(now, now_ts=now)
    assert out == timedelta(days=7)


def test_compute_expires_in_old_token() -> None:
    now = datetime.now(tz=UTC).timestamp()
    out = health.compute_expires_in(now - 8 * 86400, now_ts=now)
    assert out <= timedelta(0)


def test_human_summary_all_codes() -> None:
    cases = [
        (health.HealthExit.HEALTHY, timedelta(hours=72)),
        (health.HealthExit.EXPIRES_24H, timedelta(hours=20)),
        (health.HealthExit.EXPIRED_OR_12H, None),
        (health.HealthExit.MISSING, None),
        (health.HealthExit.MALFORMED, None),
        (health.HealthExit.INSECURE_PERMS, None),
        (99, None),  # unknown
    ]
    for code, exp in cases:
        out = health.human_summary(code, exp)
        assert isinstance(out, str)
        assert "Schwab MCP" in out


# ---------------------------------------------------------------------------
# Side channels
# ---------------------------------------------------------------------------


def test_write_desktop_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    out = health._write_desktop_marker("hello", hint="Authorization: Bearer leaky.payload")
    assert out is not None
    body = out.read_text()
    assert "hello" in body
    assert "leaky.payload" not in body
    assert "Bearer ***REDACTED***" in body


def test_write_desktop_marker_no_desktop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    # No Desktop dir
    out = health._write_desktop_marker("nothing")
    assert out is None


def test_notify_silent_on_unknown_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "fictional-os")
    health._notify("doesn't matter")


def test_emit_stderr_emits_json(capsys: pytest.CaptureFixture[str]) -> None:
    health._emit_stderr({"event": "test", "x": 1})
    err = capsys.readouterr().err
    assert json.loads(err.strip()) == {"event": "test", "x": 1}


# ---------------------------------------------------------------------------
# run() — full state matrix
# ---------------------------------------------------------------------------


def _setup_state_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    pdir = state / "schwab-marketdata-mcp"
    pdir.mkdir(exist_ok=True)
    os.chmod(pdir, 0o700)
    return pdir


def test_run_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_state_dir(monkeypatch, tmp_path)
    code = health.run(None)
    assert code == health.HealthExit.MISSING


@pytest.mark.posix_only
def test_run_insecure_perms(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdir = _setup_state_dir(monkeypatch, tmp_path)
    f = pdir / "token.json"
    f.write_text("{}")
    os.chmod(f, 0o644)
    code = health.run(None)
    assert code == health.HealthExit.INSECURE_PERMS


def test_run_malformed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdir = _setup_state_dir(monkeypatch, tmp_path)
    f = pdir / "token.json"
    f.write_text("not-json")
    os.chmod(f, 0o600)
    code = health.run(None)
    assert code == health.HealthExit.MALFORMED


def test_run_valid_via_mtime_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """schwab-py probe will fail (no real creds), then fall back to mtime."""
    pdir = _setup_state_dir(monkeypatch, tmp_path)
    f = pdir / "token.json"
    f.write_text(json.dumps({"creation_timestamp": 1700000000}))
    os.chmod(f, 0o600)
    # Set the mtime to "fresh" so we should classify as HEALTHY.
    fresh = datetime.now(tz=UTC).timestamp()
    os.utime(f, (fresh, fresh))
    code = health.run(None)
    assert code == health.HealthExit.HEALTHY


def test_run_path_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    code = health.run("/etc/some_token.json")
    assert code == health.HealthExit.MISSING


def test_run_classifies_expiring_soon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdir = _setup_state_dir(monkeypatch, tmp_path)
    f = pdir / "token.json"
    f.write_text(json.dumps({}))
    os.chmod(f, 0o600)
    # Set mtime so the token is ~6.5 days old → expires_in ≈ 12h → critical.
    old = datetime.now(tz=UTC).timestamp() - 6.6 * 86400
    os.utime(f, (old, old))
    code = health.run(None)
    assert code in (health.HealthExit.EXPIRED_OR_12H, health.HealthExit.EXPIRES_24H)


def test_run_classifies_warn_24h(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdir = _setup_state_dir(monkeypatch, tmp_path)
    f = pdir / "token.json"
    f.write_text(json.dumps({}))
    os.chmod(f, 0o600)
    old = datetime.now(tz=UTC).timestamp() - (7 * 86400 - 18 * 3600)  # 18h left
    os.utime(f, (old, old))
    code = health.run(None)
    assert code == health.HealthExit.EXPIRES_24H


@pytest.mark.posix_only
def test_run_perms_drift_after_state_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If state machine returns VALID but enforce_token_perms detects parent drift."""
    pdir = _setup_state_dir(monkeypatch, tmp_path)
    f = pdir / "token.json"
    f.write_text(json.dumps({}))
    os.chmod(f, 0o600)
    os.chmod(pdir, 0o755)  # parent loose AFTER state check
    code = health.run(None)
    assert code == health.HealthExit.INSECURE_PERMS


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_main_no_args_returns_exit_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    rc = health.cli_main([])
    assert rc == health.HealthExit.MISSING


def test_cli_main_with_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    custom = state / "alt"
    custom.mkdir()
    os.chmod(custom, 0o700)
    rc = health.cli_main(["--config-dir", str(custom)])
    assert rc == health.HealthExit.MISSING
