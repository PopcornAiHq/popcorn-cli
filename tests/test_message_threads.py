"""`message threads` reads a message's parts the way `message list` does.

The thread listing walked ``parent_message["content"]`` as if it were the
list of parts. The server sends ``content`` as ``{"parts": [...]}``, so the
loop walked the dict's keys and called ``.get`` on the string ``"parts"`` —
every thread with a parent message crashed the text listing, while
``message list --thread`` on the same channel, which reads
``content["parts"]``, worked. It also read a text part's words from
``text``, where the server keeps them under ``content``, so the preview
would have been empty even without the crash.

The fixture thread is shaped like an agent's reply: a text part, the tool
calls stored as an ``s3`` part with no text, a closing ``agentFinalized``
part, and a bare string part, which the server never sends but the reader
must not crash on.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import pytest

from popcorn_cli import cli
from popcorn_cli.formatting import format_message_text, message_parts, part_text, set_color
from popcorn_core import operations


def _uuid(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


_S3_PART = {
    "id": _uuid(90),
    "type": "s3",
    "s3_key": "example-bucket/example-key.json",
    "size_bytes": 2048,
    "part_types": ["toolCall", "toolResult"],
    "parts_count": 4,
    "metadata": {},
}

_MIXED_PARTS: list[Any] = [
    _S3_PART,
    {"id": _uuid(91), "type": "text", "content": "Deployed the\nworkflow.", "format": "markdown"},
    "and a bare string part",
    {"id": _uuid(92), "type": "agentFinalized", "success": True},
]


def _message(n: int, parts: list[Any], author: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "id": _uuid(n),
        "conversation_id": _uuid(1),
        "author": author,
        "created_at": "2026-01-01T00:00:00Z",
        "content": {"parts": parts},
    }


_AGENT = {"username": "example-agent", "display_name": "Example Agent"}


def _threads() -> dict[str, Any]:
    return {
        "ok": True,
        "threads": [
            {
                "parent_message": _message(10, _MIXED_PARTS, _AGENT),
                "reply_count": 2,
                "last_reply_at": "2026-01-01T00:05:00Z",
                "participants": [_uuid(2)],
            },
            {
                # Only tool calls, no words; and an author the server could not resolve.
                "parent_message": _message(11, [_S3_PART], None),
                "reply_count": 1,
                "last_reply_at": "2026-01-01T00:06:00Z",
                "participants": [],
            },
        ],
    }


@pytest.fixture()
def run(monkeypatch, mock_client, capsys):
    def _run(*flags: str) -> str:
        monkeypatch.setattr(cli, "_check_and_update", lambda: None)
        monkeypatch.setattr(cli, "_get_client", lambda args: mock_client)
        monkeypatch.setattr(operations, "list_threads", lambda *a, **kw: _threads())
        argv = ["popcorn", "message", "threads", "#example-channel", "--no-color", *flags]
        monkeypatch.setattr(sys, "argv", argv)
        cli.main()
        return str(capsys.readouterr().out)

    return _run


class TestThreadsListing:
    def test_mixed_parts_do_not_crash_and_preview_the_first_text(self, run):
        out = run()
        lines = out.splitlines()
        assert len(lines) == 2
        assert _uuid(10) in lines[0]
        assert "Example Agent: Deployed the workflow." in lines[0]

    def test_thread_with_no_text_part_and_no_author(self, run):
        line = run().splitlines()[1]
        assert _uuid(11) in line
        assert line.endswith("?: ")

    def test_json_passes_parts_through_untouched(self, run):
        data = json.loads(run("--json"))["data"]
        assert data["threads"][0]["parent_message"]["content"]["parts"] == _MIXED_PARTS


class TestSharedPartReader:
    """`message list`, `get`, `search`, `--watch` and the inbox all go through these."""

    def test_list_renders_the_same_parts(self):
        set_color(False)
        msg = _message(10, _MIXED_PARTS, _AGENT)
        assert format_message_text(msg) == "Deployed the\nworkflow. and a bare string part"

    def test_s3_part_alone_has_no_text(self):
        set_color(False)
        assert "no text content" in format_message_text(_message(11, [_S3_PART], None))

    def test_text_part_with_null_content(self):
        assert part_text({"type": "text", "content": None}) == ""

    @pytest.mark.parametrize(
        ("content", "expected"),
        [
            ({"parts": _MIXED_PARTS}, _MIXED_PARTS),
            ({"parts": None}, []),
            ({}, []),
            (None, []),
            ([{"type": "text", "content": "x"}], [{"type": "text", "content": "x"}]),
            ("plain", ["plain"]),
            (42, []),
        ],
    )
    def test_message_parts_tolerates_every_content_shape(self, content, expected):
        assert message_parts({"content": content}) == expected

    @pytest.mark.parametrize("part", [_S3_PART, {"type": "agentFinalized"}, None, 7, ["x"]])
    def test_non_text_parts_carry_no_text(self, part):
        assert part_text(part) is None
