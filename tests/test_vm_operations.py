"""Tests for VM operations."""

from __future__ import annotations

import pytest

from popcorn_core import operations


class TestVmMonitor:
    def test_vm_monitor(self, mock_client):
        mock_client.get.return_value = {
            "workers": [{"id": "my-channel", "state": "idle"}],
            "items": [],
            "total_cost": 0.0,
        }
        result = operations.vm_monitor(mock_client)
        mock_client.get.assert_called_once_with("/api/appchannels/monitor", {})
        assert result["workers"][0]["id"] == "my-channel"


class TestVmUsage:
    def test_vm_usage_defaults(self, mock_client):
        mock_client.get.return_value = {
            "total": {"count": 5, "total_cost_usd": 1.23},
            "by_queue": {},
            "by_model": {},
        }
        result = operations.vm_usage(mock_client)
        mock_client.get.assert_called_once_with("/api/appchannels/usage", {})
        assert result["total"]["count"] == 5

    def test_vm_usage_with_filters(self, mock_client):
        mock_client.get.return_value = {"total": {"count": 2}}
        operations.vm_usage(mock_client, hours=6, queue="my-channel", limit=5)
        mock_client.get.assert_called_once_with(
            "/api/appchannels/usage",
            {"hours": 6, "queue": "my-channel", "limit": 5},
        )


class TestVmTraceList:
    def test_vm_trace_list(self, mock_client):
        mock_client.get.return_value = {
            "recent_items": [{"item_id": "abc", "queue_id": "my-channel", "name": "build hero"}],
            "recent_items_total": 1,
        }
        result = operations.vm_trace_list(mock_client, "my-channel")
        mock_client.get.assert_called_once_with(
            "/api/appchannels/usage",
            {"queue": "my-channel", "limit": 10},
        )
        assert result["recent_items"][0]["item_id"] == "abc"

    def test_vm_trace_list_custom_limit(self, mock_client):
        mock_client.get.return_value = {"recent_items": [], "recent_items_total": 0}
        operations.vm_trace_list(mock_client, "ch", limit=5)
        mock_client.get.assert_called_once_with(
            "/api/appchannels/usage",
            {"queue": "ch", "limit": 5},
        )


class TestVmTrace:
    def test_vm_trace(self, mock_client):
        mock_client.get.return_value = {
            "item_id": "abc",
            "queue_id": "my-channel",
            "name": "build hero",
            "status": "complete",
            "prompt": "Build a hero section",
            "events": [{"type": "tool_call", "tool": "Read"}],
        }
        result = operations.vm_trace(mock_client, "my-channel", "abc")
        mock_client.get.assert_called_once_with("/api/appchannels/trace/my-channel/abc", {})
        assert result["status"] == "complete"

    def test_vm_trace_latest(self, mock_client):
        """When no item_id given, fetch latest from usage then get trace."""
        mock_client.get.side_effect = [
            {
                "recent_items": [{"item_id": "latest-id", "queue_id": "ch", "name": "task"}],
            },
            {
                "item_id": "latest-id",
                "queue_id": "ch",
                "status": "complete",
                "prompt": "do thing",
                "events": [],
            },
        ]
        result = operations.vm_trace_latest(mock_client, "ch")
        assert mock_client.get.call_count == 2
        assert result["item_id"] == "latest-id"

    def test_vm_trace_latest_with_status(self, mock_client):
        """Filter latest by status."""
        mock_client.get.side_effect = [
            {
                "recent_items": [
                    {"item_id": "a", "queue_id": "ch", "status": "complete"},
                    {"item_id": "b", "queue_id": "ch", "status": "failed"},
                ],
            },
            {
                "item_id": "b",
                "queue_id": "ch",
                "status": "failed",
                "prompt": "oops",
                "events": [],
            },
        ]
        result = operations.vm_trace_latest(mock_client, "ch", status="failed")
        assert result["item_id"] == "b"

    def test_vm_trace_latest_no_items(self, mock_client):
        mock_client.get.return_value = {"recent_items": []}
        result = operations.vm_trace_latest(mock_client, "ch")
        assert result is None


class TestVmTraceCurrent:
    def test_vm_trace_current_found(self, mock_client):
        mock_client.get.return_value = {
            "item_id": "active-1",
            "queue_id": "ch",
            "status": "processing",
            "prompt": "do thing",
            "events": [{"type": "turn_start"}],
        }
        result = operations.vm_trace_current(mock_client, "ch")
        mock_client.get.assert_called_once_with("/api/appchannels/trace/ch/current", {})
        assert result is not None
        assert result["status"] == "processing"

    def test_vm_trace_current_not_found(self, mock_client):
        from popcorn_core.errors import APIError

        mock_client.get.side_effect = APIError("No active item", status_code=404)
        result = operations.vm_trace_current(mock_client, "ch")
        assert result is None

    def test_vm_trace_current_reraises_non_404(self, mock_client):
        from popcorn_core.errors import APIError

        mock_client.get.side_effect = APIError("Server error", status_code=500)
        with pytest.raises(APIError):
            operations.vm_trace_current(mock_client, "ch")


class TestVmCancel:
    def test_vm_cancel(self, mock_client):
        mock_client.post.return_value = {"ok": True}
        result = operations.vm_cancel(mock_client, "my-channel", "abc")
        mock_client.post.assert_called_once_with(
            "/api/appchannels/queues/my-channel/items/abc/cancel"
        )
        assert result["ok"] is True

    def test_vm_cancel_latest(self, mock_client):
        """Find processing item from monitor, then cancel it."""
        mock_client.get.return_value = {
            "items": [
                {"queue_id": "ch", "item_id": "proc-1", "status": "processing"},
            ],
        }
        mock_client.post.return_value = {"ok": True}
        result = operations.vm_cancel_current(mock_client, "ch")
        mock_client.post.assert_called_once_with("/api/appchannels/queues/ch/items/proc-1/cancel")
        assert result is not None

    def test_vm_cancel_latest_no_processing(self, mock_client):
        mock_client.get.return_value = {"items": []}
        result = operations.vm_cancel_current(mock_client, "ch")
        assert result is None


class TestVmRollback:
    def test_vm_rollback(self, mock_client):
        mock_client.post.return_value = {"ok": True, "version": 3}
        result = operations.vm_rollback(mock_client, "my-channel")
        mock_client.post.assert_called_once_with(
            "/api/appchannels/sites/my-channel/rollback",
            data={},
        )
        assert result["version"] == 3

    def test_vm_rollback_specific_version(self, mock_client):
        mock_client.post.return_value = {"ok": True, "version": 2}
        operations.vm_rollback(mock_client, "my-channel", version=2)
        mock_client.post.assert_called_once_with(
            "/api/appchannels/sites/my-channel/rollback",
            data={"version": 2},
        )
