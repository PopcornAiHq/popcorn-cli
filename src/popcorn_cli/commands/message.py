"""`popcorn message` — answer, delete, download, edit, get, list, questions,
react, search, send, threads.

Surface-only migration: nine of the handlers stay in `cli.py` and are
late-bound (see `_late.late_handler`). `questions` and `answer` were written
here, and import their `..cli` helpers inside the function body, since cli.py
imports this package at module load.

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

import argparse
import json
import sys
from typing import Any

from popcorn_core import operations
from popcorn_core import questions as q
from popcorn_core.errors import PopcornError

from ..registry import Argument, Command, Subcommand, register
from ._late import late_handler

_CHANNEL = Argument(
    "conversation",
    "Channel name (#general) or UUID",
    positional=True,
    flag_alias="--channel",
)
_MESSAGE_ID = Argument("message_id", "Message UUID", positional=True)

# How far back `answer` looks for the card it was named among a channel's
# top-level messages: a pending card is almost always the newest message, and
# the history route takes up to 1000. A thread is read whole.
_ANSWER_WINDOW = 200


def _recent(client: Any, conversation: str, thread: str, limit: int) -> dict[str, Any]:
    """The newest `limit` messages, oldest first, at top level or in `thread`.

    A thread is read whole and its tail kept: the thread route answers its
    OLDEST replies first, so a single read of a long thread misses the newest.
    """
    if not thread:
        return operations.read_messages(client, conversation, "", limit)
    replies = operations.read_thread(client, conversation, thread)
    return {"messages": replies[-limit:], "has_more": len(replies) > limit}


def _card_lines(card: dict[str, Any]) -> list[str]:
    where = f"  thread {card['thread_id']}" if card.get("thread_id") else ""
    latest = "  (open on the composer)" if card.get("latest") else ""
    lines = [f"message {card['message_id']}{where}{latest}"]
    for question in card["questions"]:
        pick = "pick any" if question["multi_select"] else "pick one"
        lines.append(
            f"  {question['index']}. [{question['header']}] {question['question']} ({pick})"
        )
        for option in question["options"]:
            desc = f" — {option['description']}" if option["description"] else ""
            lines.append(f"       {option['index']}) {option['label']}{desc}")
    return lines


def _questions(args: argparse.Namespace) -> None:
    from ..cli import _get_client, _output

    resp = _recent(_get_client(args), args.conversation, args.thread or "", args.limit or 50)
    messages = resp.get("messages", [])
    cards = q.pending_cards(messages)
    data = {"questions": cards, "scanned": len(messages), "has_more": bool(resp.get("has_more"))}
    if not cards:
        _output(args, data, f"No open questions in the last {len(messages)} messages.")
        return
    lines = [f"Open questions ({len(cards)}), newest first:", ""]
    for card in cards:
        lines += [*_card_lines(card), ""]
    lines.append(
        "Answer: popcorn message answer <channel> --message <id> "
        "[--question N] (--option N | --label TEXT | --custom TEXT | --skip)"
    )
    _output(args, data, "\n".join(lines))


def _selections(args: argparse.Namespace) -> dict[int, q.Selection]:
    """The `--answers` list, or the single question the flags name."""
    from ..cli import _resolve_data_arg

    flags = bool(args.option or args.label or args.custom is not None or args.skip)
    if args.answers is not None:
        if flags or args.question is not None:
            raise PopcornError(
                "--answers replaces --question, --option, --label, --custom and --skip"
            )
        try:
            spec = json.loads(_resolve_data_arg(args.answers))
        except json.JSONDecodeError as e:
            raise PopcornError(f"--answers is not valid JSON: {e}") from e
        return q.selections_from_json(spec)
    if not flags:
        raise PopcornError(
            "Say what to answer: --option N, --label TEXT, --custom TEXT or --skip",
            hint="popcorn message questions <channel>",
        )
    selection = q.Selection(
        options=[q.option_number(o, "--option") for o in args.option or []],
        labels=list(args.label or []),
        custom=args.custom,
        skip=args.skip,
    )
    return {args.question if args.question is not None else 1: selection}


def _answer(args: argparse.Namespace) -> None:
    from ..cli import _assume_yes, _confirm, _get_client, _output, _status

    client = _get_client(args)
    if args.thread:
        resp = {"messages": operations.read_thread(client, args.conversation, args.thread)}
    else:
        resp = operations.read_messages(client, args.conversation, "", _ANSWER_WINDOW)
    message = next((m for m in resp.get("messages", []) if m.get("id") == args.message), None)
    part = q.check_answerable(message, args.message)
    assert message is not None
    answers = q.build_answers(part, _selections(args))
    answers_part = q.answers_part(part, args.message, answers)
    # Posted where the question is: the server refuses answers in any other
    # thread, and a top-level question's answers go top level.
    thread_id = message.get("thread_id", args.thread)
    preview = ["Answer the agent's question:", *q.answer_lines(answers)]
    data: dict[str, Any] = {
        "question_message_id": args.message,
        "question_id": part.get("question_id"),
        "thread_id": thread_id or None,
        "answers": answers,
    }

    if args.dry_run:
        _output(args, {**data, "dry_run": True, "part": answers_part}, "\n".join(preview))
        return
    if not _assume_yes(args):
        # Straight to stderr, not `_status`: under --quiet the prompt would
        # otherwise ask to send answers it never showed.
        print("\n".join(preview), file=sys.stderr)
    if not _confirm(args, "Send?"):
        _status("Cancelled.")
        return
    sent = operations.send_parts(client, args.conversation, [answers_part], thread_id or "")
    # The server's echo of the message is not passed on: it carries the
    # answers' `metadata.answer_plan_ids`, which no member reads.
    data["message_id"] = (sent.get("message") or {}).get("id")
    _output(args, data, f"Answered (id: {data['message_id'] or '?'})\n" + "\n".join(preview[1:]))


register(
    Command(
        name="message",
        category="messages",
        description=(
            "Message commands (answer, delete, download, edit, get, list, questions, react, "
            "search, send, threads)"
        ),
        subcommands=[
            Subcommand(
                "answer",
                "Answer an agent's question card, as tapping it in the app does",
                _answer,
                [
                    _CHANNEL,
                    Argument(
                        "message",
                        "The question's message id (from `message questions`)",
                        required=True,
                    ),
                    Argument(
                        "thread",
                        "Thread the question was asked in (its root message id)",
                        type=str,
                    ),
                    Argument(
                        "question",
                        "Which question on the card the flags answer (default 1); "
                        "the others go as skipped, as in the app",
                        type=int,
                    ),
                    Argument(
                        "option",
                        "Pick option N (repeat on a pick-any question)",
                        action="append",
                    ),
                    Argument(
                        "label",
                        "Pick the option with this label (repeat on a pick-any question)",
                        action="append",
                    ),
                    Argument("custom", "Free-text answer, alone or beside a pick", type=str),
                    Argument("skip", "Skip the question", action="store_true"),
                    Argument(
                        "answers",
                        "Every question at once, as a JSON list (literal, @file or @-): "
                        '[{"option": 1}, {"label": ["A", "B"], "custom": "…"}, {"skip": true}]',
                        type=str,
                    ),
                    Argument(
                        "dry-run",
                        "Print the answers part that would be sent, and send nothing",
                        action="store_true",
                    ),
                ],
            ),
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
                "questions",
                "List an agent's unanswered question cards, numbered for `message answer`",
                _questions,
                [
                    _CHANNEL,
                    Argument("thread", "Thread ID to look in", type=str),
                    Argument("limit", "Recent messages to look through (default 50)", type=int),
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
