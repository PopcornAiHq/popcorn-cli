"""The registry is the single source for parser, dispatch, schema, completions.

Parser tests prove argparse was built; they do *not* prove `main()` routes to
the handler. `TestDispatchIsWired` covers that separately — a family can parse
perfectly and still be unreachable.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import ClassVar

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
        expected = "|".join(registry.completion_words("flow"))
        assert f"popcorn flow [{expected}]" in capsys.readouterr().err

    def test_group_with_no_subcommand_exits_with_a_usage_error(self, monkeypatch, capsys):
        from popcorn_core.errors import EXIT_VALIDATION

        with pytest.raises(SystemExit) as exc:
            self._run(monkeypatch, ["flow", "runs"])
        assert exc.value.code == EXIT_VALIDATION
        assert "popcorn flow runs [get|list]" in capsys.readouterr().err


class TestFlowActivities:
    def test_activities_takes_filters(self, parser):
        args = parser.parse_args(
            ["flow", "activities", "--tier", "foundation", "--status", "release"]
        )
        assert args.flow_command == "activities"
        assert args.tier == "foundation"
        assert args.status == "release"

    def test_activities_does_not_require_channel(self, parser):
        args = parser.parse_args(["flow", "activities"])
        assert getattr(args, "channel", None) is None

    def test_activities_rejects_an_unknown_tier(self, parser):
        with pytest.raises(SystemExit):
            parser.parse_args(["flow", "activities", "--tier", "bogus"])

    def test_activities_reaches_its_handler_and_filters_client_side(self, monkeypatch, capsys):
        """The endpoint takes no filter params, so filtering is ours to get right."""
        from popcorn_core import operations

        catalog = {
            "activities": [
                {
                    "name": "foundation.store.upsert_rows",
                    "tier": "foundation",
                    "status": "release",
                    "category": "store",
                    "description": "Upsert rows",
                },
                {
                    "name": "foundation.channel.post",
                    "tier": "foundation",
                    "status": "beta",
                    "category": "channel",
                    "description": "Post a message",
                },
                {
                    "name": "app.alerts.tick",
                    "tier": "app",
                    "status": "release",
                    "category": "alerts",
                    "description": "Tick",
                },
            ]
        }
        monkeypatch.setattr(operations, "list_activity_catalog", lambda *a, **kw: catalog)
        TestDispatchIsWired()._run(
            monkeypatch, ["flow", "activities", "--tier", "foundation", "--status", "release"]
        )
        out = capsys.readouterr().out
        assert "foundation.store.upsert_rows" in out
        assert "foundation.channel.post" not in out  # wrong status
        assert "app.alerts.tick" not in out  # wrong tier
        assert "Activities (1)" in out


class TestTableFamily:
    def test_rows_takes_filter_and_limit(self, parser):
        args = parser.parse_args(
            [
                "table",
                "rows",
                "alerts",
                "--channel",
                "#ops",
                "--filter",
                '{"Status":"firing"}',
                "--limit",
                "5",
            ]
        )
        assert args.table_command == "rows"
        assert args.name == "alerts"
        assert args.filter == '{"Status":"firing"}'
        assert args.limit == 5

    def test_row_patch_is_nested(self, parser):
        args = parser.parse_args(
            [
                "table",
                "row",
                "patch",
                "alerts",
                "7",
                "--channel",
                "#ops",
                "--data",
                '{"Status":"acked"}',
            ]
        )
        assert args.table_row_command == "patch"
        assert args.record_id == "7"

    def test_row_patch_requires_data(self, parser):
        with pytest.raises(SystemExit):
            parser.parse_args(["table", "row", "patch", "alerts", "7", "--channel", "#ops"])

    def test_scalar_set_takes_value(self, parser):
        args = parser.parse_args(
            ["table", "scalar", "set", "alerts_summary", "3 firing", "--channel", "#ops"]
        )
        assert args.table_scalar_command == "set"
        assert args.key == "alerts_summary"
        assert args.value == "3 firing"

    def test_table_appears_in_schema(self):
        assert any(c["name"] == "table" for c in registry.schema())


class TestTableHandlersRenderTheRealShapes:
    """The API's keys, not plausible ones: `records`/`events`/`scalar.value`,
    and columns nested under `table.schema_version.schema_def`."""

    def test_rows_renders_records_and_paginates(self, monkeypatch, capsys):
        from popcorn_core import operations

        seen = {}

        def fake(client, conversation, name, filter=None, limit=50, cursor=None):
            seen.update(name=name, filter=filter, limit=limit)
            return {
                "records": [{"id": 4, "data": {"Status": "firing", "Alarm": "cpu"}}],
                "cursor": "cur-2",
                "has_more": True,
            }

        monkeypatch.setattr(operations, "list_records", fake)
        TestDispatchIsWired()._run(
            monkeypatch,
            ["table", "rows", "alerts", "--channel", "#ops", "--filter", '{"Status":"firing"}'],
        )
        out = capsys.readouterr().out
        assert seen == {"name": "alerts", "filter": {"Status": "firing"}, "limit": 50}
        assert "firing" in out and "4" in out

    def test_rows_emits_a_cursor_the_agent_can_feed_back(self, monkeypatch, capsys):
        from popcorn_core import operations

        monkeypatch.setattr(
            operations,
            "list_records",
            lambda *a, **kw: {"records": [], "cursor": "cur-2", "has_more": True},
        )
        TestDispatchIsWired()._run(
            monkeypatch, ["table", "rows", "alerts", "--channel", "#ops", "--json"]
        )
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True
        assert payload["data"]["pagination"]["next"] == {"cursor": "cur-2"}

    def test_rows_reports_no_next_page_when_exhausted(self, monkeypatch, capsys):
        from popcorn_core import operations

        monkeypatch.setattr(
            operations,
            "list_records",
            lambda *a, **kw: {"records": [], "cursor": None, "has_more": False},
        )
        TestDispatchIsWired()._run(
            monkeypatch, ["table", "rows", "alerts", "--channel", "#ops", "--json"]
        )
        payload = json.loads(capsys.readouterr().out)
        assert payload["data"]["pagination"]["next"] is None

    def test_schema_reads_columns_from_the_schema_version(self, monkeypatch, capsys):
        from popcorn_core import operations

        monkeypatch.setattr(
            operations,
            "get_table",
            lambda *a, **kw: {
                "table": {
                    "name": "alerts",
                    "schema_version": {
                        "version": 3,
                        "schema_def": {
                            "columns": [
                                {"name": "Alarm", "type": "string", "required": True},
                                {"name": "Seen At", "type": "datetime", "internal": True},
                            ]
                        },
                    },
                }
            },
        )
        TestDispatchIsWired()._run(monkeypatch, ["table", "schema", "alerts", "--channel", "#ops"])
        out = capsys.readouterr().out
        assert "2 columns" in out
        assert "Alarm" in out and "required" in out
        assert "Seen At" in out and "internal" in out

    def test_scalar_get_prints_the_nested_value(self, monkeypatch, capsys):
        from popcorn_core import operations

        monkeypatch.setattr(
            operations,
            "get_scalar",
            lambda *a, **kw: {"scalar": {"key": "alerts_summary", "value": "3 firing"}},
        )
        TestDispatchIsWired()._run(
            monkeypatch, ["table", "scalar", "get", "alerts_summary", "--channel", "#ops"]
        )
        assert capsys.readouterr().out.strip() == "3 firing"

    def test_audit_renders_events(self, monkeypatch, capsys):
        from popcorn_core import operations

        monkeypatch.setattr(
            operations,
            "list_store_audit",
            lambda *a, **kw: {
                "events": [
                    {
                        "changed_at": "2026-08-06T12:00:00Z",
                        "operation": "update",
                        "entity_type": "record",
                        "entity_id": "4",
                    }
                ],
                "has_more": False,
            },
        )
        TestDispatchIsWired()._run(monkeypatch, ["table", "audit", "--channel", "#ops"])
        out = capsys.readouterr().out
        assert "Audit (1)" in out
        assert "update" in out and "record" in out

    def test_row_delete_confirms_before_deleting(self, monkeypatch, capsys):
        """Destructive, so it must go through _confirm — which fails loudly
        in a non-TTY without --yes rather than silently deleting."""
        from popcorn_core import operations
        from popcorn_core.errors import EXIT_VALIDATION

        called = []
        monkeypatch.setattr(operations, "delete_record", lambda *a, **kw: called.append(a) or {})
        with pytest.raises(SystemExit) as exc:
            TestDispatchIsWired()._run(
                monkeypatch, ["table", "row", "delete", "alerts", "7", "--channel", "#ops"]
            )
        assert exc.value.code == EXIT_VALIDATION
        assert called == [], "deleted without confirmation"

        TestDispatchIsWired()._run(
            monkeypatch,
            ["table", "row", "delete", "alerts", "7", "--channel", "#ops", "--yes"],
        )
        assert len(called) == 1


class TestPaginationIsDeclared:
    """Every command that emits data.pagination.next must say so in the schema,
    or an agent has no way to learn it can page."""

    _PAGINATED: ClassVar[list[str]] = ["table rows", "table scalar list", "table audit"]

    def test_table_paginated_commands_are_declared(self, capsys):
        from popcorn_cli.cli import cmd_commands

        cmd_commands(argparse.Namespace(command="commands", groups=None))
        schema = json.loads(capsys.readouterr().out)
        declared = schema["envelope"]["pagination"]["commands"]
        for cmd in self._PAGINATED:
            assert cmd in declared, f"{cmd} paginates but is not declared"

    @pytest.mark.parametrize(
        ("argv", "operation", "payload_key"),
        [
            (["table", "rows", "alerts"], "list_records", "records"),
            (["table", "scalar", "list"], "list_scalars", "scalars"),
            (["table", "audit"], "list_store_audit", "events"),
        ],
    )
    def test_declared_commands_really_emit_a_cursor(
        self, monkeypatch, capsys, argv, operation, payload_key
    ):
        from popcorn_core import operations

        monkeypatch.setattr(
            operations,
            operation,
            lambda *a, **kw: {payload_key: [], "cursor": "cur-9", "has_more": True},
        )
        TestDispatchIsWired()._run(monkeypatch, [*argv, "--channel", "#ops", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["data"]["pagination"]["next"] == {"cursor": "cur-9"}
