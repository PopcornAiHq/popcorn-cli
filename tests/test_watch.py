"""What `--watch` decides is new.

The watch loop used to fetch a page of recent history every few seconds and
scan it for the message it printed last. That scan reads "the cursor is not on
this page" as "every message on this page is new" — which is what happens when
a burst outruns one page, and what happens again when the cursor's message is
deleted. These stub the history endpoint so both cases are reachable without a
channel that receives fifty messages in three seconds.

The stub mirrors the server's contract: a page is ascending by `created_at`,
without a cursor it is the newest `limit`, with `oldest` it is the first
`limit` strictly after that message, and an `oldest` the server cannot resolve
is a 400 rather than an empty page.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

import pytest

from popcorn_cli import cli
from popcorn_core import operations
from popcorn_core.errors import APIError

# Guards against a test bug turning into a hung suite: the loop only ever ends
# on --count / --max-wait, so an assertion about the wrong thing would spin.
_MAX_POLLS = 40


def _mid(index: int) -> str:
    return f"00000000-0000-4000-8000-{index:012d}"


def _msg(index: int) -> dict[str, Any]:
    """A history message carrying only what the watch loop reads."""
    return {
        "id": _mid(index),
        "created_at": f"2026-01-01T{index // 3600:02d}:{index // 60 % 60:02d}:{index % 60:02d}Z",
        "content": {"parts": [{"type": "text", "content": f"message {index}"}]},
        "author": {"username": "example-user", "display_name": "Example User"},
    }


class FakeHistory:
    """The slice of the history endpoint that `--watch` depends on."""

    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.messages = list(messages)
        self.calls: list[dict[str, Any]] = []
        # Applied before the Nth read, so a test can delete or add messages
        # between polls the way the world does.
        self.before_call: dict[int, Any] = {}
        # Raised from the Nth read, for the failures that are not about the
        # cursor. KeyboardInterrupt stands in for Ctrl+C.
        self.raise_on: dict[int, BaseException] = {}

    def read(
        self,
        client: Any,
        conversation: str,
        thread_id: str = "",
        limit: int = 25,
        latest: str = "",
        oldest: str = "",
    ) -> dict[str, Any]:
        mutate = self.before_call.get(len(self.calls))
        if mutate:
            mutate(self)
        self.calls.append({"limit": limit, "oldest": oldest})
        if len(self.calls) > _MAX_POLLS:
            raise AssertionError("watch loop did not terminate")
        failure = self.raise_on.get(len(self.calls) - 1)
        if failure:
            raise failure

        if oldest:
            anchor = next((m for m in self.messages if m["id"] == oldest), None)
            if anchor is None:
                raise APIError(
                    "Invalid oldest: message not found or not accessible",
                    status_code=400,
                )
            page = [m for m in self.messages if m["created_at"] > anchor["created_at"]][:limit]
        else:
            page = self.messages[-limit:] if limit else []
        return {"ok": True, "messages": page, "has_more": False}


@pytest.fixture()
def watch(monkeypatch, mock_client, capsys):
    """Run `cmd_watch` against a stubbed history, and hand back the ids printed."""

    def _run(history: FakeHistory, count: int = 0, **overrides: Any) -> list[str]:
        monkeypatch.setattr(cli, "_get_client", lambda args: mock_client)
        monkeypatch.setattr(operations, "read_messages", history.read)
        monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
        args = argparse.Namespace(
            conversation="#example-channel",
            interval=3,
            count=count,
            max_wait=None,
            json=True,
            **overrides,
        )
        cli.cmd_watch(args)
        out = capsys.readouterr().out
        return [json.loads(line)["data"]["id"] for line in out.splitlines() if line.strip()]

    return _run


class TestWatch:
    def test_the_poll_asks_for_what_followed_the_last_message(self, watch):
        history = FakeHistory([_msg(1)])
        history.before_call[1] = lambda h: h.messages.append(_msg(2))

        assert watch(history, count=1) == [_mid(2)]
        assert history.calls[0] == {"limit": 1, "oldest": ""}
        assert history.calls[1]["oldest"] == _mid(1)

    def test_a_burst_larger_than_one_page_prints_each_message_once(self, watch):
        """The original bug. A page-sized scan cannot find the cursor once the
        burst has pushed it off the page, and reads the whole page as new."""
        history = FakeHistory([_msg(0)])
        burst = [_msg(i) for i in range(1, 121)]
        history.before_call[1] = lambda h: h.messages.extend(burst)

        printed = watch(history, count=len(burst))

        assert printed == [m["id"] for m in burst]
        assert len(printed) == len(set(printed))
        # Caught up a page at a time, each poll resuming where the last ended.
        assert [c["oldest"] for c in history.calls[1:]] == [_mid(0), _mid(50), _mid(100)]

    def test_a_deleted_cursor_re_anchors_instead_of_replaying_history(self, watch):
        """The server cannot resolve a deleted message id, so it refuses the
        request. Falling back to unfiltered history is only safe if what was
        already printed is filtered out by timestamp."""
        history = FakeHistory([_msg(i) for i in range(1, 6)])

        def delete_the_cursor(h: FakeHistory) -> None:
            h.messages = [m for m in h.messages if m["id"] != _mid(5)]
            h.messages.append(_msg(6))

        history.before_call[1] = delete_the_cursor
        history.before_call[3] = lambda h: h.messages.append(_msg(7))

        printed = watch(history, count=2)

        assert printed == [_mid(6), _mid(7)]
        # Poll 1 was refused, retried without a cursor; poll 2 anchors on what
        # that page ended with, so the watch keeps running.
        assert [c["oldest"] for c in history.calls] == ["", _mid(5), "", _mid(6)]

    def test_the_cursor_fallback_does_not_swallow_an_unrelated_failure(self, watch):
        history = FakeHistory([_msg(1)])
        history.raise_on[1] = APIError("Service unavailable", status_code=503)

        with pytest.raises(APIError, match="Service unavailable"):
            watch(history, count=1)

    def test_an_empty_channel_prints_what_arrives_after_the_watch_starts(self, watch):
        history = FakeHistory([])
        history.before_call[1] = lambda h: h.messages.append(_msg(1))

        assert watch(history, count=1) == [_mid(1)]
        assert history.calls[1]["oldest"] == ""

    def test_a_quiet_channel_prints_nothing(self, watch):
        history = FakeHistory([_msg(1)])
        history.raise_on[3] = KeyboardInterrupt()

        assert watch(history, count=1) == []
        assert [c["oldest"] for c in history.calls] == ["", _mid(1), _mid(1), _mid(1)]


class TestWatchHelpers:
    def test_the_anchor_is_the_newest_message_in_the_page(self):
        page = [_msg(1), _msg(2), _msg(3)]
        assert cli._watch_anchor(page) == (_mid(3), page[-1]["created_at"])
        assert cli._watch_anchor([]) == (None, None)

    def test_an_unusable_cutoff_yields_nothing_rather_than_everything(self):
        """A gap of one message beats replaying a page the reader has seen."""
        page = [_msg(1), _msg(2)]
        assert cli._watch_newer_than(page, None) == []
        assert cli._watch_newer_than(page, "not-a-timestamp") == []

    def test_only_a_refused_cursor_counts_as_a_stale_anchor(self):
        assert cli._watch_stale_anchor(
            APIError("Invalid oldest: message not found or not accessible", status_code=400)
        )
        assert not cli._watch_stale_anchor(APIError("Invalid limit: too large", status_code=400))
        assert not cli._watch_stale_anchor(APIError("Gateway timeout", status_code=504))
