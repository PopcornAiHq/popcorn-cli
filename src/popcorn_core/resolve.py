"""Reference resolution — #channel-name and @user to UUID."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from .errors import ERROR_CODE_NOT_FOUND, ERROR_CODE_VALIDATION, PopcornError
from .paging import fetch_all, iter_pages

if TYPE_CHECKING:
    from .client import APIClient

_channel_cache: dict[str, tuple[str, float]] = {}
_user_cache: dict[str, tuple[str, float]] = {}
CHANNEL_CACHE_TTL = 300  # seconds
USER_CACHE_TTL = 300  # seconds


def _is_uuid(ref: str) -> bool:
    """Whether `ref` is already an id, so no lookup is worth paying for.

    Shape-only: a caller that hands us a malformed id gets the server's error
    about that id, which is more use than one invented here about a channel
    or user name we never looked up.
    """
    return len(ref) == 36 and ref.count("-") == 4


def _cached(cache: dict[str, tuple[str, float]], key: str, ttl: float) -> str | None:
    entry = cache.get(key)
    if entry is None:
        return None
    value, cached_at = entry
    return value if time.time() - cached_at < ttl else None


def resolve_conversation(client: APIClient, ref: str) -> str:
    """Resolve #channel-name to UUID, or pass through UUIDs."""
    if _is_uuid(ref):
        return ref

    # Strip leading # if present
    name = ref.lstrip("#").lower()

    cached = _cached(_channel_cache, name, CHANNEL_CACHE_TTL)
    if cached is not None:
        return cached

    # Page through the listing rather than taking one maximal page: past that
    # page the server reports a cursor, and ignoring it turned "your workspace
    # is large" into "Channel not found". Stop at the first exact match, so a
    # name near the front still costs one request.
    for page in iter_pages(client, "/api/conversations/list", {}, "conversations"):
        for conv in page:
            conv_name = (conv.get("name") or "").lower()
            if conv_name == name:
                conv_id: str = conv["id"]
                _channel_cache[name] = (conv_id, time.time())
                return conv_id

    raise PopcornError(f"Channel not found: #{name}", error_code=ERROR_CODE_NOT_FOUND)


def _user_handles(user: dict[str, Any]) -> set[str]:
    """Every spelling of a user a caller might reasonably type."""
    return {
        (user.get(field) or "").lower()
        for field in ("username", "email", "display_name")
        if user.get(field)
    }


def resolve_user(client: APIClient, ref: str) -> str:
    """Resolve a username, email or display name to a user UUID.

    Matches on the whole handle rather than a substring: a filter that
    silently widens to a second person is worse than one that fails and asks
    for a more specific spelling.
    """
    if _is_uuid(ref):
        return ref

    name = ref.lstrip("@").lower()

    cached = _cached(_user_cache, name, USER_CACHE_TTL)
    if cached is not None:
        return cached

    # Every page, not the first: the ambiguity check below is only meaningful
    # over the whole workspace, so there is no early exit here.
    users = fetch_all(client, "/api/users/list", {}, "users")

    matched = [u for u in users if name in _user_handles(u) and u.get("id")]
    ids = {str(u["id"]) for u in matched}

    if not ids:
        raise PopcornError(f"User not found: {ref}", error_code=ERROR_CODE_NOT_FOUND)

    if len(ids) > 1:
        spellings = sorted(
            f"{u.get('username') or u.get('display_name') or '?'} ({u['id']})" for u in matched
        )
        raise PopcornError(
            f"'{ref}' matches more than one user: {', '.join(spellings)}.\n"
            "   Pass the email or the id instead.",
            error_code=ERROR_CODE_VALIDATION,
        )

    user_id = ids.pop()
    _user_cache[name] = (user_id, time.time())
    return user_id
