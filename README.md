# schwab-marketdata-mcp

[English](./README.md) | [简体中文](./README_zh.md)

[![Status](https://img.shields.io/badge/status-alpha-orange.svg)](./README.md)
[![Python](https://img.shields.io/badge/python-%3E%3D3.11-blue.svg)](#requirements)
[![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey.svg)](#requirements)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](./LICENSE)

Production-grade **Model Context Protocol (MCP)** server that exposes the
Charles Schwab **Market Data Production** API as **12 tools** (10 endpoints + 2
meta tools) for use inside Cursor, Claude Code, and any other MCP-aware agent.

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

### Tooling surface — 12 MCP tools

| #  | Tool                          | Endpoint                                   |
| -- | ----------------------------- | ------------------------------------------ |
| 1  | `get_quote`                   | `GET /{symbol_id}/quotes`                  |
| 2  | `get_quotes`                  | `GET /quotes`                              |
| 3  | `get_price_history`           | `GET /pricehistory`                        |
| 4  | `get_option_chain`            | `GET /chains`                              |
| 5  | `get_option_expiration_chain` | `GET /expirationchain`                     |
| 6  | `get_market_hours`            | `GET /markets`                             |
| 7  | `get_market_hour_single`      | `GET /markets/{market_id}`                 |
| 8  | `get_movers`                  | `GET /movers/{symbol_id}`                  |
| 9  | `search_instruments`          | `GET /instruments`                         |
| 10 | `get_instrument_by_cusip`     | `GET /instruments/{cusip_id}`              |
| 11 | `health_check`                | local — token age + recent error count     |
| 12 | `get_server_info`             | local — versions + supported tool list     |

---

## Requirements

| Requirement | Version | Notes |
| ----------- | ------- | ----- |
| Python      | `>=3.11` | Type hints rely on PEP 695 syntax. |
| `uv`        | `>=0.4` | Used for env management and lockfile-pinned installs. |
| OS          | macOS 11+ / Linux / WSL2 | `fcntl.flock` is required for cross-process token locking. |
| Schwab Developer account | — | You must register your own app at <https://developer.schwab.com/dashboard/apps>. |

### Platform support

|              | macOS 11+ | Linux | WSL2 (Linux subsystem) | Windows native |
| ------------ | :-------: | :---: | :--------------------: | :------------: |
| **v1 (now)** |     ✅    |   ✅  |    ✅ (no `/mnt/c`)    |       ❌       |
| **v2 (TBD)** |     ✅    |   ✅  |          ✅            |   ⏳ (planned: `msvcrt.locking`) |

Windows native support requires replacing `fcntl.flock` with
`msvcrt.locking`; this is on the v2 roadmap. WSL2 works today provided you
keep the checkout on the Linux filesystem (avoid `/mnt/c`, where `flock`
behaves unreliably).

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

## Health probe (cron / launchd)

```bash
uv run python -m schwab_marketdata_mcp.health
# exit codes: 0=healthy, 1=<24h, 2=<12h or expired,
#             3=token MISSING, 4=token MALFORMED, 5=INSECURE_PERMS
```

See [`docs/cron.example`](docs/cron.example) for ready-to-paste **launchd
plist** (Sunday 20:00 + Wednesday 21:00 + every 4h fallback for laptop lid
close) and **crontab** snippets.

After installing, run `bash scripts/notifier-self-test.sh` once to confirm
`osascript` (macOS) or `notify-send` (Linux) actually fires.

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
- [`docs/cron.example`](docs/cron.example) — launchd / crontab templates for
  the health probe.

---

## License

MIT License — see [LICENSE](./LICENSE).
