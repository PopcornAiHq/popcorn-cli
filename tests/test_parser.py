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

        assert _hoist_global_flags(["list-messages", "--json", "#general"]) == [
            "--json",
            "list-messages",
            "#general",
        ]

    def test_quiet_after_subcommand(self):
        from popcorn_cli.cli import _hoist_global_flags

        assert _hoist_global_flags(["list-messages", "-q", "#general"]) == [
            "-q",
            "list-messages",
            "#general",
        ]

    def test_timeout_after_subcommand(self):
        from popcorn_cli.cli import _hoist_global_flags

        assert _hoist_global_flags(["list-messages", "--timeout", "60", "#general"]) == [
            "--timeout",
            "60",
            "list-messages",
            "#general",
        ]

    def test_multiple_flags_hoisted(self):
        from popcorn_cli.cli import _hoist_global_flags

        result = _hoist_global_flags(
            ["list-messages", "--json", "-q", "--timeout", "10", "#general"]
        )
        assert result == ["--json", "-q", "--timeout", "10", "list-messages", "#general"]


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
        args = parser.parse_args(["search", "channels", "test"])
        assert args.command == "search"
        assert args.search_type == "channels"
        assert args.query == "test"

    def test_list_messages(self, parser):
        args = parser.parse_args(["list-messages", "#general", "--limit", "10"])
        assert args.command == "list-messages"
        assert args.conversation == "#general"
        assert args.limit == 10

    def test_list_messages_thread(self, parser):
        args = parser.parse_args(["list-messages", "#general", "--thread", "t-123"])
        assert args.thread == "t-123"

    def test_inbox_unread(self, parser):
        args = parser.parse_args(["inbox", "--unread"])
        assert args.unread is True

    def test_inbox_read_unread_exclusive(self, parser):
        with pytest.raises(SystemExit):
            parser.parse_args(["inbox", "--unread", "--read"])

    def test_list_threads(self, parser):
        args = parser.parse_args(["list-threads", "#general"])
        assert args.command == "list-threads"
        assert args.conversation == "#general"

    def test_list_threads_with_limit(self, parser):
        args = parser.parse_args(["list-threads", "#general", "--limit", "10"])
        assert args.limit == 10

    def test_list_threads_with_offset(self, parser):
        args = parser.parse_args(["list-threads", "#general", "--offset", "50"])
        assert args.offset == 50

    def test_list_messages_cursor(self, parser):
        args = parser.parse_args(["list-messages", "#general", "--cursor", "abc123"])
        assert args.cursor == "abc123"

    def test_inbox_cursor(self, parser):
        args = parser.parse_args(["inbox", "--cursor", "xyz"])
        assert args.cursor == "xyz"

    def test_search_cursor(self, parser):
        args = parser.parse_args(["search", "messages", "hello", "--cursor", "c1"])
        assert args.cursor == "c1"

    def test_watch_count(self, parser):
        args = parser.parse_args(["watch", "#general", "--count", "5"])
        assert args.count == 5

    def test_watch_max_wait(self, parser):
        args = parser.parse_args(["watch", "#general", "--max-wait", "30"])
        assert args.max_wait == 30.0


class TestWritingCommands:
    def test_send_message(self, parser):
        args = parser.parse_args(["send-message", "#general", "hello world"])
        assert args.command == "send-message"
        assert args.conversation == "#general"
        assert args.message == "hello world"

    def test_send_message_batch(self, parser):
        args = parser.parse_args(["send-message", "--batch"])
        assert args.batch is True
        assert args.conversation is None

    def test_react(self, parser):
        args = parser.parse_args(["react", "#general", "msg-1", "thumbsup"])
        assert args.emoji == "thumbsup"

    def test_react_remove(self, parser):
        args = parser.parse_args(["react", "#general", "msg-1", "thumbsup", "--remove"])
        assert args.remove is True

    def test_edit_message(self, parser):
        args = parser.parse_args(["edit-message", "#general", "msg-1", "new content"])
        assert args.content == "new content"

    def test_delete_message(self, parser):
        args = parser.parse_args(["delete-message", "#general", "msg-1"])
        assert args.message_id == "msg-1"


class TestChannelManagement:
    def test_create_channel(self, parser):
        args = parser.parse_args(["create-channel", "new-channel"])
        assert args.command == "create-channel"
        assert args.name == "new-channel"

    def test_create_channel_private(self, parser):
        args = parser.parse_args(["create-channel", "secret", "--type", "private_channel"])
        assert args.type == "private_channel"

    def test_join_channel(self, parser):
        args = parser.parse_args(["join-channel", "#general"])
        assert args.conversation == "#general"

    def test_archive_channel_undo(self, parser):
        args = parser.parse_args(["archive-channel", "#general", "--undo"])
        assert args.undo is True

    def test_invite(self, parser):
        args = parser.parse_args(["invite", "#general", "u1,u2"])
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
            parser.parse_args(["send-mesage"])
        assert exc_info.value.code == 2

    def test_close_typo_message(self, capsys):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["send-mesage"])
        err = capsys.readouterr().err
        assert "Did you mean" in err
        assert "send-message" in err

    def test_distant_typo_no_suggestion(self, capsys):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["xyzqwfoo"])
        err = capsys.readouterr().err
        assert "unknown command" in err
        assert "Did you mean" not in err

    def test_inbox_typo(self, capsys):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["imbox"])
        err = capsys.readouterr().err
        assert "Did you mean" in err
        assert "inbox" in err


class TestCheckAccess:
    def test_check_access(self, parser):
        args = parser.parse_args(["check-access", "acme/widgets"])
        assert args.command == "check-access"
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
        for expected in ["send-message", "list-messages", "auth", "pop", "commands"]:
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

    def test_commands_send_message_has_arguments(self, capsys):
        import json

        from popcorn_cli.cli import cmd_commands

        args = argparse.Namespace(command="commands")
        cmd_commands(args)
        out = capsys.readouterr().out
        schema = json.loads(out)
        send_cmd = next(c for c in schema["commands"] if c["name"] == "send-message")
        assert "arguments" in send_cmd
        arg_names = [a.get("name") or a.get("flags", [None])[0] for a in send_cmd["arguments"]]
        assert "conversation" in arg_names

    def test_commands_have_categories(self, capsys):
        import json

        from popcorn_cli.cli import cmd_commands

        args = argparse.Namespace(command="commands")
        cmd_commands(args)
        out = capsys.readouterr().out
        schema = json.loads(out)
        pop_cmd = next(c for c in schema["commands"] if c["name"] == "pop")
        assert pop_cmd["category"] == "sites"
        send_cmd = next(c for c in schema["commands"] if c["name"] == "send-message")
        assert send_cmd["category"] == "messages"
        auth_cmd = next(c for c in schema["commands"] if c["name"] == "auth")
        assert auth_cmd["category"] == "auth"


class TestNewFlags:
    def test_debug_flag(self, parser):
        args = parser.parse_args(["--debug", "whoami"])
        assert args.debug is True

    def test_debug_flag_hoisted(self):
        from popcorn_cli.cli import _hoist_global_flags

        result = _hoist_global_flags(["list-messages", "--debug", "#general"])
        assert result == ["--debug", "list-messages", "#general"]

    def test_fail_fast_flag(self, parser):
        args = parser.parse_args(["send-message", "--batch", "--fail-fast"])
        assert args.fail_fast is True

    def test_if_not_exists_flag(self, parser):
        args = parser.parse_args(["create-channel", "test-ch", "--if-not-exists"])
        assert args.if_not_exists is True
