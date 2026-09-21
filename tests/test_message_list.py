"""Which way `message list` pages, and what it names as the next page.

History comes back oldest-first. The cursor used to be taken from the last
element of a page on the belief that the page was newest-first, so "the next
page older" actually asked for everything before the NEWEST message just
read — very nearly the page already in hand. An agent following
`data.pagination.next` re-read the same window each time, advancing about one
message per request instead of a page, with duplicates on every page. The
output looked right, which is the part an agent cannot catch for itself.

The fake mirrors the endpoints these tests care about: a history page is
ascending by `created_at`, `latest` and `oldest` are message ids meaning
strictly before and strictly after that message, `has_more` is a full page,
and the thread endpoint pages by offset and ignores the id cursors.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import pytest

from popcorn_cli import cli
from popcorn_core import operations

# A followed cursor should exhaust a stubbed history in a handful of requests.
# The bug this file pins advanced one message per request, so an unbounded
# loop would run as long as the history is tall.
_MAX_REQUESTS = 12


def _mid(index: int) -> str:
    return f"00000000-0000-4000-8000-{index:012d}"


def _msg(index: int) -> dict[str, Any]:
    """A history message carrying only what the pager and formatter read."""
    return {
        "id": _mid(index),
        "created_at": f"2026-01-01T{index // 3600:02d}:{index // 60 % 60:02d}:{index % 60:02d}Z",
        "content": {"parts": [{"type": "text", "content": f"message {index}"}]},
        "author": {"username": "example-user", "display_name": "Example User"},
    }


class FakeHistory:
    """The slice of the message endpoints that paging depends on."""

    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.messages = list(messages)
        self.calls: list[dict[str, Any]] = []

    def read(
        self,
        client: Any,
        conversation: str,
        thread_id: str = "",
        limit: int = 25,
        latest: str = "",
        oldest: str = "",
    ) -> dict[str, Any]:
        self.calls.append({"limit": limit, "latest": latest, "oldest": oldest, "thread": thread_id})
        if len(self.calls) > _MAX_REQUESTS:
            raise AssertionError("paging did not terminate")

        if thread_id:
            # The thread endpoint takes thread_ts/limit/offset only; the id
            # cursors reach it and are dropped, so every call answers the
            # same page.
            return {"ok": True, "messages": self.messages[:limit], "has_more": True}

        window = self.messages
        if latest:
            edge = self._anchor(latest)
            window = [m for m in window if m["created_at"] < edge]
        if oldest:
            edge = self._anchor(oldest)
            window = [m for m in window if m["created_at"] > edge]
        # Ascending either way: forward reads take the first `limit`, backward
        # reads take the newest `limit` and hand them back oldest-first.
        page = window[:limit] if oldest else window[-limit:] if limit else []
        return {"ok": True, "messages": page, "has_more": len(page) == limit}

    def _anchor(self, message_id: str) -> str:
        found = next((m for m in self.messages if m["id"] == message_id), None)
        if found is None:
            raise AssertionError(f"cursor named a message the history does not hold: {message_id}")
        return str(found["created_at"])


@pytest.fixture()
def page(monkeypatch, mock_client, capsys):
    """Run `message list` against a stubbed history, and hand back its envelope."""

    def _run(history: FakeHistory, argv: list[str]) -> dict[str, Any]:
        monkeypatch.setattr(cli, "_check_and_update", lambda: None)
        monkeypatch.setattr(cli, "_get_client", lambda args: mock_client)
        monkeypatch.setattr(operations, "read_messages", history.read)
        monkeypatch.setattr(sys, "argv", ["popcorn", "message", "list", *argv, "--json"])
        cli.main()
        return dict(json.loads(capsys.readouterr().out)["data"])

    return _run


def _follow(page, history: FakeHistory, argv: list[str]) -> list[str]:
    """Walk `pagination.next` the way SPEC.md tells an agent to, once per page."""
    ids: list[str] = []
    flags: list[str] = []
    while True:
        data = page(history, [*argv, *flags])
        ids.extend(m["id"] for m in data["messages"])
        nxt = data["pagination"]["next"]
        if nxt is None:
            return ids
        flags = [part for key, value in nxt.items() for part in (f"--{key}", value)]


class TestBackwardPaging:
    def test_the_next_page_resumes_at_the_oldest_message_read(self, page):
        history = FakeHistory([_msg(i) for i in range(1, 61)])

        data = page(history, ["#example-channel", "--limit", "10"])

        messages = data["messages"]
        oldest = min(messages, key=lambda m: m["created_at"])
        assert data["pagination"]["next"] == {"before": oldest["id"]}
        # Stated by position too: the page is oldest-first, so that is its head.
        assert oldest is messages[0]

    def test_following_the_cursor_reads_each_message_exactly_once(self, page):
        """The regression. Anchoring on the newest message re-read the same
        window, so the walk both duplicated and crawled."""
        history = FakeHistory([_msg(i) for i in range(1, 121)])

        ids = _follow(page, history, ["#example-channel", "--limit", "50"])

        assert sorted(ids) == sorted(_mid(i) for i in range(1, 121))
        assert len(ids) == len(set(ids))
        # 120 messages at 50 a page: two full pages, then a short one that ends it.
        assert [c["latest"] for c in history.calls] == ["", _mid(71), _mid(21)]

    def test_a_history_that_ends_names_no_next_page(self, page):
        history = FakeHistory([_msg(1), _msg(2)])

        data = page(history, ["#example-channel", "--limit", "50"])

        assert [m["id"] for m in data["messages"]] == [_mid(1), _mid(2)]
        assert data["pagination"]["next"] is None

    def test_an_empty_channel_names_no_next_page(self, page):
        data = page(FakeHistory([]), ["#example-channel", "--limit", "50"])

        assert data["messages"] == []
        assert data["pagination"]["next"] is None


class TestForwardPaging:
    def test_after_keeps_walking_forward_rather_than_turning_around(self, page):
        """`--after` asks for what followed a message, so its next page has to
        resume at the newest one read. A `before` cursor here would send the
        walk back over history the caller has already passed."""
        history = FakeHistory([_msg(i) for i in range(1, 121)])

        data = page(history, ["#example-channel", "--limit", "50", "--after", _mid(1)])

        messages = data["messages"]
        newest = max(messages, key=lambda m: m["created_at"])
        assert data["pagination"]["next"] == {"after": newest["id"]}
        assert newest is messages[-1]

    def test_following_a_forward_cursor_reads_each_message_exactly_once(self, page):
        history = FakeHistory([_msg(i) for i in range(1, 121)])

        ids = _follow(page, history, ["#example-channel", "--limit", "50", "--after", _mid(1)])

        assert sorted(ids) == sorted(_mid(i) for i in range(2, 121))
        assert len(ids) == len(set(ids))
        assert [c["oldest"] for c in history.calls] == [_mid(1), _mid(51), _mid(101)]


class TestThreadReplies:
    def test_a_thread_read_names_no_next_page(self, page):
        """The thread endpoint pages by an offset this command does not
        expose. Naming an id cursor would hand a caller the same page for as
        long as it followed it."""
        history = FakeHistory([_msg(i) for i in range(1, 121)])

        data = page(history, ["#example-channel", "--thread", _mid(1), "--limit", "50"])

        assert len(data["messages"]) == 50
        assert data["has_more"] is True
        assert data["pagination"]["next"] is None


class TestNextPageHelper:
    def test_a_page_without_ids_names_no_next_page(self):
        assert (
            cli._messages_next_page(
                [{"created_at": "2026-01-01T00:00:00Z"}],
                has_more=True,
                thread=False,
                forward=False,
            )
            is None
        )

    def test_has_more_gates_the_cursor(self):
        assert (
            cli._messages_next_page([_msg(1)], has_more=False, thread=False, forward=False) is None
        )
        assert cli._messages_next_page([_msg(1)], has_more=True, thread=False, forward=False) == {
            "before": _mid(1)
        }
