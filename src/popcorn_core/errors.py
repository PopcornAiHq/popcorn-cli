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

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": str(self),
            "code": type(self).__name__,
            "retryable": False,
        }


class AuthError(PopcornError):
    """Authentication-related error."""

    exit_code: int = EXIT_AUTH

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": str(self),
            "code": "AuthError",
            "retryable": False,
        }


class APIError(PopcornError):
    """API call failed."""

    def __init__(self, message: str, status_code: int = 0, body: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body

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
        if self.body:
            # Try to include parsed body, fall back to raw string
            try:
                d["body"] = json.loads(self.body)
            except (json.JSONDecodeError, TypeError):
                d["body"] = self.body
        return d
