# Registering with Cursor / Claude

> **Do not edit your client config blindly** — the snippets below are the
> exact paste-points for `~/.cursor/mcp.json` and the user's Claude Code
> skill folder.  Merge them into the existing structure rather than
> overwriting.

---

## 1.  MCP server — `~/.cursor/mcp.json`

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
      "envFile": "${HOME}/code/kevinkda/schwab-marketdata-mcp/.env",
      "env": {
        "LOG_LEVEL": "WARNING",
        "SCHWAB_RATE_LIMIT_PER_MIN": "120"
      },
      "stderr": "${HOME}/.local/state/schwab-marketdata-mcp/logs/server.log"
    }
  }
}
```

### Why `envFile` is recommended (no longer strictly required)

As of the bug-fix that introduced :mod:`schwab_marketdata_mcp.bootstrap`,
the server now calls `bootstrap_dotenv()` immediately after stdio
hardening, so `os.environ["SCHWAB_APP_KEY"]` is populated from a `.env`
file in the **process working directory** even when the host did not
inject it.  This means:

- Hosts that **do** support an `envFile` directive (Cursor, VS Code) get
  the cleanest setup — credentials live in the file pointed to by
  `envFile`, the host keeps `~/.cursor/mcp.json` itself credential-free,
  and the server-side dotenv loader sees host-provided values first
  because `bootstrap_dotenv` calls `load_dotenv(override=False)`.
- Hosts that **do not** support `envFile` (Claude Desktop today, plain
  `uv run schwab-marketdata-mcp` from a shell, ad-hoc scripts) still
  work as long as the server's cwd is the package checkout that
  contains `.env`.  The `--directory <repo>` flag in the `args` array
  ensures that whenever `uv run` is the launch command.

In short: **`envFile` is the recommended path** because it keeps
credentials in a host-managed location and makes the dependency on
cwd implicit; **the cwd-`.env` fallback** is a guaranteed safety net so
that a missing/unsupported `envFile` does not produce a hard
`SchwabAuthError(reason="missing_credentials")` at the first tool call.

Precedence (highest wins) when more than one source is present:

1. Host-injected env (Cursor `env` map, shell `export`, Claude Desktop
   wrapper script).
2. `envFile` contents loaded by the host before launch.
3. `.env` discovered by `bootstrap_dotenv()` in the server's cwd or
   any parent directory (`override=False`, so this never clobbers
   1 or 2).

### Host compatibility for `envFile`

| Host                | Field name | Notes |
| ------------------- | ---------- | ----- |
| **Cursor**          | `envFile`  | STDIO servers only.  Absolute path or `${workspaceFolder}/.env`.  Recommended. |
| **VS Code**         | `envFile`  | Same semantics as Cursor.  Recommended. |
| **Claude Desktop**  | _not supported_ | Use the cwd-`.env` fallback (preferred) or the wrapper-script fallback below. |
| **Cline / Continue** | _varies by version_ | Use `envFile` if available; otherwise the cwd-`.env` fallback handles it transparently. |

#### Fallback A — cwd-`.env` (preferred for hosts without `envFile`)

The server now self-loads `.env` from its cwd (and parents) via
`bootstrap_dotenv()` in `src/schwab_marketdata_mcp/bootstrap.py`.  As
long as the host launches the server with `--directory <repo>` (or
otherwise sets cwd to the package checkout), no further configuration
is required.  This is the recommended fallback for Claude Desktop:

```json
{
  "mcpServers": {
    "schwab-marketdata": {
      "command": "/abs/path/to/uv",
      "args": [
        "--directory",
        "/abs/path/to/schwab-marketdata-mcp",
        "run",
        "schwab-marketdata-mcp"
      ]
    }
  }
}
```

#### Fallback B — wrapper script (only if you cannot set cwd)

If you cannot pass `--directory` to the launcher (e.g. some Cline
versions), use a wrapper script that sources `.env` itself before
`exec`-ing the server.  Create `scripts/run-with-env.sh` (700,
owner-only) in the repo:

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -f .env ]] && set -a && source .env && set +a
exec uv run schwab-marketdata-mcp "$@"
```

then point `command` at the absolute path of that script and drop
`envFile` from `mcp.json`.

### Mandatory adjustments

- `command` — replace `${HOME}/.local/bin/uv` with the **absolute path**
  printed by `which uv`.  mise / pyenv / brew users will have a
  different prefix, e.g.
  `/Users/you/.local/share/mise/installs/python/3.12.x/bin/uv`.
- `args[1]` and `envFile` — replace
  `${HOME}/code/kevinkda/schwab-marketdata-mcp` with the absolute
  path to your local checkout.  These two paths **must** match.
- `${HOME}` may not be expanded by every Cursor version; if the server
  fails to start, substitute the literal absolute path.
- Confirm `.env` exists at the path referenced by `envFile` and is
  mode 600 (`chmod 600 .env`).  `.gitignore` already excludes it.

### Hard-no's

- **Do NOT** copy `SCHWAB_APP_KEY` / `SCHWAB_APP_SECRET` /
  `SCHWAB_CALLBACK_URL` into the `env` block of `mcp.json`.  Keep them
  in `.env` only — that is the file `.gitignore` and pre-commit hooks
  protect.  `mcp.json` is sometimes synced via dotfiles repos / IDE
  sync, which is a credential-leak vector.
- **Do NOT** add `SCHWAB_TOKEN_PATH` to `env`.  That env var is
  intentionally **not consulted** by the server (mcp.json is an
  attractive injection vector).  Use the `--config-dir` CLI flag
  instead.
- **Do NOT** register `schwab_marketdata_mcp.auth` as an MCP server.
  The auth CLI uses stdout to exchange the OAuth code with the browser;
  registering it would corrupt JSON-RPC.

### Optional `stderr` field

The `"stderr"` field is recommended but its support is **client-version
dependent**.  Independent of whether Cursor honours it, the server's
own `RotatingFileHandler` always writes to
`${XDG_STATE_HOME}/schwab-marketdata-mcp/logs/server.log` (10 MB × 5
rotated).

### Outbound `User-Agent` identification

Every request to `api.schwabapi.com` carries a stable, app-identifying
`User-Agent` header:

```text
schwab-marketdata-mcp/<our-version> python/<py-ver> schwab-py/<lib-ver>
```

- The Schwab Developer Portal **Device Type** classifier inspects this
  header; without it, schwab-py inherits httpx's generic
  `python-httpx/<ver>` UA, which the Dashboard reports as `Unknown`.
- The UA is intentionally PII-free — only package versions, never
  credentials, hostname, username, or token state.
- It is set on `client.session.headers` (the schwab-py `AsyncOAuth2Client`
  is itself an `httpx.AsyncClient` subclass), so all 10 market-data
  endpoints inherit it for free; the OAuth refresh round-trip uses the
  same UA.
- If a future schwab-py release moves or renames `session.headers`, the
  injection helper degrades gracefully (logs at DEBUG, falls back to the
  library default UA) — the data path **never** breaks because of UA
  injection.

---

## 2.  Skill repo discovery

Both skills live in
`/opt/workspace/code/kevinkda/schwab-marketdata-skill/`:

```text
schwab-marketdata-skill/
├── schwab-marketdata-ops/SKILL.md
└── schwab-marketdata-workflows/SKILL.md
```

The **easiest** activation is symlinking each skill folder into the
client's user-level skills dir:

```bash
# Cursor
ln -s "$(pwd)/schwab-marketdata-skill/schwab-marketdata-ops" \
      ~/.cursor/skills/schwab-marketdata-ops
ln -s "$(pwd)/schwab-marketdata-skill/schwab-marketdata-workflows" \
      ~/.cursor/skills/schwab-marketdata-workflows

# Claude Code (~/.claude/skills also works)
ln -s "$(pwd)/schwab-marketdata-skill/schwab-marketdata-ops" \
      ~/.claude/skills/schwab-marketdata-ops
ln -s "$(pwd)/schwab-marketdata-skill/schwab-marketdata-workflows" \
      ~/.claude/skills/schwab-marketdata-workflows
```

Each skill's `SKILL.md` frontmatter declares
`compatible_mcp_version: ">=0.1,<0.2"`.  When activated, the skill
must call `get_server_info` first and refuse to continue if the running
server's `server_version` is outside that range.

---

## 3.  Health-probe self-test

Right after configuring `cron` / `launchd` (per
[`docs/cron.example`](cron.example)):

```bash
cd ~/code/kevinkda/schwab-marketdata-mcp
bash scripts/notifier-self-test.sh
```

You should see:

- macOS: a notification "Schwab MCP self-test — if you see this,
  notifications work." in the Notification Center.
- Linux: a critical `notify-send` notification on your DE.
- In all cases: a marker file
  `~/Desktop/SCHWAB_REAUTH_NEEDED.md` is written (you can delete it
  afterwards).

If the notification did not fire:

- macOS:  System Settings → Privacy & Security → Automation, allow
  "Terminal" / "iTerm" / your shell to control "System Events".
- Linux:  Ensure `libnotify-bin` is installed and the user has a
  running session bus (D-Bus).

---

## 4.  Registering checklist

Cut down to the bare minimum:

- [ ] `which uv` returns an absolute path.
- [ ] `~/.cursor/mcp.json` has the `schwab-marketdata` server entry.
- [ ] `cp .env.example .env`; replace `dummy-not-a-real-secret` with
      real values.
- [ ] **`SCHWAB_CALLBACK_URL` in `.env` matches the redirect URI registered
      in your Schwab Developer Portal app, byte-for-byte** (scheme, host,
      port, trailing slash).  The default `https://127.0.0.1:8182` is
      strongly recommended.
- [ ] `uv run python -m schwab_marketdata_mcp.auth manual_flow --dry-run`
      exits 0 (no browser; only verifies env + path + callback URL).
- [ ] **Recommended**:
      `uv run python -m schwab_marketdata_mcp.auth manual_flow` once
      (paste the redirect URL by hand; bypasses every known
      `MismatchingStateException` failure mode — see §5).
      `login_flow` is supported but **fragile** (browser/extension/HSTS/
      stale-tab sensitive) — use it only if `manual_flow` is impractical.
- [ ] `~/Library/LaunchAgents/com.kevinkda.schwab-marketdata-mcp.health.plist`
      (macOS) or `crontab -e` (Linux) installed (see `docs/cron.example`).
- [ ] `bash scripts/notifier-self-test.sh` succeeds.
- [ ] Symlinks for both skills into `~/.cursor/skills/`.
- [ ] First Cursor message: ask "What's the latest VOO quote?" — it
      should call `get_quote(symbol="VOO")` via the MCP server and
      respond in 简体中文 (per the skill `language_directive`).

---

## 5.  Troubleshooting — `MismatchingStateException` / "CSRF Warning!"

### TL;DR — switch to `manual_flow`

If `login_flow` raised `MismatchingStateException`, **stop fighting it
and use `manual_flow`**:

```bash
uv run python -m schwab_marketdata_mcp.auth manual_flow
```

`manual_flow` does not start a local HTTPS server and does not depend
on any browser-side automation, so every `MismatchingStateException`
root cause listed below is impossible by construction.  This is the
**recommended** flow for the vast majority of users.

---

### Symptom

Running `uv run python -m schwab_marketdata_mcp.auth login_flow`, after
clicking **Allow** on the Schwab login page (and clicking **Proceed**
through the local self-signed-cert warning), the CLI crashes with:

```text
authlib.oauth2.rfc6749.errors.MismatchingStateException:
mismatching_state: CSRF Warning! State not equal in request and response.
```

### Why this happens (true root cause)

The OAuth `state` parameter is generated **once per `login_flow`
invocation** by `authlib` (random 30-char token, see
`authlib.common.security.generate_token`) and embedded into the
authorize URL that schwab-py opens in your browser.  After **Allow**,
Schwab is supposed to redirect to your registered `redirect_uri` with
the **same** `state` value appended.  schwab-py's local Flask server
captures that callback, then `authlib.oauth2.client.OAuth2Client.
fetch_token` calls `parse_authorization_code_response(received_url,
state=<this-run-state>)` which does a **byte-for-byte** comparison
([`authlib/oauth2/rfc6749/parameters.py:154-156`](
https://github.com/authlib/authlib/blob/v1.7.2/authlib/oauth2/rfc6749/parameters.py#L154-L156)).
Any of the following will desynchronise it:

1. **Stale browser tab from a previous, aborted run.**  After
   `Ctrl-C`, the _old_ tab is still parked at `https://api.schwabapi.com/
   v1/oauth/authorize?...&state=<OLD>`.  When you start a fresh
   `login_flow`, two tabs are alive simultaneously; clicking **Allow**
   in the _old_ tab POSTs `state=<OLD>` to the brand-new local
   callback server, which is expecting `state=<NEW>`.  **This is the
   #1 cause.**
2. **Schwab redirect-uri pinning rewrites `state`.**  If the
   `redirect_uri` advertised by `schwab-py` (built from
   `SCHWAB_CALLBACK_URL`) does not match what is registered in the
   Developer Portal **byte-for-byte** (scheme, host, port, trailing
   slash, case), some Schwab-side implementations silently substitute
   the registered URI **and may drop or rewrite the `state`
   parameter**.  Pre-flight now rejects the obviously-broken case
   (no port, wrong host) up front, but case / trailing-slash
   mismatches still slip through and surface here.
3. **Browser HSTS / extension rewriting the redirect.**  Chrome's
   HSTS preload, password managers, ad blockers, "HTTPS Everywhere"
   forks, or content-blocking rules can mutate the
   `https://127.0.0.1:8182/...` redirect (strip the port, drop query
   parameters, follow a 307 to a different host).  The local
   server still receives **a** request, but with a `state` that no
   longer matches the one schwab-py issued.
4. **Wrong tab focus on an already-completed run.**  If you re-run
   `login_flow` immediately after a successful run, the previous
   callback URL might still be cached in the browser history /
   address bar; pressing Enter there replays the old `state`.

Three hypotheses we explicitly **ruled out** while debugging:

- **`multiprocess` start-method races.**  schwab-py forks a child
  process to run Flask, but `state` lives entirely in the **parent**
  process (see `schwab/auth.py:580-601` — `AuthContext` is constructed
  before `multiprocess.Process` starts; the child only enqueues the
  raw URL it received).  Linux dev-desks default to `fork`, so even
  if state were process-local, it'd be copy-on-write to the child.
  **Not the bug.**
- **`authlib` `self.state` vs explicit `state=` kwarg.**  schwab-py's
  `client_from_received_url` constructs a _fresh_ `OAuth2Client` and
  passes `state=auth_context.state` explicitly to `fetch_token`.
  `authlib/oauth2/client.py:212` does `state = state or self.state`,
  so the explicit kwarg wins.  **Not the bug.**
- **`callback_url` port mismatch with Developer Portal.**  Pre-flight
  now rejects this up front.  **Already fixed.**

### Why `login_flow` is fundamentally fragile

Every cause above is **outside the Python process**: stale browser
tabs, browser extensions, HSTS, Schwab-side `redirect_uri`
normalisation.  schwab-py cannot defend against any of them — the
local Flask server has no way to verify _which_ tab originated the
callback.  This is why we now **recommend `manual_flow` as the
default**: you paste the URL **you can see right now in the address
bar**, eliminating every cross-tab and browser-rewrite vector.

### Recovery checklist

If you must use `login_flow` (e.g. headless setup, scripted onboarding):

1. Confirm `SCHWAB_CALLBACK_URL=https://127.0.0.1:8182` (or whatever
   you have registered) in `.env` matches the Developer Portal
   **byte-for-byte** (case, port, trailing slash).
2. Run `uv run python -m schwab_marketdata_mcp.auth login_flow --dry-run`
   to verify pre-flight passes without spawning a browser.
3. **Quit your browser entirely** (not just close the tab — quit).
   This guarantees zero stale Schwab tabs.
4. Re-run `uv run python -m schwab_marketdata_mcp.auth login_flow`.
5. Press **ENTER** when prompted, click **Advanced → Proceed** on the
   self-signed-cert page, and wait for the local server to capture
   the callback automatically.
6. **If it fails again — switch to `manual_flow`**:

   ```bash
   uv run python -m schwab_marketdata_mcp.auth manual_flow
   ```

   1. The CLI prints the authorize URL.
   2. Copy-paste it into your browser.
   3. Log in, click **Allow**.
   4. The browser will land on `https://127.0.0.1:8182/?code=...&
      state=...` (the page itself will fail to load — that's
      expected, the server is not running).
   5. Copy the **entire URL** from the address bar.
   6. Paste it back into the CLI prompt and press Enter.
   7. Token is written.

---

## 6.  Data coverage limits

This server exposes **read-only** Schwab Market Data Production endpoints
only.  Two clarifications that matter when integrators size up the
project:

- **`get_price_history` is the candlestick / kline endpoint.**  If you
  need OHLCV bars (candles, klines, candlesticks), use this tool — see
  the supported `period_type` × `frequency_type` × `frequency` matrix
  and lookback limits under
  [README → "Data coverage clarifications"](../README.md#data-coverage-clarifications)
  ([中文](../README_zh.md#数据覆盖澄清)).
- **What this server does NOT cover.**  Time & sales (tick-level
  trades), Level 2 historical snapshots, fundamental / earnings time
  series, and news / SEC filings are **architecturally unavailable**
  through the Schwab Market Data API.  The same README section lists
  recommended third-party providers (Polygon.io, Tiingo, Alpaca,
  Databento, FMP, SEC EDGAR) for each missing data class.
- **Trader API endpoints** (account, orders, transactions, positions)
  are explicitly out of scope per §1 of this document — this server
  is **read-only Market Data only**.  See plan §1 / §10 for the
  rationale.
