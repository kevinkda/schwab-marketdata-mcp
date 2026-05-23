# Threat model — schwab-marketdata-mcp

> **Scope** — this document covers the local-host attack surface of the
> `schwab-marketdata-mcp` server: token storage, OAuth flow, MCP stdio
> JSON-RPC channel, and the shared dependency stack.  It does **not** cover
> Schwab's server-side platform.
>
> Supersedes nothing; this is the v1 baseline.  Update **before** any
> material design change (new endpoint surface, new tool that mutates
> account state, change of token storage, etc.).
>
> Plan reference: §3, §3.3, §3.4, §6.3, §8.

## 1. Assets to defend

| ID  | Asset                                         | Sensitivity                   | Why it matters                                  |
| --- | --------------------------------------------- | ----------------------------- | ----------------------------------------------- |
| A1  | Schwab `app_key` / `app_secret`               | High (long-lived credential)  | Tied to user's Schwab Developer account; abuse → ToS termination + data subject. |
| A2  | OAuth `access_token`                          | High (90-min bearer)          | Wire access to Market Data; logs are PII-adjacent. |
| A3  | OAuth `refresh_token`                         | Highest (7-day rotate-on-use) | Rotates → race conditions = forced re-auth.      |
| A4  | Schwab market data responses                  | Medium                        | **Non-redistributable** under SOSA; see ToS snapshot. |
| A5  | MCP stdio JSON-RPC channel                    | Medium                        | Corruption breaks the agent <-> server contract. |
| A6  | Local FS — `~/.local/state/schwab-marketdata-mcp/` | High                       | Token persistence; world-readable would leak A1–A3. |
| A7  | DuckDB cache file (`cache.duckdb`)            | Medium                        | Caches Schwab market-data responses; non-redistributable per ToS. Sits inside A6's parent dir and inherits its 0o700 hardening; the file itself is `0o600`. |

## 2. Trust boundaries

```text
┌─────────────────────────────────────────────────────────────────────┐
│  User (local trusted)                                               │
│   └── Cursor / Claude (local trusted)                               │
│        └── stdio child process: schwab-marketdata-mcp  ◄── B1       │
│             ├── reads:  $XDG_STATE_HOME/.../token.json (B2: FS perm)│
│             ├── writes: $XDG_STATE_HOME/.../usage.jsonl, logs/      │
│             └── HTTPS → api.schwabapi.com                ◄── B3 net │
└─────────────────────────────────────────────────────────────────────┘
B1: process boundary  (stdio JSON-RPC; only Cursor on the same machine)
B2: filesystem boundary (mode 0o600 / 0o700, allow-list)
B3: network boundary (TLS to Schwab production)
```

## 3. STRIDE catalogue

| #  | STRIDE | Threat                                                                    | Mitigation (where)                                                                                       |
| -- | ------ | ------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| T1 | S      | Attacker spoofs `mcp.json` to point at malicious binary                   | Out-of-scope — same as the user's $PATH; Cursor uses absolute path.                                      |
| T2 | T      | Tampered `token.json` (e.g. attacker swaps in their own `refresh_token`)  | `check_token_file_state` checks **perms before parse** so attacker-owned 0o644 file is rejected.         |
| T3 | T      | Symlink redirection: `token.json` -> `/etc/shadow`                        | `resolve_token_path` rejects any symlink in the parent chain after resolve; allow-list under `~/.local/state` or `~/.config`. |
| T4 | R      | Bearer token leaked into log                                              | Global `RedactBearerFilter` on root logger; exception classes never carry raw httpx response.            |
| T5 | I      | `usage.jsonl` records request/response payloads                           | `metrics.py` records **only** `{ts, tool, status, error_class, latency_ms}` — no input/output content.   |
| T6 | I      | stdout JSON-RPC corrupted by stray `print` from schwab-py / httpx         | `server.py` monkey-patches `builtins.print` → stderr; httpx/schwab loggers forced to `WARNING`; integration test asserts first stdout byte is valid JSON-RPC. |
| T7 | D      | `429` storm wedges the agent waiting for retries                          | Token-bucket limiter releases bucket during retry sleep; `SCHWAB_RATE_LIMIT_PER_MIN` env var.            |
| T8 | E      | Cursor passes `SCHWAB_TOKEN_PATH=/etc/shadow` via `mcp.json`              | **`SCHWAB_TOKEN_PATH` env is not consulted** — only `--config-dir` CLI flag, which is allow-list checked. |
| T9 | E      | Two concurrent Cursor sessions race to refresh `refresh_token`            | `fcntl.flock(LOCK_EX)` on `${token_path}.lock`; reload-from-disk → refresh → write within the lock.       |
| T10| I      | Token file ends up in iCloud/Dropbox (replicates secret to vendor)        | `is_cloud_path` best-effort detection; explicit `--i-understand-cloud-sync-risk` opt-in required.        |
| T11| T      | Attacker injects shell metachars via `symbol` parameter                   | Pydantic `StringConstraints(pattern=…)` per layer (stock / index / OSI / CUSIP); request goes via `httpx` `params=` not URL string concat. |
| T12| I      | Schwab returns market data; user accidentally `git push` to public repo   | Workflows skill's `gh repo view --json isPrivate` pre-flight check; ToS snapshot in skill `references/`. |
| T13| T      | DuckDB cache file (A7) tampered with — attacker swaps in fake quotes      | File is created `0o600` under the same `0o700` parent as `token.json`; corrupt DB triggers quarantine to `cache.duckdb.corrupt-<ts>` and a fresh DB is opened. Cache reads are best-effort: any DuckDB / IO error returns `None` (treated as miss) so the live API path is always reachable. |
| T14| I      | Cache file replicates Schwab market data to iCloud / Dropbox              | Lives in the same `XDG_STATE_HOME` parent as `token.json`; the existing `is_cloud_path` opt-in prompt covers both files (user-supplied `--config-dir` is allow-list checked). |

## 4. OWASP Top 10 mapping

See plan §6.3 for the full matrix.  Quick map for this repo:

| OWASP 2021 | This repo's mitigation site                                                  |
| ---------- | ---------------------------------------------------------------------------- |
| A01 Broken Access Control | `security.resolve_token_path` allow-list; T8 SCHWAB_TOKEN_PATH not consulted |
| A02 Cryptographic Failures| HTTPS-only via httpx; `RedactBearerFilter`; token mode `0o600`              |
| A03 Injection             | Pydantic per-layer regex; `httpx.get(params=…)` (not str concat)            |
| **A04 Insecure Design**   | **This document + plan §3 boundary statements + cron lid-close fall-back**   |
| A05 Security Misconfig    | `pre-commit install` mandatory; `gitleaks` + `detect-secrets`; `.gitignore` precise paths; `!uv.lock` reverse rule guard |
| A06 Vulnerable Components | `pip-audit` in CI; deps locked to tested windows                            |
| A07 Identification/Auth Failures | OAuth 3-legged via schwab-py; refresh rotate-on-use serialized via flock |
| A08 Software/Data Integrity      | uv.lock committed; `json.load(strict=True)`; no `eval` / `exec`         |
| A09 Logging & Monitoring         | `usage.jsonl` + `health.py` cron; structured JSON-line logs              |
| A10 SSRF                          | All outbound URLs are constants in schwab-py; user-supplied data only via params, never as URL |

## 5. Out-of-scope risks (declared, not mitigated)

| ID  | Risk                                                                                       | Why out of scope                                                                                       |
| --- | ------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------ |
| O1  | Local malware exfiltrating `token.json`                                                    | Same trust level as the user's account on the box; no defense at this layer.                           |
| O2  | Compromised `mise`/`uv` toolchain                                                          | Supply-chain risk handled by `pip-audit` + `uv.lock` pinning; not eliminated.                          |
| O3  | Cursor (or any host agent) prompt-injection that calls `get_quote("AAPL'); rm -rf …")`     | Pydantic will reject the symbol; downstream agent prompt-hygiene is the agent's responsibility.        |
| O4  | Schwab's own server breach                                                                 | Provider-side; outside our control.                                                                    |
| O5  | Windows native (no `fcntl`)                                                                | Plan §1 platform boundary — v2 may add `msvcrt.locking`.                                               |
| O6  | User edits gitignore-listed file (`.env`) and force-pushes to public                       | Pre-commit `gitleaks` blocks; force-push of main is forbidden by the user-rule "Git Safety Protocol".   |

## 6. schwab-py upgrade review checklist

The single most fragile dependency is **schwab-py** (Bus factor = 1).  Before
bumping `schwab-py` past `1.5.x`, a reviewer **must** verify:

<!-- markdownlint-disable MD013 -->
- [ ] `schwab.client.Client.token_age()` still exists and returns `timedelta`.  Plan §3.2.2 health probe assumes this; fall-back is `token.json` mtime read but that's a degraded mode.
- [ ] `schwab.client.Client.Movers.Index` / `Quote.Fields` / `Options.*` / `MarketHours.Market` / `Instrument.Projection` enum **names** unchanged.  ``models._assert_enum_alignment`` will throw at import time if any drift; fix the Literal first, then re-run tests.
- [ ] `schwab.auth.client_from_login_flow(asyncio=True, …)` keyword still accepts ``asyncio``.  If renamed, update `client.py::make_client` and `auth_logic.py`.
- [ ] No new mandatory parameter on `easy_client` / `client_from_*_flow` — silent default-arg additions can change behavior.
- [ ] schwab-py changed how it stores `token.json` internal schema?  See **Token field-name drift log** below.
<!-- markdownlint-enable MD013 -->

## 6.5. mcp Python SDK 1.x → 2.x compatibility checklist

When `mcp` 2.0 is released, the following API surfaces must be verified
before bumping the upper bound in `pyproject.toml`:

- [ ] `mcp.server.fastmcp.FastMCP(name=...)` constructor signature unchanged
- [ ] `@mcp.tool(name=..., description=...)` decorator semantics unchanged
- [ ] `mcp.run(transport="stdio")` entry point still supported (vs new
  `Server.run()`)
- [ ] `tools/list` and `tools/call` JSON-RPC method names unchanged
- [ ] `Context` injection (if introduced in 2.x) does not require `@mcp.tool`
  refactor
- [ ] `Resource` / `Prompt` APIs (if added in 2.x) optional, not required
- [ ] Stdio frame format compatible with 1.x clients (Cursor, Claude Desktop)

Migration steps:

1. Bump `mcp>=2.0,<3.0` in `pyproject.toml`.
2. `uv sync --upgrade mcp` and run the full test suite.
3. Verify `tests/test_server_integration.py` stdio harness passes.
4. Smoke-test in real Cursor / Claude Desktop instance.
5. Update `CHANGELOG.md` with breaking changes if any.
6. Skill repo: bump `compatible_mcp_version` in all 4 `SKILL.md` files.

### 6.6 schwab-py upgrade drift log

`schwab-py` is the bus-factor-1 dependency (see §6 above).  Whenever the
pin is bumped (typically via `uv lock --upgrade-package schwab-py`), the
maintainer **must** record the diff in this section **before** merging the
upgrade PR.  This complements §6.5 (mcp 2.x checklist) and §7 (token
field-name drift) so silent breakage in any of the four most-volatile
schwab-py surfaces is forced into a reviewable artifact.

The fields tracked are the four known-fragile surfaces:

| Field                                | Why it's tracked                                                                                                                         |
| ------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `TokenState` fields verified         | `health.py` and `client.py` read `creation_timestamp` / `expires_at`; renames break the health probe and the in-process token age cache. |
| `Movers.Index` members count         | `tools.get_movers` enumerates the literal; new members add tool surface but old members vanishing **breaks live agents**.                |
| `Client.token_age` signature         | Plan §3.2.2 health probe and `health.py` assume `timedelta` return.  A signature change drops us into the degraded `mtime`-fallback.     |
| Streaming services count             | `tools.get_streaming_snapshot` whitelists service names; renames silently shrink the experimental tool surface.                          |

<!-- markdownlint-disable MD013 -->

| schwab-py version | bumped_at                | TokenState fields verified                                          | Movers.Index members                                                                                          | Client.token_age signature | Streaming services |
| ----------------- | ------------------------ | ------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- | -------------------------- | ------------------ |
| 1.5.1             | 2026-05-23 (v0.1.0 baseline) | creation_timestamp / expires_at present                              | 11 (DJI/COMPX/SPX/NYSE/NASDAQ/OTCBB/INDEX_ALL/EQUITY_ALL/OPTION_ALL/OPTION_PUT/OPTION_CALL)                   | timedelta returned         | 13 services per audit report |

<!-- markdownlint-enable MD013 -->

Verification recipe (run inside the upgraded `uv` env before adding the row):

```bash
uv run python -c "
import schwab
from schwab.client import Client
print('schwab-py', schwab.__version__)
print('TokenState fields:', list(Client.TokenState.__annotations__.keys())
      if hasattr(Client, 'TokenState') else 'absent')
print('Movers.Index members:',
      len(Client.Movers.Index.__members__),
      list(Client.Movers.Index.__members__.keys()))
print('token_age return annotation:',
      Client.token_age.__annotations__.get('return'))
"
```

If any field diverges from the previous row:

1. **Stop.** Do not merge the upgrade PR.
2. Open a tracking issue summarizing the diff.
3. Patch the affected code (Literal types in `models.py`, enum alignment
   asserts, health probe fallback path) before resuming the bump.
4. Re-run the recipe; only then append a new row to the drift log.

## 7. Token field-name drift log

Plan §3.2.2 — schwab-py docs explicitly say "do not inspect token.json
fields directly".  We **only** read raw `token.json` via:

1. `security.check_token_file_state` — calls `json.load`; checks the result
   is a `dict`; returns the dict but never touches inner fields.
2. The fall-back path in `health.py` — uses **file mtime only**, never
   inner fields.

**Drift log** (append a row when schwab-py changes):

| Date       | schwab-py version | Observed change                                  | Action taken |
| ---------- | ----------------- | ------------------------------------------------ | ------------ |
| 2026-05-23 | 1.5.1             | Baseline.  Top-level keys: `creation_timestamp`, `token` (nested). | None.        |

## 8. Early-exit / abandoned-work artifacts

If the project is paused mid-implementation (per STRATEGY.md §2.3 early-exit
budget), **the following are stable, reusable in isolation**:

- `errors.py` — usable as a generic redact-filter library.
- `models.py` — Pydantic v2 schemas reusable for any other Schwab client.
- `security.py` — path allow-list + flock primitives reusable for any
  token-bearing CLI tool.
- `cache.py` — DuckDB-backed local cache with TTL + best-effort
  corrupt-recovery semantics, reusable for any other rate-limited
  HTTP API client (the four cache tables are independent of the
  Schwab schema).
- `tests/fixtures/seed/*.json` — hand-crafted shape references, no real
  market data.

Do **not** treat any partial state as "ready to merge" without the full
`scripts/local-ci.sh` gate green.
