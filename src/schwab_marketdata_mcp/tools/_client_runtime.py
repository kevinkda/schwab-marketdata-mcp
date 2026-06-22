"""Client lifecycle management — authentication/connection only.

This module owns the lazily-constructed, process-wide
:class:`RateLimitedClient` singleton. It is intentionally **decoupled from
the cache layer**: authentication and connection management have nothing to
do with response caching, and keeping them in separate modules prevents the
(incorrect) impression that the auth path depends on the cache.

The client is built via :func:`make_rate_limited` →
``schwab.auth.easy_client`` (token state-machine + token.json). No cache
import appears here by design.
"""

from __future__ import annotations

import asyncio

from ..client import RateLimitedClient, make_rate_limited

_lock = asyncio.Lock()
_client: RateLimitedClient | None = None


async def get_client() -> RateLimitedClient:
    """Lazily instantiate the rate-limited client (one per process)."""
    global _client
    if _client is None:
        async with _lock:
            # Double-checked locking.  The False arc of the guard below (a
            # racing coroutine populated `_client` while we waited on the lock)
            # is concurrency-only and is exercised by
            # test_runtime_get_client_double_checked_lock_race, but coverage.py
            # cannot reliably record a branch arc that spans an asyncio task
            # switch, so the arc is excluded with `# pragma: no branch` rather
            # than dropping the 100% gate.
            if _client is None:  # pragma: no branch
                _client = make_rate_limited()
    return _client


def reset_client_cache() -> None:
    """Clear the cached client.  Used by integration tests between scenarios."""
    global _client
    _client = None


__all__ = ["get_client", "reset_client_cache"]
