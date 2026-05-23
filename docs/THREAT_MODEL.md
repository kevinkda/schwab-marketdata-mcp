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
- `tests/fixtures/seed/*.json` — hand-crafted shape references, no real
  market data.

Do **not** treat any partial state as "ready to merge" without the full
`scripts/local-ci.sh` gate green.
