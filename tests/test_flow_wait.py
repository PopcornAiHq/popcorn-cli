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
