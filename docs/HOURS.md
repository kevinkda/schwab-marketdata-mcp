# Hours invested

> Tracks cumulative hours invested per the project's `STRATEGY.md` §2.3
> Early Exit Criteria (kept in the maintainer's planning notes, outside
> this repo).  The 300-hour threshold (75% of the 480 h project budget)
> triggers a **mandatory pause and reassessment** before any further
> implementation work continues.
>
> Estimates are **maintainer self-reports**, not time-tracker entries; they
> are intentionally rounded to the nearest hour because the goal is the
> early-exit signal, not invoiceable precision.  Update this file in the
> same commit that bumps `pyproject.toml` and writes the `CHANGELOG.md`
> release row.

## Per-version log

| Version | Released   | Estimated dev hours | Cumulative | Notes                                                                              |
| ------- | ---------- | ------------------- | ---------- | ---------------------------------------------------------------------------------- |
| v0.1.0  | 2026-05-23 | ~12 h               | 12 h       | Initial scaffold: 12 tools, OAuth, rate limiter, retry/backoff, full unit tests, docs (THREAT_MODEL, REGISTER, RELEASE), threat model, pre-commit gates. |
| v0.1.1  | 2026-05-23 | ~3 h                | 15 h       | Streaming snapshot tool (#13), GitHub community files (issue / PR templates).      |
| v0.2.0  | 2026-05-23 | ~6 h                | 21 h       | DuckDB-backed local cache layer (`cache.py`), 14th tool `get_cache_stats`, threat-model T13 / T14 / A7 entries, Shakeout v2 playbook (zh + en).         |
| v0.2.1  | 2026-05-23 | ~4 h                | 25 h       | Skill repo English mirror reaches 100% parity (workflows + ops); CHANGELOG bilingual. |
| v0.3.0  | 2026-05-23 | ~1 h                | 26 h       | `docs/SLO.md`, `Cache.hourly_breakdown`, `metrics.recent_errors_window`, `health_check.overall_status` + 5-min error rate. |
| v0.3.1  | 2026-05-24 | ~0.5 h              | 26.5 h     | `serverInfo.version` reports release tag (mcp 1.27.x compatibility fix). |
| v0.4.0  | 2026-05-27 | ~5.5 h              | 32 h       | P1/C: `option_chain_snapshots` + `iv_history` + `get_iv_percentile` (15th tool) + 28 tests. |

## Phase 0 budget (per STRATEGY)

- **Allocated**: 120 h (Phase 0 — "build the core")
- **Used**: 32 h (≈ 27%)
- **Remaining**: 88 h
- **Status**: ✅ Within budget
- **75% Phase 0 alarm (90 h)**: NOT YET REACHED

## Total project budget (per STRATEGY)

- **Allocated**: 480 h (Phase 0 + Phase 2 + Phase 3 / Phase 4 fragments)
- **Used**: 32 h (≈ 7%)
- **Remaining**: 448 h
- **Status**: ✅ Within budget
- **Early-exit threshold (75% = 360 h)**: NOT YET REACHED
- **Hard ceiling (100% = 480 h)**: NOT YET REACHED

## How to update this file

1. Estimate hours invested in the new version *honestly* — round to the
   nearest hour.  Lying to yourself defeats the early-exit guard.
2. Append a new row to **Per-version log** (do not edit historical rows).
3. Update both **Phase 0** and **Total project** counters.
4. If `Used ≥ 75% Phase 0` or `Used ≥ 75% total`, do **not** ship the
   release: open a STRATEGY review issue first.
5. Mention this file's update in the `CHANGELOG.md` entry's
   "Project metadata" section.

## Why this exists

`STRATEGY.md` §2.3 commits to an early-exit budget so that the project
does not silently consume more attention than its expected return.
Without an explicit ledger, "feature creep" can spend the budget invisibly
— each individual sprint feels small but the sum drifts past the ceiling.

This file is the **only** place the project tracks cumulative cost.  Keep
it up to date; it is intentionally low-ceremony so the friction of
updating it is lower than the friction of remembering not to ship past
the budget.
