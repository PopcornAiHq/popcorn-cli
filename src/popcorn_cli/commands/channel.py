"""`popcorn channel` — archive, create, delete, edit, info, invite, join, kick,
leave, list, templates.

Surface-only migration: the eleven handlers stay in `cli.py` and are late-bound.
See `_late.late_handler`.

Eight of the eleven take the channel as a positional that also answers to
`--channel` (`_CHANNEL`). None of them has an optional positional after it, so
none needs `trailing` — unlike `message send`, where the text would otherwise
land in the channel's slot.

`create` is the odd one out: its positional is the NEW channel's name, not an
existing channel to act on, so it is a plain positional with no `--channel`
spelling. Giving it one would let `channel create --channel x` read as "create
a channel in channel x", which is not a thing.
"""

from __future__ import annotations

from ..registry import Argument, Command, Subcommand, register
from ._late import late_handler

_CHANNEL = Argument(
    "conversation",
    "Channel name (#general) or UUID",
    positional=True,
    flag_alias="--channel",
)

register(
    Command(
        name="channel",
        category="channels",
        description=(
            "Channel commands (archive, create, delete, edit, info, invite, join, "
            "kick, leave, list, templates)"
        ),
        subcommands=[
            Subcommand(
                "archive",
                "Archive or unarchive a channel",
                late_handler("cmd_archive_channel"),
                [_CHANNEL, Argument("undo", "Unarchive instead", action="store_true")],
            ),
            Subcommand(
                "create",
                "Create a channel",
                late_handler("cmd_create_channel"),
                [
                    Argument("name", "Channel name", positional=True),
                    Argument(
                        "type",
                        "Conversation type",
                        choices=["public_channel", "private_channel"],
                        default="public_channel",
                    ),
                    Argument("members", "Comma-separated user IDs", type=str),
                    Argument(
                        "template",
                        "Install a channel template (see `popcorn channel templates`)",
                        type=str,
                    ),
                    Argument(
                        "if-not-exists",
                        "Return existing channel instead of failing on duplicate name",
                        action="store_true",
                    ),
                ],
            ),
            Subcommand(
                "delete", "Delete a channel", late_handler("cmd_delete_channel"), [_CHANNEL]
            ),
            Subcommand(
                "edit",
                "Update channel name or description",
                late_handler("cmd_edit_channel"),
                [
                    _CHANNEL,
                    Argument("name", "New name", type=str),
                    Argument("description", "New description", type=str),
                ],
            ),
            Subcommand(
                "info", "Show channel info and members", late_handler("cmd_info"), [_CHANNEL]
            ),
            Subcommand(
                "invite",
                "Invite users to a channel",
                late_handler("cmd_invite"),
                [_CHANNEL, Argument("user_ids", "Comma-separated user IDs", positional=True)],
            ),
            Subcommand("join", "Join a channel", late_handler("cmd_join_channel"), [_CHANNEL]),
            Subcommand(
                "kick",
                "Remove a user from a channel",
                late_handler("cmd_kick"),
                [_CHANNEL, Argument("user_id", "User UUID to remove", positional=True)],
            ),
            Subcommand("leave", "Leave a channel", late_handler("cmd_leave_channel"), [_CHANNEL]),
            Subcommand(
                "list",
                "List channels",
                late_handler("cmd_channel_list"),
                [
                    Argument("query", "Filter query", positional=True, nargs="?", default=""),
                    Argument("dms", "List DMs instead of channels", action="store_true"),
                ],
            ),
            Subcommand(
                "templates",
                "List available channel templates",
                late_handler("cmd_channel_templates"),
            ),
        ],
    )
)
