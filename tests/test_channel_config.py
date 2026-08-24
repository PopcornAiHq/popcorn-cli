"""Tests for `popcorn channel-config`.

The load-bearing test is `test_set_keeps_the_other_parameters`. `PUT
/channel-config/parameters` replaces the whole section, so the naive
implementation — send only the new key — silently deletes every other
parameter and reports success. That is the failure this command family exists
to avoid, and it is invisible in any test that starts from an empty config.
"""

from __future__ import annotations

import argparse
from unittest.mock import patch

import pytest

from popcorn_core import operations
from popcorn_core.channel_config import (
    FATAL_COMPARISON_FIELDS,
    fatal_findings,
    merge_parameters,
    parameters_of,
    parse_assignments,
    remove_parameters,
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
# merge / remove
# ---------------------------------------------------------------------------


class TestMergeParameters:
    def test_keeps_keys_not_being_set(self):
        got = merge_parameters({"a": 1, "b": 2}, {"b": 3})
        assert got == {"a": 1, "b": 3}

    def test_does_not_mutate_the_input(self):
        current = {"a": 1}
        merge_parameters(current, {"b": 2})
        assert current == {"a": 1}

    def test_replaces_an_object_value_wholesale(self):
        """Shallow on purpose — there is no path syntax to shrink a subtree."""
        got = merge_parameters({"limits": {"max": 5, "min": 1}}, {"limits": {"max": 9}})
        assert got == {"limits": {"max": 9}}


class TestRemoveParameters:
    def test_removes_and_keeps_the_rest(self):
        remaining, missing = remove_parameters({"a": 1, "b": 2}, ["a"])
        assert remaining == {"b": 2}
        assert missing == []

    def test_reports_absent_keys_without_raising(self):
        remaining, missing = remove_parameters({"a": 1}, ["a", "gone"])
        assert remaining == {}
        assert missing == ["gone"]


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
    def __init__(self, response=None):
        self.calls: list[tuple] = []
        self.response = response or {"ok": True, "channel_parameters": {}}

    def __call__(self, client, conversation, parameters):
        self.calls.append((conversation, parameters))
        return {**self.response, "channel_parameters": parameters}


def _run(handler, args, inspect_response, recorder=None, accounts=None):
    from popcorn_cli.commands import channel_config as mod

    captured: dict = {}
    patches = [
        patch("popcorn_cli.cli._get_client", return_value=object()),
        patch(
            "popcorn_cli.cli._output",
            lambda a, d, r: captured.update(data=d, rendered=r),
        ),
        patch.object(operations, "inspect_channel_config", return_value=inspect_response),
    ]
    if recorder is not None:
        patches.append(patch.object(operations, "replace_channel_parameters", recorder))
    if accounts is not None:
        patches.append(patch.object(operations, "list_own_integrations", return_value=accounts))
    from contextlib import ExitStack

    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        getattr(mod, handler)(args)
    return captured


class TestParamsSet:
    def test_keeps_the_other_parameters(self):
        """The whole reason this is a read-modify-write.

        A naive `set` sends only {"tone": ...} and the endpoint's
        whole-section replace deletes `retries` — successfully, with no error.
        """
        rec = _Recorder()
        _run(
            "_params_set",
            _args(assignment=["tone=crisp"]),
            _inspect({"retries": 3, "tone": "flat"}),
            rec,
        )
        assert rec.calls[0][1] == {"retries": 3, "tone": "crisp"}

    def test_replace_drops_the_rest(self):
        rec = _Recorder()
        _run(
            "_params_set",
            _args(assignment=["tone=crisp"], replace=True),
            _inspect({"retries": 3}),
            rec,
        )
        assert rec.calls[0][1] == {"tone": "crisp"}

    def test_replace_does_not_read_first(self):
        """--replace is the raw endpoint semantics; a GET would be wasted."""
        from popcorn_cli.commands import channel_config as mod

        rec = _Recorder()
        with (
            patch("popcorn_cli.cli._get_client", return_value=object()),
            patch("popcorn_cli.cli._output"),
            patch.object(operations, "inspect_channel_config") as inspect,
            patch.object(operations, "replace_channel_parameters", rec),
        ):
            mod._params_set(_args(assignment=["tone=crisp"], replace=True))
        inspect.assert_not_called()

    def test_sets_several_keys_at_once(self):
        rec = _Recorder()
        _run(
            "_params_set",
            _args(assignment=["a=1", "b=2"]),
            _inspect({"c": 3}),
            rec,
        )
        assert rec.calls[0][1] == {"a": 1, "b": 2, "c": 3}


class TestParamsUnset:
    def test_removes_one_and_keeps_the_rest(self):
        rec = _Recorder()
        _run("_params_unset", _args(key=["tone"]), _inspect({"tone": "x", "a": 1}), rec)
        assert rec.calls[0][1] == {"a": 1}

    def test_refuses_when_nothing_would_change(self):
        """Distinct from the partial case: this would be a pointless write."""
        rec = _Recorder()
        with pytest.raises(PopcornError) as exc:
            _run("_params_unset", _args(key=["gone"]), _inspect({"a": 1}), rec)
        assert "nothing to unset" in str(exc.value)
        assert rec.calls == []

    def test_a_partially_absent_key_still_writes(self):
        rec = _Recorder()
        out = _run(
            "_params_unset",
            _args(key=["tone", "gone"]),
            _inspect({"tone": "x", "a": 1}),
            rec,
        )
        assert rec.calls[0][1] == {"a": 1}
        assert "already absent: gone" in out["rendered"]


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
