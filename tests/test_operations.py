"""Tests for popcorn_core.operations."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx
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

    def test_search_messages_searches_only_messages(self, mock_client):
        """Otherwise `total` and `index_counts` count file hits the command never renders."""
        mock_client.get.return_value = {"messages": []}
        operations.search_messages(mock_client, "deploy")
        _, params = mock_client.get.call_args[0]
        assert params["search_messages"] == "true"
        assert params["search_files"] == "false"
        assert params["search_conversations"] == "false"
        assert params["search_links"] == "false"

    def test_search_messages_omits_unset_filters(self, mock_client):
        mock_client.get.return_value = {"messages": []}
        operations.search_messages(mock_client, "deploy")
        _, params = mock_client.get.call_args[0]
        for absent in (
            "conversations",
            "from_users",
            "created_after",
            "created_before",
            "has",
            "sort_by",
            "offset",
        ):
            assert absent not in params

    def test_search_messages_resolves_channel_names(self, mock_client):
        mock_client.get.return_value = {"messages": []}
        with patch(
            "popcorn_core.operations.resolve_conversation",
            side_effect=lambda _c, ref: f"id-of-{ref.lstrip('#')}",
        ):
            operations.search_messages(mock_client, "deploy", conversations="#ops, #general")
        _, params = mock_client.get.call_args[0]
        assert params["conversations"] == "id-of-ops,id-of-general"

    def test_search_messages_resolves_usernames(self, mock_client):
        mock_client.get.return_value = {"messages": []}
        with patch(
            "popcorn_core.operations.resolve_user",
            side_effect=lambda _c, ref: f"id-of-{ref}",
        ):
            operations.search_messages(mock_client, "deploy", from_users="ana,bo")
        _, params = mock_client.get.call_args[0]
        assert params["from_users"] == "id-of-ana,id-of-bo"

    def test_search_messages_passes_time_and_sort_filters(self, mock_client):
        mock_client.get.return_value = {"messages": []}
        operations.search_messages(
            mock_client,
            "deploy",
            created_after="2026-01-01",
            created_before="2026-02-01",
            sort_by="date_desc",
            has="link",
        )
        _, params = mock_client.get.call_args[0]
        assert params["created_after"] == "2026-01-01"
        assert params["created_before"] == "2026-02-01"
        assert params["sort_by"] == "date_desc"
        assert params["has"] == "link"

    @pytest.mark.parametrize(
        "filter_kwargs",
        [
            {"conversations": "#ops"},
            {"from_users": "ana"},
            {"created_after": "2026-01-01"},
            {"created_before": "2026-02-01"},
            {"has": "file"},
        ],
    )
    def test_search_messages_allows_empty_query_with_a_filter(self, mock_client, filter_kwargs):
        """Mirrors what the server itself accepts in place of a query."""
        mock_client.get.return_value = {"messages": []}
        with patch("popcorn_core.operations.resolve_user", side_effect=lambda _c, ref: ref):
            operations.search_messages(mock_client, "", **filter_kwargs)
        mock_client.get.assert_called_once()

    def test_search_messages_sort_alone_does_not_substitute_for_a_query(self, mock_client):
        """Sorting narrows nothing, so it cannot stand in for a query."""
        with pytest.raises(PopcornError, match="Query required"):
            operations.search_messages(mock_client, "", sort_by="date_desc")


class TestMessages:
    def test_read_messages(self, mock_client):
        mock_client.get.return_value = {"messages": [{"id": "m1"}]}
        result = operations.read_messages(mock_client, "conv-id")
        mock_client.get.assert_called_once_with(
            "/api/messages/history", {"limit": 25, "conversation": "conv-id"}
        )
        assert result["messages"][0]["id"] == "m1"

    def test_read_messages_thread(self, mock_client):
        mock_client.get.return_value = {"messages": []}
        operations.read_messages(mock_client, "conv-id", thread_id="t1", limit=10)
        mock_client.get.assert_called_once_with(
            "/api/messages/thread",
            {"thread_ts": "t1", "limit": 10, "conversation": "conv-id"},
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

    def test_create_conversation_installs_a_template(self, mock_client):
        """`--template` on create is the ONLY install path left: the bundle
        registry has no client-reachable install endpoint of its own."""
        mock_client.post.return_value = {"id": "c1"}
        operations.create_conversation(mock_client, "#ops", template="chat")
        assert mock_client.post.call_args[1]["data"]["template"] == "chat"

    def test_create_conversation_omits_template_when_absent(self, mock_client):
        """A present-but-null key is not the same as an absent one on a create
        body — send nothing rather than an explicit no-template."""
        mock_client.post.return_value = {"id": "c1"}
        operations.create_conversation(mock_client, "#ops")
        assert "template" not in mock_client.post.call_args[1]["data"]

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


class TestWebhookDeliveries:
    def test_basic(self, mock_client):
        mock_client.get.return_value = {"deliveries": []}
        operations.list_webhook_deliveries(mock_client, "conv-1", limit=10)
        mock_client.get.assert_called_once_with(
            "/api/webhooks/deliveries", {"conversation": "conv-1", "limit": 10}
        )

    def test_include_passthrough(self, mock_client):
        mock_client.get.return_value = {"deliveries": []}
        operations.list_webhook_deliveries(mock_client, "conv-1", limit=10, include="payload_raw")
        mock_client.get.assert_called_once_with(
            "/api/webhooks/deliveries",
            {"conversation": "conv-1", "limit": 10, "include": "payload_raw"},
        )

    def test_include_omitted_when_none(self, mock_client):
        mock_client.get.return_value = {"deliveries": []}
        operations.list_webhook_deliveries(mock_client, "conv-1", include=None)
        params = mock_client.get.call_args[0][1]
        assert "include" not in params

    def test_all_params(self, mock_client):
        mock_client.get.return_value = {"deliveries": []}
        operations.list_webhook_deliveries(
            mock_client,
            "conv-1",
            limit=25,
            since="2026-01-01T00:00:00Z",
            after="d-abc",
            status="failed",
            include="payload_raw",
        )
        mock_client.get.assert_called_once_with(
            "/api/webhooks/deliveries",
            {
                "conversation": "conv-1",
                "limit": 25,
                "since": "2026-01-01T00:00:00Z",
                "after": "d-abc",
                "status": "failed",
                "include": "payload_raw",
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


class TestWebhookCreate:
    def test_minimal(self, mock_client):
        mock_client.post.return_value = {"id": "wh-1"}
        operations.create_webhook(mock_client, "conv-1", "deploy hook")
        mock_client.post.assert_called_once_with(
            "/api/webhooks/create",
            data={"name": "deploy hook"},
            params={"conversation": "conv-1"},
        )

    def test_trigger_workflow(self, mock_client):
        mock_client.post.return_value = {"id": "wh-1"}
        operations.create_webhook(
            mock_client,
            "conv-1",
            "flow hook",
            action_mode="trigger_workflow",
            trigger_flow_id="flow-abc",
        )
        body = mock_client.post.call_args.kwargs["data"]
        assert body["action_mode"] == "trigger_workflow"
        assert body["trigger_flow_id"] == "flow-abc"

    def test_trigger_flow_id_omitted_when_none(self, mock_client):
        mock_client.post.return_value = {"id": "wh-1"}
        operations.create_webhook(mock_client, "conv-1", "hook")
        assert "trigger_flow_id" not in mock_client.post.call_args.kwargs["data"]

    def test_trigger_flow_name(self, mock_client):
        """Bundle flows are named, not UUID'd — the name form must reach the API."""
        mock_client.post.return_value = {"id": "wh-1"}
        operations.create_webhook(
            mock_client,
            "conv-1",
            "flow hook",
            action_mode="trigger_workflow",
            trigger_flow_name="alert_webhook",
        )
        body = mock_client.post.call_args.kwargs["data"]
        assert body["trigger_flow_name"] == "alert_webhook"
        assert "trigger_flow_id" not in body

    def test_trigger_flow_name_omitted_when_none(self, mock_client):
        mock_client.post.return_value = {"id": "wh-1"}
        operations.create_webhook(mock_client, "conv-1", "hook")
        assert "trigger_flow_name" not in mock_client.post.call_args.kwargs["data"]

    def test_event_types(self, mock_client):
        mock_client.get.return_value = {"sources": [], "action_modes": []}
        operations.webhook_event_types(mock_client)
        mock_client.get.assert_called_once_with("/api/webhooks/event-types")


_HOOKS = {
    "webhooks": [
        {"id": "11111111-2222-3333-4444-555555555555", "name": "Intake", "url": "u/intake"},
        {"id": "66666666-7777-8888-9999-000000000000", "name": "Alerts", "url": "u/alerts"},
    ]
}


class TestWebhookSendResolution:
    def test_url_target_needs_no_lookup(self, mock_client):
        url = operations.resolve_webhook_url(
            mock_client, "https://hooks.popcorn.ai/ingest/tok-1", None
        )
        assert url == "https://hooks.popcorn.ai/ingest/tok-1"
        mock_client.get.assert_not_called()

    def test_by_uuid(self, mock_client):
        mock_client.get.return_value = _HOOKS
        url = operations.resolve_webhook_url(
            mock_client, "11111111-2222-3333-4444-555555555555", "conv-1"
        )
        assert url == "u/intake"

    def test_by_name(self, mock_client):
        mock_client.get.return_value = _HOOKS
        assert operations.resolve_webhook_url(mock_client, "Alerts", "conv-1") == "u/alerts"

    def test_by_name_is_case_insensitive(self, mock_client):
        mock_client.get.return_value = _HOOKS
        assert operations.resolve_webhook_url(mock_client, "iNtAkE", "conv-1") == "u/intake"

    def test_missing_channel_explains_why(self, mock_client):
        """The only lookup is conversation-scoped, so say that, not 'usage'."""
        with pytest.raises(PopcornError) as exc:
            operations.resolve_webhook_url(mock_client, "Intake", None)
        assert "channel" in str(exc.value).lower()
        assert exc.value.error_code == "validation"
        mock_client.get.assert_not_called()

    def test_unknown_target_lists_what_exists(self, mock_client):
        mock_client.get.return_value = _HOOKS
        with pytest.raises(PopcornError) as exc:
            operations.resolve_webhook_url(mock_client, "Nope", "conv-1")
        assert exc.value.error_code == "not_found"
        assert "Intake" in str(exc.value)

    def test_hook_without_url(self, mock_client):
        mock_client.get.return_value = {"webhooks": [{"id": "wh-1", "name": "Intake"}]}
        with pytest.raises(PopcornError, match="no ingest URL"):
            operations.resolve_webhook_url(mock_client, "Intake", "conv-1")


class TestWebhookSend:
    @staticmethod
    def _resp(status=200, json_body=None, text=""):
        resp = MagicMock()
        resp.status_code = status
        resp.text = text
        if json_body is None:
            resp.json.side_effect = ValueError("not json")
        else:
            resp.json.return_value = json_body
        return resp

    def test_send_carries_no_authorization_header(self):
        """The ingest host is not the API host — the POST must be credential-free."""
        resp = self._resp(json_body={"status": "ok", "request_id": "r-1"})
        with patch("popcorn_core.operations.httpx.post", return_value=resp) as post:
            operations.send_webhook("https://hooks.popcorn.ai/ingest/tok-1", {"a": 1})
        headers = post.call_args.kwargs["headers"]
        assert headers == {"Content-Type": "application/json"}
        assert not any(k.lower() == "authorization" for k in headers)

    def test_send_posts_json_body(self):
        resp = self._resp(json_body={"status": "ok", "request_id": "r-1"})
        with patch("popcorn_core.operations.httpx.post", return_value=resp) as post:
            result = operations.send_webhook("https://hooks.popcorn.ai/ingest/t", {"a": 1})
        assert post.call_args[0][0] == "https://hooks.popcorn.ai/ingest/t"
        assert json.loads(post.call_args.kwargs["content"]) == {"a": 1}
        assert result == {
            "url": "https://hooks.popcorn.ai/ingest/t",
            "status": 200,
            "response": {"status": "ok", "request_id": "r-1"},
        }

    def test_non_2xx_is_an_error_carrying_the_body(self):
        resp = self._resp(status=422, json_body={"detail": "bad payload"}, text='{"detail":"bad"}')
        with (
            patch("popcorn_core.operations.httpx.post", return_value=resp),
            pytest.raises(APIError) as exc,
        ):
            operations.send_webhook("https://hooks.popcorn.ai/ingest/t", {})
        assert exc.value.status_code == 422
        assert exc.value.exit_code != 0
        assert "bad" in str(exc.value)

    def test_non_json_body_falls_back_to_text(self):
        resp = self._resp(status=200, json_body=None, text="accepted")
        with patch("popcorn_core.operations.httpx.post", return_value=resp):
            result = operations.send_webhook("https://hooks.popcorn.ai/ingest/t", {})
        assert result["response"] == "accepted"

    def test_timeout(self):
        with (
            patch(
                "popcorn_core.operations.httpx.post",
                side_effect=httpx.TimeoutException("timed out"),
            ),
            pytest.raises(APIError, match="timed out"),
        ):
            operations.send_webhook("https://hooks.popcorn.ai/ingest/t", {})


class TestFlows:
    def test_list_flows(self, mock_client):
        mock_client.get.return_value = {"flows": [{"id": "f1"}], "has_more": False}
        result = operations.list_flows(mock_client, "conv-1")
        mock_client.get.assert_called_once_with(
            "/api/customer-flows/list", {"conversation_id": "conv-1", "limit": 50}
        )
        assert result["flows"][0]["id"] == "f1"

    def test_list_flows_offset(self, mock_client):
        mock_client.get.return_value = {"flows": []}
        operations.list_flows(mock_client, "conv-1", limit=10, offset=20)
        mock_client.get.assert_called_once_with(
            "/api/customer-flows/list",
            {"conversation_id": "conv-1", "limit": 10, "offset": 20},
        )

    def test_get_flow(self, mock_client):
        mock_client.get.return_value = {"flow": {"id": "f1"}}
        operations.get_flow(mock_client, "conv-1", "f1")
        mock_client.get.assert_called_once_with(
            "/api/customer-flows/get", {"conversation_id": "conv-1", "flow_id": "f1"}
        )

    def test_get_flow_include_triggers(self, mock_client):
        mock_client.get.return_value = {"flow": {"id": "f1"}, "triggers": None}
        operations.get_flow(mock_client, "conv-1", "f1", include_triggers=True)
        mock_client.get.assert_called_once_with(
            "/api/customer-flows/get",
            {"conversation_id": "conv-1", "flow_id": "f1", "include_triggers": True},
        )

    def test_run_flow_no_inputs(self, mock_client):
        """conversation_id is supplied even with no --inputs: nearly every flow
        declares it, and omitting it fails at runtime rather than here."""
        mock_client.post.return_value = {"workflow_id": "wf-1"}
        operations.run_flow(mock_client, "conv-1", "f1")
        mock_client.post.assert_called_once_with(
            "/api/customer-flows/run",
            data={
                "conversation_id": "conv-1",
                "flow_id": "f1",
                "inputs": {"conversation_id": "conv-1"},
            },
            params={"conversation_id": "conv-1"},
        )

    def test_run_flow_with_inputs(self, mock_client):
        mock_client.post.return_value = {"workflow_id": "wf-1"}
        operations.run_flow(mock_client, "conv-1", "f1", inputs={"x": 1})
        body = mock_client.post.call_args.kwargs["data"]
        assert body["inputs"] == {"x": 1, "conversation_id": "conv-1"}

    def test_run_flow_does_not_override_an_explicit_conversation_id(self, mock_client):
        mock_client.post.return_value = {"workflow_id": "wf-1"}
        operations.run_flow(mock_client, "conv-1", "f1", inputs={"conversation_id": "other"})
        body = mock_client.post.call_args.kwargs["data"]
        assert body["inputs"]["conversation_id"] == "other"

    def test_list_flow_runs(self, mock_client):
        mock_client.get.return_value = {"executions": [], "count": 0, "next_page_token": None}
        operations.list_flow_runs(mock_client, "conv-1")
        mock_client.get.assert_called_once_with(
            "/api/customer-flow-runs/list", {"conversation_id": "conv-1", "limit": 50}
        )

    def test_list_flow_runs_filters(self, mock_client):
        mock_client.get.return_value = {"executions": []}
        operations.list_flow_runs(
            mock_client, "conv-1", status="running", limit=10, page_token="tok"
        )
        mock_client.get.assert_called_once_with(
            "/api/customer-flow-runs/list",
            {
                "conversation_id": "conv-1",
                "limit": 10,
                "status": "running",
                "page_token": "tok",
            },
        )

    def test_get_flow_run(self, mock_client):
        mock_client.get.return_value = {"run": {"workflow_id": "wf-1"}}
        operations.get_flow_run(mock_client, "conv-1", "wf-1")
        mock_client.get.assert_called_once_with(
            "/api/customer-flow-runs/get",
            {"conversation_id": "conv-1", "workflow_id": "wf-1"},
        )

    def test_get_flow_run_with_options(self, mock_client):
        mock_client.get.return_value = {"run": {}}
        operations.get_flow_run(mock_client, "conv-1", "wf-1", run_id="r-1", include_errors=True)
        mock_client.get.assert_called_once_with(
            "/api/customer-flow-runs/get",
            {
                "conversation_id": "conv-1",
                "workflow_id": "wf-1",
                "run_id": "r-1",
                "include_errors": True,
            },
        )


class TestChannelTemplates:
    def test_list_templates(self, mock_client):
        mock_client.get.return_value = {"templates": [{"name": "crm"}]}
        result = operations.list_channel_templates(mock_client)
        mock_client.get.assert_called_once_with("/api/conversations/templates")
        assert result["templates"][0]["name"] == "crm"


class TestDataStoreOperations:
    """The data-store surface at /api/v1/conversations/{id}/data-store/…

    The channel ref is resolved to a conversation UUID and baked into the
    path, so every assertion here pins the resolved path shape.
    """

    def test_list_tables(self, mock_client):
        mock_client.get.return_value = {"ok": True, "tables": []}
        operations.list_tables(mock_client, "conv-uuid")
        mock_client.get.assert_called_once_with("/api/v1/conversations/conv-uuid/data-store/tables")

    def test_get_table(self, mock_client):
        mock_client.get.return_value = {"ok": True, "table": {}}
        operations.get_table(mock_client, "conv-uuid", "alerts")
        mock_client.get.assert_called_once_with(
            "/api/v1/conversations/conv-uuid/data-store/tables/alerts"
        )

    def test_list_records_builds_scoped_path(self, mock_client):
        mock_client.get.return_value = {"ok": True, "records": []}
        operations.list_records(
            mock_client, "conv-uuid", "alerts", filter={"Status": "firing"}, limit=10
        )
        path, params = mock_client.get.call_args.args
        assert path == "/api/v1/conversations/conv-uuid/data-store/tables/alerts/records"
        assert params["limit"] == 10
        # The endpoint takes `filter` as a JSON-encoded string, not a nested dict.
        assert params["filter"] == '{"Status": "firing"}'

    def test_list_records_omits_absent_filter(self, mock_client):
        mock_client.get.return_value = {"ok": True, "records": []}
        operations.list_records(mock_client, "conv-uuid", "alerts")
        _, params = mock_client.get.call_args.args
        assert "filter" not in params
        assert params == {"limit": 50}

    def test_list_records_passes_cursor(self, mock_client):
        mock_client.get.return_value = {"ok": True, "records": []}
        operations.list_records(mock_client, "conv-uuid", "alerts", cursor="cur-1")
        _, params = mock_client.get.call_args.args
        assert params["cursor"] == "cur-1"

    def test_get_record(self, mock_client):
        mock_client.get.return_value = {"ok": True, "record": {}}
        operations.get_record(mock_client, "conv-uuid", "alerts", 7)
        mock_client.get.assert_called_once_with(
            "/api/v1/conversations/conv-uuid/data-store/tables/alerts/records/7"
        )

    def test_patch_record_sends_data_body(self, mock_client):
        mock_client.patch.return_value = {"ok": True, "record": {}}
        operations.patch_record(mock_client, "conv-uuid", "alerts", 7, {"Status": "acked"})
        (path,) = mock_client.patch.call_args.args
        assert path.endswith("/data-store/tables/alerts/records/7")
        # PatchRecordRequest wraps the columns in a `data` envelope.
        assert mock_client.patch.call_args.kwargs["data"] == {"data": {"Status": "acked"}}

    def test_delete_record(self, mock_client):
        mock_client.delete.return_value = {}
        operations.delete_record(mock_client, "conv-uuid", "alerts", 7)
        mock_client.delete.assert_called_once_with(
            "/api/v1/conversations/conv-uuid/data-store/tables/alerts/records/7"
        )

    def test_list_scalars(self, mock_client):
        mock_client.get.return_value = {"ok": True, "scalars": []}
        operations.list_scalars(mock_client, "conv-uuid")
        mock_client.get.assert_called_once_with(
            "/api/v1/conversations/conv-uuid/data-store/scalars", {"limit": 50}
        )

    def test_get_scalar(self, mock_client):
        mock_client.get.return_value = {"ok": True, "scalar": {}}
        operations.get_scalar(mock_client, "conv-uuid", "alerts_summary")
        mock_client.get.assert_called_once_with(
            "/api/v1/conversations/conv-uuid/data-store/scalars/alerts_summary"
        )

    def test_set_scalar_uses_put(self, mock_client):
        mock_client.put.return_value = {"ok": True, "scalar": {}}
        operations.set_scalar(mock_client, "conv-uuid", "alerts_summary", "3 firing")
        (path,) = mock_client.put.call_args.args
        assert path.endswith("/data-store/scalars/alerts_summary")
        assert mock_client.put.call_args.kwargs["data"] == {"value": "3 firing"}

    def test_list_store_audit(self, mock_client):
        mock_client.get.return_value = {"ok": True, "events": []}
        operations.list_store_audit(mock_client, "conv-uuid", limit=10)
        mock_client.get.assert_called_once_with(
            "/api/v1/conversations/conv-uuid/data-store/audit", {"limit": 10}
        )

    def test_list_store_audit_omits_unset_filters(self, mock_client):
        """An unset filter is absent, never an empty string: the server reads
        `entity_type=` as a value to match and answers with nothing."""
        mock_client.get.return_value = {"ok": True, "events": []}
        operations.list_store_audit(mock_client, "conv-uuid", entity_type="record")
        mock_client.get.assert_called_once_with(
            "/api/v1/conversations/conv-uuid/data-store/audit",
            {"limit": 50, "entity_type": "record"},
        )

    def test_list_store_audit_sends_every_filter(self, mock_client):
        mock_client.get.return_value = {"ok": True, "events": []}
        operations.list_store_audit(
            mock_client,
            "conv-uuid",
            limit=10,
            cursor="cur-9",
            entity_type="record",
            entity_id="42",
            since="2026-09-01T00:00:00Z",
        )
        mock_client.get.assert_called_once_with(
            "/api/v1/conversations/conv-uuid/data-store/audit",
            {
                "limit": 10,
                "entity_type": "record",
                "entity_id": "42",
                "since": "2026-09-01T00:00:00Z",
                "cursor": "cur-9",
            },
        )


class TestActivityCatalog:
    def test_list_activity_catalog_hits_the_human_surface(self, mock_client):
        mock_client.get.return_value = {"ok": True, "activities": []}
        operations.list_activity_catalog(mock_client)
        mock_client.get.assert_called_once_with("/api/customer-flows/activity-catalog", params={})

    def test_conversation_is_accepted_and_ignored(self, mock_client):
        """The catalog is global — no conversation scope reaches the wire."""
        mock_client.get.return_value = {"ok": True, "activities": []}
        operations.list_activity_catalog(mock_client, "#ops")
        mock_client.get.assert_called_once_with("/api/customer-flows/activity-catalog", params={})

    def test_omitted_filters_are_not_sent(self, mock_client):
        """A backend predating the filter params must see the same request it
        always saw — an unknown query param would be ignored there, so sending
        `view=full` would be harmless but sending nothing is honest."""
        mock_client.get.return_value = {"ok": True, "activities": []}
        operations.list_activity_catalog(mock_client, tier="foundation")
        mock_client.get.assert_called_once_with(
            "/api/customer-flows/activity-catalog", params={"tier": "foundation"}
        )

    def test_every_filter_reaches_the_wire(self, mock_client):
        mock_client.get.return_value = {"ok": True, "activities": []}
        operations.list_activity_catalog(
            mock_client,
            name="foundation.store.upsert_rows",
            tier="foundation",
            status="release",
            category="store",
            view="summary",
        )
        mock_client.get.assert_called_once_with(
            "/api/customer-flows/activity-catalog",
            params={
                "name": "foundation.store.upsert_rows",
                "tier": "foundation",
                "status": "release",
                "category": "store",
                "view": "summary",
            },
        )


class TestFlowValidation:
    def test_validate_flow_yaml_posts_yaml_text(self, mock_client):
        mock_client.post.return_value = {"ok": True, "valid": True, "steps": []}
        operations.validate_flow_yaml(mock_client, "conv-uuid", "name: x\n")
        mock_client.post.assert_called_once_with(
            "/api/customer-flows/validate",
            data={"yaml_text": "name: x\n"},
            params={"conversation_id": "conv-uuid"},
        )

    def test_validate_returns_the_body_verbatim(self, mock_client):
        """An invalid flow is a 200 with valid:false — not an APIError — so the
        caller must see the issues rather than an exception."""
        mock_client.post.return_value = {
            "ok": True,
            "valid": False,
            "issues": ["steps[0](a).args.text: missing required arg: text"],
            "steps": [],
        }
        resp = operations.validate_flow_yaml(mock_client, "conv-uuid", "name: x\n")
        assert resp["valid"] is False
        assert len(resp["issues"]) == 1


class TestTemplatePacking:
    def test_pack_skips_dotfiles_and_macos_cruft(self, tmp_path):
        import io
        import zipfile

        (tmp_path / "manifest.yaml").write_text("display_name: X\n")
        (tmp_path / ".DS_Store").write_text("junk")
        (tmp_path / "__MACOSX").mkdir()
        (tmp_path / "__MACOSX" / "x.yaml").write_text("name: ghost\n")
        (tmp_path / "fixtures").mkdir()
        (tmp_path / "fixtures" / "a.json").write_text("{}")

        data = operations.pack_template_dir(str(tmp_path))
        names = set(zipfile.ZipFile(io.BytesIO(data)).namelist())
        assert "manifest.yaml" in names
        assert "fixtures/a.json" in names, "nested paths must keep their relative prefix"
        assert not any(".DS_Store" in n or "__MACOSX" in n for n in names)

    def test_pack_skips_a_dotdir_anywhere_in_the_path(self, tmp_path):
        import io
        import zipfile

        (tmp_path / "manifest.yaml").write_text("display_name: X\n")
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "config").write_text("junk")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / ".hidden.yaml").write_text("name: ghost\n")

        names = set(
            zipfile.ZipFile(io.BytesIO(operations.pack_template_dir(str(tmp_path)))).namelist()
        )
        assert names == {"manifest.yaml"}

    def test_pack_rejects_oversized_entry(self, tmp_path):
        (tmp_path / "manifest.yaml").write_text("display_name: X\n")
        (tmp_path / "big.yaml").write_text("x" * (1024 * 1024 + 1))

        with pytest.raises(PopcornError) as exc:
            operations.pack_template_dir(str(tmp_path))
        assert "1 MiB" in str(exc.value) or "per-file limit" in str(exc.value)

    def test_pack_requires_a_manifest(self, tmp_path):
        (tmp_path / "only_a_flow.yaml").write_text("name: x\n")
        with pytest.raises(PopcornError) as exc:
            operations.pack_template_dir(str(tmp_path))
        assert "manifest" in str(exc.value).lower()

    def test_pack_accepts_config_yaml_as_the_manifest(self, tmp_path):
        (tmp_path / "config.yaml").write_text("display_name: X\n")
        (tmp_path / "a.yaml").write_text("name: x\n")
        assert operations.pack_template_dir(str(tmp_path))

    def test_pack_rejects_a_non_directory(self, tmp_path):
        f = tmp_path / "bundle.zip"
        f.write_text("x")
        with pytest.raises(PopcornError, match="Not a directory"):
            operations.pack_template_dir(str(f))


class TestTemplateImportIsFenced:
    """`import_template` raises before it touches the network.

    Ordering is the whole point. The old body zipped the directory, uploaded
    the zip to the target channel, and only then posted the file key to a route
    that now 404s — so every failed attempt left a stray zip behind in the
    channel. The fence has to come first.
    """

    def _bundle(self, tmp_path):
        (tmp_path / "manifest.yaml").write_text("display_name: X\n")
        return str(tmp_path)

    def test_it_raises(self, mock_client, tmp_path):
        with pytest.raises(PopcornError):
            operations.import_template(mock_client, "conv-uuid", self._bundle(tmp_path))

    def test_it_uploads_nothing_and_posts_nothing(self, mock_client, monkeypatch, tmp_path):
        uploaded = []
        monkeypatch.setattr(
            operations,
            "upload_file",
            lambda c, conv, path: uploaded.append(path) or {"url": "k"},
        )
        with pytest.raises(PopcornError):
            operations.import_template(mock_client, "conv-uuid", self._bundle(tmp_path))
        assert uploaded == [], "a fenced install still uploaded a zip"
        mock_client.post.assert_not_called()

    def test_it_does_not_even_resolve_the_channel(self, mock_client, tmp_path):
        """Resolution is a GET against the workspace. Nothing about a removed
        command should need the network — or a valid channel."""
        with pytest.raises(PopcornError):
            operations.import_template(mock_client, "#no-such-channel", self._bundle(tmp_path))
        mock_client.get.assert_not_called()

    def test_dry_run_is_fenced_too(self, mock_client, tmp_path):
        with pytest.raises(PopcornError):
            operations.import_template(
                mock_client, "conv-uuid", self._bundle(tmp_path), dry_run=True
            )

    def test_it_raises_for_a_bundle_that_would_not_even_pack(self, mock_client, tmp_path):
        """The fence outranks the packing checks: "this endpoint is gone" is
        more useful than "your bundle has no manifest" when neither bundle can
        be installed."""
        (tmp_path / "no_manifest.yaml").write_text("name: x\n")
        with pytest.raises(PopcornError) as exc:
            operations.import_template(mock_client, "conv-uuid", str(tmp_path))
        assert "bundle registry" in str(exc.value)

    def test_the_message_carries_the_replacement_path(self, mock_client, tmp_path):
        with pytest.raises(PopcornError) as exc:
            operations.import_template(mock_client, "conv-uuid", self._bundle(tmp_path))
        msg = str(exc.value)
        assert "bundle registry" in msg
        assert "channel create" in msg
        assert "template check" in msg
