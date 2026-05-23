"""Async wrapper for the Schwab Market Data client.

Plan §3.2.4 / §6.2 — wraps :func:`schwab.auth.easy_client` with:

* a sliding-window **token-bucket** limiter that does **not** hold a slot
  across retry sleeps (so a 429 retry does not block other concurrent
  tools);
* an exponential-backoff retry policy honouring ``Retry-After``;
* a ``SchwabClientProtocol`` indirection so tests can substitute a
  :class:`FakeSchwabClient` reading hand-crafted JSON fixtures.

All public entry points are ``async def`` to match
``easy_client(asyncio=True)``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import sys
import time
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol, cast

import httpx

from . import _platform
from .errors import (
    SchwabAuthError,
    SchwabRateLimitError,
    SchwabTransientError,
)
from .security import (
    TokenState,
    check_token_file_state,
    enforce_token_perms,
    insecure_perms_hint,
    resolve_token_path,
    token_file_lock,
)

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration knobs
# ---------------------------------------------------------------------------

DEFAULT_RATE_LIMIT_PER_MIN: Final[int] = 120
DEFAULT_MAX_RETRIES_429: Final[int] = 2
DEFAULT_MAX_RETRIES_5XX: Final[int] = 3
DEFAULT_BACKOFF_BASE_SEC: Final[float] = 0.5
RATE_LIMIT_WARN_THRESHOLD: Final[int] = 20  # tokens remaining → emit warning


def _build_user_agent() -> str:
    """Build a Schwab-Dashboard-recognisable User-Agent string.

    Format: ``schwab-marketdata-mcp/<our-version> python/<py-ver> schwab-py/<lib-ver>``

    The Schwab Developer Portal "Device Type" classifier inspects the
    outbound User-Agent header.  Without an explicit UA, schwab-py inherits
    httpx's generic default (``python-httpx/<ver>``), which the Dashboard
    classifies as ``Unknown``.  Sending a stable, app-identifying UA flips
    it to a recognisable label and gives Schwab's abuse-detection a clean
    fingerprint for this MCP server.

    Security: the UA is intentionally **public** — it carries only
    package versions, never credentials, hostname, username, or token
    state.  PII-free by construction.
    """
    from . import __version__ as _our_version

    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    try:
        import schwab as _schwab

        schwab_ver = getattr(_schwab, "__version__", "unknown")
    except Exception:  # pragma: no cover - defensive: schwab not installed in some test envs
        schwab_ver = "unknown"
    return f"schwab-marketdata-mcp/{_our_version} python/{py_ver} schwab-py/{schwab_ver}"


#: Module-level constant so tests can assert on it without re-deriving.
USER_AGENT: Final[str] = _build_user_agent()


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Token bucket (sliding-window, retry-friendly)
# ---------------------------------------------------------------------------


@dataclass
class TokenBucket:
    """Sliding-window token bucket.

    Each successful ``acquire`` consumes a slot; the slot is automatically
    released after ``window_seconds`` of wall time.  Retries inside the
    client **do not** hold a slot across sleep, so other concurrent tools
    can proceed.
    """

    capacity: int
    window_seconds: float = 60.0
    _slots: deque[float] = field(default_factory=deque)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def try_acquire(self) -> bool:
        """Non-blocking; ``True`` if a token was granted."""
        async with self._lock:
            self._evict_expired()
            if len(self._slots) >= self.capacity:
                return False
            self._slots.append(time.monotonic())
            return True

    def remaining(self) -> int:
        # Snapshot — caller doesn't need the lock for this advisory value.
        self._evict_expired()
        return max(0, self.capacity - len(self._slots))

    def _evict_expired(self) -> None:
        now = time.monotonic()
        while self._slots and now - self._slots[0] >= self.window_seconds:
            self._slots.popleft()


# ---------------------------------------------------------------------------
# SchwabClientProtocol — the abstraction layer between tools and backends
# ---------------------------------------------------------------------------


class SchwabClientProtocol(Protocol):
    """Subset of ``schwab.client.Client`` used by the 10 business tools.

    Every method must be ``async`` (matches ``easy_client(asyncio=True)``)
    and return an ``httpx.Response``-compatible object exposing
    ``.status_code``, ``.json()``, and ``.headers``.
    """

    async def get_quote(self, symbol: str, *, fields: Any = None) -> Any: ...
    async def get_quotes(self, symbols: list[str], *, fields: Any = None, indicative: Any = None) -> Any: ...
    async def get_price_history(self, symbol: str, **kwargs: Any) -> Any: ...
    async def get_option_chain(self, symbol: str, **kwargs: Any) -> Any: ...
    async def get_option_expiration_chain(self, symbol: str) -> Any: ...
    async def get_market_hours(self, markets: list[str], *, date: Any = None) -> Any: ...
    async def get_movers(self, index: str, *, sort_order: Any = None, frequency: Any = None) -> Any: ...
    async def get_instruments(self, symbols: list[str], projection: Any) -> Any: ...
    async def get_instrument_by_cusip(self, cusip: str) -> Any: ...
    def token_age(self) -> Any: ...


# ---------------------------------------------------------------------------
# Retry decorator — wraps the protocol methods at call time
# ---------------------------------------------------------------------------


@dataclass
class RetryPolicy:
    """Tunable retry policy.  ``SCHWAB_MAX_RETRIES=0`` disables all retries."""

    max_429: int = DEFAULT_MAX_RETRIES_429
    max_5xx: int = DEFAULT_MAX_RETRIES_5XX
    backoff_base: float = DEFAULT_BACKOFF_BASE_SEC

    @classmethod
    def from_env(cls) -> RetryPolicy:
        override = os.environ.get("SCHWAB_MAX_RETRIES")
        if override == "0":
            return cls(max_429=0, max_5xx=0)
        return cls()


def _retry_after_seconds(resp: Any) -> int | None:
    """Best-effort parse of an HTTP ``Retry-After`` header (seconds only)."""
    try:
        headers = getattr(resp, "headers", {}) or {}
        raw = headers.get("Retry-After") or headers.get("retry-after")
    except Exception:
        return None
    if raw is None:
        return None
    try:
        return max(0, int(float(raw)))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# RateLimitedClient — wraps a SchwabClientProtocol with bucket + retry
# ---------------------------------------------------------------------------


class RateLimitedClient:
    """Adds token-bucket limiting + retry/backoff around any backend.

    Usage:

        async with RateLimitedClient.from_env() as client:
            data = await client.call(client.inner.get_quote, "AAPL")

    The ``call`` helper centralises the limiter + retry policy so individual
    tool modules do not duplicate the logic.
    """

    def __init__(
        self,
        inner: SchwabClientProtocol,
        *,
        bucket: TokenBucket,
        retry: RetryPolicy,
    ) -> None:
        self.inner = inner
        self._bucket = bucket
        self._retry = retry

    @classmethod
    def from_env(cls, inner: SchwabClientProtocol) -> RateLimitedClient:
        cap = _env_int("SCHWAB_RATE_LIMIT_PER_MIN", DEFAULT_RATE_LIMIT_PER_MIN)
        return cls(
            inner,
            bucket=TokenBucket(capacity=max(1, cap)),
            retry=RetryPolicy.from_env(),
        )

    @asynccontextmanager
    async def _slot(self) -> Any:
        """Reserve a bucket slot for the duration of one network call.

        Released as soon as the call returns (success **or** error) — retry
        sleep happens **outside** this context manager so the next concurrent
        caller can reuse the slot.  This is the key plan §3.2.4 invariant.
        """
        ok = await self._bucket.try_acquire()
        if not ok:
            used = self._bucket.capacity - self._bucket.remaining()
            raise SchwabRateLimitError(
                retry_after_seconds=1,
                current_window_used=used,
            )
        remaining = self._bucket.remaining()
        if remaining <= RATE_LIMIT_WARN_THRESHOLD:
            log.warning('{"event":"rate_limit_warning","remaining":%d}', remaining)
        yield

    async def call(
        self,
        coro_factory: Callable[..., Awaitable[Any]],
        *args: Any,
        tool_name: str = "?",
        **kwargs: Any,
    ) -> Any:
        """Invoke ``coro_factory(*args, **kwargs)`` with limiter + retries."""
        attempt_429 = 0
        attempt_5xx = 0
        while True:
            try:
                async with self._slot():
                    return await coro_factory(*args, **kwargs)
            except SchwabRateLimitError:
                # Bucket full locally — bubble up; agent decides what to do.
                raise
            except httpx.HTTPStatusError as exc:
                resp = exc.response
                code = resp.status_code
                if code == 401:
                    raise SchwabAuthError(
                        reason="access_token_invalid",
                        hint=(
                            "Schwab returned 401; access token is rejected. "
                            "If this persists, the refresh token has expired — "
                            "run: uv run python -m schwab_marketdata_mcp.auth login_flow"
                        ),
                    ) from None
                if code == 429:
                    if attempt_429 >= self._retry.max_429:
                        retry_after = _retry_after_seconds(resp) or 60
                        raise SchwabRateLimitError(
                            retry_after_seconds=retry_after,
                            current_window_used=self._bucket.capacity - self._bucket.remaining(),
                        ) from None
                    delay = _retry_after_seconds(resp)
                    if delay is None:
                        delay = int(self._retry.backoff_base * (2**attempt_429) + random.random() * 0.25)
                    attempt_429 += 1
                    log.warning(
                        '{"event":"retry","tool":"%s","attempt":%d,"reason":"429","wait_s":%d}',
                        tool_name,
                        attempt_429,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                if 500 <= code < 600:
                    if attempt_5xx >= self._retry.max_5xx:
                        raise SchwabTransientError(
                            status_code=code,
                            attempt=attempt_5xx,
                            hint=f"Schwab returned {code}; retries exhausted",
                        ) from None
                    delay = self._retry.backoff_base * (2**attempt_5xx) + random.random() * 0.25
                    attempt_5xx += 1
                    log.warning(
                        '{"event":"retry","tool":"%s","attempt":%d,"reason":"5xx","status":%d}',
                        tool_name,
                        attempt_5xx,
                        code,
                    )
                    await asyncio.sleep(delay)
                    continue
                # 4xx other than 401/429 — non-retryable; surface as transient
                # with a clear hint.  We deliberately don't include the body.
                raise SchwabTransientError(
                    status_code=code,
                    attempt=0,
                    hint=f"Schwab returned {code} (non-retryable)",
                ) from None
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout) as exc:
                if attempt_5xx >= self._retry.max_5xx:
                    raise SchwabTransientError(
                        status_code=0,
                        attempt=attempt_5xx,
                        hint=f"network error after {attempt_5xx} attempts: {type(exc).__name__}",
                    ) from None
                delay = self._retry.backoff_base * (2**attempt_5xx) + random.random() * 0.25
                attempt_5xx += 1
                log.warning(
                    '{"event":"retry","tool":"%s","attempt":%d,"reason":"network","exc":"%s"}',
                    tool_name,
                    attempt_5xx,
                    type(exc).__name__,
                )
                await asyncio.sleep(delay)
                continue


# ---------------------------------------------------------------------------
# FakeSchwabClient — fixture-driven stand-in for integration / unit tests
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Minimal ``httpx.Response``-shaped object."""

    def __init__(
        self,
        status_code: int,
        body: Any,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}

    def json(self) -> Any:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            req = httpx.Request("GET", "https://fake.local/")
            resp = httpx.Response(self.status_code, request=req, headers=self.headers)
            raise httpx.HTTPStatusError(f"fixture status {self.status_code}", request=req, response=resp)


class FakeSchwabClient:
    """Reads hand-crafted JSON fixtures from disk.

    Plan §6.2 — the integration test suite drives the stdio server with
    ``SCHWAB_MOCK_BACKEND=fixtures``; this class is the resulting backend.
    """

    def __init__(self, fixtures_dir: Path, scenario: str = "normal") -> None:
        self.fixtures_dir = fixtures_dir
        self.scenario = scenario

    def _fixture_path(self, tool: str) -> Path:
        scenario = os.environ.get("SCHWAB_MOCK_SCENARIO", self.scenario)
        # Try scenario-qualified file first, then plain {tool}.json.
        for stem in (f"{tool}_{scenario}", tool):
            for root in (self.fixtures_dir, self.fixtures_dir / "seed"):
                candidate = root / f"{stem}.json"
                if candidate.exists():
                    return candidate
        raise FileNotFoundError(f"no fixture found for tool={tool!r} scenario={scenario!r} under {self.fixtures_dir!s}")

    def _load(self, tool: str) -> Any:
        import json

        path = self._fixture_path(tool)
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    def _scenario_response(self, tool: str) -> _FakeResponse:
        scenario = os.environ.get("SCHWAB_MOCK_SCENARIO", self.scenario)
        if scenario == "auth_error":
            return _FakeResponse(401, self._load(tool), headers={})
        if scenario == "rate_limit":
            return _FakeResponse(429, self._load(tool), headers={"Retry-After": "1"})
        if scenario == "5xx":
            return _FakeResponse(503, self._load(tool), headers={})
        return _FakeResponse(200, self._load(tool))

    # ---- 10 business methods ------------------------------------------------

    async def get_quote(self, symbol: str, *, fields: Any = None) -> _FakeResponse:
        del symbol, fields
        return self._scenario_response("get_quote")

    async def get_quotes(self, symbols: list[str], *, fields: Any = None, indicative: Any = None) -> _FakeResponse:
        del symbols, fields, indicative
        return self._scenario_response("get_quotes")

    async def get_price_history(self, symbol: str, **kwargs: Any) -> _FakeResponse:
        del symbol, kwargs
        return self._scenario_response("get_price_history")

    async def get_option_chain(self, symbol: str, **kwargs: Any) -> _FakeResponse:
        del symbol, kwargs
        return self._scenario_response("get_option_chain")

    async def get_option_expiration_chain(self, symbol: str) -> _FakeResponse:
        del symbol
        return self._scenario_response("get_option_expiration_chain")

    async def get_market_hours(self, markets: list[str], *, date: Any = None) -> _FakeResponse:
        del markets, date
        return self._scenario_response("get_market_hours")

    async def get_movers(self, index: str, *, sort_order: Any = None, frequency: Any = None) -> _FakeResponse:
        del index, sort_order, frequency
        return self._scenario_response("get_movers")

    async def get_instruments(self, symbols: list[str], projection: Any) -> _FakeResponse:
        del symbols, projection
        return self._scenario_response("search_instruments")

    async def get_instrument_by_cusip(self, cusip: str) -> _FakeResponse:
        del cusip
        return self._scenario_response("get_instrument_by_cusip")

    def token_age(self) -> Any:
        from datetime import timedelta

        return timedelta(hours=1)


# ---------------------------------------------------------------------------
# Factory — applies token state machine before letting tools through
# ---------------------------------------------------------------------------


def _enforce_token_or_raise(token_path: Path) -> None:
    """Apply plan §3.2.2.1 state machine and raise structured error if bad."""
    state, _ = check_token_file_state(token_path)
    if state is TokenState.MISSING:
        raise SchwabAuthError(
            reason="token_not_initialized",
            hint=("No token file found.  Run: uv run python -m schwab_marketdata_mcp.auth login_flow"),
        )
    if state is TokenState.INSECURE_PERMS:
        actual = _platform.file_mode(token_path)
        raise SchwabAuthError(
            reason="insecure_token_perms",
            hint=insecure_perms_hint(token_path, actual),
        )
    if state is TokenState.MALFORMED:
        raise SchwabAuthError(
            reason="token_corrupted",
            hint="token.json failed to parse.  Back it up and re-run auth login_flow.",
        )
    enforce_token_perms(token_path)


def _make_real_client(token_path: Path) -> SchwabClientProtocol:
    """Instantiate a real schwab-py async client."""
    api_key = os.environ.get("SCHWAB_APP_KEY")
    app_secret = os.environ.get("SCHWAB_APP_SECRET")
    callback = os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8182")
    if not api_key or not app_secret:
        raise SchwabAuthError(
            reason="credential_missing",
            hint=(
                "SCHWAB_APP_KEY / SCHWAB_APP_SECRET are not set.  "
                "Copy .env.example to .env and fill in real values from "
                "https://developer.schwab.com/dashboard/apps"
            ),
        )
    from schwab.auth import easy_client

    with token_file_lock(token_path):
        # easy_client may refresh the token; we serialise across processes.
        client = easy_client(
            api_key=api_key,
            app_secret=app_secret,
            callback_url=callback,
            token_path=str(token_path),
            asyncio=True,
            enforce_enums=True,
            interactive=False,
        )
    _inject_user_agent(client)
    return cast(SchwabClientProtocol, client)


def _inject_user_agent(schwab_client: Any) -> None:
    """Pin a stable, identifying User-Agent on the schwab-py session.

    schwab-py (1.5.x) does not expose a ``user_agent`` constructor parameter
    (see ``schwab.auth.easy_client`` → ``client_from_token_file``), but it
    stores its ``AsyncOAuth2Client`` (a subclass of ``httpx.AsyncClient``)
    on ``client.session``.  Setting ``session.headers["User-Agent"]`` on a
    live ``httpx.Client`` is the documented path for outbound UA control —
    every subsequent request inherits the merged client headers.

    Failure here must never break the data path: if the schwab-py internals
    move ``session`` (or rename ``headers``) in a future release, we log at
    DEBUG and fall back to the library default UA.
    """
    try:
        session = getattr(schwab_client, "session", None)
        if session is None:
            return
        headers = getattr(session, "headers", None)
        if headers is None:
            return
        headers["User-Agent"] = USER_AGENT
    except Exception:  # pragma: no cover - defensive: never break tools on UA injection
        log.debug("failed to inject User-Agent header on schwab session", exc_info=True)


def make_client(token_path_arg: str | None = None) -> SchwabClientProtocol:
    """Construct the appropriate backend per environment.

    * ``SCHWAB_MOCK_BACKEND=fixtures`` → :class:`FakeSchwabClient`
    * else → real schwab-py ``easy_client`` (after token state-machine check)
    """
    if os.environ.get("SCHWAB_MOCK_BACKEND") == "fixtures":
        # Fixtures live under ``tests/fixtures`` relative to the repo root.
        # Tests can override via SCHWAB_MOCK_FIXTURES_DIR.
        fixtures_dir = Path(
            os.environ.get(
                "SCHWAB_MOCK_FIXTURES_DIR",
                str(Path(__file__).resolve().parent.parent.parent / "tests" / "fixtures"),
            )
        )
        scenario = os.environ.get("SCHWAB_MOCK_SCENARIO", "normal")
        return FakeSchwabClient(fixtures_dir=fixtures_dir, scenario=scenario)

    token_path = resolve_token_path(token_path_arg)
    _enforce_token_or_raise(token_path)
    return _make_real_client(token_path)


def make_rate_limited(token_path_arg: str | None = None) -> RateLimitedClient:
    """High-level constructor used by :mod:`server`."""
    return RateLimitedClient.from_env(make_client(token_path_arg))


__all__ = [
    "DEFAULT_MAX_RETRIES_5XX",
    "DEFAULT_MAX_RETRIES_429",
    "DEFAULT_RATE_LIMIT_PER_MIN",
    "USER_AGENT",
    "FakeSchwabClient",
    "RateLimitedClient",
    "RetryPolicy",
    "SchwabClientProtocol",
    "TokenBucket",
    "make_client",
    "make_rate_limited",
]
