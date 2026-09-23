"""`flow get`'s trigger section, rendered from the server's report.

The server owns the analysis of what starts a flow; the CLI's job is to ask
for it and say it without overstating it. The verdict these tests protect is
the negative one: "nothing on this channel starts this flow" finds a dead
flow, so it may only print when the server says every source was read — and
never when the server did not send a report at all.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from popcorn_cli import registry
from popcorn_cli.cli import build_parser
from popcorn_cli.commands.flow import _TRIGGERS_UNSUPPORTED, _render_triggers
from popcorn_core import operations

_FLOW = {"id": "example_turn", "name": "example_turn", "version": 3}

# One of each kind, as the server serializes them: flat, `kind` + `summary`
# plus that kind's own fields. Summaries are the server's sentences.
_SCHEDULE = {
    "kind": "schedule",
    "summary": "schedule 'example-tick' — every 180s",
    "slug": "example-tick",
    "interval_seconds": 180,
    "paused": False,
}
_WEBHOOK_OFF = {
    "kind": "webhook",
    "summary": "webhook 'Intake' is DISABLED",
    "name": "Intake",
    "is_active": False,
}
_MESSAGE = {
    "kind": "message",
    "summary": "a message posted in this channel runs this flow",
    "enabled": True,
}
_DOCUMENT = {
    "kind": "document",
    "summary": "uploading the 'questions' channel document runs this flow",
    "document": "questions",
}
_STATE = {
    "kind": "state",
    "summary": "state edge staff.retain IS this flow — button 'Retain'",
    "event": "staff.retain",
    "via": "edge",
}
_FLOW_CALL = {
    "kind": "flow",
    "summary": "example_eval step 'turns' (foreach) calls this flow",
    "flow": "example_eval",
    "step": "turns",
    "via": "call_flow",
    "foreach": True,
}
_DYNAMIC = {
    "kind": "flow",
    "summary": "example_cta step 'launch' launches a flow named at run time ($launch.flow)",
    "flow": "example_cta",
    "flow_name_expression": "$launch.flow",
}


def _report(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "flow_name": "example_turn",
        "app": "example-app",
        "semver": "1.0.0",
        "has_trigger": False,
        "complete": True,
        "agent_runnable": False,
        "triggers": [],
        "dynamic_callers": [],
        "unread": [],
    }
    base.update(over)
    base["has_trigger"] = bool(base["triggers"])
    base["complete"] = over.get("complete", not base["unread"])
    return base


def _render(report: dict[str, Any] | None, error: str | None = None) -> str:
    return "\n".join(_render_triggers(report, error))


class TestRendering:
    @pytest.mark.parametrize(
        "trigger", [_SCHEDULE, _WEBHOOK_OFF, _MESSAGE, _DOCUMENT, _STATE, _FLOW_CALL]
    )
    def test_each_kind_prints_the_server_summary_verbatim(self, trigger):
        out = _render(_report(triggers=[trigger]))
        assert f"    {trigger['summary']}" in out.splitlines()
        assert "nothing on this channel" not in out

    def test_nothing_starts_it_is_said_when_every_source_was_read(self):
        out = _render(_report())
        assert "Triggers: nothing on this channel starts this flow" in out
        assert "bundle" not in out

    def test_an_incomplete_report_withholds_the_negative_verdict(self):
        out = _render(_report(unread=[{"source": "schedules", "error": "timed out"}]))
        assert "nothing" not in out
        assert "none found" in out
        assert "Not read: schedules — timed out" in out

    def test_complete_false_alone_withholds_the_verdict(self):
        out = _render(_report(complete=False))
        assert "nothing" not in out
        assert "none found" in out

    def test_the_absence_of_a_schedule_is_said_in_words(self):
        out = _render(_report(triggers=[_FLOW_CALL]))
        assert "no schedule of its own" in out

    def test_a_scheduled_flow_does_not_also_claim_it_has_no_schedule(self):
        out = _render(_report(triggers=[_SCHEDULE]))
        assert "no schedule of its own" not in out

    def test_no_schedule_is_not_claimed_when_schedules_were_unread(self):
        out = _render(
            _report(triggers=[_FLOW_CALL], unread=[{"source": "schedules", "error": "down"}])
        )
        assert "no schedule of its own" not in out
        assert "Not read: schedules — down" in out

    def test_operator_only_is_spelled_out(self):
        assert "not agent-runnable — 'flow run' is operator-only" in _render(_report())

    def test_an_agent_runnable_flow_says_the_agent_may_run_it(self):
        out = _render(_report(agent_runnable=True))
        assert "the channel agent may run it" in out
        assert "operator-only" not in out

    def test_a_dynamic_launcher_softens_the_verdict_and_prints_after_it(self):
        out = _render(_report(dynamic_callers=[_DYNAMIC]))
        assert "nothing on this channel starts this flow" not in out
        assert (
            "Triggers: nothing names this flow as its target — "
            "1 run-time launcher may start it (below)"
        ) in out
        assert out.index("nothing names this flow") < out.index("Unresolved:")
        assert f"    Unresolved: {_DYNAMIC['summary']}" in out.splitlines()

    def test_several_dynamic_launchers_are_counted(self):
        out = _render(_report(dynamic_callers=[_DYNAMIC, _DYNAMIC]))
        assert "2 run-time launchers may start it" in out

    def test_unread_sources_withhold_the_verdict_even_if_complete_says_true(self):
        out = _render(_report(complete=True, unread=[{"source": "webhooks", "error": "boom"}]))
        assert "nothing" not in out
        assert "none found" in out
        assert "    Not read: webhooks — boom" in out.splitlines()

    def test_not_checked_says_why_rather_than_reporting_no_triggers(self):
        out = _render(None, _TRIGGERS_UNSUPPORTED)
        assert f"Triggers: not checked — {_TRIGGERS_UNSUPPORTED}" in out
        assert "nothing" not in out


def _run(argv: list[str], response: dict[str, Any]) -> tuple[list[str], Any]:
    args = build_parser().parse_args(argv)
    printed: list[str] = []
    with (
        patch("popcorn_cli.cli._get_client", return_value=object()),
        patch.object(operations, "get_flow", return_value=response) as get_flow,
        patch("builtins.print", lambda *a, **k: printed.append(" ".join(map(str, a)))),
    ):
        assert registry.dispatch(args)
    return printed, get_flow


class TestCommand:
    def test_triggers_are_requested_by_default(self):
        _, get_flow = _run(
            ["--json", "flow", "get", "example_turn", "--channel", "#x"],
            {"ok": True, "flow": dict(_FLOW), "triggers": _report()},
        )
        assert get_flow.call_args.kwargs["include_triggers"] is True

    def test_no_triggers_does_not_request_them(self):
        printed, get_flow = _run(
            ["flow", "get", "example_turn", "--channel", "#x", "--no-triggers"],
            {"ok": True, "flow": dict(_FLOW), "triggers": None},
        )
        assert get_flow.call_args.kwargs["include_triggers"] is False
        assert "Triggers" not in "\n".join(printed)

    def test_no_triggers_json_keeps_the_same_key_set(self):
        printed, _ = _run(
            ["--json", "flow", "get", "example_turn", "--channel", "#x", "--no-triggers"],
            {"ok": True, "flow": dict(_FLOW)},
        )
        data = json.loads(printed[0])["data"]
        assert data["triggers"] is None
        assert "triggers_error" in data
        assert data["triggers_error"] is None

    def test_json_carries_the_server_report_verbatim(self):
        report = _report(triggers=[_FLOW_CALL], dynamic_callers=[_DYNAMIC])
        printed, _ = _run(
            ["--json", "flow", "get", "example_turn", "--channel", "#x"],
            {"ok": True, "flow": dict(_FLOW), "triggers": json.loads(json.dumps(report))},
        )
        out = json.loads(printed[0])
        assert out["ok"] is True
        assert out["data"]["flow"] == _FLOW
        assert out["data"]["triggers"] == report
        assert out["data"]["triggers_error"] is None

    @pytest.mark.parametrize("absent", [True, False], ids=["absent", "null"])
    def test_an_old_server_is_not_checked_and_keeps_the_definition(self, absent):
        response: dict[str, Any] = {"ok": True, "flow": dict(_FLOW)}
        if not absent:
            response["triggers"] = None
        printed, _ = _run(["--json", "flow", "get", "example_turn", "--channel", "#x"], response)
        out = json.loads(printed[0])
        assert out["data"]["flow"] == _FLOW
        assert out["data"]["triggers"] is None
        assert out["data"]["triggers_error"] == _TRIGGERS_UNSUPPORTED

    def test_an_old_server_renders_not_checked_under_the_definition(self):
        printed, _ = _run(
            ["flow", "get", "example_turn", "--channel", "#x"],
            {"ok": True, "flow": dict(_FLOW)},
        )
        text = "\n".join(printed)
        assert text.startswith("example_turn (v3)")
        assert "  id: example_turn" in text
        assert f"Triggers: not checked — {_TRIGGERS_UNSUPPORTED}" in text
        assert "nothing" not in text
