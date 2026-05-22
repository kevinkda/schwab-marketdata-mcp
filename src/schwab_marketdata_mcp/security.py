"""Path / permission / file-lock primitives — the security backbone.

Plan §3.2.2.1 (TokenState state machine), §3.3 (credential & file-system
safety), §6.4 (CRITICAL_MODULES → 100 % coverage).

This module is **macOS / Linux only** — ``fcntl.flock`` is unavailable on
Windows native.  Plan §1 platform boundary.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import stat
import sys
from collections.abc import Iterator
from enum import Enum
from pathlib import Path
from typing import Final

from .errors import SchwabAuthError

# ---------------------------------------------------------------------------
# Path allow-list (Plan §3.3.1)
# ---------------------------------------------------------------------------

#: Required mode bits.  Anything broader (group / world) is rejected.
TOKEN_FILE_MODE: Final[int] = 0o600
TOKEN_DIR_MODE: Final[int] = 0o700

#: Cloud-sync path prefixes that are known to silently replicate data
#: outside of the local filesystem.  Best-effort: a user who renames their
#: Dropbox folder to e.g. ``~/work-stuff`` will defeat this check.  The
#: limitation is documented in plan §3.3.3 and in THREAT_MODEL.md.
KNOWN_CLOUD_PREFIXES: Final[tuple[str, ...]] = (
    "Library/Mobile Documents",  # iCloud Drive (macOS)
    "Library/CloudStorage",  # macOS 13+ generic cloud mount
    "Dropbox",
    "OneDrive",
    "Google Drive",
    "GoogleDrive",
    "Box",
    "Box Sync",
    "pCloud",
    "Sync.com",
)


def _xdg_state_root() -> Path:
    """Return ``$XDG_STATE_HOME`` falling back to ``~/.local/state``.

    Plan §2 — we deliberately use the same path on macOS as on Linux to
    simplify cross-machine token migration.  Time Machine exclusion is the
    user's responsibility (plan §3.3.3).
    """
    raw = os.environ.get("XDG_STATE_HOME")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".local" / "state"


def xdg_state_root() -> Path:
    """Public alias for :func:`_xdg_state_root` (used by ``metrics.py``)."""
    return _xdg_state_root()


def default_token_path() -> Path:
    """Default ``token.json`` location.  Plan §3.3.1."""
    return _xdg_state_root() / "schwab-marketdata-mcp" / "token.json"


def _path_within(child: Path, parent: Path) -> bool:
    """Return ``True`` iff *child* is exactly equal to *parent* or strictly under it."""
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def is_cloud_path(path: Path) -> bool:
    """Best-effort cloud-sync path detection.

    Plan §3.3.3 — this is **best-effort**.  A user who renames their Dropbox
    folder defeats this check; the limitation is documented but explicit.
    """
    home = Path.home().resolve(strict=False)
    try:
        rel = path.resolve(strict=False).relative_to(home)
    except ValueError:
        # Outside $HOME — we don't claim to know what's there.
        return False
    parts = rel.parts
    head_one = parts[0] if parts else ""
    head_two = "/".join(parts[:2]) if len(parts) >= 2 else head_one
    return any(prefix in (head_two, head_one) for prefix in KNOWN_CLOUD_PREFIXES)


def resolve_token_path(path: str | os.PathLike[str] | None) -> Path:
    """Resolve and validate a token-file path against the allow-list.

    Allow-list (plan §3.3.1):
      1. ``$XDG_STATE_HOME`` (or its fallback ``~/.local/state``) sub-tree.
      2. ``~/.config`` sub-tree.

    Rejections:
      * Any symlink along the parent chain.
      * Path strings containing ``..``.
      * Paths outside the two allow-listed roots.

    Raises :class:`SchwabAuthError` on rejection — the caller (auth CLI or
    server start-up) is expected to surface the structured ``hint`` to the
    user verbatim.
    """
    if path is None:
        resolved = default_token_path().expanduser()
    else:
        raw = os.fspath(path)
        if ".." in raw.split(os.sep):
            raise SchwabAuthError(
                reason="path_not_in_allow_list",
                hint=f"Refusing parent-traversal segment in token path: {raw!r}",
            )
        resolved = Path(raw).expanduser()

    # We use ``resolve(strict=False)`` so the file does not need to already
    # exist (the auth CLI will create it).  Symlink detection is done on the
    # parent chain *after* resolving so we follow real intent.
    resolved = resolved.resolve(strict=False)

    # Build allow-list roots (resolved so symlinked $HOME chains line up).
    allow_roots = (
        _xdg_state_root().resolve(strict=False),
        (Path.home() / ".config").resolve(strict=False),
    )
    if not any(_path_within(resolved, root) for root in allow_roots):
        raise SchwabAuthError(
            reason="path_not_in_allow_list",
            hint=(
                f"token path {resolved!s} is outside the allow-list. "
                f"Allowed roots: {[str(r) for r in allow_roots]}. "
                "Either omit --config-dir to use the default, or set "
                "$XDG_STATE_HOME to a directory you control."
            ),
        )

    # Walk parent chain and reject any symlink — parents must be real dirs.
    # Note: ``resolve()`` already collapses symlinks, so this loop is mostly
    # belt-and-suspenders.  The branch is kept as a defensive depth check; it
    # rarely fires on a healthy POSIX filesystem.
    cursor = resolved.parent
    while True:
        if cursor.exists() and cursor.is_symlink():  # pragma: no cover - defensive
            raise SchwabAuthError(
                reason="path_not_in_allow_list",
                hint=(
                    f"token path parent contains a symlink at {cursor!s}; "
                    "this is rejected to prevent symlink redirection attacks."
                ),
            )
        if cursor.parent == cursor:  # reached fs root
            break
        cursor = cursor.parent

    return resolved


# ---------------------------------------------------------------------------
# Token state machine (Plan §3.2.2.1)
# ---------------------------------------------------------------------------


class TokenState(str, Enum):
    """States returned by :func:`check_token_file_state`.

    The enum order mirrors the **mandatory check sequence** from plan
    §3.2.2.1 — exists → perms → JSON parse — so any future maintainer can
    simply iterate states for documentation purposes.
    """

    MISSING = "missing"
    INSECURE_PERMS = "insecure_perms"
    MALFORMED = "malformed"
    VALID = "valid"


def _file_mode(path: Path) -> int:
    """Return only the permission bits of *path* (mask 0o7777)."""
    return stat.S_IMODE(path.lstat().st_mode)


def check_token_file_state(path: Path) -> tuple[TokenState, dict[str, object] | None]:
    """Inspect *path* and return the matching :class:`TokenState`.

    The order of checks is mandated by plan §3.2.2.1: existence → permissions
    → JSON parse.  Each step early-returns so an attacker cannot have a
    malformed-but-readable file deserialized into Python before we have
    confirmed permissions are safe.

    Returns ``(state, parsed_json_or_None)``.  ``parsed_json_or_None`` is
    only populated for :attr:`TokenState.VALID`.
    """
    # 1. Existence
    if not path.exists():
        return TokenState.MISSING, None

    # 2. Permissions — must be checked BEFORE json.load so a 0o644 attacker
    #    file is not deserialized into memory.
    actual_mode = _file_mode(path)
    if actual_mode != TOKEN_FILE_MODE:
        return TokenState.INSECURE_PERMS, None

    # 3. JSON parse — strict, no eval.
    try:
        with path.open("r", encoding="utf-8") as fh:
            parsed = json.load(fh)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return TokenState.MALFORMED, None

    if not isinstance(parsed, dict):
        return TokenState.MALFORMED, None

    return TokenState.VALID, parsed


def insecure_perms_hint(path: Path, actual_mode: int) -> str:
    """Render the user-facing repair hint for ``INSECURE_PERMS``.

    Plan §3.3.2 — must always reference the *actual* path, never a hard-coded
    one (a user with ``--config-dir`` will have a different location).
    """
    return (
        f"ERROR: token.json has insecure permissions (got: {oct(actual_mode)}, required: 0o600).\n"
        "Run the following to fix and retry:\n"
        f"  chmod 600 {path}\n"
        f"  chmod 700 {path.parent}\n"
        "Then restart the MCP server."
    )


def enforce_token_perms(path: Path) -> None:
    """Verify the token file and its parent directory have correct modes.

    Raises :class:`SchwabAuthError` with reason ``insecure_token_perms`` if
    either is too permissive.  The hint contains the exact ``chmod`` command
    a user can copy-paste.
    """
    if not path.exists():
        return  # nothing to enforce yet — caller handles MISSING separately
    actual = _file_mode(path)
    if actual != TOKEN_FILE_MODE:
        raise SchwabAuthError(
            reason="insecure_token_perms",
            hint=insecure_perms_hint(path, actual),
        )
    parent = path.parent
    parent_mode = _file_mode(parent)
    if parent_mode != TOKEN_DIR_MODE:
        raise SchwabAuthError(
            reason="insecure_token_perms",
            hint=(
                f"ERROR: token directory has insecure permissions "
                f"(got: {oct(parent_mode)}, required: 0o700).\n"
                f"  chmod 700 {parent}\n"
                "Then restart the MCP server."
            ),
        )


def ensure_secure_dir(parent: Path) -> None:
    """Create *parent* with ``0o700`` if missing and chmod-tighten if loose."""
    if not parent.exists():
        parent.mkdir(parents=True, mode=TOKEN_DIR_MODE, exist_ok=True)
        # mkdir respects umask; explicitly chmod to be safe.
        os.chmod(parent, TOKEN_DIR_MODE)
        return
    actual = _file_mode(parent)
    if actual != TOKEN_DIR_MODE:
        os.chmod(parent, TOKEN_DIR_MODE)


def secure_chmod(path: Path) -> None:
    """Chmod *path* to ``0o600``."""
    os.chmod(path, TOKEN_FILE_MODE)


# ---------------------------------------------------------------------------
# Cross-process token file lock (Plan §3.2.6)
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def token_file_lock(token_path: Path) -> Iterator[None]:
    """``fcntl.flock`` (LOCK_EX) on ``${token_path}.lock`` — POSIX only.

    Schwab refresh tokens rotate-on-use; two concurrent ``Cursor`` sessions
    racing to refresh would result in ``invalid_grant`` for the loser.  This
    context manager serializes refresh.

    Plan §3.2.6 / §1 platform boundary — Windows native is **not** supported.
    """
    if sys.platform == "win32":  # pragma: no cover - unsupported in v1
        raise RuntimeError("token_file_lock requires POSIX (macOS/Linux only)")

    lock_path = token_path.with_suffix(token_path.suffix + ".lock")
    ensure_secure_dir(lock_path.parent)
    # Use os.open with mode 0o600 to prevent any window where the file is
    # group/world-readable.
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, TOKEN_FILE_MODE)
    try:
        # In case the file existed with looser bits, tighten it.
        os.fchmod(fd, TOKEN_FILE_MODE)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Cloud-path opt-in
# ---------------------------------------------------------------------------


CLOUD_OPT_IN_FLAG: Final[str] = "--i-understand-cloud-sync-risk"


def assert_cloud_path_consent(token_path: Path, opt_in: bool) -> None:
    """If *token_path* lives in a cloud-sync prefix, require explicit opt-in.

    Plan §3.3.3.
    """
    if not is_cloud_path(token_path):
        return
    if not opt_in:
        raise SchwabAuthError(
            reason="cloud_path_detected",
            hint=(
                f"Refusing to write token to a cloud-sync path: {token_path!s}\n"
                "Cloud sync replicates secrets to provider servers and other devices.\n"
                f"If you understand the risk, re-run with the {CLOUD_OPT_IN_FLAG!s} flag.\n"
                "Recommended: use the default $XDG_STATE_HOME path or a non-synced directory."
            ),
        )


__all__ = [
    "CLOUD_OPT_IN_FLAG",
    "KNOWN_CLOUD_PREFIXES",
    "TOKEN_DIR_MODE",
    "TOKEN_FILE_MODE",
    "TokenState",
    "assert_cloud_path_consent",
    "check_token_file_state",
    "default_token_path",
    "enforce_token_perms",
    "ensure_secure_dir",
    "insecure_perms_hint",
    "is_cloud_path",
    "resolve_token_path",
    "secure_chmod",
    "token_file_lock",
    "xdg_state_root",
]
