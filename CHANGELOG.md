# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Windows native support (Tier A best-effort, experimental)**: new
  `_platform.py` shim abstracts `fcntl.flock` ↔ `msvcrt.locking`,
  `os.chmod` (POSIX) ↔ NTFS-ACL no-op, `osascript` / `notify-send` ↔
  plyer / PowerShell toasts, `XDG_STATE_HOME` ↔ `%LOCALAPPDATA%`.
  `python -c "import schwab_marketdata_mcp"` no longer crashes with
  `ModuleNotFoundError: fcntl` on Windows.
  - `pyproject.toml` now ships an optional `[windows]` extras group
    (`pip install schwab-marketdata-mcp[windows]`) pinning
    `plyer>=2.1,<3 ; platform_system == 'Windows'`.
  - `pytest --strict-markers` registers a new `posix_only` marker that
    `tests/conftest.py` auto-skips on Windows; existing tests that
    assert exact POSIX bits (`stat.S_IMODE == 0o600`, `os.chmod 0o644`)
    are tagged so the suite stays green on either platform.
  - `tests/test_platform.py` exercises both branches of every shim
    helper (state_root, secure_chmod, secure_fchmod, is_secure_perms,
    file_mode, restrictive_umask, exclusive_file_lock, notify_desktop)
    via `monkeypatch.setattr(_platform, "IS_WINDOWS", True)` so the
    Windows branches reach 100% coverage on a Linux / macOS CI runner.
  - `docs/cron.example` gains a Windows Task Scheduler section with a
    ready-to-run `Register-ScheduledTask` PowerShell snippet (Sunday
    20:00 + Wednesday 21:00 + 4h fallback).
  - `scripts/notifier-self-test.ps1` is the PowerShell analogue of
    `scripts/notifier-self-test.sh` (toast via `Windows.UI.Notifications`
    - Desktop marker fallback).
  - **Known limitations** (still requires Tier B): NTFS ACLs are not
    inspected — we trust the default `%LOCALAPPDATA%` ACL; SMB-mapped
    state dirs are unsupported (`msvcrt.locking` is unreliable on SMB);
    `scripts/local-ci.sh` is still bash-only (use Git Bash or WSL).

- Initial implementation of Schwab Market Data Production MCP server
  (12 tools: 10 business + 2 meta).
- Stdio JSON-RPC protocol with safe-print, RotatingFileHandler, Bearer redact
  filter (plan §3.2.3).
- TokenState machine (MISSING / INSECURE_PERMS / MALFORMED / VALID) for
  circular-dependency-free token health checks (plan §3.2.2.1).
- Token-bucket rate limiter (120 req/min, releases slot during retry,
  plan §3.2.4).
- Cross-process file lock for token rotate-on-use (`fcntl.flock`).
- 247+ unit + integration tests with OWASP-driven matrix (2017+2021+2025),
  93%+ coverage, 4 critical modules at 100%.
- Three documentation guides:
  - `docs/REGISTER.md` — Schwab Developer Portal app + Cursor mcp.json +
    envFile precedence + troubleshooting.
  - `docs/THREAT_MODEL.md` — STRIDE threat model + token field drift mitigation
    - schwab-py upgrade checklist.
  - `docs/WINDOWS_PORTING.md` — Tier-A/B/C effort estimates + ready-to-paste
    `_platform.py` shim for friend-implemented Windows-native port.
- Bilingual READMEs (English master + Chinese mirror via README_zh.md, language
  switcher header).
- `chore(license)`: switched from Unlicense to MIT for upstream-deps
  consistency (Copyright 2026 Tang Keyin).
- `feat(client)`: stable User-Agent
  (`schwab-marketdata-mcp/<ver> python/<ver> schwab-py/<ver>`) so Schwab
  Dashboard reports recognizable Device Type.
- `fix(server)`: bootstrap dotenv before importing tool modules (server.py +
  auth.py share `bootstrap.py`).
- `chore(pre-commit)`: 4 hook root-cause fix for offline CDD environments
  (gitleaks→manual, ruff/ruff-format/mypy→`uv run`, detect-secrets baseline
  regen).
- `chore(lint)`: markdownlint-cli2 config + 49 errors fixed across docs.
- `docs(readme)`: Acknowledgements section crediting schwab-py (Alex Golec, MIT)
  and upstream MIT/BSD-3 dependencies.

### Security

- Path allow list rejects symlinks, `..`, and out-of-XDG-state paths.
- File permissions enforced: token.json mode 0o600, parent dir mode 0o700.
- detect-secrets + gitleaks (manual stage) integration via pre-commit.
- All secrets tagged with `# pragma: allowlist secret` for documented
  placeholders.

### Compatibility

- Python 3.11+ required (`pyproject.toml` `requires-python = ">=3.11"`).
- Tested on macOS 11+ / Linux. Windows native is experimental
  (see `docs/WINDOWS_PORTING.md`).
- mcp Python SDK >=1.6,<2.0; schwab-py >=1.5.1,<1.6;
  httpx >=0.28.1,<0.29; respx >=0.22.0,<0.24.

[Unreleased]: https://github.com/kevinkda/schwab-marketdata-mcp/compare/...HEAD
