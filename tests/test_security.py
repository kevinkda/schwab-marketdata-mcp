"""OWASP-aware security tests.

Plan §6.3 — exercise:
* OWASP 2017 A1/A2/A3/A6/A10
* OWASP 2021 A01/A02/A03/A05/A07/A09/A10
* OWASP 2025 (where applicable)

Each ``# OWASP …`` comment explicitly maps the test to the matrix.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path

import pytest

from schwab_marketdata_mcp.errors import (
    RedactBearerFilter,
    SchwabAuthError,
    SchwabRateLimitError,
    SchwabTransientError,
    SchwabValidationError,
    redact_secrets,
)
from schwab_marketdata_mcp.security import (
    CLOUD_OPT_IN_FLAG,
    TokenState,
    assert_cloud_path_consent,
    check_token_file_state,
    enforce_token_perms,
    ensure_secure_dir,
    insecure_perms_hint,
    is_cloud_path,
    resolve_token_path,
    secure_chmod,
    token_file_lock,
    xdg_state_root,
)

# ---------------------------------------------------------------------------
# OWASP 2017 A3 / 2021 A02 — Sensitive Data Exposure / Cryptographic Failures
# ---------------------------------------------------------------------------


def test_redact_bearer_in_log_message() -> None:
    """OWASP A02 — Bearer token must never appear in stringified log records."""
    logger = logging.getLogger("test.redact.bearer")
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG)
    handler = logging.StreamHandler()
    handler.addFilter(RedactBearerFilter())
    logger.addHandler(handler)

    raw = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload"
    record = logger.makeRecord(
        name=logger.name,
        level=logging.INFO,
        fn=__file__,
        lno=0,
        msg=raw,
        args=(),
        exc_info=None,
    )
    f = RedactBearerFilter()
    f.filter(record)
    assert "Bearer" in record.msg
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload" not in record.msg
    assert "***REDACTED***" in record.msg


def test_redact_secrets_idempotent() -> None:
    twice = redact_secrets(redact_secrets("Bearer abc.def"))
    assert "abc.def" not in twice


def test_redact_access_and_refresh_token_json() -> None:
    """OWASP A02 — JSON-shaped credential lines are also redacted."""
    raw = '{"access_token":"xxx-yyy","refresh_token":"aaa-bbb","other":"safe"}'
    out = redact_secrets(raw)
    assert "xxx-yyy" not in out
    assert "aaa-bbb" not in out
    assert "safe" in out


def test_exception_str_does_not_leak_token() -> None:
    """OWASP A02 — every exception class must keep tokens out of its str()."""
    e = SchwabAuthError(
        reason="access_token_invalid",
        hint="Got Authorization: Bearer hunter2.payload from upstream",
    )
    assert "hunter2" not in str(e)


def test_exceptions_reject_wrong_types() -> None:
    """Sanity — ``message`` field whitelist."""
    with pytest.raises(TypeError):
        SchwabAuthError(reason=123, hint="x")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        SchwabAuthError(reason="x", hint=123)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        SchwabAuthError(reason="x", hint="y", expires_in_seconds="zzz")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        SchwabRateLimitError(retry_after_seconds="x", current_window_used=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        SchwabRateLimitError(retry_after_seconds=1, current_window_used="y")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        SchwabTransientError(status_code="500", attempt=0, hint="x")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        SchwabTransientError(status_code=500, attempt="0", hint="x")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        SchwabTransientError(status_code=500, attempt=0, hint=123)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        SchwabValidationError(field=1, reason="x")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        SchwabValidationError(field="x", reason=2)  # type: ignore[arg-type]


def test_exception_str_formats() -> None:
    """All exception __str__ methods exercised."""
    e1 = SchwabAuthError(reason="refresh_token_expired", hint="run auth", expires_in_seconds=0)
    assert "expires_in" in str(e1)
    e1b = SchwabAuthError(reason="x", hint="y")  # type: ignore[arg-type]
    assert "x" in str(e1b)
    e2 = SchwabRateLimitError(retry_after_seconds=10, current_window_used=120)
    assert "retry_after=10s" in str(e2)
    e3 = SchwabTransientError(status_code=503, attempt=2, hint="upstream down")
    assert "status=503" in str(e3)
    e4 = SchwabValidationError(field="symbol", reason="bad")
    assert "field=symbol" in str(e4)


# ---------------------------------------------------------------------------
# OWASP 2017 A1 / 2021 A03 — Injection (symbol injection)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "evil",
    [
        "AAPL'; DROP TABLE",
        "AAPL; rm -rf /",
        "AAPL<script>alert(1)</script>",
        "$(/bin/whoami)",
        "../../../etc/passwd",
    ],
)
def test_symbol_injection_rejected(evil: str) -> None:
    """OWASP A03 — Pydantic regex must reject metachars."""
    from schwab_marketdata_mcp.models import GetQuoteInput

    with pytest.raises(Exception):
        GetQuoteInput(symbol=evil)


# ---------------------------------------------------------------------------
# OWASP 2021 A01 — Broken Access Control / path allow-list
# ---------------------------------------------------------------------------


def test_token_path_allow_list_rejects_etc(_isolate_xdg_state: Path) -> None:
    with pytest.raises(SchwabAuthError) as ei:
        resolve_token_path("/etc/passwd")
    assert ei.value.reason == "path_not_in_allow_list"


def test_token_path_allow_list_rejects_root(_isolate_xdg_state: Path) -> None:
    with pytest.raises(SchwabAuthError):
        resolve_token_path("/root/.ssh/authorized_keys")


def test_token_path_rejects_dotdot_segment(_isolate_xdg_state: Path) -> None:
    with pytest.raises(SchwabAuthError) as ei:
        resolve_token_path("~/.local/state/../../../etc/passwd")
    assert ei.value.reason == "path_not_in_allow_list"


def test_token_path_default_is_xdg(_isolate_xdg_state: Path) -> None:
    p = resolve_token_path(None)
    assert p.parent.name == "schwab-marketdata-mcp"
    assert "state" in str(p) or ".local" in str(p)


def test_token_path_accepts_xdg_state(_isolate_xdg_state: Path) -> None:
    p = resolve_token_path(str(_isolate_xdg_state / "schwab-marketdata-mcp/token.json"))
    assert _isolate_xdg_state in p.parents or _isolate_xdg_state == p.parents[1]


def test_token_path_symlink_in_parent_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If we point the allow-list root at a real dir but a symlink leads
    elsewhere within it, the symlink walk catches the redirection."""
    state = tmp_path / "real-state"
    state.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    # Create a symlink from inside the allow-list root pointing outside it
    target = tmp_path / "outside"
    target.mkdir()
    sym = state / "schwab-marketdata-mcp"
    sym.symlink_to(target)
    # The resolved path is now under tmp_path/outside, which is NOT in the allow-list,
    # so the allow-list check rejects it.
    with pytest.raises(SchwabAuthError):
        resolve_token_path(str(sym / "token.json"))


# ---------------------------------------------------------------------------
# OWASP 2017 A3 / 2021 A02 — token file permissions
# ---------------------------------------------------------------------------


def test_token_file_state_missing(tmp_path: Path) -> None:
    state, parsed = check_token_file_state(tmp_path / "nope.json")
    assert state is TokenState.MISSING
    assert parsed is None


def test_token_file_state_insecure_before_malformed(tmp_path: Path) -> None:
    """Plan §3.2.2.1 — perms checked BEFORE json.load."""
    f = tmp_path / "tok.json"
    f.write_text("{not valid json")
    os.chmod(f, 0o644)
    state, parsed = check_token_file_state(f)
    assert state is TokenState.INSECURE_PERMS
    assert parsed is None  # json never parsed


def test_token_file_state_malformed_after_perm_ok(tmp_path: Path) -> None:
    f = tmp_path / "tok.json"
    f.write_text("{not valid json")
    os.chmod(f, 0o600)
    state, _ = check_token_file_state(f)
    assert state is TokenState.MALFORMED


def test_token_file_state_malformed_non_dict(tmp_path: Path) -> None:
    """Top-level array / scalar are also rejected."""
    f = tmp_path / "tok.json"
    f.write_text("[1, 2, 3]")
    os.chmod(f, 0o600)
    state, _ = check_token_file_state(f)
    assert state is TokenState.MALFORMED


def test_token_file_state_valid(tmp_path: Path) -> None:
    f = tmp_path / "tok.json"
    f.write_text(json.dumps({"creation_timestamp": 1700000000}))
    os.chmod(f, 0o600)
    state, parsed = check_token_file_state(f)
    assert state is TokenState.VALID
    assert parsed == {"creation_timestamp": 1700000000}


def test_enforce_token_perms_raises_on_wrong_mode(tmp_path: Path) -> None:
    f = tmp_path / "tok.json"
    f.write_text("{}")
    os.chmod(f, 0o644)
    with pytest.raises(SchwabAuthError) as ei:
        enforce_token_perms(f)
    assert ei.value.reason == "insecure_token_perms"
    assert "chmod 600" in ei.value.hint
    assert str(f) in ei.value.hint


def test_enforce_token_perms_raises_on_wrong_parent(tmp_path: Path) -> None:
    sub = tmp_path / "schwab"
    sub.mkdir()
    os.chmod(sub, 0o755)  # too loose
    f = sub / "tok.json"
    f.write_text("{}")
    os.chmod(f, 0o600)
    with pytest.raises(SchwabAuthError) as ei:
        enforce_token_perms(f)
    assert ei.value.reason == "insecure_token_perms"
    assert "chmod 700" in ei.value.hint


def test_enforce_token_perms_no_op_when_missing(tmp_path: Path) -> None:
    enforce_token_perms(tmp_path / "absent.json")  # should not raise


def test_insecure_perms_hint_uses_actual_path() -> None:
    """Plan §3.3.2 — hint must use real path, never hardcoded."""
    p = Path("/tmp/different/path/token.json")
    h = insecure_perms_hint(p, 0o644)
    assert str(p) in h
    assert str(p.parent) in h
    assert "0o644" in h


def test_secure_chmod(tmp_path: Path) -> None:
    f = tmp_path / "x.json"
    f.write_text("{}")
    os.chmod(f, 0o644)
    secure_chmod(f)
    assert stat.S_IMODE(f.stat().st_mode) == 0o600


def test_ensure_secure_dir_creates_with_700(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "nest"
    ensure_secure_dir(target)
    assert target.is_dir()
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_ensure_secure_dir_tightens_loose(tmp_path: Path) -> None:
    target = tmp_path / "loose"
    target.mkdir(mode=0o755)
    os.chmod(target, 0o755)
    ensure_secure_dir(target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


# ---------------------------------------------------------------------------
# OWASP 2021 A05 — Security misconfig: cloud paths & .gitignore
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rel",
    ["Dropbox/x.json", "OneDrive/foo.json", "Library/Mobile Documents/x.json"],
)
def test_is_cloud_path_detects_known_prefixes(rel: str) -> None:
    p = Path.home() / rel
    assert is_cloud_path(p) is True


def test_is_cloud_path_not_under_home() -> None:
    assert is_cloud_path(Path("/tmp/elsewhere/x.json")) is False


def test_is_cloud_path_xdg_default_safe() -> None:
    assert is_cloud_path(xdg_state_root() / "schwab-marketdata-mcp/token.json") is False


def test_xdg_state_root_fallback_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """OWASP A05 — confirm fallback to ``~/.local/state`` when the env var is unset."""
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    p = xdg_state_root()
    assert p.parts[-2:] == (".local", "state")


def test_assert_cloud_path_consent_blocks_without_optin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_home = tmp_path
    monkeypatch.setenv("HOME", str(fake_home))
    cloud = fake_home / "Dropbox" / "x.json"
    cloud.parent.mkdir(parents=True)
    with pytest.raises(SchwabAuthError) as ei:
        assert_cloud_path_consent(cloud, opt_in=False)
    assert ei.value.reason == "cloud_path_detected"
    assert CLOUD_OPT_IN_FLAG in ei.value.hint


def test_assert_cloud_path_consent_passes_with_optin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cloud = tmp_path / "Dropbox" / "x.json"
    cloud.parent.mkdir(parents=True)
    assert_cloud_path_consent(cloud, opt_in=True)  # no raise


def test_assert_cloud_path_consent_noop_for_safe_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    safe = tmp_path / ".local/state/schwab-marketdata-mcp/token.json"
    safe.parent.mkdir(parents=True)
    assert_cloud_path_consent(safe, opt_in=False)  # no raise


def test_gitignore_excludes_secrets() -> None:
    """OWASP A05 — `.env` and friends must be in .gitignore (precise paths)."""
    gi = Path(__file__).resolve().parents[1] / ".gitignore"
    txt = gi.read_text()
    for must in (".env", "token.json", "*.token", "token.json.lock", "usage.jsonl", "logs/"):
        assert must in txt, f".gitignore missing {must!r}"
    assert "!uv.lock" in txt, ".gitignore missing reverse rule for uv.lock"
    # Plan §8.1 mandates that .env.example MUST be allowed
    assert "!.env.example" in txt


# ---------------------------------------------------------------------------
# OWASP 2017 A8 / 2021 A08 — insecure deserialization / data integrity
# ---------------------------------------------------------------------------


def test_token_file_state_uses_strict_json(tmp_path: Path) -> None:
    """OWASP A08 — never eval; broken JSON must yield MALFORMED, not exec code."""
    f = tmp_path / "tok.json"
    # Python `pickle` magic bytes — must not be deserialized.
    f.write_text("__import__('os').system('echo PWNED')")
    os.chmod(f, 0o600)
    state, _ = check_token_file_state(f)
    assert state is TokenState.MALFORMED


def test_fixtures_directory_loads_with_json_only() -> None:
    """OWASP A08 — every fixture in tests/fixtures must be valid JSON."""
    fdir = Path(__file__).resolve().parent / "fixtures"
    for f in fdir.glob("*.json"):
        with f.open("r", encoding="utf-8") as fh:
            json.load(fh)  # raises if not strict JSON


# ---------------------------------------------------------------------------
# OWASP 2021 A09 — logging & monitoring
# ---------------------------------------------------------------------------


def test_redact_filter_filter_returns_true(caplog: pytest.LogCaptureFixture) -> None:
    """RedactBearerFilter must return True so messages still propagate."""
    f = RedactBearerFilter()
    rec = logging.makeLogRecord({"msg": "no secrets here"})
    assert f.filter(rec) is True


def test_redact_filter_handles_args() -> None:
    """RedactBearerFilter must merge args and clear them after."""
    f = RedactBearerFilter()
    rec = logging.makeLogRecord({"msg": "Authorization: Bearer %s.x", "args": ("payload",)})
    f.filter(rec)
    assert "payload" not in rec.msg


def test_redact_filter_handles_getmessage_failure() -> None:
    """If getMessage raises (mismatched %-format), fall back to str(record.msg)."""
    f = RedactBearerFilter()
    # %d format with a non-int arg makes getMessage raise TypeError.
    rec = logging.makeLogRecord({"msg": "Bearer %d", "args": ("not-an-int",)})
    f.filter(rec)
    assert "Bearer" in rec.msg
    assert "not-an-int" not in rec.msg or "***REDACTED***" in rec.msg


# ---------------------------------------------------------------------------
# Cross-process file lock smoke test
# ---------------------------------------------------------------------------


def test_token_file_lock_acquires_and_releases(tmp_path: Path) -> None:
    p = tmp_path / "tok.json"
    with token_file_lock(p):
        assert (tmp_path / "tok.json.lock").exists()
        assert stat.S_IMODE((tmp_path / "tok.json.lock").stat().st_mode) == 0o600
    # Should be releasable a second time without contention.
    with token_file_lock(p):
        pass
