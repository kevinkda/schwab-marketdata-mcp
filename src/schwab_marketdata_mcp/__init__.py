"""Schwab Market Data Production MCP Server.

A Model Context Protocol (MCP) server exposing 12 tools that wrap the 10
Schwab Market Data Production endpoints plus 2 meta tools (``health_check``
and ``get_server_info``).

Public modules:
    - :mod:`schwab_marketdata_mcp.server` — FastMCP entry point.
    - :mod:`schwab_marketdata_mcp.client` — async Schwab client wrapper.
    - :mod:`schwab_marketdata_mcp.auth_logic` — testable OAuth helpers.
    - :mod:`schwab_marketdata_mcp.security` — path / permission / lock helpers.
    - :mod:`schwab_marketdata_mcp.errors` — structured exception hierarchy.
    - :mod:`schwab_marketdata_mcp.models` — Pydantic v2 input schemas.
    - :mod:`schwab_marketdata_mcp.metrics` — usage.jsonl recorder + stats CLI.
    - :mod:`schwab_marketdata_mcp.health` — token health probe CLI.

Platform: macOS 11+ / Linux only (uses ``fcntl.flock``). Windows native is
not supported in v1.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
