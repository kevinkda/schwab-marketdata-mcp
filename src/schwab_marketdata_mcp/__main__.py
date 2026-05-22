"""``python -m schwab_marketdata_mcp`` entry — runs the stdio server.

Excluded from coverage (see ``[tool.coverage.run].omit`` in pyproject.toml).
"""

from __future__ import annotations

from .server import main

if __name__ == "__main__":  # pragma: no cover
    main()
