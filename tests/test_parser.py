"""Tests for CLI parser — ensures all commands parse correctly."""

from __future__ import annotations

import argparse
import json
from unittest.mock import patch

import pytest

import popcorn_cli
from popcorn_cli import registry
from popcorn_cli.cli import _hoist_global_flags, build_parser
from popcorn_cli.registry import dispatch
from popcorn_core.errors import EXIT_SERVER, APIError, PopcornError


@pytest.fixture()
def parser():
    return build_parser()


class TestGlobalFlags:
    def test_json_flag(self, parser):
        args = parser.parse_args(["--json", "whoami"])
        assert args.json is True

    def test_env_flag(self, parser):
        args = parser.parse_args(["-e", "dev", "whoami"])
        assert args.env == "dev"

    def test_workspace_flag(self, parser):
        args = parser.parse_args(["--workspace", "ws-123", "whoami"])
        assert args.workspace == "ws-123"

    def test_quiet_flag(self, parser):
        args = parser.parse_args(["--quiet", "whoami"])
        assert args.quiet is True

    def test_quiet_short_flag(self, parser):
        args = parser.parse_args(["-q", "whoami"])
        assert args.quiet is True

    def test_timeout_flag(self, parser):
        args = parser.parse_args(["--timeout", "60", "whoami"])
        assert args.timeout == 60.0

    def test_timeout_default_none(self, parser):
        args = parser.parse_args(["whoami"])
        assert args.timeout is None

    def test_no_command_defaults_to_none(self, parser):
        args = parser.parse_args([])
        assert args.command is None


class TestFlagHoisting:
    def test_json_after_subcommand(self):
        from popcorn_cli.cli import _hoist_global_flags

        assert _hoist_global_flags(["message", "list", "--json", "#general"]) == [
            "--json",
            "message",
            "list",
            "#general",
        ]

    def test_quiet_after_subcommand(self):
        from popcorn_cli.cli import _hoist_global_flags

        assert _hoist_global_flags(["message", "list", "-q", "#general"]) == [
            "-q",
            "message",
            "list",
            "#general",
        ]

    def test_timeout_after_subcommand(self):
        from popcorn_cli.cli import _hoist_global_flags

        assert _hoist_global_flags(["message", "list", "--timeout", "60", "#general"]) == [
            "--timeout",
            "60",
            "message",
            "list",
            "#general",
        ]

    def test_multiple_flags_hoisted(self):
        from popcorn_cli.cli import _hoist_global_flags

        result = _hoist_global_flags(
            ["message", "list", "--json", "-q", "--timeout", "10", "#general"]
        )
        assert result == ["--json", "-q", "--timeout", "10", "message", "list", "#general"]


class TestAuthLoginEnv:
    """`auth login --env <name>` must reach cmd_auth_login intact.

    Regression: the login subparser used to redefine -e/--env with the same
    dest as the global flag. _hoist_global_flags moves --env ahead of the
    subcommand, then the subparser re-applied its None default and clobbered
    the hoisted value, so `auth login --env prod` silently reused the current
    default profile.
    """

    def test_env_survives_hoist_and_parse(self, parser):
        from popcorn_cli.cli import _hoist_global_flags

        args = parser.parse_args(_hoist_global_flags(["auth", "login", "--env", "prod"]))
        assert args.command == "auth"
        assert args.auth_command == "login"
        assert args.env == "prod"

    def test_env_short_flag_survives(self, parser):
        from popcorn_cli.cli import _hoist_global_flags

        args = parser.parse_args(_hoist_global_flags(["auth", "login", "-e", "prod"]))
        assert args.env == "prod"

    def test_env_survives_with_other_login_flags(self, parser):
        from popcorn_cli.cli import _hoist_global_flags

        args = parser.parse_args(_hoist_global_flags(["auth", "login", "--env", "prod", "--force"]))
        assert args.env == "prod"
        assert args.force is True


class TestAgentMode:
    """POPCORN_AGENT=1 should inject --json, -q, --no-color as defaults."""

    def test_agent_mode_injects_defaults(self, monkeypatch):
        from popcorn_cli.cli import _hoist_global_flags

        monkeypatch.setenv("POPCORN_AGENT", "1")
        result = _hoist_global_flags(["whoami"])
        assert "--json" in result
        assert "--quiet" in result
        assert "--no-color" in result

    def test_agent_mode_does_not_duplicate_json(self, monkeypatch):
        from popcorn_cli.cli import _hoist_global_flags

        monkeypatch.setenv("POPCORN_AGENT", "1")
        result = _hoist_global_flags(["whoami", "--json"])
        assert result.count("--json") == 1

    def test_agent_mode_does_not_duplicate_quiet(self, monkeypatch):
        from popcorn_cli.cli import _hoist_global_flags

        monkeypatch.setenv("POPCORN_AGENT", "1")
        result = _hoist_global_flags(["whoami", "-q"])
        # Either -q or --quiet, but not both
        count = result.count("-q") + result.count("--quiet")
        assert count == 1

    def test_agent_mode_off_by_default(self, monkeypatch):
        from popcorn_cli.cli import _hoist_global_flags

        monkeypatch.delenv("POPCORN_AGENT", raising=False)
        result = _hoist_global_flags(["whoami"])
        assert "--json" not in result
        assert "--quiet" not in result

    def test_agent_mode_accepts_true(self, monkeypatch):
        from popcorn_cli.cli import _agent_mode_enabled

        monkeypatch.setenv("POPCORN_AGENT", "true")
        assert _agent_mode_enabled() is True

    def test_agent_mode_rejects_zero(self, monkeypatch):
        from popcorn_cli.cli import _agent_mode_enabled

        monkeypatch.setenv("POPCORN_AGENT", "0")
        assert _agent_mode_enabled() is False


class TestAssumeYes:
    """--yes / POPCORN_ASSUME_YES opts into non-interactive confirmation."""

    def test_yes_flag_via_parser(self, parser):
        args = parser.parse_args(["--yes", "whoami"])
        assert args.yes is True

    def test_short_y_flag(self, parser):
        args = parser.parse_args(["-y", "whoami"])
        assert args.yes is True

    def test_assume_yes_flag(self, monkeypatch):
        import argparse

        from popcorn_cli.cli import _assume_yes

        monkeypatch.delenv("POPCORN_ASSUME_YES", raising=False)
        assert _assume_yes(argparse.Namespace(yes=True)) is True
        assert _assume_yes(argparse.Namespace(yes=False)) is False

    def test_assume_yes_env(self, monkeypatch):
        import argparse

        from popcorn_cli.cli import _assume_yes

        monkeypatch.setenv("POPCORN_ASSUME_YES", "1")
        assert _assume_yes(argparse.Namespace(yes=False)) is True

    def test_yes_flag_hoisted(self):
        from popcorn_cli.cli import _hoist_global_flags

        result = _hoist_global_flags(["channel", "delete", "#old", "--yes"])
        assert result[0] == "--yes"


class TestConfirm:
    """_confirm fails loudly on non-TTY unless --yes is set."""

    def test_returns_true_when_assume_yes(self, monkeypatch):
        import argparse

        from popcorn_cli.cli import _confirm

        monkeypatch.delenv("POPCORN_ASSUME_YES", raising=False)
        args = argparse.Namespace(yes=True)
        assert _confirm(args, "Delete everything?") is True

    def test_raises_on_non_tty_without_yes(self, monkeypatch):
        import argparse

        import pytest

        from popcorn_cli.cli import _confirm
        from popcorn_core.errors import PopcornError

        monkeypatch.delenv("POPCORN_ASSUME_YES", raising=False)
        # Force stdin to look non-interactive
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        args = argparse.Namespace(yes=False)
        with pytest.raises(PopcornError, match="Refusing to prompt"):
            _confirm(args, "Delete everything?")

    def test_env_var_bypasses_tty_check(self, monkeypatch):
        import argparse

        from popcorn_cli.cli import _confirm

        monkeypatch.setenv("POPCORN_ASSUME_YES", "1")
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        args = argparse.Namespace(yes=False)
        assert _confirm(args, "Delete?") is True


class TestResolveDataArg:
    """_resolve_data_arg supports curl/gh-style @file and @- (stdin)."""

    def test_literal_string(self):
        from popcorn_cli.cli import _resolve_data_arg

        assert _resolve_data_arg('{"a": 1}') == '{"a": 1}'

    def test_stdin(self, monkeypatch):
        import io

        from popcorn_cli.cli import _resolve_data_arg

        monkeypatch.setattr("sys.stdin", io.StringIO('{"from": "stdin"}'))
        assert _resolve_data_arg("@-") == '{"from": "stdin"}'

    def test_file(self, tmp_path):
        from popcorn_cli.cli import _resolve_data_arg

        f = tmp_path / "body.json"
        f.write_text('{"from": "file"}')
        assert _resolve_data_arg(f"@{f}") == '{"from": "file"}'

    def test_file_not_found(self, tmp_path):
        import pytest

        from popcorn_cli.cli import _resolve_data_arg
        from popcorn_core.errors import PopcornError

        missing = tmp_path / "nope.json"
        with pytest.raises(PopcornError, match="Cannot read --data file"):
            _resolve_data_arg(f"@{missing}")

    def test_escaped_at_sign(self):
        from popcorn_cli.cli import _resolve_data_arg

        # '\@literal' → literal '@literal' — escape hatch for payloads that
        # genuinely start with '@'.
        assert _resolve_data_arg("\\@literal") == "@literal"


class TestFormatPayloadPreview:
    """_format_payload_preview renders webhook payload_raw values compactly."""

    def test_dict_compact_json(self):
        from popcorn_cli.cli import _format_payload_preview

        assert _format_payload_preview({"a": 1, "b": 2}) == '{"a":1,"b":2}'

    def test_list_compact_json(self):
        from popcorn_cli.cli import _format_payload_preview

        assert _format_payload_preview([1, 2, 3]) == "[1,2,3]"

    def test_string_passthrough(self):
        from popcorn_cli.cli import _format_payload_preview

        assert _format_payload_preview("raw bytes here") == "raw bytes here"

    def test_truncation(self):
        from popcorn_cli.cli import _format_payload_preview

        long = "x" * 500
        out = _format_payload_preview(long, max_len=50)
        assert len(out) == 50
        assert out.endswith("…")


class TestJsonEnvelopeStripping:
    """_json_ok should strip top-level `ok` keys leaked from API responses."""

    def test_strips_ok_from_dict(self):
        import json

        from popcorn_cli.cli import _json_ok

        out = json.loads(_json_ok({"ok": True, "user": {"id": "u1"}}))
        assert out == {"ok": True, "data": {"user": {"id": "u1"}}}

    def test_preserves_non_dict_data(self):
        import json

        from popcorn_cli.cli import _json_ok

        out = json.loads(_json_ok([1, 2, 3]))
        assert out == {"ok": True, "data": [1, 2, 3]}

    def test_preserves_dict_without_ok(self):
        import json

        from popcorn_cli.cli import _json_ok

        out = json.loads(_json_ok({"user": "shaun"}))
        assert out == {"ok": True, "data": {"user": "shaun"}}


class TestDoctorCommand:
    """`popcorn doctor` parses and maps to the handler."""

    def test_doctor_parses(self, parser):
        args = parser.parse_args(["doctor"])
        assert args.command == "doctor"

    def test_doctor_json_flag(self, parser):
        args = parser.parse_args(["--json", "doctor"])
        assert args.command == "doctor"
        assert args.json is True

    def test_doctor_registered_in_commands_map(self):
        from popcorn_cli.cli import _COMMANDS

        assert "doctor" in _COMMANDS


class TestAttachPagination:
    """_attach_pagination adds data.pagination.next in a uniform shape."""

    def test_sets_next_when_more(self):
        from popcorn_cli.cli import _attach_pagination

        data = {"messages": []}
        out = _attach_pagination(data, {"before": "m1"})
        assert out["pagination"] == {"next": {"before": "m1"}}

    def test_sets_next_null_when_no_more(self):
        from popcorn_cli.cli import _attach_pagination

        data = {"messages": []}
        out = _attach_pagination(data, None)
        assert out["pagination"] == {"next": None}

    def test_mutates_in_place(self):
        from popcorn_cli.cli import _attach_pagination

        data = {"messages": []}
        _attach_pagination(data, None)
        assert "pagination" in data


class TestJsonLine:
    """_json_line emits NDJSON — single line, no indentation, still strips ok."""

    def test_single_line(self):
        from popcorn_cli.cli import _json_line

        out = _json_line({"id": "m1"})
        assert "\n" not in out
        assert out.startswith('{"ok":') or out.startswith('{"ok"')

    def test_strips_leaked_ok(self):
        import json

        from popcorn_cli.cli import _json_line

        parsed = json.loads(_json_line({"ok": True, "id": "m1"}))
        assert parsed["ok"] is True
        assert "ok" not in parsed["data"]
        assert parsed["data"]["id"] == "m1"

    def test_preserves_non_dict(self):
        import json

        from popcorn_cli.cli import _json_line

        parsed = json.loads(_json_line([1, 2, 3]))
        assert parsed == {"ok": True, "data": [1, 2, 3]}


class TestAuthCommands:
    def test_auth_login(self, parser):
        args = parser.parse_args(["auth", "login"])
        assert args.command == "auth"
        assert args.auth_command == "login"

    def test_auth_login_with_token(self, parser):
        args = parser.parse_args(["auth", "login", "--with-token"])
        assert args.with_token is True

    def test_auth_login_workspace(self, parser):
        """Parsed the way `main()` parses, which is the only way it is reached.

        This asserted the same thing against raw argv, and passed while the
        behaviour was broken for every real caller: `auth login` declared its
        own `--workspace`, so raw `parse_args` let the subparser consume the
        value, while a real invocation went through the hoist and had the
        subparser's `None` default copied back over it. The flag is gone and
        the global spelling does the work; hoisting here is what makes the test
        see what a user sees.
        """
        args = parser.parse_args(_hoist_global_flags(["auth", "login", "--workspace", "acme"]))
        assert args.workspace == "acme"

    def test_auth_status(self, parser):
        args = parser.parse_args(["auth", "status"])
        assert args.auth_command == "status"


class TestReadingCommands:
    def test_search(self, parser):
        args = parser.parse_args(["message", "search", "test query"])
        assert args.command == "message"
        assert args.message_command == "search"
        assert args.query == "test query"

    def test_list_messages(self, parser):
        args = parser.parse_args(["message", "list", "#general", "--limit", "10"])
        assert args.command == "message"
        assert args.message_command == "list"
        assert args.conversation == "#general"
        assert args.limit == 10

    def test_list_messages_thread(self, parser):
        args = parser.parse_args(["message", "list", "#general", "--thread", "t-123"])
        assert args.thread == "t-123"

    def test_inbox_unread(self, parser):
        args = parser.parse_args(["workspace", "inbox", "--unread"])
        assert args.unread is True

    def test_inbox_read_unread_exclusive(self, parser):
        with pytest.raises(SystemExit):
            parser.parse_args(["workspace", "inbox", "--unread", "--read"])

    def test_list_threads(self, parser):
        args = parser.parse_args(["message", "threads", "#general"])
        assert args.command == "message"
        assert args.message_command == "threads"
        assert args.conversation == "#general"

    def test_list_threads_with_limit(self, parser):
        args = parser.parse_args(["message", "threads", "#general", "--limit", "10"])
        assert args.limit == 10

    def test_list_threads_with_offset(self, parser):
        args = parser.parse_args(["message", "threads", "#general", "--offset", "50"])
        assert args.offset == 50

    def test_list_messages_before(self, parser):
        args = parser.parse_args(["message", "list", "#general", "--before", "msg123"])
        assert args.before == "msg123"

    def test_list_messages_after(self, parser):
        args = parser.parse_args(["message", "list", "#general", "--after", "msg456"])
        assert args.after == "msg456"

    def test_list_watch(self, parser):
        args = parser.parse_args(["message", "list", "#general", "--watch"])
        assert args.watch is True

    def test_list_watch_count(self, parser):
        args = parser.parse_args(["message", "list", "#general", "--watch", "--count", "5"])
        assert args.count == 5

    def test_list_watch_max_wait(self, parser):
        args = parser.parse_args(["message", "list", "#general", "--watch", "--max-wait", "30"])
        assert args.max_wait == 30.0

    def test_channel_list(self, parser):
        args = parser.parse_args(["channel", "list"])
        assert args.command == "channel"
        assert args.channel_command == "list"

    def test_channel_list_dms(self, parser):
        args = parser.parse_args(["channel", "list", "--dms"])
        assert args.dms is True

    def test_users_list(self, parser):
        args = parser.parse_args(["workspace", "users"])
        assert args.command == "workspace"
        assert args.workspace_command == "users"


class TestWritingCommands:
    def test_send_message(self, parser):
        args = parser.parse_args(["message", "send", "#general", "hello world"])
        assert args.command == "message"
        assert args.message_command == "send"
        assert args.conversation == "#general"
        assert args.message == "hello world"

    def test_send_message_batch(self, parser):
        args = parser.parse_args(["message", "send", "--batch"])
        assert args.batch is True
        assert args.conversation is None

    def test_react(self, parser):
        args = parser.parse_args(["message", "react", "#general", "msg-1", "thumbsup"])
        assert args.emoji == "thumbsup"

    def test_react_remove(self, parser):
        args = parser.parse_args(["message", "react", "#general", "msg-1", "thumbsup", "--remove"])
        assert args.remove is True

    def test_edit_message(self, parser):
        args = parser.parse_args(["message", "edit", "#general", "msg-1", "new content"])
        assert args.content == "new content"

    def test_delete_message(self, parser):
        args = parser.parse_args(["message", "delete", "#general", "msg-1"])
        assert args.message_id == "msg-1"


class TestChannelManagement:
    def test_create_channel(self, parser):
        args = parser.parse_args(["channel", "create", "new-channel"])
        assert args.command == "channel"
        assert args.channel_command == "create"
        assert args.name == "new-channel"

    def test_create_channel_private(self, parser):
        args = parser.parse_args(["channel", "create", "secret", "--type", "private_channel"])
        assert args.type == "private_channel"

    def test_join_channel(self, parser):
        args = parser.parse_args(["channel", "join", "#general"])
        assert args.conversation == "#general"

    def test_archive_channel_undo(self, parser):
        args = parser.parse_args(["channel", "archive", "#general", "--undo"])
        assert args.undo is True

    def test_invite(self, parser):
        args = parser.parse_args(["channel", "invite", "#general", "u1,u2"])
        assert args.user_ids == "u1,u2"


class TestApiEscapeHatch:
    def test_api_get(self, parser):
        args = parser.parse_args(["api", "/api/users/me"])
        assert args.path == "/api/users/me"
        assert args.method is None

    def test_api_post_with_data(self, parser):
        args = parser.parse_args(["api", "/api/messages/post", "-d", '{"key": "val"}'])
        assert args.data == '{"key": "val"}'

    def test_api_explicit_method(self, parser):
        args = parser.parse_args(["api", "-X", "DELETE", "/api/webhooks/delete"])
        assert args.method == "DELETE"

    def test_api_raw_flag(self, parser):
        args = parser.parse_args(["api", "/api/users/me", "--raw"])
        assert args.raw is True


class TestWebhook:
    def test_webhook_create(self, parser):
        args = parser.parse_args(["webhook", "create", "#general", "my-hook"])
        assert args.command == "webhook"
        assert args.webhook_command == "create"
        assert args.name == "my-hook"

    def test_webhook_create_with_options(self, parser):
        args = parser.parse_args(
            [
                "webhook",
                "create",
                "#general",
                "my-hook",
                "--description",
                "A test hook",
                "--action-mode",
                "silent",
            ]
        )
        assert args.name == "my-hook"
        assert args.description == "A test hook"
        assert args.action_mode == "silent"

    def test_webhook_create_trigger_flow_name(self, parser):
        args = parser.parse_args(
            [
                "webhook",
                "create",
                "#general",
                "my-hook",
                "--action-mode",
                "trigger_workflow",
                "--trigger-flow-name",
                "alert_webhook",
            ]
        )
        assert args.trigger_flow_name == "alert_webhook"
        assert args.trigger_flow_id is None

    def test_webhook_create_flow_id_and_name_are_exclusive(self, parser, capsys):
        """The API rejects both; fail in the parser rather than at the server.

        Asserts on the mutual-exclusion message specifically. A bare
        `raises(SystemExit)` would also pass when the flag does not exist at
        all — argparse exits on an unrecognized argument too — so it could
        not tell the fix from its absence.
        """
        with pytest.raises(SystemExit):
            parser.parse_args(
                [
                    "webhook",
                    "create",
                    "#general",
                    "my-hook",
                    "--trigger-flow-id",
                    "00000000-0000-4000-8000-000000000011",
                    "--trigger-flow-name",
                    "alert_webhook",
                ]
            )
        assert "not allowed with argument" in capsys.readouterr().err

    def test_trigger_workflow_without_a_flow_is_refused_locally(self, parser):
        """Fail with an actionable hint instead of a server 422."""
        args = parser.parse_args(
            [
                "webhook",
                "create",
                "#general",
                "my-hook",
                "--action-mode",
                "trigger_workflow",
            ]
        )
        with patch("popcorn_cli.cli._get_client"), pytest.raises(PopcornError) as exc:
            dispatch(args)
        assert "trigger_workflow" in str(exc.value)

    def test_webhook_list(self, parser):
        args = parser.parse_args(["webhook", "list", "#general"])
        assert args.webhook_command == "list"

    def test_webhook_deliveries(self, parser):
        args = parser.parse_args(["webhook", "deliveries", "#general", "--limit", "10"])
        assert args.webhook_command == "deliveries"
        assert args.conversation == "#general"
        assert args.limit == 10

    def test_webhook_deliveries_include(self, parser):
        args = parser.parse_args(["webhook", "deliveries", "#general", "--include", "payload_raw"])
        assert args.include == "payload_raw"

    def test_webhook_deliveries_include_default_none(self, parser):
        args = parser.parse_args(["webhook", "deliveries", "#general"])
        assert args.include is None

    def test_webhook_create_trigger_workflow(self, parser):
        args = parser.parse_args(
            [
                "webhook",
                "create",
                "#general",
                "flow-hook",
                "--action-mode",
                "trigger_workflow",
                "--trigger-flow-id",
                "flow-abc",
            ]
        )
        assert args.action_mode == "trigger_workflow"
        assert args.trigger_flow_id == "flow-abc"

    def test_webhook_event_types(self, parser):
        args = parser.parse_args(["webhook", "event-types"])
        assert args.webhook_command == "event-types"


_SENT = {"url": "https://hooks.popcorn.ai/ingest/tok", "status": 200, "response": {"ok": 1}}


class TestWebhookSend:
    """`webhook send` resolves a target, then POSTs to the ingest host."""

    def test_parses_target_payload_and_channel(self, parser):
        args = parser.parse_args(["webhook", "send", "Intake", '{"a": 1}', "--channel", "#ops"])
        assert args.webhook_command == "send"
        assert args.target == "Intake"
        assert args.payload == '{"a": 1}'
        assert args.channel == "#ops"

    def test_payload_and_channel_are_optional(self, parser):
        args = parser.parse_args(["webhook", "send", "https://hooks.popcorn.ai/ingest/tok"])
        assert args.payload is None
        assert args.channel is None

    def test_url_target_sends_without_a_client(self, parser):
        """An ingest URL needs no lookup and no credentials."""
        args = parser.parse_args(["webhook", "send", "https://hooks.popcorn.ai/ingest/tok"])
        with (
            patch("popcorn_cli.cli._get_client") as get_client,
            patch("popcorn_core.operations.send_webhook", return_value=_SENT) as send,
        ):
            dispatch(args)
        get_client.assert_not_called()
        assert send.call_args[0] == ("https://hooks.popcorn.ai/ingest/tok", {})

    def test_payload_defaults_to_empty_object(self, parser):
        args = parser.parse_args(["webhook", "send", "Intake", "--channel", "#ops"])
        with (
            patch("popcorn_cli.cli._get_client"),
            patch("popcorn_core.operations.resolve_webhook_url", return_value="u/1"),
            patch("popcorn_core.operations.send_webhook", return_value=_SENT) as send,
        ):
            dispatch(args)
        assert send.call_args[0][1] == {}

    def test_name_target_is_resolved_through_the_channel(self, parser):
        args = parser.parse_args(["webhook", "send", "Intake", "--channel", "#ops"])
        with (
            patch("popcorn_cli.cli._get_client"),
            patch("popcorn_core.operations.resolve_webhook_url", return_value="u/1") as resolve,
            patch("popcorn_core.operations.send_webhook", return_value=_SENT),
        ):
            dispatch(args)
        assert resolve.call_args[0][1:] == ("Intake", "#ops")

    def test_file_payload(self, parser, tmp_path):
        body = tmp_path / "lead.json"
        body.write_text('{"from": "file"}')
        args = parser.parse_args(
            ["webhook", "send", "https://hooks.popcorn.ai/ingest/tok", f"@{body}"]
        )
        with patch("popcorn_core.operations.send_webhook", return_value=_SENT) as send:
            dispatch(args)
        assert send.call_args[0][1] == {"from": "file"}

    def test_stdin_payload(self, parser, monkeypatch):
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO('{"from": "stdin"}'))
        args = parser.parse_args(["webhook", "send", "https://hooks.popcorn.ai/ingest/tok", "@-"])
        with patch("popcorn_core.operations.send_webhook", return_value=_SENT) as send:
            dispatch(args)
        assert send.call_args[0][1] == {"from": "stdin"}

    def test_bad_payload_is_a_validation_error(self, parser):
        args = parser.parse_args(["webhook", "send", "https://hooks.popcorn.ai/ingest/tok", "nope"])
        with pytest.raises(PopcornError) as exc:
            dispatch(args)
        assert exc.value.error_code == "validation"

    def test_human_output_shows_status_and_body(self, parser, capsys):
        args = parser.parse_args(["webhook", "send", "https://hooks.popcorn.ai/ingest/tok"])
        sent = {
            "url": "https://hooks.popcorn.ai/ingest/tok",
            "status": 202,
            "response": {"status": "ok", "request_id": "req-9"},
        }
        with patch("popcorn_core.operations.send_webhook", return_value=sent):
            dispatch(args)
        out = capsys.readouterr().out
        assert "HTTP 202" in out
        assert "req-9" in out

    def test_json_output_carries_url_status_and_response(self, parser, capsys):
        args = parser.parse_args(
            ["--json", "webhook", "send", "https://hooks.popcorn.ai/ingest/tok"]
        )
        sent = {
            "url": "https://hooks.popcorn.ai/ingest/tok",
            "status": 200,
            "response": {"status": "ok", "request_id": "req-9"},
        }
        with patch("popcorn_core.operations.send_webhook", return_value=sent):
            dispatch(args)
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True
        assert payload["data"] == sent

    def test_missing_channel_for_a_name_target_errors_helpfully(self, parser):
        args = parser.parse_args(["webhook", "send", "Intake"])
        with patch("popcorn_cli.cli._get_client"), pytest.raises(PopcornError) as exc:
            dispatch(args)
        assert "--channel" in str(exc.value) or "channel" in str(exc.value).lower()
        assert exc.value.error_code == "validation"

    def test_non_2xx_exits_non_zero_with_the_body(self, parser):
        args = parser.parse_args(["webhook", "send", "https://hooks.popcorn.ai/ingest/tok"])
        err = APIError("Webhook send failed: HTTP 500\nboom", status_code=500, body="boom")
        with (
            patch("popcorn_core.operations.send_webhook", side_effect=err),
            pytest.raises(APIError) as exc,
        ):
            dispatch(args)
        assert exc.value.exit_code == EXIT_SERVER
        assert "boom" in exc.value.to_dict()["body"]


class TestChannelTemplates:
    def test_templates(self, parser):
        args = parser.parse_args(["channel", "templates"])
        assert args.command == "channel"
        assert args.channel_command == "templates"


class TestFlowCommands:
    def test_flow_list(self, parser):
        args = parser.parse_args(["flow", "list", "--channel", "#ops"])
        assert args.command == "flow"
        assert args.flow_command == "list"
        assert args.channel == "#ops"

    def test_flow_list_requires_channel(self, parser):
        with pytest.raises(SystemExit):
            parser.parse_args(["flow", "list"])

    def test_flow_get(self, parser):
        args = parser.parse_args(["flow", "get", "flow-1", "--channel", "#ops"])
        assert args.flow_command == "get"
        assert args.flow_id == "flow-1"

    def test_flow_run(self, parser):
        args = parser.parse_args(["flow", "run", "flow-1", "--channel", "#ops"])
        assert args.flow_command == "run"
        assert args.flow_id == "flow-1"
        assert args.inputs is None

    def test_flow_run_inputs(self, parser):
        args = parser.parse_args(
            ["flow", "run", "flow-1", "--channel", "#ops", "--inputs", '{"x":1}']
        )
        assert args.inputs == '{"x":1}'

    def test_flow_runs_list(self, parser):
        args = parser.parse_args(["flow", "runs", "list", "--channel", "#ops"])
        assert args.flow_command == "runs"
        assert args.flow_runs_command == "list"

    def test_flow_runs_list_status(self, parser):
        args = parser.parse_args(
            ["flow", "runs", "list", "--channel", "#ops", "--status", "running"]
        )
        assert args.status == "running"

    def test_flow_runs_list_status_invalid(self, parser):
        with pytest.raises(SystemExit):
            parser.parse_args(["flow", "runs", "list", "--channel", "#ops", "--status", "bogus"])

    def test_flow_runs_list_flow(self, parser):
        args = parser.parse_args(
            ["flow", "runs", "list", "--channel", "#ops", "--flow", "claim_turn"]
        )
        assert args.flow == "claim_turn"

    def test_flow_runs_list_page_token(self, parser):
        args = parser.parse_args(
            ["flow", "runs", "list", "--channel", "#ops", "--page-token", "tok"]
        )
        assert args.page_token == "tok"

    def test_flow_runs_get(self, parser):
        args = parser.parse_args(
            ["flow", "runs", "get", "wf-1", "--channel", "#ops", "--include-errors"]
        )
        assert args.flow_runs_command == "get"
        assert args.workflow_id == "wf-1"
        assert args.include_errors is True


class TestScheduleCommands:
    def test_schedule_list(self, parser):
        args = parser.parse_args(["schedule", "list", "--channel", "#ops"])
        assert args.schedule_command == "list"
        assert args.channel == "#ops"

    def test_schedule_list_requires_a_channel(self, parser):
        with pytest.raises(SystemExit):
            parser.parse_args(["schedule", "list"])

    def test_schedule_get(self, parser):
        args = parser.parse_args(["schedule", "get", "claim-tick", "--channel", "#ops"])
        assert args.schedule_command == "get"
        assert args.schedule == "claim-tick"

    def test_schedule_get_requires_a_ref(self, parser):
        with pytest.raises(SystemExit):
            parser.parse_args(["schedule", "get", "--channel", "#ops"])


class TestDidYouMean:
    def test_close_typo_suggests(self):
        parser = build_parser()
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["mesage"])
        assert exc_info.value.code == 2

    def test_close_typo_message(self, capsys):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["mesage"])
        err = capsys.readouterr().err
        assert "Did you mean" in err
        assert "message" in err

    def test_distant_typo_no_suggestion(self, capsys):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["xyzqwfoo"])
        err = capsys.readouterr().err
        assert "unknown command" in err
        assert "Did you mean" not in err

    def test_mistyped_subcommand_suggests_a_sibling(self, capsys):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["message", "serch"])
        err = capsys.readouterr().err
        assert 'unknown command "serch"' in err
        assert "Did you mean" in err
        assert "search" in err

    def test_unmatched_subcommand_points_at_its_own_help(self, capsys):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["message", "xyzqwfoo"])
        err = capsys.readouterr().err
        assert "Did you mean" not in err
        assert '"popcorn message --help"' in err


class TestInvalidChoiceOnAFlag:
    """A bad value for a `choices=` flag is not a mistyped command.

    The did-you-mean rewrite used to swallow every "invalid choice", so a bad
    `--type` was reported as an unknown command and argparse's list of the
    valid values — the one useful part — was dropped.
    """

    def test_flag_value_keeps_argparses_message(self, capsys):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["channel", "create", "example-channel", "--type", "publik"])
        err = capsys.readouterr().err
        assert "unknown command" not in err
        assert "--type" in err
        assert "invalid choice: 'publik'" in err
        assert "public_channel" in err

    def test_positional_value_keeps_argparses_message(self, capsys):
        # `completion`'s shell is a positional with `choices=`, but it is not
        # the subcommand positional, so it gets argparse's message too.
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["completion", "fish"])
        err = capsys.readouterr().err
        assert "unknown command" not in err
        assert "invalid choice: 'fish'" in err
        assert "zsh" in err


class TestCheckAccess:
    def test_check_access(self, parser):
        args = parser.parse_args(["workspace", "check-access", "acme/widgets"])
        assert args.command == "workspace"
        assert args.workspace_command == "check-access"
        assert args.repo == "acme/widgets"


class TestCommands:
    def test_commands_parses(self, parser):
        args = parser.parse_args(["commands"])
        assert args.command == "commands"

    def test_flow_schema_two_level_nesting(self, capsys):
        import argparse
        import json

        from popcorn_cli.cli import cmd_commands

        cmd_commands(argparse.Namespace(command="commands", groups=None))
        schema = json.loads(capsys.readouterr().out)
        flow = next(c for c in schema["commands"] if c["name"] == "flow")
        assert flow["category"] == "flows"
        names = {s["name"] for s in flow["subcommands"]}
        assert {"list", "get", "run", "runs"} <= names
        runs = next(s for s in flow["subcommands"] if s["name"] == "runs")
        # `runs` is itself a group → its own nested subcommands are described
        runs_names = {s["name"] for s in runs["subcommands"]}
        assert runs_names == {"list", "get", "cancel"}

    def test_flow_runs_list_in_pagination_commands(self, capsys):
        import argparse
        import json

        from popcorn_cli.cli import cmd_commands

        cmd_commands(argparse.Namespace(command="commands", groups=None))
        schema = json.loads(capsys.readouterr().out)
        paginated = schema["envelope"]["pagination"]["commands"]
        assert "flow runs list" in paginated
        # The bulk cancel pages on the same cursor; an agent reading the
        # schema has to learn that a sweep of >200 continues with --page-token.
        assert "flow runs cancel --flow" in paginated

    def test_commands_json_output(self, capsys):
        import json

        from popcorn_cli.cli import cmd_commands

        args = argparse.Namespace(command="commands")
        cmd_commands(args)
        out = capsys.readouterr().out
        schema = json.loads(out)
        assert "version" in schema
        assert "global_flags" in schema
        assert "commands" in schema
        # All top-level commands are present
        cmd_names = [c["name"] for c in schema["commands"]]
        for expected in ["message", "channel", "app", "auth", "commands"]:
            assert expected in cmd_names

    def test_commands_has_subcommands_for_auth(self, capsys):
        import json

        from popcorn_cli.cli import cmd_commands

        args = argparse.Namespace(command="commands")
        cmd_commands(args)
        out = capsys.readouterr().out
        schema = json.loads(out)
        auth_cmd = next(c for c in schema["commands"] if c["name"] == "auth")
        assert "subcommands" in auth_cmd
        sub_names = [s["name"] for s in auth_cmd["subcommands"]]
        assert "login" in sub_names
        assert "status" in sub_names

    def test_commands_message_has_subcommands(self, capsys):
        import json

        from popcorn_cli.cli import cmd_commands

        args = argparse.Namespace(command="commands")
        cmd_commands(args)
        out = capsys.readouterr().out
        schema = json.loads(out)
        msg_cmd = next(c for c in schema["commands"] if c["name"] == "message")
        assert "subcommands" in msg_cmd
        sub_names = [s["name"] for s in msg_cmd["subcommands"]]
        assert "send" in sub_names
        # send subcommand should have arguments
        send_sub = next(s for s in msg_cmd["subcommands"] if s["name"] == "send")
        arg_names = [a.get("name") or a.get("flags", [None])[0] for a in send_sub["arguments"]]
        assert "conversation" in arg_names

    def test_commands_have_categories(self, capsys):
        import json

        from popcorn_cli.cli import cmd_commands

        args = argparse.Namespace(command="commands")
        cmd_commands(args)
        out = capsys.readouterr().out
        schema = json.loads(out)
        chan_cmd = next(c for c in schema["commands"] if c["name"] == "channel")
        assert chan_cmd["category"] == "channels"
        msg_cmd = next(c for c in schema["commands"] if c["name"] == "message")
        assert msg_cmd["category"] == "messages"
        auth_cmd = next(c for c in schema["commands"] if c["name"] == "auth")
        assert auth_cmd["category"] == "auth"


class TestNewFlags:
    def test_debug_flag(self, parser):
        args = parser.parse_args(["--debug", "whoami"])
        assert args.debug is True

    def test_debug_flag_hoisted(self):
        from popcorn_cli.cli import _hoist_global_flags

        result = _hoist_global_flags(["message", "list", "--debug", "#general"])
        assert result == ["--debug", "message", "list", "#general"]

    def test_fail_fast_flag(self, parser):
        args = parser.parse_args(["message", "send", "--batch", "--fail-fast"])
        assert args.fail_fast is True

    def test_if_not_exists_flag(self, parser):
        args = parser.parse_args(["channel", "create", "test-ch", "--if-not-exists"])
        assert args.if_not_exists is True


_WEBHOOKS = {
    "webhooks": [
        {
            "id": "00000000-0000-4000-8000-000000000012",
            "name": "Intake",
            "url": "https://hooks.popcorn.ai/ingest/s3cr3t-token",
        }
    ]
}


class TestWebhookListUrl:
    """The ingest URL was reachable only through `--json`.

    The token in that URL is the credential — holding it is enough to post to
    the channel — so the decision here was to keep it out of default human
    output and make it opt-in, rather than printing it for everyone who lists
    their webhooks in a shared terminal.
    """

    def _run(self, parser, argv, capsys):
        args = parser.parse_args(argv)
        with (
            patch("popcorn_cli.cli._get_client", return_value=object()),
            patch("popcorn_core.operations.list_webhooks", return_value=_WEBHOOKS),
        ):
            dispatch(args)
        return capsys.readouterr().out

    def test_the_token_is_not_printed_by_default(self, parser, capsys):
        out = self._run(parser, ["webhook", "list", "#ops"], capsys)
        assert "s3cr3t-token" not in out
        assert "Intake" in out

    def test_the_default_output_says_the_url_is_available(self, parser, capsys):
        """Hiding it without saying so just sends people back to `--json`."""
        out = self._run(parser, ["webhook", "list", "#ops"], capsys)
        assert "--show-url" in out

    def test_show_url_prints_it(self, parser, capsys):
        out = self._run(parser, ["webhook", "list", "#ops", "--show-url"], capsys)
        assert "https://hooks.popcorn.ai/ingest/s3cr3t-token" in out
        assert "--show-url" not in out, "the footer is pointless once the URLs are shown"

    def test_json_still_carries_the_url_untouched(self, parser, capsys):
        out = self._run(parser, ["--json", "webhook", "list", "#ops"], capsys)
        payload = json.loads(out)
        assert payload["data"]["webhooks"][0]["url"].endswith("s3cr3t-token")

    def test_no_footer_when_the_api_returned_no_urls(self, parser, capsys):
        args = parser.parse_args(["webhook", "list", "#ops"])
        with (
            patch("popcorn_cli.cli._get_client", return_value=object()),
            patch(
                "popcorn_core.operations.list_webhooks",
                return_value={"webhooks": [{"id": "x", "name": "Intake"}]},
            ),
        ):
            dispatch(args)
        assert "--show-url" not in capsys.readouterr().out


class TestAppSurfaceListings:
    """`app lines` must reach every restatement of the app subcommand list.

    Mirrors the webhook guards above. The app family is registry-driven, so
    both shell completions derive from one declaration — the epilog is the
    one place still hand-maintained, and the one this catches.
    """

    def test_lines_reaches_both_completions(self, capsys):
        from popcorn_cli.cli import cmd_completion

        for shell in ("bash", "zsh"):
            cmd_completion(argparse.Namespace(shell=shell))
            out = capsys.readouterr().out
            assert "apply checkout fork lines list publish status" in out, (
                f"stale {shell} completion"
            )

    def test_lines_reaches_the_help_listing(self, parser):
        app_lines = [
            ln for ln in parser.format_help().splitlines() if ln.strip().startswith("app ")
        ]
        assert app_lines and all("lines" in ln for ln in app_lines)

    def test_the_epilog_lists_every_registered_app_subcommand(self, parser):
        """The epilog is hand-written, so it drifts from the registry silently.
        Checked as a set, so the next subcommand is caught too."""
        from popcorn_cli.registry import completion_words

        epilog_line = next(
            ln for ln in parser.format_help().splitlines() if ln.strip().startswith("app ")
        )
        missing = [name for name in completion_words("app") if name not in epilog_line]
        assert missing == [], f"app subcommands missing from the epilog: {missing}"

    def test_lines_is_dispatchable(self, parser):
        args = parser.parse_args(["app", "lines", "--channel", "#ops"])
        assert args.command == "app"
        assert args.app_command == "lines"
        assert args.channel == "#ops"

    def test_status_takes_a_channel_without_a_directory(self, parser):
        args = parser.parse_args(["app", "status", "--channel", "#ops"])
        assert args.app_command == "status"
        assert args.channel == "#ops"
        assert args.directory is None


# The channel a command acts on, spelled both ways. The pairs are the survey
# behind the dual-spelling work: every command in the message/channel/webhook
# families
# that names a channel, and for each the positional form callers already use
# alongside the `--channel` form that now works everywhere.
_CHANNEL_SPELLINGS = [
    (["message", "delete", "#c", "m-1"], ["message", "delete", "--channel", "#c", "m-1"]),
    (
        ["message", "edit", "#c", "m-1", "new text"],
        ["message", "edit", "--channel", "#c", "m-1", "new text"],
    ),
    (["message", "list", "#c"], ["message", "list", "--channel", "#c"]),
    (
        ["message", "react", "#c", "m-1", "tada"],
        ["message", "react", "--channel", "#c", "m-1", "tada"],
    ),
    (["message", "send", "#c"], ["message", "send", "--channel", "#c"]),
    (["message", "send", "#c", "hi"], ["message", "send", "--channel", "#c", "hi"]),
    (["message", "threads", "#c"], ["message", "threads", "--channel", "#c"]),
    (["channel", "archive", "#c"], ["channel", "archive", "--channel", "#c"]),
    (["channel", "delete", "#c"], ["channel", "delete", "--channel", "#c"]),
    (["channel", "edit", "#c"], ["channel", "edit", "--channel", "#c"]),
    (["channel", "info", "#c"], ["channel", "info", "--channel", "#c"]),
    (["channel", "invite", "#c", "u-1,u-2"], ["channel", "invite", "--channel", "#c", "u-1,u-2"]),
    (["channel", "join", "#c"], ["channel", "join", "--channel", "#c"]),
    (["channel", "kick", "#c", "u-1"], ["channel", "kick", "--channel", "#c", "u-1"]),
    (["channel", "leave", "#c"], ["channel", "leave", "--channel", "#c"]),
    (["webhook", "create", "#c", "hook"], ["webhook", "create", "--channel", "#c", "hook"]),
    (["webhook", "deliveries", "#c"], ["webhook", "deliveries", "--channel", "#c"]),
    (["webhook", "list", "#c"], ["webhook", "list", "--channel", "#c"]),
]


def _leaf_parsers(parser, path=()):
    """Every leaf subcommand parser, keyed by its command path."""
    nested = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    if not nested:
        yield path, parser
        return
    for action in nested:
        for name, sub in action.choices.items():
            yield from _leaf_parsers(sub, (*path, name))


_DIRECTORY_SPELLINGS = [
    (["app", "status", "/tmp/co"], ["app", "status", "--dir", "/tmp/co"]),
    (["app", "publish", "/tmp/co"], ["app", "publish", "--dir", "/tmp/co"]),
    (["app", "apply", "/tmp/co"], ["app", "apply", "--dir", "/tmp/co"]),
    (["template", "check", "/tmp/co"], ["template", "check", "--dir", "/tmp/co"]),
    (
        ["app", "checkout", "--channel", "#c", "/tmp/co"],
        ["app", "checkout", "--channel", "#c", "--dir", "/tmp/co"],
    ),
]


class TestDirectoryArgument:
    """One directory, two spellings, one namespace attribute.

    The channel half of this shipped first; this is the remainder. The
    machinery is shared — `registry.add_dual_spelled_argument` — so these
    guard the wiring and the cases the channel's own tests cannot reach: a
    REQUIRED positional (`template check`), and the `--fork` collision.
    """

    @pytest.mark.parametrize("positional,flag", _DIRECTORY_SPELLINGS, ids=lambda v: " ".join(v))
    def test_both_spellings_parse_to_the_same_namespace(self, parser, positional, flag):
        def strip(ns: dict) -> dict:
            return {k: v for k, v in ns.items() if not k.startswith(registry.FLAG_DEST_PREFIX)}

        assert strip(vars(parser.parse_args(positional))) == strip(vars(parser.parse_args(flag)))

    def test_every_directory_positional_also_accepts_the_flag(self, parser):
        """The guard for the next command someone adds.

        `flow import` is exempt: it is a removed command that only prints
        where bundles install from now, so its directory is accepted and
        ignored rather than read.
        """
        missing = []
        for path, leaf in _leaf_parsers(parser):
            if path == ("flow", "import"):
                continue
            positionals = {a.dest for a in leaf._actions if not a.option_strings}
            options = {opt for a in leaf._actions for opt in a.option_strings}
            if "directory" in positionals and "--dir" not in options:
                missing.append(" ".join(path))
        assert missing == [], f"directory positional without a --dir spelling: {missing}"

    def test_an_optional_directory_stays_optional(self, parser):
        """`app status` defaults to the cwd checkout, so neither form is required."""
        assert parser.parse_args(["app", "status"]).directory is None
        assert parser.parse_args(["app", "status", "--dir", "/tmp/co"]).directory == "/tmp/co"

    def test_a_required_directory_is_still_required(self, parser):
        """`template check` takes no default, and `nargs="?"` moved the
        requirement out of argparse, so it has to survive the fold."""
        with pytest.raises(SystemExit):
            parser.parse_args(["template", "check"])

    def test_the_required_error_names_both_spellings(self, parser, capsys):
        """A caller who omitted it should not have to guess which form exists."""
        with pytest.raises(SystemExit):
            parser.parse_args(["template", "check"])
        err = capsys.readouterr().err
        assert "directory" in err and "--dir" in err

    def test_giving_the_directory_twice_is_a_usage_error(self, parser):
        with pytest.raises(SystemExit):
            parser.parse_args(["app", "status", "/tmp/a", "--dir", "/tmp/b"])

    def test_dir_disentangles_the_checkout_fork_collision(self, parser):
        """`app checkout`'s own help documents that a bare `--fork` cannot be
        told apart from the directory positional, so `--fork mydir` names the
        LINE. Spelling the directory as a flag removes the collision instead
        of working around it."""
        args = parser.parse_args(
            ["app", "checkout", "--channel", "#c", "--dir", "/tmp/co", "--fork"]
        )
        assert (args.directory, args.fork) == ("/tmp/co", "")


class TestChannelArgument:
    """One channel, two spellings, one namespace attribute.

    The message/channel/webhook families take the channel positionally and
    the registry families take `--channel`; the split is an artifact of the
    order they were written. `--channel` now works on all of them, and the
    positional keeps working because skills, scripts and the eval harness are
    written that way.
    """

    @pytest.mark.parametrize("positional,flag", _CHANNEL_SPELLINGS, ids=lambda v: " ".join(v))
    def test_both_spellings_parse_to_the_same_namespace(self, parser, positional, flag):
        from_positional = vars(parser.parse_args(positional)).copy()
        from_flag = vars(parser.parse_args(flag)).copy()

        # A flag's own dest is scratch space the fold consumes; handlers read
        # the positional's dest, which is what has to match. Dropped by prefix
        # rather than by name so a new dual-spelled flag needs no edit here.
        def strip(ns: dict) -> dict:
            return {k: v for k, v in ns.items() if not k.startswith(registry.FLAG_DEST_PREFIX)}

        assert strip(from_positional) == strip(from_flag)

    def test_every_channel_positional_also_accepts_the_flag(self, parser):
        """The guard for the next command someone adds.

        A leaf declaring a bare `conversation`/`channel` positional instead of
        going through `_add_channel_argument` reintroduces exactly the split
        this ticket closed, and nothing else would notice.
        """
        missing = []
        for path, leaf in _leaf_parsers(parser):
            positionals = {a.dest for a in leaf._actions if not a.option_strings}
            options = {opt for a in leaf._actions for opt in a.option_strings}
            if positionals & {"conversation", "channel"} and "--channel" not in options:
                missing.append(" ".join(path))
        assert missing == [], f"channel positional without a --channel spelling: {missing}"

    def test_the_flag_fills_a_genuinely_optional_channel(self, parser):
        """Where neither form is required, the flag still lands on the dest.

        `message send` declares its channel `nargs="?"`, so the positional can
        be absent — and the fold has to put the flag's value on the positional's
        dest anyway. This was covered through `site status` until that family
        was removed.
        """
        assert parser.parse_args(["message", "send"]).conversation is None
        assert parser.parse_args(["message", "send", "--channel", "#c"]).conversation == "#c"

    def test_a_required_channel_is_still_required(self, parser):
        """`nargs="?"` moved the requirement out of argparse; it has to survive."""
        with pytest.raises(SystemExit):
            parser.parse_args(["message", "list"])
        with pytest.raises(SystemExit):
            parser.parse_args(["channel", "info"])

    def test_giving_the_channel_twice_is_a_usage_error(self, parser):
        with pytest.raises(SystemExit):
            parser.parse_args(["message", "list", "#a", "--channel", "#b"])

    def test_a_trailing_positional_is_not_swallowed_by_the_flag(self, parser):
        """argparse fills positionals left to right, so `--channel` plus an
        all-optional tail would otherwise land the tail in the channel slot."""
        args = parser.parse_args(["message", "send", "--channel", "#c", "hi"])
        assert (args.conversation, args.message) == ("#c", "hi")

    def test_the_schema_still_reports_the_channel_as_required(self):
        """`commands --json` is what an agent reads instead of `--help`.

        The positional is `nargs="?"` only so the flag can stand in for it —
        reporting argparse's answer would tell an agent `message list` runs
        without a channel.
        """
        from popcorn_cli.cli import _introspect_parser

        leaves = dict(_leaf_parsers(build_parser()))
        by_name = {
            a.get("name") or a["flags"][0]: a
            for a in _introspect_parser(leaves[("message", "list")])
        }
        assert by_name["conversation"]["required"] is True
        assert by_name["--channel"]["required"] is False
        # A leaf whose channel really is optional reports it that way, so the
        # required flag is computed per leaf rather than hardcoded by name.
        optional = {
            a.get("name") or a["flags"][0]: a
            for a in _introspect_parser(leaves[("message", "send")])
        }
        assert optional["conversation"]["required"] is False


class TestVersionUpdateNotice:
    """Plain `popcorn version` should say when a newer version exists.

    `version` is exempt from the auto-upgrade path (upgrading underneath
    someone who only asked what they are running is the wrong behaviour), so
    before this it was the one command that could truthfully report an old
    version forever while every other command silently self-upgraded.
    """

    @staticmethod
    def _run(parser, argv):
        from popcorn_cli.cli import cmd_version

        cmd_version(parser.parse_args(argv))

    def test_stdout_is_only_the_version(self, parser, capsys, monkeypatch):
        """Anything parsing this must not start seeing an update notice."""
        monkeypatch.setattr("popcorn_cli.cli._read_version_cache", lambda: ("99.0.0", 2**31))
        self._run(parser, ["version"])
        captured = capsys.readouterr()
        assert captured.out.strip() == f"popcorn {popcorn_cli.__version__}"
        assert "99.0.0" in captured.err

    def test_says_nothing_when_up_to_date(self, parser, capsys, monkeypatch):
        monkeypatch.setattr(
            "popcorn_cli.cli._read_version_cache",
            lambda: (popcorn_cli.__version__, 2**31),
        )
        self._run(parser, ["version"])
        assert capsys.readouterr().err == ""

    def test_a_stale_cache_is_refreshed_from_the_network(self, parser, capsys, monkeypatch):
        """Otherwise someone who only runs `version` never warms the cache."""
        monkeypatch.setattr("popcorn_cli.cli._read_version_cache", lambda: (None, 0))
        monkeypatch.setattr("popcorn_cli.cli._fetch_latest_version", lambda: "99.0.0")
        monkeypatch.setattr("popcorn_cli.cli._write_version_cache", lambda v: None)
        self._run(parser, ["version"])
        assert "99.0.0" in capsys.readouterr().err

    def test_offline_is_silent_not_an_error(self, parser, capsys, monkeypatch):
        monkeypatch.setattr("popcorn_cli.cli._read_version_cache", lambda: (None, 0))
        monkeypatch.setattr("popcorn_cli.cli._fetch_latest_version", lambda: None)
        self._run(parser, ["version"])
        captured = capsys.readouterr()
        assert captured.out.strip() == f"popcorn {popcorn_cli.__version__}"
        assert captured.err == ""

    def test_the_opt_out_is_honoured(self, parser, capsys, monkeypatch):
        monkeypatch.setenv("POPCORN_NO_UPDATE_CHECK", "1")
        monkeypatch.setattr("popcorn_cli.cli._read_version_cache", lambda: ("99.0.0", 2**31))
        self._run(parser, ["version"])
        assert capsys.readouterr().err == ""
