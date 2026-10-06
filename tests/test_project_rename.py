"""Channels became projects, templates became apps, and DMs are on their way out.

The old spellings are kept working for the callers that already use them —
skills, the eval harness, people's scripts — but each says what replaced it,
and none of them is advertised anywhere a new caller would learn it. The
per-row proof that every old spelling parses to the same values is
`test_parser_parity.test_the_old_spelling_parses_the_same`; this file covers
what that cannot see: dispatch, the notices, and where the names are listed.
"""

from __future__ import annotations

import argparse
import json
from unittest.mock import patch

import pytest

from popcorn_cli import cli, registry
from popcorn_cli.cli import _rewrite_legacy_flags, build_parser
from popcorn_core import operations


@pytest.fixture
def parser():
    return build_parser()


class TestLegacyFlags:
    def test_each_old_flag_becomes_the_new_one(self):
        assert _rewrite_legacy_flags(["table", "list", "--channel", "#ops"]) == [
            "table",
            "list",
            "--project",
            "#ops",
        ]
        assert _rewrite_legacy_flags(["--channel=#ops"]) == ["--project=#ops"]
        assert _rewrite_legacy_flags(["--template", "x"]) == ["--app", "x"]

    def test_a_value_that_merely_contains_the_text_is_left_alone(self):
        argv = ["message", "send", "#ops", "use --channel next time", "--channels"]
        assert _rewrite_legacy_flags(argv) == argv

    def test_nothing_after_a_double_dash_is_touched(self):
        argv = ["message", "send", "#ops", "--", "--channel"]
        assert _rewrite_legacy_flags(argv) == argv

    def test_each_old_flag_is_noted_once(self, capsys):
        _rewrite_legacy_flags(["--channel", "#a", "--channel", "#b", "--template", "t"])
        assert capsys.readouterr().err.splitlines() == [
            "Note: --channel is deprecated; use --project",
            "Note: --template is deprecated; use --app",
        ]


class TestLegacyFamilies:
    def test_an_old_family_runs_the_same_handler_and_says_what_replaced_it(self, parser, capsys):
        args = parser.parse_args(["channel", "info", "#ops"])
        with patch.object(cli, "cmd_info") as handler:
            registry.dispatch(args)
        handler.assert_called_once_with(args)
        assert args.project == "#ops"
        assert "popcorn channel info is now popcorn project info" in capsys.readouterr().err

    def test_templates_is_now_apps(self, parser, capsys):
        args = parser.parse_args(["channel", "templates"])
        with patch.object(cli, "cmd_project_apps") as handler:
            registry.dispatch(args)
        handler.assert_called_once_with(args)
        assert "popcorn channel templates is now popcorn project apps" in capsys.readouterr().err

    def test_a_nested_subcommand_of_an_old_family_dispatches(self, parser, capsys):
        args = parser.parse_args(["channel-config", "params", "unset", "k", "--channel", "#a"])
        # The mirror captured the handler when it was registered, so patch what
        # that handler calls instead: the operation behind it.
        with (
            patch.object(cli, "_get_client", return_value=object()),
            patch.object(
                operations, "patch_channel_parameters", return_value={"channel_parameters": {}}
            ) as op,
        ):
            registry.dispatch(args)
        assert op.call_args.args[1] == "#a"
        assert (
            "popcorn channel-config params unset is now popcorn project-config params unset"
            in capsys.readouterr().err
        )

    @pytest.mark.parametrize("old", ["channel", "channel-config"])
    def test_an_old_family_is_listed_nowhere(self, old, capsys):
        assert old in registry.hidden_names()
        assert old not in registry.descriptions()
        assert old not in {name for name, _ in registry.completion_groups()}
        cli.cmd_commands(argparse.Namespace(command="commands", groups=None))
        names = {c["name"] for c in json.loads(capsys.readouterr().out)["commands"]}
        assert old not in names
        assert old.replace("channel", "project") in names
        assert f"\n  {old} " not in build_parser().epilog


class TestDms:
    def _list(self, dms: bool):
        args = argparse.Namespace(
            query="", dms=dms, include_archived=False, include_hidden=False, json=False
        )
        with (
            patch.object(cli, "_get_client", return_value=object()),
            patch.object(cli, "_output"),
            patch.object(operations, "search_dms", return_value={"conversations": []}) as dm,
            patch.object(operations, "search_channels", return_value={"conversations": []}),
        ):
            cli.cmd_project_list(args)
        return dm

    def test_listing_dms_still_works_and_says_it_is_going(self, capsys):
        assert self._list(dms=True).called
        assert "--dms is deprecated" in capsys.readouterr().err

    def test_listing_projects_says_nothing_about_dms(self, capsys):
        assert not self._list(dms=False).called
        assert capsys.readouterr().err == ""
