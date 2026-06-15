# schwab-marketdata-mcp

[English](./README.md) | [简体中文](./README_zh.md)

![Tests](https://img.shields.io/github/actions/workflow/status/kevinkda/schwab-marketdata-mcp/test.yml?branch=main&label=tests)
![Coverage](https://img.shields.io/badge/coverage-94.92%25-brightgreen)
![License](https://img.shields.io/github/license/kevinkda/schwab-marketdata-mcp)
![Python](https://img.shields.io/badge/python-3.11+-blue)
![Status](https://img.shields.io/badge/status-alpha-orange)
![Release](https://img.shields.io/github/v/release/kevinkda/schwab-marketdata-mcp)
![Releases](https://img.shields.io/github/release-date/kevinkda/schwab-marketdata-mcp?label=last%20release)
![CI](https://img.shields.io/github/actions/workflow/status/kevinkda/schwab-marketdata-mcp/test.yml?branch=main&label=CI&logo=github)

> **v0.3 sprint (in flight)** — Sprint A focuses on Windows Tier A real-hardware
> verification, dependency hygiene (Dependabot), schwab-py drift log, and a
> cumulative hour ledger.  See:
>
> - [`docs/WINDOWS_VERIFICATION.md`](docs/WINDOWS_VERIFICATION.md) — 8-criterion
>   acceptance checklist for a volunteer with a real Windows box.
> - [`docs/THREAT_MODEL.md` §6.6](docs/THREAT_MODEL.md) — schwab-py upgrade
>   drift log (must be filled in **before** any schwab-py bump merges).
> - [`docs/HOURS.md`](docs/HOURS.md) — cumulative hours vs the 480 h project
>   budget; current usage ≈ 5%.

Production-grade **Model Context Protocol (MCP)** server that exposes the
Charles Schwab **Market Data Production** API as **17 tools** (10 endpoints +
2 derived option-analytics + 3 meta tools + 1 cached-history analytics +
1 experimental streaming snapshot tool) for use inside Cursor,
Claude Code, and any other MCP-aware agent.

> **Read-only** — this project calls only the Schwab Market Data API. It does
> **NOT** call the Schwab Trader API and **does NOT** place orders. See
> [Responsible Use](#responsible-use) for the Schwab Terms of Service impact.

---

## Overview

`schwab-marketdata-mcp` is the server-side half of a two-repo system:

- **This repo** — the MCP server. Owns OAuth, rate limiting, retry/backoff,
  token rotation, structured error mapping, and stdio framing.
- **Companion repo** — [`schwab-marketdata-skill`](../schwab-marketdata-skill)
  ships two Cursor / Claude **Skills** that document how to call this server
  (single-tool `ops` skill and multi-step `workflows` skill).

The server wraps the unofficial [`schwab-py`](https://github.com/alexgolec/schwab-py)
SDK with the production hardening required for an always-on MCP host:

- Atomic refresh-token rotation with `fcntl.flock` (cross-process safe).
- Token-on-disk permission audit (`0600` enforced on every read).
- Per-process rate limiter (default 120 req/min, configurable per call).
- Adaptive 429 / 5xx retry with exponential backoff and `Retry-After` parsing.
- Structured error hierarchy (`SchwabAuthError`, `SchwabRateLimitError`, etc.)
  so agents can surface actionable messages instead of stack traces.
- stdio hardening so log lines never corrupt the JSON-RPC stream.

See [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md) for the full architecture
and threat model.

---

## Quick Start

> **Step 0 (mandatory)** — install pre-commit hooks **before** copying any
> secrets into the working tree:
>
> ```bash
> uv sync --extra dev
> uv run pre-commit install
> ```

```bash
# 1. Sync deps (uses the committed uv.lock)
uv sync --extra dev

# 2. Install pre-commit hooks (gitleaks + detect-secrets + ruff + mypy)
uv run pre-commit install

# 3. Configure your Schwab Developer Portal app credentials
cp .env.example .env
# then edit .env and replace `dummy-not-a-real-secret` placeholders with
# real values from https://developer.schwab.com/dashboard/apps

# 4. One-time OAuth login (browser opens; click through the self-signed cert)
uv run python -m schwab_marketdata_mcp.auth login_flow
# Or (containerized / headless): manual_flow
uv run python -m schwab_marketdata_mcp.auth manual_flow

# 5. Schedule the health probe (once per project, see docs/cron.example)
#    macOS: copy the launchd plist into ~/Library/LaunchAgents/
#    Linux: append the crontab snippet via `crontab -e`

# 6. Verify everything wires up
uv run python -m schwab_marketdata_mcp.health   # exit 0 if healthy
uv run pytest --cov                              # ≥85% overall, 100% on critical modules
```

For full client registration walkthroughs (Cursor / Claude Code / VS Code /
Claude Desktop), see [`docs/REGISTER.md`](docs/REGISTER.md).

---

## Features

### Authentication & token lifecycle

- **OAuth 2.0 authorization code flow** with two ergonomic CLIs:
  `login_flow` (auto-opens a browser, captures the redirect) and `manual_flow`
  (paste-the-URL, for headless / containerized environments).
- **Atomic refresh-token rotation** — every refresh writes to a tempfile and
  `os.replace`s the live token, guarded by an `fcntl.flock` so concurrent
  agents never race or corrupt the on-disk token.
- **Permission audit** — refuses to load a token file that is group- or
  world-readable; emits `SchwabAuthError(reason="insecure_token_perms")`
  with a copy-pasteable `chmod 600` hint.
- **7-day refresh window detection** — translates Schwab's opaque
  `invalid_grant` into `SchwabAuthError(reason="refresh_token_expired")` so
  agents can surface a "reconnect" UX instead of looping.

### Reliability

- **Per-process rate limiter** — token-bucket, default 120 req/min,
  configurable via `SCHWAB_RATE_LIMIT_PER_MIN`.
- **Adaptive retry** on `429` and `5xx` (default: 2 retries with exponential
  backoff and `Retry-After` parsing).
- **Pluggable cache** — a backend-agnostic response + derived-analysis cache
  short-circuits repeat reads of the 5 cacheable tools (`get_quote`,
  `get_price_history`, `get_option_chain`, `search_instruments`,
  `get_instrument_by_cusip`). Per-table TTLs (60 s quotes / 5 m option
  chains / 24 h instruments / 60 s recent price history) cut Schwab API
  pressure. **Disabled by default (opt-in)** — every call hits Schwab live.
  Enable with `SCHWAB_CACHE_ENABLED=true` (also accepts `1` / `yes` / `on`);
  force fresh reads via `SCHWAB_CACHE_BYPASS=1`. When disabled, cacheable
  tools report `_cache_status: "disabled"`; the response shape is unchanged.

  ⚠️ **BREAKING (v0.5.0):** the embedded DuckDB cache is removed in favour of
  a pluggable backend selected via `SCHWAB_CACHE_BACKEND`:

  | Backend | Default | Dependency | Notes |
  | --- | --- | --- | --- |
  | `memory` | ✅ | none (stdlib) | In-process LRU + TTL, concurrency-safe, non-blocking, no files. Derived-analysis history (`option_chain_snapshots` / `iv_history` / candle OLAP) keeps **no durable store** — those degrade gracefully (`get_iv_percentile` returns a `cache_disabled`/empty payload, snapshot writes report `_cached_rows: 0`). |
  | `clickhouse` | — | `pip install schwab-marketdata-mcp[clickhouse]` + `SCHWAB_CLICKHOUSE_URL` | Durably persists the derived-analysis time series and serves the real ATM-IV / IV-percentile / candle-OLAP analytics. |

  All 17 tools keep working out of the box on the default memory backend; only
  the IV-history-backed analytics require the ClickHouse extra to retain
  cross-session history.
- **Health probe** (`schwab_marketdata_mcp.health`) returns distinct exit
  codes for token age, missing/malformed token, and insecure permissions —
  ready for cron / launchd alerting.
- **Rotating file logs** under `${XDG_STATE_HOME}/schwab-marketdata-mcp/logs/`
  (10 MB × 5), regardless of whether the MCP host honors a `stderr` field.

### Security

- **stdio hardening** — `bootstrap_dotenv()` runs before any `print()` could
  fire, so `.env` loading never corrupts the JSON-RPC stream.
- **Path-injection prevention** — `SCHWAB_TOKEN_PATH` env var is intentionally
  **unsupported**; use the `--config-dir` CLI flag instead.
- **Pre-commit hardening** — `gitleaks`, `detect-secrets`, `ruff`, `mypy`,
  and `markdownlint` run on every commit; `.secrets.baseline` is committed.
- **Non-redistributable data guardrail** — the companion workflows skill
  refuses to write Schwab data into a public repo (it calls
  `gh repo view --json isPrivate` first).

### Tooling surface — 17 MCP tools

At-a-glance map of name → endpoint:

| #  | Tool                          | Endpoint                                   |
| -- | ----------------------------- | ------------------------------------------ |
| 1  | `get_quote`                   | `GET /{symbol_id}/quotes`                  |
| 2  | `get_quotes`                  | `GET /quotes`                              |
| 3  | `get_price_history`           | `GET /pricehistory`                        |
| 4  | `get_option_chain`            | `GET /chains`                              |
| 5  | `get_option_expiration_chain` | `GET /expirationchain`                     |
| 6  | `get_option_greeks_summary`   | derived — net Greeks from `GET /chains` (no ClickHouse needed) |
| 7  | `get_market_hours`            | `GET /markets`                             |
| 8  | `get_market_hour_single`      | `GET /markets/{market_id}`                 |
| 9  | `get_movers`                  | `GET /movers/{symbol_id}`                  |
| 10 | `search_instruments`          | `GET /instruments`                         |
| 11 | `get_instrument_by_cusip`     | `GET /instruments/{cusip_id}`              |
| 12 | `health_check`                | local — token age + cache health           |
| 13 | `get_server_info`             | local — versions + supported tool list     |
| 14 | `get_cache_stats`             | local — cache backend (memory/clickhouse) + live entry count |
| 15 | `get_iv_percentile`           | local — ATM IV percentile rank from cached `iv_history` (refresh=True pulls fresh chain) |
| 16 | `get_iv_surface`              | local — ATM IV surface across 30d/60d/90d buckets from cached `iv_history` |
| 17 | `get_streaming_snapshot` 🧪    | Streamer WebSocket — bounded snapshot      |

Detailed per-tool reference is below.

#### Business tools (10) — Schwab Market Data Production endpoints

##### `get_quote` — single-symbol real-time quote

- **When**: fetch the latest bid / ask / last for one symbol; quick P&L
  read or one-off decision; not a batch.
- **Input**: `symbol` (str — stock, ETF, index `$XYZ`, or OSI option),
  `fields?` (list of field groups, e.g. `["quote", "fundamental"]`).
- **Returns**:
  `{<symbol>: {quote: {bidPrice, askPrice, lastPrice, totalVolume, ...},
  fundamental: {...}, reference: {...}}}`
- **Example**: `{"symbol": "VOO"}`

##### `get_quotes` — batch quote for ≤ 50 symbols

- **When**: watchlist refresh, portfolio-wide scan, ETF / stock
  comparison; one HTTP round-trip instead of N.
- **Input**: `symbols` (list[str], len ≤ 50), `fields?`,
  `indicative?` (bool — include indicative quotes for indices).
- **Returns**: `{<sym1>: {quote: {...}, fundamental: {...}}, <sym2>: {...}, ...}`
- **Example**: `{"symbols": ["VOO", "QQQ", "SPY"]}`

##### `get_price_history` — OHLC candles (minute → month)

- **When**: technical analysis, backtests, charting, computing SMA / RSI
  / ATR or any windowed indicator.
- **Input**: `symbol`, `period_type` (`DAY` / `MONTH` / `YEAR` / `YTD`),
  `period`, `frequency_type` (`MINUTE` / `DAILY` / `WEEKLY` / `MONTHLY`),
  `frequency`. Pydantic pre-validates the legal cartesian product.
- **Returns**: `{candles: [{open, high, low, close, volume, datetime}, ...],
  symbol, empty}`
- **Example**:
  `{"symbol": "VOO", "period_type": "DAY", "period": "FIVE_DAYS",
  "frequency_type": "MINUTE", "frequency": "EVERY_FIVE_MINUTES"}`

##### `get_option_chain` — option chain snapshot with Greeks

- **When**: option research, IV-rank analysis, covered-call /
  cash-secured-put strike selection, vertical-spread modeling.
- **Input**: `symbol`, plus a dozen optional filters —
  `contract_type?` (`CALL` / `PUT` / `ALL`), `strike_count?`,
  `from_date?`, `to_date?`, `strategy?`, `range?`, `volatility?`,
  `interest_rate?`, `days_to_expiration?` …
- **Returns**:
  `{status, callExpDateMap: {...}, putExpDateMap: {...},
  underlying: {...}, numberOfContracts}`
- **Example**: `{"symbol": "VOO", "strike_count": 3, "contract_type": "ALL"}`

##### `get_option_expiration_chain` — list of available expirations

- **When**: pre-flight before `get_option_chain`; check whether weekly /
  monthly / LEAPS expiries exist for an underlying.
- **Input**: `symbol`.
- **Returns**:
  `{expirationList: [{expirationDate, daysToExpiration, expirationType,
  settlementType}, ...]}`
- **Example**: `{"symbol": "VOO"}`

##### `get_option_greeks_summary` — net Greeks aggregation (no ClickHouse needed)

- **When**: read net dealer/book positioning at a glance — aggregate
  `delta` / `gamma` / `theta` / `vega` / `rho` across the live chain
  instead of eyeballing hundreds of contracts; split by call/put and by
  expiry; isolate a single expiration.
- **Works without ClickHouse**: the Greeks are computed live from the
  freshly-fetched chain (the data is already in `get_option_chain`), so
  this tool is useful on the default memory backend.
- **Input**: `underlying`, `expiry?` (`YYYY-MM-DD` — restrict to one
  expiration), `weighting?` (`open_interest` default / `equal`).
  `open_interest` weights each Greek by open interest and falls back to
  equal weighting (with a warning) when no contract reports OI.
- **Returns**:
  `{underlying, expiry_filter, weighting, requested_weighting,
  contract_count, net: {delta, gamma, theta, vega, rho},
  by_side: {CALL: {...}, PUT: {...}}, by_expiry: {<date>: {...}},
  warning}`
- **Example**: `{"underlying": "AAPL", "weighting": "open_interest"}`

##### `get_market_hours` — multi-market session hours

- **When**: pre-market / after-hours dashboards; decide whether a market
  is open before kicking off a scan; cross-market view.
- **Input**: `markets` (list[str] — any of `EQUITY` / `OPTION` / `BOND` /
  `FUTURE` / `FOREX`), `date?` (`YYYY-MM-DD`, defaults to today).
- **Returns**:
  `{<market>: {<product>: {date, marketType, isOpen, sessionHours: {...}}}}`
- **Example**: `{"markets": ["EQUITY", "OPTION"]}`

##### `get_market_hour_single` — single-market session hours

- **When**: same intent as `get_market_hours` but only one market —
  smaller payload, path-param flavor.
- **Input**: `market_id` (`EQUITY` / `OPTION` / `BOND` / `FUTURE` /
  `FOREX`), `date?`.
- **Returns**: same shape as `get_market_hours`, scoped to a single market.
- **Example**: `{"market_id": "EQUITY"}`

##### `get_movers` — top movers for an index

- **When**: intraday / end-of-day market-tone read; spot unusual movers
  in `$SPX` / `$DJI` / `$COMPX` / NYSE / NASDAQ.
- **Input**: `index` (`$SPX` / `$DJI` / `$COMPX` / `NYSE` / `NASDAQ` /
  `OTCBB` / `INDEX_ALL` / `EQUITY_ALL` / `OPTION_ALL` / `OPTION_PUT` /
  `OPTION_CALL`), `sort?`, `frequency?`.
- **Returns**:
  `{screeners: [{symbol, description, lastPrice, netChange,
  netPercentChange, volume, ...}, ...]}`
- **Example**: `{"index": "$SPX"}`

##### `search_instruments` — instrument search / fundamentals

- **When**: resolve user-typed strings (`"AAPL"`, `"Apple"`) to canonical
  instruments; bulk-validate tickers; pull `fundamental` block (PE,
  market cap, dividend yield, …).
- **Input**: `symbols` (list[str]), `projection`
  (`SYMBOL_SEARCH` / `SYMBOL_REGEX` / `DESC_SEARCH` / `DESC_REGEX` /
  `SEARCH` / `FUNDAMENTAL`).
- **Returns**:
  `{instruments: [{symbol, description, exchange, assetType,
  fundamental?: {...}}, ...]}`
- **Example**: `{"symbols": ["VOO"], "projection": "FUNDAMENTAL"}`

##### `get_instrument_by_cusip` — reverse lookup by CUSIP

- **When**: a brokerage statement / SEC filing gave you a 9-character
  CUSIP and you need the matching ticker / description.
- **Input**: `cusip` (str, 9 chars, must match `^[A-Z0-9]{9}$`).
- **Returns**: a single instrument dict
  `{symbol, description, exchange, assetType, ...}`.
- **Example**: `{"cusip": "922908363"}` (VOO)

#### Meta tools (2) — local, offline-safe, no Schwab API call

##### `health_check` — token + rate-limit + recent-error self-check

- **When**: first call after MCP startup; before deciding "do we need to
  re-auth"; periodic cron / launchd probe (every 4 h is plenty).
- **Input**: none.
- **Returns**:
  `{server_version, token_state (VALID | MISSING | INSECURE_PERMS |
  MALFORMED), token_age_days, token_expires_in_days,
  last_request_status, rate_limit_remaining_per_min,
  recent_error_count_24h, platform_supported}`
- **Example**: `{}`

##### `get_server_info` — version handshake + capability discovery

- **When**: skill activation (validates `compatible_mcp_version`); first
  time registering on a new client; debugging / bug reports.
- **Input**: none.
- **Returns**:
  `{server_version, mcp_sdk_version, schwab_py_version,
  supported_tools: [...17 tool names], platform_supported_v1}`
- **Example**: `{}`

#### Experimental — bounded streaming snapshot (1)

##### `get_iv_surface` — ATM IV term-structure surface (cached history)

- **When**: read the whole IV term structure (30d / 60d / 90d) at once
  — front-vs-back skew, term-structure inversion, or IV-rank per bucket
  — instead of calling `get_iv_percentile` three times.
- **Input**: `underlying`, `lookback_days?` (int, default 252, 30–730).
- **Returns**:
  `{underlying, lookback_days, total_sample_count,
  buckets: {30d|60d|90d: {current_iv, percentile_rank, sample_count,
  min_iv, max_iv, median_iv, current_asof}}, warning}`
- **Persistence**: built from durable `iv_history` rows that only the
  **ClickHouse** backend retains. On the memory backend / disabled cache
  the buckets come back empty with
  `warning: ["requires_clickhouse_persistence"]` — the read path never
  raises.
- **Example**: `{"underlying": "AAPL", "lookback_days": 252}`

##### `get_streaming_snapshot` 🧪 — bounded WebSocket snapshot

- **When**: you need real-time bid/ask/last (sub-second freshness) or a
  live 1-minute candle and the REST `get_quote` / `get_price_history`
  freshness (delayed ~5–15 s) is not good enough. Tool opens a Schwab
  Streamer WebSocket, collects messages for the requested duration,
  then disconnects — fits the request-response semantics of MCP rather
  than holding a long-running subscription.
- **Input**: `symbols` (list[str], ≤ 20), `service`
  (`LEVELONE_EQUITIES` for real-time bid/ask/last/volume **or**
  `CHART_EQUITY` for real-time 1-minute candles),
  `duration_ms?` (int, default 2000, hard-bounded 500 – 10000).
- **Returns**:
  `{service, symbols_requested, symbols_received, duration_ms,
  messages_count, snapshots: {sym: [{ts, bid|open, ask|high, ...}, ...]},
  metadata: {first_message_at, last_message_at, connection_duration_ms}}`
- **Example**:
  `{"symbols": ["VOO", "QQQ"], "service": "LEVELONE_EQUITIES",
  "duration_ms": 2000}`
- **Caveats**: Each call opens, authenticates, subscribes, and closes a
  WebSocket — ~300–500 ms of overhead per call. Use sparingly. For
  long-running subscriptions, a dedicated streaming MCP server is on
  the v0.3+ roadmap (see plan §10).

> See [`schwab-marketdata-skill`](https://github.com/kevinkda/schwab-marketdata-skill)
> for full input / output schemas, edge cases, retry semantics, and
> end-to-end usage playbooks.

### Data coverage clarifications

#### `get_price_history` is the candlestick / kline endpoint

If you're looking for OHLCV bars (candles, klines, candlesticks),
`get_price_history` is the tool. It returns:

```json
{
  "candles": [
    {"open": 432.10, "high": 432.55, "low": 431.92, "close": 432.40, "volume": 12034, "datetime": 1716470400000},
    {"open": 432.40, "high": 432.62, "low": 432.18, "close": 432.50, "volume":  9821, "datetime": 1716470700000}
  ],
  "symbol": "VOO",
  "empty": false
}
```

Supported granularity matrix (Schwab Market Data API limits):

| period_type | Legal `frequency_type` × `frequency` | Historical lookback |
|-------------|--------------------------------------|---------------------|
| `DAY`       | `MINUTE` × {1, 5, 10, 15, 30}        | ~48 days (1-min), ~9 months (5-30 min) |
| `MONTH`     | `DAILY` / `WEEKLY`                   | up to 6 months      |
| `YEAR`      | `DAILY` / `WEEKLY` / `MONTHLY`       | **up to 20 years**  |
| `YEAR_TO_DATE` | `DAILY` / `WEEKLY`                | year-to-date        |

> Sub-minute candles (seconds, ticks) are not provided by the Schwab Market
> Data API. For real-time minute candles, use the experimental
> `get_streaming_snapshot` tool with `service="CHART_EQUITY"`.

#### What the Schwab Market Data API does NOT provide

The following data is **architecturally unavailable** through the Schwab
Market Data Production API and would require third-party providers:

| Data type | Status | Recommended third-party |
|-----------|--------|-------------------------|
| Time & sales / tape (trade-level) | Not in REST or Streaming since Schwab's 2024 API migration removed `TIMESALE_*` services | [Polygon.io `/v3/trades/{ticker}`](https://polygon.io/docs/stocks/get_v3_trades__ticker) (full SIP, ~$199/mo); [Tiingo IEX](https://api.tiingo.com/documentation/iex) ($10/mo, IEX-only); [Alpaca tick API](https://docs.alpaca.markets/reference/stocktrades) (free tier, paid $99/mo for SIP) |
| Tick-by-tick history | Not in Schwab API | Same as above |
| Level 2 historical snapshots (NYSE Book / NASDAQ Book replay) | Streaming only, no REST history | Polygon.io / Databento |
| Fundamental / earnings time series (EPS history, revenue history, etc.) | `quotes` carry FUNDAMENTAL fields but no historical endpoint | [FMP](https://financialmodelingprep.com/), [Tiingo](https://api.tiingo.com/), [Polygon.io fundamentals](https://polygon.io/docs/stocks/financials) |
| News / SEC filings | Not in Market Data API | [Polygon.io news](https://polygon.io/docs/stocks/get_v2_reference_news), [SEC EDGAR](https://www.sec.gov/edgar) |

**Trader API endpoints** (account, orders, transactions, positions) are
explicitly out of scope per [`docs/REGISTER.md`](docs/REGISTER.md) — this
server is **read-only Market Data only**. See plan §1 / §10 for the
rationale.

---

## Requirements

| Requirement | Version | Notes |
| ----------- | ------- | ----- |
| Python      | `>=3.11` | Type hints rely on PEP 695 syntax. |
| `uv`        | `>=0.4` | Used for env management and lockfile-pinned installs. |
| OS          | macOS 11+ / Linux / WSL2 / Windows 10/11 native (Tier A) | `fcntl.flock` on POSIX, `msvcrt.locking` on Windows. See [`docs/WINDOWS_PORTING.md`](docs/WINDOWS_PORTING.md). |
| CPU arch    | `x86_64` / `arm64` | Both Intel Macs (Mac Pro 2019, MacBook Pro 2019) and Apple Silicon (M1/M2/M3/M4) are supported with native wheels — no Rosetta 2 needed. Linux `aarch64` (Graviton, Raspberry Pi 5) also works. |
| Schwab Developer account | — | You must register your own app at <https://developer.schwab.com/dashboard/apps>. |

### Platform support

|              | macOS 11+ (Intel x86_64) | macOS 11+ (Apple Silicon arm64) | Linux x86_64 | Linux arm64 | WSL2 (Linux subsystem) | Windows 10/11 native |
| ------------ | :----------------------: | :-----------------------------: | :----------: | :---------: | :--------------------: | :------------------: |
| **v1 (now)** |            ✅            |                ✅               |      ✅      |      ✅     |    ✅ (no `/mnt/c`)    |   🧪 experimental (Tier A) |

Windows native is **Tier A best-effort** as of this release - token locking
uses `msvcrt.locking` instead of `fcntl.flock`, and file permissions rely on
the default NTFS ACL inherited from `%LOCALAPPDATA%` rather than POSIX
`0o600`.  Install the optional Windows extras for richer toast notifications:

```powershell
pip install schwab-marketdata-mcp[windows]
# or with uv:
uv sync --extra dev --extra windows
```

See [`docs/WINDOWS_PORTING.md`](docs/WINDOWS_PORTING.md) for the full caveat
list.  Tier B (production-grade Windows ACL via `pywin32` + a `windows-latest`
CI runner) is on the roadmap.

All native-extension dependencies (`cryptography`, `pydantic-core`,
`websockets`, `cffi`, `pyyaml`, `markupsafe`, `msgpack`, `rpds-py`) ship
both `macosx_*_x86_64` (or `universal2`) and `macosx_*_arm64` wheels on
PyPI, so `uv sync --extra dev` resolves to native binaries on either
architecture without invoking the host C/Rust toolchain. The remaining
runtime deps (`mcp`, `schwab-py`, `httpx`, `httpcore`, `h11`, `anyio`,
`pydantic`, `httpx-sse`, `joserfc`) are pure-Python (`py3-none-any`) and
therefore architecture-agnostic.

The server itself uses only architecture-independent abstractions
(`pathlib.Path`, `os.replace`) plus a thin `_platform` shim that picks the
right primitive at runtime: `fcntl.flock` / `os.chmod` / `os.umask` on POSIX
and `msvcrt.locking` / NTFS ACL inheritance on Windows.  Behavior on Intel
and Apple Silicon Macs is byte-for-byte identical.

Windows native support is Tier A (experimental) — file locking uses
`msvcrt.locking` and POSIX `0o600` permission bits are best-effort no-ops
backed by the user's `%LOCALAPPDATA%` NTFS ACL.  Tier B (production-grade
ACL via `pywin32` + Windows CI matrix) is on the roadmap.  WSL2 still works
today provided you keep the checkout on the Linux filesystem (avoid
`/mnt/c`, where `flock` behaves unreliably).

> **CI coverage note** — the `test.yml` matrix currently runs
> `macos-latest` (= `macos-14`, arm64) and `ubuntu-latest` (x86_64). Intel
> macOS x86_64 (`macos-13`) is not yet in the matrix; if you depend on
> Intel-Mac compatibility, run `uv sync --extra dev && uv run pytest --cov`
> locally on a Mac Pro / Intel MacBook to verify before deployment, or
> open a PR adding `macos-13` to the runner matrix.

---

## Architecture

```text
┌─────────────────────┐      stdio (JSON-RPC)      ┌─────────────────────┐
│  Cursor / Claude    │ ─────────────────────────▶ │  schwab-marketdata- │
│  / Claude Code      │ ◀───────────────────────── │  mcp (this repo)    │
└─────────────────────┘                            └──────────┬──────────┘
                                                              │
                                              HTTPS + OAuth   │
                                                              ▼
                                                  ┌─────────────────────┐
                                                  │ Schwab Market Data  │
                                                  │ Production API      │
                                                  └─────────────────────┘
```

For the full data-flow diagram, threat model, and trust boundaries, see
[`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md).

---

## Integration

To register the server with your MCP-aware agent, follow the relevant
section in [`docs/REGISTER.md`](docs/REGISTER.md):

- Cursor — `~/.cursor/mcp.json`
- Claude Code — `~/.claude/mcp.json`
- VS Code (Continue / Cline) — workspace settings
- Claude Desktop — `~/Library/Application Support/Claude/claude_desktop_config.json`

> **Important caveats** (covered in detail in `docs/REGISTER.md`):
>
> - Replace `${HOME}` with the absolute output of `echo $HOME` if your host
>   does not expand it.
> - Replace `${HOME}/.local/bin/uv` with the absolute path returned by
>   `which uv` (mise / pyenv users will have a different prefix).
> - **Do NOT** add `SCHWAB_TOKEN_PATH` to `env` — that variable is
>   intentionally **unsupported** to prevent path-injection attacks via
>   `mcp.json`. Use the `--config-dir` CLI flag instead.
> - **NEVER** register `schwab_marketdata_mcp.auth` as an MCP server — the
>   auth CLI uses stdout to talk to the browser and will corrupt the
>   JSON-RPC stream.

---

## Development

```bash
# Sync deps and install hooks
cd /path/to/schwab-marketdata-mcp
uv sync --extra dev
uv run pre-commit install

# Run the full local CI loop (mirrors GitHub Actions)
bash scripts/local-ci.sh

# Or run individual stages
uv run ruff check .
uv run mypy src
uv run pytest --cov
uv run pre-commit run --all-files
```

The `scripts/local-ci.sh` script runs lint, type-check, full test suite with
coverage, pre-commit hooks, and `markdownlint-cli2` — and exits non-zero on
any failure. CI runs the same script on every push.

---

## Testing

| Metric | Target | Current |
| ------ | ------ | ------- |
| Test count | ≥ 200 | **250** collected |
| Overall coverage | ≥ 85% | enforced by `--cov-fail-under=85` |
| Critical-module coverage | 100% | `auth.py`, `rate_limiter.py`, `health.py` |
| OWASP coverage | 2017 + 2021 + 2025 | matrix-tracked in `tests/security/` |

Test categories:

- **Unit tests** — `tests/unit/` (per-module, mocked Schwab API).
- **Integration tests** — `tests/integration/` (live `schwab-py`, recorded
  cassettes via `vcrpy`).
- **Security tests** — `tests/security/` (OWASP Top 10 matrix:
  injection / broken-auth / sensitive-data / SSRF / etc.).
- **Boundary tests** — empty, max-size, malformed, and timezone-edge inputs.
- **Exception tests** — every `SchwabAuthError` / `SchwabRateLimitError` /
  `SchwabRetryableError` reason code is exercised.

Run a focused subset:

```bash
uv run pytest tests/security -v
uv run pytest -k "rate_limit" --cov
uv run pytest --cov --cov-report=html  # outputs to htmlcov/
```

---

## Health probe (cron / launchd / Task Scheduler)

```bash
uv run python -m schwab_marketdata_mcp.health
# exit codes: 0=healthy, 1=<24h, 2=<12h or expired,
#             3=token MISSING, 4=token MALFORMED, 5=INSECURE_PERMS
```

See [`docs/cron.example`](docs/cron.example) for ready-to-paste **launchd
plist** (Sunday 20:00 + Wednesday 21:00 + every 4h fallback for laptop lid
close) and **crontab** snippets.  Windows users: the same file ships a
`Register-ScheduledTask` PowerShell snippet under
"Windows native (Task Scheduler)".

After installing, run `bash scripts/notifier-self-test.sh` (POSIX) or
`pwsh scripts/notifier-self-test.ps1` (Windows) once to confirm
`osascript` / `notify-send` / Windows toast actually fires.

---

## Troubleshooting

| Symptom                                                       | Fix                                                                |
| ------------------------------------------------------------- | ------------------------------------------------------------------ |
| Cursor reports `Invalid JSON` on the first byte               | You probably registered `auth` instead of the server. Re-register. |
| `SchwabAuthError(reason="refresh_token_expired")`             | Run `uv run python -m schwab_marketdata_mcp.auth login_flow`.      |
| `SchwabAuthError(reason="insecure_token_perms")`              | Follow the `chmod 600 …` hint printed in the error.                |
| `429 Rate limit exceeded` returned to the agent               | Auto-retried twice; if persistent, lower `SCHWAB_RATE_LIMIT_PER_MIN`. |

---

## Responsible use

This server calls the Schwab Market Data Production API. **You** are
responsible for reading and complying with:

- <https://www.schwab.com/legal/terms> — Schwab Online Services Agreement
- <https://developer.schwab.com/legal> — Developer Portal terms (login required)

In particular:

- Schwab Market Data is **non-redistributable**. Any markdown / report this
  server writes (e.g., via the workflows skill) **must** stay in **private**
  repositories. The companion workflows skill enforces
  `gh repo view --json isPrivate` before writing.
- `schwab-py` is an **unofficial wrapper**; this project makes no warranty
  about its Terms of Service standing. You must register your own app in the
  Schwab Developer Portal.

See [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md) for the full threat model
and the companion skill's `references/tos-snapshot.md` for the cited ToS
excerpts.

---

## See also

- [`schwab-marketdata-skill`](../schwab-marketdata-skill) — companion Cursor /
  Claude Skills repo. Ships `ops` (single tool calls) and `workflows`
  (multi-step playbooks) skills with English mirrors.
- [`docs/REGISTER.md`](docs/REGISTER.md) — full client registration
  walkthrough.
- [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md) — architecture and threat
  model.
- [`docs/RELEASE.md`](docs/RELEASE.md) — release process for maintainers.
- [`docs/WINDOWS_PORTING.md`](docs/WINDOWS_PORTING.md) — Windows-native porting
  effort estimate.
- [`docs/cron.example`](docs/cron.example) — launchd / crontab templates for
  the health probe.

---

## Acknowledgements

This project is built on top of several excellent open-source libraries:

- **[schwab-py](https://github.com/alexgolec/schwab-py)** by Alex Golec —
  Unofficial Charles Schwab Trader / Market Data API wrapper for Python
  (MIT License). Handles 3-legged OAuth, token refresh rotation, and
  provides typed enums for all Schwab Market Data Production endpoints.
  Without `schwab-py`, this MCP server would be ~1.5 days more work and
  significantly more brittle.
- **[mcp](https://github.com/modelcontextprotocol/python-sdk)** —
  Anthropic's official Python SDK for the Model Context Protocol
  (MIT License).
- **[httpx](https://github.com/encode/httpx)** — Next-generation HTTP
  client (BSD-3-Clause).
- **[pydantic](https://github.com/pydantic/pydantic)** — Data validation
  using Python type hints (MIT License).

This project is **not affiliated with, endorsed by, or sponsored by
Charles Schwab Corporation or Alex Golec**. Use at your own risk per
Schwab's [Terms of Service](https://www.schwab.com/legal/terms).

---

## License

MIT License — see [LICENSE](./LICENSE).
