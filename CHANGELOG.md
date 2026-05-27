# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.0] - 2026-05-27

### Added

- **`get_iv_percentile` (15th MCP tool)** — surfaces ATM IV percentile
  rank for an underlying versus N days of cached history (default
  252 ≈ 1 trading year). Buckets: `30d` / `60d` / `90d`. With
  `refresh=False` (default) the tool serves only from the local
  DuckDB `iv_history` table — appropriate for batch / dashboard
  reads. With `refresh=True` it fetches a fresh option chain via
  `get_option_chain`, persists a snapshot to
  `option_chain_snapshots`, and aggregates today's ATM IV before
  computing the rank. Sample-count < 30 → `percentile_rank=null` +
  `sample_count_below_30` warning so callers do not over-interpret a
  tiny sample.
- **`option_chain_snapshots` analytics table** — row-normalised
  cache of every option chain returned by `get_option_chain`. Schema:
  `(underlying, snapshot_at, expiry, strike, call_put)` PK with
  bid/ask/last/volume/open_interest plus the full Greek vector
  (delta/gamma/theta/vega/rho) and `implied_vol`. Index on
  `(underlying, snapshot_at DESC)` for fast latest-snapshot fetch.
  Coexists with the legacy raw-JSON `option_chain_cache` table —
  the read-back hit path is unchanged; the new table is purely for
  analytics (`aggregate_atm_iv` / `get_iv_percentile_rank`). v0.4
  spec called the new table `option_chain_cache`; we shipped it as
  `option_chain_snapshots` to avoid colliding with the legacy
  raw-JSON cache table of the same name from v0.2.
- **`iv_history` materialised aggregation** — one row per
  `(underlying, asof_date, expiry_bucket)` carrying the ATM IV
  computed from the most recent `option_chain_snapshots` row at or
  before `asof_date`. Written by `Cache.aggregate_atm_iv` (also
  exposed via `get_iv_percentile(refresh=True)`). Empty buckets are
  still written with `sample_count = 0` so the lookback window
  preserves correct calendar density.

### Changed

- **`get_option_chain` now persists a flattened snapshot** to
  `option_chain_snapshots` opportunistically — failures are
  warning-logged and never break the tool. The response is enriched
  with two new fields: `_cache_status` (already present from the
  legacy cache layer) and `_cached_rows` (number of rows written to
  the analytics table on this call). The tool's input signature is
  **unchanged** — purely additive on the response side.
- **Tool count** 14 → 15. `supported_tool_names()` and
  `get_server_info().supported_tools` both reflect the new tool.

### Compatibility

- Test count: 337 → 365 passing on Linux. Coverage: 89.08% → 88.98%
  (slight dip is the new `cache.py` analytics block; `tools/options.py`
  is at 98%).

## [0.3.1] - 2026-05-24

### Fixed

- **`serverInfo.version` now reports the project release tag** (e.g. `0.3.1`)
  instead of the underlying mcp framework version (`1.27.x`). The mcp
  Python SDK 1.27.x `FastMCP.__init__` does not accept a `version=` kwarg,
  so the lowlevel `Server.version` defaulted to `None` and the
  `initialize` response fell back to `importlib.metadata.version("mcp")`.
  Fix: directly set `mcp._mcp_server.version = SERVER_VERSION` after
  FastMCP construction. Adds `test_initialize_reports_release_tag_version`
  integration test asserting the fix.

### Compatibility

- Test count: 336 → 337 passing on Linux (89.08% coverage; 4 critical
  modules at 100%).

## [0.3.0] - 2026-05-23

### Added

- **Windows Tier A native support verification doc** (`docs/WINDOWS_VERIFICATION.md`):
  scaffold for testing the Tier A implementation on real Windows
  hardware. Lists 8 acceptance criteria, expected outputs, and
  failure-mode triage. Status: `pending verification on Windows hardware`.
- **Dependabot config** (`.github/dependabot.yml`): weekly Python +
  GitHub Actions dependency updates. Single-PR/week with minor + patch
  grouped. Ignores schwab-py (manual review per drift log).
- **schwab-py upgrade drift log** (`docs/THREAT_MODEL.md §6.6`):
  records TokenState fields, Movers.Index members, Client.token_age
  signature for each schwab-py version bump. Mandatory before
  merging upgrade PRs.
- **HOURS log** (`docs/HOURS.md`): cumulative dev hours per
  STRATEGY.md §2.3 budget tracking. Per-version + Phase 0 alarm
  (90h) + early-exit threshold (75% of 480h = 360h).
- **README badges + v0.3 sprint navigation** (English + Chinese
  mirrors).
- `docs/SLO.md`: 7 measurable service-level objectives (latency p95,
  cache hit rate, token health uptime, error rate, etc.) with
  measurement sources and window definitions.  Acts as the source of
  truth for the in-band alerting policy implemented by `health_check`.
- `Cache.hourly_breakdown(hours=24)`: per-hour `hits`/`misses`/`expired`
  counts derived from the existing `cache_events` table.  Exposed as a
  new `hourly_breakdown_24h` field on the `get_cache_stats` MCP tool so
  LLM agents and the SLO checker can plot an hourly time-series without
  parsing usage.jsonl.  Sparse output: hours with zero events are
  omitted; chronological order; ISO-8601 UTC `hour_utc` keys.
- `metrics.recent_errors_window(window_minutes=5, last_n=5)`: sliding-
  window error-rate computation backed by an O(1)-memory tail-read of
  `usage.jsonl` (caps at 2 MB regardless of file size).  Returns
  `{window_minutes, total_calls, error_count, error_rate, last_errors}`
  where `last_errors` deliberately contains **only** `ts` / `tool` /
  `error_class` — no exception messages, Bearer tokens, or PII.
- `health_check` MCP tool now includes:
  - `overall_status` field — `healthy` / `degraded` (error_rate > 5 %)
    / `unhealthy` (error_rate > 20 % or token missing/malformed/insecure)
  - `error_rate_5min`, `error_count_5min`, `total_calls_5min`,
    `last_errors` (5 most recent — metadata-only)
  - Documented thresholds in `DEGRADED_ERROR_RATE_THRESHOLD` /
    `UNHEALTHY_ERROR_RATE_THRESHOLD` constants.

### Changed

- `tests/conftest.py`: the autouse `_no_real_creds` fixture now also
  unsets `SCHWAB_RATE_LIMIT_PER_MIN` so a developer's `.env` (auto-
  loaded by `uv run`) can no longer pollute `health_check`'s
  rate-limit reading inside tests.

### Compatibility

- Test count: 314 → 336 passing on Linux (89.08% coverage).
- 4 critical modules (`errors`, `security`, `auth_logic`, `models`)
  remain at 100%.
- All 14 MCP tools unchanged in API surface; new fields are additive.

## [0.2.0] - 2026-05-23

### Added

- **DuckDB local cache layer** (plan v0.2 sprint task #2): a single-file
  DuckDB store under
  `${XDG_STATE_HOME:-~/.local/state}/schwab-marketdata-mcp/cache.duckdb`
  with four cache tables (quotes, price history, option chain,
  instruments) plus an internal `cache_events` log used by the new
  `get_cache_stats` meta tool. Tool implementations for
  `get_quote`, `get_price_history`, `get_option_chain`,
  `search_instruments`, and `get_instrument_by_cusip` short-circuit
  through the cache before reaching the rate limiter / Schwab API.
- TTLs follow a per-table strategy: 60 s for quotes, 5 m for option
  chains, 24 h for instrument metadata, and "historical candles
  immutable + recent candles refresh after 60 s" for price history
  (the recent boundary is 1 h).
- New env knobs: `SCHWAB_CACHE_ENABLED=true|false` (default true)
  and `SCHWAB_CACHE_BYPASS=true` (single-call force fresh).
- New MCP tool `get_cache_stats` (14th tool): returns
  `{db_path, enabled, size_mb, rows_per_table, expired_rows,
  hit_rate_24h, hits_24h, misses_24h}`. Offline-safe like the
  other meta tools.
- `health_check` now also returns `cache_enabled`, `cache_size_mb`,
  `cache_hit_rate_24h`.
- `usage.jsonl` now carries an optional `cache_status` field per row
  (`hit | miss | bypass | disabled`) so the 24 h hit-rate aggregate
  is auditable from the same JSONL stream as token health.
- `tests/test_cache.py` adds 28 unit + integration tests covering hit
  / miss / expire / replace / window query / OLAP / disabled /
  bypass / corrupt-recovery / concurrent-writers / POSIX mode 0o600
  / `_cache_status` payload field / health_check shape.

### Changed

- `pyproject.toml`: pin `duckdb>=1.0,<2.0` as a runtime dependency.
- Tool count surface in README / `get_server_info` / stdio integration
  test moved from 13 → 14.

### Security

- The cache DB file is created with mode `0o600` (parent dir
  `0o700`) on POSIX, falling back to inherited `%LOCALAPPDATA%`
  ACLs on Windows — same pattern as `token.json` / `usage.jsonl`.
  THREAT_MODEL.md §1 / §3 now lists the DB as asset A7.
- A corrupt DB file is renamed aside as
  `cache.duckdb.corrupt-<unix_ts>` and a fresh DB is created on
  next call, never silently overwriting potentially recoverable
  data.

## [0.1.1] - 2026-05-23

### Added

- `get_streaming_snapshot` tool (experimental): bounded WebSocket
  snapshot for `LEVELONE_EQUITIES` (real-time bid/ask/last/volume) or
  `CHART_EQUITY` (real-time 1-minute candles). Wraps schwab-py
  `StreamerClient` with a 500 ms - 10 s duration cap (default 2 s) and
  caps `symbols` at 20. Long-running subscriptions remain out of scope
  per plan §10; this addresses the v0.1.0 user request for "real-time"
  data without restructuring `server.py` to a daemon model. Tool list
  grew from 12 to 13.
- `tests/test_streaming.py` adds 12 unit tests with a mocked streamer
  (no real Schwab WebSocket): 5 input-validation boundaries, 5
  behavioural cases (happy-path L1, CHART_EQUITY candle, no-message,
  multi-frame aggregation, finally-block disconnect), and 2 server-
  layer integration cases.
- README "Data coverage clarifications" section explaining
  `get_price_history` is the kline endpoint, with the legal frequency-
  period combination matrix and historical lookback per type.
- README "What the Schwab Market Data API does NOT provide" table
  listing time-and-sales / tick / Level 2 historical / fundamentals
  history / news with recommended third-party providers.
- `CONTRIBUTING.md` with development setup, quality gates, commit
  message style, branching strategy, and inclusive language guidance.
- `.github/ISSUE_TEMPLATE/` with bug_report / feature_request /
  security_report templates.
- `.github/PULL_REQUEST_TEMPLATE.md` with checklist (tests,
  conventional commits, CHANGELOG, docs, inclusive language, no
  secrets).
- README badges (status / coverage / license / Python / release).
- `docs/RELEASE.md §9 Repository metadata` with `gh repo edit`
  commands for description and topics.
- `docs/REGISTER.md §6 Data coverage limits` cross-referencing the
  README clarifications section.

## [0.1.0] - 2026-05-23

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
- Bilingual READMEs (English primary + Chinese mirror via README_zh.md, language
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

[Unreleased]: https://github.com/kevinkda/schwab-marketdata-mcp/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/kevinkda/schwab-marketdata-mcp/releases/tag/v0.4.0
[0.3.1]: https://github.com/kevinkda/schwab-marketdata-mcp/releases/tag/v0.3.1
[0.3.0]: https://github.com/kevinkda/schwab-marketdata-mcp/releases/tag/v0.3.0
[0.2.0]: https://github.com/kevinkda/schwab-marketdata-mcp/releases/tag/v0.2.0
[0.1.1]: https://github.com/kevinkda/schwab-marketdata-mcp/releases/tag/v0.1.1
[0.1.0]: https://github.com/kevinkda/schwab-marketdata-mcp/releases/tag/v0.1.0
