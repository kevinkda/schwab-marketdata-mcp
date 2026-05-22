# schwab-marketdata-mcp

[![Status](https://img.shields.io/badge/status-alpha-orange.svg)](#)
[![Python](https://img.shields.io/badge/python-%3E%3D3.11-blue.svg)](#)
[![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey.svg)](#-platform-support)

Production-grade **Model Context Protocol (MCP)** server that exposes the
Charles Schwab **Market Data Production** API as 12 tools (10 endpoints + 2
meta tools) for use inside Cursor / Claude Code.

> **Important** — this project is **read-only market data**. It does NOT call
> the Schwab Trader API and does NOT place orders. See [§ Responsible Use](#-responsible-use)
> for the Schwab ToS impact.

---

## Platform support

|              | macOS 11+ | Linux | WSL2 (Linux subsystem) | Windows native |
| ------------ | :-------: | :---: | :--------------------: | :------------: |
| **v1 (now)** |     ✅    |   ✅  |    ✅ (no `/mnt/c`)    |       ❌       |

`fcntl.flock` is required for cross-process token locking; Windows native
support (via `msvcrt.locking`) is on the v2 roadmap.

---

## 5-minute quickstart

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

---

## Cursor / Claude registration

Add to your **user-level** `~/.cursor/mcp.json` (merge into existing
`mcpServers` object — do NOT overwrite):

```json
{
  "mcpServers": {
    "schwab-marketdata": {
      "command": "${HOME}/.local/bin/uv",
      "args": [
        "--directory",
        "${HOME}/code/kevinkda/schwab-marketdata-mcp",
        "run",
        "schwab-marketdata-mcp"
      ],
      "env": {
        "LOG_LEVEL": "WARNING",
        "SCHWAB_RATE_LIMIT_PER_MIN": "120"
      },
      "stderr": "${HOME}/.local/state/schwab-marketdata-mcp/logs/server.log"
    }
  }
}
```

> - Replace `${HOME}` with the absolute output of `echo $HOME` if Cursor does
>   not expand it.
> - Replace `${HOME}/.local/bin/uv` with the absolute path returned by
>   `which uv` (mise / pyenv users will have a different prefix).
> - **Do NOT** add `SCHWAB_TOKEN_PATH` to `env` — that variable is
>   intentionally **unsupported** to prevent path-injection attacks via
>   `mcp.json`. Use the `--config-dir` CLI flag instead.
> - The `"stderr"` field’s actual support depends on Cursor version; the
>   server itself always rotates `${XDG_STATE_HOME}/schwab-marketdata-mcp/logs/server.log`
>   (10 MB × 5) regardless.
> - **NEVER** register `schwab_marketdata_mcp.auth` as an MCP server — the
>   auth CLI uses stdout to talk to the browser and will corrupt the
>   JSON-RPC stream.

---

## The 12 tools

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

| Symptom                                                       | Fix                                                               |
| ------------------------------------------------------------- | ----------------------------------------------------------------- |
| Cursor reports `Invalid JSON` on the first byte               | You probably registered `auth` instead of the server. Re-register. |
| `SchwabAuthError(reason="refresh_token_expired")`             | Run `uv run python -m schwab_marketdata_mcp.auth login_flow`.      |
| `SchwabAuthError(reason="insecure_token_perms")`              | Follow the `chmod 600 …` hint printed in the error.                |
| `429 Rate limit exceeded` returned to the agent               | Auto-retried twice; if persistent, lower `SCHWAB_RATE_LIMIT_PER_MIN`. |

---

## 🔒 Responsible use

This server calls the Schwab Market Data Production API. **You** are
responsible for reading and complying with:

- <https://www.schwab.com/legal/terms> (Schwab Online Services Agreement)
- <https://developer.schwab.com/legal> (Developer Portal terms; requires login)

In particular:

- Schwab Market Data is **non-redistributable**. Any markdown / report this
  server writes (e.g., via the workflows skill) **must** stay in **private**
  repositories. The companion workflows skill enforces `gh repo view --json
  isPrivate` before writing.
- `schwab-py` is an **unofficial wrapper**; this project makes no warranty
  about its ToS standing. You must register your own app in the Schwab
  Developer Portal.

See [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md) and the companion skill’s
`references/tos-snapshot.md` for the cited ToS excerpts.

---

## Development

```bash
uv sync --extra dev
uv run pre-commit install
bash scripts/local-ci.sh   # equivalent to GitHub Actions
```

License: see [LICENSE](LICENSE).
