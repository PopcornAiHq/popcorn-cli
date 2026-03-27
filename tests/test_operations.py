"""Tests for popcorn_core.operations."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from popcorn_core import operations
from popcorn_core.errors import APIError, PopcornError


@pytest.fixture(autouse=True)
def _patch_resolve():
    """Bypass channel resolution — tests pass UUIDs directly."""
    with patch("popcorn_core.operations.resolve_conversation", side_effect=lambda _c, ref: ref):
        yield


class TestIdentity:
    def test_get_whoami(self, mock_client):
        mock_client.get.return_value = {"user": {"id": "u1", "email": "a@b.com"}}
        result = operations.get_whoami(mock_client)
        mock_client.get.assert_called_once_with("/api/users/current-user")
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
            data={"name": "test-channel", "conversation_type": "public_channel"},
        )
        assert result["id"] == "c1"

    def test_create_conversation_with_options(self, mock_client):
        mock_client.post.return_value = {"id": "c1"}
        operations.create_conversation(
            mock_client,
            "secret",
            conv_type="private_channel",
            member_ids=["u1", "u2"],
        )
        call_data = mock_client.post.call_args[1]["data"]
        assert call_data["conversation_type"] == "private_channel"
        assert call_data["member_ids"] == ["u1", "u2"]

    def test_join_conversation(self, mock_client):
        mock_client.post.return_value = {"ok": True}
        operations.join_conversation(mock_client, "conv-id")
        mock_client.post.assert_called_once_with(
            "/api/conversations/join", data={"conversation": "conv-id"}
        )

    def test_archive_unarchive(self, mock_client):
        mock_client.post.return_value = {"ok": True}
        operations.archive_conversation(mock_client, "conv-id")
        mock_client.post.assert_called_with(
            "/api/conversations/archive", data={"conversation": "conv-id"}
        )
        operations.unarchive_conversation(mock_client, "conv-id")
        mock_client.post.assert_called_with(
            "/api/conversations/unarchive", data={"conversation": "conv-id"}
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


class TestSiteStatus:
    def test_get_site_status(self, mock_client):
        mock_client.get.return_value = {
            "site_name": "my-site",
            "status": "deployed",
            "url": "https://my-site.popcorn.ai",
        }
        result = operations.get_site_status(mock_client, "conv-1")
        mock_client.get.assert_called_once_with("/api/conversations/conv-1/site/status")
        assert result["status"] == "deployed"
        assert result["site_name"] == "my-site"

    def test_get_site_status_fallback(self, mock_client):
        mock_client.get.side_effect = [
            APIError("Not found", status_code=404),
            {"conversation": {"id": "conv-1", "name": "my-site"}},
        ]
        result = operations.get_site_status(mock_client, "conv-1")
        assert mock_client.get.call_count == 2
        mock_client.get.assert_called_with("/api/conversations/info", {"conversation_id": "conv-1"})
        assert result["fallback"] is True
        assert result["conversation"]["id"] == "conv-1"

    def test_get_site_log(self, mock_client):
        mock_client.get.return_value = {
            "versions": [{"version": 1, "commit_hash": "abc123"}],
        }
        result = operations.get_site_log(mock_client, "conv-1")
        mock_client.get.assert_called_once_with("/api/conversations/conv-1/site/log", {"limit": 10})
        assert len(result["versions"]) == 1
        assert result["versions"][0]["version"] == 1

    def test_get_site_log_fallback(self, mock_client):
        mock_client.get.side_effect = APIError("Not found", status_code=404)
        result = operations.get_site_log(mock_client, "conv-1")
        assert result["fallback"] is True
        assert result["versions"] == []

    def test_deploy_publish_with_force(self, mock_client):
        mock_client.post.return_value = {
            "conversation_id": "conv-1",
            "site_name": "my-site",
            "version": 3,
        }
        operations.deploy_publish(mock_client, "conv-1", "s3-key-1", force=True)
        mock_client.post.assert_called_once_with(
            "/api/conversations/publish",
            data={
                "conversation_id": "conv-1",
                "s3_key": "s3-key-1",
                "force": True,
            },
        )


class TestCheckAccess:
    def test_check_access_accessible(self, mock_client):
        mock_client.post.return_value = {"accessible": True}
        result = operations.check_access(mock_client, "acme/widgets")
        mock_client.post.assert_called_once_with(
            "/api/integrations/check-access",
            data={"provider": "github", "owner": "acme", "repo": "widgets"},
        )
        assert result["accessible"] is True

    def test_check_access_not_accessible(self, mock_client):
        mock_client.post.return_value = {
            "accessible": False,
            "auth_url": "https://github.com/login/oauth/authorize?...",
        }
        result = operations.check_access(mock_client, "acme/widgets")
        assert result["accessible"] is False
        assert "auth_url" in result

    def test_check_access_invalid_format(self, mock_client):
        with pytest.raises(PopcornError, match="Invalid repo format"):
            operations.check_access(mock_client, "no-slash-here")

    @pytest.mark.parametrize("bad_input", ["/", "owner/", "/repo", "org/repo/extra"])
    def test_check_access_empty_parts(self, mock_client, bad_input):
        with pytest.raises(PopcornError, match="Invalid repo format"):
            operations.check_access(mock_client, bad_input)
