"""`flow runs get` detail rendering — failures, retries, in-flight activities.

`--include-errors` asks the API for `error_history`; before these tests the
text renderer dropped it on the floor, so the flag only did anything under
`--json`. A template author debugging a failed run is the whole audience for
this command.
"""

from __future__ import annotations

from popcorn_cli.commands.flow import _run_detail_lines


def _render(run):
    return "\n".join(_run_detail_lines(run))


class TestHeader:
    def test_core_fields_always_present(self):
        out = _render(
            {
                "status": "COMPLETED",
                "workflow_id": "alert_tick-1",
                "workflow_type": "CustomerFlowInterpreter",
                "run_id": "r-1",
                "start_time": "2026-08-09T10:00:00Z",
                "close_time": "2026-08-09T10:00:05Z",
            }
        )
        assert "COMPLETED" in out
        assert "alert_tick-1" in out
        assert "CustomerFlowInterpreter" in out
        assert "r-1" in out

    def test_missing_fields_render_placeholders_not_crash(self):
        out = _render({})
        assert "?" in out or "-" in out


class TestErrorHistory:
    def test_failed_activity_is_named_with_attempt_and_message(self):
        out = _render(
            {
                "status": "FAILED",
                "error_history": [
                    {
                        "activity_type": "store.upsert_record",
                        "attempt": 3,
                        "message": "row already exists",
                        "type": "ApplicationError",
                        "time": "2026-08-09T10:00:03Z",
                    }
                ],
            }
        )
        # The activity name is the only identifier the API gives us for which
        # part of the flow blew up — it must survive to the output.
        assert "store.upsert_record" in out
        assert "row already exists" in out
        assert "3" in out
        assert "ApplicationError" in out

    def test_every_entry_is_rendered_not_just_the_first(self):
        out = _render(
            {
                "error_history": [
                    {"activity_type": "a.one", "attempt": 1, "message": "first"},
                    {"activity_type": "a.two", "attempt": 2, "message": "second"},
                ]
            }
        )
        assert "a.one" in out and "a.two" in out
        assert "first" in out and "second" in out

    def test_absent_error_history_adds_no_noise(self):
        out = _render({"status": "COMPLETED"})
        assert "error" not in out.lower()

    def test_entry_missing_optional_fields_still_renders(self):
        """`type` and `time` are Optional on the API dataclass."""
        out = _render(
            {"error_history": [{"activity_type": "a.one", "attempt": 1, "message": "boom"}]}
        )
        assert "a.one" in out and "boom" in out


class TestTerminalFailure:
    def test_failure_message_and_type_shown(self):
        out = _render(
            {
                "status": "FAILED",
                "failure": {"message": "flow aborted", "type": "ApplicationError"},
            }
        )
        assert "flow aborted" in out
        assert "ApplicationError" in out

    def test_cause_chain_is_unwound(self):
        """The API recursively unwinds `cause`; the root cause is usually the
        line that actually explains the failure."""
        out = _render(
            {
                "failure": {
                    "message": "step failed",
                    "type": "ApplicationError",
                    "cause": {
                        "message": "invalid phone number",
                        "type": "ValueError",
                        "cause": None,
                    },
                }
            }
        )
        assert "step failed" in out
        assert "invalid phone number" in out

    def test_null_failure_is_not_rendered(self):
        """`failure` is always present in the payload, null when there is none."""
        out = _render({"status": "COMPLETED", "failure": None})
        assert "None" not in out


class TestInFlightActivities:
    def test_running_activity_shows_attempt_of_max(self):
        out = _render(
            {
                "status": "RUNNING",
                "current_activities": [
                    {
                        "activity_type": "agent.transform",
                        "state": "STARTED",
                        "attempt": 2,
                        "maximum_attempts": 3,
                    }
                ],
            }
        )
        assert "agent.transform" in out
        assert "2" in out and "3" in out

    def test_pending_activity_last_failure_is_surfaced(self):
        """A retrying activity carries the failure that caused the retry — the
        reason a run is stuck, and invisible without it."""
        out = _render(
            {
                "current_activities": [
                    {
                        "activity_type": "http.request",
                        "state": "STARTED",
                        "attempt": 4,
                        "maximum_attempts": 5,
                        "last_failure": {"message": "connection refused", "type": None},
                    }
                ]
            }
        )
        assert "connection refused" in out


class TestInputsAndVersion:
    """`inputs` and `version_id` are null for a run started on an older build
    or never pinned, and absent from an api older than the fields."""

    def test_present(self):
        out = _render({"version_id": 12, "inputs": {"ticket": "example-1", "limit": 5}})
        assert "  version: 12" in out
        assert "  inputs:" in out
        assert '    ticket: "example-1"' in out
        assert "    limit: 5" in out

    def test_nested_value_is_json(self):
        out = _render({"inputs": {"filter": {"status": "open"}}})
        assert '    filter: {"status": "open"}' in out

    def test_null_prints_a_dash(self):
        out = _render({"version_id": None, "inputs": None})
        assert "  version: -" in out
        assert "  inputs:  -" in out
        assert "None" not in out

    def test_empty_inputs_are_shown_as_empty(self):
        assert "  inputs:  {}" in _render({"inputs": {}})

    def test_absent_prints_nothing(self):
        out = _render({"status": "COMPLETED"})
        assert "version" not in out
        assert "inputs" not in out
