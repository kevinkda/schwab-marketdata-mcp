"""Unit tests for ``get_streaming_snapshot`` (plan section 10 follow-up).

We deliberately do **not** open a real Schwab WebSocket - those tests
would be slow, flaky, and would burn the user's 7-day refresh-token
clock on every CI run.  Instead we monkeypatch the
``_make_stream_client`` factory in :mod:`schwab_marketdata_mcp.tools.streaming`
to return a ``FakeStreamerClient`` that drives the registered handler
synchronously from a hand-crafted fixture.

Coverage focus:
* All four input boundaries (empty list, > 20 symbols, bad service enum,
  out-of-range duration_ms) come from Pydantic and never reach the
  streamer.
* The happy path returns the documented dict shape.
* messages_count and per-symbol aggregation are correctly derived from
  the handler dispatch.
* The ``finally`` block calls ``logout`` even when the dispatch loop
  raises.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from schwab_marketdata_mcp import server
from schwab_marketdata_mcp.errors import SchwabValidationError
from schwab_marketdata_mcp.models import (
    GetStreamingSnapshotInput,
    validate_tool_input,
)
from schwab_marketdata_mcp.tools import streaming as streaming_mod

ROOT = Path(__file__).resolve().parent
SEED = ROOT / "fixtures" / "seed"


def _load_fixture(name: str) -> dict[str, Any]:
    with (SEED / name).open(encoding="utf-8") as fh:
        return json.load(fh)


class _FakeStreamerClient:
    """Records calls and drives the registered handler from a fixture.

    Mimics the subset of :class:`schwab.streaming.StreamClient` that
    :mod:`tools.streaming` actually invokes:
    ``login`` / ``logout`` / ``add_*_handler`` /
    ``level_one_equity_subs`` / ``chart_equity_subs`` / ``handle_message``.
    """

    def __init__(self, fixture_msgs: list[dict[str, Any]] | None = None) -> None:
        self._fixture_msgs = list(fixture_msgs or [])
        self._handlers: list[Any] = []
        self.login_calls = 0
        self.logout_calls = 0
        self.subs_args: list[Any] = []
        self.handle_calls = 0

    async def login(self) -> None:
        self.login_calls += 1

    async def logout(self) -> None:
        self.logout_calls += 1

    def add_level_one_equity_handler(self, handler: Any) -> None:
        self._handlers.append(handler)

    def add_chart_equity_handler(self, handler: Any) -> None:
        self._handlers.append(handler)

    async def level_one_equity_subs(self, symbols: list[str]) -> None:
        self.subs_args.append(("LEVELONE_EQUITIES", list(symbols)))

    async def chart_equity_subs(self, symbols: list[str]) -> None:
        self.subs_args.append(("CHART_EQUITY", list(symbols)))

    async def handle_message(self) -> None:
        self.handle_calls += 1
        if self._fixture_msgs:
            msg = self._fixture_msgs.pop(0)
            for h in self._handlers:
                h(msg)
            return
        # Once the fixture is exhausted we sleep so the deadline-driver
        # times out cleanly rather than tight-looping.
        await asyncio.sleep(0.1)


@pytest.fixture
def _patched_stream_client(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Return a callable that installs a ``_FakeStreamerClient`` instance.

    Tests obtain the underlying fake by calling the returned factory; this
    pattern lets each test customize the fixture messages and assert on
    side effects after the tool returns.
    """

    instances: list[_FakeStreamerClient] = []

    def _install(fixture_msgs: list[dict[str, Any]] | None = None) -> _FakeStreamerClient:
        fake = _FakeStreamerClient(fixture_msgs)
        instances.append(fake)

        def _factory(_client: Any) -> _FakeStreamerClient:
            return fake

        monkeypatch.setattr(streaming_mod, "_make_stream_client", _factory)
        # ``make_client`` may dive into the real OAuth state machine; the
        # FakeSchwabClient backend short-circuits that without needing
        # real creds (conftest._no_real_creds set the placeholder env).
        monkeypatch.setenv("SCHWAB_MOCK_BACKEND", "fixtures")
        return fake

    return _install


# ---------------------------------------------------------------------------
# 1-4: Pydantic input validation (no streamer call)
# ---------------------------------------------------------------------------


def test_validates_symbols_max_20() -> None:
    raw = {
        "symbols": [f"SYM{i:03d}" for i in range(21)],
        "service": "LEVELONE_EQUITIES",
    }
    with pytest.raises(SchwabValidationError) as exc:
        validate_tool_input("get_streaming_snapshot", raw)
    assert "symbols" in exc.value.field


def test_validates_duration_ms_bounds() -> None:
    for bad in (0, 100, 499, 10_001, 60_000):
        with pytest.raises(SchwabValidationError) as exc:
            validate_tool_input(
                "get_streaming_snapshot",
                {
                    "symbols": ["VOO"],
                    "service": "LEVELONE_EQUITIES",
                    "duration_ms": bad,
                },
            )
        assert "duration_ms" in exc.value.field


def test_validates_service_enum() -> None:
    with pytest.raises(SchwabValidationError) as exc:
        validate_tool_input(
            "get_streaming_snapshot",
            {"symbols": ["VOO"], "service": "BAD_SERVICE"},
        )
    assert "service" in exc.value.field


def test_validates_symbols_not_empty() -> None:
    with pytest.raises(SchwabValidationError) as exc:
        validate_tool_input(
            "get_streaming_snapshot",
            {"symbols": [], "service": "LEVELONE_EQUITIES"},
        )
    assert "symbols" in exc.value.field


def test_validates_lowercase_symbol_rejected() -> None:
    """Layered StockSymbol regex must reject lowercase."""
    with pytest.raises(SchwabValidationError):
        validate_tool_input(
            "get_streaming_snapshot",
            {"symbols": ["voo"], "service": "LEVELONE_EQUITIES"},
        )


# ---------------------------------------------------------------------------
# 5-8: Behavioural tests with the fake streamer
# ---------------------------------------------------------------------------


async def test_returns_correct_shape(_patched_stream_client: Any) -> None:
    fake = _patched_stream_client([_load_fixture("streaming_snapshot_levelone_normal.json")])
    args = GetStreamingSnapshotInput(
        symbols=["VOO", "QQQ"],
        service="LEVELONE_EQUITIES",
        duration_ms=500,
    )
    result = await streaming_mod.get_streaming_snapshot_impl(args)

    assert result["service"] == "LEVELONE_EQUITIES"
    assert result["symbols_requested"] == ["VOO", "QQQ"]
    assert set(result["symbols_received"]) == {"VOO", "QQQ"}
    assert result["duration_ms"] == 500
    assert result["messages_count"] == 1
    assert "snapshots" in result
    voo_frames = result["snapshots"]["VOO"]
    assert len(voo_frames) == 1
    assert voo_frames[0]["bid"] == 685.0
    assert voo_frames[0]["ask"] == 685.19
    assert voo_frames[0]["last"] == 685.18
    assert voo_frames[0]["volume"] == 1234567
    meta = result["metadata"]
    assert meta["first_message_at"] is not None
    assert meta["last_message_at"] is not None
    assert isinstance(meta["connection_duration_ms"], int)
    assert fake.login_calls == 1
    assert fake.logout_calls == 1
    assert fake.subs_args == [("LEVELONE_EQUITIES", ["VOO", "QQQ"])]


async def test_chart_equity_returns_candle_fields(_patched_stream_client: Any) -> None:
    fake = _patched_stream_client([_load_fixture("streaming_snapshot_chart_normal.json")])
    args = GetStreamingSnapshotInput(
        symbols=["VOO", "QQQ"],
        service="CHART_EQUITY",
        duration_ms=500,
    )
    result = await streaming_mod.get_streaming_snapshot_impl(args)

    assert result["service"] == "CHART_EQUITY"
    voo = result["snapshots"]["VOO"][0]
    assert voo["open"] == 685.0
    assert voo["high"] == 685.42
    assert voo["low"] == 684.91
    assert voo["close"] == 685.18
    assert voo["volume"] == 12034
    assert voo["chart_time_millis"] == 1716470400000
    assert fake.subs_args == [("CHART_EQUITY", ["VOO", "QQQ"])]


async def test_no_messages_returns_empty_snapshots(_patched_stream_client: Any) -> None:
    fake = _patched_stream_client([])
    args = GetStreamingSnapshotInput(
        symbols=["VOO"],
        service="LEVELONE_EQUITIES",
        duration_ms=500,
    )
    result = await streaming_mod.get_streaming_snapshot_impl(args)

    assert result["messages_count"] == 0
    assert result["symbols_received"] == []
    assert result["snapshots"] == {"VOO": []}
    assert result["metadata"]["first_message_at"] is None
    assert result["metadata"]["last_message_at"] is None
    # We still drained at least once and disconnected cleanly.
    assert fake.logout_calls == 1


async def test_messages_aggregated_per_symbol(_patched_stream_client: Any) -> None:
    msg1 = _load_fixture("streaming_snapshot_levelone_normal.json")
    msg2 = _load_fixture("streaming_snapshot_levelone_normal.json")
    # Tweak msg2 so it is clearly distinguishable.
    for entry in msg2["content"]:
        entry["LAST_PRICE"] = entry["LAST_PRICE"] + 1.0
    fake = _patched_stream_client([msg1, msg2])
    args = GetStreamingSnapshotInput(
        symbols=["VOO", "QQQ"],
        service="LEVELONE_EQUITIES",
        duration_ms=500,
    )
    result = await streaming_mod.get_streaming_snapshot_impl(args)

    assert result["messages_count"] == 2
    voo = result["snapshots"]["VOO"]
    qqq = result["snapshots"]["QQQ"]
    assert len(voo) == 2
    assert len(qqq) == 2
    assert voo[1]["last"] == voo[0]["last"] + 1.0
    assert qqq[1]["last"] == qqq[0]["last"] + 1.0
    assert fake.handle_calls >= 2


async def test_disconnects_even_on_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the dispatch loop raises, ``logout`` must still run.

    schwab-py's recv-loop occasionally raises CancelledError or
    ConnectionClosed; the tool must not leak the websocket.
    """

    class _BoomStreamer(_FakeStreamerClient):
        async def handle_message(self) -> None:
            raise RuntimeError("simulated socket reset")

    fake = _BoomStreamer()
    monkeypatch.setattr(streaming_mod, "_make_stream_client", lambda _client: fake)
    monkeypatch.setenv("SCHWAB_MOCK_BACKEND", "fixtures")

    args = GetStreamingSnapshotInput(
        symbols=["VOO"],
        service="LEVELONE_EQUITIES",
        duration_ms=500,
    )
    # The drain loop catches the exception and returns the partial
    # snapshot; logout must still have been called.
    result = await streaming_mod.get_streaming_snapshot_impl(args)
    assert result["messages_count"] == 0
    assert fake.login_calls == 1
    assert fake.logout_calls == 1


# ---------------------------------------------------------------------------
# Server-level smoke (validates the registered tool can be called via
# the server module without surfacing exceptions across the MCP boundary).
# ---------------------------------------------------------------------------


async def test_server_get_streaming_snapshot_invokes_impl(
    _patched_stream_client: Any,
) -> None:
    _patched_stream_client([_load_fixture("streaming_snapshot_levelone_normal.json")])
    out = await server.get_streaming_snapshot_(
        symbols=["VOO", "QQQ"],
        service="LEVELONE_EQUITIES",
        duration_ms=500,
    )
    assert out["service"] == "LEVELONE_EQUITIES"
    assert "VOO" in out["snapshots"]


async def test_server_get_streaming_snapshot_validation_error_returned_as_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid input must surface as ``{"error": "SchwabValidationError"}``,
    not raise across the MCP JSON-RPC frame.
    """
    monkeypatch.setenv("SCHWAB_MOCK_BACKEND", "fixtures")
    out = await server.get_streaming_snapshot_(
        symbols=["voo"],  # lowercase rejected by StockSymbol regex
        service="LEVELONE_EQUITIES",
    )
    assert out.get("error") == "SchwabValidationError"
