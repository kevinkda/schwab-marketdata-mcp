"""Cross-platform shim tests (``schwab_marketdata_mcp._platform``).

Plan §6.4 - 100% coverage requirement on critical modules.  These tests
exercise both the POSIX and the Windows branches; the Windows-specific paths
are invoked via ``monkeypatch`` of ``_platform.IS_WINDOWS`` so they can be
covered on a Linux / macOS CI runner without an actual Windows box.
"""

from __future__ import annotations

import logging
import os
import stat
import sys
from pathlib import Path

import pytest

from schwab_marketdata_mcp import _platform

# ---------------------------------------------------------------------------
# state_root()
# ---------------------------------------------------------------------------


def test_state_root_xdg_state_home_always_wins(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """XDG_STATE_HOME takes precedence on every platform."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    assert _platform.state_root() == tmp_path / "xdg"


def test_state_root_posix_fallback_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(_platform, "IS_WINDOWS", False)
    assert _platform.state_root() == Path.home() / ".local" / "state"


def test_state_root_uses_localappdata_on_windows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """On Windows, %LOCALAPPDATA% is the fallback when XDG is unset."""
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(_platform, "IS_WINDOWS", True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    assert _platform.state_root() == tmp_path / "Local"


def test_state_root_windows_localappdata_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows last-resort fallback when LOCALAPPDATA is also unset."""
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(_platform, "IS_WINDOWS", True)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    expected = Path.home() / "AppData" / "Local"
    assert _platform.state_root() == expected


# ---------------------------------------------------------------------------
# secure_chmod
# ---------------------------------------------------------------------------


def test_secure_chmod_posix_strict(tmp_path: Path) -> None:
    """On POSIX (real run), chmod must actually set the bits."""
    if _platform.IS_WINDOWS:
        pytest.skip("POSIX-only assertion")
    f = tmp_path / "file.bin"
    f.write_text("x")
    f.chmod(0o644)
    _platform.secure_chmod(f, 0o600)
    assert stat.S_IMODE(f.lstat().st_mode) == 0o600


def test_secure_chmod_windows_logs_warning_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Windows: chmod is a no-op but emits a one-time warning per path."""
    monkeypatch.setattr(_platform, "IS_WINDOWS", True)
    # Reset the de-dup set so the warning fires on this fresh path.
    monkeypatch.setattr(_platform, "_WIN_PERMS_WARNED", set())
    f = tmp_path / "win.bin"
    f.write_text("x")
    with caplog.at_level(logging.WARNING, logger="schwab_marketdata_mcp._platform"):
        _platform.secure_chmod(f, 0o600)
        # Second call must NOT re-emit the warning.
        _platform.secure_chmod(f, 0o600)
    msgs = [r.getMessage() for r in caplog.records]
    chmod_msgs = [m for m in msgs if "chmod is no-op" in m]
    assert len(chmod_msgs) == 1


# ---------------------------------------------------------------------------
# secure_fchmod
# ---------------------------------------------------------------------------


def test_secure_fchmod_posix(tmp_path: Path) -> None:
    if _platform.IS_WINDOWS:
        pytest.skip("POSIX-only assertion")
    f = tmp_path / "fd.bin"
    f.write_text("x")
    fd = os.open(str(f), os.O_RDWR)
    try:
        _platform.secure_fchmod(fd, 0o600)
    finally:
        os.close(fd)
    assert stat.S_IMODE(f.lstat().st_mode) == 0o600


def test_secure_fchmod_windows_noop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Windows fchmod is a no-op (function does not exist on Win)."""
    monkeypatch.setattr(_platform, "IS_WINDOWS", True)
    f = tmp_path / "fd.bin"
    f.write_text("x")
    fd = os.open(str(f), os.O_RDWR)
    try:
        # Must not raise even though fchmod is unavailable on Windows.
        _platform.secure_fchmod(fd, 0o600)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# is_secure_perms
# ---------------------------------------------------------------------------


def test_is_secure_perms_missing_path_is_false(tmp_path: Path) -> None:
    assert _platform.is_secure_perms(tmp_path / "absent.json", 0o600) is False


def test_is_secure_perms_posix_strict(tmp_path: Path) -> None:
    if _platform.IS_WINDOWS:
        pytest.skip("POSIX-only assertion")
    f = tmp_path / "tok.json"
    f.write_text("{}")
    f.chmod(0o644)
    assert _platform.is_secure_perms(f, 0o600) is False
    f.chmod(0o600)
    assert _platform.is_secure_perms(f, 0o600) is True


def test_is_secure_perms_windows_lenient(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """On Windows, any readable file is considered secure (Tier A)."""
    monkeypatch.setattr(_platform, "IS_WINDOWS", True)
    f = tmp_path / "tok.json"
    f.write_text("{}")
    # Even with broad POSIX bits set on the underlying tmp_path, the Windows
    # branch only checks os.access(path, os.R_OK).
    assert _platform.is_secure_perms(f, 0o600) is True


# ---------------------------------------------------------------------------
# file_mode
# ---------------------------------------------------------------------------


def test_file_mode_posix_returns_imode(tmp_path: Path) -> None:
    if _platform.IS_WINDOWS:
        pytest.skip("POSIX-only assertion")
    f = tmp_path / "x.bin"
    f.write_text("x")
    f.chmod(0o600)
    assert _platform.file_mode(f) == 0o600


def test_file_mode_windows_returns_zero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(_platform, "IS_WINDOWS", True)
    f = tmp_path / "x.bin"
    f.write_text("x")
    assert _platform.file_mode(f) == 0


# ---------------------------------------------------------------------------
# restrictive_umask
# ---------------------------------------------------------------------------


def test_restrictive_umask_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    if _platform.IS_WINDOWS:
        pytest.skip("POSIX-only assertion")
    before = os.umask(0o022)
    os.umask(before)
    with _platform.restrictive_umask():
        current = os.umask(0o022)
        assert current == 0o077
        os.umask(current)
    assert os.umask(0o022) == before
    os.umask(before)


def test_restrictive_umask_windows_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_platform, "IS_WINDOWS", True)
    with _platform.restrictive_umask():
        pass


# ---------------------------------------------------------------------------
# exclusive_file_lock
# ---------------------------------------------------------------------------


def test_exclusive_file_lock_acquires_and_releases(tmp_path: Path) -> None:
    p = tmp_path / "lock.bin"
    p.write_bytes(b"\x00")
    fd = os.open(str(p), os.O_RDWR)
    try:
        with _platform.exclusive_file_lock(fd):
            pass
        # Second acquire must succeed after release (POSIX advisory locks
        # are recursive when the same fd holds them; on Windows msvcrt locks
        # are released by LK_UNLCK in our finally block).
        with _platform.exclusive_file_lock(fd):
            pass
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# notify_desktop
# ---------------------------------------------------------------------------


def test_notify_desktop_never_raises_on_unknown_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract: notify_desktop must swallow any failure silently."""
    monkeypatch.setattr(_platform, "IS_MACOS", False)
    monkeypatch.setattr(_platform, "IS_LINUX", False)
    monkeypatch.setattr(_platform, "IS_WINDOWS", False)
    _platform.notify_desktop("title", "message")


def test_notify_desktop_macos_uses_osascript_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_platform, "IS_MACOS", True)
    monkeypatch.setattr(_platform, "IS_LINUX", False)
    monkeypatch.setattr(_platform, "IS_WINDOWS", False)
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> object:
        calls.append(cmd)

        class _Done:
            returncode = 0

        return _Done()

    def fake_which(name: str) -> str | None:
        if name == "osascript":
            return "/usr/bin/osascript"
        return None

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("shutil.which", fake_which)
    _platform.notify_desktop("Schwab MCP", "self-test")
    assert any("osascript" in cmd[0] for cmd in calls)


def test_notify_desktop_macos_skips_when_osascript_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_platform, "IS_MACOS", True)
    monkeypatch.setattr(_platform, "IS_LINUX", False)
    monkeypatch.setattr(_platform, "IS_WINDOWS", False)
    monkeypatch.setattr("shutil.which", lambda _name: None)
    # Must not raise.
    _platform.notify_desktop("Schwab MCP", "self-test")


def test_notify_desktop_linux_uses_notify_send_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_platform, "IS_MACOS", False)
    monkeypatch.setattr(_platform, "IS_LINUX", True)
    monkeypatch.setattr(_platform, "IS_WINDOWS", False)
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> object:
        calls.append(cmd)

        class _Done:
            returncode = 0

        return _Done()

    def fake_which(name: str) -> str | None:
        if name == "notify-send":
            return "/usr/bin/notify-send"
        return None

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("shutil.which", fake_which)
    _platform.notify_desktop("Schwab MCP", "self-test")
    assert any("notify-send" in cmd[0] for cmd in calls)


def test_notify_desktop_linux_skips_when_notify_send_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_platform, "IS_MACOS", False)
    monkeypatch.setattr(_platform, "IS_LINUX", True)
    monkeypatch.setattr(_platform, "IS_WINDOWS", False)
    monkeypatch.setattr("shutil.which", lambda _name: None)
    _platform.notify_desktop("Schwab MCP", "self-test")


def test_notify_desktop_swallows_exception_from_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the subprocess shellout itself raises, notify_desktop must not propagate."""
    monkeypatch.setattr(_platform, "IS_MACOS", True)
    monkeypatch.setattr(_platform, "IS_LINUX", False)
    monkeypatch.setattr(_platform, "IS_WINDOWS", False)

    def boom(*_args: object, **_kwargs: object) -> object:
        raise OSError("simulated failure")

    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/osascript")
    monkeypatch.setattr("subprocess.run", boom)
    # Contract: never raises.
    _platform.notify_desktop("title", "msg")


# ---------------------------------------------------------------------------
# Platform constants - sanity
# ---------------------------------------------------------------------------


def test_platform_constants_match_sys_platform() -> None:
    """Sanity: the module-level constants must align with sys.platform."""
    assert (sys.platform == "win32") == _platform.IS_WINDOWS
    assert (sys.platform == "darwin") == _platform.IS_MACOS
    assert sys.platform.startswith("linux") == _platform.IS_LINUX
