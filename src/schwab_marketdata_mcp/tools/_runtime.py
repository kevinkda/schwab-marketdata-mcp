"""Shared runtime helper for the 7 tool modules.

Each business tool needs:

1. A lazily-constructed :class:`RateLimitedClient` (so tests can override
   the backend via env) — created **once per server process**.
2. A small wrapper that runs ``raise_for_status()`` on the schwab-py
   response so HTTP errors surface as :class:`httpx.HTTPStatusError`, which
   the rate-limited client translates into our structured exceptions.
3. ``metrics.time_tool`` to record latency / status.
4. Optional DuckDB cache lookup / store hooks (plan v0.2 sprint task #2).

This module centralises that boilerplate so each tool file is just three
lines of business logic.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from ..cache import Cache, cache_bypass, get_cache
from ..client import RateLimitedClient, make_rate_limited
from ..metrics import time_tool

log = logging.getLogger(__name__)

_lock = asyncio.Lock()
_client: RateLimitedClient | None = None


async def get_client() -> RateLimitedClient:
    """Lazily instantiate the rate-limited client (one per process)."""
    global _client
    if _client is None:
        async with _lock:
            if _client is None:
                _client = make_rate_limited()
    return _client


def reset_client_cache() -> None:
    """Clear the cached client.  Used by integration tests between scenarios."""
    global _client
    _client = None


CacheLookup = Callable[[Cache], "dict[str, Any] | None"]
CacheStore = Callable[[Cache, "dict[str, Any]"], None]


async def call_endpoint(
    tool_name: str,
    fetch: Callable[[Any], Awaitable[Any]],
    *,
    cache_lookup: CacheLookup | None = None,
    cache_store: CacheStore | None = None,
) -> dict[str, Any]:
    """Run ``fetch(client.inner)`` under metrics + retry + raise_for_status.

    ``fetch`` receives the raw inner backend (real schwab-py client or
    :class:`FakeSchwabClient`) and must return the raw response object —
    we call ``raise_for_status`` here.

    When ``cache_lookup`` is provided and returns a non-None dict, we
    short-circuit before touching the rate limiter / API.  When the live
    fetch succeeds and ``cache_store`` is provided, the response dict is
    handed to ``cache_store`` for persistence.

    The cache is bypassed entirely when:
        * ``cache_enabled()`` is False (handled by ``get_cache``);
        * ``cache_bypass()`` is True (single-call force fresh);
        * ``cache_lookup`` returns ``None``.

    The returned dict always carries a ``_cache_status`` field
    (``"hit" | "miss" | "bypass" | "disabled"``) so the metrics layer
    and downstream agents can reason about cache behaviour.
    """
    cache = get_cache() if (cache_lookup is not None or cache_store is not None) else None
    bypass = cache_bypass()
    cache_status = "disabled"

    if cache is not None and not bypass and cache_lookup is not None:
        try:
            hit = cache_lookup(cache)
        except Exception:  # pragma: no cover - cache errors must never break tools
            hit = None
        if isinstance(hit, dict):
            with time_tool(tool_name) as state:
                state["cache_status"] = "hit"
            payload = dict(hit)
            payload["_cache_status"] = "hit"
            return payload

    if cache is not None and bypass:
        cache_status = "bypass"
    elif cache is not None:
        cache_status = "miss"

    client = await get_client()

    async def _wrapped() -> Any:
        resp = await fetch(client.inner)
        # schwab-py async client returns httpx.Response; FakeSchwabClient
        # returns the lookalike shim — both expose .raise_for_status().
        if hasattr(resp, "raise_for_status"):
            resp.raise_for_status()
        return resp

    with time_tool(tool_name) as state:
        state["cache_status"] = cache_status
        resp = await client.call(_wrapped, tool_name=tool_name)
    payload = _to_dict(resp)
    if cache is not None and not bypass and cache_store is not None:
        try:
            cache_store(cache, payload)
        except Exception as exc:  # pragma: no cover - cache errors must never break tools
            log.warning('{"event":"cache_store_failed","error":"%s"}', exc)
    payload["_cache_status"] = cache_status
    return payload


def _to_dict(resp: Any) -> dict[str, Any]:
    """Convert a httpx.Response (or fake) to a JSON-safe dict."""
    if isinstance(resp, dict):
        return resp
    if hasattr(resp, "json"):
        try:
            data = resp.json()
        except (ValueError, httpx.DecodingError):
            return {"raw": getattr(resp, "text", "")}
        if isinstance(data, list):
            return {"items": data}
        if isinstance(data, dict):
            return data
        return {"value": data}
    return {"value": resp}


__all__ = ["CacheLookup", "CacheStore", "call_endpoint", "get_client", "reset_client_cache"]
