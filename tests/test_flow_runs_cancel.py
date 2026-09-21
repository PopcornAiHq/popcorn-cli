"""`flow runs cancel` — one run by workflow id, or every running run of a
flow by `--flow`; and the `queue:` line `runs get` / `runs list` gained.

The bulk form exists for a driver like `run_eval`, which launches dozens of
independent runs and is itself done in seconds: cancelling the driver stops
nothing, the runs it started are what must stop.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

from popcorn_cli.commands.flow import _run_detail_lines
from popcorn_core import operations
from popcorn_core.errors import PopcornError


@pytest.fixture
def mock_client(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(operations, "resolve_conversation", lambda c, conv: "conv-1")
    return client


class TestOperation:
    def test_one_run_posts_workflow_id(self, mock_client):
        mock_client.post.return_value = {"cancelled": [], "count": 0}
        operations.cancel_flow_runs(mock_client, "#ops", workflow_id="wf-1")
        mock_client.post.assert_called_once_with(
            "/api/customer-flow-runs/cancel",
            data={"force": False, "workflow_id": "wf-1"},
            params={"conversation_id": "conv-1"},
        )

    def test_one_run_with_run_id_force_and_reason(self, mock_client):
        mock_client.post.return_value = {"cancelled": []}
        operations.cancel_flow_runs(
            mock_client, "#ops", workflow_id="wf-1", run_id="r-1", force=True, reason="wrong"
        )
        body = mock_client.post.call_args.kwargs["data"]
        assert body == {"force": True, "workflow_id": "wf-1", "run_id": "r-1", "reason": "wrong"}

    def test_flow_posts_flow_name_and_page_token(self, mock_client):
        mock_client.post.return_value = {"cancelled": []}
        operations.cancel_flow_runs(mock_client, "#ops", flow_name="claim_turn", page_token="tok")
        body = mock_client.post.call_args.kwargs["data"]
        assert body == {"force": False, "flow_name": "claim_turn", "page_token": "tok"}

    def test_run_id_is_dropped_under_flow(self, mock_client):
        """A run id only means something beside a workflow id; under --flow
        it would be a stray field the server has no use for."""
        mock_client.post.return_value = {"cancelled": []}
        operations.cancel_flow_runs(mock_client, "#ops", flow_name="claim_turn", run_id="r-1")
        assert "run_id" not in mock_client.post.call_args.kwargs["data"]

    @pytest.mark.parametrize("kwargs", [{}, {"workflow_id": "wf-1", "flow_name": "claim_turn"}])
    def test_exactly_one_selector_is_checked_before_any_request(self, mock_client, kwargs):
        with pytest.raises(PopcornError) as exc:
            operations.cancel_flow_runs(mock_client, "#ops", **kwargs)
        assert exc.value.error_code == "validation"
        mock_client.post.assert_not_called()


class TestDispatch:
    def _run(self, monkeypatch, argv):
        from popcorn_cli import cli

        monkeypatch.setattr(cli, "_check_and_update", lambda: None)
        monkeypatch.setattr(cli, "_get_client", lambda args: object())
        monkeypatch.setattr(sys, "argv", ["popcorn", *argv])
        cli.main()

    def test_flow_form_reaches_the_operation_and_prints_each_run(self, monkeypatch, capsys):
        seen = {}

        def fake(client, conversation, **kw):
            seen.update(kw)
            return {
                "cancelled": [
                    {"workflow_id": "wf-a", "status": "Running", "action": "cancel_requested"},
                    {"workflow_id": "wf-b", "status": "Completed", "action": "already_closed"},
                ],
                "count": 2,
                "more": True,
                "next_page_token": "tok2",
            }

        monkeypatch.setattr(operations, "cancel_flow_runs", fake)
        self._run(
            monkeypatch,
            [
                "flow",
                "runs",
                "cancel",
                "--flow",
                "claim_turn",
                "--channel",
                "#ops",
                "--reason",
                "bad",
            ],
        )

        assert seen["flow_name"] == "claim_turn"
        assert seen["workflow_id"] is None
        assert seen["force"] is False
        assert seen["reason"] == "bad"
        out = capsys.readouterr().out
        assert "wf-a" in out and "cancel_requested" in out
        assert "wf-b" in out and "already_closed" in out
        assert "--page-token" in out

    def test_one_run_form_with_force(self, monkeypatch, capsys):
        seen = {}

        def fake(client, conversation, **kw):
            seen.update(kw)
            return {
                "cancelled": [
                    {"workflow_id": "wf-9", "status": "Terminated", "action": "terminated"}
                ],
                "count": 1,
            }

        monkeypatch.setattr(operations, "cancel_flow_runs", fake)
        self._run(monkeypatch, ["flow", "runs", "cancel", "wf-9", "--channel", "#ops", "--force"])

        assert seen["workflow_id"] == "wf-9"
        assert seen["flow_name"] is None
        assert seen["force"] is True
        out = capsys.readouterr().out
        assert "Terminated" in out and "wf-9" in out
        assert "--page-token" not in out

    def test_json_carries_pagination_next(self, monkeypatch, capsys):
        import json

        monkeypatch.setattr(
            operations,
            "cancel_flow_runs",
            lambda client, conversation, **kw: {
                "cancelled": [],
                "count": 0,
                "more": True,
                "next_page_token": "tok2",
            },
        )
        self._run(
            monkeypatch,
            ["--json", "flow", "runs", "cancel", "--flow", "claim_turn", "--channel", "#ops"],
        )
        data = json.loads(capsys.readouterr().out)["data"]
        assert data["pagination"] == {"next": {"page-token": "tok2"}}


class TestQueueLine:
    def test_get_shows_the_tier_and_origin(self):
        out = "\n".join(
            _run_detail_lines(
                {
                    "status": "Running",
                    "workflow_id": "wf-1",
                    "task_queue": "batch",
                    "trigger_source": "app_user",
                }
            )
        )
        assert "queue:   batch (started by app_user)" in out

    def test_get_without_the_field_prints_no_queue_line(self):
        """An api older than the field sends nothing; the line stays out
        rather than printing a placeholder that reads as a tier."""
        out = "\n".join(_run_detail_lines({"status": "Running", "workflow_id": "wf-1"}))
        assert "queue:" not in out
