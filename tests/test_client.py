"""``client.py`` deep tests — retry, bucket, FakeSchwabClient, factories."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from schwab_marketdata_mcp import client
from schwab_marketdata_mcp.errors import (
    SchwabAuthError,
    SchwabRateLimitError,
    SchwabTransientError,
)

# asyncio_mode='auto' in pyproject takes care of marking async funcs.
# Sync tests stay un-marked.


# ---------------------------------------------------------------------------
# TokenBucket additional cases
# ---------------------------------------------------------------------------


async def test_bucket_remaining_advisory_only() -> None:
    b = client.TokenBucket(capacity=2, window_seconds=10)
    await b.try_acquire()
    assert b.remaining() == 1


# ---------------------------------------------------------------------------
# RetryPolicy.from_env / env_int
# ---------------------------------------------------------------------------


def test_retry_policy_from_env_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCHWAB_MAX_RETRIES", raising=False)
    p = client.RetryPolicy.from_env()
    assert p.max_429 == client.DEFAULT_MAX_RETRIES_429
    assert p.max_5xx == client.DEFAULT_MAX_RETRIES_5XX


def test_retry_policy_from_env_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCHWAB_MAX_RETRIES", "0")
    p = client.RetryPolicy.from_env()
    assert p.max_429 == 0 and p.max_5xx == 0


def test_env_int_invalid_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCHWAB_RATE_LIMIT_PER_MIN", "not-a-number")
    assert client._env_int("SCHWAB_RATE_LIMIT_PER_MIN", 7) == 7


def test_env_int_unset_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCHWAB_RATE_LIMIT_PER_MIN", raising=False)
    assert client._env_int("SCHWAB_RATE_LIMIT_PER_MIN", 99) == 99


def test_retry_after_seconds_parsing() -> None:
    class R:
        headers = {"Retry-After": "12"}

    assert client._retry_after_seconds(R()) == 12

    class R2:
        headers = {"retry-after": "not-a-number"}

    assert client._retry_after_seconds(R2()) is None

    class R3:
        headers = {}

    assert client._retry_after_seconds(R3()) is None


# ---------------------------------------------------------------------------
# RateLimitedClient.call — full retry decision tree
# ---------------------------------------------------------------------------


class _Stub:
    """Minimal stub that returns a configurable sequence of outcomes."""

    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    async def go(self) -> Any:
        self.calls += 1
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def _http_status_error(code: int, retry_after: str | None = None) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://fake.local/")
    headers = {"Retry-After": retry_after} if retry_after else {}
    resp = httpx.Response(code, request=req, headers=headers)
    return httpx.HTTPStatusError(f"status {code}", request=req, response=resp)


async def _build_client(*, capacity: int = 100, max_429: int = 2, max_5xx: int = 3) -> client.RateLimitedClient:
    fake = client.FakeSchwabClient(fixtures_dir=Path(__file__).resolve().parent / "fixtures")
    return client.RateLimitedClient(
        fake,
        bucket=client.TokenBucket(capacity=capacity),
        retry=client.RetryPolicy(max_429=max_429, max_5xx=max_5xx, backoff_base=0.0),
    )


async def test_call_401_translates_to_auth_error() -> None:
    c = await _build_client()
    stub = _Stub([_http_status_error(401)])
    with pytest.raises(SchwabAuthError) as ei:
        await c.call(stub.go, tool_name="t")
    assert ei.value.reason == "access_token_invalid"


async def test_call_429_retry_then_success() -> None:
    c = await _build_client(max_429=2)
    stub = _Stub([_http_status_error(429, "0"), {"ok": 1}])
    out = await c.call(stub.go, tool_name="t")
    assert out == {"ok": 1}


async def test_call_429_exhaust() -> None:
    c = await _build_client(max_429=1)
    stub = _Stub([_http_status_error(429, "0"), _http_status_error(429, "0")])
    with pytest.raises(SchwabRateLimitError):
        await c.call(stub.go, tool_name="t")


async def test_call_5xx_retry_then_success() -> None:
    c = await _build_client(max_5xx=2)
    stub = _Stub([_http_status_error(503), {"ok": 1}])
    out = await c.call(stub.go, tool_name="t")
    assert out == {"ok": 1}


async def test_call_5xx_exhaust_raises_transient() -> None:
    c = await _build_client(max_5xx=1)
    stub = _Stub([_http_status_error(503), _http_status_error(503)])
    with pytest.raises(SchwabTransientError) as ei:
        await c.call(stub.go, tool_name="t")
    assert ei.value.status_code == 503


async def test_call_other_4xx_non_retryable() -> None:
    c = await _build_client()
    stub = _Stub([_http_status_error(400)])
    with pytest.raises(SchwabTransientError) as ei:
        await c.call(stub.go, tool_name="t")
    assert ei.value.status_code == 400


async def test_call_network_retry_then_success() -> None:
    c = await _build_client(max_5xx=2)
    stub = _Stub([httpx.ConnectError("boom"), {"ok": 1}])
    out = await c.call(stub.go, tool_name="t")
    assert out == {"ok": 1}


async def test_call_network_exhaust() -> None:
    c = await _build_client(max_5xx=1)
    stub = _Stub([httpx.ReadTimeout("slow"), httpx.WriteTimeout("slow"), httpx.ReadTimeout("slow")])
    with pytest.raises(SchwabTransientError):
        await c.call(stub.go, tool_name="t")


async def test_slot_warns_low_remaining(caplog: pytest.LogCaptureFixture) -> None:
    """Warning fires when bucket falls under the warn threshold."""
    c = await _build_client(capacity=client.RATE_LIMIT_WARN_THRESHOLD)
    caplog.set_level("WARNING", logger="schwab_marketdata_mcp.client")
    stub = _Stub([{"ok": 1}])
    await c.call(stub.go, tool_name="t")
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "rate_limit_warning" in msgs


# ---------------------------------------------------------------------------
# FakeSchwabClient — every method + missing fixture path
# ---------------------------------------------------------------------------


async def test_fake_client_missing_fixture(tmp_path: Path) -> None:
    fake = client.FakeSchwabClient(fixtures_dir=tmp_path)
    with pytest.raises(FileNotFoundError):
        await fake.get_quote("AAPL")


async def test_fake_client_all_methods_with_seed_fixtures(tmp_path: Path) -> None:
    fdir = tmp_path / "fx"
    fdir.mkdir()
    for name in (
        "get_quote",
        "get_quotes",
        "get_price_history",
        "get_option_chain",
        "get_option_expiration_chain",
        "get_market_hours",
        "get_movers",
        "search_instruments",
        "get_instrument_by_cusip",
    ):
        (fdir / f"{name}_normal.json").write_text("{}")
    fake = client.FakeSchwabClient(fixtures_dir=fdir)
    assert (await fake.get_quote("AAPL")).status_code == 200
    assert (await fake.get_quotes(["AAPL"])).status_code == 200
    assert (await fake.get_price_history("AAPL")).status_code == 200
    assert (await fake.get_option_chain("AAPL")).status_code == 200
    assert (await fake.get_option_expiration_chain("AAPL")).status_code == 200
    assert (await fake.get_market_hours(["EQUITY"])).status_code == 200
    assert (await fake.get_movers("NASDAQ")).status_code == 200
    assert (await fake.get_instruments(["AAPL"], "SYMBOL_SEARCH")).status_code == 200
    assert (await fake.get_instrument_by_cusip("037833100")).status_code == 200
    age = fake.token_age()
    assert age.total_seconds() > 0


async def test_fake_response_raise_for_status_4xx() -> None:
    r = client._FakeResponse(401, {"err": 1})
    with pytest.raises(httpx.HTTPStatusError):
        r.raise_for_status()


async def test_fake_response_raise_for_status_2xx_noop() -> None:
    r = client._FakeResponse(200, {"ok": 1})
    r.raise_for_status()


async def test_fake_client_scenario_via_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fdir = tmp_path / "fx"
    fdir.mkdir()
    (fdir / "get_quote_auth_error.json").write_text("{}")
    (fdir / "get_quote_rate_limit.json").write_text("{}")
    (fdir / "get_quote_5xx.json").write_text("{}")
    fake = client.FakeSchwabClient(fixtures_dir=fdir)

    monkeypatch.setenv("SCHWAB_MOCK_SCENARIO", "auth_error")
    r = await fake.get_quote("AAPL")
    assert r.status_code == 401

    monkeypatch.setenv("SCHWAB_MOCK_SCENARIO", "rate_limit")
    r = await fake.get_quote("AAPL")
    assert r.status_code == 429

    monkeypatch.setenv("SCHWAB_MOCK_SCENARIO", "5xx")
    r = await fake.get_quote("AAPL")
    assert r.status_code == 503


# ---------------------------------------------------------------------------
# make_client / make_rate_limited factory branches
# ---------------------------------------------------------------------------


def test_make_client_fixtures_backend(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SCHWAB_MOCK_BACKEND", "fixtures")
    monkeypatch.setenv("SCHWAB_MOCK_FIXTURES_DIR", str(tmp_path))
    out = client.make_client()
    assert isinstance(out, client.FakeSchwabClient)


def test_make_rate_limited_uses_env_capacity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SCHWAB_MOCK_BACKEND", "fixtures")
    monkeypatch.setenv("SCHWAB_MOCK_FIXTURES_DIR", str(tmp_path))
    monkeypatch.setenv("SCHWAB_RATE_LIMIT_PER_MIN", "5")
    rl = client.make_rate_limited()
    assert rl._bucket.capacity == 5


def test_make_client_credential_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real backend with no creds → SchwabAuthError(reason='credential_missing')."""
    monkeypatch.delenv("SCHWAB_MOCK_BACKEND", raising=False)
    monkeypatch.delenv("SCHWAB_APP_KEY", raising=False)
    monkeypatch.delenv("SCHWAB_APP_SECRET", raising=False)
    # token state is MISSING (XDG isolated) → enforce_or_raise short-circuits first.
    with pytest.raises(SchwabAuthError) as ei:
        client.make_client()
    assert ei.value.reason in ("token_not_initialized", "credential_missing")


def test_make_client_token_state_branches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cover MISSING, INSECURE_PERMS, MALFORMED branches in _enforce_token_or_raise."""
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.delenv("SCHWAB_MOCK_BACKEND", raising=False)
    pdir = state / "schwab-marketdata-mcp"
    pdir.mkdir(exist_ok=True)
    os.chmod(pdir, 0o700)
    f = pdir / "token.json"

    # MISSING
    with pytest.raises(SchwabAuthError) as ei:
        client.make_client()
    assert ei.value.reason == "token_not_initialized"

    # MALFORMED (perms must be 0o600 first to pass that check)
    f.write_text("not-json")
    os.chmod(f, 0o600)
    with pytest.raises(SchwabAuthError) as ei:
        client.make_client()
    assert ei.value.reason == "token_corrupted"

    # INSECURE_PERMS (perms wrong → checked BEFORE json.load)
    os.chmod(f, 0o644)
    with pytest.raises(SchwabAuthError) as ei:
        client.make_client()
    assert ei.value.reason == "insecure_token_perms"


def test_to_dict_branches() -> None:
    """``_to_dict`` covers list/dict/scalar/raw paths."""
    from schwab_marketdata_mcp.tools._runtime import _to_dict

    class R:
        def json(self) -> Any:
            return [1, 2, 3]

    out = _to_dict(R())
    assert out == {"items": [1, 2, 3]}

    class S:
        def json(self) -> Any:
            return "hello"

    assert _to_dict(S()) == {"value": "hello"}

    class T:
        text = "raw text"

        def json(self) -> Any:
            raise ValueError("bad json")

    assert _to_dict(T()) == {"raw": "raw text"}

    assert _to_dict({"already": "dict"}) == {"already": "dict"}


def test_to_dict_no_json_method() -> None:
    """Object without .json() falls into the 'value' branch."""
    from schwab_marketdata_mcp.tools._runtime import _to_dict

    assert _to_dict("plain") == {"value": "plain"}


# ---------------------------------------------------------------------------
# User-Agent injection (Schwab Dashboard "Device Type" classifier)
# ---------------------------------------------------------------------------


def test_user_agent_format() -> None:
    """USER_AGENT must include all three identifying tokens and no PII."""
    ua = client.USER_AGENT
    # Identifies our MCP server so Schwab Dashboard shows a recognisable name.
    assert ua.startswith("schwab-marketdata-mcp/")
    # Carries enough version triage info for Schwab support, and nothing else.
    assert "python/" in ua
    assert "schwab-py/" in ua
    # Must NOT leak any developer-specific identifier.
    forbidden = (
        os.environ.get("USER", ""),
        os.environ.get("HOSTNAME", ""),
        os.environ.get("HOME", ""),
    )
    for token in forbidden:
        if token:  # only guard real values; CI may not set these
            assert token not in ua, f"User-Agent leaked env token: {token!r}"


def test_inject_user_agent_sets_header() -> None:
    """_inject_user_agent must mutate session.headers['User-Agent']."""
    from schwab_marketdata_mcp.client import _inject_user_agent  # type: ignore[attr-defined]

    class _Session:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}

    class _SchwabLike:
        def __init__(self) -> None:
            self.session = _Session()

    sc = _SchwabLike()
    _inject_user_agent(sc)
    assert sc.session.headers["User-Agent"] == client.USER_AGENT


def test_inject_user_agent_missing_session_is_safe() -> None:
    """A schwab client without ``.session`` must not raise (forward-compat)."""
    from schwab_marketdata_mcp.client import _inject_user_agent  # type: ignore[attr-defined]

    class _NoSession:
        pass

    _inject_user_agent(_NoSession())  # must not raise


def test_inject_user_agent_missing_headers_is_safe() -> None:
    """Session without ``.headers`` must not raise (forward-compat)."""
    from schwab_marketdata_mcp.client import _inject_user_agent  # type: ignore[attr-defined]

    class _BareSession:
        pass

    class _SchwabLike:
        session = _BareSession()

    _inject_user_agent(_SchwabLike())  # must not raise


def test_inject_user_agent_swallows_attribute_errors() -> None:
    """A pathological session whose headers raise on assignment must not break tools."""
    from schwab_marketdata_mcp.client import _inject_user_agent  # type: ignore[attr-defined]

    class _ExplodingHeaders:
        def __setitem__(self, key: str, value: str) -> None:
            raise RuntimeError("simulated header set failure")

    class _Session:
        headers = _ExplodingHeaders()

    class _SchwabLike:
        session = _Session()

    _inject_user_agent(_SchwabLike())  # must not raise


async def test_user_agent_propagates_through_httpx_session() -> None:
    """End-to-end: a real httpx.AsyncClient with our UA emits it on every request.

    Documents the integration contract: the way we mutate
    ``session.headers["User-Agent"]`` is the documented httpx path for
    outbound UA control, and respx confirms the header lands on the wire.
    """
    import httpx
    import respx

    async with respx.mock(base_url="https://api.schwabapi.com") as router:
        route = router.get("/marketdata/v1/quotes/AAPL/quotes").respond(200, json={"AAPL": {"symbol": "AAPL"}})
        async with httpx.AsyncClient(base_url="https://api.schwabapi.com") as session:
            session.headers["User-Agent"] = client.USER_AGENT
            resp = await session.get("/marketdata/v1/quotes/AAPL/quotes")
            assert resp.status_code == 200
        sent_ua = route.calls.last.request.headers.get("User-Agent")
        assert sent_ua is not None
        assert "schwab-marketdata-mcp/" in sent_ua
