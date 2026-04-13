"""Error types for Popcorn."""

from __future__ import annotations

import json
from typing import Any

# Exit codes — agents can switch on these to decide retry vs bail
EXIT_OK = 0
EXIT_VALIDATION = 1  # Bad input, missing args, invalid state
EXIT_AUTH = 2  # Auth failures — re-login required
EXIT_CLIENT = 3  # 4xx API errors — request is wrong
EXIT_SERVER = 4  # 5xx API errors — retryable
EXIT_UNHEALTHY = 5  # Deploy succeeded but site is unhealthy
EXIT_INTERRUPT = 130  # Ctrl+C

# Stable machine-readable error codes for agents.
# These are orthogonal to the Python exception class name (kept in `code` for
# backward compatibility) and safe to branch on from scripts.
ERROR_CODE_VALIDATION = "validation"
ERROR_CODE_UNAUTHORIZED = "unauthorized"
ERROR_CODE_FORBIDDEN = "forbidden"
ERROR_CODE_NOT_FOUND = "not_found"
ERROR_CODE_CONFLICT = "conflict"
ERROR_CODE_RATE_LIMITED = "rate_limited"
ERROR_CODE_CLIENT = "client_error"
ERROR_CODE_SERVER = "server_error"
ERROR_CODE_NETWORK = "network_error"
ERROR_CODE_UNHEALTHY = "unhealthy"
ERROR_CODE_INTERNAL = "internal"

# List form for schema introspection via `popcorn commands --json`
ERROR_CODES: list[dict[str, str]] = [
    {"code": ERROR_CODE_VALIDATION, "description": "Bad input, missing args, or invalid state"},
    {"code": ERROR_CODE_UNAUTHORIZED, "description": "Not logged in or token expired — re-auth"},
    {"code": ERROR_CODE_FORBIDDEN, "description": "Authenticated but lacks permission"},
    {"code": ERROR_CODE_NOT_FOUND, "description": "Resource does not exist"},
    {
        "code": ERROR_CODE_CONFLICT,
        "description": "Conflicts with current state (e.g. already exists)",
    },
    {"code": ERROR_CODE_RATE_LIMITED, "description": "Rate limited — honor retry_after field"},
    {"code": ERROR_CODE_CLIENT, "description": "Other 4xx error — request is wrong"},
    {"code": ERROR_CODE_SERVER, "description": "5xx error — retryable with backoff"},
    {"code": ERROR_CODE_NETWORK, "description": "Transport failure (no HTTP response)"},
    {"code": ERROR_CODE_UNHEALTHY, "description": "Deploy succeeded but site is unhealthy"},
    {"code": ERROR_CODE_INTERNAL, "description": "Unexpected internal CLI error"},
]


def _api_status_to_error_code(status_code: int) -> str:
    """Map an HTTP status code to a stable error_code."""
    if status_code == 0:
        return ERROR_CODE_NETWORK
    if status_code == 401:
        return ERROR_CODE_UNAUTHORIZED
    if status_code == 403:
        return ERROR_CODE_FORBIDDEN
    if status_code == 404:
        return ERROR_CODE_NOT_FOUND
    if status_code == 409:
        return ERROR_CODE_CONFLICT
    if status_code == 422:
        return ERROR_CODE_VALIDATION
    if status_code == 429:
        return ERROR_CODE_RATE_LIMITED
    if 400 <= status_code < 500:
        return ERROR_CODE_CLIENT
    if status_code >= 500:
        return ERROR_CODE_SERVER
    return ERROR_CODE_INTERNAL


class PopcornError(Exception):
    """Base error — printed to stderr without traceback."""

    exit_code: int = EXIT_VALIDATION
    hint: str | None = None
    # Default stable machine-readable code; subclasses or callers can override
    # by passing `error_code=` to __init__ or by setting the class attribute.
    error_code: str = ERROR_CODE_VALIDATION

    def __init__(self, *args: Any, error_code: str | None = None, hint: str | None = None) -> None:
        super().__init__(*args)
        if error_code is not None:
            self.error_code = error_code
        if hint is not None:
            self.hint = hint

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "error": str(self),
            "error_code": self.error_code,
            "code": type(self).__name__,
            "retryable": False,
        }
        if self.hint:
            d["hint"] = self.hint
        return d


class AuthError(PopcornError):
    """Authentication-related error."""

    exit_code: int = EXIT_AUTH
    hint: str | None = "popcorn auth login"
    error_code: str = ERROR_CODE_UNAUTHORIZED

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "error": str(self),
            "error_code": self.error_code,
            "code": "AuthError",
            "retryable": False,
            "hint": self.hint or "popcorn auth login",
        }
        return d


class APIError(PopcornError):
    """API call failed."""

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        body: str | None = None,
        retry_after: float | None = None,
        request_id: str | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.body = body
        self.retry_after = retry_after
        self.request_id = request_id

    @property
    def exit_code(self) -> int:  # type: ignore[override]
        if self.status_code >= 500:
            return EXIT_SERVER
        if self.status_code >= 400:
            return EXIT_CLIENT
        return EXIT_VALIDATION  # network errors, no status code

    @property
    def retryable(self) -> bool:
        return self.status_code >= 500 or self.status_code == 429

    @property  # type: ignore[override]
    def error_code(self) -> str:  # type: ignore[override]
        return _api_status_to_error_code(self.status_code)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "error": str(self),
            "error_code": self.error_code,
            "code": "APIError",
            "retryable": self.retryable,
        }
        if self.status_code:
            d["status"] = self.status_code
        if self.retry_after is not None:
            d["retry_after"] = self.retry_after
        if self.hint:
            d["hint"] = self.hint
        if self.request_id:
            d["request_id"] = self.request_id
        if self.body:
            # Try to include parsed body, fall back to raw string
            try:
                d["body"] = json.loads(self.body)
            except (json.JSONDecodeError, TypeError):
                d["body"] = self.body
        return d
