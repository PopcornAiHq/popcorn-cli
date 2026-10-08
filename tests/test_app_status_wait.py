"""`app status`: drift exits `unhealthy` with a next step, one envelope under
`--json`, and `--wait-installed` / `channel create --template --wait`.

The drift classification itself is covered by `tests/test_schedule_drift.py`
and the status report by `tests/test_app_publish.py`; this is what a caller
of the command sees — exit codes, the envelope, and the wait.
"""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import ExitStack
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from popcorn_cli import cli
from popcorn_cli.commands import app as mod
from popcorn_core import operations
from popcorn_core.errors import (
    EXIT_TIMEOUT,
    EXIT_UNHEALTHY,
    EXIT_VALIDATION,
    APIError,
    PopcornError,
    ReportedError,
)
from tests.test_app_publish import _install, _status_response

CHANNEL = "#example-chan"
CHANNEL_ID = "00000000-0000-4000-8000-000000000002"

_TICK_MANIFEST = "version: 0.2.0\nschedules:\n  - flow: tick\n    slug: tick\n    interval: 900\n"

# Two on-demand schedules (no cadence) beside one on an interval.
_ON_DEMAND_MANIFEST = """\
version: 0.2.0
schedules:
  - flow: example_tick
    slug: example-tick
    interval: 900
    overlap: skip
  - flow: example_reminder
    slug: example-reminder
    overlap: buffer_one
  - flow: example_release
    slug: example-release
    overlap: buffer_one
"""


def _live(slug: str, **over: Any) -> dict:
    item: dict = {
        "schedule_id": f"channel:c:flow:f:{slug}",
        "slug": slug,
        "cron_expr": None,
        "interval_seconds": 900,
        "offset_seconds": 51,
        "paused": False,
        "note": None,
    }
    item.update(over)
    item["intended"] = {
        "create_time_schedule_class": "periodic",
        "cron_expr": item["cron_expr"],
        "interval_seconds": item["interval_seconds"],
        "offset_seconds": item["offset_seconds"],
    }
    return item


def _on_demand(slug: str, **over: Any) -> dict:
    over.setdefault("paused", True)
    return _live(slug, interval_seconds=None, offset_seconds=None, **over)


def _args(**over: Any) -> argparse.Namespace:
    base = {
        "channel": CHANNEL,
        "directory": "/nonexistent",
        "json": False,
        "quiet": True,
        "no_color": True,
        "wait_installed": False,
        "wait_timeout": None,
    }
    base.update(over)
    return argparse.Namespace(**base)


def _serve(
    *,
    status: Any = None,
    manifest: str = _TICK_MANIFEST,
    live: list[dict] | None = None,
) -> list:
    """Patches for one channel-scoped `app status` read.

    `status` is one `/apps/status` response, an exception, or a list of
    either to serve in turn — the polls of a wait.
    """
    status = _status_response() if status is None else status
    status_patch = (
        patch.object(operations, "get_channel_app_status", side_effect=status)
        if isinstance(status, list | Exception)
        else patch.object(operations, "get_channel_app_status", return_value=status)
    )
    return [
        patch("popcorn_cli.cli._get_client", return_value=object()),
        status_patch,
        patch.object(operations, "get_channel_app_file", return_value={"content": manifest}),
        patch.object(
            operations,
            "list_scheduled_flows",
            return_value={"scheduled_flows": [_live("tick")] if live is None else live},
        ),
        patch.object(operations, "get_scalar", return_value={"scalar": {"value": "prod"}}),
        # Polls are instant: a wait's clock is the test's, not the wall's.
        patch.object(mod.time, "sleep", lambda s: None),
    ]


def _status(args: argparse.Namespace, **serve: Any) -> dict:
    """Run `_app_status`, returning what it printed and what it raised."""
    captured: dict = {"error": None}
    with ExitStack() as stack:
        for p in _serve(**serve):
            stack.enter_context(p)
        stack.enter_context(
            patch(
                "popcorn_cli.cli._output",
                lambda a, data, rendered: captured.update(data=data, rendered=rendered),
            )
        )
        try:
            mod._app_status(args)
        except PopcornError as exc:
            captured["error"] = exc
    return captured


def _main(monkeypatch, capsys, argv: list[str], **serve: Any) -> dict:
    """Run `popcorn <argv>` through `main`: exit code, stdout, stderr."""
    monkeypatch.setattr(sys, "argv", ["popcorn", *argv])
    monkeypatch.setattr(cli, "_check_and_update", lambda: None)
    monkeypatch.setattr(cli, "_quiet", cli._quiet)
    code = 0
    with ExitStack() as stack:
        for p in _serve(**serve):
            stack.enter_context(p)
        try:
            cli.main()
        except SystemExit as exc:
            code = int(exc.code or 0)
    out = capsys.readouterr()
    return {"code": code, "out": out.out, "err": out.err}


# ---------------------------------------------------------------------------
# Drift: on-demand schedules, the exit code, the next step
# ---------------------------------------------------------------------------


class TestDriftExit:
    def test_two_on_demand_schedules_give_a_clean_status(self):
        out = _status(
            _args(),
            manifest=_ON_DEMAND_MANIFEST,
            live=[
                _live("example-tick"),
                _on_demand("example-reminder"),
                _on_demand("example-release"),
            ],
        )
        assert out["error"] is None
        assert out["data"]["schedule_drift"]["alarming"] == 0
        assert out["data"]["schedule_drift"]["clean"] == 3
        assert "OK    example-reminder — on demand" in out["rendered"]

    def test_drift_exits_unhealthy_not_validation(self):
        out = _status(_args(), live=[_live("tick", paused=True)])
        error = out["error"]
        assert isinstance(error, ReportedError)
        assert error.exit_code == EXIT_UNHEALTHY
        assert error.error_code == "unhealthy"

    def test_an_unexplained_pause_names_the_schedule_to_read(self):
        out = _status(_args(), live=[_live("tick", paused=True)])
        step = f"popcorn schedule get tick --channel '{CHANNEL}'"
        assert out["data"]["schedule_drift"]["findings"][0]["next"] == step
        assert f"next: {step}" in out["rendered"]
        assert out["error"].hint == step

    def test_a_missing_schedule_on_a_channel_behind_is_fixed_by_apply(self):
        out = _status(
            _args(),
            status=_status_response(_install(state="failed", error="boom")),
            live=[],
        )
        step = f"popcorn app apply --channel '{CHANNEL}'"
        assert out["data"]["schedule_drift"]["findings"][0]["next"] == step
        assert f"next: {step}" in out["rendered"]

    def test_drift_during_an_install_says_to_wait_for_it(self):
        out = _status(
            _args(),
            status=_status_response(_install(state="installing")),
            live=[],
        )
        step = f"popcorn app status --channel '{CHANNEL}' --wait-installed"
        assert out["data"]["schedule_drift"]["findings"][0]["next"] == step

    def test_clean_findings_carry_no_next_step(self):
        out = _status(_args())
        assert out["data"]["schedule_drift"]["findings"][0]["next"] is None
        assert "next:" not in out["rendered"]

    def test_different_steps_give_no_single_hint(self):
        manifest = _TICK_MANIFEST + "  - flow: tock\n    slug: tock\n    interval: 900\n"
        out = _status(
            _args(),
            manifest=manifest,
            status=_status_response(_install(state="failed")),
            live=[_live("tick", paused=True)],
        )
        nexts = {f["next"] for f in out["data"]["schedule_drift"]["findings"]}
        assert len(nexts) == 2
        assert out["error"].hint is None


# ---------------------------------------------------------------------------
# One envelope per invocation
# ---------------------------------------------------------------------------


class TestOneEnvelope:
    def test_drift_under_json_is_one_success_envelope_and_exit_5(self, monkeypatch, capsys):
        out = _main(
            monkeypatch,
            capsys,
            ["--json", "app", "status", "--channel", CHANNEL],
            live=[_live("tick", paused=True)],
        )
        assert out["code"] == EXIT_UNHEALTHY
        envelope = json.loads(out["out"])
        assert envelope["ok"] is True
        assert envelope["data"]["schedule_drift"]["alarming"] == 1
        assert out["err"] == ""

    def test_drift_in_text_mode_still_says_so_on_stderr(self, monkeypatch, capsys):
        out = _main(
            monkeypatch,
            capsys,
            ["app", "status", "--channel", CHANNEL],
            live=[_live("tick", paused=True)],
        )
        assert out["code"] == EXIT_UNHEALTHY
        assert "DRIFT tick" in out["out"]
        assert "Error: 1 schedule(s) drifted" in out["err"]
        assert "Run: popcorn schedule get tick" in out["err"]

    def test_a_clean_status_exits_zero(self, monkeypatch, capsys):
        out = _main(monkeypatch, capsys, ["--json", "app", "status", "--channel", CHANNEL])
        assert out["code"] == 0
        assert json.loads(out["out"])["ok"] is True

    def test_a_real_error_is_still_one_error_envelope_on_stderr(self, monkeypatch, capsys):
        out = _main(
            monkeypatch,
            capsys,
            ["--json", "app", "status", "--channel", CHANNEL],
            status=APIError("this channel does not run an app bundle", status_code=404),
        )
        assert out["out"] == ""
        envelope = json.loads(out["err"])
        assert envelope["ok"] is False
        assert envelope["error_code"] == "not_found"

    def test_flow_validate_is_one_envelope(self, monkeypatch, capsys, tmp_path):
        flow = tmp_path / "example.yaml"
        flow.write_text("name: example\n")
        with patch.object(
            operations,
            "validate_flow_yaml",
            return_value={"valid": False, "issues": ["boom"]},
        ):
            out = _main(
                monkeypatch,
                capsys,
                ["--json", "flow", "validate", str(flow), "--channel", CHANNEL],
            )
        assert out["code"] == EXIT_VALIDATION
        envelope = json.loads(out["out"])
        assert envelope["ok"] is True
        assert envelope["data"]["invalid"] == 1
        assert out["err"] == ""


# ---------------------------------------------------------------------------
# The wait
# ---------------------------------------------------------------------------


def _not_bound() -> APIError:
    return APIError("this channel does not run an app bundle", status_code=404)


class _Clock:
    """`time.monotonic` advancing by `step` seconds a read."""

    def __init__(self, step: float = 1.0) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def _wait(responses: list, *, timeout: int = 600, step: float = 1.0, **kw: Any):
    with (
        patch.object(operations, "get_channel_app_status", side_effect=responses),
        patch.object(mod.time, "sleep", lambda s: None),
        patch.object(mod.time, "monotonic", _Clock(step)),
    ):
        return mod.wait_for_install(object(), CHANNEL, timeout, **kw)


class TestWaitForInstall:
    def test_not_bound_then_installing_then_current_is_installed(self):
        wait = _wait(
            [
                _not_bound(),
                _not_bound(),
                _status_response(_install(state="installing"), app=None),
                _status_response(_install(state="retrying", attempt=2)),
                _status_response(),
            ]
        )
        assert wait.outcome == mod.WAIT_INSTALLED
        assert wait.state == "current"
        assert wait.error(reported=True) is None

    def test_failed_carries_the_state_and_the_reason(self):
        wait = _wait(
            [
                _status_response(_install(state="installing")),
                _status_response(
                    _install(
                        state="failed",
                        error="manifest.yaml: unknown table",
                        retry_hint="run 'app apply' to retry",
                    )
                ),
            ]
        )
        assert wait.outcome == mod.WAIT_FAILED
        assert (
            wait.message == f"the install on {CHANNEL} ended failed: manifest.yaml: unknown table"
        )
        error = wait.error(reported=True)
        assert isinstance(error, ReportedError)
        assert error.exit_code == EXIT_UNHEALTHY
        assert error.error_code == "unhealthy"
        assert error.hint == "run 'app apply' to retry"

    @pytest.mark.parametrize("state", ["skipped", "locked", "behind", "some_future_state"])
    def test_every_settled_state_but_current_is_a_failure(self, state):
        wait = _wait([_status_response(_install(state=state, reason="stale_target"))])
        assert wait.outcome == mod.WAIT_FAILED
        assert f"ended {state}" in str(wait.message)

    def test_behind_while_the_workflow_is_unreadable_keeps_waiting(self):
        """`live: false` hides a running install as `behind`."""
        wait = _wait(
            [
                _status_response(_install(state="behind", live=False)),
                _status_response(),
            ]
        )
        assert wait.outcome == mod.WAIT_INSTALLED

    def test_times_out_with_the_last_state(self):
        wait = _wait([_status_response(_install(state="installing"))] * 10, timeout=3)
        assert wait.outcome == mod.WAIT_TIMEOUT
        assert "state installing" in str(wait.message)
        error = wait.error(reported=True)
        assert error is not None
        assert error.exit_code == EXIT_TIMEOUT
        assert error.error_code == "timeout"
        assert error.retryable is True

    def test_times_out_on_a_channel_never_bound(self):
        wait = _wait([_not_bound()] * 10, timeout=3)
        assert wait.outcome == mod.WAIT_TIMEOUT
        assert wait.served is None
        assert "no install has started yet" in str(wait.message)

    def test_unbound_is_an_answer_when_nothing_was_started(self):
        wait = _wait([_not_bound()], unbound_is_pending=False)
        assert wait.outcome == mod.WAIT_FAILED
        assert "runs no app bundle" in str(wait.message)

    def test_other_errors_are_raised(self):
        with pytest.raises(APIError):
            _wait([APIError("boom", status_code=500)])


class TestStatusWaitInstalled:
    def test_installed_exits_zero_and_reports_the_wait(self):
        out = _status(
            _args(wait_installed=True),
            status=[_status_response(_install(state="installing")), _status_response()],
        )
        assert out["error"] is None
        assert out["data"]["install_wait"]["outcome"] == "installed"
        assert out["data"]["install_wait"]["state"] == "current"
        assert "Install wait: installed" in out["rendered"]

    def test_a_failed_install_is_reported_then_exits_unhealthy(self):
        out = _status(
            _args(wait_installed=True),
            status=[_status_response(_install(state="failed", error="boom"))],
        )
        assert out["data"]["install_wait"]["outcome"] == "failed"
        assert out["data"]["install"]["error"] == "boom"
        assert isinstance(out["error"], ReportedError)
        assert out["error"].exit_code == EXIT_UNHEALTHY

    def test_a_failed_install_outranks_drift(self):
        out = _status(
            _args(wait_installed=True),
            status=[_status_response(_install(state="failed"))],
            live=[],
        )
        assert out["data"]["schedule_drift"]["alarming"] == 1
        assert "install" in str(out["error"])

    def test_a_timeout_on_a_channel_never_bound_is_one_error(self):
        with patch.object(mod.time, "monotonic", _Clock(step=10)):
            out = _status(_args(wait_installed=True, wait_timeout=5), status=[_not_bound()] * 5)
        assert "data" not in out
        error = out["error"]
        assert not isinstance(error, ReportedError)
        assert error.exit_code == EXIT_TIMEOUT

    def test_wait_timeout_needs_the_wait(self):
        with pytest.raises(PopcornError, match="--wait-installed"):
            mod._app_status(_args(wait_timeout=5))

    @pytest.mark.parametrize("bad", [0, -1, mod.INSTALL_WAIT_MAX_SECONDS + 1])
    def test_wait_timeout_is_bounded(self, bad):
        with pytest.raises(PopcornError, match="between 1 and"):
            mod._app_status(_args(wait_installed=True, wait_timeout=bad))

    def test_exit_codes_through_main(self, monkeypatch, capsys):
        out = _main(
            monkeypatch,
            capsys,
            ["--json", "app", "status", "--channel", CHANNEL, "--wait-installed"],
            status=[_status_response(_install(state="failed", error="boom"))],
        )
        assert out["code"] == EXIT_UNHEALTHY
        envelope = json.loads(out["out"])
        assert envelope["data"]["install_wait"]["message"].endswith("ended failed: boom")
        # Progress lines only: no second envelope.
        assert '"ok"' not in out["err"]


# ---------------------------------------------------------------------------
# `channel create --template --wait`
# ---------------------------------------------------------------------------


def _created(already: bool = False) -> dict:
    return {
        "ok": True,
        "conversation": {"id": CHANNEL_ID, "name": "example-fresh", "type": "workspace_channel"},
        "already_existed": already,
    }


def _create(monkeypatch, capsys, argv: list[str], *, created: dict, statuses: list) -> dict:
    client = MagicMock()
    seen: list[str] = []

    def _status_of(c, conversation):
        seen.append(conversation)
        item = statuses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(sys, "argv", ["popcorn", *argv])
    monkeypatch.setattr(cli, "_check_and_update", lambda: None)
    monkeypatch.setattr(cli, "_quiet", cli._quiet)
    code = 0
    with (
        patch("popcorn_cli.cli._get_client", return_value=client),
        patch.object(operations, "create_conversation", return_value=created) as create,
        patch.object(operations, "get_channel_app_status", side_effect=_status_of),
        patch.object(mod.time, "sleep", lambda s: None),
    ):
        try:
            cli.main()
        except SystemExit as exc:
            code = int(exc.code or 0)
    out = capsys.readouterr()
    return {"code": code, "out": out.out, "err": out.err, "create": create, "seen": seen}


class TestChannelCreateWait:
    def test_waits_for_the_install_by_the_new_channels_id(self, monkeypatch, capsys):
        out = _create(
            monkeypatch,
            capsys,
            ["--json", "channel", "create", "example-fresh", "--template", "example", "--wait"],
            created=_created(),
            statuses=[
                _not_bound(),
                _status_response(_install(state="installing")),
                _status_response(),
            ],
        )
        assert out["code"] == 0
        assert set(out["seen"]) == {CHANNEL_ID}
        data = json.loads(out["out"])["data"]
        assert data["conversation"]["id"] == CHANNEL_ID
        assert data["install_wait"]["outcome"] == "installed"
        assert data["install"]["state"] == "current"

    def test_a_failed_install_reports_the_channel_and_exits_5(self, monkeypatch, capsys):
        out = _create(
            monkeypatch,
            capsys,
            ["--json", "channel", "create", "example-fresh", "--template", "example", "--wait"],
            created=_created(),
            statuses=[_status_response(_install(state="failed", error="boom"), app=None)],
        )
        assert out["code"] == EXIT_UNHEALTHY
        data = json.loads(out["out"])["data"]
        assert data["conversation"]["id"] == CHANNEL_ID
        assert data["install_wait"]["state"] == "failed"
        assert data["install_wait"]["message"] == "the install on #example-fresh ended failed: boom"
        assert '"ok"' not in out["err"]

    def test_an_existing_channel_with_no_app_does_not_wait_out_the_timeout(
        self, monkeypatch, capsys
    ):
        out = _create(
            monkeypatch,
            capsys,
            [
                "channel",
                "create",
                "example-fresh",
                "--template",
                "example",
                "--if-not-exists",
                "--wait",
            ],
            created=_created(already=True),
            statuses=[_not_bound()],
        )
        assert out["code"] == EXIT_UNHEALTHY
        assert "runs no app bundle" in out["err"]

    def test_wait_needs_a_template(self, monkeypatch, capsys):
        out = _create(
            monkeypatch,
            capsys,
            ["channel", "create", "example-fresh", "--wait"],
            created=_created(),
            statuses=[],
        )
        assert out["code"] == EXIT_VALIDATION
        assert "--template" in out["err"]
        out["create"].assert_not_called()

    def test_without_wait_nothing_is_polled(self, monkeypatch, capsys):
        out = _create(
            monkeypatch,
            capsys,
            ["channel", "create", "example-fresh", "--template", "example"],
            created=_created(),
            statuses=[],
        )
        assert out["code"] == 0
        assert out["seen"] == []
        assert "install_wait" not in out["out"]
