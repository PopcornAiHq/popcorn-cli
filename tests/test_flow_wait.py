"""`flow run --wait` polling: terminal detection, failure exit, timeout."""

from __future__ import annotations

import pytest

from popcorn_cli.commands.flow import _poll_until_closed
from popcorn_core.errors import EXIT_TIMEOUT, EXIT_VALIDATION, PopcornError


class _Runs:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = 0

    def __call__(self, client, channel, workflow_id, run_id=None, include_errors=False):
        self.calls += 1
        # Keep returning the last status once the script is exhausted, so a
        # timeout test can poll indefinitely without an IndexError.
        if len(self.statuses) > 1:
            status = self.statuses.pop(0)
        else:
            status = self.statuses[0]
        return {"run": {"status": status, "workflow_id": workflow_id}}


def _script(monkeypatch, statuses):
    from popcorn_cli.commands import flow as mod

    runs = _Runs(statuses)
    monkeypatch.setattr(mod.operations, "get_flow_run", runs, raising=False)
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    return runs


def test_polls_until_completed(monkeypatch):
    runs = _script(monkeypatch, ["RUNNING", "RUNNING", "COMPLETED"])

    run = _poll_until_closed(None, "#ops", "wid-1", timeout=30)
    assert run["status"] == "COMPLETED"
    assert runs.calls == 3


def test_returns_immediately_when_already_terminal(monkeypatch):
    runs = _script(monkeypatch, ["COMPLETED"])

    _poll_until_closed(None, "#ops", "wid-1", timeout=30)
    assert runs.calls == 1


def test_lowercase_status_is_still_terminal(monkeypatch):
    """Status casing is not something to bet the poll loop on."""
    _script(monkeypatch, ["completed"])

    assert _poll_until_closed(None, "#ops", "wid-1", timeout=30)["status"] == "completed"


@pytest.mark.parametrize("bad", ["FAILED", "TIMED_OUT", "CANCELED", "TERMINATED"])
def test_raises_on_every_bad_terminal_status(monkeypatch, bad):
    _script(monkeypatch, ["RUNNING", bad])

    with pytest.raises(PopcornError) as exc:
        _poll_until_closed(None, "#ops", "wid-1", timeout=30)
    assert bad in str(exc.value)
    # Non-zero exit so a shell/agent sees the failure.
    assert exc.value.exit_code == EXIT_VALIDATION


# Every execution status the flow run API can put in `run.status`, and how the
# poll loop must treat it.
#
# TRANSCRIPTION. The server maps Temporal's execution-status enum to these
# canonical strings and returns them verbatim; the mapping lives in a private
# repo this CLI cannot import, so the vocabulary is copied here by hand and
# this test is the only thing holding the two in step.
#
# When this fails, a status was added or respelled server-side. Do NOT delete
# the offending entry or relax the assertion: decide what `--wait` should do
# with the new status, put it in the matching set in
# `popcorn_cli.commands.flow`, and add it here. Leaving it out of both is the
# failure this pin exists to prevent — an unmatched status is not "unknown, be
# careful", it is "poll until the deadline and then claim the run may still be
# going".
_SERVER_STATUSES = {
    "Running": "in_flight",
    "Completed": "ok",
    "Failed": "bad",
    "Canceled": "bad",
    "Terminated": "bad",
    "ContinuedAsNew": "in_flight",
    "TimedOut": "bad",
}


def test_terminal_sets_classify_every_status_the_server_can_emit():
    from popcorn_cli.commands import flow as mod

    by_bucket = {
        "ok": mod._TERMINAL_OK,
        "bad": mod._TERMINAL_BAD,
        "in_flight": mod._IN_FLIGHT,
    }
    for status, bucket in _SERVER_STATUSES.items():
        assert status in by_bucket[bucket], f"{status} missing from {bucket}"

    # And nothing else: a set carrying a status the server never sends is a
    # spelling the CLI invented, which is how `TIMED_OUT` survived unnoticed.
    classified = set().union(*by_bucket.values())
    assert classified == set(_SERVER_STATUSES)


@pytest.mark.parametrize("status", [s for s, b in _SERVER_STATUSES.items() if b == "bad"])
def test_server_spelled_failure_ends_the_wait(monkeypatch, status):
    """The spellings as the API actually sends them, not upper-cased guesses."""
    runs = _script(monkeypatch, ["Running", status])

    with pytest.raises(PopcornError) as exc:
        _poll_until_closed(None, "#ops", "wid-1", timeout=30)
    # Stopped on the status, rather than polling out the deadline.
    assert runs.calls == 2
    assert status in str(exc.value)
    assert exc.value.error_code == "validation"
    assert exc.value.exit_code == EXIT_VALIDATION
    # A dead run is not something to come back and wait for again.
    assert exc.value.to_dict()["retryable"] is False


def test_server_spelled_completion_ends_the_wait(monkeypatch):
    runs = _script(monkeypatch, ["Running", "Completed"])

    assert _poll_until_closed(None, "#ops", "wid-1", timeout=30)["status"] == "Completed"
    assert runs.calls == 2


def test_continued_as_new_keeps_polling(monkeypatch):
    """A run that continued-as-new handed its work to a successor run — the
    flow is still going, so waiting on it is the point, not a missed match."""
    runs = _script(monkeypatch, ["ContinuedAsNew", "ContinuedAsNew", "Completed"])

    assert _poll_until_closed(None, "#ops", "wid-1", timeout=30)["status"] == "Completed"
    assert runs.calls == 3


def test_raises_on_timeout(monkeypatch):
    from popcorn_cli.commands import flow as mod

    _script(monkeypatch, ["RUNNING"] * 50)
    clock = iter([0, 1, 2, 99])
    monkeypatch.setattr(mod.time, "monotonic", lambda: next(clock))

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
