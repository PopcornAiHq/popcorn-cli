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
