"""`popcorn schedule` — operations, ref resolution, and rendering."""

from __future__ import annotations

import argparse
from unittest.mock import MagicMock, patch

import pytest

from popcorn_cli.commands.schedule import (
    _cadence,
    _humanize_seconds,
    _schedule_list,
    _schedule_trigger,
)
from popcorn_core import operations
from popcorn_core.errors import PopcornError


@pytest.fixture(autouse=True)
def _patch_resolve():
    with patch("popcorn_core.operations.resolve_conversation", side_effect=lambda _c, ref: ref):
        yield


def _item(**over):
    base = {
        "schedule_id": "project:conv-1:flow:claim_tick:claim-tick",
        "conversation_id": "conv-1",
        "flow_id": "claim_tick",
        "slug": "claim-tick",
        "cron_expr": None,
        "interval_seconds": 180,
        "timezone": "UTC",
        "paused": False,
        "note": "auto-resumed: set_app_mode",
        "overlap_policy": "skip",
        "jitter_seconds": 60,
        "inputs": {"conversation_id": "conv-1"},
        "next_run_at": "2026-09-11T20:10:51Z",
        "last_run_at": "2026-09-11T20:07:51Z",
        "num_actions": 12,
        "num_actions_skipped_overlap": 0,
        "num_actions_missed_catchup_window": 0,
        "running_count": 0,
    }
    base.update(over)
    return base


class TestOperations:
    def test_list_hits_the_live_endpoint(self, mock_client):
        mock_client.get.return_value = {"scheduled_flows": [], "count": 0}
        operations.list_scheduled_flows(mock_client, "conv-1")
        mock_client.get.assert_called_once_with(
            "/api/customer-scheduled-flows/list", {"conversation_id": "conv-1"}
        )

    def test_get_resolves_a_slug_to_the_composite_id(self, mock_client):
        mock_client.get.side_effect = [
            {"scheduled_flows": [_item()], "count": 1},
            {"scheduled_flow": _item()},
        ]
        operations.get_scheduled_flow(mock_client, "conv-1", "claim-tick")
        assert mock_client.get.call_args.args[0] == "/api/customer-scheduled-flows/get"
        assert (
            mock_client.get.call_args.args[1]["schedule_id"]
            == "project:conv-1:flow:claim_tick:claim-tick"
        )

    def test_a_full_schedule_id_is_not_looked_up(self, mock_client):
        """The composite passes through, so `get` costs one request, not two."""
        mock_client.get.return_value = {"scheduled_flow": _item()}
        operations.get_scheduled_flow(mock_client, "conv-1", "project:conv-1:flow:f:s")
        assert mock_client.get.call_count == 1

    def test_flow_id_resolves_when_it_differs_from_the_slug(self, mock_client):
        mock_client.get.side_effect = [
            {"scheduled_flows": [_item()], "count": 1},
            {"scheduled_flow": _item()},
        ]
        operations.get_scheduled_flow(mock_client, "conv-1", "claim_tick")
        assert (
            mock_client.get.call_args.args[1]["schedule_id"]
            == "project:conv-1:flow:claim_tick:claim-tick"
        )

    def test_slug_wins_over_flow_id_on_a_cross_match(self, mock_client):
        """A ref matching one schedule's slug and another's flow_id takes the
        slug — the slug is the schedule's own name, the flow_id is shared by
        every schedule running that flow."""
        other = _item(slug="claim_tick", flow_id="other_flow", schedule_id="project:c:flow:o:x")
        mock_client.get.side_effect = [
            {"scheduled_flows": [_item(), other], "count": 2},
            {"scheduled_flow": other},
        ]
        operations.get_scheduled_flow(mock_client, "conv-1", "claim_tick")
        assert mock_client.get.call_args.args[1]["schedule_id"] == "project:c:flow:o:x"

    def test_an_unknown_ref_names_what_is_there(self, mock_client):
        mock_client.get.return_value = {"scheduled_flows": [_item()], "count": 1}
        with pytest.raises(PopcornError) as exc:
            operations.get_scheduled_flow(mock_client, "conv-1", "nope")
        assert exc.value.error_code == "not_found"
        assert "claim-tick" in str(exc.value)


class TestCadence:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [(180, "3m"), (900, "15m"), (3600, "1h"), (7200, "2h"), (90, "90s"), (45, "45s")],
    )
    def test_humanize(self, seconds, expected):
        assert _humanize_seconds(seconds) == expected

    def test_interval_form(self):
        assert _cadence(_item()) == "every 3m"

    def test_cron_form_carries_the_timezone(self):
        item = _item(interval_seconds=None, cron_expr="58 8 * * *", timezone="America/New_York")
        assert _cadence(item) == "cron 58 8 * * * (America/New_York)"

    def test_neither_form_does_not_crash(self):
        assert _cadence(_item(interval_seconds=None, cron_expr=None)) == "no cadence"


_SID = "project:conv-1:flow:claim_tick:claim-tick"


class TestTriggerOperation:
    def test_posts_the_resolved_id_with_no_body_by_default(self, mock_client):
        mock_client.get.return_value = {"scheduled_flows": [_item()], "count": 1}
        mock_client.post.return_value = {"schedule_id": _SID, "workflow_id": "wf-1"}
        operations.trigger_scheduled_flow(mock_client, "conv-1", "claim-tick")
        mock_client.post.assert_called_once_with(
            "/api/customer-scheduled-flows/trigger",
            {},
            {"conversation_id": "conv-1", "schedule_id": _SID},
        )

    def test_overlap_policy_rides_in_the_body(self, mock_client):
        mock_client.post.return_value = {"schedule_id": _SID}
        operations.trigger_scheduled_flow(mock_client, "conv-1", _SID, "allow_all")
        body = mock_client.post.call_args.args[1]
        assert body == {"overlap_policy": "allow_all"}
        mock_client.get.assert_not_called()  # a full id needs no lookup

    def test_an_unknown_ref_is_refused_before_any_trigger(self, mock_client):
        mock_client.get.return_value = {"scheduled_flows": [_item()], "count": 1}
        with pytest.raises(PopcornError, match="No schedule 'nope'"):
            operations.trigger_scheduled_flow(mock_client, "conv-1", "nope")
        mock_client.post.assert_not_called()


class TestTriggerRendering:
    def _render(self, resp, overlap=None):
        captured = {}
        args = argparse.Namespace(
            project="#ops", schedule="claim-tick", overlap_policy=overlap, json=False
        )
        with (
            patch("popcorn_cli.cli._get_client", return_value=MagicMock()),
            patch(
                "popcorn_cli.cli._output",
                side_effect=lambda _a, data, text: captured.update(data=data, text=text),
            ),
            patch("popcorn_core.operations.trigger_scheduled_flow", return_value=resp) as trigger,
        ):
            _schedule_trigger(args)
        captured["call"] = trigger.call_args
        return captured

    def test_a_started_run_says_how_to_follow_it(self):
        out = self._render(
            {
                "ok": True,
                "schedule_id": _SID,
                "workflow_id": "wf-1",
                "run_id": "run-1",
                "skipped_overlap": False,
            }
        )
        assert "wf-1" in out["text"] and "run-1" in out["text"]
        assert "popcorn flow runs get wf-1 --project '#ops'" in out["text"]

    def test_an_overlap_skip_is_not_reported_as_triggered(self):
        out = self._render(
            {"schedule_id": _SID, "workflow_id": None, "run_id": None, "skipped_overlap": True}
        )
        assert "Not run" in out["text"]
        assert "Triggered" not in out["text"]
        assert "--overlap-policy allow_all" in out["text"]

    def test_unobserved_ids_are_not_called_a_failure(self):
        out = self._render(
            {"schedule_id": _SID, "workflow_id": None, "run_id": None, "skipped_overlap": False}
        )
        assert "no run was seen starting yet" in out["text"]
        assert "fail" not in out["text"].lower()

    def test_json_is_the_served_response(self):
        resp = {
            "schedule_id": _SID,
            "workflow_id": "wf-1",
            "run_id": "run-1",
            "skipped_overlap": False,
        }
        assert self._render(resp)["data"] == resp

    def test_the_overlap_flag_reaches_the_operation(self):
        out = self._render({"schedule_id": _SID, "workflow_id": "wf-1"}, overlap="allow_all")
        assert out["call"].args[1:] == ("#ops", "claim-tick", "allow_all")


class TestRendering:
    def _render(self, items):
        captured = {}
        client = MagicMock()
        args = argparse.Namespace(project="#ops", json=False)
        with (
            patch("popcorn_cli.cli._get_client", return_value=client),
            patch(
                "popcorn_cli.cli._output",
                side_effect=lambda _a, _d, text: captured.update(text=text),
            ),
            patch(
                "popcorn_core.operations.list_scheduled_flows",
                return_value={"scheduled_flows": items, "count": len(items)},
            ),
        ):
            _schedule_list(args)
        return captured["text"]

    def test_list_shows_cadence_and_next_run(self):
        text = self._render([_item()])
        assert "claim-tick" in text and "every 3m" in text
        assert "2026-09-11T20:10:51Z" in text

    def test_paused_is_visible(self):
        """A paused schedule looks identical to a live one by cadence alone —
        the whole point of reading live state is catching that it is off."""
        assert "[paused]" in self._render([_item(paused=True)])
        assert "[paused]" not in self._render([_item(paused=False)])

    def test_empty_channel_renders_a_count(self):
        assert "(0)" in self._render([])
