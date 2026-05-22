"""Process-bootstrap helpers shared by the stdio server and the OAuth CLI.

Plan §3.3 / §3.3.1 — both entry points (``server.main`` and
``auth.cli_main``) need to load credentials from a ``.env`` file *before*
any code path reads ``os.environ["SCHWAB_APP_KEY"]`` etc.  Centralising the
logic here:

* avoids duplicate ``load_dotenv()`` calls drifting out of sync,
* keeps the import-order contract explicit (must run after stdio
  hardening, before any business import that reads ``os.environ``),
* makes the behaviour testable in isolation without spawning a
  subprocess.

Security contract:

* We **only** call :func:`dotenv.load_dotenv` with its default search
  algorithm (cwd → parents) — we never accept a user-supplied path here,
  so there is no path-traversal surface.
* :func:`dotenv.load_dotenv` is a no-op if the package is missing or the
  file does not exist; both are tolerated silently because a
  host-injected env (e.g. Cursor ``mcp.json`` ``envFile``) is the
  recommended path and ``.env`` is only a developer fallback.
* We **never** raise from this helper — any failure must not prevent the
  server from starting if the host already provided the env vars.
* :data:`override` is **always** ``False`` so host-provided env vars take
  precedence over a stale developer ``.env``.
"""

from __future__ import annotations

import os
from typing import Final

#: Sentinel env var that tests assert on to confirm the loader actually ran.
#: See ``tests/test_bootstrap.py``.
_BOOTSTRAP_RAN_ENV: Final[str] = "SCHWAB_MCP_DOTENV_LOADED"


def bootstrap_dotenv() -> bool:
    """Load ``.env`` from the current working directory (or a parent).

    Returns ``True`` if ``python-dotenv`` was importable and its loader
    ran (regardless of whether a file was actually found), ``False`` if
    the optional dependency is missing.  Never raises.

    The function is **idempotent**: calling it multiple times is safe and
    will not overwrite env vars that the host already injected, because
    ``override=False`` is enforced.
    """
    # ``python-dotenv`` is a declared dependency in ``pyproject.toml`` but we
    # still guard the import so that an unusual install (e.g. running the
    # raw source tree without ``uv sync``) degrades to "fall back to host
    # env" instead of crashing the server before stdio is up.
    try:
        from dotenv import find_dotenv, load_dotenv
    except ImportError:
        return False

    # ``usecwd=True`` anchors the search at the **process** working
    # directory, not at the caller's source file.  This matters in two
    # ways:
    #
    # 1. When the package is installed into site-packages, ``find_dotenv``
    #    would otherwise search upwards from the installed module path
    #    (which has no ``.env``) and silently miss the user's project
    #    ``.env``.
    # 2. In tests, the default frame-stack lookup would walk up from
    #    ``tests/test_bootstrap.py`` and hit the repo-root ``.env``,
    #    leaking real developer credentials into the sandboxed test —
    #    using ``usecwd=True`` keeps every test honest about its cwd.
    try:
        dotenv_path = find_dotenv(usecwd=True)
    except OSError:  # pragma: no cover - defensive: filesystem errors during search
        return True

    if dotenv_path:
        try:
            load_dotenv(dotenv_path=dotenv_path, override=False)
        except OSError:  # pragma: no cover - defensive: e.g. .env became unreadable mid-call
            pass

    os.environ.setdefault(_BOOTSTRAP_RAN_ENV, "1")
    return True


__all__ = ["bootstrap_dotenv"]
