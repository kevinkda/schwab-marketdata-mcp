# Service Level Objectives & Indicators

## Purpose

Define measurable service quality targets so users / future contributors can
detect regressions before they impact research workflows.  SLOs are
aspirational; failures don't block release but trigger investigation.

The signals below are emitted by code that already lives in this repo —
`metrics.py` (usage.jsonl), `cache.py` (`cache_events` table) and
`health.py` (token-health probe + `health_check` MCP tool).  No external
SaaS observability stack is required.

## SLOs

| SLO | Target | Measurement source | Window | Status |
|---|---|---|---|---|
| `get_quote` p95 latency (cache hit) | < 500 ms | `usage.jsonl` `latency_ms` filtered by `cache_status=="hit"` | 7d rolling | 🟡 not yet measured |
| `get_quote` p95 latency (cache miss) | < 1500 ms | same, filtered by `cache_status=="miss"` | 7d rolling | 🟡 |
| `price_history` cache hit rate (active hours) | > 50% | `cache_events` `kind="hit"` / total during 9:30-16:00 ET | 24h | 🟡 |
| Token health uptime | > 99% | `health.py` exit code 0 / total runs | 7d rolling | 🟡 |
| Tool call error rate | < 5% | `usage.jsonl` `status=="err"` / total calls | 5 min sliding window | 🟢 normally < 1% |
| Cache DB size | < 500 MB | `cache.duckdb` file size | instant | 🟢 currently 0.3 MB |
| Schwab API rate limit headroom | > 20% | `(120 - calls_in_last_minute) / 120` | 1 min sliding | 🟢 |

Status legend: 🟢 currently within target · 🟡 not yet instrumented for
automated reporting · 🔴 currently violating target (none today).

## SLIs (raw signals)

- `usage.jsonl` events: `ts`, `tool`, `status`, `error_class`, `latency_ms`,
  `cache_status` — append-only one-line-JSON under `$XDG_STATE_HOME/schwab-marketdata-mcp/usage.jsonl`.
- `cache_events` table: `ts`, `kind` (`hit`/`miss`/`expired`/`write`),
  `table_name` — DuckDB table inside `cache.duckdb`.
- `server.log`: structured JSON events at WARN/ERROR level emitted via
  the standard `logging` module (currently stderr-only; users can redirect).

## Alerting policy (current implementation)

The `health_check` MCP tool collapses these signals into a single
`overall_status` field on every call:

- **degraded**: `error_rate > 5%` over 5 min sliding window
- **unhealthy**: `error_rate > 20%` **OR** `token_state == EXPIRED`
- **healthy**: otherwise

Implementation: see `health_check_impl` in
[`src/schwab_marketdata_mcp/tools/meta.py`](../src/schwab_marketdata_mcp/tools/meta.py)
and the supporting `metrics.recent_errors_window` helper in
[`src/schwab_marketdata_mcp/metrics.py`](../src/schwab_marketdata_mcp/metrics.py).

There is no remote pager; alerting is deliberately in-band so a human
operator who pings `health_check` (or who reads
`~/Desktop/SCHWAB_REAUTH_NEEDED.md` after the cron probe) sees the
state without a SaaS dashboard.

## SLO compliance reporting

Run `python -m schwab_marketdata_mcp.stats --slo-report` to print 7d SLO
compliance per row above.  (Future enhancement; not yet implemented as
of v0.3.0.  Tracked under [Unreleased] → planned.)

In the meantime, ad-hoc compliance can be checked with the existing CLI:

```bash
uv run python -m schwab_marketdata_mcp.stats --window-days 7 --json
uv run python -c "
from schwab_marketdata_mcp.cache import get_cache
c = get_cache()
if c is not None:
    for row in c.hourly_breakdown(hours=24):
        print(row)
"
```

## When SLOs change

Update this file when bumping minor version, or when adding/removing SLI
sources.  Keep history in CHANGELOG `[Unreleased]` Changed section so
future readers can correlate the policy change with the release.
