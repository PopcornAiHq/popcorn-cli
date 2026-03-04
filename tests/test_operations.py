"""Tests for popcorn_core.operations."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from popcorn_core import operations
from popcorn_core.errors import PopcornError


@pytest.fixture(autouse=True)
def _patch_resolve():
    """Bypass channel resolution — tests pass UUIDs directly."""
    with patch("popcorn_core.operations.resolve_conversation", side_effect=lambda _c, ref: ref):
        yield


class TestIdentity:
    def test_get_whoami(self, mock_client):
        mock_client.get.return_value = {"user": {"id": "u1", "email": "a@b.com"}}
        result = operations.get_whoami(mock_client)
        mock_client.get.assert_called_once_with("/api/users/me")
        assert result["user"]["id"] == "u1"


class TestSearch:
    def test_search_channels_no_query(self, mock_client):
        mock_client.get.return_value = {"conversations": [{"name": "general"}, {"name": "random"}]}
        result = operations.search_channels(mock_client)
        assert len(result["conversations"]) == 2

    def test_search_channels_with_query(self, mock_client):
        mock_client.get.return_value = {"conversations": [{"name": "general"}, {"name": "random"}]}
        result = operations.search_channels(mock_client, "gen")
        assert len(result["conversations"]) == 1
        assert result["conversations"][0]["name"] == "general"

    def test_search_messages_requires_query(self, mock_client):
        with pytest.raises(PopcornError, match="Query required"):
            operations.search_messages(mock_client, "")


class TestMessages:
    def test_read_messages(self, mock_client):
        mock_client.get.return_value = {"messages": [{"id": "m1"}]}
        result = operations.read_messages(mock_client, "conv-id")
        mock_client.get.assert_called_once_with(
            "/api/messages/history", {"limit": 25, "conversation_id": "conv-id"}
        )
        assert result["messages"][0]["id"] == "m1"

    def test_read_messages_thread(self, mock_client):
        mock_client.get.return_value = {"messages": []}
        operations.read_messages(mock_client, "conv-id", thread_id="t1", limit=10)
        mock_client.get.assert_called_once_with(
            "/api/messages/thread",
            {"thread_ts": "t1", "limit": 10, "conversation_id": "conv-id"},
        )

    def test_send_message_text(self, mock_client):
        mock_client.post.return_value = {"id": "m1"}
        result = operations.send_message(mock_client, "conv-id", "hello")
        mock_client.post.assert_called_once_with(
            "/api/messages/post",
            data={
                "conversation": "conv-id",
                "content": {"parts": [{"type": "text", "content": "hello"}]},
            },
        )
        assert result["id"] == "m1"

    def test_send_message_empty_raises(self, mock_client):
        with pytest.raises(PopcornError, match="Nothing to send"):
            operations.send_message(mock_client, "conv-id")

    def test_send_message_thread(self, mock_client):
        mock_client.post.return_value = {"id": "m1"}
        operations.send_message(mock_client, "conv-id", "hi", thread_id="t1")
        call_data = mock_client.post.call_args[1]["data"]
        assert call_data["thread_id"] == "t1"

    def test_add_reaction(self, mock_client):
        mock_client.post.return_value = {"ok": True}
        operations.add_reaction(mock_client, "conv-id", "msg-id", "thumbsup")
        mock_client.post.assert_called_once_with(
            "/api/messages/reactions-add",
            data={"conversation": "conv-id", "message": "msg-id", "emoji": "thumbsup"},
        )

    def test_edit_message(self, mock_client):
        mock_client.post.return_value = {"ok": True}
        operations.edit_message(mock_client, "conv-id", "msg-id", "updated")
        mock_client.post.assert_called_once_with(
            "/api/messages/edit",
            data={
                "conversation": "conv-id",
                "message": "msg-id",
                "content": {"parts": [{"type": "text", "content": "updated"}]},
            },
        )

    def test_delete_message(self, mock_client):
        mock_client.post.return_value = {"ok": True}
        operations.delete_message(mock_client, "conv-id", "msg-id")
        mock_client.post.assert_called_once_with(
            "/api/messages/delete",
            data={"conversation": "conv-id", "message": "msg-id"},
        )

    def test_get_message(self, mock_client):
        mock_client.get.return_value = {"id": "m1", "content": {}}
        result = operations.get_message(mock_client, "m1")
        mock_client.get.assert_called_once_with("/api/messages/get", {"message": "m1"})
        assert result["id"] == "m1"


class TestConversations:
    def test_create_conversation(self, mock_client):
        mock_client.post.return_value = {"id": "c1"}
        result = operations.create_conversation(mock_client, "test-channel")
        mock_client.post.assert_called_once_with(
            "/api/conversations/create",
            data={"name": "test-channel", "type": "public_channel"},
        )
        assert result["id"] == "c1"

    def test_create_conversation_with_options(self, mock_client):
        mock_client.post.return_value = {"id": "c1"}
        operations.create_conversation(
            mock_client,
            "secret",
            conv_type="private_channel",
            description="Top secret",
            members=["u1", "u2"],
        )
        call_data = mock_client.post.call_args[1]["data"]
        assert call_data["type"] == "private_channel"
        assert call_data["description"] == "Top secret"
        assert call_data["members"] == ["u1", "u2"]

    def test_join_conversation(self, mock_client):
        mock_client.post.return_value = {"ok": True}
        operations.join_conversation(mock_client, "conv-id")
        mock_client.post.assert_called_once_with(
            "/api/conversations/join", data={"conversation_id": "conv-id"}
        )

    def test_archive_unarchive(self, mock_client):
        mock_client.post.return_value = {"ok": True}
        operations.archive_conversation(mock_client, "conv-id")
        mock_client.post.assert_called_with(
            "/api/conversations/archive", data={"conversation_id": "conv-id"}
        )
        operations.unarchive_conversation(mock_client, "conv-id")
        mock_client.post.assert_called_with(
            "/api/conversations/unarchive", data={"conversation_id": "conv-id"}
        )


class TestRawApi:
    def test_raw_api_call_get(self, mock_client):
        mock_client.request.return_value = {"ok": True}
        result = operations.raw_api_call(mock_client, "GET", "/api/users/me")
        mock_client.request.assert_called_once_with("GET", "/api/users/me", params=None, data=None)
        assert result["ok"] is True

    def test_raw_api_call_post_with_data(self, mock_client):
        mock_client.request.return_value = {"id": "m1"}
        data = {"conversation": "c1", "content": {"parts": []}}
        operations.raw_api_call(mock_client, "POST", "/api/messages/post", data)
        mock_client.request.assert_called_once_with(
            "POST", "/api/messages/post", params=None, data=data
        )

    def test_raw_api_call_with_query_string_in_path(self, mock_client):
        mock_client.request.return_value = {"url": "https://example.com"}
        operations.raw_api_call(mock_client, "GET", "/api/foo?bar=baz&x=1")
        mock_client.request.assert_called_once_with(
            "GET", "/api/foo", params={"bar": "baz", "x": "1"}, data=None
        )

    def test_raw_api_call_with_explicit_params(self, mock_client):
        mock_client.request.return_value = {"ok": True}
        operations.raw_api_call(mock_client, "GET", "/api/foo", params={"key": "val"})
        mock_client.request.assert_called_once_with(
            "GET", "/api/foo", params={"key": "val"}, data=None
        )

    def test_raw_api_call_embedded_and_explicit_params_merged(self, mock_client):
        mock_client.request.return_value = {"ok": True}
        operations.raw_api_call(mock_client, "GET", "/api/foo?a=1", params={"b": "2"})
        mock_client.request.assert_called_once_with(
            "GET", "/api/foo", params={"a": "1", "b": "2"}, data=None
        )
