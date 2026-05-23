"""Cross-platform OS shims.

This module abstracts the small set of POSIX-vs-Windows differences this
project actually uses, so the rest of the codebase stays platform-neutral.

Tier A (best-effort) Windows support:
    * file lock           - msvcrt.locking on Windows; fcntl.flock on POSIX
    * file permissions    - POSIX chmod where supported, no-op + warning on Windows
    * permission checks   - strict 0o600/0o700 on POSIX, "exists & readable" on Windows
    * desktop notify      - osascript / notify-send / plyer / PowerShell

Tier B (production-grade ACL via pywin32) is intentionally NOT implemented
here.  When the project upgrades to Tier B, replace ``is_secure_perms`` and
``secure_chmod`` with real Windows ACL checks; everything else stays.

Plan reference: docs/WINDOWS_PORTING.md (Tier A best-effort).
"""

from __future__ import annotations

import contextlib
import logging
import os
import stat
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Final

IS_WINDOWS: Final[bool] = sys.platform == "win32"
IS_MACOS: Final[bool] = sys.platform == "darwin"
IS_LINUX: Final[bool] = sys.platform.startswith("linux")

log = logging.getLogger("schwab_marketdata_mcp._platform")


# ---------------------------------------------------------------------------
# File locking
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def exclusive_file_lock(fd: int) -> Iterator[None]:
    """Acquire an exclusive (LOCK_EX) advisory lock on an open file descriptor.

    POSIX: ``fcntl.flock(LOCK_EX)`` - blocks until acquired.
    Windows: ``msvcrt.locking(LK_LOCK)`` on byte 0 - blocks (retries internally).
    """
    if IS_WINDOWS:  # pragma: no cover - windows-only branch
        import msvcrt  # type: ignore[import-not-found,unused-ignore]

        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined,unused-ignore]
        try:
            yield
        finally:
            try:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined,unused-ignore]
            except OSError:
                # Best-effort release: another lock holder or the file may
                # already be closed by the time we get here.
                pass
        return

    import fcntl

    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# File permissions
# ---------------------------------------------------------------------------

#: 0o600 / 0o700 still drive the *intent*; on Windows we just log a warning.
_WIN_PERMS_WARNED: set[str] = set()


def secure_chmod(path: Path, mode: int) -> None:
    """Set restrictive permissions.  POSIX-strict; Windows best-effort no-op."""
    if IS_WINDOWS:  # pragma: no cover - windows-only branch
        # Tier A: rely on user-profile NTFS ACLs (default: only the owner +
        # admins have access to %LOCALAPPDATA%).  Tier B will add explicit ACL
        # hardening via pywin32.
        key = str(path)
        if key not in _WIN_PERMS_WARNED:
            _WIN_PERMS_WARNED.add(key)
            log.warning(
                "platform=windows chmod is no-op; relying on default NTFS ACL "
                "inherited from %%LOCALAPPDATA%%. path=%s mode=%o",
                path,
                mode,
            )
        return
    os.chmod(path, mode)


def secure_fchmod(fd: int, mode: int) -> None:
    """``fchmod`` equivalent.  POSIX-strict; Windows best-effort no-op."""
    if IS_WINDOWS:  # pragma: no cover - windows-only branch
        return
    os.fchmod(fd, mode)


def is_secure_perms(path: Path, expected: int) -> bool:
    """Return ``True`` iff *path* has restrictive perms equal to *expected*.

    POSIX: strict equality on ``stat.S_IMODE``.
    Windows: best-effort - returns ``True`` iff the file exists and is
    owner-readable (we cannot strictly check NTFS ACLs without ``pywin32``).
    This deliberately bypasses the 0o600 check so existing tests like
    ``INSECURE_PERMS`` do not false-positive.  Tier B should replace this
    with a real ACL check.
    """
    if not path.exists():
        return False
    if IS_WINDOWS:  # pragma: no cover - windows-only branch
        return os.access(path, os.R_OK)
    return stat.S_IMODE(path.lstat().st_mode) == expected


def file_mode(path: Path) -> int:
    """Return permission bits.  On Windows, returns ``0`` to signal "unknown".

    Callers MUST check :data:`IS_WINDOWS` before treating the result as
    comparable to ``0o600``.  Existing call sites in ``security.py`` /
    ``health.py`` / ``client.py`` are routed through :func:`is_secure_perms`
    instead.
    """
    if IS_WINDOWS:  # pragma: no cover - windows-only branch
        return 0
    return stat.S_IMODE(path.lstat().st_mode)


@contextlib.contextmanager
def restrictive_umask() -> Iterator[None]:
    """``umask(0o077)`` on POSIX; no-op on Windows."""
    if IS_WINDOWS:  # pragma: no cover - windows-only branch
        yield
        return
    old = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(old)


# ---------------------------------------------------------------------------
# XDG / state directory
# ---------------------------------------------------------------------------


def state_root() -> Path:
    """Cross-platform state-directory root.

    Order of precedence:
        1. ``$XDG_STATE_HOME`` (always honored - lets advanced users override).
        2. Windows: ``%LOCALAPPDATA%`` (typically ``C:\\Users\\<u>\\AppData\\Local``).
        3. POSIX fallback: ``~/.local/state``.
    """
    raw = os.environ.get("XDG_STATE_HOME")
    if raw:
        return Path(raw).expanduser()
    if IS_WINDOWS:  # pragma: no cover - windows-only branch
        local_app = os.environ.get("LOCALAPPDATA")
        if local_app:
            return Path(local_app)
        return Path.home() / "AppData" / "Local"
    return Path.home() / ".local" / "state"


# ---------------------------------------------------------------------------
# Desktop notifications
# ---------------------------------------------------------------------------


def notify_desktop(title: str, message: str) -> None:
    """Best-effort desktop toast.  Never raises."""
    try:
        if IS_MACOS:
            _notify_macos(title, message)
        elif IS_LINUX:
            _notify_linux(title, message)
        elif IS_WINDOWS:  # pragma: no cover - windows-only branch
            _notify_windows(title, message)
    except Exception:
        # Notifications are best-effort - never propagate failures.
        return


def _notify_macos(title: str, message: str) -> None:
    import shutil
    import subprocess

    osa = shutil.which("osascript")
    if not osa:
        return
    subprocess.run(
        [
            osa,
            "-e",
            f'display notification "{message}" with title "{title}" sound name "Sosumi"',
        ],
        check=False,
        timeout=5,
    )


def _notify_linux(title: str, message: str) -> None:
    import shutil
    import subprocess

    ns = shutil.which("notify-send")
    if not ns:
        return
    subprocess.run([ns, "-u", "critical", title, message], check=False, timeout=5)


def _notify_windows(title: str, message: str) -> None:  # pragma: no cover - windows-only branch
    # Prefer plyer (cross-platform; bundles win10toast under the hood).
    # Fall back to PowerShell Windows.UI.Notifications if plyer is missing.
    try:
        from plyer import notification

        notification.notify(title=title, message=message, app_name="Schwab MCP", timeout=5)
        return
    except Exception as exc:
        log.debug("plyer unavailable, falling back to PowerShell toast: %s", exc)

    import shutil
    import subprocess

    ps = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if not ps:
        return
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
        "ContentType=WindowsRuntime] | Out-Null;"
        "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(1);"
        f'$t.GetElementsByTagName("text")[0].AppendChild($t.CreateTextNode("{title}")) | Out-Null;'
        f'$t.GetElementsByTagName("text")[1].AppendChild($t.CreateTextNode("{message}")) | Out-Null;'
        '[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("Schwab MCP")'
        ".Show([Windows.UI.Notifications.ToastNotification]::new($t))"
    )
    subprocess.run([ps, "-NoProfile", "-Command", script], check=False, timeout=5)


__all__ = [
    "IS_LINUX",
    "IS_MACOS",
    "IS_WINDOWS",
    "exclusive_file_lock",
    "file_mode",
    "is_secure_perms",
    "notify_desktop",
    "restrictive_umask",
    "secure_chmod",
    "secure_fchmod",
    "state_root",
]
