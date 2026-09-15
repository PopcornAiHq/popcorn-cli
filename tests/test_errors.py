"""Tests for popcorn_core.errors."""

from __future__ import annotations

from popcorn_core.errors import (
    EXIT_AUTH,
    EXIT_CLIENT,
    EXIT_SERVER,
    EXIT_UNHEALTHY,
    EXIT_VALIDATION,
    APIError,
    AuthError,
    PopcornError,
)


def test_exit_unhealthy_is_5():
    assert EXIT_UNHEALTHY == 5


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
        assert d == {
            "error": "bad input",
            "error_code": "validation",
            "code": "PopcornError",
            "retryable": False,
        }

    def test_auth_error_to_dict(self):
        d = AuthError("not logged in").to_dict()
        assert d["error"] == "not logged in"
        assert d["code"] == "AuthError"
        assert d["error_code"] == "unauthorized"
        assert d["retryable"] is False
        assert d["hint"] == "popcorn auth login"

    def test_api_error_to_dict_with_status(self):
        err = APIError("not found", status_code=404, body='{"detail": "nope"}')
        d = err.to_dict()
        assert d["error"] == "not found"
        assert d["code"] == "APIError"
        assert d["error_code"] == "not_found"
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


class TestErrorCode:
    """Stable machine-readable error_code for agent branching."""

    def test_popcorn_error_default(self):
        assert PopcornError("x").error_code == "validation"

    def test_popcorn_error_override_via_init(self):
        assert PopcornError("x", error_code="not_found").error_code == "not_found"

    def test_popcorn_error_hint_via_init(self):
        e = PopcornError("x", hint="try this")
        assert e.hint == "try this"
        assert e.to_dict()["hint"] == "try this"

    def test_auth_error_default(self):
        assert AuthError("x").error_code == "unauthorized"

    def test_api_error_401(self):
        assert APIError("x", status_code=401).error_code == "unauthorized"

    def test_api_error_403(self):
        assert APIError("x", status_code=403).error_code == "forbidden"

    def test_api_error_404(self):
        assert APIError("x", status_code=404).error_code == "not_found"

    def test_api_error_409(self):
        assert APIError("x", status_code=409).error_code == "conflict"

    def test_api_error_422(self):
        assert APIError("x", status_code=422).error_code == "validation"

    def test_api_error_429(self):
        assert APIError("x", status_code=429).error_code == "rate_limited"

    def test_api_error_other_4xx(self):
        assert APIError("x", status_code=418).error_code == "client_error"

    def test_api_error_5xx(self):
        assert APIError("x", status_code=500).error_code == "server_error"
        assert APIError("x", status_code=503).error_code == "server_error"

    def test_api_error_network(self):
        assert APIError("x").error_code == "network_error"


class TestHintRendering:
    """KEW-2373: the hint label used to be a flat "Run:", which was wrong both
    ways — it doubled up on hints carrying their own verb, and it told people
    to type things that are not commands."""

    def test_a_command_hint_says_run(self):
        from popcorn_cli.cli import _hint_line

        assert _hint_line("popcorn auth login") == "Run: popcorn auth login"

    def test_advice_is_not_labelled_as_a_command(self):
        from popcorn_cli.cli import _hint_line

        assert _hint_line("pass --force to overwrite them") == (
            "Hint: pass --force to overwrite them"
        )

    def test_a_self_prefixed_hint_does_not_print_the_verb_twice(self):
        """The reported bug: `Run: run: popcorn app checkout …`."""
        from popcorn_cli.cli import _hint_line

        rendered = _hint_line("run: popcorn app checkout --channel '#your-channel'")
        assert rendered == "Run: popcorn app checkout --channel '#your-channel'"
        assert "run: run:" not in rendered.lower()

    def test_no_hint_in_the_source_carries_its_own_verb(self):
        """The renderer tolerates a self-prefixed hint; the source must not
        write one. Grepped rather than spot-checked, because fixing only the
        one that was reported is how the next copy gets added."""
        import re
        from pathlib import Path

        import popcorn_cli
        import popcorn_core

        offenders = []
        roots = {Path(popcorn_cli.__file__).parent, Path(popcorn_core.__file__).parent}
        for root in roots:
            for path in root.rglob("*.py"):
                for match in re.finditer(r"""hint=f?["']([^"']*)""", path.read_text()):
                    if match.group(1).strip().lower().startswith("run:"):
                        offenders.append(f"{path.name}: {match.group(1)}")
        assert offenders == [], f"hints carrying their own 'run:' prefix: {offenders}"

    def test_the_main_loop_renders_through_the_labeller(self):
        """Guards the wiring, not the function: an un-labelled f-string in
        `main` would pass every test above."""
        import inspect

        from popcorn_cli import cli

        source = inspect.getsource(cli.main)
        assert "_hint_line(e.hint)" in source
        assert "Run: {e.hint}" not in source
