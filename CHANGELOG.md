# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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
