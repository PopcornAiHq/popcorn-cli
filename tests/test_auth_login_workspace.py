"""`auth login --workspace` reaches workspace selection, on both login paths.

The flag promises to skip the interactive picker. It shipped doing nothing at
all, for two independent reasons, and each hid the other:

1. `auth login` declared its own `--workspace` alongside the global one. The
   global is hoisted ahead of the subcommand, argparse then copies the
   subparser's namespace back over the parent's, and the subcommand's `None`
   default landed on top of the parsed value. Covered by
   `test_registry.py — TestHoistedGlobalFlags`.
2. The browser OAuth path — the default login — called `_select_workspace`
   without forwarding the value, so even a correctly parsed flag was dropped.
   That is what this module covers.

The second survived because the only login tests that existed drove the
`--with-token` path, which happened to forward it. A test per path is the point
here, not a test per flag.
"""

from __future__ import annotations

import argparse
from unittest.mock import patch

import jwt
import pytest

from popcorn_cli.cli import cmd_auth_login
from popcorn_core.config import Config

_ISS = "https://clerk.example.com"
_SIGNING_KEY = "x" * 32  # length only matters to silence PyJWT's key-length warning


def _jwt() -> str:
    return jwt.encode(
        {"iss": _ISS, "email": "u@example.com", "exp": 9999999999},
        _SIGNING_KEY,
        algorithm="HS256",
    )


def _args(**over: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "env": None,
        "force": True,
        "with_token": False,
        "workspace": "example-workspace",
        "yes": True,
    }
    base.update(over)
    return argparse.Namespace(**base)


class _ImmediateCallbackMeta(type):
    """`auth_code` that survives the production code clearing it.

    `cmd_auth_login` resets `CallbackHandler.auth_code = None` before polling
    for it, so a plain attribute on a stub would be wiped and the flow would
    block until its 120s deadline. Swallowing the write keeps the poll loop to
    a single pass.
    """

    @property
    def auth_code(cls) -> str:
        return "code-from-callback"

    @auth_code.setter
    def auth_code(cls, value: object) -> None:
        pass


class _ImmediateCallback(metaclass=_ImmediateCallbackMeta):
    error = None
    expected_state = None


@pytest.fixture()
def select_workspace():
    """Patch out everything past token exchange; spy on workspace selection."""
    with (
        patch("popcorn_cli.cli.load_config", return_value=Config()),
        patch("popcorn_cli.cli.save_config"),
        patch("popcorn_cli.cli.APIClient"),
        patch("popcorn_cli.cli.assert_token_env_match"),
        patch("popcorn_cli.cli._select_workspace") as spy,
    ):
        yield spy


def test_browser_login_forwards_the_workspace(select_workspace):
    tokens = {
        "id_token": _jwt(),
        "access_token": "at",
        "refresh_token": "rt",
        "email": "u@example.com",
        "exp": 9999999999,
    }
    with (
        patch(
            "popcorn_cli.cli.discover_oidc",
            return_value={
                "authorization_endpoint": f"{_ISS}/authorize",
                "token_endpoint": f"{_ISS}/token",
            },
        ),
        patch("popcorn_cli.cli.pkce_pair", return_value=("verifier", "challenge")),
        patch("popcorn_cli.cli.run_callback_server"),
        patch("popcorn_cli.cli.webbrowser.open"),
        patch("popcorn_cli.cli.CallbackHandler", _ImmediateCallback),
        patch("popcorn_cli.cli.exchange_code_for_tokens", return_value=tokens),
    ):
        cmd_auth_login(_args())

    assert select_workspace.call_args.args[2] == "example-workspace"


def test_with_token_login_forwards_the_workspace(select_workspace):
    with patch("popcorn_cli.cli.sys.stdin") as stdin:
        stdin.read.return_value = _jwt()
        stdin.isatty.return_value = False
        cmd_auth_login(_args(with_token=True))

    assert select_workspace.call_args.args[2] == "example-workspace"
