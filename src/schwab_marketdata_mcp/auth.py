"""OAuth CLI — thin entry point delegating to :mod:`auth_logic`.

# pragma: no cover  (entire module — see ``[tool.coverage.run].omit``)

Plan §3.2.1.  Two sub-commands:
    * ``login_flow``  — :func:`schwab.auth.client_from_login_flow` (browser).
    * ``manual_flow`` — :func:`schwab.auth.client_from_manual_flow` (paste).

This module performs **no business logic** — every testable piece lives in
``auth_logic.py`` so the unit-test suite can hit 100 % coverage there.
``auth.py`` itself is excluded from coverage.

DO NOT register this module as an MCP server (plan §3.2.3): the OAuth flows
write to stdout to talk to the user's browser and would corrupt JSON-RPC.
"""

# pragma: no cover

from __future__ import annotations

import argparse
import sys
from typing import Any

from .auth_logic import (
    build_auth_config,
    make_token_write_func,
    preflight_summary,
)
from .errors import SchwabAuthError
from .security import CLOUD_OPT_IN_FLAG


def _bootstrap_dotenv() -> None:
    """Load .env for ad-hoc CLI invocation.  Idempotent."""
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass


def _run_login_flow(args: argparse.Namespace) -> int:
    cfg = build_auth_config(
        config_dir=args.config_dir,
        cloud_opt_in=getattr(args, "cloud_opt_in", False),
    )
    print(preflight_summary(cfg), file=sys.stderr)
    if getattr(args, "dry_run", False):
        print(
            "dry-run: pre-flight passed; skipping browser-based OAuth flow.",
            file=sys.stderr,
        )
        return 0
    print(
        "WARNING: login_flow is FRAGILE.  It depends on browser auto-redirect,\n"
        "  no stale Schwab tabs, no HSTS rewrite, and no extension interference.\n"
        "  If it fails with MismatchingStateException, switch to manual_flow:\n"
        "    uv run python -m schwab_marketdata_mcp.auth manual_flow\n"
        "  See docs/REGISTER.md §5 for details.\n"
        "Opening a browser to https://api.schwabapi.com/...\n"
        "If the page shows a self-signed certificate warning, that is expected — "
        "click 'Advanced' → 'Proceed anyway'.\n"
        "Tip: QUIT your browser entirely first; stale tabs are the #1 cause of "
        "MismatchingStateException (CSRF Warning).",
        file=sys.stderr,
    )
    from schwab.auth import client_from_login_flow

    try:
        client_from_login_flow(
            api_key=cfg.api_key,
            app_secret=cfg.app_secret,
            callback_url=cfg.callback_url,
            token_path=str(cfg.token_path),
            asyncio=False,
            enforce_enums=True,
            token_write_func=make_token_write_func(cfg.token_path),
        )
    except Exception as exc:
        if type(exc).__name__ == "MismatchingStateException":
            print(
                "\nERROR: MismatchingStateException — the OAuth state in the "
                "callback URL\n  does not match what schwab-py issued.  This is "
                "almost always caused by\n  a stale browser tab from a previous "
                "run, or by Schwab/extension/HSTS\n  rewriting the redirect.\n\n"
                "  RECOMMENDED FIX: switch to the manual flow, which avoids the\n"
                "  local callback server entirely:\n\n"
                "    uv run python -m schwab_marketdata_mcp.auth manual_flow\n\n"
                "  See docs/REGISTER.md §5 for the full root-cause analysis.",
                file=sys.stderr,
            )
        raise
    print(f"OK — token persisted at {cfg.token_path}", file=sys.stderr)
    return 0


def _run_manual_flow(args: argparse.Namespace) -> int:
    cfg = build_auth_config(
        config_dir=args.config_dir,
        cloud_opt_in=getattr(args, "cloud_opt_in", False),
    )
    print(preflight_summary(cfg), file=sys.stderr)
    if getattr(args, "dry_run", False):
        print(
            "dry-run: pre-flight passed; skipping interactive paste step.",
            file=sys.stderr,
        )
        return 0
    print(
        "Manual flow (RECOMMENDED for reliability):\n"
        "  1. The CLI will print an authorize URL.\n"
        "  2. Copy-paste it into your browser.\n"
        "  3. Log in, click Allow.\n"
        "  4. The browser will land on https://127.0.0.1:8182/?code=...&state=...\n"
        "     (the page itself will fail to load — that is expected).\n"
        "  5. Copy the ENTIRE URL from the browser's address bar.\n"
        "  6. Paste it back into the prompt below and press Enter.",
        file=sys.stderr,
    )
    from schwab.auth import client_from_manual_flow

    client_from_manual_flow(
        api_key=cfg.api_key,
        app_secret=cfg.app_secret,
        callback_url=cfg.callback_url,
        token_path=str(cfg.token_path),
        asyncio=False,
        token_write_func=make_token_write_func(cfg.token_path),
        enforce_enums=True,
    )
    print(f"OK — token persisted at {cfg.token_path}", file=sys.stderr)
    return 0


def cli_main(argv: list[str] | None = None) -> int:
    _bootstrap_dotenv()
    parser = argparse.ArgumentParser(
        prog="schwab_marketdata_mcp.auth",
        description="OAuth credential capture for schwab-marketdata-mcp.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, runner in (("login_flow", _run_login_flow), ("manual_flow", _run_manual_flow)):
        sp = sub.add_parser(name, help=f"Run the {name} variant.")
        sp.add_argument(
            "--config-dir",
            type=str,
            default=None,
            help="Override the token directory (subject to allow-list).",
        )
        sp.add_argument(
            CLOUD_OPT_IN_FLAG,
            dest="cloud_opt_in",
            action="store_true",
            help=f"Acknowledge that {CLOUD_OPT_IN_FLAG} risk and proceed if token path is on a cloud-sync drive.",
        )
        sp.add_argument(
            "--dry-run",
            dest="dry_run",
            action="store_true",
            help=(
                "Run pre-flight checks (env, callback URL, token path) and exit "
                "without spawning a browser or contacting Schwab.  Useful for "
                "verifying configuration after editing .env."
            ),
        )
        sp.set_defaults(func=runner)

    args = parser.parse_args(argv)
    fn: Any = args.func
    try:
        return int(fn(args))
    except SchwabAuthError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("aborted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(cli_main())
