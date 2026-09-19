"""`popcorn message` — delete, download, edit, get, list, react, search, send, threads.

Surface-only migration: the nine handlers stay in `cli.py` and are late-bound.
See `_late.late_handler`.

Most subcommands take the channel as a positional that also answers to
`--channel`, which is `_CHANNEL` below. `send` is the exception worth reading
twice: its channel is OPTIONAL (a default channel may be configured) and a
second optional positional follows it, so it has to name that follower in
`trailing`. Without that, argparse — which fills positionals left to right —
puts the message text in the channel's slot the moment `--channel` is used,
and `message send --channel '#ops' "hi"` sends nothing to a channel called
"hi". `cli.py — _shift_trailing_positionals` undoes that, but only for
arguments that declared what follows them.
"""

from __future__ import annotations

from popcorn_core import operations

from ..registry import Argument, Command, Subcommand, register
from ._late import late_handler

_CHANNEL = Argument(
    "conversation",
    "Channel name (#general) or UUID",
    positional=True,
    flag_alias="--channel",
)
_MESSAGE_ID = Argument("message_id", "Message UUID", positional=True)

register(
    Command(
        name="message",
        category="messages",
        description=(
            "Message commands (delete, download, edit, get, list, react, search, send, threads)"
        ),
        subcommands=[
            Subcommand(
                "delete",
                "Delete a message",
                late_handler("cmd_delete_message"),
                [_CHANNEL, _MESSAGE_ID],
            ),
            Subcommand(
                "download",
                "Download a file attachment",
                late_handler("cmd_download"),
                [
                    Argument(
                        "file_key",
                        "File key (from message media part URL field)",
                        positional=True,
                    ),
                    Argument(
                        "output",
                        "Output path (default: original filename)",
                        type=str,
                        flags=["-o"],
                    ),
                ],
            ),
            Subcommand(
                "edit",
                "Edit a message",
                late_handler("cmd_edit_message"),
                [
                    _CHANNEL,
                    _MESSAGE_ID,
                    Argument("content", "New message content", positional=True),
                ],
            ),
            Subcommand(
                "get",
                "Get a single message by ID",
                late_handler("cmd_get_message"),
                [_MESSAGE_ID],
            ),
            Subcommand(
                "list",
                "Read message history",
                late_handler("cmd_list_messages"),
                [
                    _CHANNEL,
                    Argument("thread", "Thread ID to read replies", type=str),
                    Argument("limit", "Max messages (default 25)", type=int),
                    Argument("before", "Message ID — show messages before this", type=str),
                    Argument("after", "Message ID — show messages after this", type=str),
                    Argument("watch", "Tail new messages (polling)", action="store_true"),
                    Argument(
                        "interval",
                        "Poll interval in seconds (default 3, with --watch)",
                        type=int,
                        default=3,
                    ),
                    Argument(
                        "count",
                        "Exit after receiving N messages (with --watch)",
                        type=int,
                    ),
                    Argument(
                        "max-wait",
                        "Exit after N seconds even if no messages received (with --watch)",
                        type=float,
                    ),
                ],
            ),
            Subcommand(
                "react",
                "React to a message",
                late_handler("cmd_react"),
                [
                    _CHANNEL,
                    _MESSAGE_ID,
                    Argument("emoji", 'Emoji (e.g. "thumbs up")', positional=True),
                    Argument("remove", "Remove reaction instead of adding", action="store_true"),
                ],
            ),
            Subcommand(
                "search",
                "Full-text message search",
                late_handler("cmd_search_messages"),
                [
                    Argument("query", "Search query", positional=True, nargs="?", default=""),
                    Argument("limit", "Max results (default 50)", type=int),
                    Argument("offset", "Pagination offset", type=int),
                    # The query is optional when one of these narrows the
                    # search instead, which is what makes "everything I posted
                    # in #ops last week" expressible at all.
                    Argument(
                        "in",
                        "Only search these channels (#general or UUID, comma-separated)",
                        type=str,
                    ),
                    Argument(
                        "from",
                        "Only messages from these users (username, email or UUID, comma-separated)",
                        type=str,
                    ),
                    Argument("since", "Only messages after this time (ISO 8601)", type=str),
                    Argument("until", "Only messages before this time (ISO 8601)", type=str),
                    Argument(
                        "has",
                        "Only messages containing these "
                        "(file, images, link, mention, video — comma-separated)",
                        type=str,
                    ),
                    Argument(
                        "sort",
                        "Result order (default relevance)",
                        type=str,
                        choices=list(operations.SORT_OPTIONS),
                    ),
                ],
            ),
            Subcommand(
                "send",
                "Send a message",
                late_handler("cmd_send_message"),
                [
                    # Optional (`nargs="?"`), and it names the optional
                    # positional that follows it — see the module docstring.
                    Argument(
                        "conversation",
                        "Channel name (#general) or UUID",
                        positional=True,
                        nargs="?",
                        flag_alias="--channel",
                        trailing=("message",),
                    ),
                    Argument(
                        "message",
                        'Message text (use "-" for stdin)',
                        positional=True,
                        nargs="?",
                    ),
                    Argument("thread", "Reply to thread ID", type=str),
                    Argument("file", "File path to upload and attach", type=str),
                    Argument(
                        "batch",
                        'Read NDJSON from stdin: {"conversation": "...", "message": "..."}',
                        action="store_true",
                    ),
                    Argument(
                        "fail-fast", "Stop batch processing on first error", action="store_true"
                    ),
                ],
            ),
            Subcommand(
                "threads",
                "List threads in a channel",
                late_handler("cmd_list_threads"),
                [
                    _CHANNEL,
                    Argument("limit", "Max threads (default 50)", type=int),
                    Argument("offset", "Pagination offset", type=int),
                ],
            ),
        ],
    )
)
