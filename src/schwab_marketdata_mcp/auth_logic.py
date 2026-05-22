"""Pure-function helpers backing the OAuth CLI in :mod:`auth`.

Plan §3.2.1 / §6.4 — these functions are kept independent of any browser /
network side-effects so the unit-test suite can drive them deterministically
and reach 100 % coverage on the security-critical bits (path allow-list,
permission tightening, atomic token write, cloud-path consent).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlparse

from .errors import SchwabAuthError, redact_secrets
from .security import (
    CLOUD_OPT_IN_FLAG,
    TOKEN_DIR_MODE,
    TOKEN_FILE_MODE,
    assert_cloud_path_consent,
    ensure_secure_dir,
    is_cloud_path,
    resolve_token_path,
    secure_chmod,
    token_file_lock,
)

DEFAULT_LOGIN_FLOW_CALLBACK: Final[str] = "https://127.0.0.1:8182"

#: Lowest TCP port we will accept in ``SCHWAB_CALLBACK_URL``.  Ports ≤1024
#: require root on most kernels and conflict with the schwab-py advisory
#: (`auth.py` docstring) — they are rejected outright.
MIN_CALLBACK_PORT: Final[int] = 1025

#: Highest valid TCP port number.
MAX_CALLBACK_PORT: Final[int] = 65535

#: Hostnames acceptable for ``client_from_login_flow``.  schwab-py itself
#: only allows ``127.0.0.1`` (its own ValueError happens *after* a server
#: process has been forked); we check earlier so the user gets a clean
#: ``SchwabAuthError`` instead of a multiprocess traceback.
LOGIN_FLOW_ALLOWED_HOSTS: Final[tuple[str, ...]] = ("127.0.0.1",)


# ---------------------------------------------------------------------------
# Credential resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthConfig:
    """Validated OAuth configuration ready for ``schwab.auth.client_from_*_flow``."""

    api_key: str
    app_secret: str
    callback_url: str
    token_path: Path
    cloud_opt_in: bool


def _validate_login_flow_callback(callback: str) -> None:
    """Strictly validate ``callback`` for ``client_from_login_flow``.

    Raises :class:`SchwabAuthError` with a structured ``callback_url_mismatch``
    reason when any of the following holds:

    * scheme is not ``https``;
    * hostname is not ``127.0.0.1`` (schwab-py rejects anything else, but
      only after spawning a multiprocess server — fail fast here);
    * **no explicit port** (without a port, schwab-py falls back to 443
      which requires root and triggers a ``RedirectServerExitedError``;
      worse, if the user's Schwab Developer Portal app is registered with
      a different port, the OAuth ``state`` round-trip can desynchronise
      and surface as ``MismatchingStateException`` — see ``docs/REGISTER.md``
      "Troubleshooting").
    * port is outside ``[1025, 65535]`` (privileged ports rejected).

    The callback URL **must** match the value registered in the Schwab
    Developer Portal exactly; mismatches are the #1 root cause of the
    ``MismatchingStateException`` / "CSRF Warning" surfaced by authlib.
    """
    if not callback.startswith("https://"):
        raise SchwabAuthError(
            reason="callback_url_mismatch",
            hint=(
                f"SCHWAB_CALLBACK_URL must use https:// (got: {callback!r}); "
                "Schwab Developer Portal rejects http callbacks."
            ),
        )
    parsed = urlparse(callback)
    if not parsed.netloc:
        raise SchwabAuthError(
            reason="callback_url_mismatch",
            hint=f"SCHWAB_CALLBACK_URL is not a valid URL: {callback!r}",
        )
    if parsed.hostname not in LOGIN_FLOW_ALLOWED_HOSTS:
        raise SchwabAuthError(
            reason="callback_url_mismatch",
            hint=(
                f"SCHWAB_CALLBACK_URL host {parsed.hostname!r} is not allowed "
                f"for login_flow.  schwab-py only accepts hosts in "
                f"{LOGIN_FLOW_ALLOWED_HOSTS}.  Use 'https://127.0.0.1:<port>' "
                "(the port MUST match what is registered in the Schwab "
                "Developer Portal app dashboard)."
            ),
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise SchwabAuthError(
            reason="callback_url_mismatch",
            hint=(
                f"SCHWAB_CALLBACK_URL has an invalid port component: {callback!r} "
                f"({exc})."
            ),
        ) from exc
    if port is None:
        raise SchwabAuthError(
            reason="callback_url_mismatch",
            hint=(
                f"SCHWAB_CALLBACK_URL must include an explicit port "
                f"(got: {callback!r}).  Without one, schwab-py falls back "
                "to port 443 (privileged) and the local callback server "
                "fails to bind; the OAuth flow may also surface a "
                "MismatchingStateException because the registered "
                "redirect_uri in your Schwab Developer Portal app no "
                "longer matches what schwab-py advertises.  Use the "
                "default 'https://127.0.0.1:8182' unless you have a "
                "deliberate reason to change it."
            ),
        )
    if port < MIN_CALLBACK_PORT or port > MAX_CALLBACK_PORT:
        raise SchwabAuthError(
            reason="callback_url_mismatch",
            hint=(
                f"SCHWAB_CALLBACK_URL port {port} is outside the allowed "
                f"range [{MIN_CALLBACK_PORT}, {MAX_CALLBACK_PORT}].  "
                "Privileged ports (≤1024) require root and are rejected; "
                "use a high ephemeral port such as 8182."
            ),
        )


def load_credentials_from_env(
    env: dict[str, str] | None = None,
) -> tuple[str, str, str]:
    """Return ``(api_key, app_secret, callback_url)`` or raise structured error."""
    src = env if env is not None else dict(os.environ)
    key = src.get("SCHWAB_APP_KEY", "").strip()
    secret = src.get("SCHWAB_APP_SECRET", "").strip()
    callback = src.get("SCHWAB_CALLBACK_URL", "").strip() or DEFAULT_LOGIN_FLOW_CALLBACK
    if not key or key == "dummy-not-a-real-secret":
        raise SchwabAuthError(
            reason="credential_missing",
            hint=(
                "SCHWAB_APP_KEY is missing or still a placeholder.  Edit .env "
                "and replace 'dummy-not-a-real-secret' with the real value "
                "from https://developer.schwab.com/dashboard/apps"
            ),
        )
    if not secret or secret == "dummy-not-a-real-secret":
        raise SchwabAuthError(
            reason="credential_missing",
            hint=(
                "SCHWAB_APP_SECRET is missing or still a placeholder.  Edit .env "
                "and replace the dummy with the real value."
            ),
        )
    _validate_login_flow_callback(callback)
    return key, secret, callback


def build_auth_config(
    *,
    config_dir: str | None,
    cloud_opt_in: bool,
    env: dict[str, str] | None = None,
) -> AuthConfig:
    """Resolve env + path → :class:`AuthConfig` with full safety checks."""
    api_key, app_secret, callback = load_credentials_from_env(env)
    raw_path: str | None = None
    if config_dir is not None:
        raw_path = str(Path(config_dir).expanduser() / "token.json")
    token_path = resolve_token_path(raw_path)
    assert_cloud_path_consent(token_path, cloud_opt_in)
    return AuthConfig(
        api_key=api_key,
        app_secret=app_secret,
        callback_url=callback,
        token_path=token_path,
        cloud_opt_in=cloud_opt_in,
    )


# ---------------------------------------------------------------------------
# Atomic token write — used as ``token_write_func`` arg to schwab-py
# ---------------------------------------------------------------------------


def atomic_write_token(path: Path, payload: Any) -> None:
    """Write ``payload`` (dict/list of JSON-serializable values) atomically.

    Steps:
        1. ``os.umask(0o077)`` to deny group/world read.
        2. Ensure parent dir exists with mode ``0o700``.
        3. Write to ``${path}.tmp`` then ``os.replace`` over ``path``.
        4. ``chmod 600`` after rename.

    The write is wrapped in :func:`security.token_file_lock` so concurrent
    Cursor sessions cannot race a refresh-token rotation.
    """
    if not isinstance(payload, (dict, list)):
        raise TypeError("token payload must be JSON dict or list")
    ensure_secure_dir(path.parent)
    old_umask = os.umask(0o077)
    try:
        with token_file_lock(path):
            tmp = path.with_suffix(path.suffix + ".tmp")
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, TOKEN_FILE_MODE)
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, path)
            secure_chmod(path)
    finally:
        os.umask(old_umask)


def make_token_write_func(path: Path) -> Any:
    """Factory returning the closure schwab-py expects.

    schwab-py calls the function with one positional argument: the token
    dict/list to persist.  We delegate to :func:`atomic_write_token`.
    """

    def _writer(token: Any, *_args: Any, **_kwargs: Any) -> None:
        atomic_write_token(path, token)

    return _writer


# ---------------------------------------------------------------------------
# Pre-flight summary used by the CLI to print what it is about to do
# ---------------------------------------------------------------------------


def preflight_summary(cfg: AuthConfig) -> str:
    """Render a redacted summary the CLI prints to stderr before starting."""
    return redact_secrets(
        "Pre-flight:\n"
        f"  api_key:      {cfg.api_key[:4]}…{cfg.api_key[-2:]}\n"
        f"  callback:     {cfg.callback_url}\n"
        f"  token path:   {cfg.token_path}\n"
        f"  cloud path:   {is_cloud_path(cfg.token_path)} "
        f"(opt-in flag {CLOUD_OPT_IN_FLAG} = {cfg.cloud_opt_in})\n"
        f"  parent mode:  {oct(TOKEN_DIR_MODE)}, file mode: {oct(TOKEN_FILE_MODE)}\n"
        "  reminder:     callback URL above MUST exactly match the redirect URI\n"
        "                registered in your Schwab Developer Portal app — a\n"
        "                mismatch surfaces as 'MismatchingStateException / CSRF\n"
        "                Warning' after Allow.  See docs/REGISTER.md."
    )


__all__ = [
    "DEFAULT_LOGIN_FLOW_CALLBACK",
    "LOGIN_FLOW_ALLOWED_HOSTS",
    "MAX_CALLBACK_PORT",
    "MIN_CALLBACK_PORT",
    "AuthConfig",
    "atomic_write_token",
    "build_auth_config",
    "load_credentials_from_env",
    "make_token_write_func",
    "preflight_summary",
]
