"""Tests for CLI parser — ensures all commands parse correctly."""

from __future__ import annotations

import argparse

import pytest

from popcorn_cli.cli import build_parser


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


class TestAuthCommands:
    def test_auth_login(self, parser):
        args = parser.parse_args(["auth", "login"])
        assert args.command == "auth"
        assert args.auth_command == "login"

    def test_auth_login_with_token(self, parser):
        args = parser.parse_args(["auth", "login", "--with-token"])
        assert args.with_token is True

    def test_auth_login_workspace(self, parser):
        args = parser.parse_args(["auth", "login", "--workspace", "acme"])
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
        assert args.ws_command == "users"


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

    def test_webhook_list(self, parser):
        args = parser.parse_args(["webhook", "list", "#general"])
        assert args.webhook_command == "list"

    def test_webhook_deliveries(self, parser):
        args = parser.parse_args(["webhook", "deliveries", "#general", "--limit", "10"])
        assert args.webhook_command == "deliveries"
        assert args.conversation == "#general"
        assert args.limit == 10


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


class TestCheckAccess:
    def test_check_access(self, parser):
        args = parser.parse_args(["workspace", "check-access", "acme/widgets"])
        assert args.command == "workspace"
        assert args.ws_command == "check-access"
        assert args.repo == "acme/widgets"


class TestCommands:
    def test_commands_parses(self, parser):
        args = parser.parse_args(["commands"])
        assert args.command == "commands"

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
        for expected in ["message", "channel", "site", "auth", "commands"]:
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
        site_cmd = next(c for c in schema["commands"] if c["name"] == "site")
        assert site_cmd["category"] == "sites"
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
