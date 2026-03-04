"""Tests for popcorn_cli.formatting."""

from __future__ import annotations

from popcorn_cli.formatting import (
    fmt_conversation,
    fmt_message,
    fmt_user,
    format_author,
    format_message_text,
    format_timestamp,
    set_color,
)


class TestFormatTimestamp:
    def test_valid_iso(self):
        assert format_timestamp("2025-01-15T14:30:00Z") == "2025-01-15 14:30"

    def test_none(self):
        assert format_timestamp(None) == "????"

    def test_empty(self):
        assert format_timestamp("") == "????"

    def test_invalid(self):
        result = format_timestamp("not-a-date")
        assert result == "not-a-date"[:16]


class TestFormatAuthor:
    def test_display_name(self):
        assert format_author({"author": {"display_name": "Alice"}}) == "Alice"

    def test_username_fallback(self):
        assert format_author({"author": {"username": "alice"}}) == "alice"

    def test_email_fallback(self):
        assert format_author({"author": {"email": "a@b.com"}}) == "a@b.com"

    def test_no_author(self):
        assert format_author({}) == "Unknown"


class TestFormatMessageText:
    def test_text_part(self):
        msg = {"content": {"parts": [{"type": "text", "content": "hello"}]}}
        assert format_message_text(msg) == "hello"

    def test_multiple_parts(self):
        msg = {
            "content": {
                "parts": [
                    {"type": "text", "content": "check this"},
                    {"type": "text", "content": "out"},
                ]
            }
        }
        assert format_message_text(msg) == "check this out"

    def test_empty_content(self):
        # With color disabled, no ANSI codes
        set_color(False)
        result = format_message_text({})
        assert "no text content" in result


class TestFmtMessage:
    def test_basic_message(self):
        set_color(False)
        msg = {
            "id": "m1",
            "created_at": "2025-01-15T14:30:00Z",
            "author": {"display_name": "Alice"},
            "content": {"parts": [{"type": "text", "content": "hello"}]},
        }
        result = fmt_message(msg)
        assert "Alice" in result
        assert "hello" in result
        assert "2025-01-15 14:30" in result

    def test_message_with_replies(self):
        set_color(False)
        msg = {
            "id": "m1",
            "created_at": "2025-01-15T14:30:00Z",
            "author": {"display_name": "Alice"},
            "content": {"parts": [{"type": "text", "content": "hello"}]},
            "reply_count": 3,
        }
        result = fmt_message(msg)
        assert "3 replies" in result


class TestFmtConversation:
    def test_channel(self):
        set_color(False)
        conv = {"name": "general", "type": "public_channel", "id": "c1"}
        result = fmt_conversation(conv)
        assert "#general" in result

    def test_dm(self):
        set_color(False)
        conv = {
            "name": "dm",
            "type": "dm",
            "id": "c2",
            "other_participants": [{"display_name": "Bob"}],
        }
        result = fmt_conversation(conv)
        assert "Bob" in result


class TestFmtUser:
    def test_with_email(self):
        result = fmt_user({"display_name": "Alice", "id": "u1", "email": "a@b.com"})
        assert "Alice" in result
        assert "a@b.com" in result

    def test_without_email(self):
        result = fmt_user({"username": "alice", "id": "u1"})
        assert "alice" in result
