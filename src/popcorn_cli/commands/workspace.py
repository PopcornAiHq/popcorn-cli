"""`popcorn workspace` — check-access, inbox, list, switch, users.

Surface-only migration, same shape as `auth`: the five handlers stay in
`cli.py` and are late-bound. See `_late.late_handler`.

One deliberate change comes with the move. The hand-written parser named its
subcommand dest `ws_command`; the registry derives dest from the family name,
so it is now `workspace_command`. Nothing outside the parser depended on the
abbreviation — `commands --json` reports subcommand names, never dests — and
the two tests that asserted it were updated alongside. Keeping `ws_command`
would have meant adding a dest override to the registry purely to preserve an
abbreviation, which is a worse trade than renaming.
"""

from __future__ import annotations

from ..registry import Argument, Command, Subcommand, register
from ._late import late_handler

register(
    Command(
        name="workspace",
        category="auth",
        description="Workspace commands (check-access, inbox, list, switch, users)",
        subcommands=[
            Subcommand(
                "check-access",
                "Check repository access",
                late_handler("cmd_check_access"),
                [Argument("repo", "Repository (owner/repo)", positional=True)],
            ),
            Subcommand(
                "inbox",
                "Show notifications",
                late_handler("cmd_inbox"),
                [
                    Argument(
                        "unread",
                        "Show only unread",
                        action="store_true",
                        exclusive_group="read_state",
                    ),
                    Argument(
                        "read",
                        "Show only read",
                        action="store_true",
                        exclusive_group="read_state",
                    ),
                    # No argparse default, matching the hand-written form: the
                    # 20 the help mentions is the server's, applied when the
                    # param is absent rather than sent by the CLI.
                    Argument("limit", "Max results (default 20)", type=int),
                    Argument("offset", "Pagination offset", type=int),
                ],
            ),
            Subcommand("list", "List available workspaces", late_handler("cmd_workspace_list")),
            Subcommand(
                "switch",
                "Switch active workspace",
                late_handler("cmd_workspace_switch"),
                [Argument("workspace", "Workspace name or UUID", positional=True, nargs="?")],
            ),
            Subcommand(
                "users",
                "List workspace users",
                late_handler("cmd_search_users"),
                [Argument("query", "Filter query", positional=True, nargs="?", default="")],
            ),
        ],
    )
)
