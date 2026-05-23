"""Local usage / latency metrics — ``usage.jsonl`` recorder.

Plan §3.4.3 — append-only one-line-JSON.  We record **only metadata**
(``ts``, ``tool``, ``status``, ``error_class``, ``latency_ms``); never the
input parameters or response body, to avoid PII / token leakage.

The 30-day rolling truncate is performed by ``health.py`` cron (plan §3.4.1)
to keep tool-call hot paths free of FS bookkeeping.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any, Final

from . import _platform
from .security import xdg_state_root

USAGE_FILE_NAME: Final[str] = "usage.jsonl"
DEFAULT_RETENTION_DAYS: Final[int] = 30


def usage_path() -> Path:
    """Return the canonical usage.jsonl path under XDG_STATE_HOME."""
    return xdg_state_root() / "schwab-marketdata-mcp" / USAGE_FILE_NAME


def _utc_now_iso() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="milliseconds")


def record(
    *,
    tool: str,
    status: str,
    error_class: str | None,
    latency_ms: int,
    path: Path | None = None,
    cache_status: str | None = None,
) -> None:
    """Append a single JSON line to usage.jsonl.

    Failure to write **must not** raise to the tool caller — metrics are
    best-effort.  Any exception is suppressed and a warning written to
    stderr.
    """
    if status not in {"ok", "err"}:
        raise ValueError(f"status must be 'ok' or 'err', got {status!r}")
    target = path or usage_path()
    payload: dict[str, Any] = {
        "ts": _utc_now_iso(),
        "tool": tool,
        "status": status,
        "error_class": error_class,
        "latency_ms": int(latency_ms),
    }
    if cache_status is not None:
        payload["cache_status"] = cache_status
    line = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    try:
        target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        # Touch first so we can chmod before writing real content (defense in
        # depth - avoids a window where the file is world-readable on POSIX).
        if not target.exists():
            target.touch(mode=0o600, exist_ok=True)
            _platform.secure_chmod(target, 0o600)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError as exc:
        sys.stderr.write(f'{{"event":"metrics_write_failed","error":"{exc!s}"}}\n')


@contextmanager
def time_tool(tool: str, *, path: Path | None = None) -> Iterator[dict[str, Any]]:
    """Context manager that records latency + status for a tool call.

    On exception (any subclass of :class:`Exception`), the recorded ``status``
    is ``"err"`` and ``error_class`` is the exception's qualified class name.
    The exception is **re-raised**, never swallowed.

    Callers may set ``state["cache_status"]`` inside the ``with`` block to
    record one of ``"hit" | "miss" | "bypass" | "disabled"``.
    """
    start = time.perf_counter()
    state: dict[str, Any] = {"tool": tool, "status": "ok", "error_class": None}
    try:
        yield state
    except Exception as exc:
        state["status"] = "err"
        state["error_class"] = type(exc).__name__
        raise
    finally:
        latency_ms = int((time.perf_counter() - start) * 1000)
        record(
            tool=state["tool"],
            status=state["status"],
            error_class=state["error_class"],
            latency_ms=latency_ms,
            path=path,
            cache_status=state.get("cache_status"),
        )


# ---------------------------------------------------------------------------
# 30-day rolling truncate (called from health.py cron)
# ---------------------------------------------------------------------------


def truncate_to_window(*, days: int = DEFAULT_RETENTION_DAYS, path: Path | None = None) -> int:
    """Keep only entries whose ``ts`` is newer than ``now - days``.

    Returns the number of entries kept.  Atomic: writes to a temp file then
    renames over the original, so a crash mid-truncate cannot lose data.
    """
    if days < 1:
        raise ValueError("days must be >= 1")
    target = path or usage_path()
    if not target.exists():
        return 0

    cutoff = datetime.now(tz=UTC) - timedelta(days=days)
    keep: list[str] = []
    with target.open("r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                ts = datetime.fromisoformat(obj["ts"].replace("Z", "+00:00"))
            except (ValueError, KeyError, TypeError):
                # Drop malformed lines.
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            if ts >= cutoff:
                keep.append(line)

    tmp = target.with_suffix(target.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for line in keep:
            fh.write(line + "\n")
    _platform.secure_chmod(tmp, 0o600)
    os.replace(tmp, target)
    _platform.secure_chmod(target, 0o600)
    return len(keep)


# ---------------------------------------------------------------------------
# Aggregate stats (`python -m schwab_marketdata_mcp.stats`)
# ---------------------------------------------------------------------------


def aggregate_stats(
    *,
    window_days: int,
    path: Path | None = None,
) -> dict[str, Any]:
    """Aggregate ``usage.jsonl`` over the last ``window_days`` days."""
    if window_days < 1:
        raise ValueError("window_days must be >= 1")
    target = path or usage_path()
    if not target.exists():
        return {
            "window_days": window_days,
            "count": 0,
            "by_tool": {},
            "by_status": {},
            "by_error_class": {},
            "p50_latency_ms": None,
            "p95_latency_ms": None,
            "distinct_days": 0,
        }
    cutoff = datetime.now(tz=UTC) - timedelta(days=window_days)
    by_tool: Counter[str] = Counter()
    by_status: Counter[str] = Counter()
    by_error: Counter[str] = Counter()
    latencies: list[int] = []
    days: set[str] = set()
    count = 0
    with target.open("r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                ts = datetime.fromisoformat(obj["ts"].replace("Z", "+00:00"))
            except (ValueError, KeyError, TypeError):
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            if ts < cutoff:
                continue
            count += 1
            by_tool[str(obj.get("tool", "?"))] += 1
            by_status[str(obj.get("status", "?"))] += 1
            ec = obj.get("error_class")
            if ec:
                by_error[str(ec)] += 1
            lat = obj.get("latency_ms")
            if isinstance(lat, (int, float)):
                latencies.append(int(lat))
            days.add(ts.date().isoformat())

    p50 = int(median(latencies)) if latencies else None
    p95 = int(_percentile(latencies, 95)) if latencies else None
    return {
        "window_days": window_days,
        "count": count,
        "by_tool": dict(by_tool),
        "by_status": dict(by_status),
        "by_error_class": dict(by_error),
        "p50_latency_ms": p50,
        "p95_latency_ms": p95,
        "distinct_days": len(days),
    }


def _percentile(values: list[int], pct: float) -> float:
    if not values:
        return 0.0
    if not 0 < pct <= 100:
        raise ValueError("pct must be in (0, 100]")
    s = sorted(values)
    if len(s) == 1:
        return float(s[0])
    k = (len(s) - 1) * (pct / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return float(s[f])
    return s[f] + (s[c] - s[f]) * (k - f)


def recent_error_count_24h(path: Path | None = None) -> int:
    """Count of error rows in the last 24 hours.  Used by ``health_check``."""
    return int(aggregate_stats(window_days=1, path=path)["by_status"].get("err", 0))


def cli_main(argv: list[str] | None = None) -> int:
    """``python -m schwab_marketdata_mcp.stats`` entry point."""
    import argparse
    import sys

    p = argparse.ArgumentParser(prog="schwab_marketdata_mcp.stats")
    p.add_argument("--window-days", type=int, default=7)
    p.add_argument("--json", action="store_true", help="emit JSON instead of human text")
    args = p.parse_args(argv)
    stats = aggregate_stats(window_days=args.window_days)
    out = sys.stdout
    if args.json:
        out.write(json.dumps(stats, indent=2, sort_keys=True) + "\n")
        return 0
    out.write(f"== last {stats['window_days']}d ==\n")
    out.write(f"count: {stats['count']}  distinct_days: {stats['distinct_days']}\n")
    out.write(f"p50: {stats['p50_latency_ms']}ms  p95: {stats['p95_latency_ms']}ms\n")
    out.write("by_tool:\n")
    for tool, c in sorted(stats["by_tool"].items(), key=lambda kv: -kv[1]):
        out.write(f"  {tool}: {c}\n")
    out.write("by_status:\n")
    for k, c in stats["by_status"].items():
        out.write(f"  {k}: {c}\n")
    if stats["by_error_class"]:
        out.write("by_error_class:\n")
        for k, c in stats["by_error_class"].items():
            out.write(f"  {k}: {c}\n")
    return 0


__all__ = [
    "DEFAULT_RETENTION_DAYS",
    "USAGE_FILE_NAME",
    "aggregate_stats",
    "cli_main",
    "recent_error_count_24h",
    "record",
    "time_tool",
    "truncate_to_window",
    "usage_path",
]
