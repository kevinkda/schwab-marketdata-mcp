"""``python -m schwab_marketdata_mcp.stats`` entry point.

Plan §3.4.3.  Thin shim — all logic lives in :func:`metrics.cli_main`.
"""

from __future__ import annotations

from .metrics import cli_main

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(cli_main())
