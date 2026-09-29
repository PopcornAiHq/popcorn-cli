"""`flow runs timeline` — one page of a run's steps, newest first.

The server owns ordering, clamping and the cursor. What the CLI owns is
sending `before`/`limit`/`run_id` through, handing the next page's flags back
in `pagination.next` (the run id with the cursor, so a continue-as-new cannot
swap runs mid-sequence), and a text rendering that splits on whitespace.
"""

from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock

import pytest

from popcorn_cli.commands.flow import _timeline_lines
from popcorn_core import operations

_PATH = "/api/customer-flow-runs/timeline"
_RUN = "00000000-0000-4000-8000-000000000001"


def _entry(id_, **extra):
    return {
        "id": id_,
        "kind": "activity",
        "name": "store.upsert_record",
        "step": "save_row",
        "scheduled_time": "2026-09-01T10:00:00Z",
        "started_time": "2026-09-01T10:00:01Z",
        "closed_time": "2026-09-01T10:00:02Z",
        "duration_ms": 1250,
        "attempt": 1,
        "outcome": "completed",
        "failure_message": None,
        **extra,
    }


def _page(entries, next_before=None, **extra):
    return {
        "ok": True,
        "timeline": {
            "workflow_id": "example-flow-1",
            "run_id": _RUN,
            "status": "Running",
            "outcome": "still_running",
            "order": "newest_first",
            "entries": entries,
            "next_before": next_before,
            "has_more": next_before is not None,
            **extra,
        },
    }


@pytest.fixture
def mock_client(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(operations, "resolve_conversation", lambda c, conv: "conv-1")
    return client


class TestOperation:
    def test_first_page_sends_only_the_run_address(self, mock_client):
        mock_client.get.return_value = _page([])
        operations.get_flow_run_timeline(mock_client, "#ops", "example-flow-1")
        mock_client.get.assert_called_once_with(
            _PATH, {"conversation_id": "conv-1", "workflow_id": "example-flow-1"}
        )

    def test_cursor_limit_and_run_id_are_sent(self, mock_client):
        mock_client.get.return_value = _page([])
        operations.get_flow_run_timeline(
            mock_client, "#ops", "example-flow-1", run_id=_RUN, before=40, limit=10
        )
        assert mock_client.get.call_args.args[1] == {
            "conversation_id": "conv-1",
            "workflow_id": "example-flow-1",
            "run_id": _RUN,
            "before": 40,
            "limit": 10,
        }


class TestRendering:
    def _render(self, timeline):
        return "\n".join(_timeline_lines(timeline))

    def test_header_names_the_run_and_its_outcome(self):
        out = self._render(_page([_entry(7)])["timeline"])
        assert out.splitlines()[0] == (
            f"Timeline of example-flow-1 (run {_RUN}): Running, still_running"
        )
        assert "1 entries, newest first" in out

    def test_entry_columns(self):
        line = _timeline_lines(_page([_entry(7)])["timeline"])[2]
        assert line.split() == [
            "7",
            "2026-09-01T10:00:00Z",
            "activity",
            "completed",
            "1.2s",
            "#1",
            "store.upsert_record",
            "save_row",
        ]

    def test_a_timer_with_missing_fields_keeps_every_column(self):
        """Timers and never-started activities have no attempt, duration or
        step; each still renders as a token so the columns stay aligned."""
        timer = _entry(
            3,
            kind="timer",
            name="sleep",
            step=None,
            duration_ms=None,
            attempt=None,
            outcome="running",
        )
        line = _timeline_lines(_page([timer])["timeline"])[2]
        assert line.split() == [
            "3",
            "2026-09-01T10:00:00Z",
            "timer",
            "running",
            "-",
            "-",
            "sleep",
            "-",
        ]
        assert "None" not in line

    def test_sub_second_duration_is_in_ms(self):
        line = _timeline_lines(_page([_entry(7, duration_ms=340)])["timeline"])[2]
        assert "340ms" in line.split()

    def test_failure_message_follows_its_entry(self):
        failed = _entry(9, outcome="failed", attempt=3, failure_message="row already exists")
        lines = _timeline_lines(_page([failed])["timeline"])
        assert "failed" in lines[2].split() and "#3" in lines[2].split()
        assert lines[3].strip() == "row already exists"

    def test_more_pages_say_how_to_get_them(self):
        out = self._render(_page([_entry(60), _entry(51)], next_before=51)["timeline"])
        assert f"--before 51 --run-id {_RUN}" in out

    def test_last_page_has_no_continuation(self):
        out = self._render(_page([_entry(2)])["timeline"])
        assert "--before" not in out

    def test_empty_timeline(self):
        out = self._render(_page([])["timeline"])
        assert "0 entries" in out


class TestDispatch:
    def _run_cli(self, monkeypatch, argv):
        from popcorn_cli import cli

        monkeypatch.setattr(cli, "_check_and_update", lambda: None)
        monkeypatch.setattr(cli, "_get_client", lambda args: object())
        monkeypatch.setattr(sys, "argv", ["popcorn", *argv])
        cli.main()

    def test_json_pages_with_next_before_and_the_resolved_run_id(self, monkeypatch, capsys):
        """Page two is the same command plus pagination.next: `before` is the
        first page's next_before, and the run id the server resolved is pinned."""
        calls = []

        def fake(client, conversation, workflow_id, **kw):
            calls.append({"workflow_id": workflow_id, **kw})
            if kw.get("before") is None:
                return _page([_entry(60), _entry(51)], next_before=51)
            return _page([_entry(40)])

        monkeypatch.setattr(operations, "get_flow_run_timeline", fake)
        base = ["--json", "flow", "runs", "timeline", "example-flow-1", "--channel", "#ops"]
        self._run_cli(monkeypatch, [*base, "--limit", "2"])
        first = json.loads(capsys.readouterr().out)
        assert first["ok"] is True
        data = first["data"]
        assert [e["id"] for e in data["timeline"]["entries"]] == [60, 51]
        assert data["pagination"] == {"next": {"before": "51", "run-id": _RUN}}

        nxt = [f"--{k}={v}" for k, v in data["pagination"]["next"].items()]
        self._run_cli(monkeypatch, [*base, "--limit", "2", *nxt])
        second = json.loads(capsys.readouterr().out)["data"]
        assert second["pagination"] == {"next": None}
        assert calls[0] == {
            "workflow_id": "example-flow-1",
            "run_id": None,
            "before": None,
            "limit": 2,
        }
        assert calls[1] == {
            "workflow_id": "example-flow-1",
            "run_id": _RUN,
            "before": 51,
            "limit": 2,
        }

    def test_text_output(self, monkeypatch, capsys):
        monkeypatch.setattr(
            operations,
            "get_flow_run_timeline",
            lambda client, conversation, workflow_id, **kw: _page([_entry(7)], next_before=7),
        )
        self._run_cli(
            monkeypatch, ["flow", "runs", "timeline", "example-flow-1", "--channel", "#ops"]
        )
        out = capsys.readouterr().out
        assert out.startswith("Timeline of example-flow-1")
        assert "store.upsert_record" in out
        assert "--before 7" in out
