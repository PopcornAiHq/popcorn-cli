"""The registry is the single source for parser, dispatch, schema, completions.

Parser tests prove argparse was built; they do *not* prove `main()` routes to
the handler. `TestDispatchIsWired` covers that separately — a family can parse
perfectly and still be unreachable.
"""

from __future__ import annotations

import argparse
import json
import sys

import pytest

from popcorn_cli import registry
from popcorn_cli.cli import build_parser


@pytest.fixture()
def parser():
    return build_parser()


class TestFlowFamilyParsesFromRegistry:
    def test_flow_list_requires_channel(self, parser):
        args = parser.parse_args(["flow", "list", "--channel", "#ops"])
        assert args.command == "flow"
        assert args.flow_command == "list"
        assert args.channel == "#ops"

    def test_flow_list_without_channel_is_rejected(self, parser):
        with pytest.raises(SystemExit):
            parser.parse_args(["flow", "list"])

    def test_flow_run_takes_inputs(self, parser):
        args = parser.parse_args(["flow", "run", "abc", "--channel", "#ops", "--inputs", "{}"])
        assert args.flow_id == "abc"
        assert args.inputs == "{}"

    def test_flow_runs_get_nested_subcommand(self, parser):
        args = parser.parse_args(
            ["flow", "runs", "get", "wid-1", "--channel", "#ops", "--include-errors"]
        )
        assert args.flow_runs_command == "get"
        assert args.workflow_id == "wid-1"
        assert args.include_errors is True

    def test_flow_runs_list_status_choices_are_enforced(self, parser):
        args = parser.parse_args(
            ["flow", "runs", "list", "--channel", "#ops", "--status", "failed"]
        )
        assert args.status == "failed"
        with pytest.raises(SystemExit):
            parser.parse_args(["flow", "runs", "list", "--channel", "#ops", "--status", "bogus"])


class TestDerivedSurfaces:
    def test_schema_lists_flow_subcommands(self):
        entry = next(c for c in registry.schema() if c["name"] == "flow")
        names = {s["name"] for s in entry["subcommands"]}
        assert {"list", "get", "run", "runs"} <= names

    def test_completion_words_match_subcommands(self):
        words = set(registry.completion_words("flow"))
        assert {"list", "get", "run", "runs"} <= words

    def test_completion_words_for_unknown_family_is_empty(self):
        assert registry.completion_words("not-a-family") == []

    def test_every_registered_subcommand_has_a_handler(self):
        def walk(subs):
            for s in subs:
                if s.subcommands:
                    walk(s.subcommands)
                else:
                    assert callable(s.handler), f"{s.name} has no handler"

        for cmd in registry.COMMANDS:
            walk(cmd.subcommands)

    def test_dispatch_returns_false_for_unregistered_command(self):
        assert registry.dispatch(argparse.Namespace(command="definitely-not-registered")) is False

    def test_registry_families_reach_the_commands_schema(self, capsys):
        from popcorn_cli.cli import cmd_commands

        cmd_commands(argparse.Namespace(command="commands", groups=None))
        schema = json.loads(capsys.readouterr().out)
        assert schema["schema_version"] == 1
        by_name = {c["name"]: c for c in schema["commands"]}
        for cmd in registry.COMMANDS:
            assert cmd.name in by_name, f"{cmd.name} missing from `popcorn commands --json`"
            emitted = by_name[cmd.name]
            assert emitted["category"] == cmd.category
            assert emitted["description"] == cmd.description
            assert {s["name"] for s in emitted["subcommands"]} == {s.name for s in cmd.subcommands}

    def test_registry_families_reach_both_completions(self, capsys):
        from popcorn_cli.cli import cmd_completion

        for shell in ("bash", "zsh"):
            cmd_completion(argparse.Namespace(shell=shell))
            out = capsys.readouterr().out
            for cmd in registry.COMMANDS:
                assert cmd.name in out, f"{cmd.name} missing from {shell} completion"
                for sub in cmd.subcommands:
                    assert sub.name in out, f"{cmd.name} {sub.name} missing from {shell}"

    def test_registry_families_appear_in_the_help_epilog(self, parser):
        # The epilog is the one surface still hand-maintained (it groups
        # registry and non-registry families together under prose headings).
        # This guard makes forgetting it a test failure, not a silent gap.
        epilog = parser.epilog or ""
        for cmd in registry.COMMANDS:
            assert f"\n  {cmd.name}" in epilog, f"{cmd.name} missing from the --help epilog"

    def test_registry_families_are_fuzzy_match_candidates(self):
        from popcorn_cli.cli import _ALL_COMMAND_NAMES

        for cmd in registry.COMMANDS:
            assert cmd.name in _ALL_COMMAND_NAMES


class TestDispatchIsWired:
    """`main()` must actually route a registry family to its handler."""

    def _run(self, monkeypatch, argv):
        from popcorn_cli import cli

        monkeypatch.setattr(cli, "_check_and_update", lambda: None)
        monkeypatch.setattr(cli, "_get_client", lambda args: object())
        monkeypatch.setattr(sys, "argv", ["popcorn", *argv])
        cli.main()

    def test_flow_list_reaches_its_handler(self, monkeypatch, capsys):
        from popcorn_core import operations

        seen = {}

        def fake_list_flows(client, conversation, limit=50, offset=0):
            seen["conversation"] = conversation
            seen["limit"] = limit
            return {"flows": [{"id": "flow-1", "name": "ingest", "version": 2}]}

        monkeypatch.setattr(operations, "list_flows", fake_list_flows)
        self._run(monkeypatch, ["flow", "list", "--channel", "#ops", "--limit", "7"])

        assert seen == {"conversation": "#ops", "limit": 7}
        assert "flow-1" in capsys.readouterr().out

    def test_flow_runs_get_reaches_its_nested_handler(self, monkeypatch, capsys):
        from popcorn_core import operations

        seen = {}

        def fake_get_flow_run(client, conversation, workflow_id, run_id=None, include_errors=False):
            seen["workflow_id"] = workflow_id
            seen["include_errors"] = include_errors
            return {"run": {"status": "COMPLETED", "workflow_id": workflow_id}}

        monkeypatch.setattr(operations, "get_flow_run", fake_get_flow_run)
        self._run(
            monkeypatch,
            ["flow", "runs", "get", "wid-9", "--channel", "#ops", "--include-errors"],
        )

        assert seen == {"workflow_id": "wid-9", "include_errors": True}
        assert "COMPLETED" in capsys.readouterr().out

    def test_family_with_no_subcommand_exits_with_a_usage_error(self, monkeypatch, capsys):
        from popcorn_core.errors import EXIT_VALIDATION

        with pytest.raises(SystemExit) as exc:
            self._run(monkeypatch, ["flow"])
        assert exc.value.code == EXIT_VALIDATION
        assert "popcorn flow [get|list|run|runs]" in capsys.readouterr().err

    def test_group_with_no_subcommand_exits_with_a_usage_error(self, monkeypatch, capsys):
        from popcorn_core.errors import EXIT_VALIDATION

        with pytest.raises(SystemExit) as exc:
            self._run(monkeypatch, ["flow", "runs"])
        assert exc.value.code == EXIT_VALIDATION
        assert "popcorn flow runs [get|list]" in capsys.readouterr().err
