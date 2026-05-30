# Known Issues

Tracked known issues and limitations for `schwab-marketdata-mcp`. For
resolved issues see [CHANGELOG.md](./CHANGELOG.md).

## Open

### Windows Tier A support is experimental (not real-machine verified)

The `_platform.py` cross-platform shim ships with full POSIX/Windows test
coverage and a `windows-latest` CI runner, but Windows is classified
**Tier A (experimental)**: file-locking, `secure_chmod`, and
`restrictive_umask` paths have not been validated on a real Windows
machine, only in CI. Treat Windows as best-effort until real-hardware
verification is done.

### Option chain schema split (`option_chain_snapshots` vs `option_chain_cache`)

Two DuckDB tables coexist: the v0.2 raw-JSON `option_chain_cache`
(read-back hit path) and the v0.4 row-normalised `option_chain_snapshots`
(analytics: `aggregate_atm_iv` / `get_iv_percentile`). They are not
unified; a schema-consolidation migration is a candidate for a future
sprint. Documented in CHANGELOG v0.4.0 + the v0.4 retrospective.

### IV percentile under-samples below 30 observations

`get_iv_percentile` returns `percentile_rank=null` with a
`sample_count_below_30` warning when fewer than 30 cached IV observations
exist for the underlying — by design, so callers do not over-interpret a
tiny sample. Build cache depth before relying on the percentile rank.

## Upstream / Deferred

- **`schwab-py` is bus-factor-1** — tracked in `docs/THREAT_MODEL.md` §6 /
  §6.6. Dependabot is configured to **ignore** `schwab-py` bumps; any
  upgrade must go through the manual upgrade checklist + drift log.
- **`mcp` 1.x → 2.x major bump deferred** — requires the §6.5
  compatibility checklist; dependabot ignores the major bump.

## Resolved

See [CHANGELOG.md](./CHANGELOG.md) for the full history.
