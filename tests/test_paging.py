"""Following the listing cursors.

Every workspace on hand is smaller than one page, which is exactly why the
single-page listings looked correct for so long. These stub the client instead,
so the boundary that nobody's workspace reaches is the one under test.
"""

from __future__ import annotations

import argparse
from typing import Any

import pytest

from popcorn_core import operations, paging
from popcorn_core.errors import PopcornError
from popcorn_core.resolve import _channel_cache, _user_cache, resolve_conversation, resolve_user


@pytest.fixture(autouse=True)
def _clear_cache():
    _channel_cache.clear()
    _user_cache.clear()
    yield
    _channel_cache.clear()
    _user_cache.clear()


def _page(key: str, items: list[dict[str, Any]], next_cursor: str = "") -> dict[str, Any]:
    return {key: items, "response_metadata": {"next_cursor": next_cursor}}


def _named(key: str, *names: str) -> list[dict[str, Any]]:
    return [{"id": f"{key}-{n}", "name": n} for n in names]


class TestFetchAll:
    def test_one_short_page_is_one_request(self, mock_client):
        mock_client.get.return_value = _page("conversations", _named("conv", "general"))
        assert (
            len(paging.fetch_all(mock_client, "/api/conversations/list", {}, "conversations")) == 1
        )
        assert mock_client.get.call_count == 1

    def test_a_response_without_metadata_still_terminates(self, mock_client):
        """Not every listing response carries `response_metadata`, and a missing
        cursor has to read as "that was the last page", not as an error."""
        mock_client.get.return_value = {"conversations": _named("conv", "general")}
        assert paging.fetch_all(mock_client, "/api/conversations/list", {}, "conversations") == [
            {"id": "conv-general", "name": "general"}
        ]
        assert mock_client.get.call_count == 1

    def test_a_full_page_is_followed_even_when_the_next_one_is_empty(self, mock_client):
        """A page filled to the server's limit says nothing on its own; only an
        empty `next_cursor` does. Stopping at a full page is the original bug."""
        full = _named("conv", *(f"c{i}" for i in range(paging.PAGE_LIMIT)))
        mock_client.get.side_effect = [
            _page("conversations", full, next_cursor=str(paging.PAGE_LIMIT)),
            _page("conversations", []),
        ]
        got = paging.fetch_all(mock_client, "/api/conversations/list", {}, "conversations")
        assert len(got) == paging.PAGE_LIMIT
        assert mock_client.get.call_count == 2

    def test_pages_accumulate_in_order(self, mock_client):
        mock_client.get.side_effect = [
            _page("conversations", _named("conv", "a", "b"), next_cursor="2"),
            _page("conversations", _named("conv", "c"), next_cursor="4"),
            _page("conversations", _named("conv", "d")),
        ]
        got = paging.fetch_all(mock_client, "/api/conversations/list", {}, "conversations")
        assert [c["name"] for c in got] == ["a", "b", "c", "d"]

    def test_the_cursor_is_sent_back_and_the_other_params_survive(self, mock_client):
        mock_client.get.side_effect = [
            _page("conversations", _named("conv", "a"), next_cursor="7"),
            _page("conversations", _named("conv", "b")),
        ]
        paging.fetch_all(mock_client, "/api/conversations/list", {"types": "dm"}, "conversations")
        first, second = [call[0][1] for call in mock_client.get.call_args_list]
        assert "cursor" not in first
        assert first["types"] == "dm" and first["limit"] == paging.PAGE_LIMIT
        assert second["cursor"] == "7"
        assert second["types"] == "dm" and second["limit"] == paging.PAGE_LIMIT

    def test_a_server_that_never_stops_does_not_hang_the_cli(self, mock_client):
        """The cursors here are all distinct, so only the page cap ends it."""
        counter = iter(range(10_000))
        mock_client.get.side_effect = lambda *_a, **_k: _page(
            "conversations", _named("conv", "x"), next_cursor=str(next(counter))
        )
        got = paging.fetch_all(mock_client, "/api/conversations/list", {}, "conversations")
        assert mock_client.get.call_count == paging.MAX_PAGES
        assert len(got) == paging.MAX_PAGES

    def test_a_repeated_cursor_stops_rather_than_looping(self, mock_client):
        mock_client.get.side_effect = lambda *_a, **_k: _page(
            "conversations", _named("conv", "x"), next_cursor="same"
        )
        paging.fetch_all(mock_client, "/api/conversations/list", {}, "conversations")
        assert mock_client.get.call_count == 2


class TestSearchPaging:
    def test_channel_listing_spans_pages(self, mock_client):
        mock_client.get.side_effect = [
            _page("conversations", _named("conv", "alpha", "beta"), next_cursor="2"),
            _page("conversations", _named("conv", "gamma")),
        ]
        got = operations.search_channels(mock_client)["conversations"]
        assert [c["name"] for c in got] == ["alpha", "beta", "gamma"]

    def test_the_name_filter_sees_every_page(self, mock_client):
        """Filtering per page would have dropped the match sitting on page two —
        the endpoints take no name query, so the filter has to run over the
        whole accumulated list."""
        mock_client.get.side_effect = [
            _page("conversations", _named("conv", "alpha"), next_cursor="1"),
            _page("conversations", _named("conv", "release-notes", "beta")),
        ]
        got = operations.search_channels(mock_client, "release")["conversations"]
        assert [c["name"] for c in got] == ["release-notes"]

    def test_dm_listing_spans_pages(self, mock_client):
        mock_client.get.side_effect = [
            _page("conversations", [{"id": "dm-1"}], next_cursor="1"),
            _page("conversations", [{"id": "dm-2"}]),
        ]
        got = operations.search_dms(mock_client)["conversations"]
        assert [c["id"] for c in got] == ["dm-1", "dm-2"]

    def test_user_listing_spans_pages(self, mock_client):
        mock_client.get.side_effect = [
            _page("users", [{"id": "u1", "username": "ada"}], next_cursor="1"),
            _page("users", [{"id": "u2", "username": "grace"}]),
        ]
        got = operations.search_users(mock_client, "grace")["users"]
        assert [u["id"] for u in got] == ["u2"]


class TestArchivedAndHidden:
    def test_archived_and_hidden_are_excluded_by_default(self, mock_client):
        """Archived channels are server-side opt-OUT, so they pad every listing
        and eat into the same page budget unless the CLI says otherwise."""
        mock_client.get.return_value = _page("conversations", [])
        operations.search_channels(mock_client)
        params = mock_client.get.call_args[0][1]
        assert params["exclude_archived"] == "true"
        assert params["exclude_hidden"] == "true"

    def test_the_flags_ask_for_them_back(self, mock_client):
        mock_client.get.return_value = _page("conversations", [])
        operations.search_channels(mock_client, include_archived=True, include_hidden=True)
        params = mock_client.get.call_args[0][1]
        assert params["exclude_archived"] == "false"
        assert params["exclude_hidden"] == "false"

    def test_dms_take_the_same_switches(self, mock_client):
        mock_client.get.return_value = _page("conversations", [])
        operations.search_dms(mock_client, include_archived=True)
        params = mock_client.get.call_args[0][1]
        assert params["exclude_archived"] == "false"
        assert params["exclude_hidden"] == "true"


class TestResolvePaging:
    def test_a_channel_on_a_later_page_resolves(self, mock_client):
        mock_client.get.side_effect = [
            _page("conversations", _named("conv", "alpha"), next_cursor="1"),
            _page("conversations", _named("conv", "general")),
        ]
        assert resolve_conversation(mock_client, "#general") == "conv-general"

    def test_resolution_stops_at_the_page_holding_the_match(self, mock_client):
        """Name resolution runs before most commands, so paying for pages past
        the answer would tax every one of them."""
        mock_client.get.side_effect = [
            _page("conversations", _named("conv", "general"), next_cursor="1"),
            _page("conversations", _named("conv", "later")),
        ]
        resolve_conversation(mock_client, "#general")
        assert mock_client.get.call_count == 1

    def test_a_missing_channel_is_still_not_found(self, mock_client):
        mock_client.get.side_effect = [
            _page("conversations", _named("conv", "alpha"), next_cursor="1"),
            _page("conversations", _named("conv", "beta")),
        ]
        with pytest.raises(PopcornError, match="Channel not found"):
            resolve_conversation(mock_client, "#nope")

    def test_a_user_on_a_later_page_resolves(self, mock_client):
        mock_client.get.side_effect = [
            _page("users", [{"id": "u1", "username": "ada"}], next_cursor="1"),
            _page("users", [{"id": "u2", "username": "grace"}]),
        ]
        assert resolve_user(mock_client, "@grace") == "u2"

    def test_ambiguity_is_detected_across_pages(self, mock_client):
        """Short-circuiting on the first page's match would resolve a name that
        two people answer to, which is the failure this check exists to stop."""
        mock_client.get.side_effect = [
            _page("users", [{"id": "u1", "username": "sam"}], next_cursor="1"),
            _page("users", [{"id": "u2", "email": "sam"}]),
        ]
        with pytest.raises(PopcornError, match="matches more than one user"):
            resolve_user(mock_client, "sam")


class TestCommandWiring:
    def test_channel_list_forwards_the_flags(self):
        from unittest.mock import patch

        from popcorn_cli import cli

        args = argparse.Namespace(
            query="", dms=False, include_archived=True, include_hidden=True, json=False
        )
        with (
            patch.object(cli, "_get_client", return_value=object()),
            patch.object(cli, "_output"),
            patch.object(
                operations, "search_channels", return_value={"conversations": []}
            ) as search,
        ):
            cli.cmd_channel_list(args)
        assert search.call_args.kwargs == {"include_archived": True, "include_hidden": True}

    def test_create_if_not_exists_looks_past_archived_and_hidden(self):
        """The server's name-uniqueness check ignores both, so a channel the CLI
        cannot see still takes the name — and `--if-not-exists` would report the
        duplicate as an error instead of returning the existing channel."""
        from unittest.mock import patch

        from popcorn_cli import cli

        args = argparse.Namespace(name="general", if_not_exists=True, json=False)
        with (
            patch.object(cli, "_get_client", return_value=object()),
            patch.object(cli, "_output"),
            patch.object(
                operations,
                "search_channels",
                return_value={"conversations": [{"id": "conv-1", "name": "general"}]},
            ) as search,
            patch.object(operations, "create_conversation") as create,
        ):
            cli.cmd_create_channel(args)
        assert search.call_args.kwargs == {"include_archived": True, "include_hidden": True}
        assert create.call_count == 0
