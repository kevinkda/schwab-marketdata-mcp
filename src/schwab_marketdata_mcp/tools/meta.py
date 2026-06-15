"""Meta tools: ``health_check``, ``get_server_info``, ``get_cache_stats``.

Plan §3.1 / §3.4.2 / v0.2 sprint task #2 — these are local-only.  They
never touch the Schwab API so they remain available even when
``token.json`` is missing/expired/etc.
"""

from __future__ import annotations

import os
from datetime import UTC
from typing import Any

import mcp
import schwab

from .. import metrics
from ..cache import cache_enabled, get_cache
from ..metrics import recent_error_count_24h
from ..models import supported_tool_names
from ..security import (
    TokenState,
    check_token_file_state,
    resolve_token_path,
)

# Error rate thresholds for the health_check state machine (Sprint v0.3
# observability candidate E).  Documented in docs/SLO.md.
DEGRADED_ERROR_RATE_THRESHOLD: float = 0.05
UNHEALTHY_ERROR_RATE_THRESHOLD: float = 0.20


def _safe_token_state() -> tuple[TokenState, dict[str, Any] | None]:
    """Return ``(state, parsed_or_None)`` without raising."""
    try:
        token_path = resolve_token_path(None)
    except Exception:
        return TokenState.MISSING, None
    return check_token_file_state(token_path)


def _safe_cache_summary() -> dict[str, Any]:
    """Best-effort cache stats; never raises into the caller.

    Returns ``{enabled, backend, entries}`` — the minimal surface
    ``health_check`` needs.  If the cache cannot be constructed we still
    return a summary with ``enabled`` honest and the fields zeroed.
    """
    if not cache_enabled():
        return {"enabled": False, "backend": None, "entries": 0}
    cache = get_cache()
    if cache is None:
        return {"enabled": False, "backend": None, "entries": 0}
    try:
        stats = cache.get_stats()
    except Exception:
        return {"enabled": True, "backend": None, "entries": 0}
    return {
        "enabled": stats.enabled,
        "backend": stats.backend,
        "entries": stats.entries,
    }


async def health_check_impl() -> dict[str, Any]:
    """Local health probe — never calls Schwab; safe even with no token."""
    state, _parsed = _safe_token_state()
    token_age_days: float | None = None
    expires_in_days: float | None = None
    if state is TokenState.VALID:
        # Best-effort age estimate via mtime — we deliberately avoid
        # constructing a real Client here (would require app key+secret and
        # fail if they aren't set).  See plan §3.2.2 fall-back.
        try:
            from datetime import datetime

            from ..security import resolve_token_path

            tp = resolve_token_path(None)
            mtime = tp.stat().st_mtime
            age_seconds = datetime.now(tz=UTC).timestamp() - mtime
            token_age_days = round(age_seconds / 86400, 3)
            expires_in_days = round(7 - token_age_days, 3)
        except OSError:
            pass

    cache_summary = _safe_cache_summary()
    err_window = metrics.recent_errors_window(window_minutes=5, last_n=5)
    error_rate = float(err_window["error_rate"])
    overall_status = _classify_overall_status(state, error_rate)
    return {
        "server_version": _SERVER_VERSION,
        "overall_status": overall_status,
        "token_state": state.value,
        "token_age_days": token_age_days,
        "token_expires_in_days": expires_in_days,
        "last_request_status": "unknown",  # filled in once we add a counter
        "rate_limit_remaining_per_min": _rate_limit_budget(),
        "recent_error_count_24h": recent_error_count_24h(),
        "error_rate_5min": error_rate,
        "error_count_5min": int(err_window["error_count"]),
        "total_calls_5min": int(err_window["total_calls"]),
        "last_errors": err_window["last_errors"],
        "platform_supported": True,
        "cache_enabled": cache_summary["enabled"],
        "cache_backend": cache_summary["backend"],
        "cache_entries": cache_summary["entries"],
    }


def _classify_overall_status(token_state: TokenState, error_rate: float) -> str:
    """Collapse signals to ``healthy`` / ``degraded`` / ``unhealthy``.

    Rules (mirrors docs/SLO.md):

    * ``unhealthy`` if the token is expired/missing/malformed/insecure or
      the 5-minute error rate exceeds 20%.
    * ``degraded`` if the 5-minute error rate is between 5% and 20%.
    * ``healthy`` otherwise.
    """
    unhealthy_token_states = {
        TokenState.MISSING,
        TokenState.MALFORMED,
        TokenState.INSECURE_PERMS,
    }
    if token_state in unhealthy_token_states:
        return "unhealthy"
    if error_rate > UNHEALTHY_ERROR_RATE_THRESHOLD:
        return "unhealthy"
    if error_rate > DEGRADED_ERROR_RATE_THRESHOLD:
        return "degraded"
    return "healthy"


def _rate_limit_budget() -> int:
    raw = os.environ.get("SCHWAB_RATE_LIMIT_PER_MIN")
    if raw is None or raw == "":
        return 120
    try:
        return max(0, int(raw))
    except ValueError:
        return 120


# Captured at import time so health_check stays offline-safe.
_SERVER_VERSION: str | None = None


async def get_server_info_impl(*, server_version: str) -> dict[str, Any]:
    """Local server metadata — version + tool list.  Never calls Schwab."""
    global _SERVER_VERSION
    _SERVER_VERSION = server_version
    return {
        "server_version": server_version,
        "mcp_sdk_version": getattr(mcp, "__version__", "unknown"),
        "schwab_py_version": getattr(schwab, "version", None) and getattr(schwab.version, "version", "unknown"),
        "supported_tools": supported_tool_names(),
        "platform_supported_v1": ["macos>=11", "linux"],
    }


async def get_cache_stats_impl() -> dict[str, Any]:
    """Local cache backend health — never calls Schwab.

    Returns ``{backend, enabled, entries}`` so an LLM agent can reason
    about cache effectiveness before deciding whether to bypass.

    .. versionchanged:: 0.5.0
        The DuckDB-specific ``db_path`` / ``size_mb`` / ``rows_per_table`` /
        ``expired_rows`` / ``hit_rate_24h`` / ``hits_24h`` / ``misses_24h`` /
        ``hourly_breakdown_24h`` fields are replaced by ``backend`` (active
        backend name) + ``entries`` (live response-cache entry count).
    """
    cache = get_cache()
    if cache is None:
        return {
            "backend": None,
            "enabled": False,
            "entries": 0,
        }
    try:
        return cache.get_stats().to_dict()
    except Exception as exc:  # pragma: no cover - cache stats must never break tools
        return {
            "backend": cache.backend.name,
            "enabled": True,
            "entries": 0,
            "error": type(exc).__name__,
        }


__all__ = ["get_cache_stats_impl", "get_server_info_impl", "health_check_impl"]
