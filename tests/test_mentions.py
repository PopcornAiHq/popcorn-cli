"""`@name` mentions in `message send`/`edit`: tokenized, resolved against the
workspace's members, and sent as `mentioned_user_ids` so the server notifies."""

from __future__ import annotations

import io
import json
import sys
from typing import Any
from unittest.mock import MagicMock

import pytest

from popcorn_cli import cli
from popcorn_core import operations
from popcorn_core.errors import APIError
from popcorn_core.mentions import MentionResolver, mention_tokens


def _uuid(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


MEMBERS = [
    {"id": _uuid(11), "username": "example-shaun", "display_name": "Shaun"},
    {"id": _uuid(12), "username": "example-ben", "display_name": "Ben G"},
    {"id": _uuid(13), "username": "example-sam", "display_name": "Sam"},
    {"id": _uuid(14), "username": "example-sam2", "display_name": "Sam"},
]


@pytest.mark.parametrize(
    ("text", "tokens"),
    [
        ("@shaun can you look", ["shaun"]),
        ("hey @shaun, and @Ben.", ["shaun", "ben"]),
        ("(@shaun) @shaun! @SHAUN?", ["shaun"]),
        ("@example-shaun: ping", ["example-shaun"]),
        ("mail a@b.com or shaun@example.com", []),
        ("see https://example.com/@shaun?u=@ben and http://x.io/a@b", []),
        ("@everyone and @here", ["everyone", "here"]),
        ("@@shaun x@shaun /@shaun", []),
        ("no mentions at all @", []),
    ],
)
def test_mention_tokens(text: str, tokens: list[str]) -> None:
    assert mention_tokens(text) == tokens


def _resolver(members: list[dict[str, Any]] | None = None) -> tuple[Any, MagicMock, list[str]]:
    client = MagicMock()
    client.get.return_value = {"users": MEMBERS if members is None else members}
    warnings: list[str] = []
    return MentionResolver(client, warnings.append), client, warnings


def test_resolves_username_and_display_name_case_insensitively() -> None:
    resolve, _, warnings = _resolver()
    assert resolve("@SHAUN and @Example-Ben") == {"mentioned_user_ids": [_uuid(11), _uuid(12)]}
    assert warnings == []


def test_unknown_and_ambiguous_names_stay_text_with_a_warning() -> None:
    resolve, _, warnings = _resolver()
    assert resolve("@nobody @sam @shaun") == {"mentioned_user_ids": [_uuid(11)]}
    assert warnings == [
        "@nobody didn't match a member; sent as plain text",
        "@sam matches 2 members; sent as plain text",
    ]


def test_broadcasts_need_no_member_lookup() -> None:
    resolve, client, _ = _resolver()
    assert resolve("@everyone @here") == {"mentioned_everyone": True, "mentioned_here": True}
    assert resolve("no names") == {}
    client.get.assert_not_called()


def test_members_are_read_once_per_resolver() -> None:
    resolve, client, _ = _resolver()
    resolve("@shaun")
    resolve("@ben")
    assert client.get.call_count == 1
    assert client.get.call_args.args[0] == "/api/users/list"


def test_a_failed_member_lookup_never_fails_the_send() -> None:
    resolve, client, warnings = _resolver()
    client.get.side_effect = APIError("boom")
    assert resolve("@shaun @ben @everyone") == {"mentioned_everyone": True}
    assert len(warnings) == 1 and "members" in warnings[0]


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    client = MagicMock()
    client.get.return_value = {"users": MEMBERS}
    client.post.return_value = {"message": {"id": _uuid(99)}}
    monkeypatch.setattr(operations, "resolve_conversation", lambda c, conv: _uuid(1))
    monkeypatch.setattr(cli, "_check_and_update", lambda: None)
    monkeypatch.setattr(cli, "_get_client", lambda args: client)
    return client


def _run(monkeypatch: pytest.MonkeyPatch, *argv: str) -> None:
    monkeypatch.setattr(sys, "argv", ["popcorn", *argv])
    cli.main()


def test_send_carries_mentioned_user_ids(
    client: MagicMock, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(monkeypatch, "message", "send", "#general", "@shaun @ghost, @everyone look")
    path = client.post.call_args.args[0]
    body = client.post.call_args.kwargs["data"]
    assert path == "/api/messages/post"
    assert body["mentioned_user_ids"] == [_uuid(11)]
    assert body["mentioned_everyone"] is True
    assert body["content"]["parts"][0]["content"] == "@shaun @ghost, @everyone look"
    assert "@ghost didn't match a member" in capsys.readouterr().err


def test_no_mentions_sends_plain_text(client: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
    _run(monkeypatch, "message", "send", "#general", "@shaun hi", "--no-mentions")
    body = client.post.call_args.kwargs["data"]
    assert "mentioned_user_ids" not in body
    client.get.assert_not_called()


def test_edit_carries_mentioned_user_ids(
    client: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(monkeypatch, "message", "edit", "#general", _uuid(5), "cc @Example-Ben")
    assert client.post.call_args.args[0] == "/api/messages/edit"
    assert client.post.call_args.kwargs["data"]["mentioned_user_ids"] == [_uuid(12)]


def test_batch_resolves_each_line_with_one_lookup(
    client: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    lines = [
        {"conversation": "#a", "message": "@shaun one"},
        {"conversation": "#b", "message": "@example-ben two"},
    ]
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n".join(json.dumps(x) for x in lines)))
    _run(monkeypatch, "message", "send", "--batch", "--json")
    bodies = [c.kwargs["data"] for c in client.post.call_args_list]
    assert [b["mentioned_user_ids"] for b in bodies] == [[_uuid(11)], [_uuid(12)]]
    assert client.get.call_count == 1
