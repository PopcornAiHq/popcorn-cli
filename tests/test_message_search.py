"""`popcorn message search` — parsed flags reaching the search operation.

The flags this covers are named for how they read on the command line, so two
of their dests (`in`, `from`) are Python keywords the handler can only read
through `getattr`. A misspelled dest there does not raise: it falls back to the
default and the filter is silently dropped, which looks exactly like a search
that legitimately matched everything.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from popcorn_cli.cli import build_parser, cmd_search_messages


@pytest.fixture(scope="module")
def parser():
    return build_parser()


def _search(parser, argv: list[str]) -> dict:
    """Parse `argv` and capture the keywords the operation is called with."""
    args = parser.parse_args(["message", "search", *argv])
    captured: dict = {}

    def _record(_client, query, **kwargs):
        captured.update(query=query, **kwargs)
        return {"messages": []}

    with (
        patch("popcorn_cli.cli._get_client", return_value=object()),
        patch("popcorn_cli.cli._output", lambda *a, **k: None),
        patch("popcorn_core.operations.search_messages", side_effect=_record),
    ):
        cmd_search_messages(args)
    return captured


class TestFiltersReachTheOperation:
    def test_every_filter_is_forwarded(self, parser):
        got = _search(
            parser,
            [
                "deploy",
                "--in",
                "#ops",
                "--from",
                "example-ana",
                "--since",
                "2026-01-01",
                "--until",
                "2026-02-01",
                "--has",
                "link",
                "--sort",
                "date_desc",
            ],
        )
        assert got["query"] == "deploy"
        assert got["conversations"] == "#ops"
        assert got["from_users"] == "example-ana"
        assert got["created_after"] == "2026-01-01"
        assert got["created_before"] == "2026-02-01"
        assert got["has"] == "link"
        assert got["sort_by"] == "date_desc"

    def test_unset_filters_arrive_empty(self, parser):
        got = _search(parser, ["deploy"])
        for key in (
            "conversations",
            "from_users",
            "created_after",
            "created_before",
            "has",
            "sort_by",
        ):
            assert got[key] == ""

    def test_a_filter_alone_reaches_the_operation(self, parser):
        """No positional query — the operation decides whether that is allowed."""
        got = _search(parser, ["--in", "#ops"])
        assert got["query"] == ""
        assert got["conversations"] == "#ops"

    def test_limit_and_offset_keep_their_defaults(self, parser):
        got = _search(parser, ["deploy"])
        assert got["limit"] == 50
        assert got["offset"] == 0


class TestSortIsConstrainedAtParseTime:
    def test_an_unknown_sort_is_rejected_before_the_network(self, parser):
        with pytest.raises(SystemExit):
            parser.parse_args(["message", "search", "x", "--sort", "newest"])

    @pytest.mark.parametrize("option", ["relevance", "date_asc", "date_desc"])
    def test_every_documented_sort_parses(self, parser, option):
        args = parser.parse_args(["message", "search", "x", "--sort", option])
        assert args.sort == option
