"""Tests for popcorn_core.errors."""

from __future__ import annotations

from popcorn_core.errors import APIError, AuthError, PopcornError


class TestErrorHierarchy:
    def test_popcorn_error_is_exception(self):
        assert issubclass(PopcornError, Exception)

    def test_auth_error_is_popcorn_error(self):
        assert issubclass(AuthError, PopcornError)

    def test_api_error_is_popcorn_error(self):
        assert issubclass(APIError, PopcornError)

    def test_api_error_attrs(self):
        err = APIError("not found", status_code=404, body='{"detail": "nope"}')
        assert str(err) == "not found"
        assert err.status_code == 404
        assert err.body == '{"detail": "nope"}'

    def test_api_error_defaults(self):
        err = APIError("oops")
        assert err.status_code == 0
        assert err.body is None
