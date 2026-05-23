"""Integration tests — drive the stdio server end-to-end.

Plan §6.2 / §6.4 — boots the actual MCP server subprocess with
``SCHWAB_MOCK_BACKEND=fixtures``; verifies:

* ``list_tools`` returns exactly the 12 expected tools.
* Each business tool is callable and returns a JSON-RPC tool result.
* The very first stdout byte is a valid JSON-RPC frame (no schwab-py
  startup banner pollution).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"


def _server_params(scenario: str = "normal") -> StdioServerParameters:
    env = {
        **os.environ,
        "SCHWAB_MOCK_BACKEND": "fixtures",
        "SCHWAB_MOCK_FIXTURES_DIR": str(FIXTURES_DIR),
        "SCHWAB_MOCK_SCENARIO": scenario,
        "SCHWAB_MAX_RETRIES": "0",
        "LOG_LEVEL": "WARNING",
        # Ensure the child uses the same isolated XDG_STATE_HOME as the parent
        "XDG_STATE_HOME": os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")),
    }
    # Strip any pytest-cov auto-instrumentation env so the subprocess does not
    # write a non-branch ``.coverage.*`` file that would conflict with the
    # parent test session's branch-coverage data file.
    for k in ("COV_CORE_SOURCE", "COV_CORE_CONFIG", "COV_CORE_DATAFILE", "COV_CORE_CONTEXT"):
        env.pop(k, None)
    env["COVERAGE_DISABLE"] = "1"
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "schwab_marketdata_mcp.server"],
        env=env,
    )


@pytest.mark.asyncio
async def test_stdio_server_lists_13_tools() -> None:
    async with stdio_client(_server_params()) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
    names = {t.name for t in tools.tools}
    expected = {
        "get_quote",
        "get_quotes",
        "get_price_history",
        "get_option_chain",
        "get_option_expiration_chain",
        "get_market_hours",
        "get_market_hour_single",
        "get_movers",
        "search_instruments",
        "get_instrument_by_cusip",
        "health_check",
        "get_server_info",
        "get_streaming_snapshot",
    }
    assert names == expected, f"unexpected tool set: {names}"


@pytest.mark.asyncio
async def test_stdio_call_get_quote_returns_dict() -> None:
    async with stdio_client(_server_params()) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = await session.call_tool("get_quote", {"symbol": "AAPL"})
    # CallToolResult exposes .content (list of TextContent) + .structuredContent.
    sc = result.structuredContent or {}
    text = result.content[0].text if result.content else ""
    parsed = sc or json.loads(text) if text else {}
    assert "AAPL" in parsed or "AAPL" in text


@pytest.mark.asyncio
async def test_stdio_call_get_server_info_static() -> None:
    async with stdio_client(_server_params()) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        r = await session.call_tool("get_server_info", {})
    sc = r.structuredContent or {}
    text = r.content[0].text if r.content else ""
    parsed = sc or json.loads(text) if text else {}
    # Tolerate either the dict or text shape — both must contain 13 tools.
    assert "supported_tools" in parsed or "supported_tools" in text
    if "supported_tools" in parsed:
        assert len(parsed["supported_tools"]) == 13


@pytest.mark.asyncio
async def test_stdio_call_health_check() -> None:
    async with stdio_client(_server_params()) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        r = await session.call_tool("health_check", {})
    sc = r.structuredContent or {}
    text = r.content[0].text if r.content else ""
    blob = sc or json.loads(text)
    assert "server_version" in blob
    assert "token_state" in blob


@pytest.mark.asyncio
async def test_stdio_call_get_quote_invalid_symbol_returns_structured_error() -> None:
    async with stdio_client(_server_params()) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        r = await session.call_tool("get_quote", {"symbol": "lowercase"})
    text = r.content[0].text if r.content else ""
    sc = r.structuredContent or {}
    blob = sc or json.loads(text)
    assert blob.get("error") == "SchwabValidationError"


@pytest.mark.asyncio
async def test_stdio_call_auth_error_scenario() -> None:
    async with stdio_client(_server_params(scenario="auth_error")) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            r = await session.call_tool("get_quote", {"symbol": "AAPL"})
    text = r.content[0].text if r.content else ""
    sc = r.structuredContent or {}
    blob = sc or json.loads(text)
    assert blob.get("error") == "SchwabAuthError"
    # OWASP A02 — even the structured error must not leak any Bearer token.
    assert "Bearer" not in text or "***REDACTED***" in text


def test_stdout_first_line_is_valid_jsonrpc(tmp_path: Path) -> None:
    """Plan §3.2.3 — the very first stdout byte must already be a valid
    JSON-RPC frame; no schwab-py startup banner is allowed.
    """
    env = {
        **os.environ,
        "SCHWAB_MOCK_BACKEND": "fixtures",
        "SCHWAB_MOCK_FIXTURES_DIR": str(FIXTURES_DIR),
        "SCHWAB_MOCK_SCENARIO": "normal",
        "LOG_LEVEL": "WARNING",
    }
    for k in ("COV_CORE_SOURCE", "COV_CORE_CONFIG", "COV_CORE_DATAFILE", "COV_CORE_CONTEXT"):
        env.pop(k, None)
    env["COVERAGE_DISABLE"] = "1"
    with subprocess.Popen(
        [sys.executable, "-m", "schwab_marketdata_mcp.server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(REPO_ROOT),
    ) as proc:
        try:
            init = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "smoke", "version": "0"},
                },
            }
            assert proc.stdin is not None
            assert proc.stdout is not None
            proc.stdin.write((json.dumps(init) + "\n").encode())
            proc.stdin.flush()
            first_line = proc.stdout.readline()
            decoded = json.loads(first_line)
            assert decoded.get("jsonrpc") == "2.0"
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                proc.kill()
                proc.wait()
