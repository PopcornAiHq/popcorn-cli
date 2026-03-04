"""Tests for popcorn_core.resolve."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from popcorn_core.errors import PopcornError
from popcorn_core.resolve import _channel_cache, resolve_conversation


@pytest.fixture(autouse=True)
def _clear_cache():
    """Clear the channel cache between tests."""
    _channel_cache.clear()
    yield
    _channel_cache.clear()


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
