# Windows Tier A — Real-Hardware Verification Guide

![Status](https://img.shields.io/badge/status-pending%20verification%20on%20Windows%20hardware-yellow)
![Tier](https://img.shields.io/badge/tier-A%20best--effort-lightgrey)
![Audience](https://img.shields.io/badge/audience-volunteer%20Windows%20user-blue)

> **Status: pending verification on Windows hardware.**
>
> The Tier A Windows port (see `docs/WINDOWS_PORTING.md`) was implemented and
> unit-tested on macOS / Linux only.  The maintainer (kevinkda) does **not own
> Windows hardware** at the time of v0.3.0, so this guide exists so a
> volunteer with a real Windows 10 / 11 box can run the acceptance checklist
> below and report back via a GitHub issue.
>
> Until at least one row in §5 (Verification log) is filled in by a real
> human on real hardware, every Windows-related claim in `README.md` and
> `docs/WINDOWS_PORTING.md` is **best-effort, not verified.**

---

## 0. Why this document exists

`docs/WINDOWS_PORTING.md` describes Tier A — a "best-effort" Windows port
that:

1. Hides `fcntl` behind a thin shim so the module imports on Windows.
2. Uses `msvcrt.locking` for cross-process token lock serialization.
3. Skips POSIX-mode permission checks on NTFS (returns "n/a" instead of
   false-positive `INSECURE_PERMS`).
4. Falls back to a `plyer`-based desktop toast (or a no-op) for the health
   probe, since `osascript` / `notify-send` are POSIX-only.

All four behaviors **compile and unit-test on Linux** (the test suite uses
mocks / `monkeypatch`), but compile-time success ≠ runtime success on a
real Windows host.  This document closes that gap by listing the **smallest
possible set of manual checks** a non-developer can run.

---

## 1. Prerequisites

The volunteer needs:

| Requirement                | Why                                                     |
| -------------------------- | ------------------------------------------------------- |
| Windows 10 (1909+) or 11   | `msvcrt.locking` requires a modern Windows kernel.      |
| Python 3.11+ (from python.org or `winget install Python.Python.3.11`) | Project pins Python 3.11+. WSL is **not** acceptable for this verification — we are testing **native** Windows. |
| Git for Windows (Git-Bash) | `scripts/local-ci.sh` is bash; running it inside Git-Bash is easiest. |
| `uv` ≥ 0.4.0               | `winget install astral-sh.uv` or `pip install uv`.      |
| A Schwab Developer Portal app with `https://127.0.0.1:8182` callback | Only needed if you want to do a live OAuth handshake; the unit tests in §3 do **not** require live credentials. |
| **NOT** required: Admin / Visual Studio / WSL / Docker | Tier A is pure Python + stdlib + `plyer`. |

---

## 2. One-time setup

```powershell
# 1. Clone the repo somewhere short — Windows MAX_PATH bites long paths.
git clone https://github.com/kevinkda/schwab-marketdata-mcp.git C:\src\sm-mcp
cd C:\src\sm-mcp

# 2. Sync the dev environment.  uv handles platform-specific extras.
uv sync --extra dev

# 3. Confirm plyer is installed (needed for desktop toasts on Windows).
uv run python -c "import plyer; print(plyer.__version__)"
# Expected: any version ≥ 2.1.0 prints; ImportError = stop and report.
```

If step 3 fails, **stop and file an issue** titled
`Windows Tier A: plyer not installed by uv sync --extra dev` with the
output of `uv run python -m pip list | findstr plyer`.

---

## 3. Acceptance criteria (8 items)

Run each command **in order** in a fresh PowerShell or Git-Bash window.
Tick the box only after the actual output matches the **Expected** column.

### AC-1 — Module imports cleanly on Windows native

```powershell
uv run python -c "import schwab_marketdata_mcp.security; print('ok')"
```

| Field    | Value                                                              |
| -------- | ------------------------------------------------------------------ |
| Expected | Single line `ok` (no `ModuleNotFoundError: fcntl`).                |
| Why      | The `fcntl` shim must hide the POSIX-only import on Windows.       |
| Fail mode | `ModuleNotFoundError: fcntl` → `security.py` shim regression.     |

### AC-2 — `auth_logic` and `client` import cleanly

```powershell
uv run python -c "import schwab_marketdata_mcp.auth_logic, schwab_marketdata_mcp.client; print('ok')"
```

| Field    | Value                                                              |
| -------- | ------------------------------------------------------------------ |
| Expected | Single line `ok`.                                                  |
| Why      | These are the two largest modules; both transitively depend on `security`. |
| Fail mode | `AttributeError: module 'os' has no attribute 'fchmod'` → fchmod not gated; treat as **CRITICAL**. |

### AC-3 — Full unit-test suite passes

```bash
# In Git-Bash (recommended) or Powershell:
uv run pytest -q
```

| Field    | Value                                                              |
| -------- | ------------------------------------------------------------------ |
| Expected | Final line `=== <N> passed in <T>s ===` with **0 failures, 0 errors**. Some tests may show as `SKIPPED` (POSIX-only assertions). |
| Why      | Confirms that the `pytest.skipif(sys.platform == 'win32', …)` markers are correctly placed and that the cross-platform code path actually runs. |
| Fail mode | Any **failure** (not skip) — capture the full trace and file an issue. |

### AC-4 — `health_check` returns `valid` against a real token

```powershell
# Pre-req: completed `uv run python -m schwab_marketdata_mcp.auth login_flow` once.
uv run python -m schwab_marketdata_mcp.health
echo $LASTEXITCODE
```

| Field    | Value                                                              |
| -------- | ------------------------------------------------------------------ |
| Expected | JSON output containing `"token_state": "valid"` AND `$LASTEXITCODE` (or `$?` in Git-Bash) is `0`. |
| Why      | Confirms the platform-aware token-perm path returns `n/a` on NTFS instead of false-positive `INSECURE_PERMS`. |
| Fail mode | `"token_state": "insecure_perms"` on a fresh token → §3 perm-check still wired to POSIX. |

### AC-5 — Cross-process token lock works under contention

```powershell
# Open two PowerShell windows side-by-side.
# Window 1:
uv run python -c "
from schwab_marketdata_mcp.security import token_file_lock, resolve_token_path
import time, os
p = resolve_token_path()
with token_file_lock(p):
    print('window 1 holds the lock'); time.sleep(15)
print('window 1 released')
"
# Window 2 (start within 5 s of window 1):
uv run python -c "
from schwab_marketdata_mcp.security import token_file_lock, resolve_token_path
import time
p = resolve_token_path()
t0 = time.time()
with token_file_lock(p):
    print(f'window 2 acquired after {time.time()-t0:.1f}s')
"
```

| Field    | Value                                                              |
| -------- | ------------------------------------------------------------------ |
| Expected | Window 2 prints `window 2 acquired after 1<N>s` where `<N>` is roughly the remaining time of window 1 (i.e. window 2 **blocked**, did not race). |
| Why      | Confirms `msvcrt.locking(LK_LOCK)` actually serializes (it is a **byte-range lock**, not a global mutex). |
| Fail mode | Both windows print "acquired" within < 100 ms of each other → lock is a no-op; the refresh-token rotation race is **not** mitigated. **CRITICAL** — file an issue and **do not run live OAuth**. |

### AC-6 — Local CI gate passes

Run inside **Git-Bash**:

```bash
bash scripts/local-ci.sh
```

| Field    | Value                                                              |
| -------- | ------------------------------------------------------------------ |
| Expected | All 7 sections print, final line is `local-ci: all gates green`. |
| Why      | This is the same gate CI runs.  Failing here = the GitHub Actions `windows-latest` matrix would also fail. |
| Fail mode | `bash: scripts/local-ci.sh: line N: <something>: command not found` → likely a Windows path translation (`/usr/bin/env` etc.). Not blocking, but worth filing. |

### AC-7 — Desktop toast appears (or fails-soft)

```powershell
uv run python -c "
from schwab_marketdata_mcp.health import _emit_desktop_toast
_emit_desktop_toast('Schwab MCP test', 'Tier A verification')
"
```

| Field    | Value                                                              |
| -------- | ------------------------------------------------------------------ |
| Expected | Either: (a) a Windows toast notification briefly appears in the bottom-right corner, OR (b) the call returns silently with **no exception**. |
| Why      | `plyer.notification.notify` may silently no-op on certain Windows builds (Server Core, headless). Either outcome is acceptable for Tier A; an exception is **not**. |
| Fail mode | `Exception: ...` printed → the Windows code path is raising, not failing-soft. |

### AC-8 — `gh repo view` private check still works

```powershell
gh repo view kevinkda/stock-personal --json isPrivate -q .isPrivate
```

| Field    | Value                                                              |
| -------- | ------------------------------------------------------------------ |
| Expected | Prints `true` and exits `0`.  (This validates that the workflows-skill's pre-flight private-repo gate is reachable on Windows.) |
| Why      | Tier A's Windows port does not change `gh` invocation semantics, but the volunteer should confirm `gh` is on PATH on Windows so the workflows skill works end-to-end. |
| Fail mode | `gh: command not found` → install GitHub CLI: `winget install GitHub.cli`. |

---

## 4. Expected output sample

Below is what AC-3 (`uv run pytest -q`) **should** look like on Windows.
The exact pass count drifts as tests are added; the shape is what matters.

```text
$ uv run pytest -q
............s.s..........s............ssssss....                        [ 38%]
.......................................                                  [ 76%]
....................                                                      [100%]
=== 162 passed, 8 skipped in 11.42s ===
```

Note: `s` (skipped) entries are expected — they are POSIX-only assertions
(file-mode `0o600` checks) gated by `pytest.mark.skipif(sys.platform == "win32")`.

A successful AC-4 looks like:

```json
{
  "ok": true,
  "token_state": "valid",
  "token_age_seconds": 1234,
  "expires_in_seconds": 4166,
  "perm_check": "n/a (windows)",
  "schwab_py_version": "1.5.1",
  "mcp_version": "0.3.0"
}
```

The `"perm_check": "n/a (windows)"` field is the most important Windows-specific
indicator — on macOS / Linux it would say `"0o600"`.

---

## 5. Verification log (fill in when you run)

| Date (UTC) | Windows version  | Python version | uv version | All 8 ACs pass? | Volunteer GitHub handle | Notes |
| ---------- | ---------------- | -------------- | ---------- | --------------- | ----------------------- | ----- |
| _TBD_      | _e.g. Win11 24H2_ | _3.11.9_       | _0.4.18_   | _Y / N_         | _your-github-handle_    | _link to issue/PR if any_ |

When at least **one** row is filled in with all 8 ACs green:

1. Update the `Status` badge at the top of this file from `pending verification`
   to `verified on <Win-version> (<date>)`.
2. Open a PR titled `docs(windows): mark Tier A verified by <volunteer>`.
3. Reference this PR in the next `CHANGELOG.md` entry.

---

## 6. Common fail modes & remediation

| Symptom                                                    | Likely cause                                              | Remediation                                                                                                |
| ---------------------------------------------------------- | --------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| `ModuleNotFoundError: fcntl` on import                     | `security.py` shim regressed; conditional import broke.   | File issue with stack trace; revert to last known-good commit on `windows-native-tier-a` branch.            |
| `AttributeError: module 'os' has no attribute 'fchmod'`    | `auth_logic.py` or `metrics.py` calls `os.fchmod` unguarded. | File issue; remediation = wrap call in `if hasattr(os, "fchmod"):`.                                         |
| `ImportError: No module named 'plyer'`                     | `uv sync --extra dev` did not install `plyer` because the platform marker is wrong in `pyproject.toml`. | `uv pip install plyer` as a workaround; file issue to fix the dep marker (`plyer; sys_platform == 'win32'` should be unconditional or use `extra`). |
| `OSError: [Errno 13] Permission denied` writing `token.json` | NTFS ACL forbids the user from writing to `%LOCALAPPDATA%`. | Confirm `$env:LOCALAPPDATA` resolves; try `--config-dir "$HOME\.schwab-marketdata-mcp"` to override.        |
| `msvcrt.error: [Errno 13] Permission denied` from `token_file_lock` | Antivirus is holding the `.lock` file open.              | Whitelist the project dir in AV; retry. Document AV used in §5 notes column.                               |
| `plyer.notification.notify(...)` raises instead of no-op    | `plyer` ≥ 2.1.0 changed the Windows backend.              | File issue; remediation = wrap `_emit_desktop_toast` body in `try / except Exception: pass`.               |
| AC-5 both windows acquire lock simultaneously              | `msvcrt.locking(LK_LOCK)` is being called on a closed fd, or fd is opened with mode `'r'` not `'r+'`. | **Treat as CRITICAL.** File issue and **do not use this build for live OAuth**; the refresh-token rotation race is unmitigated. |
| `bash: scripts/local-ci.sh: command not found`             | Running in `cmd.exe` not Git-Bash.                        | Switch to Git-Bash. Long-term: see `docs/WINDOWS_PORTING.md` §5 for a PowerShell port roadmap.             |
| `gh: command not found`                                    | GitHub CLI not installed.                                 | `winget install GitHub.cli` and re-open the shell.                                                         |
| AC-3 reports >0 failures                                   | Real Windows-specific test regression OR a flaky network test. | Re-run; if reproducible, save the full pytest output and file an issue with **all 8 fail modes ruled out** in the description. |

---

## 7. Reporting back

If **all 8 ACs pass**, file:

```text
Title: Windows Tier A: verified on Win<version> by @<handle>
Body : - All 8 ACs in docs/WINDOWS_VERIFICATION.md passed on <date>.
       - System: Windows <version>, Python <version>, uv <version>.
       - Notes: <anything weird, e.g. AV blocked .lock briefly>.
       - PR: opening doc PR to flip the status badge.
```

If **any AC fails**, file one issue per failure, each titled:

```text
Title: Windows Tier A: AC-<N> failed on Win<version>
Body : - Command run:  <copy-paste>
       - Actual output: <copy-paste>
       - Expected:      <from this doc>
       - Hypothesis:    <if any>
```

The maintainer will triage; do **not** attempt to fix Tier A bugs without
opening an issue first — Tier A is intentionally minimal and any "fix"
might cross the line into Tier B (production-grade ACL via `pywin32`).

---

## 8. Out of scope for this doc

The following are explicitly Tier B / Tier C and **not** part of this
verification:

- `pywin32` ACL hardening so the token file is unreadable by other users.
- `windows-latest` GitHub Actions matrix.
- MSI / `pipx` installer.
- Task Scheduler XML for the cron health probe.

See `docs/WINDOWS_PORTING.md` §6–7 for the Tier B/C roadmap.

---

_Last updated: 2026-05-23 (v0.3 sprint, Sprint A Deliverable 1)._
