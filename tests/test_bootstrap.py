"""Tests for :mod:`schwab_marketdata_mcp.bootstrap`.

Plan §3.3 — verifies that the stdio server self-loads ``.env`` from the
current working directory before any business code reads
``os.environ["SCHWAB_APP_KEY"]``.  Without this, hosts that do not
support an ``envFile`` directive (Claude Desktop, raw ``uv run`` from a
shell) fall over with ``SCHWAB_APP_KEY not set``.

The bug being regression-tested:

    server.py imported and ran ``_harden_stdio()`` but never invoked
    ``_bootstrap_dotenv()``, so any host without env-injection produced
    an immediate ``SchwabAuthError(reason="missing_credentials")``.
"""

from __future__ import annotations

import builtins
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from schwab_marketdata_mcp.bootstrap import _BOOTSTRAP_RAN_ENV, bootstrap_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"


# ---------------------------------------------------------------------------
# Unit-level: bootstrap_dotenv() is callable, idempotent, and never raises.
# ---------------------------------------------------------------------------


def test_bootstrap_dotenv_loads_file_into_environ(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Given a ``.env`` in cwd, ``bootstrap_dotenv()`` must surface the
    declared keys via :data:`os.environ`."""
    sentinel = "SCHWAB_MCP_TEST_DOTENV_SENTINEL"
    monkeypatch.delenv(sentinel, raising=False)
    monkeypatch.delenv(_BOOTSTRAP_RAN_ENV, raising=False)
    (tmp_path / ".env").write_text(f"{sentinel}=loaded-from-dotenv\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    loaded = bootstrap_dotenv()

    assert loaded is True, "python-dotenv is a hard dependency; loader must run"
    assert os.environ.get(sentinel) == "loaded-from-dotenv"
    assert os.environ.get(_BOOTSTRAP_RAN_ENV) == "1"


def test_bootstrap_dotenv_does_not_override_host_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Host-injected env vars (Cursor ``envFile``, shell ``export``) must
    take precedence over a stale developer ``.env`` — guarantee provided
    by ``override=False`` inside :func:`bootstrap_dotenv`."""
    sentinel = "SCHWAB_MCP_TEST_OVERRIDE_SENTINEL"
    monkeypatch.setenv(sentinel, "from-host")
    (tmp_path / ".env").write_text(f"{sentinel}=from-dotenv\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    bootstrap_dotenv()

    assert os.environ.get(sentinel) == "from-host"


def test_bootstrap_dotenv_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling :func:`bootstrap_dotenv` multiple times is a no-op the
    second time around (no exception, return value still ``True``)."""
    sentinel = "SCHWAB_MCP_TEST_IDEMPOTENT_SENTINEL"
    monkeypatch.delenv(sentinel, raising=False)
    (tmp_path / ".env").write_text(f"{sentinel}=v1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    first = bootstrap_dotenv()
    second = bootstrap_dotenv()

    assert first is True
    assert second is True
    assert os.environ.get(sentinel) == "v1"


def test_bootstrap_dotenv_missing_file_does_not_raise(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``.env`` present must not error — host-injected env is the
    primary path, the ``.env`` fallback is best-effort only."""
    monkeypatch.chdir(tmp_path)
    assert not (tmp_path / ".env").exists()

    result = bootstrap_dotenv()

    assert result is True


def test_bootstrap_dotenv_missing_dependency_returns_false(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``python-dotenv`` is unavailable, the helper degrades to a
    silent ``False`` instead of raising at server-start time."""
    monkeypatch.chdir(tmp_path)
    real_import = builtins.__import__

    def _fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "dotenv" or name.startswith("dotenv."):
            raise ImportError("simulated missing dotenv")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.delitem(sys.modules, "dotenv", raising=False)
    monkeypatch.setattr(builtins, "__import__", _fake_import)

    assert bootstrap_dotenv() is False


# ---------------------------------------------------------------------------
# Integration: a fresh stdio server subprocess started in a directory with a
# ``.env`` file must see those env vars even when the parent did NOT inject
# them.  This is the exact scenario that broke for Claude Desktop / direct
# CLI invocation before the fix.
# ---------------------------------------------------------------------------


def _strip_schwab_creds(env: dict[str, str]) -> dict[str, str]:
    """Return a copy of ``env`` with any host-side Schwab creds removed,
    forcing the child process to rely on its cwd ``.env`` for them."""
    cleaned = dict(env)
    for k in (
        "SCHWAB_APP_KEY",
        "SCHWAB_APP_SECRET",
        "SCHWAB_CALLBACK_URL",
    ):
        cleaned.pop(k, None)
    for k in (
        "COV_CORE_SOURCE",
        "COV_CORE_CONFIG",
        "COV_CORE_DATAFILE",
        "COV_CORE_CONTEXT",
    ):
        cleaned.pop(k, None)
    cleaned["COVERAGE_DISABLE"] = "1"
    return cleaned


def test_server_subprocess_loads_dotenv_from_cwd(tmp_path: Path) -> None:
    """End-to-end regression: spawn the server with **no** Schwab creds in
    the parent env, drop a ``.env`` file with placeholder creds in the
    child's cwd, and verify ``initialize`` succeeds (proof that
    ``bootstrap_dotenv`` ran before any business code touched
    ``os.environ['SCHWAB_APP_KEY']``).
    """
    child_state = tmp_path / "child_state"
    child_state.mkdir(exist_ok=True)
    (tmp_path / ".env").write_text(
        "SCHWAB_APP_KEY=test-from-dotenv-fixture\n"
        "SCHWAB_APP_SECRET=test-secret-from-dotenv-fixture\n"
        "SCHWAB_CALLBACK_URL=https://127.0.0.1:8182\n"
        "SCHWAB_MOCK_BACKEND=fixtures\n"
        f"SCHWAB_MOCK_FIXTURES_DIR={FIXTURES_DIR}\n"
        "SCHWAB_MOCK_SCENARIO=normal\n"
        "SCHWAB_MAX_RETRIES=0\n"
        "LOG_LEVEL=WARNING\n"
        f"XDG_STATE_HOME={child_state}\n",
        encoding="utf-8",
    )

    parent_env = _strip_schwab_creds(dict(os.environ))

    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "regression", "version": "0"},
        },
    }

    with subprocess.Popen(
        [sys.executable, "-m", "schwab_marketdata_mcp.server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=parent_env,
        cwd=str(tmp_path),
    ) as proc:
        try:
            assert proc.stdin is not None
            assert proc.stdout is not None
            assert proc.stderr is not None
            proc.stdin.write((json.dumps(initialize) + "\n").encode())
            proc.stdin.flush()
            first_line = proc.stdout.readline()
            decoded = json.loads(first_line)

            assert decoded.get("jsonrpc") == "2.0", (
                f"server failed to respond with a valid JSON-RPC frame: {first_line!r}"
            )
            assert "error" not in decoded, (
                f"server reported a startup error — bootstrap_dotenv may not have run: {decoded!r}"
            )
        finally:
            proc.terminate()
            try:
                _, stderr_bytes = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                proc.kill()
                _, stderr_bytes = proc.communicate()

    stderr_text = stderr_bytes.decode("utf-8", errors="replace")
    assert "SCHWAB_APP_KEY" not in stderr_text or "missing" not in stderr_text.lower(), (
        f"server logged a missing-credential error despite a .env in cwd:\n{stderr_text}"
    )
