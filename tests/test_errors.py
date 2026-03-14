"""Tests for popcorn_core.errors."""

from __future__ import annotations

from popcorn_core.errors import (
    EXIT_AUTH,
    EXIT_CLIENT,
    EXIT_SERVER,
    EXIT_VALIDATION,
    APIError,
    AuthError,
    PopcornError,
)


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


class TestExitCodes:
    def test_popcorn_error_exit_code(self):
        assert PopcornError("x").exit_code == EXIT_VALIDATION

    def test_auth_error_exit_code(self):
        assert AuthError("x").exit_code == EXIT_AUTH

    def test_api_error_4xx_exit_code(self):
        assert APIError("x", status_code=404).exit_code == EXIT_CLIENT

    def test_api_error_5xx_exit_code(self):
        assert APIError("x", status_code=502).exit_code == EXIT_SERVER

    def test_api_error_no_status_exit_code(self):
        assert APIError("network error").exit_code == EXIT_VALIDATION

    def test_api_error_429_exit_code(self):
        assert APIError("x", status_code=429).exit_code == EXIT_CLIENT


class TestRetryable:
    def test_5xx_is_retryable(self):
        assert APIError("x", status_code=500).retryable is True
        assert APIError("x", status_code=502).retryable is True
        assert APIError("x", status_code=503).retryable is True

    def test_429_is_retryable(self):
        assert APIError("x", status_code=429).retryable is True

    def test_4xx_not_retryable(self):
        assert APIError("x", status_code=404).retryable is False
        assert APIError("x", status_code=400).retryable is False

    def test_no_status_not_retryable(self):
        assert APIError("network").retryable is False


class TestToDict:
    def test_popcorn_error_to_dict(self):
        d = PopcornError("bad input").to_dict()
        assert d == {"error": "bad input", "code": "PopcornError", "retryable": False}

    def test_auth_error_to_dict(self):
        d = AuthError("not logged in").to_dict()
        assert d["error"] == "not logged in"
        assert d["code"] == "AuthError"
        assert d["retryable"] is False
        assert d["hint"] == "popcorn auth login"

    def test_api_error_to_dict_with_status(self):
        err = APIError("not found", status_code=404, body='{"detail": "nope"}')
        d = err.to_dict()
        assert d["error"] == "not found"
        assert d["code"] == "APIError"
        assert d["status"] == 404
        assert d["retryable"] is False
        assert d["body"] == {"detail": "nope"}

    def test_api_error_to_dict_5xx(self):
        d = APIError("server error", status_code=502).to_dict()
        assert d["retryable"] is True
        assert d["status"] == 502

    def test_api_error_to_dict_no_status(self):
        d = APIError("network error").to_dict()
        assert "status" not in d
        assert "body" not in d

    def test_api_error_to_dict_unparseable_body(self):
        d = APIError("err", status_code=500, body="not json").to_dict()
        assert d["body"] == "not json"

    def test_api_error_retry_after(self):
        d = APIError("rate limited", status_code=429, retry_after=30.0).to_dict()
        assert d["retry_after"] == 30.0
        assert d["retryable"] is True

    def test_api_error_retry_after_absent(self):
        d = APIError("rate limited", status_code=429).to_dict()
        assert "retry_after" not in d

    def test_popcorn_error_hint(self):
        e = PopcornError("bad input")
        e.hint = "popcorn help"
        d = e.to_dict()
        assert d["hint"] == "popcorn help"

    def test_popcorn_error_no_hint(self):
        d = PopcornError("bad input").to_dict()
        assert "hint" not in d

    def test_api_error_hint(self):
        e = APIError("not found", status_code=404)
        e.hint = "popcorn search channels"
        d = e.to_dict()
        assert d["hint"] == "popcorn search channels"

    def test_api_error_request_id(self):
        e = APIError("server error", status_code=500, request_id="req-abc-123")
        d = e.to_dict()
        assert d["request_id"] == "req-abc-123"

    def test_api_error_request_id_absent(self):
        d = APIError("server error", status_code=500).to_dict()
        assert "request_id" not in d
