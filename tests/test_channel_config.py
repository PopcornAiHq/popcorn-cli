"""Tests for `popcorn channel-config`.

The load-bearing test is `test_sends_only_the_new_keys_as_a_patch`. `PUT
/channel-config/parameters` replaces the whole section, so a per-key `set`
sent there as only the new key silently deletes every other parameter and
reports success; built as a read-merge-PUT instead, it loses a concurrent
edit's keys. Only the PATCH, which merges on the server, is safe.
"""

from __future__ import annotations

import argparse
from unittest.mock import patch

import pytest

from popcorn_core import operations
from popcorn_core.channel_config import (
    FATAL_COMPARISON_FIELDS,
    fatal_findings,
    parameters_of,
    parse_assignments,
)
from popcorn_core.errors import EXIT_UNHEALTHY, PopcornError


def _args(**over):
    base = {
        "channel": "#alerts",
        "strict": False,
        "replace": False,
        "assignment": [],
        "key": [],
        "name": None,
        "integration_id": None,
        "json": False,
        "quiet": True,
        "no_color": True,
    }
    base.update(over)
    return argparse.Namespace(**base)


def _inspect(parameters=None, comparison=None, **over):
    payload = {
        "ok": True,
        "config_found": True,
        "config_error": None,
        "channel_parameters": parameters if parameters is not None else {},
        "integrations": {},
        "flows": [],
        "comparison": {
            "missing_parameters": [],
            "unused_parameters": [],
            "missing_integrations": [],
            "unused_integrations": [],
            "provider_mismatches": [],
        },
    }
    if comparison:
        payload["comparison"].update(comparison)
    payload.update(over)
    return payload


# ---------------------------------------------------------------------------
# parse_assignments
# ---------------------------------------------------------------------------


class TestParseAssignments:
    def test_leaves_a_bare_word_as_a_string(self):
        assert parse_assignments(["tone=crisp"]) == {"tone": "crisp"}

    def test_parses_json_scalars(self):
        got = parse_assignments(["retries=3", "enabled=true", "ratio=0.5"])
        assert got == {"retries": 3, "enabled": True, "ratio": 0.5}

    def test_parses_a_json_object(self):
        got = parse_assignments(['limits={"max": 5}'])
        assert got == {"limits": {"max": 5}}

    def test_keeps_an_equals_sign_in_the_value(self):
        assert parse_assignments(["q=a=b"]) == {"q": "a=b"}

    def test_allows_an_empty_value(self):
        assert parse_assignments(["tone="]) == {"tone": ""}

    def test_refuses_a_pair_without_equals(self):
        with pytest.raises(PopcornError) as exc:
            parse_assignments(["tone"])
        assert "key=value" in str(exc.value)

    def test_refuses_an_empty_key(self):
        with pytest.raises(PopcornError):
            parse_assignments(["=crisp"])

    def test_refuses_the_reserved_integrations_key(self):
        """The backend rejects it on the round trip; name the rule here."""
        with pytest.raises(PopcornError) as exc:
            parse_assignments(["integrations=x"])
        assert "integrations set" in str(exc.value)


# ---------------------------------------------------------------------------
# fatal_findings
# ---------------------------------------------------------------------------


class TestFatalFindings:
    def test_unused_entries_are_not_fatal(self):
        """A shared config legitimately carries keys one flow does not read.

        Failing --strict on these would make it useless on every real channel.
        """
        comparison = {"unused_parameters": ["x"], "unused_integrations": ["y"]}
        assert fatal_findings(comparison) == {}

    @pytest.mark.parametrize("field", FATAL_COMPARISON_FIELDS)
    def test_each_run_breaking_field_is_fatal(self, field):
        assert fatal_findings({field: ["thing"]}) == {field: ["thing"]}

    def test_an_empty_comparison_is_clean(self):
        assert fatal_findings({}) == {}


class TestParametersOf:
    def test_tolerates_a_missing_section(self):
        assert parameters_of({"ok": True}) == {}

    def test_tolerates_a_non_dict(self):
        assert parameters_of({"channel_parameters": []}) == {}


# ---------------------------------------------------------------------------
# The commands
# ---------------------------------------------------------------------------


class _Recorder:
    """Stands in for `replace_channel_parameters` (the wholesale PUT)."""

    def __init__(self):
        self.calls: list[tuple] = []

    def __call__(self, client, conversation, parameters):
        self.calls.append((conversation, parameters))
        return {"ok": True, "channel_parameters": parameters}


class _PatchRecorder:
    """Stands in for `patch_channel_parameters`, applying the edit to `stored`."""

    def __init__(self, stored=None):
        self.calls: list[dict] = []
        self.stored = dict(stored or {})

    def __call__(self, client, conversation, set_=None, unset=None):
        self.calls.append({"conversation": conversation, "set": set_, "unset": unset})
        after = {**self.stored, **(set_ or {})}
        for key in unset or []:
            after.pop(key, None)
        return {"ok": True, "channel_parameters": after, "rev": "r1"}


def _run(handler, args, inspect_response=None, recorder=None, accounts=None, patcher=None):
    """Run a handler with every endpoint stubbed.

    `inspect_channel_config` and both writes are always patched, so an
    unexpected call is recorded rather than reaching `object()` as a client.
    """
    from contextlib import ExitStack

    from popcorn_cli.commands import channel_config as mod

    captured: dict = {}
    with ExitStack() as stack:
        stack.enter_context(patch("popcorn_cli.cli._get_client", return_value=object()))
        stack.enter_context(
            patch("popcorn_cli.cli._output", lambda a, d, r: captured.update(data=d, rendered=r))
        )
        captured["inspect"] = stack.enter_context(
            patch.object(operations, "inspect_channel_config", return_value=inspect_response)
        )
        stack.enter_context(
            patch.object(operations, "replace_channel_parameters", recorder or _Recorder())
        )
        stack.enter_context(
            patch.object(operations, "patch_channel_parameters", patcher or _PatchRecorder())
        )
        if accounts is not None:
            stack.enter_context(
                patch.object(operations, "list_own_integrations", return_value=accounts)
            )
        getattr(mod, handler)(args)
    return captured


class TestParamsSet:
    def test_sends_only_the_new_keys_as_a_patch(self):
        """The whole reason this command family is careful.

        A PUT of only {"tone": ...} deletes `retries`; a read-merge-PUT loses
        a concurrent edit's keys. The PATCH carries the edit alone and the
        server merges it.
        """
        put, patcher = _Recorder(), _PatchRecorder({"retries": 3, "tone": "flat"})
        out = _run("_params_set", _args(assignment=["tone=crisp"]), recorder=put, patcher=patcher)
        assert patcher.calls == [
            {"conversation": "#alerts", "set": {"tone": "crisp"}, "unset": None}
        ]
        assert put.calls == [], "a per-key set must never PUT the section"
        assert "2 parameters now set" in out["rendered"]

    def test_does_not_read_first(self):
        """The inspect endpoint parses every flow in the bundle — not for a write."""
        out = _run("_params_set", _args(assignment=["tone=crisp"]))
        out["inspect"].assert_not_called()

    def test_sets_several_keys_in_one_patch(self):
        patcher = _PatchRecorder({"c": 3})
        _run("_params_set", _args(assignment=["a=1", "b=2"]), patcher=patcher)
        assert len(patcher.calls) == 1
        assert patcher.calls[0]["set"] == {"a": 1, "b": 2}

    def test_replace_puts_the_whole_section(self):
        put, patcher = _Recorder(), _PatchRecorder()
        _run(
            "_params_set",
            _args(assignment=["tone=crisp"], replace=True),
            recorder=put,
            patcher=patcher,
        )
        assert put.calls == [("#alerts", {"tone": "crisp"})]
        assert patcher.calls == []

    def test_replace_does_not_read_first(self):
        """--replace is the raw endpoint semantics; a GET would be wasted."""
        out = _run("_params_set", _args(assignment=["tone=crisp"], replace=True))
        out["inspect"].assert_not_called()


class TestParamsUnset:
    def test_sends_only_the_keys_as_a_patch(self):
        put, patcher = _Recorder(), _PatchRecorder({"tone": "x", "a": 1})
        out = _run("_params_unset", _args(key=["tone"]), recorder=put, patcher=patcher)
        assert patcher.calls == [{"conversation": "#alerts", "set": None, "unset": ["tone"]}]
        assert put.calls == []
        out["inspect"].assert_not_called()
        assert "1 parameter remain" in out["rendered"]

    def test_an_absent_key_is_not_an_error(self):
        """Idempotent: the end state is what was asked for.

        The response carries the section as written, not what changed, so
        there is no "already absent" note to give — and no reason to fail.
        """
        patcher = _PatchRecorder({"a": 1})
        out = _run("_params_unset", _args(key=["gone"]), patcher=patcher)
        assert patcher.calls[0]["unset"] == ["gone"]
        assert out["data"]["channel_parameters"] == {"a": 1}


class TestShow:
    def test_reports_agreement(self):
        out = _run("_channel_config_show", _args(), _inspect({"tone": "x"}))
        assert "Config and flows agree." in out["rendered"]
        assert out["data"]["fatal"] == {}

    def test_names_a_missing_parameter(self):
        out = _run(
            "_channel_config_show",
            _args(),
            _inspect({}, {"missing_parameters": ["tone"]}),
        )
        assert "missing_parameters: tone" in out["rendered"]
        assert out["data"]["fatal"] == {"missing_parameters": ["tone"]}

    def test_strict_exits_unhealthy_on_a_fatal_finding(self):
        with pytest.raises(SystemExit) as exc:
            _run(
                "_channel_config_show",
                _args(strict=True),
                _inspect({}, {"missing_integrations": ["gmail"]}),
            )
        assert exc.value.code == EXIT_UNHEALTHY

    def test_strict_ignores_an_unused_parameter(self):
        """Exits 0 — a superset config is legal, not a lint failure."""
        out = _run(
            "_channel_config_show",
            _args(strict=True),
            _inspect({"spare": 1}, {"unused_parameters": ["spare"]}),
        )
        assert "unused_parameters: spare" in out["rendered"]

    def test_surfaces_a_malformed_config(self):
        out = _run(
            "_channel_config_show",
            _args(),
            _inspect({}, config_found=True, config_error="bad yaml at line 3"),
        )
        assert "malformed" in out["rendered"]

    def test_says_so_when_there_is_no_config(self):
        out = _run("_channel_config_show", _args(), _inspect({}, config_found=False))
        assert "no config yet" in out["rendered"]

    def test_renders_a_flow_that_failed_to_parse(self):
        out = _run(
            "_channel_config_show",
            _args(),
            _inspect({}, flows=[{"id": "1", "name": "broken", "error": "bad step"}]),
        )
        assert "broken" in out["rendered"] and "bad step" in out["rendered"]


class TestIntegrations:
    def test_set_passes_the_name_and_id(self):
        from popcorn_cli.commands import channel_config as mod

        calls = []

        def _set(client, conversation, name, integration_id):
            calls.append((conversation, name, integration_id))
            return {
                "ok": True,
                "integrations": {name: {"provider": "google", "provider_account": "me@x"}},
                "channel_parameters": {},
            }

        with (
            patch("popcorn_cli.cli._get_client", return_value=object()),
            patch("popcorn_cli.cli._output"),
            patch.object(operations, "set_channel_integration", _set),
        ):
            mod._integrations_set(_args(name="mail", integration_id="abc-123"))
        assert calls == [("#alerts", "mail", "abc-123")]

    def test_unset_says_the_grant_survives(self):
        from popcorn_cli.commands import channel_config as mod

        captured: dict = {}
        with (
            patch("popcorn_cli.cli._get_client", return_value=object()),
            patch(
                "popcorn_cli.cli._output",
                lambda a, d, r: captured.update(rendered=r),
            ),
            patch.object(
                operations,
                "unset_channel_integration",
                lambda c, cv, n: {"ok": True, "integrations": {}, "channel_parameters": {}},
            ),
        ):
            mod._integrations_unset(_args(name="mail"))
        assert "OAuth grant is untouched" in captured["rendered"]

    def test_accounts_points_at_the_set_command(self):
        out = _run(
            "_accounts",
            _args(),
            _inspect(),
            accounts={
                "ok": True,
                "integrations": [{"id": "abc", "provider": "google", "provider_account": "me@x"}],
            },
        )
        assert "abc" in out["rendered"]
        assert "integrations set" in out["rendered"]

    def test_accounts_handles_none_connected(self):
        out = _run("_accounts", _args(), _inspect(), accounts={"ok": True, "integrations": []})
        assert "no connected accounts" in out["rendered"]


class TestAccountsParser:
    """`accounts` lists YOUR accounts, so it needs no channel — but every
    sibling takes --channel, and a scripted `channel-config <sub> --channel X`
    must not blow up on this one subcommand (unrecognized arguments)."""

    def test_accepts_channel(self):
        from popcorn_cli.cli import build_parser

        args = build_parser().parse_args(["channel-config", "accounts", "--channel", "#ops"])
        assert args.channel == "#ops"

    def test_channel_stays_optional(self):
        from popcorn_cli.cli import build_parser

        args = build_parser().parse_args(["channel-config", "accounts"])
        assert args.channel is None
