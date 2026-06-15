"""Exception-path security suite (batch 2).

Exercises every error-normalisation path so that:
* 401 / 429 / 5xx / timeout / malformed-body all map to a structured
  ``Schwab*Error`` (never a raw httpx exception across the MCP frame);
* exception messages never embed secrets;
* best-effort layers (cache / metrics) swallow their own errors and let the
  tool path continue.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from schwab_marketdata_mcp import client
from schwab_marketdata_mcp.errors import (
    SchwabAuthError,
    SchwabRateLimitError,
    SchwabTransientError,
)
from tests.conftest import make_clickhouse_cache


def _http_error(status: int, headers: dict[str, str] | None = None) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://api.schwabapi.com/marketdata/v1/quotes")
    resp = httpx.Response(status, headers=headers or {}, request=req)
    return httpx.HTTPStatusError(f"{status}", request=req, response=resp)


async def _instant_sleep(_s: Any) -> None:
    return None


# ---------------------------------------------------------------------------
# 401 — authentication failure → SchwabAuthError
# ---------------------------------------------------------------------------


async def test_exc_401_maps_to_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 401 from Schwab normalises to SchwabAuthError with an actionable hint."""
    monkeypatch.setattr(client.asyncio, "sleep", _instant_sleep)

    async def _fetch() -> Any:
        raise _http_error(401)

    rl = client.RateLimitedClient.from_env(MagicMock())
    with pytest.raises(SchwabAuthError) as ei:
        await rl.call(_fetch, tool_name="get_quote")
    assert ei.value.reason == "access_token_invalid"
    # The hint must not echo any token material.
    assert "Bearer" not in ei.value.hint


# ---------------------------------------------------------------------------
# 429 — rate limit → retries then SchwabRateLimitError
# ---------------------------------------------------------------------------


async def test_exc_429_retries_then_raises_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A persistent 429 exhausts retries and raises SchwabRateLimitError."""
    monkeypatch.setattr(client.asyncio, "sleep", _instant_sleep)
    monkeypatch.setenv("SCHWAB_MAX_RETRIES", "0")  # no retries → immediate raise

    async def _fetch() -> Any:
        raise _http_error(429, {"Retry-After": "30"})

    rl = client.RateLimitedClient.from_env(MagicMock())
    with pytest.raises(SchwabRateLimitError) as ei:
        await rl.call(_fetch, tool_name="get_quote")
    assert ei.value.retry_after_seconds == 30


async def test_exc_429_recovers_after_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transient 429 that clears on retry returns the eventual success."""
    monkeypatch.setattr(client.asyncio, "sleep", _instant_sleep)
    calls = {"n": 0}

    async def _fetch() -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(429, {"Retry-After": "1"})
        return {"ok": True}

    rl = client.RateLimitedClient.from_env(MagicMock())
    out = await rl.call(_fetch, tool_name="get_quote")
    assert out == {"ok": True}
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# 5xx — server error → retries then SchwabTransientError
# ---------------------------------------------------------------------------


async def test_exc_5xx_exhausts_to_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    """A persistent 503 exhausts retries → SchwabTransientError(status=503)."""
    monkeypatch.setattr(client.asyncio, "sleep", _instant_sleep)
    monkeypatch.setenv("SCHWAB_MAX_RETRIES", "0")

    async def _fetch() -> Any:
        raise _http_error(503)

    rl = client.RateLimitedClient.from_env(MagicMock())
    with pytest.raises(SchwabTransientError) as ei:
        await rl.call(_fetch, tool_name="get_quote")
    assert ei.value.status_code == 503


# ---------------------------------------------------------------------------
# 4xx (non-401/429) — non-retryable → SchwabTransientError
# ---------------------------------------------------------------------------


async def test_exc_404_non_retryable_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 404 is surfaced as a non-retryable transient error (no body leak)."""
    monkeypatch.setattr(client.asyncio, "sleep", _instant_sleep)

    async def _fetch() -> Any:
        raise _http_error(404)

    rl = client.RateLimitedClient.from_env(MagicMock())
    with pytest.raises(SchwabTransientError) as ei:
        await rl.call(_fetch, tool_name="get_quote")
    assert ei.value.status_code == 404
    assert "non-retryable" in ei.value.hint


# ---------------------------------------------------------------------------
# network timeouts → retries then SchwabTransientError
# ---------------------------------------------------------------------------


async def test_exc_timeout_maps_to_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    """Connect/read timeouts exhaust retries → SchwabTransientError."""
    monkeypatch.setattr(client.asyncio, "sleep", _instant_sleep)
    monkeypatch.setenv("SCHWAB_MAX_RETRIES", "0")

    async def _fetch() -> Any:
        raise httpx.ReadTimeout("read timed out")

    rl = client.RateLimitedClient.from_env(MagicMock())
    with pytest.raises(SchwabTransientError):
        await rl.call(_fetch, tool_name="get_quote")


async def test_exc_connect_error_maps_to_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    """A connection error is normalised to a transient error."""
    monkeypatch.setattr(client.asyncio, "sleep", _instant_sleep)
    monkeypatch.setenv("SCHWAB_MAX_RETRIES", "0")

    async def _fetch() -> Any:
        raise httpx.ConnectError("connection refused")

    rl = client.RateLimitedClient.from_env(MagicMock())
    with pytest.raises(SchwabTransientError):
        await rl.call(_fetch, tool_name="get_quote")


# ---------------------------------------------------------------------------
# malformed response body → safe dict, never a crash
# ---------------------------------------------------------------------------


def test_exc_malformed_json_body_yields_raw_fallback() -> None:
    """A response whose .json() raises returns a ``{'raw': ...}`` shape."""
    from schwab_marketdata_mcp.tools import _runtime

    class _BadJson:
        text = "<<not json>>"

        def json(self) -> Any:
            raise ValueError("invalid json")

    out = _runtime._to_dict(_BadJson())
    assert out == {"raw": "<<not json>>"}


# ---------------------------------------------------------------------------
# best-effort layers swallow their own errors
# ---------------------------------------------------------------------------


def test_exc_cache_get_failure_returns_miss() -> None:
    """A backend query error is swallowed → treated as a cache miss."""
    c, client = make_clickhouse_cache()
    client.query.side_effect = RuntimeError("query exploded")
    # Getter swallows the error and returns None (miss) rather than raising.
    assert c.get_quote("AAPL") is None


def test_exc_metrics_write_failure_is_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A metrics write failure must not raise into the tool path."""
    from schwab_marketdata_mcp import metrics

    target = tmp_path / "state" / "usage.jsonl"
    real_mkdir = Path.mkdir

    def _boom_mkdir(self: Path, *a: Any, **k: Any) -> Any:
        if self == target.parent:
            raise OSError("mkdir denied")
        return real_mkdir(self, *a, **k)

    monkeypatch.setattr(Path, "mkdir", _boom_mkdir)
    metrics.record(tool="get_quote", status="ok", error_class=None, latency_ms=1, path=target)
    # No exception propagated; a structured warning went to stderr.
    err = capsys.readouterr().err
    assert "metrics_write_failed" in err


def test_exc_exception_messages_never_leak_secrets() -> None:
    """All structured exception str() outputs are secret-free by construction."""
    errs = [
        SchwabAuthError(reason="refresh_token_expired", hint="Bearer xyz.secret"),
        SchwabRateLimitError(retry_after_seconds=10, current_window_used=120),
        SchwabTransientError(status_code=503, attempt=2, hint="upstream Bearer tok.leak"),
    ]
    for e in errs:
        s = str(e)
        assert "secret" not in s.lower() or "***" in s
        assert "tok.leak" not in s
