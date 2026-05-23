"""Bounded-snapshot streaming tool (plan section 10 follow-up).

Opens a Schwab Streamer WebSocket connection, collects messages for the
caller-supplied duration (default 2 s, hard-bounded 500 ms - 10 s), then
disconnects and returns aggregated per-symbol snapshots.

The MCP protocol is request-response by construction; long-running
WebSocket subscriptions are intentionally **out of scope** for this
server (plan section 10).  The bounded-snapshot model is a deliberate
compromise that gives an LLM agent near-real-time bid/ask/last or
1-minute candles without restructuring the transport into a daemon.

Design notes:

* schwab-py's :class:`schwab.streaming.StreamClient` is NOT exposed as
  ``client.stream``; we construct it explicitly and pass the underlying
  schwab-py async client.
* Handlers run inside the :meth:`handle_message` dispatch loop and
  receive each ``data`` element with field names already relabeled via
  ``_BaseFieldEnum.relabel_message`` (so ``BID_PRICE``, ``OPEN_PRICE``
  etc. are dict keys, not numeric strings).
* We always call :meth:`logout` in ``finally`` so a torn-down socket
  cannot leak the websocket task.  ``logout`` failures are swallowed at
  WARNING level - we already have the snapshot the caller asked for.
* Every error path maps to a structured ``Schwab*Error`` so the MCP
  server's error normalization layer surfaces it as a dict, not an
  exception that crashes the JSON-RPC frame.

Coverage target: best-effort.  ``handle_message`` and the websocket
internals live inside schwab-py and are mocked in unit tests; we do not
chase 100% on the integration glue here because faithful mocks of
async websocket dispatch are noisy and add little signal.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, Final

from ..client import make_client
from ..errors import SchwabAuthError, SchwabError, SchwabTransientError
from ..models import (
    DEFAULT_STREAMING_SNAPSHOT_DURATION_MS,
    GetStreamingSnapshotInput,
)

log = logging.getLogger(__name__)

#: Service value used by Pydantic; must match StreamClient request strings.
_SERVICE_LEVELONE_EQUITIES: Final = "LEVELONE_EQUITIES"
_SERVICE_CHART_EQUITY: Final = "CHART_EQUITY"


def _make_stream_client(client: Any) -> Any:
    """Construct a :class:`schwab.streaming.StreamClient` for ``client``.

    Isolated so unit tests can monkeypatch the import boundary cleanly.
    """
    from schwab.streaming import StreamClient

    return StreamClient(client)


def _utcnow() -> datetime:
    """Wall clock, hoisted so tests can monkeypatch deterministically."""
    return datetime.now(tz=UTC)


def _extract_levelone(content: dict[str, Any], ts_iso: str) -> dict[str, Any]:
    """Pluck the L1 quote fields we expose to the agent.

    Handler dispatch already relabeled numeric field IDs to enum names
    (see ``StreamClient.LevelOneEquityFields``).  We deliberately keep
    the exposed surface narrow - bid / ask / last / volume - because
    that's what the agent actually uses for a quick "is the trade still
    on" check; full L1 frames carry 50+ fields most of which are
    redundant under a 2-second window.
    """
    return {
        "ts": ts_iso,
        "bid": content.get("BID_PRICE"),
        "ask": content.get("ASK_PRICE"),
        "last": content.get("LAST_PRICE"),
        "volume": content.get("TOTAL_VOLUME"),
    }


def _extract_chart(content: dict[str, Any], ts_iso: str) -> dict[str, Any]:
    """Pluck the 1-minute candle fields from a CHART_EQUITY frame."""
    return {
        "ts": ts_iso,
        "open": content.get("OPEN_PRICE"),
        "high": content.get("HIGH_PRICE"),
        "low": content.get("LOW_PRICE"),
        "close": content.get("CLOSE_PRICE"),
        "volume": content.get("VOLUME"),
        # schwab-py renames numeric field 7 to ``CHART_TIME_MILLIS``;
        # we preserve it under a friendlier key so chart consumers don't
        # need to reach into the streamer's raw schema.
        "chart_time_millis": content.get("CHART_TIME_MILLIS"),
    }


async def _drain_until_deadline(
    streamer: Any,
    deadline: float,
) -> None:
    """Loop ``handle_message`` until the wall-clock deadline.

    schwab-py's websocket recv blocks; we wrap each recv in a short
    timeout so the deadline is honored even when the server stops
    sending frames (low-volume pre-market, halted stock, etc.).
    """
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            return
        try:
            await asyncio.wait_for(streamer.handle_message(), timeout=remaining)
        except TimeoutError:
            return
        except Exception:
            # An exception while draining is usually a closed socket or a
            # transient network blip; we record the partial snapshot
            # rather than failing the whole tool call.
            log.warning(
                '{"event":"streaming_drain_error","msg":"handle_message raised; returning partial snapshot"}',
                exc_info=True,
            )
            return


async def get_streaming_snapshot_impl(
    args: GetStreamingSnapshotInput,
) -> dict[str, Any]:
    """Open a Schwab Streamer connection, collect for ``duration_ms``, return.

    Returns a dict shaped as:

    .. code-block:: text

        {
          "service": "LEVELONE_EQUITIES" | "CHART_EQUITY",
          "symbols_requested": [...],
          "symbols_received":  [...],          # subset that produced >=1 frame
          "duration_ms":       int,            # what we asked for (after defaults)
          "messages_count":    int,            # # of frames the streamer dispatched
          "snapshots":         {sym: [{...}, ...]},
          "metadata": {
              "first_message_at": str | None,  # ISO 8601 UTC
              "last_message_at":  str | None,
              "connection_duration_ms": int,
          }
        }
    """
    duration = args.duration_ms if args.duration_ms is not None else DEFAULT_STREAMING_SNAPSHOT_DURATION_MS
    symbols = list(args.symbols)
    snapshots: dict[str, list[dict[str, Any]]] = {sym: [] for sym in symbols}
    messages_count = 0
    first_at: datetime | None = None
    last_at: datetime | None = None

    def _handle(msg: dict[str, Any]) -> None:
        # ``msg`` is a single ``data`` element with relabeled fields.
        nonlocal messages_count, first_at, last_at
        content_list = msg.get("content") or []
        if not isinstance(content_list, list):
            return
        now = _utcnow()
        ts_iso = now.isoformat()
        recorded_any = False
        for content in content_list:
            if not isinstance(content, dict):
                continue
            sym = content.get("key") or content.get("SYMBOL") or content.get("symbol")
            if sym not in snapshots:
                continue
            if args.service == _SERVICE_LEVELONE_EQUITIES:
                snapshots[sym].append(_extract_levelone(content, ts_iso))
            else:
                snapshots[sym].append(_extract_chart(content, ts_iso))
            recorded_any = True
        if recorded_any:
            messages_count += 1
            if first_at is None:
                first_at = now
            last_at = now

    try:
        client = make_client()
    except SchwabError:
        # Token state machine already raised the most actionable error.
        raise

    streamer = _make_stream_client(client)

    connection_start = _utcnow()
    logged_in = False
    try:
        try:
            await streamer.login()
            logged_in = True
        except SchwabAuthError:
            raise
        except Exception as exc:
            raise SchwabTransientError(
                status_code=0,
                attempt=0,
                hint=f"streamer login failed: {type(exc).__name__}",
            ) from exc

        if args.service == _SERVICE_LEVELONE_EQUITIES:
            streamer.add_level_one_equity_handler(_handle)
            await streamer.level_one_equity_subs(symbols)
        else:
            streamer.add_chart_equity_handler(_handle)
            await streamer.chart_equity_subs(symbols)

        deadline = asyncio.get_event_loop().time() + duration / 1000.0
        await _drain_until_deadline(streamer, deadline)
    finally:
        if logged_in:
            try:
                await streamer.logout()
            except Exception:  # pragma: no cover - defensive log-only path
                log.warning(
                    '{"event":"streaming_logout_error","msg":"streamer.logout '
                    'raised; ignoring (snapshot already captured)"}',
                    exc_info=True,
                )

    connection_end = _utcnow()
    return {
        "service": args.service,
        "symbols_requested": symbols,
        "symbols_received": [sym for sym in symbols if snapshots[sym]],
        "duration_ms": duration,
        "messages_count": messages_count,
        "snapshots": snapshots,
        "metadata": {
            "first_message_at": first_at.isoformat() if first_at is not None else None,
            "last_message_at": last_at.isoformat() if last_at is not None else None,
            "connection_duration_ms": int((connection_end - connection_start).total_seconds() * 1000),
        },
    }


__all__ = ["get_streaming_snapshot_impl"]
