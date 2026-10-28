"""Resolve `@name` mentions in message text to the ids the server notifies.

The server stores text as typed and notifies only the users named in
`mentioned_user_ids` (and everyone, or everyone present, for the two flags);
the app's composer resolves `@name` client-side, so a CLI message that skips
this step reads as a mention and notifies nobody.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .errors import PopcornError
from .paging import fetch_all

if TYPE_CHECKING:
    from .client import APIClient

# An `@` not glued to a word, dot or slash on its left — which rules out
# emails (a@b.com) and `/@handle` paths. URLs are blanked first so a query
# string's `=@x` doesn't count either.
_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)
_MENTION = re.compile(r"(?<![\w@./])@(\w[\w.-]*)")

_BROADCAST = {"everyone": "mentioned_everyone", "here": "mentioned_here"}


def mention_tokens(text: str) -> list[str]:
    """Every distinct `@token` in `text`, lowercased, in order of appearance."""
    seen: dict[str, None] = {}
    for match in _MENTION.finditer(_URL.sub(" ", text)):
        # A sentence can end on a mention: `@shaun.` names shaun.
        seen.setdefault(match.group(1).rstrip(".-").lower())
    return list(seen)


def _match(token: str, users: list[dict[str, Any]]) -> list[str]:
    """The ids `token` names: by username if any, else by display name."""
    for field in ("username", "display_name"):
        ids = sorted(
            {str(u["id"]) for u in users if u.get("id") and (u.get(field) or "").lower() == token}
        )
        if ids:
            return ids
    return []


class MentionResolver:
    """Turns message text into the post/edit body's mention fields.

    The workspace's members are read once, on the first token that needs them,
    and reused for every later message (a `--batch` run). A token that names no
    member, or more than one, stays plain text and is reported through `warn`;
    it never fails the send.
    """

    def __init__(self, client: APIClient, warn: Callable[[str], None]) -> None:
        self._client = client
        self._warn = warn
        self._users: list[dict[str, Any]] | None = None
        self._lookup_failed = False

    def __call__(self, text: str) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        ids: list[str] = []
        for token in mention_tokens(text):
            if token in _BROADCAST:
                fields[_BROADCAST[token]] = True
                continue
            if self._users is None:
                try:
                    self._users = fetch_all(self._client, "/api/users/list", {}, "users")
                except PopcornError as e:
                    self._warn(f"Couldn't read the workspace's members ({e}); @names sent as text")
                    self._users = []
                    self._lookup_failed = True
            if self._lookup_failed:
                continue
            matched = _match(token, self._users)
            if not matched:
                self._warn(f"@{token} didn't match a member; sent as plain text")
            elif len(matched) > 1:
                self._warn(f"@{token} matches {len(matched)} members; sent as plain text")
            elif matched[0] not in ids:
                ids.append(matched[0])
        if ids:
            fields["mentioned_user_ids"] = ids
        return fields
