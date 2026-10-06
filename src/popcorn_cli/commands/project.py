"""`popcorn project` — apps, archive, create, delete, edit, info, invite, join,
kick, leave, list.

A project is what the API calls a channel (a conversation of a channel type).
This family was `popcorn channel`; that name stays registered, hidden, as a
copy that prints one line naming the new spelling (`register_renamed`), with
`templates` mapped to its old name. `--channel` and `--template` are rewritten
to `--project` and `--app` before parsing (`cli.py — _rewrite_legacy_flags`).

Surface-only migration: the handlers stay in `cli.py` and are late-bound.
See `_late.late_handler`.

Eight of the eleven take the project as a positional that also answers to
`--project` (`_PROJECT`). None of them has an optional positional after it, so
none needs `trailing` — unlike `message send`, where the text would otherwise
land in the project's slot.

`create` is the odd one out: its positional is the NEW project's name, not an
existing project to act on, so it is a plain positional with no `--project`
spelling. Giving it one would let `project create --project x` read as "create
a project in project x", which is not a thing.
"""

from __future__ import annotations

from popcorn_core.operations import DEFAULT_CHANNEL_TYPE

from ..registry import Argument, Command, Subcommand, register, register_renamed
from ._late import late_handler

_PROJECT = Argument(
    "project",
    "Project name (#general) or UUID",
    positional=True,
    flag_alias="--project",
)

PROJECT = register(
    Command(
        name="project",
        category="projects",
        description=(
            "Project commands (apps, archive, create, delete, edit, info, invite, join, "
            "kick, leave, list)"
        ),
        subcommands=[
            Subcommand(
                "apps",
                "List the apps a project can be created with",
                late_handler("cmd_project_apps"),
            ),
            Subcommand(
                "archive",
                "Archive or unarchive a project",
                late_handler("cmd_archive_project"),
                [_PROJECT, Argument("undo", "Unarchive instead", action="store_true")],
            ),
            Subcommand(
                "create",
                "Create a project",
                late_handler("cmd_create_project"),
                [
                    Argument("name", "Project name", positional=True),
                    Argument(
                        "type",
                        "workspace_channel (default): everyone in the workspace is a member, "
                        "now and as people join; public_channel: anyone can see and join it; "
                        "private_channel: only invited members",
                        choices=["workspace_channel", "public_channel", "private_channel"],
                        default=DEFAULT_CHANNEL_TYPE,
                    ),
                    Argument(
                        "members",
                        "Comma-separated user IDs (ignored for workspace_channel, "
                        "which already has everyone)",
                        type=str,
                    ),
                    Argument(
                        "app",
                        "Run this app in the new project (see `popcorn project apps`)",
                        type=str,
                    ),
                    Argument(
                        "if-not-exists",
                        "Return the project already holding this name (one you are a member of) "
                        "instead of failing on the duplicate",
                        action="store_true",
                    ),
                ],
            ),
            Subcommand(
                "delete", "Delete a project", late_handler("cmd_delete_project"), [_PROJECT]
            ),
            Subcommand(
                "edit",
                "Update project name or description",
                late_handler("cmd_edit_project"),
                [
                    _PROJECT,
                    Argument("name", "New name", type=str),
                    Argument("description", "New description", type=str),
                ],
            ),
            Subcommand(
                "info", "Show project info and members", late_handler("cmd_info"), [_PROJECT]
            ),
            Subcommand(
                "invite",
                "Invite users to a project",
                late_handler("cmd_invite"),
                [_PROJECT, Argument("user_ids", "Comma-separated user IDs", positional=True)],
            ),
            Subcommand("join", "Join a project", late_handler("cmd_join_project"), [_PROJECT]),
            Subcommand(
                "kick",
                "Remove a user from a project",
                late_handler("cmd_kick"),
                [_PROJECT, Argument("user_id", "User UUID to remove", positional=True)],
            ),
            Subcommand("leave", "Leave a project", late_handler("cmd_leave_project"), [_PROJECT]),
            Subcommand(
                "list",
                "List projects",
                late_handler("cmd_project_list"),
                [
                    Argument("query", "Filter query", positional=True, nargs="?", default=""),
                    Argument(
                        "dms",
                        "Deprecated: list DMs and group DMs instead of projects",
                        action="store_true",
                    ),
                    Argument(
                        "include-archived",
                        "Include archived projects (excluded by default)",
                        action="store_true",
                    ),
                    Argument(
                        "include-hidden",
                        "Include hidden projects (excluded by default)",
                        action="store_true",
                    ),
                ],
            ),
        ],
    )
)

register_renamed(PROJECT, "channel", renamed_subcommands={"apps": "templates"})
