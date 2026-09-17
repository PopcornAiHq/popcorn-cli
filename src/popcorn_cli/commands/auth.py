"""`popcorn auth` — login, logout, status, token.

Surface-only migration: the four handlers stay in `cli.py` and are late-bound
through `_late.late_handler`. `cmd_auth_login` alone is ~150 lines of OAuth flow with its
own test module importing it by name, and moving that buys nothing the registry
cares about — the registry's job is that the *surface* is declared once, so
argparse, dispatch, both completions and `commands --json` cannot drift apart.
Where the function body physically lives is a separate question.

The families migrated before this one happen to keep their handlers alongside
the declaration, which suits handlers written for them. Either shape is fine;
`Subcommand.handler` only wants a callable.
"""

from __future__ import annotations

from ..registry import Argument, Command, Subcommand, register
from ._late import late_handler

register(
    Command(
        name="auth",
        category="auth",
        description="Auth commands (login, logout, status, token)",
        subcommands=[
            Subcommand(
                "login",
                "Log in via browser OAuth",
                late_handler("cmd_auth_login"),
                [
                    # `--env` is deliberately NOT declared here. The global
                    # -e/--env is hoisted ahead of the subcommand by
                    # `cli.py — _hoist_global_flags`; a subcommand flag sharing
                    # its dest would re-apply its own None default afterwards
                    # and clobber the hoisted value, so `auth login --env prod`
                    # would silently log in against the current default profile.
                    Argument("with-token", "Read token from stdin", action="store_true"),
                    Argument("force", "Re-authenticate", action="store_true"),
                    Argument(
                        "workspace",
                        "Select workspace by name or ID (skips interactive prompt)",
                        type=str,
                    ),
                ],
            ),
            Subcommand("logout", "Clear stored tokens", late_handler("cmd_auth_logout")),
            Subcommand("status", "Show current auth status", late_handler("cmd_auth_status")),
            Subcommand("token", "Print auth token to stdout", late_handler("cmd_auth_token")),
        ],
    )
)
