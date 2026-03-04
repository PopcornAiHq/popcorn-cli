"""Error types for Popcorn."""

from __future__ import annotations


class PopcornError(Exception):
    """Base error — printed to stderr without traceback."""


class AuthError(PopcornError):
    """Authentication-related error."""


class APIError(PopcornError):
    """API call failed."""

    def __init__(self, message: str, status_code: int = 0, body: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body
