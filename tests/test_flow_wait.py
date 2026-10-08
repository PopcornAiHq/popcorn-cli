"""`flow run --wait` polling: branches on the served `outcome`, never on `status`."""

from __future__ import annotations

import pytest

from popcorn_cli.commands.flow import _poll_until_closed
from popcorn_core.errors import EXIT_TIMEOUT, EXIT_VALIDATION, PopcornError

_MISSING = object()


class _Runs:
    """Scripted `get_flow_run`: each entry is `(status, outcome)`, or
    `(status, outcome, extra)` with `extra` merged into the run.

    An outcome of `_MISSING` leaves the field out, as an API older than it does.
    """

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.run_ids = []

    def __call__(self, client, channel, workflow_id, run_id=None, include_errors=False):
        self.calls += 1
        self.run_ids.append(run_id)
        # Keep returning the last entry once the script is exhausted, so a
        # timeout test can poll indefinitely without an IndexError.
        self.include_errors = include_errors
        entry = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        status, outcome, extra = (*entry, {}) if len(entry) == 2 else entry
        run = {"status": status, "workflow_id": workflow_id, **extra}
        if outcome is not _MISSING:
            run["outcome"] = outcome
        return {"run": run}


def _script(monkeypatch, script):
    from popcorn_cli.commands import flow as mod

    runs = _Runs(script)
    monkeypatch.setattr(mod.operations, "get_flow_run", runs, raising=False)
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    return runs


def _freeze_then_expire(monkeypatch):
    """A clock that reads 0 until the fourth look, then is far past any deadline."""
    from popcorn_cli.commands import flow as mod

    clock = iter([0, 1, 2, 99])
    monkeypatch.setattr(mod.time, "monotonic", lambda: next(clock))


# --- each outcome pinned to its result ---------------------------------------


def test_succeeded_returns_the_run(monkeypatch):
    runs = _script(monkeypatch, [("Running", "still_running"), ("Completed", "succeeded")])

    run = _poll_until_closed(None, "#ops", "wid-1", timeout=30)
    assert run["outcome"] == "succeeded"
    # The raw status is still carried for display.
    assert run["status"] == "Completed"
    assert runs.calls == 2


def test_failed_raises_a_non_retryable_validation_exit(monkeypatch):
    runs = _script(monkeypatch, [("Running", "still_running"), ("TimedOut", "failed")])

    with pytest.raises(PopcornError) as exc:
        _poll_until_closed(None, "#ops", "wid-1", timeout=30)
    # Stopped on the outcome, rather than polling out the deadline.
    assert runs.calls == 2
    # The message names the server's status so the caller can tell a timeout
    # from a cancel.
    assert "TimedOut" in str(exc.value)
    assert exc.value.error_code == "validation"
    assert exc.value.exit_code == EXIT_VALIDATION
    # A dead run is not something to come back and wait for again.
    assert exc.value.to_dict()["retryable"] is False


def test_still_running_polls_until_the_deadline(monkeypatch):
    _script(monkeypatch, [("Running", "still_running")])
    _freeze_then_expire(monkeypatch)

    with pytest.raises(PopcornError) as exc:
        _poll_until_closed(None, "#ops", "wid-1", timeout=30)
    assert "timed out" in str(exc.value).lower()
    assert exc.value.error_code == "timeout"
    # A deadline is not bad input. Exiting EXIT_VALIDATION would tell an agent
    # its request was malformed and to stop retrying — the exact opposite of
    # what a wait deadline means, and it defeats the point of --wait.
    assert exc.value.exit_code == EXIT_TIMEOUT
    assert exc.value.exit_code != EXIT_VALIDATION
    # Same correction in the JSON envelope: an agent reading retryable:false
    # would give up on a run that is very likely still going.
    assert exc.value.to_dict()["retryable"] is True


def test_returns_immediately_when_already_succeeded(monkeypatch):
    runs = _script(monkeypatch, [("Completed", "succeeded")])

    _poll_until_closed(None, "#ops", "wid-1", timeout=30)
    assert runs.calls == 1


# --- a failed run says why ----------------------------------------------------

_NESTED_FAILURE = {
    "type": "WorkflowExecutionError",
    "message": "Workflow execution failed",
    "cause": {
        "type": "ActivityError",
        "message": "Activity task failed",
        "cause": {
            "type": "ProcessInvalid",
            "message": "columns: machine 'document' is stored\n  in column 'doc'",
        },
    },
}


def test_failed_run_names_its_innermost_cause(monkeypatch):
    """The outer links are Temporal's wrappers; the root is what explains the
    failure, and having it in the message spares an agent a `runs get` call."""
    runs = _script(monkeypatch, [("Failed", "failed", {"failure": _NESTED_FAILURE})])

    with pytest.raises(PopcornError) as exc:
        _poll_until_closed(None, "#ops", "wid-1", timeout=30)
    assert str(exc.value) == (
        "Flow run wid-1 failed: ProcessInvalid: "
        "columns: machine 'document' is stored in column 'doc'"
    )
    # Taken from the poll that saw the failure, not a second request.
    assert runs.calls == 1
    assert runs.include_errors is True
    assert exc.value.exit_code == EXIT_VALIDATION
    assert exc.value.error_code == "validation"
    assert exc.value.to_dict()["retryable"] is False


def test_failed_run_keeps_a_status_other_than_failed(monkeypatch):
    """A timeout must still read apart from a cancel once a cause is shown."""
    failure = {"type": "TimeoutError", "message": "start-to-close timeout"}
    _script(monkeypatch, [("TimedOut", "failed", {"failure": failure})])

    with pytest.raises(PopcornError) as exc:
        _poll_until_closed(None, "#ops", "wid-1", timeout=30)
    assert str(exc.value) == (
        "Flow run wid-1 failed (TimedOut): TimeoutError: start-to-close timeout"
    )


def test_failed_run_without_a_failure_labels_the_latest_activity_error(monkeypatch):
    history = [
        {
            "activity_type": "a.one",
            "attempt": 1,
            "type": "Boom",
            "message": "old",
            "time": "2026-08-09T10:00:05Z",
        },
        {
            "activity_type": "a.two",
            "attempt": 3,
            "type": "ApplicationError",
            "message": "row already exists",
            "time": "2026-08-09T10:00:09Z",
        },
        {
            "activity_type": "a.one",
            "attempt": 2,
            "type": "Boom",
            "message": "older",
            "time": "2026-08-09T10:00:01Z",
        },
    ]
    _script(monkeypatch, [("Terminated", "failed", {"error_history": history})])

    with pytest.raises(PopcornError) as exc:
        _poll_until_closed(None, "#ops", "wid-1", timeout=30)
    assert str(exc.value) == (
        "Flow run wid-1 ended Terminated "
        "(last activity error: ApplicationError: row already exists)"
    )
    # Not a cause, so `runs get` is still where the answer is.
    assert exc.value.hint == "popcorn flow runs get --channel '#ops' wid-1 --include-errors"
    assert exc.value.exit_code == EXIT_VALIDATION


@pytest.mark.parametrize("status", ["TimedOut", "Terminated"])
def test_a_retried_activity_error_is_not_reported_as_the_cause(monkeypatch, status):
    """`error_history` keeps every failed attempt, including one a retry got
    past. A run that then timed out or was terminated did not fail *because*
    of it, so the message must not say it did."""
    history = [
        {
            "activity_type": "a.fetch",
            "attempt": 1,
            "type": "ConnectionError",
            "message": "connection reset",
            "time": "2026-08-09T10:00:01Z",
        },
    ]
    _script(monkeypatch, [(status, "failed", {"error_history": history})])

    with pytest.raises(PopcornError) as exc:
        _poll_until_closed(None, "#ops", "wid-1", timeout=30)
    message = str(exc.value)
    assert message == (
        f"Flow run wid-1 ended {status} (last activity error: ConnectionError: connection reset)"
    )
    assert "failed" not in message


@pytest.mark.parametrize(
    "extra",
    [{}, {"failure": None, "error_history": []}, {"failure": {"cause": {}}}],
)
def test_failed_run_without_a_cause_keeps_the_status_and_points_at_runs_get(monkeypatch, extra):
    _script(monkeypatch, [("Failed", "failed", extra)])

    with pytest.raises(PopcornError) as exc:
        _poll_until_closed(None, "#ops", "wid-1", timeout=30)
    assert str(exc.value) == "Flow run wid-1 ended Failed"
    # Quoted: an unquoted `#ops` is a comment to the shell.
    assert exc.value.hint == "popcorn flow runs get --channel '#ops' wid-1 --include-errors"
    assert exc.value.exit_code == EXIT_VALIDATION


def test_a_very_long_cause_is_truncated(monkeypatch):
    from popcorn_cli.commands.flow import _CAUSE_MAX_CHARS

    failure = {"type": "ProcessInvalid", "message": "column x is bad; " * 500}
    _script(monkeypatch, [("Failed", "failed", {"failure": failure})])

    with pytest.raises(PopcornError) as exc:
        _poll_until_closed(None, "#ops", "wid-1", timeout=30)
    prefix = "Flow run wid-1 failed: "
    message = str(exc.value)
    assert message.startswith(prefix + "ProcessInvalid: column x is bad;")
    assert message.endswith("…")
    assert len(message) == len(prefix) + _CAUSE_MAX_CHARS


def test_a_timeout_still_says_timed_out_not_failed(monkeypatch):
    """The deadline elapsing is not a failure: no cause, a retryable exit."""
    failure = {"type": "ActivityError", "message": "retrying"}
    _script(monkeypatch, [("Running", "still_running", {"failure": failure})])
    _freeze_then_expire(monkeypatch)

    with pytest.raises(PopcornError) as exc:
        _poll_until_closed(None, "#ops", "wid-1", timeout=30)
    assert str(exc.value) == "Waiting for wid-1 timed out after 30s (last status Running)"
    assert exc.value.exit_code == EXIT_TIMEOUT
    assert exc.value.to_dict()["retryable"] is True


# --- the status no longer decides anything ------------------------------------
#
# Every status paired with an outcome it would never normally carry. If the
# poll still read `status`, one of these would land on the status's verdict
# instead of the outcome's.

_STATUSES = [
    "Running",
    "Completed",
    "Failed",
    "Canceled",
    "Terminated",
    "ContinuedAsNew",
    "TimedOut",
    "",
    "SomethingNew",
]


@pytest.mark.parametrize("status", _STATUSES)
def test_succeeded_ends_the_wait_whatever_the_status(monkeypatch, status):
    runs = _script(monkeypatch, [(status, "succeeded")])

    assert _poll_until_closed(None, "#ops", "wid-1", timeout=30)["outcome"] == "succeeded"
    assert runs.calls == 1


@pytest.mark.parametrize("status", _STATUSES)
def test_failed_ends_the_wait_whatever_the_status(monkeypatch, status):
    runs = _script(monkeypatch, [(status, "failed")])

    with pytest.raises(PopcornError) as exc:
        _poll_until_closed(None, "#ops", "wid-1", timeout=30)
    assert runs.calls == 1
    assert exc.value.exit_code == EXIT_VALIDATION


@pytest.mark.parametrize("status", _STATUSES)
def test_still_running_keeps_polling_whatever_the_status(monkeypatch, status):
    runs = _script(monkeypatch, [(status, "still_running"), (status, "succeeded")])

    assert _poll_until_closed(None, "#ops", "wid-1", timeout=30)["outcome"] == "succeeded"
    assert runs.calls == 2


# --- continued-as-new ----------------------------------------------------------


def test_continued_as_new_follows_the_workflow_not_the_run(monkeypatch):
    """The server answers `still_running` for a run that continued-as-new, and
    the poll asks for the workflow's latest run each time — never a pinned run
    id — so it reaches the successor's real ending instead of re-reading the
    closed first link until the deadline."""
    runs = _script(
        monkeypatch,
        [
            ("ContinuedAsNew", "still_running"),
            ("Running", "still_running"),
            ("Completed", "succeeded"),
        ],
    )

    assert _poll_until_closed(None, "#ops", "wid-1", timeout=30)["status"] == "Completed"
    assert runs.calls == 3
    assert runs.run_ids == [None, None, None]


# --- a response without a usable outcome ------------------------------------


@pytest.mark.parametrize("status", ["Completed", "Failed", "Running"])
def test_missing_outcome_fails_loud_on_the_first_poll(monkeypatch, status):
    """An API older than the field is refused, not second-guessed from
    `status` — even a status that plainly reads as finished."""
    runs = _script(monkeypatch, [(status, _MISSING)])

    with pytest.raises(PopcornError) as exc:
        _poll_until_closed(None, "#ops", "wid-1", timeout=30)
    assert runs.calls == 1
    assert "predates" in str(exc.value)
    assert status in str(exc.value)
    assert exc.value.error_code == "validation"
    assert exc.value.to_dict()["retryable"] is False
    assert "flow runs get" in (exc.value.hint or "")


@pytest.mark.parametrize("outcome", [None, "", "cancelled", "SUCCEEDED", {"v": 1}, ["failed"], 1])
def test_unrecognised_outcome_fails_loud(monkeypatch, outcome):
    runs = _script(monkeypatch, [("Completed", outcome)])

    with pytest.raises(PopcornError) as exc:
        _poll_until_closed(None, "#ops", "wid-1", timeout=30)
    assert runs.calls == 1
    assert exc.value.error_code == "validation"


def test_timeout_error_code_is_in_the_stable_enum():
    """`timeout` is agent-facing, so it must be discoverable via the schema."""
    from popcorn_core.errors import ERROR_CODES

    assert "timeout" in {e["code"] for e in ERROR_CODES}


def test_timeout_exit_code_is_discoverable_in_the_schema(capsys):
    """Agents switch on `popcorn commands --json` exit_codes — a code that is
    not published there is a code they cannot branch on."""
    import argparse
    import json

    from popcorn_cli.cli import cmd_commands

    cmd_commands(argparse.Namespace(groups=None))
    schema = json.loads(capsys.readouterr().out)

    exit_codes = schema["exit_codes"]
    assert exit_codes["timeout"] == EXIT_TIMEOUT
    # Every published code must be distinct, or branching on one is ambiguous.
    assert len(set(exit_codes.values())) == len(exit_codes)
