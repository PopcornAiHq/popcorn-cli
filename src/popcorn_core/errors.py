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
EXIT_INTERRUPT = 130  # Ctrl+C


class PopcornError(Exception):
    """Base error — printed to stderr without traceback."""

    exit_code: int = EXIT_VALIDATION
    hint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "error": str(self),
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

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "error": str(self),
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

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "error": str(self),
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
