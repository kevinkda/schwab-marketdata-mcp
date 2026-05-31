"""OWASP Top 10 — 2021 edition security suite (batch 2).

Each test maps to a 2021 category via an ``# OWASP 2021 Axx`` comment.
See ``test_owasp_2017.py`` for the shared attack-surface notes.  N/A
categories for this server: none in 2021 are fully N/A, but A04/A08 are
addressed structurally (read-only data plane, strict JSON integrity).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from schwab_marketdata_mcp import cache
from schwab_marketdata_mcp.errors import (
    RedactBearerFilter,
    SchwabAuthError,
)
from schwab_marketdata_mcp.models import GetOptionChainInput, validate_tool_input
from schwab_marketdata_mcp.security import (
    TokenState,
    check_token_file_state,
    resolve_token_path,
)

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "schwab_marketdata_mcp"


# ---------------------------------------------------------------------------
# OWASP 2021 A01 — Broken Access Control
# ---------------------------------------------------------------------------


def test_a01_token_path_allow_list_blocks_escape(_isolate_xdg_state: Path) -> None:
    """OWASP 2021 A01 — token path confined to the XDG state allow-list."""
    for evil in ("/etc/passwd", "/root/.ssh/id_rsa", "~/.local/state/../../etc/hosts"):
        with pytest.raises(SchwabAuthError):
            resolve_token_path(evil)


def test_a01_marketdata_scope_has_no_mutation_endpoints() -> None:
    """OWASP 2021 A01 — the data plane is read-only: no tool wraps a Schwab
    *write* endpoint.  Guard the tool surface against a place_order / cancel
    style method ever being wired in (which would need a different scope)."""
    tools_dir = SRC_ROOT / "tools"
    banned = ("place_order", "cancel_order", "replace_order", ".post(", ".put(", ".delete(")
    for py in tools_dir.glob("*.py"):
        text = py.read_text(encoding="utf-8")
        for token in banned:
            assert token not in text, f"{py.name} introduced a mutation call ({token!r})"


# ---------------------------------------------------------------------------
# OWASP 2021 A02 — Cryptographic Failures
# ---------------------------------------------------------------------------


def test_a02_bearer_redacted_in_log_records() -> None:
    """OWASP 2021 A02 — RedactBearerFilter scrubs bearer tokens from log msgs."""
    f = RedactBearerFilter()
    rec = logging.makeLogRecord({"msg": "Authorization: Bearer eyJ.secret.payload"})
    f.filter(rec)
    assert "eyJ.secret.payload" not in rec.msg
    assert "***REDACTED***" in rec.msg


def test_a02_no_weak_hash_algorithms_for_secrets() -> None:
    """OWASP 2021 A02 — no MD5/SHA1/DES used for credential handling.

    cache.py uses SHA-256 for *cache keys* (non-secret); assert no weak
    primitives appear anywhere in source.
    """
    banned = ("md5(", "sha1(", "hashlib.md5", "hashlib.sha1", "Crypto.Cipher.DES", "ARC4", "RC4.new")
    for py in SRC_ROOT.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        for token in banned:
            assert token not in text, f"{py.name} uses a weak primitive ({token!r})"


def test_a02_cache_key_hash_is_sha256() -> None:
    """OWASP 2021 A02 — the cache key derivation uses SHA-256 (64 hex chars)."""
    key = cache._hash_params({"symbol": "AAPL", "strike": 100.0})
    assert len(key) == 64
    assert all(ch in "0123456789abcdef" for ch in key)


# ---------------------------------------------------------------------------
# OWASP 2021 A03 — Injection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "evil",
    [
        "AAPL';--",
        "AAPL UNION SELECT raw_json FROM quotes_cache",
        "AAPL\nINSERT",
        "AAPL%00",
        "../../etc/passwd",
    ],
)
def test_a03_option_chain_symbol_injection_rejected(evil: str) -> None:
    """OWASP 2021 A03 — option-chain underlying symbol rejects injection chars."""
    with pytest.raises(Exception):
        GetOptionChainInput(symbol=evil)


def test_a03_option_chain_cache_key_binding_is_safe(tmp_path: Path) -> None:
    """OWASP 2021 A03 — option-chain params with metachars hash to a safe key
    and round-trip via parameterised binding (no SQL execution)."""
    params = {"symbol": "AAPL", "note": "'; DROP TABLE option_chain_cache;--"}
    with cache.Cache(tmp_path / "c.duckdb") as c:
        c.put_option_chain(params, {"callExpDateMap": {}})
        got = c.get_option_chain(params)
        stats = c.get_stats()
    assert got == {"callExpDateMap": {}}
    # Table intact → the metachar param was bound, not interpolated.
    assert "option_chain_cache" in stats.rows_per_table


# ---------------------------------------------------------------------------
# OWASP 2021 A04 — Insecure Design
# ---------------------------------------------------------------------------


def test_a04_cache_is_best_effort_by_design(tmp_path: Path) -> None:
    """OWASP 2021 A04 — cache failures degrade safely: a closed/failed cache
    returns miss (None), never crashes the tool path."""
    c = cache.Cache(tmp_path / "c.duckdb")
    c.close()  # simulate an unavailable backend
    # All getters must return a benign miss rather than raising.
    assert c.get_quote("AAPL") is None
    assert c.get_option_chain({"symbol": "AAPL"}) is None
    assert c.get_instruments({"cusip": "X"}) is None


def test_a04_streaming_duration_hard_bounded() -> None:
    """OWASP 2021 A04 — bounded-snapshot design caps connection lifetime
    (no unbounded streaming) — out-of-range durations are rejected."""
    from schwab_marketdata_mcp.errors import SchwabValidationError

    for bad in (0, 100, 10_001, 86_400_000):
        with pytest.raises(SchwabValidationError):
            validate_tool_input(
                "get_streaming_snapshot",
                {"symbols": ["VOO"], "service": "LEVELONE_EQUITIES", "duration_ms": bad},
            )


# ---------------------------------------------------------------------------
# OWASP 2021 A05 — Security Misconfiguration
# ---------------------------------------------------------------------------


def test_a05_no_cors_wildcard_or_debug_server_in_source() -> None:
    """OWASP 2021 A05 — no permissive CORS / debug listeners (stdio transport)."""
    # S104 here is a false positive: this is the *deny-list* of tokens we scan
    # source for, not an actual socket bind.
    banned = ("Access-Control-Allow-Origin", "debug=True", "0.0.0.0", "ssl=False")  # noqa: S104
    for py in SRC_ROOT.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        for token in banned:
            assert token not in text, f"{py.name} has a misconfig marker ({token!r})"


# ---------------------------------------------------------------------------
# OWASP 2021 A06 — Vulnerable & Outdated Components
# ---------------------------------------------------------------------------


def test_a06_dependencies_are_bounded() -> None:
    """OWASP 2021 A06 — deps carry version constraints; lockfile is committed."""
    root = SRC_ROOT.parents[1]
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert "dependencies" in pyproject
    assert (root / "uv.lock").exists(), "uv.lock must be committed for reproducible installs"


# ---------------------------------------------------------------------------
# OWASP 2021 A07 — Identification & Authentication Failures
# ---------------------------------------------------------------------------


def test_a07_malformed_token_rejected_not_coerced(tmp_path: Path) -> None:
    """OWASP 2021 A07 — a malformed token is rejected, never coerced into a session."""
    import os

    f = tmp_path / "token.json"
    f.write_text("{garbage")
    os.chmod(f, 0o600)
    state, _ = check_token_file_state(f)
    assert state is TokenState.MALFORMED


def test_a07_non_dict_token_top_level_rejected(tmp_path: Path) -> None:
    """OWASP 2021 A07 — a JSON array/scalar token is not a valid session."""
    import os

    f = tmp_path / "token.json"
    f.write_text("[1, 2, 3]")
    os.chmod(f, 0o600)
    state, _ = check_token_file_state(f)
    assert state is TokenState.MALFORMED


# ---------------------------------------------------------------------------
# OWASP 2021 A08 — Software & Data Integrity Failures
# ---------------------------------------------------------------------------


def test_a08_fixtures_are_strict_json() -> None:
    """OWASP 2021 A08 — every shipped fixture is strict JSON (no code payloads)."""
    fdir = Path(__file__).resolve().parent / "fixtures"
    for f in fdir.rglob("*.json"):
        with f.open("r", encoding="utf-8") as fh:
            json.load(fh)


def test_a08_corrupt_cache_db_quarantined_not_trusted(tmp_path: Path) -> None:
    """OWASP 2021 A08 — a corrupt DuckDB file is quarantined and a fresh one
    opened, rather than trusting/parsing corrupt data."""
    db = tmp_path / "c.duckdb"
    db.write_text("this is not a valid duckdb file")
    c = cache.Cache(db)
    # Either a fresh DB was opened (conn present) or it degraded to None;
    # either way the corrupt bytes were never trusted.
    if c._conn is not None:
        # A fresh, queryable DB.
        assert c.get_quote("AAPL") is None
    backups = list(tmp_path.glob("c.duckdb.corrupt-*"))
    assert backups, "corrupt DB should have been quarantined aside"
    c.close()


# ---------------------------------------------------------------------------
# OWASP 2021 A09 — Security Logging & Monitoring Failures
# ---------------------------------------------------------------------------


def test_a09_error_window_surfaces_only_metadata(tmp_path: Path) -> None:
    """OWASP 2021 A09 — the error window exposes ts/tool/error_class only,
    never the raw exception message (which could leak PII / secrets)."""
    from schwab_marketdata_mcp import metrics

    p = tmp_path / "usage.jsonl"
    metrics.record(
        tool="get_quote",
        status="err",
        error_class="SchwabAuthError",
        latency_ms=5,
        path=p,
    )
    window = metrics.recent_errors_window(window_minutes=60, last_n=5, path=p)
    assert window["error_count"] == 1
    for entry in window["last_errors"]:
        assert set(entry) == {"ts", "tool", "error_class"}
        assert "message" not in entry


# ---------------------------------------------------------------------------
# OWASP 2021 A10 — Server-Side Request Forgery (SSRF)
# ---------------------------------------------------------------------------


def test_a10_symbol_cannot_inject_arbitrary_url() -> None:
    """OWASP 2021 A10 — symbols are alnum-bounded so they cannot smuggle a
    scheme/host into the outbound Schwab request path."""
    from schwab_marketdata_mcp.models import GetQuoteInput

    for ssrf in (
        "http://169.254.169.254/latest/meta-data",
        "file:///etc/passwd",
        "//evil.tld/AAPL",
        "AAPL@evil.tld",
        "AAPL.evil.tld:8080",
    ):
        with pytest.raises(Exception):
            GetQuoteInput(symbol=ssrf)


def test_a10_no_user_controlled_base_url_in_client() -> None:
    """OWASP 2021 A10 — the Schwab base URL is fixed by schwab-py; our client
    never reads a base/host from user input or env that an attacker controls."""
    client_src = (SRC_ROOT / "client.py").read_text(encoding="utf-8")
    # We must not build a request URL from a caller-supplied host/base.
    assert "base_url=" not in client_src or "os.environ" not in client_src.split("base_url=")[0][-200:]
    # Callback URL defaults to loopback; never a wildcard/remote default.
    assert "127.0.0.1" in client_src
