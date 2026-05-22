"""Shared runtime helper for the 7 tool modules.

Each business tool needs:

1. A lazily-constructed :class:`RateLimitedClient` (so tests can override
   the backend via env) — created **once per server process**.
2. A small wrapper that runs ``raise_for_status()`` on the schwab-py
   response so HTTP errors surface as :class:`httpx.HTTPStatusError`, which
   the rate-limited client translates into our structured exceptions.
3. ``metrics.time_tool`` to record latency / status.

This module centralises that boilerplate so each tool file is just three
lines of business logic.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from ..client import RateLimitedClient, make_rate_limited
from ..metrics import time_tool

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


async def call_endpoint(
    tool_name: str,
    fetch: Callable[[Any], Awaitable[Any]],
) -> dict[str, Any]:
    """Run ``fetch(client.inner)`` under metrics + retry + raise_for_status.

    ``fetch`` receives the raw inner backend (real schwab-py client or
    :class:`FakeSchwabClient`) and must return the raw response object —
    we call ``raise_for_status`` here.
    """
    client = await get_client()

    async def _wrapped() -> Any:
        resp = await fetch(client.inner)
        # schwab-py async client returns httpx.Response; FakeSchwabClient
        # returns the lookalike shim — both expose .raise_for_status().
        if hasattr(resp, "raise_for_status"):
            resp.raise_for_status()
        return resp

    with time_tool(tool_name):
        resp = await client.call(_wrapped, tool_name=tool_name)
    return _to_dict(resp)


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


__all__ = ["call_endpoint", "get_client", "reset_client_cache"]
