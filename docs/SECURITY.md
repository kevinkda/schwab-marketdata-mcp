# Security

`schwab-marketdata-mcp` is a read-only market-data MCP server (quotes,
option chains, movers, market hours). It holds a Schwab OAuth credential
but exposes **no order-placement, cancel, or fund-transfer tool** — it is
read-only by API design (the schwab-py read surface it wraps cannot move
money or open/close positions).

For the full STRIDE catalogue, asset inventory, and trust-boundary
diagram, see [`docs/THREAT_MODEL.md`](./THREAT_MODEL.md). This document is
the short operator-facing summary.

## Threat model (summary)

The primary assets are the Schwab credentials: `app_key` / `app_secret`
(High), the 90-minute `access_token` (High), and the 7-day rotate-on-use
`refresh_token` (Highest). The main local-host threats are:

- **Token leak at rest** → tokens stored under
  `$XDG_STATE_HOME/schwab-marketdata-mcp/` with directory `0o700` and the
  `token.json` file `0o600`.
- **PII in logs** → credentials and tokens are never logged; redaction is
  applied on error paths; default log level is WARNING.
- **TLS spoofing / MITM** → httpx `verify=True` always; TLS verification
  is never disabled.
- **Non-redistribution** → Schwab market-data responses are
  non-redistributable under SOSA ToS; this server is for interactive
  single-user research only.

Host compromise (RCE, malware, token exfiltration) is **out of scope** —
an attacker with shell access can replay the token directly against
`api.schwabapi.com`. Use full-disk encryption and a dedicated user
account; never commit `.env` / token files.

## Secret handling

- Schwab `app_key` / `app_secret` come from `.env` (git-ignored); tokens
  persist to `$XDG_STATE_HOME/schwab-marketdata-mcp/token.json` at POSIX
  mode `0o600` inside a `0o700` directory.
- No secret is logged; error paths redact token / key material.
- Pre-commit runs `detect-secrets`; CI runs `gitleaks-action@v2` on every
  push and PR to block accidental secret commits.

## Read/write boundary

This MCP is **read-only by API design**: it wraps only the schwab-py
market-data read endpoints (quotes, option chains, price history, movers,
market hours). There is no trade / order path in the codebase. The OAuth
token nonetheless carries Schwab's broad scope because Schwab exposes no
narrower market-data-only scope; see `docs/THREAT_MODEL.md` for the scope
discussion.

## Reporting security issues

Open a private security advisory on GitHub:
<https://github.com/kevinkda/schwab-marketdata-mcp/security/advisories>.
Do **not** open a public issue with the details.
