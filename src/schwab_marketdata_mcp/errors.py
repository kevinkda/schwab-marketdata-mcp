"""Structured exception hierarchy and global Bearer-redact log filter.

Plan §3.2.5 — every custom exception accepts only allow-listed structured
fields; **no raw httpx Response object is ever stored** so a `repr()` cannot
leak ``Authorization: Bearer …`` headers into MCP client logs.

Plan §3.2.3 / §8.3 — :class:`RedactBearerFilter` strips ``Bearer …`` tokens
from every log record.  It is registered globally in :mod:`server` so the
mitigation survives any sub-logger that user code or third-party libraries
may attach.

Coverage target: **100 %** (see ``CRITICAL_MODULES`` in
``tests/test_coverage_critical.py``).
"""

from __future__ import annotations

import logging
import re
from typing import Final, Literal

# ---------------------------------------------------------------------------
# Bearer redact filter (Plan §3.2.3 / §8.3)
# ---------------------------------------------------------------------------

_BEARER_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)Bearer\s+[A-Za-z0-9._\-+/=]+",
)
_ACCESS_TOKEN_RE: Final[re.Pattern[str]] = re.compile(
    r'(?i)("?access_token"?\s*[:=]\s*"?)[A-Za-z0-9._\-+/=]+',
)
_REFRESH_TOKEN_RE: Final[re.Pattern[str]] = re.compile(
    r'(?i)("?refresh_token"?\s*[:=]\s*"?)[A-Za-z0-9._\-+/=]+',
)
_REDACTED: Final[str] = "***REDACTED***"


def redact_secrets(text: str) -> str:
    """Strip Bearer / access_token / refresh_token values from *text*.

    Idempotent and side-effect-free.  Used by both the logging filter and the
    exception ``__init__`` constructors so structured fields cannot leak via
    ``repr()`` either.
    """
    out = _BEARER_RE.sub(f"Bearer {_REDACTED}", text)
    out = _ACCESS_TOKEN_RE.sub(rf"\1{_REDACTED}", out)
    out = _REFRESH_TOKEN_RE.sub(rf"\1{_REDACTED}", out)
    return out


class RedactBearerFilter(logging.Filter):
    """Logging filter that redacts ``Bearer …`` and ``*_token`` values.

    Applied to the root logger handlers in :mod:`server` startup so even
    sub-loggers (httpx, schwab, etc.) cannot leak credentials.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except (TypeError, ValueError):
            msg = str(record.msg)
        if "Bearer" in msg or "token" in msg.lower():
            record.msg = redact_secrets(msg)
            record.args = ()
        return True


# ---------------------------------------------------------------------------
# Exception hierarchy (Plan §3.2.5)
# ---------------------------------------------------------------------------

AuthErrorReason = Literal[
    "token_not_initialized",
    "token_corrupted",
    "insecure_token_perms",
    "refresh_token_expired",
    "refresh_token_expired_soon",
    "access_token_invalid",
    "callback_url_mismatch",
    "credential_missing",
    "cloud_path_detected",
    "path_not_in_allow_list",
]


class SchwabError(Exception):
    """Base class for all Schwab MCP errors.

    Subclasses MUST only accept allow-listed structured fields.  This base
    class deliberately keeps ``__str__`` short and does not capture extra
    args so a raw ``repr(exc)`` cannot accidentally leak credentials.
    """

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.__class__.__name__


class SchwabAuthError(SchwabError):
    """Authentication / authorization failure.

    Plan §3.2.2 token state machine and §3.2.5 white-listed message.
    """

    def __init__(
        self,
        *,
        reason: AuthErrorReason,
        hint: str,
        expires_in_seconds: int | None = None,
    ) -> None:
        if not isinstance(reason, str):
            raise TypeError("reason must be a string literal")
        if not isinstance(hint, str):
            raise TypeError("hint must be a string")
        if expires_in_seconds is not None and not isinstance(expires_in_seconds, int):
            raise TypeError("expires_in_seconds must be int or None")
        self.reason: AuthErrorReason = reason
        self.hint: str = redact_secrets(hint)
        self.expires_in_seconds: int | None = expires_in_seconds
        super().__init__(self.hint)

    def __str__(self) -> str:
        if self.expires_in_seconds is not None:
            return f"SchwabAuthError(reason={self.reason}, expires_in={self.expires_in_seconds}s): {self.hint}"
        return f"SchwabAuthError(reason={self.reason}): {self.hint}"


class SchwabRateLimitError(SchwabError):
    """Per-minute request budget exhausted.

    Plan §3.2.4.  Carries metadata for the agent to plan back-off.
    """

    def __init__(self, *, retry_after_seconds: int, current_window_used: int) -> None:
        if not isinstance(retry_after_seconds, int):
            raise TypeError("retry_after_seconds must be int")
        if not isinstance(current_window_used, int):
            raise TypeError("current_window_used must be int")
        self.retry_after_seconds: int = retry_after_seconds
        self.current_window_used: int = current_window_used
        super().__init__(
            f"Rate limit exceeded; retry after {retry_after_seconds}s (used {current_window_used} in window)"
        )

    def __str__(self) -> str:
        return f"SchwabRateLimitError(retry_after={self.retry_after_seconds}s, window_used={self.current_window_used})"


class SchwabTransientError(SchwabError):
    """Retryable transient backend / network error (5xx, timeout, conn reset).

    Carries only ``status_code`` and ``attempt`` — never the response body.
    """

    def __init__(self, *, status_code: int, attempt: int, hint: str) -> None:
        if not isinstance(status_code, int):
            raise TypeError("status_code must be int")
        if not isinstance(attempt, int):
            raise TypeError("attempt must be int")
        if not isinstance(hint, str):
            raise TypeError("hint must be str")
        self.status_code: int = status_code
        self.attempt: int = attempt
        self.hint: str = redact_secrets(hint)
        super().__init__(self.hint)

    def __str__(self) -> str:
        return f"SchwabTransientError(status={self.status_code}, attempt={self.attempt}): {self.hint}"


class SchwabValidationError(SchwabError):
    """Input validation failure (raised before any HTTP call).

    Plan §3.2.5.  Used by Pydantic adapters and by ``get_quotes`` symbol-list
    length guard.
    """

    def __init__(self, *, field: str, reason: str) -> None:
        if not isinstance(field, str):
            raise TypeError("field must be str")
        if not isinstance(reason, str):
            raise TypeError("reason must be str")
        self.field: str = field
        self.reason: str = redact_secrets(reason)
        super().__init__(f"validation failed: {field} — {self.reason}")

    def __str__(self) -> str:
        return f"SchwabValidationError(field={self.field}): {self.reason}"


__all__ = [
    "AuthErrorReason",
    "RedactBearerFilter",
    "SchwabAuthError",
    "SchwabError",
    "SchwabRateLimitError",
    "SchwabTransientError",
    "SchwabValidationError",
    "redact_secrets",
]
