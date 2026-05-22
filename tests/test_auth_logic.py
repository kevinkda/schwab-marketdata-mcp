"""Pure-function tests for ``auth_logic.py``.

Plan §6.4 — ``auth_logic`` is in CRITICAL_MODULES at 100 % coverage.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from schwab_marketdata_mcp.auth_logic import (
    DEFAULT_LOGIN_FLOW_CALLBACK,
    LOGIN_FLOW_ALLOWED_HOSTS,
    MAX_CALLBACK_PORT,
    MIN_CALLBACK_PORT,
    AuthConfig,
    atomic_write_token,
    build_auth_config,
    load_credentials_from_env,
    make_token_write_func,
    preflight_summary,
)
from schwab_marketdata_mcp.errors import SchwabAuthError

# ---------------------------------------------------------------------------
# load_credentials_from_env
# ---------------------------------------------------------------------------


def test_load_credentials_happy_path() -> None:
    env = {
        "SCHWAB_APP_KEY": "real-key",
        "SCHWAB_APP_SECRET": "real-secret",
        "SCHWAB_CALLBACK_URL": "https://127.0.0.1:8182",
    }
    k, s, c = load_credentials_from_env(env)
    assert (k, s, c) == ("real-key", "real-secret", "https://127.0.0.1:8182")


def test_load_credentials_default_callback_when_absent() -> None:
    k, s, c = load_credentials_from_env({"SCHWAB_APP_KEY": "k", "SCHWAB_APP_SECRET": "s"})
    assert c == DEFAULT_LOGIN_FLOW_CALLBACK


@pytest.mark.parametrize(
    "env",
    [
        {"SCHWAB_APP_KEY": "", "SCHWAB_APP_SECRET": "s"},
        {"SCHWAB_APP_KEY": "dummy-not-a-real-secret", "SCHWAB_APP_SECRET": "s"},
        {"SCHWAB_APP_KEY": "k", "SCHWAB_APP_SECRET": ""},
        {"SCHWAB_APP_KEY": "k", "SCHWAB_APP_SECRET": "dummy-not-a-real-secret"},
    ],
)
def test_load_credentials_rejects_placeholder(env: dict) -> None:
    with pytest.raises(SchwabAuthError) as ei:
        load_credentials_from_env(env)
    assert ei.value.reason == "credential_missing"


def test_load_credentials_rejects_http_callback() -> None:
    with pytest.raises(SchwabAuthError) as ei:
        load_credentials_from_env({"SCHWAB_APP_KEY": "k", "SCHWAB_APP_SECRET": "s", "SCHWAB_CALLBACK_URL": "http://x"})
    assert ei.value.reason == "callback_url_mismatch"


def test_load_credentials_rejects_bare_https() -> None:
    with pytest.raises(SchwabAuthError) as ei:
        load_credentials_from_env({"SCHWAB_APP_KEY": "k", "SCHWAB_APP_SECRET": "s", "SCHWAB_CALLBACK_URL": "https://"})
    assert ei.value.reason == "callback_url_mismatch"


def test_load_credentials_falls_back_to_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCHWAB_APP_KEY", "from-os")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "from-os")
    k, _, _ = load_credentials_from_env(env=None)
    assert k == "from-os"


# ---------------------------------------------------------------------------
# build_auth_config — full path resolution + cloud opt-in
# ---------------------------------------------------------------------------


def test_build_auth_config_default_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    cfg = build_auth_config(
        config_dir=None,
        cloud_opt_in=False,
        env={"SCHWAB_APP_KEY": "k", "SCHWAB_APP_SECRET": "s"},
    )
    assert cfg.token_path.name == "token.json"


def test_build_auth_config_with_config_dir_in_allow_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    custom = tmp_path / "state" / "alt"
    cfg = build_auth_config(
        config_dir=str(custom),
        cloud_opt_in=False,
        env={"SCHWAB_APP_KEY": "k", "SCHWAB_APP_SECRET": "s"},
    )
    assert "alt" in str(cfg.token_path)
    assert cfg.token_path.name == "token.json"


def test_build_auth_config_outside_allow_list_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "state").mkdir(exist_ok=True)
    with pytest.raises(SchwabAuthError):
        build_auth_config(
            config_dir="/etc",
            cloud_opt_in=False,
            env={"SCHWAB_APP_KEY": "k", "SCHWAB_APP_SECRET": "s"},
        )


# ---------------------------------------------------------------------------
# atomic_write_token + make_token_write_func
# ---------------------------------------------------------------------------


def test_atomic_write_creates_dir_and_file(tmp_path: Path) -> None:
    p = tmp_path / "deep" / "nest" / "token.json"
    atomic_write_token(p, {"creation_timestamp": 1700000000, "token": {"x": 1}})
    assert p.exists()
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert stat.S_IMODE(p.parent.stat().st_mode) == 0o700
    parsed = json.loads(p.read_text())
    assert parsed["creation_timestamp"] == 1700000000


def test_atomic_write_rejects_non_serializable_payload(tmp_path: Path) -> None:
    p = tmp_path / "tok.json"
    with pytest.raises(TypeError):
        atomic_write_token(p, "string-not-dict")  # type: ignore[arg-type]


def test_atomic_write_overwrites_existing(tmp_path: Path) -> None:
    p = tmp_path / "tok.json"
    atomic_write_token(p, {"v": 1})
    atomic_write_token(p, {"v": 2})
    assert json.loads(p.read_text()) == {"v": 2}


def test_make_token_write_func_round_trip(tmp_path: Path) -> None:
    p = tmp_path / "tok.json"
    fn = make_token_write_func(p)
    fn({"a": 1}, "ignored", positional="ignored")
    assert json.loads(p.read_text()) == {"a": 1}


def test_atomic_write_resets_umask_even_on_failure(tmp_path: Path) -> None:
    """OWASP A02 / A05 — umask must be restored after a failed write."""
    old = os.umask(0o022)
    try:
        with pytest.raises(TypeError):
            atomic_write_token(tmp_path / "tok.json", "bad")  # type: ignore[arg-type]
        cur = os.umask(0o022)
        assert cur == 0o022, "umask leaked"
    finally:
        os.umask(old)


# ---------------------------------------------------------------------------
# preflight_summary — never leaks the full key
# ---------------------------------------------------------------------------


def test_preflight_summary_redacts_key() -> None:
    cfg = AuthConfig(
        api_key="ABCDEFGHIJKLMNOP",
        app_secret="superseekrit",
        callback_url="https://127.0.0.1:8182",
        token_path=Path("/tmp/x/token.json"),
        cloud_opt_in=False,
    )
    out = preflight_summary(cfg)
    assert "superseekrit" not in out
    assert "ABCDEFGHIJKLMNOP" not in out
    # Tail two characters ("OP") are kept by design — not the secret material.
    assert "ABCD" in out
    assert "OP" in out


# ---------------------------------------------------------------------------
# Regression tests for MismatchingStateException root-cause analysis
# (callback URL mis-configuration → schwab-py state desynchronisation).
# ---------------------------------------------------------------------------


def _ok_env(callback: str | None = None) -> dict[str, str]:
    env = {"SCHWAB_APP_KEY": "k", "SCHWAB_APP_SECRET": "s"}
    if callback is not None:
        env["SCHWAB_CALLBACK_URL"] = callback
    return env


def test_load_credentials_rejects_callback_without_explicit_port() -> None:
    """Regression: a callback like ``https://127.0.0.1`` falls back to port 443
    inside schwab-py.  The local server cannot bind 443 without root, so the
    user gets a confusing ``RedirectServerExitedError``; worse, if the Schwab
    Developer Portal app is registered with a real port (e.g. 8182), the
    advertised redirect_uri is truncated and Schwab returns a different
    ``state`` value, surfacing as ``MismatchingStateException``.
    """
    with pytest.raises(SchwabAuthError) as ei:
        load_credentials_from_env(_ok_env("https://127.0.0.1"))
    assert ei.value.reason == "callback_url_mismatch"
    assert "explicit port" in ei.value.hint


def test_load_credentials_rejects_non_loopback_host() -> None:
    """``client_from_login_flow`` only accepts ``127.0.0.1``.  We surface a
    structured error before schwab-py spawns a multiprocess server.
    """
    with pytest.raises(SchwabAuthError) as ei:
        load_credentials_from_env(_ok_env("https://dev-dsk-example.us-east-1.amazon.com:8182"))
    assert ei.value.reason == "callback_url_mismatch"
    assert "127.0.0.1" in ei.value.hint


@pytest.mark.parametrize(
    "callback",
    [
        "https://127.0.0.1:80",
        "https://127.0.0.1:443",
        "https://127.0.0.1:1024",
        "https://127.0.0.1:0",
    ],
)
def test_load_credentials_rejects_privileged_or_invalid_low_port(callback: str) -> None:
    """Privileged ports (≤1024) and 0 are rejected — they require root or are
    invalid and lead to a server-exit cascade.
    """
    with pytest.raises(SchwabAuthError) as ei:
        load_credentials_from_env(_ok_env(callback))
    assert ei.value.reason == "callback_url_mismatch"


def test_load_credentials_rejects_port_above_65535() -> None:
    """``urlparse`` raises ValueError on a port above 65535; we wrap it into
    a structured ``SchwabAuthError`` instead of leaking the stacktrace.
    """
    with pytest.raises(SchwabAuthError) as ei:
        load_credentials_from_env(_ok_env("https://127.0.0.1:99999"))
    assert ei.value.reason == "callback_url_mismatch"


@pytest.mark.parametrize(
    "callback",
    [
        "https://127.0.0.1:8182",
        "https://127.0.0.1:1025",
        "https://127.0.0.1:65535",
        "https://127.0.0.1:57565",
    ],
)
def test_load_credentials_accepts_valid_loopback_callback(callback: str) -> None:
    """Determinism: any valid loopback URL with a high port round-trips
    unchanged so schwab-py's callback server binds the *same* port that
    appears in the ``redirect_uri`` query parameter.
    """
    _, _, returned = load_credentials_from_env(_ok_env(callback))
    assert returned == callback


def test_default_callback_is_high_port_loopback() -> None:
    """Sanity: the default callback baked into the module must itself pass
    the strict validator — otherwise the default would always trigger the
    error we just added.
    """
    _, _, returned = load_credentials_from_env({"SCHWAB_APP_KEY": "k", "SCHWAB_APP_SECRET": "s"})
    assert returned == DEFAULT_LOGIN_FLOW_CALLBACK
    assert "127.0.0.1" in DEFAULT_LOGIN_FLOW_CALLBACK
    assert ":8182" in DEFAULT_LOGIN_FLOW_CALLBACK


def test_login_flow_allowed_hosts_only_loopback() -> None:
    """Defence-in-depth — the allow-list of hosts must not silently widen."""
    assert LOGIN_FLOW_ALLOWED_HOSTS == ("127.0.0.1",)


def test_callback_port_bounds_constants() -> None:
    """Constants documenting the rejection ranges must stay in sync with the
    docstrings and the troubleshooting notes in ``docs/REGISTER.md``.
    """
    assert MIN_CALLBACK_PORT == 1025
    assert MAX_CALLBACK_PORT == 65535


def test_preflight_summary_includes_state_mismatch_reminder() -> None:
    """The stderr pre-flight banner must carry an inline reminder about the
    Schwab Developer Portal redirect_uri match — this is what stops a user
    from racing through the flow with a stale .env and hitting CSRF Warning.
    """
    cfg = AuthConfig(
        api_key="ABCD12345678",
        app_secret="dontleak",
        callback_url="https://127.0.0.1:8182",
        token_path=Path("/tmp/x/token.json"),
        cloud_opt_in=False,
    )
    out = preflight_summary(cfg)
    assert "MismatchingStateException" in out or "CSRF" in out
    assert "Schwab Developer Portal" in out
