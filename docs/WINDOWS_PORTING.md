# Windows Native Porting Guide — schwab-marketdata-mcp

> **Audience**: a Python intermediate developer helping Tang Keyin (kevinkda) port the
> project from macOS / Linux to Windows 10/11 native (non-WSL).
>
> **You are NOT changing core logic.** You are adding a Windows-compatible
> implementation layer behind the same public APIs, so existing macOS / Linux
> behavior stays bit-identical.
>
> **Scope** — Tier A (best-effort, experimental) per the user's brief.  Tier B
> (production-grade ACL + Windows test matrix) and Tier C (installer / package)
> are out of scope for this PR.

---

## 0. 30-second mental model

The project is a small (~3k LOC) Python 3.11+ MCP server that:

1. Persists a Schwab OAuth `token.json` under `$XDG_STATE_HOME/schwab-marketdata-mcp/`.
2. Serializes refresh-token rotation across processes via `fcntl.flock` on a `.lock` file.
3. Enforces `0o600 / 0o700` POSIX perms on token + parent dir.
4. Has a CLI health probe scheduled via `launchd` (macOS) / `crontab` (Linux)
   that fires desktop notifications via `osascript` / `notify-send`.

**On Windows native, items 2–4 break.** Item 1 is fine because `pathlib.Path` is
cross-platform.

This guide turns each break into a focused, mechanical change. **Do them in
order — later steps depend on earlier ones.**

---

## 1. POSIX-only dependency inventory (where the breaks live)

Confirmed by repo grep on commit currently checked out.  All file paths are
relative to repo root.

| # | File | Lines | Current implementation | Windows behavior | Severity |
|---|------|-------|------------------------|------------------|----------|
| 1 | `src/schwab_marketdata_mcp/security.py` | 13, 296, 304-305, 311-321 | `import fcntl` at module top; `fcntl.flock(fd, LOCK_EX)` inside `token_file_lock` | **Hard crash on import** (`ModuleNotFoundError: fcntl`) | **CRITICAL** — blocks every entry point |
| 2 | `src/schwab_marketdata_mcp/security.py` | 277, 281, 286, 314 | `os.chmod(parent, 0o700)`, `os.chmod(path, 0o600)`, `os.fchmod(fd, 0o600)` | `chmod` is silently no-op on Windows; `fchmod` does not exist (raises `AttributeError`) | **HIGH** — `os.fchmod` raises; `os.chmod` is silent footgun |
| 3 | `src/schwab_marketdata_mcp/security.py` | 191, 211-212, 252-253, 259-260 | `stat.S_IMODE(path.lstat().st_mode) != 0o600` perm checks | Always returns broad mode bits on NTFS (typically `0o666`) → every fresh-written token gets flagged `INSECURE_PERMS` | **HIGH** — false positives lock user out |
| 4 | `src/schwab_marketdata_mcp/auth_logic.py` | 215, 229 | `os.umask(0o077)` | Honored only by Python's own `open` calls; meaningless against Windows ACL. Does not raise. | LOW — silently ineffective |
| 5 | `src/schwab_marketdata_mcp/metrics.py` | 66, 70-71, 144, 146 | `os.chmod(target, 0o600)`, `target.touch(mode=0o600)`, `target.parent.mkdir(mode=0o700)` | `chmod`/`mode` ignored on NTFS | MEDIUM — perms enforced elsewhere are weaker on Win |
| 6 | `src/schwab_marketdata_mcp/server.py` | 49 | `log_dir.mkdir(parents=True, mode=0o700, exist_ok=True)` | `mode` ignored | LOW — log dir is non-secret |
| 7 | `src/schwab_marketdata_mcp/health.py` | 116, 145-174 | `Path.home() / "Desktop"` for marker file; `osascript` / `notify-send` for desktop toast | `~/Desktop` exists on Windows (good); both shellouts silently no-op (the early-returns at 162/174 already cover `sys.platform == "win32"`) | LOW — already gracefully degrades, only marker file works |
| 8 | `src/schwab_marketdata_mcp/health.py` | 260 | `os.stat(token_path).st_mode & 0o7777` | Returns broad bits on NTFS (same as #3) | HIGH — same false-positive |
| 9 | `src/schwab_marketdata_mcp/server.py` | 8-10 (comments only) | Docstring mentions `os.dup2(2, 1)` as a thing they avoid | No actual `os.dup` call in code — **non-issue** | none |
| 10 | `src/schwab_marketdata_mcp/client.py` | 480 | `os.stat(token_path).st_mode & 0o7777` | Same as #3, #8 | HIGH |
| 11 | `scripts/local-ci.sh` | entire file | Bash | Cannot run in `cmd.exe` / PowerShell without Git-Bash / WSL | MEDIUM — dev-loop only, not runtime |
| 12 | `scripts/notifier-self-test.sh` | entire file | Bash, branches on `uname -s` | Same — falls into `*)` arm, exits 1 | MEDIUM — dev-loop only |
| 13 | `scripts/ensure-uv-lock-tracked.sh` | entire file | Bash, calls `git ls-files` | Same — needs PowerShell port or Git-Bash | MEDIUM — pre-commit only |
| 14 | `docs/cron.example` | all | macOS `launchd` + Linux `crontab` snippets only | No Task Scheduler equivalent documented | MEDIUM — UX gap |
| 15 | `tests/test_security.py`, `test_health.py`, `test_auth_logic.py`, `test_client.py`, `test_tools_unit.py`, `test_metrics.py` | various — see Section 5 | Many `os.chmod` / `os.umask` / `stat.S_IMODE == 0o600` assertions | Will all fail on Windows | HIGH — but most are `skipif` candidates |
| 16 | `pyproject.toml` | 14-15 | `Operating System :: MacOS`, `Operating System :: POSIX :: Linux` classifiers only | Just metadata | LOW |
| 17 | `.github/workflows/test.yml` | 58 | Matrix is `[ubuntu-latest, macos-latest]` | No Windows runner | MEDIUM — needed to keep port alive |
| 18 | `src/schwab_marketdata_mcp/__init__.py` | 17 | Docstring claims POSIX only | Self-documenting | LOW — update at end |

**Not present (good news)**:
- No `os.fork`, `os.geteuid`, `os.setuid`, `signal.SIGUSR*`, `pwd`, `grp` calls.
- No raw `/` path concatenation that would break on Windows (everything goes through `pathlib`).
- No `os.dup` / `os.dup2` calls in actual code (only mentioned in a comment).
- `subprocess.run` always uses `argv` lists (never `shell=True` with concatenation), so no shell-injection-style portability issues.

---

## 2. Tier A scope (what you ARE doing)

You are implementing **best-effort Windows native** support, marked
**experimental** in README and `__init__.py`.

Acceptance criteria:

1. `python -c "import schwab_marketdata_mcp"` does not raise on Windows.
2. `uv run python -m schwab_marketdata_mcp.auth login_flow --dry-run` runs to
   the credential check on Windows (no browser).
3. `uv run python -m schwab_marketdata_mcp.health` returns a sensible exit code
   on Windows when `token.json` exists with default NTFS perms (i.e. it must
   **not** return `INSECURE_PERMS=5` just because the user is on Windows).
4. `uv run pytest -m "not posix_only"` reports `0 failed` on Windows 10/11
   with Python 3.11 and 3.12.
5. `bb release` equivalent on macOS / Linux still passes 100% (no regressions
   for existing users).
6. README reflects Windows native = experimental.

What you are **NOT** doing in this PR:

- Implementing Windows ACL via `pywin32` (Tier B).
- Making `bb release` (the bash-driven `local-ci.sh`) run natively in
  PowerShell (Tier B).
- Adding a `windows-latest` GitHub Actions runner (Tier B).
- Building a chocolatey / MSI installer (Tier C).

---

## 3. Implementation plan (do these in order)

### Step 3.1 — Create platform shim module (foundation for everything else)

**New file**: `src/schwab_marketdata_mcp/_platform.py`

This is the only new module. Everything else imports from it.

```python
"""Cross-platform OS shims.

This module abstracts the small set of POSIX-vs-Windows differences this
project actually uses, so the rest of the codebase stays platform-neutral.

Tier A (best-effort) Windows support:
    * file lock           — portalocker (cross-platform), fcntl.flock on POSIX
    * file permissions    — POSIX chmod where supported, no-op + warning on Windows
    * permission checks   — strict 0o600/0o700 on POSIX, "exists & readable" on Windows
    * desktop notify      — osascript / notify-send / win10toast.ToastNotifier()

Tier B (production-grade ACL via pywin32) is intentionally NOT implemented
here.  When the project upgrades to Tier B, replace `is_secure_perms` and
`set_secure_perms` with real Windows ACL checks; everything else stays.
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


# --- File locking ----------------------------------------------------------

@contextlib.contextmanager
def exclusive_file_lock(fd: int) -> Iterator[None]:
    """Acquire an exclusive (LOCK_EX) advisory lock on an open file descriptor.

    POSIX: fcntl.flock(LOCK_EX) — blocks until acquired.
    Windows: msvcrt.locking(LK_LOCK) on byte 0 — blocks (10× 1s retries).
    """
    if IS_WINDOWS:
        import msvcrt
        # LK_LOCK retries up to 10× at 1s intervals; we wrap in a longer loop.
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            try:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:  # pragma: no cover - best-effort on Windows
                pass
        return

    import fcntl
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)


# --- File permissions ------------------------------------------------------

#: 0o600 / 0o700 still drive the *intent*; on Windows we just log a warning.
_WIN_PERMS_WARNED: set[str] = set()


def secure_chmod(path: Path, mode: int) -> None:
    """Set restrictive perms.  POSIX-strict; Windows best-effort."""
    if IS_WINDOWS:
        # Tier A: rely on user-profile NTFS ACLs (default: only owner + admins
        # have access to %LOCALAPPDATA%). Document that Tier B will add
        # explicit ACL hardening via pywin32.
        key = str(path)
        if key not in _WIN_PERMS_WARNED:
            _WIN_PERMS_WARNED.add(key)
            log.warning(
                'platform=windows chmod is no-op; relying on default NTFS ACL '
                'inherited from %%LOCALAPPDATA%%. path=%s mode=%o',
                path, mode,
            )
        return
    os.chmod(path, mode)


def secure_fchmod(fd: int, mode: int) -> None:
    """fchmod equivalent.  POSIX-strict; Windows best-effort no-op."""
    if IS_WINDOWS:
        return  # NTFS ACLs already restrict; fchmod doesn't exist on Windows
    os.fchmod(fd, mode)


def is_secure_perms(path: Path, expected: int) -> bool:
    """Return True iff *path* has restrictive perms equal to *expected*.

    POSIX: strict equality on `stat.S_IMODE`.
    Windows: best-effort — returns True iff the file exists and is owner-readable
        (we cannot strictly check NTFS ACLs without pywin32).  This deliberately
        bypasses the 0o600 check so existing tests like INSECURE_PERMS do not
        false-positive.  Tier B should replace this with a real ACL check.
    """
    if not path.exists():
        return False
    if IS_WINDOWS:
        return os.access(path, os.R_OK)
    return stat.S_IMODE(path.lstat().st_mode) == expected


def file_mode(path: Path) -> int:
    """Return permission bits.  On Windows, returns 0 to signal 'unknown'.

    Callers MUST check IS_WINDOWS before treating the result as comparable
    to 0o600.  Existing call sites in security.py / health.py / client.py are
    fixed in Step 3.3 to call is_secure_perms() instead.
    """
    if IS_WINDOWS:
        return 0
    return stat.S_IMODE(path.lstat().st_mode)


@contextlib.contextmanager
def restrictive_umask() -> Iterator[None]:
    """umask(0o077) on POSIX; no-op on Windows."""
    if IS_WINDOWS:
        yield
        return
    old = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(old)


# --- XDG / state directory -------------------------------------------------

def state_root() -> Path:
    """Cross-platform state-directory root.

    Order of precedence:
      1. $XDG_STATE_HOME (always honored — lets advanced users override).
      2. Windows: %LOCALAPPDATA% (typically C:\\Users\\<u>\\AppData\\Local).
      3. POSIX fallback: ~/.local/state.
    """
    raw = os.environ.get("XDG_STATE_HOME")
    if raw:
        return Path(raw).expanduser()
    if IS_WINDOWS:
        local_app = os.environ.get("LOCALAPPDATA")
        if local_app:
            return Path(local_app)
        # Last-resort fallback (e.g. user nuked LOCALAPPDATA).
        return Path.home() / "AppData" / "Local"
    return Path.home() / ".local" / "state"


# --- Desktop notifications -------------------------------------------------

def notify_desktop(title: str, message: str) -> None:
    """Best-effort desktop toast.  Never raises."""
    try:
        if IS_MACOS:
            _notify_macos(title, message)
        elif IS_LINUX:
            _notify_linux(title, message)
        elif IS_WINDOWS:
            _notify_windows(title, message)
    except Exception:  # pragma: no cover - notifications are best-effort
        return


def _notify_macos(title: str, message: str) -> None:
    import shutil
    import subprocess
    osa = shutil.which("osascript")
    if not osa:
        return
    subprocess.run(
        [osa, "-e", f'display notification "{message}" with title "{title}" sound name "Sosumi"'],
        check=False, timeout=5,
    )


def _notify_linux(title: str, message: str) -> None:
    import shutil
    import subprocess
    ns = shutil.which("notify-send")
    if not ns:
        return
    subprocess.run([ns, "-u", "critical", title, message], check=False, timeout=5)


def _notify_windows(title: str, message: str) -> None:
    # Prefer plyer (cross-platform; bundles win10toast under the hood).
    # Fall back to PowerShell BurntToast if plyer is not installed.
    try:
        from plyer import notification
        notification.notify(title=title, message=message, app_name="Schwab MCP", timeout=5)
        return
    except Exception:
        pass
    import shutil
    import subprocess
    ps = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if not ps:
        return
    # Built-in toast via Windows.UI.Notifications (no extra module needed).
    script = (
        '[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null;'
        f'$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(1);'
        f'$t.GetElementsByTagName("text")[0].AppendChild($t.CreateTextNode("{title}")) | Out-Null;'
        f'$t.GetElementsByTagName("text")[1].AppendChild($t.CreateTextNode("{message}")) | Out-Null;'
        f'[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("Schwab MCP").Show([Windows.UI.Notifications.ToastNotification]::new($t))'
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
```

**Tests to add** for `_platform.py` — see Section 5.

**Estimated time**: 0.5 day (incl. test scaffolding).

---

### Step 3.2 — Refactor `security.py` to use the shim

**File**: `src/schwab_marketdata_mcp/security.py`

Make these mechanical edits:

1. **Remove the top-level `import fcntl`** (line 13).  This is the import that
   crashes Windows.  Replace it with:
   ```python
   from . import _platform
   ```

2. **Remove the `sys.platform == "win32"` raise** in `token_file_lock`
   (lines 304-305).  The shim handles platform branching now.

3. **Rewrite `token_file_lock`** (lines 294-321) — keep the public signature
   identical:

   ```python
   @contextlib.contextmanager
   def token_file_lock(token_path: Path) -> Iterator[None]:
       """Cross-platform exclusive lock on ``${token_path}.lock``.

       POSIX: fcntl.flock(LOCK_EX).
       Windows: msvcrt.locking(LK_LOCK) on byte 0.

       Plan §3.2.6 — serializes refresh-token rotation across processes.
       Windows native is **experimental** (see docs/WINDOWS_PORTING.md).
       """
       lock_path = token_path.with_suffix(token_path.suffix + ".lock")
       ensure_secure_dir(lock_path.parent)
       fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, TOKEN_FILE_MODE)
       try:
           _platform.secure_fchmod(fd, TOKEN_FILE_MODE)
           with _platform.exclusive_file_lock(fd):
               yield
       finally:
           os.close(fd)
       ```

4. **Replace bare `os.chmod` calls** (lines 277, 281, 286) with
   `_platform.secure_chmod`:

   ```python
   _platform.secure_chmod(parent, TOKEN_DIR_MODE)
   ...
   _platform.secure_chmod(path, TOKEN_FILE_MODE)
   ```

5. **Rewrite `_xdg_state_root`** (lines 51-61) — delegate to the shim so the
   Windows fallback to `%LOCALAPPDATA%` kicks in:

   ```python
   def _xdg_state_root() -> Path:
       return _platform.state_root()
   ```

6. **Rewrite the perms branches** in `check_token_file_state` (lines 211-213)
   and `enforce_token_perms` (lines 252-269) to use `_platform.is_secure_perms`:

   ```python
   # check_token_file_state, around line 211:
   if not _platform.is_secure_perms(path, TOKEN_FILE_MODE):
       return TokenState.INSECURE_PERMS, None

   # enforce_token_perms, around line 252:
   if not _platform.is_secure_perms(path, TOKEN_FILE_MODE):
       actual = _platform.file_mode(path)  # 0 on Windows -> hint will say so
       raise SchwabAuthError(
           reason="insecure_token_perms",
           hint=insecure_perms_hint(path, actual),
       )
   ```

7. **`insecure_perms_hint`** (lines 228-240) — make it cross-platform: when
   `actual_mode == 0`, emit a Windows-friendly hint (no `chmod` command):

   ```python
   def insecure_perms_hint(path: Path, actual_mode: int) -> str:
       if actual_mode == 0:  # Windows / unknown — no POSIX bits to report
           return (
               f"ERROR: token.json at {path} appears unreadable.\n"
               "On Windows, ensure the file is under your user-profile %LOCALAPPDATA% "
               "and not on a network share with restrictive ACLs.\n"
               "On POSIX, the file should have mode 0o600."
           )
       return (
           f"ERROR: token.json has insecure permissions (got: {oct(actual_mode)}, required: 0o600).\n"
           "Run the following to fix and retry:\n"
           f"  chmod 600 {path}\n"
           f"  chmod 700 {path.parent}\n"
           "Then restart the MCP server."
       )
   ```

8. **Update the module docstring** (lines 1-8) — replace the macOS/Linux only
   claim with: "POSIX preferred; Windows native experimental (Tier A best-effort)."

**Estimated time**: 0.25 day.

**Tests affected** (will need `skipif(IS_WINDOWS)` markers OR rewrite to use
`is_secure_perms`):
- `tests/test_security.py` lines 224, 233, 242, 250, 259, 270, 273, 296, 305,
  311, 313, 391, 443 (any `stat.S_IMODE(...) == 0o600` or `os.chmod(..., 0o600)`).
- `tests/test_metrics.py` line 23.

---

### Step 3.3 — Refactor `auth_logic.py`, `metrics.py`, `health.py`, `client.py`

#### `src/schwab_marketdata_mcp/auth_logic.py`

- **Line 215**: replace `old_umask = os.umask(0o077)` block with the shim
  context manager. The whole `try: ... finally: os.umask(old_umask)` becomes:

  ```python
  ensure_secure_dir(path.parent)
  with _platform.restrictive_umask():
      with token_file_lock(path):
          tmp = path.with_suffix(path.suffix + ".tmp")
          data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
          fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, TOKEN_FILE_MODE)
          try:
              os.write(fd, data)
              os.fsync(fd)
          finally:
              os.close(fd)
          os.replace(tmp, path)
          secure_chmod(path)
  ```

  Add `from . import _platform` at the top of the file.

#### `src/schwab_marketdata_mcp/metrics.py`

- **Lines 66, 70-71, 144, 146**: replace each `os.chmod(target, 0o600)` and
  `target.touch(mode=0o600)` with `_platform.secure_chmod(target, 0o600)`.
  `target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)` stays — the
  `mode=` kwarg is silently ignored on Windows, no harm done. Add the import.

#### `src/schwab_marketdata_mcp/server.py`

- **Lines 33-88 (`_harden_stdio`)**: this is fine as-is on Windows. The
  `RotatingFileHandler` works on NTFS. The only nit: `log_dir.mkdir(mode=0o700, ...)` is a no-op
  on Windows but does no harm. **No change needed.**

- **Line 45**: `state_root = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))`
  — replace with:

  ```python
  from . import _platform  # at top, after the _harden_stdio block but before other imports

  state_root = _platform.state_root()
  ```

  This routes Windows users to `%LOCALAPPDATA%` for logs.

#### `src/schwab_marketdata_mcp/health.py`

- **Lines 145-174 (`_notify`)**: replace the body with a single call to the shim:

  ```python
  def _notify(message: str) -> None:
      """Fire a best-effort desktop notification — never raises."""
      from . import _platform
      _platform.notify_desktop("Schwab MCP", message)
  ```

  This kills the platform-branching shellouts and gives Windows users toast
  notifications via plyer / PowerShell.

- **Line 260**: `actual = os.stat(token_path).st_mode & 0o7777` — replace with
  `actual = _platform.file_mode(token_path)`. On Windows this returns 0 and
  the hint emitted by `insecure_perms_hint` (Step 3.2 #7) handles it.

#### `src/schwab_marketdata_mcp/client.py`

- **Line 480**: `actual = os.stat(token_path).st_mode & 0o7777` — same
  treatment, replace with `_platform.file_mode(token_path)`. Add the import.

#### `src/schwab_marketdata_mcp/__init__.py`

- **Line 17**: replace the platform claim:

  ```python
  Platform: macOS 11+ / Linux fully supported.  Windows 10/11 native is
  experimental (Tier A best-effort) — see docs/WINDOWS_PORTING.md.
  ```

**Estimated time**: 0.5 day for all five files combined (mechanical search-and-replace plus running tests after each file).

---

### Step 3.4 — Add Windows scheduling docs (no code change)

**Edit**: `docs/cron.example`

Append a third section after the Linux/crontab block:

````markdown
---

## Windows native (Task Scheduler) — experimental

> **Status**: experimental.  See `docs/WINDOWS_PORTING.md` for caveats.

Save the following as `register-schwab-health.ps1` somewhere convenient and
run **once** in an elevated PowerShell:

```powershell
$Repo = "$env:USERPROFILE\code\kevinkda\schwab-marketdata-mcp"
$UvBin = "$env:USERPROFILE\.local\bin\uv.exe"
$LogDir = "$env:LOCALAPPDATA\schwab-marketdata-mcp\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$Action = New-ScheduledTaskAction `
    -Execute $UvBin `
    -Argument "--directory `"$Repo`" run python -m schwab_marketdata_mcp.health" `
    -WorkingDirectory $Repo

# Sunday 20:00 + Wednesday 21:00 + every 4 hours fallback.
$Triggers = @(
    (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday    -At 8:00PM),
    (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Wednesday -At 9:00PM),
    (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Hours 4))
)

$Principal = New-ScheduledTaskPrincipal -UserId "$env:USERNAME" -LogonType Interactive
$Settings  = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

Register-ScheduledTask `
    -TaskName "SchwabMcpHealth" `
    -Action $Action `
    -Trigger $Triggers `
    -Principal $Principal `
    -Settings $Settings `
    -Description "Schwab Market Data MCP token-health probe (read-only)."
```

To remove later:

```powershell
Unregister-ScheduledTask -TaskName "SchwabMcpHealth" -Confirm:$false
```

> **StartWhenAvailable** is the analogue of macOS launchd's `RunAtLoad` /
> the 4h fallback — Windows Task Scheduler will run a missed weekly trigger
> as soon as the laptop wakes, but **only if** `StartWhenAvailable=$true`.
````

**Edit**: also add a 3-line PowerShell equivalent of `notifier-self-test.sh`:

**New file**: `scripts/notifier-self-test.ps1`

```powershell
# scripts/notifier-self-test.ps1 — Windows analogue of notifier-self-test.sh
$ErrorActionPreference = "Stop"
$Title = "Schwab MCP"
$Msg   = "Schwab MCP self-test - if you see this, notifications work."

# Toast via Windows.UI.Notifications (no third-party module required).
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
$tpl = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(1)
$tpl.GetElementsByTagName("text")[0].AppendChild($tpl.CreateTextNode($Title)) | Out-Null
$tpl.GetElementsByTagName("text")[1].AppendChild($tpl.CreateTextNode($Msg))   | Out-Null
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($Title).Show([Windows.UI.Notifications.ToastNotification]::new($tpl))
Write-Host "[ok] toast fired (check Action Center)"

# Marker file
$Desktop = [Environment]::GetFolderPath("Desktop")
if (Test-Path $Desktop) {
    @"
# Schwab MCP - self-test marker

This file was created by ``scripts/notifier-self-test.ps1`` to confirm the
fallback markdown channel is working. You can safely delete it.
"@ | Set-Content -Path (Join-Path $Desktop "SCHWAB_REAUTH_NEEDED.md") -Encoding UTF8
    Write-Host "[ok] wrote $Desktop\SCHWAB_REAUTH_NEEDED.md"
} else {
    Write-Warning "[skip] Desktop folder not found"
}
```

**Estimated time**: 0.25 day.

---

### Step 3.5 — Update `pyproject.toml` and dependency manifest

**File**: `pyproject.toml`

1. Add Windows to classifiers (around line 14):

   ```toml
   classifiers = [
       "Development Status :: 3 - Alpha",
       "Intended Audience :: Developers",
       "License :: OSI Approved :: MIT License",
       "Operating System :: MacOS",
       "Operating System :: POSIX :: Linux",
       "Operating System :: Microsoft :: Windows",
       "Programming Language :: Python :: 3",
       "Programming Language :: Python :: 3.11",
       "Programming Language :: Python :: 3.12",
       "Topic :: Office/Business :: Financial :: Investment",
   ]
   ```

2. Add a `windows` extras group (after `dev`, around line 42):

   ```toml
   [project.optional-dependencies]
   dev = [ ... unchanged ... ]
   windows = [
       "plyer>=2.1,<3 ; platform_system == 'Windows'",
       # Tier B will add: pywin32>=306; platform_system == 'Windows'
   ]
   ```

   **Why plyer (not portalocker / win10toast)?**
   - Lock: `msvcrt.locking` is in the stdlib — no third-party needed.
   - Toast: `plyer` is the cross-platform notify abstraction (used by Kivy);
     `win10toast` is unmaintained (last release 2020), `winotify` works but is
     more bespoke. PowerShell `Windows.UI.Notifications` is the no-dependency
     fallback inside the shim.

3. Add a pytest marker so existing tests can opt out cleanly:

   ```toml
   [tool.pytest.ini_options]
   ...
   markers = [
       "last: ensure this test runs after others (used by coverage assertion).",
       "posix_only: test depends on POSIX file-permission semantics; skipped on Windows.",
   ]
   ```

**Estimated time**: 0.1 day.

> **Do NOT** run `uv add plyer` in the macOS / Linux dev environment — the
> install must be guarded by `platform_system == 'Windows'` so non-Windows
> contributors do not inherit the dependency.

---

### Step 3.6 — Update README.md

**Replace** the platform-support table (lines 17-24):

```markdown
## Platform support

|              | macOS 11+ | Linux | WSL2 (Linux subsystem) | Windows 10/11 native |
| ------------ | :-------: | :---: | :--------------------: | :------------------: |
| **v1 (now)** |     ✅    |   ✅  |    ✅ (no `/mnt/c`)    |   🧪 experimental    |

Windows native is **Tier A best-effort** as of this release — token locking
uses `msvcrt.locking` instead of `fcntl.flock`, and file permissions rely on
the default NTFS ACL inherited from `%LOCALAPPDATA%` rather than POSIX
`0o600`.  See [`docs/WINDOWS_PORTING.md`](docs/WINDOWS_PORTING.md) for the
full caveat list.  Tier B (production-grade Windows ACL + CI matrix) is on
the roadmap.
```

**Append** to the "Health probe" section (after line 137):

```markdown
> Windows users: see `docs/cron.example` § "Windows native (Task Scheduler)"
> for the PowerShell `Register-ScheduledTask` equivalent.
```

**Estimated time**: 0.1 day.

---

## 4. Tier B / C work explicitly NOT done (record so user knows)

| Tier | Item | Reason deferred |
|------|------|-----------------|
| B | `pywin32`-based ACL hardening (replace `is_secure_perms` no-op) | Requires native build toolchain; user can run without it. |
| B | `windows-latest` GitHub Actions runner | Needs the test suite to pass on Windows first; do this after Tier A merge stabilizes. |
| B | PowerShell port of `scripts/local-ci.sh` | Big rewrite (90 lines of bash). |
| B | `--service-mode` install (NSSM / SC.EXE) for non-interactive Schwab use | Non-trivial; user does not need it. |
| C | Chocolatey / WinGet manifest | Requires public release. |
| C | MSI / Inno Setup installer | Out of scope for OSS dev tool. |
| C | Windows Defender ASR rule audit | Enterprise-only concern. |

---

## 5. Test strategy (mandatory)

Add this fixture to `tests/conftest.py` (do NOT replace the existing one):

```python
import sys
import pytest

# --- Cross-platform skip helper (Tier A Windows port) ----------------------
collect_ignore_glob: list[str] = []  # noqa: PLW0245 - pytest hook

def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Honor the @pytest.mark.posix_only marker."""
    if sys.platform != "win32":
        return
    skip_posix = pytest.mark.skip(reason="POSIX-only file-permission semantics")
    for item in items:
        if "posix_only" in item.keywords:
            item.add_marker(skip_posix)
```

Then mark the existing tests that depend on POSIX perms.  Apply
`@pytest.mark.posix_only` at the **function level** to these tests:

| File | Test functions to mark |
|------|------------------------|
| `tests/test_security.py` | every test that asserts `stat.S_IMODE(...) == 0o600` or calls `os.chmod(..., 0o644)` to simulate insecure state — lines 224, 233, 242, 250, 259, 270, 273, 296, 305, 311, 313, 391, 443 |
| `tests/test_health.py` | `test_*_insecure_perms*` and any that calls `os.chmod(f, 0o644)` (lines 101-203) |
| `tests/test_auth_logic.py` | `test_atomic_write_token_*` that asserts `stat.S_IMODE(p.stat().st_mode) == 0o600` (line 130) and any `os.umask` assertion (lines 158-165) |
| `tests/test_metrics.py` | line 23 perm assertion |
| `tests/test_client.py` | the perm-injection test that does `os.chmod(f, 0o644)` (line 311) |
| `tests/test_tools_unit.py` | line 252 / 255 perm setup |

**Important**: do NOT mark tests that only *call* `os.chmod(..., 0o600)` for
setup; those calls are no-op on Windows and do not break unless the assertion
later checks the actual mode.  Read each failing test's body before slapping
the marker on.

**Add NEW Windows-specific tests** in `tests/test_platform.py`:

```python
"""Cross-platform shim tests."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from schwab_marketdata_mcp import _platform


def test_state_root_uses_localappdata_on_windows(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    if _platform.IS_WINDOWS:
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
        assert _platform.state_root() == tmp_path / "Local"
    else:
        assert _platform.state_root() == Path.home() / ".local" / "state"


def test_xdg_state_home_always_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert _platform.state_root() == tmp_path


def test_secure_chmod_no_op_on_windows(monkeypatch, tmp_path, caplog):
    f = tmp_path / "x.json"
    f.write_text("{}")
    if _platform.IS_WINDOWS:
        with caplog.at_level("WARNING"):
            _platform.secure_chmod(f, 0o600)
        assert any("chmod is no-op" in r.message for r in caplog.records)
    else:
        _platform.secure_chmod(f, 0o600)
        import stat
        assert stat.S_IMODE(f.lstat().st_mode) == 0o600


def test_exclusive_file_lock_acquires_and_releases(tmp_path):
    import os
    p = tmp_path / "lock.bin"
    p.write_bytes(b"\x00")
    fd = os.open(str(p), os.O_RDWR)
    try:
        with _platform.exclusive_file_lock(fd):
            pass
        # second acquire should succeed after release
        with _platform.exclusive_file_lock(fd):
            pass
    finally:
        os.close(fd)


def test_notify_desktop_never_raises(monkeypatch):
    # No assertion on side-effect; only contract is "never raises".
    _platform.notify_desktop("test", "test message")


def test_restrictive_umask_context_manager():
    import os
    if _platform.IS_WINDOWS:
        with _platform.restrictive_umask():
            pass  # no-op, just must not raise
    else:
        before = os.umask(0o022)
        os.umask(before)
        with _platform.restrictive_umask():
            current = os.umask(0o022)
            assert current == 0o077
            os.umask(current)
        assert os.umask(0o022) == before
        os.umask(before)
```

**Coverage target**: 100% on `_platform.py` on every platform we ship.
Use `pytest --cov=src/schwab_marketdata_mcp/_platform`.  The platform
branches that won't run on the current OS are excluded via
`# pragma: no cover` markers — see the existing pattern in
`security.py:304`.

**Estimated time**: 0.5 day for marker passes + new platform tests.

---

## 6. Risks and unfixable scenarios

These are **definitionally impossible** on Windows native — call them out
explicitly so the user does not expect parity:

1. **`launchd` plist** — macOS-only.  Use Task Scheduler (Step 3.4); same
   triggers, different format.
2. **`crontab -e`** — POSIX-only.  Task Scheduler covers it.
3. **Mode bit fidelity** — On NTFS, `os.stat().st_mode` does not faithfully
   represent ACLs.  Tier A relies on the default `%LOCALAPPDATA%` ACL (which
   is owner + admins only by default).  A user who deliberately weakens the
   ACL bypasses our checks.  Tier B (`pywin32` ACL inspection) is the only
   true fix.
4. **`fcntl.flock` cross-process semantics on SMB** — `msvcrt.locking` works
   on local NTFS but is unreliable on SMB shares (mandatory locks behave
   differently from advisory locks).  **Recommendation**: document that
   `$XDG_STATE_HOME` must be on a local drive (no `Z:\` mapped shares).
5. **Browser auto-launch in OAuth flow** — `schwab-py`'s
   `client_from_login_flow` uses `webbrowser.open()` which is cross-platform
   and works on Windows out of the box (defaults to Edge / Chrome / Firefox
   per user preference).  No change needed.
6. **Self-signed cert TLS warning** — same browser UX on Windows; user must
   click "Advanced → Proceed".  Already documented in `docs/REGISTER.md`.
7. **WSL `/mnt/c` cross-FS** — already excluded from v1.  Windows native does
   not have this issue (it's all NTFS).  This actually IMPROVES on WSL.
8. **`scripts/local-ci.sh`** — bash-only.  Tier A leaves it as is; Windows
   contributors must use Git Bash (ships with Git for Windows) or WSL to run
   the local CI gate.  Document this in README's "Contributing" section.
9. **`gitleaks` in pre-commit** — gitleaks ships a Windows binary; no port
   needed.  Just verify the user's `pre-commit` install picks it up.
10. **`detect-secrets`** — pure Python; no port needed.

---

## 7. Total time budget (Tier A)

Sum of step estimates:

| Step | Task | Time |
|------|------|------|
| 3.1 | Create `_platform.py` shim + tests | 0.5 day |
| 3.2 | Refactor `security.py` | 0.25 day |
| 3.3 | Refactor 5 other files | 0.5 day |
| 3.4 | Add `cron.example` Windows section + `notifier-self-test.ps1` | 0.25 day |
| 3.5 | Update `pyproject.toml` (classifiers + extras + marker) | 0.1 day |
| 3.6 | Update README.md | 0.1 day |
| 5 | Apply `posix_only` markers + new platform tests | 0.5 day |
| — | Smoke-test on a real Windows 10/11 box (login_flow dry-run, health probe, server start) | 0.5 day |
| — | Buffer for surprises (mypy strict on Windows, ruff cross-platform line endings, etc.) | 0.5 day |
| **Total** | | **~3.2 days** |

The user's brief estimated **1-2 days for Tier A**.  My estimate is **higher
(~3 days)** because:

- The `posix_only` marker pass is mechanical but touches ~20 tests.
- A real Windows VM smoke test catches issues that grep cannot (e.g.
  `mypy --strict` may flag platform-specific imports differently;
  `ruff format` may complain about CRLF in newly created files).
- The shim has 100% coverage requirement (per project rules) so each branch
  needs both POSIX and Windows-side tests.

Tier B (production-grade): add **2-3 more days** for `pywin32` ACL +
`windows-latest` runner + bash-to-PowerShell port of `local-ci.sh`.

Tier C (perfect): add **another 5-7 days** for installer + Defender audit +
matrix testing.

---

## 8. Friend's quickstart — "5 minutes to first commit"

### Prerequisites on the friend's Windows box

- Windows 10 (build 19041+) or Windows 11.
- Python 3.11 or 3.12 (`winget install Python.Python.3.12`).
- `uv` (`winget install astral-sh.uv` OR
  `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`).
- Git for Windows (so Git Bash is available for `local-ci.sh`):
  `winget install Git.Git`.
- A clone of `https://github.com/kevinkda/schwab-marketdata-mcp` (any branch).

### Step-by-step

1. **Branch off `main`**:
   ```powershell
   git switch -c windows-native-tier-a
   ```

2. **Bootstrap dev environment** (in regular PowerShell, not elevated):
   ```powershell
   uv sync --extra dev
   ```

3. **Run the existing test suite to confirm a clean baseline**:
   ```powershell
   uv run pytest -x
   ```
   Expect ~6-10 failures, all on perms / fcntl. **Save this output** — it
   doubles as your "before" snapshot.

4. **Implement Step 3.1** (create `src/schwab_marketdata_mcp/_platform.py`).
   Run `uv run pytest tests/test_platform.py -v` after writing the new tests
   from Section 5.

5. **Implement Step 3.2** (refactor `security.py`).  After this step:
   ```powershell
   uv run python -c "import schwab_marketdata_mcp"   # must NOT raise
   uv run python -c "from schwab_marketdata_mcp.security import token_file_lock; from pathlib import Path; import tempfile, os; d = Path(tempfile.mkdtemp()); p = d / 'tok.json'; p.write_text('{}'); ctx = token_file_lock(p); ctx.__enter__(); ctx.__exit__(None, None, None); print('lock OK')"
   ```

6. **Implement Steps 3.3-3.6** in order.  After each file edit, run:
   ```powershell
   uv run ruff check src tests
   uv run ruff format --check src tests
   uv run mypy --strict src
   ```

7. **Apply test markers (Section 5)** — do this LAST, when you know which
   tests still fail.  Use `uv run pytest -x --co -q | rg -i "fail"` if you
   need to enumerate.

8. **Final gate** on Windows:
   ```powershell
   uv run pytest --cov
   ```
   Acceptance: 0 failed, 0 errored.  Some skipped (the `posix_only` ones)
   is expected.

9. **Smoke-test the actual server bootstrap** (no real Schwab creds needed):
   ```powershell
   $env:SCHWAB_APP_KEY    = "test-key-not-real"
   $env:SCHWAB_APP_SECRET = "test-secret-not-real"  # pragma: allowlist secret
   $env:SCHWAB_CALLBACK_URL = "https://127.0.0.1:8182"
   uv run python -m schwab_marketdata_mcp.health
   # Expected exit code: 3 (token MISSING) — this proves the import chain works.
   ```

10. **Smoke-test on a real macOS / Linux box** before submitting (the user's
    main platform).  This confirms no regressions:
    ```bash
    bash scripts/local-ci.sh
    ```
    All gates must pass.  If any gate that was green before is red now,
    **stop and report back** — do not paper over it.

### How to deliver the change to Tang Keyin

The user has asked you **NOT** to push directly.  Deliver via:

```powershell
# Stage all changes
git add -A

# Commit with a clear message (English per project rules)
git commit -m "feat(windows): add Tier A best-effort Windows native support`n`n- Cross-platform shim (_platform.py) for fcntl/msvcrt/chmod/notifications`n- Windows %LOCALAPPDATA% as XDG_STATE_HOME fallback`n- Task Scheduler equivalent of launchd plist + crontab`n- pytest 'posix_only' marker; new tests for the shim`n- README + pyproject.toml classifiers updated`n`nTier B (pywin32 ACL + windows-latest CI runner) intentionally NOT in scope."

# Generate a patch file the user can review locally
git format-patch main..HEAD --stdout > windows-native-tier-a.patch

# OR: if the user prefers a branch on their fork, push to YOUR fork:
# git remote add fork https://github.com/<your-handle>/schwab-marketdata-mcp
# git push -u fork windows-native-tier-a
# Then open a PR against kevinkda/schwab-marketdata-mcp:main

# Send Tang Keyin:
#   1. The patch file (or PR URL).
#   2. Output of `uv run pytest --cov` from your Windows box.
#   3. Output of `bash scripts/local-ci.sh` from a macOS / Linux box.
#   4. A note listing which tests you marked `posix_only` and why.
```

### What NOT to do

- **Do not** delete or refactor any test that is not platform-specific.
- **Do not** add `pywin32` to dependencies (that is Tier B).
- **Do not** change the OAuth flow, the Schwab API client, or any tool
  implementation under `src/schwab_marketdata_mcp/tools/`.
- **Do not** force-push to `main`.
- **Do not** commit anything under `.env`, `.local`, `htmlcov/`, or
  `~/.local/state/schwab-marketdata-mcp/` (the `.gitignore` should already
  catch these — verify with `git status` before commit).
- **Do not** raise the `coverage.fail_under` threshold; the existing 85%
  global / 100% on critical-modules contract stays.

### When to ask Tang Keyin a question

- If `mypy --strict` fails on Windows in a way that does not on macOS
  (the strict-platform branches sometimes need `# type: ignore[attr-defined]`).
- If a test marked `posix_only` is actually NOT posix-only on closer reading
  (sometimes a test calls `os.chmod` only as setup, not as assertion).
- If the `_platform.notify_desktop` PowerShell fallback fails on the
  friend's box (Windows N / KN editions ship without `Windows.UI.Notifications` —
  in that case fall back to a simple `[System.Windows.Forms.MessageBox]::Show`
  or document the limitation).

---

## 9. Summary checklist (for Tang Keyin's review)

- [ ] `_platform.py` created with `IS_WINDOWS`, `exclusive_file_lock`, `secure_chmod`, `is_secure_perms`, `state_root`, `notify_desktop`, `restrictive_umask`.
- [ ] `security.py` no longer imports `fcntl` at module top.
- [ ] `security.py:token_file_lock` uses `_platform.exclusive_file_lock`.
- [ ] `security.py:check_token_file_state` and `enforce_token_perms` use `_platform.is_secure_perms`.
- [ ] `auth_logic.py:atomic_write_token` uses `_platform.restrictive_umask()`.
- [ ] `metrics.py`, `health.py`, `client.py` chmod/stat calls routed through shim.
- [ ] `server.py:_harden_stdio` state-root resolution uses `_platform.state_root()`.
- [ ] `__init__.py` docstring reflects experimental Windows support.
- [ ] `pyproject.toml`: Windows classifier, `[project.optional-dependencies].windows`, `posix_only` marker.
- [ ] `docs/cron.example`: Task Scheduler section added.
- [ ] `scripts/notifier-self-test.ps1` created.
- [ ] `docs/WINDOWS_PORTING.md` (this file).
- [ ] `README.md`: platform-support table updated.
- [ ] `tests/test_platform.py` new file.
- [ ] `tests/conftest.py` honors `posix_only` marker.
- [ ] All POSIX-only tests marked.
- [ ] `uv run pytest --cov` green on Windows 10/11 + macOS + Linux.
- [ ] `bash scripts/local-ci.sh` green on macOS / Linux.
- [ ] Patch / PR delivered with no force-push.

---

*End of guide.  Ping Tang Keyin if anything in Section 6 ("unfixable") turns
out to be fixable — that means we found a Tier B candidate worth doing.*
