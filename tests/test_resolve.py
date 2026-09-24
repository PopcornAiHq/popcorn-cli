"""Tests for popcorn_core.resolve."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from popcorn_core.errors import PopcornError
from popcorn_core.resolve import _channel_cache, _user_cache, resolve_conversation, resolve_user


@pytest.fixture(autouse=True)
def _clear_cache():
    """Clear the resolution caches between tests."""
    _channel_cache.clear()
    _user_cache.clear()
    yield
    _channel_cache.clear()
    _user_cache.clear()


class TestResolveConversation:
    def test_uuid_passthrough(self):
        client = MagicMock()
        uuid = "12345678-1234-1234-1234-123456789012"
        assert resolve_conversation(client, uuid) == uuid
        client.get.assert_not_called()

    def test_channel_name_with_hash(self, mock_client):
        mock_client.get.return_value = {
            "conversations": [
                {"id": "conv-001", "name": "general"},
                {"id": "conv-002", "name": "random"},
            ]
        }
        assert resolve_conversation(mock_client, "#general") == "conv-001"

    def test_channel_name_without_hash(self, mock_client):
        mock_client.get.return_value = {"conversations": [{"id": "conv-001", "name": "general"}]}
        assert resolve_conversation(mock_client, "general") == "conv-001"

    def test_channel_name_case_insensitive(self, mock_client):
        mock_client.get.return_value = {"conversations": [{"id": "conv-001", "name": "General"}]}
        assert resolve_conversation(mock_client, "#GENERAL") == "conv-001"

    def test_channel_not_found(self, mock_client):
        mock_client.get.return_value = {"conversations": []}
        with pytest.raises(PopcornError, match="Channel not found"):
            resolve_conversation(mock_client, "#nonexistent")

    def test_caches_result(self, mock_client):
        mock_client.get.return_value = {"conversations": [{"id": "conv-001", "name": "general"}]}
        resolve_conversation(mock_client, "#general")
        resolve_conversation(mock_client, "#general")
        # Only one API call — second hit cache
        assert mock_client.get.call_count == 1

    def test_asks_for_archived_and_hidden(self, mock_client):
        """Naming a channel means that channel, whatever its visibility."""
        mock_client.get.return_value = {"conversations": [{"id": "conv-001", "name": "general"}]}
        resolve_conversation(mock_client, "#general")
        _, params = mock_client.get.call_args[0]
        assert params["exclude_hidden"] == "false"
        assert params["exclude_archived"] == "false"

    def test_hidden_channel_resolves(self, mock_client):
        """A hidden channel is only in the response when the switch is sent."""

        def _list(_path, params):
            # The server excludes hidden conversations when the switch is
            # absent, so the default here is "true", not None.
            convs = [{"id": "conv-002", "name": "example-hidden", "is_hidden": True}]
            visible = [] if params.get("exclude_hidden", "true") == "true" else convs
            return {"conversations": visible}

        mock_client.get.side_effect = _list
        assert resolve_conversation(mock_client, "#example-hidden") == "conv-002"

    def test_archived_channel_resolves(self, mock_client):
        """Archived is the server's default, so this guards against the CLI
        excluding it — the switch is sent explicitly rather than left to a
        default that points the other way from the hidden one."""

        def _list(_path, params):
            convs = [{"id": "conv-003", "name": "example-archived", "is_archived": True}]
            visible = [] if params.get("exclude_archived") == "true" else convs
            return {"conversations": visible}

        mock_client.get.side_effect = _list
        assert resolve_conversation(mock_client, "#example-archived") == "conv-003"

    def test_exact_match_preferred_over_case_variant(self, mock_client):
        """The variant comes first in the listing and must not win."""
        mock_client.get.return_value = {
            "conversations": [
                {"id": "conv-lower", "name": "ops"},
                {"id": "conv-upper", "name": "Ops"},
            ]
        }
        assert resolve_conversation(mock_client, "#Ops") == "conv-upper"

    def test_ambiguous_case_names_both_candidates(self, mock_client):
        """Neither spelling is exact, and picking one would be a silent guess."""
        mock_client.get.return_value = {
            "conversations": [
                {"id": "conv-lower", "name": "ops"},
                {"id": "conv-upper", "name": "Ops"},
            ]
        }
        with pytest.raises(PopcornError, match="matches more than one channel") as excinfo:
            resolve_conversation(mock_client, "#OPS")
        assert "conv-lower" in str(excinfo.value)
        assert "conv-upper" in str(excinfo.value)

    def test_cache_does_not_collapse_case_variants(self, mock_client):
        """A cache keyed on the folded name would answer #Ops with #ops."""
        mock_client.get.return_value = {
            "conversations": [
                {"id": "conv-lower", "name": "ops"},
                {"id": "conv-upper", "name": "Ops"},
            ]
        }
        assert resolve_conversation(mock_client, "#ops") == "conv-lower"
        assert resolve_conversation(mock_client, "#Ops") == "conv-upper"


_USERS = {
    "users": [
        {
            "id": "00000000-0000-4000-8000-000000000001",
            "username": "example-ana",
            "email": "ana@example.com",
            "display_name": "Ana Example",
        },
        {
            "id": "00000000-0000-4000-8000-000000000002",
            "username": "example-bo",
            "email": "bo@example.com",
            "display_name": "Bo Example",
        },
    ]
}


class TestResolveUser:
    def test_uuid_passthrough(self):
        client = MagicMock()
        uuid = "00000000-0000-4000-8000-000000000001"
        assert resolve_user(client, uuid) == uuid
        client.get.assert_not_called()

    def test_by_username(self, mock_client):
        mock_client.get.return_value = _USERS
        assert resolve_user(mock_client, "example-ana") == "00000000-0000-4000-8000-000000000001"

    def test_leading_at_is_stripped(self, mock_client):
        mock_client.get.return_value = _USERS
        assert resolve_user(mock_client, "@example-bo") == "00000000-0000-4000-8000-000000000002"

    def test_by_email(self, mock_client):
        mock_client.get.return_value = _USERS
        assert resolve_user(mock_client, "BO@example.com") == (
            "00000000-0000-4000-8000-000000000002"
        )

    def test_by_display_name(self, mock_client):
        mock_client.get.return_value = _USERS
        assert resolve_user(mock_client, "ana example") == ("00000000-0000-4000-8000-000000000001")

    def test_partial_handle_does_not_match(self, mock_client):
        """A substring match would silently widen the filter to someone else."""
        mock_client.get.return_value = _USERS
        with pytest.raises(PopcornError, match="User not found"):
            resolve_user(mock_client, "example")

    def test_not_found(self, mock_client):
        mock_client.get.return_value = {"users": []}
        with pytest.raises(PopcornError, match="User not found"):
            resolve_user(mock_client, "nobody")

    def test_ambiguous_names_both_candidates(self, mock_client):
        mock_client.get.return_value = {
            "users": [
                {"id": "00000000-0000-4000-8000-000000000001", "display_name": "Sam"},
                {"id": "00000000-0000-4000-8000-000000000002", "username": "sam"},
            ]
        }
        with pytest.raises(PopcornError, match="matches more than one user") as excinfo:
            resolve_user(mock_client, "sam")
        assert "00000000-0000-4000-8000-000000000001" in str(excinfo.value)
        assert "00000000-0000-4000-8000-000000000002" in str(excinfo.value)

    def test_one_user_matched_twice_is_not_ambiguous(self, mock_client):
        """Handles collide on ONE user — an id set, not a row count, decides."""
        mock_client.get.return_value = {
            "users": [
                {
                    "id": "00000000-0000-4000-8000-000000000003",
                    "username": "kim",
                    "display_name": "kim",
                }
            ]
        }
        assert resolve_user(mock_client, "kim") == "00000000-0000-4000-8000-000000000003"

    def test_caches_result(self, mock_client):
        mock_client.get.return_value = _USERS
        resolve_user(mock_client, "example-ana")
        resolve_user(mock_client, "example-ana")
        assert mock_client.get.call_count == 1


class TestResolveByName:
    """The server answers the lookup; these pin how few requests it takes and
    that its filter is never trusted as the match itself."""

    @staticmethod
    def _server(*convs: dict):
        """A listing that honours `name=` (exact) and `query=` (folded substring)."""

        def _list(_path, params):
            rows = list(convs)
            if "name" in params:
                rows = [c for c in rows if c["name"] == params["name"]]
            if "query" in params:
                rows = [c for c in rows if params["query"].lower() in c["name"].lower()]
            return {"conversations": rows, "response_metadata": {"next_cursor": ""}}

        return _list

    def test_an_exact_name_is_one_request_by_name(self, mock_client):
        mock_client.get.side_effect = self._server({"id": "conv-001", "name": "general"})
        assert resolve_conversation(mock_client, "#general") == "conv-001"
        assert mock_client.get.call_count == 1
        _, params = mock_client.get.call_args[0]
        assert params["name"] == "general"
        assert "query" not in params

    def test_a_case_variant_costs_one_more_request_not_a_walk(self, mock_client):
        mock_client.get.side_effect = self._server({"id": "conv-001", "name": "General"})
        assert resolve_conversation(mock_client, "#general") == "conv-001"
        assert mock_client.get.call_count == 2
        _, params = mock_client.get.call_args[0]
        assert params["query"] == "general"
        assert "name" not in params

    def test_the_fallback_keeps_visibility_switches(self, mock_client):
        mock_client.get.side_effect = self._server({"id": "conv-001", "name": "General"})
        resolve_conversation(mock_client, "#general")
        for call in mock_client.get.call_args_list:
            assert call[0][1]["exclude_hidden"] == "false"
            assert call[0][1]["exclude_archived"] == "false"

    def test_a_substring_hit_is_not_a_match(self, mock_client):
        """`query=` is a substring search; #ops must not resolve to #devops."""
        mock_client.get.side_effect = self._server({"id": "conv-001", "name": "devops"})
        with pytest.raises(PopcornError, match="Channel not found"):
            resolve_conversation(mock_client, "#ops")

    def test_two_channels_with_the_identical_name_raise(self, mock_client):
        """A channel shared in from another workspace can carry a local name."""
        mock_client.get.side_effect = self._server(
            {"id": "conv-local", "name": "ops"}, {"id": "conv-shared", "name": "ops"}
        )
        with pytest.raises(PopcornError, match="matches more than one channel") as excinfo:
            resolve_conversation(mock_client, "#ops")
        assert "conv-local" in str(excinfo.value)
        assert "conv-shared" in str(excinfo.value)

    def test_a_server_ignoring_the_filter_does_not_pick_the_top_row(self, mock_client):
        """An API that predates the parameters drops them silently and serves
        an unfiltered page; trusting it would resolve to whatever sits first."""
        mock_client.get.return_value = {
            "conversations": [
                {"id": "conv-top", "name": "random"},
                {"id": "conv-001", "name": "general"},
            ]
        }
        assert resolve_conversation(mock_client, "#general") == "conv-001"

    def test_an_empty_name_asks_nothing(self, mock_client):
        """An empty `query=` is no filter at all, so it would walk everything."""
        with pytest.raises(PopcornError, match="Channel not found"):
            resolve_conversation(mock_client, "#")
        mock_client.get.assert_not_called()

    def test_an_overlong_name_is_not_found_without_asking(self, mock_client):
        """The server answers an over-long `name=` with a 422, which would turn
        a miss into a validation error naming a query parameter."""
        with pytest.raises(PopcornError, match="Channel not found") as excinfo:
            resolve_conversation(mock_client, "#" + "a" * 256)
        assert excinfo.value.error_code == "not_found"
        mock_client.get.assert_not_called()

    def test_a_case_variant_is_cached_under_the_spelling_asked(self, mock_client):
        mock_client.get.side_effect = self._server({"id": "conv-001", "name": "General"})
        resolve_conversation(mock_client, "#general")
        resolve_conversation(mock_client, "#general")
        assert mock_client.get.call_count == 2
