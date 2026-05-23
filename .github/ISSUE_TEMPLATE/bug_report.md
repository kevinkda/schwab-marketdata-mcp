---
name: Bug report
about: Something is broken or returns unexpected output.
title: "bug: <short summary>"
labels: ["bug"]
---

## Environment

| Field | Value |
| ----- | ----- |
| OS (and version) | e.g. macOS 14.5 (arm64) / Ubuntu 22.04 (x86_64) / Windows 11 23H2 |
| Python | output of `python --version` |
| `uv` | output of `uv --version` |
| `mcp` (Python SDK) | output of `uv pip show mcp \| grep Version` |
| `schwab-py` | output of `uv pip show schwab-py \| grep Version` |
| `schwab-marketdata-mcp` (this server) | git commit hash + tag |
| MCP host (Cursor / Claude Code / etc.) | host name + version |

## Reproduction steps

1. ...
2. ...
3. ...

Include the exact tool input you sent, e.g.:

```json
{
  "name": "get_price_history",
  "arguments": {"symbol": "VOO", "period_type": "DAY"}
}
```

## Expected behavior

What you expected to see in the response or in the MCP host UI.

## Actual behavior

What you actually saw — JSON response, exception, or hung tool call.

## Logs

Paste relevant lines from `${XDG_STATE_HOME}/schwab-marketdata-mcp/logs/server.log`
or the host-redirected stderr file.

> **Important — redact before pasting:** the server should never log a
> Schwab Bearer access token or refresh token, but if any line contains
> something resembling a token (`access_token=...`, `Bearer ey...`,
> `refresh_token=...`), replace it with `<REDACTED>` before pasting.
> Do **not** attach `.env` or `token.json`.

```text
<paste log here>
```

## Additional context

- Did this start after a specific commit or version bump?
- Does it reproduce after a clean `uv sync --extra dev`?
- Is it specific to one MCP host or platform?
