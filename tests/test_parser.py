"""Tests for CLI parser — ensures all commands parse correctly."""

from __future__ import annotations

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

        assert _hoist_global_flags(["read", "--json", "#general"]) == [
            "--json",
            "read",
            "#general",
        ]

    def test_quiet_after_subcommand(self):
        from popcorn_cli.cli import _hoist_global_flags

        assert _hoist_global_flags(["read", "-q", "#general"]) == [
            "-q",
            "read",
            "#general",
        ]

    def test_timeout_after_subcommand(self):
        from popcorn_cli.cli import _hoist_global_flags

        assert _hoist_global_flags(["read", "--timeout", "60", "#general"]) == [
            "--timeout",
            "60",
            "read",
            "#general",
        ]

    def test_multiple_flags_hoisted(self):
        from popcorn_cli.cli import _hoist_global_flags

        result = _hoist_global_flags(["read", "--json", "-q", "--timeout", "10", "#general"])
        assert result == ["--json", "-q", "--timeout", "10", "read", "#general"]


class TestAuthCommands:
    def test_auth_login(self, parser):
        args = parser.parse_args(["auth", "login"])
        assert args.command == "auth"
        assert args.auth_command == "login"

    def test_auth_login_with_token(self, parser):
        args = parser.parse_args(["auth", "login", "--with-token"])
        assert args.with_token is True

    def test_auth_status(self, parser):
        args = parser.parse_args(["auth", "status"])
        assert args.auth_command == "status"


class TestReadingCommands:
    def test_search(self, parser):
        args = parser.parse_args(["search", "channels", "test"])
        assert args.command == "search"
        assert args.search_type == "channels"
        assert args.query == "test"

    def test_read(self, parser):
        args = parser.parse_args(["read", "#general", "--limit", "10"])
        assert args.command == "read"
        assert args.conversation == "#general"
        assert args.limit == 10

    def test_read_thread(self, parser):
        args = parser.parse_args(["read", "#general", "--thread", "t-123"])
        assert args.thread == "t-123"

    def test_inbox_unread(self, parser):
        args = parser.parse_args(["inbox", "--unread"])
        assert args.unread is True

    def test_inbox_read_unread_exclusive(self, parser):
        with pytest.raises(SystemExit):
            parser.parse_args(["inbox", "--unread", "--read"])


class TestWritingCommands:
    def test_send(self, parser):
        args = parser.parse_args(["send", "#general", "hello world"])
        assert args.command == "send"
        assert args.conversation == "#general"
        assert args.message == "hello world"

    def test_send_batch(self, parser):
        args = parser.parse_args(["send", "--batch"])
        assert args.batch is True
        assert args.conversation is None

    def test_react(self, parser):
        args = parser.parse_args(["react", "#general", "msg-1", "thumbsup"])
        assert args.emoji == "thumbsup"

    def test_react_remove(self, parser):
        args = parser.parse_args(["react", "#general", "msg-1", "thumbsup", "--remove"])
        assert args.remove is True

    def test_edit(self, parser):
        args = parser.parse_args(["edit", "#general", "msg-1", "new content"])
        assert args.content == "new content"

    def test_delete(self, parser):
        args = parser.parse_args(["delete", "#general", "msg-1"])
        assert args.message_id == "msg-1"


class TestChannelManagement:
    def test_create(self, parser):
        args = parser.parse_args(["create", "new-channel"])
        assert args.command == "create"
        assert args.name == "new-channel"

    def test_create_private(self, parser):
        args = parser.parse_args(["create", "secret", "--type", "private_channel"])
        assert args.type == "private_channel"

    def test_join(self, parser):
        args = parser.parse_args(["join", "#general"])
        assert args.conversation == "#general"

    def test_archive_undo(self, parser):
        args = parser.parse_args(["archive", "#general", "--undo"])
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


class TestWebhook:
    def test_webhook_create(self, parser):
        args = parser.parse_args(["webhook", "create", "#general", "https://example.com/hook"])
        assert args.command == "webhook"
        assert args.webhook_command == "create"
        assert args.url == "https://example.com/hook"

    def test_webhook_list(self, parser):
        args = parser.parse_args(["webhook", "list", "#general"])
        assert args.webhook_command == "list"


class TestDidYouMean:
    def test_close_typo_suggests(self):
        parser = build_parser()
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["sned"])
        assert exc_info.value.code == 2

    def test_close_typo_message(self, capsys):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["sned"])
        err = capsys.readouterr().err
        assert "Did you mean" in err
        assert "send" in err

    def test_distant_typo_no_suggestion(self, capsys):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["zzzznotacommand"])
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
