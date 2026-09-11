"""`popcorn schedule` — operations, ref resolution, and rendering."""

from __future__ import annotations

import argparse
from unittest.mock import MagicMock, patch

import pytest

from popcorn_cli.commands.schedule import _cadence, _humanize_seconds, _schedule_list
from popcorn_core import operations
from popcorn_core.errors import PopcornError


@pytest.fixture(autouse=True)
def _patch_resolve():
    with patch("popcorn_core.operations.resolve_conversation", side_effect=lambda _c, ref: ref):
        yield


def _item(**over):
    base = {
        "schedule_id": "channel:conv-1:flow:claim_tick:claim-tick",
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
            == "channel:conv-1:flow:claim_tick:claim-tick"
        )

    def test_a_full_schedule_id_is_not_looked_up(self, mock_client):
        """The composite passes through, so `get` costs one request, not two."""
        mock_client.get.return_value = {"scheduled_flow": _item()}
        operations.get_scheduled_flow(mock_client, "conv-1", "channel:conv-1:flow:f:s")
        assert mock_client.get.call_count == 1

    def test_flow_id_resolves_when_it_differs_from_the_slug(self, mock_client):
        mock_client.get.side_effect = [
            {"scheduled_flows": [_item()], "count": 1},
            {"scheduled_flow": _item()},
        ]
        operations.get_scheduled_flow(mock_client, "conv-1", "claim_tick")
        assert (
            mock_client.get.call_args.args[1]["schedule_id"]
            == "channel:conv-1:flow:claim_tick:claim-tick"
        )

    def test_slug_wins_over_flow_id_on_a_cross_match(self, mock_client):
        """A ref matching one schedule's slug and another's flow_id takes the
        slug — the slug is the schedule's own name, the flow_id is shared by
        every schedule running that flow."""
        other = _item(slug="claim_tick", flow_id="other_flow", schedule_id="channel:c:flow:o:x")
        mock_client.get.side_effect = [
            {"scheduled_flows": [_item(), other], "count": 2},
            {"scheduled_flow": other},
        ]
        operations.get_scheduled_flow(mock_client, "conv-1", "claim_tick")
        assert mock_client.get.call_args.args[1]["schedule_id"] == "channel:c:flow:o:x"

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


class TestRendering:
    def _render(self, items):
        captured = {}
        client = MagicMock()
        args = argparse.Namespace(channel="#ops", json=False)
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
