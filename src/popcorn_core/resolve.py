"""Reference resolution — #channel-name and @user to UUID."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .errors import ERROR_CODE_NOT_FOUND, ERROR_CODE_VALIDATION, PopcornError
from .paging import fetch_all, iter_pages, listing_params

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


def _ambiguous(ref: str, matches: dict[str, dict[str, Any]]) -> PopcornError:
    spellings = sorted(
        f"#{conv.get('name') or '?'} ({conv_id})" for conv_id, conv in matches.items()
    )
    return PopcornError(
        f"'{ref}' matches more than one channel: {', '.join(spellings)}.\n"
        "   Channel names are case-sensitive — pass the exact name or the id instead.",
        error_code=ERROR_CODE_VALIDATION,
    )


def _matching(
    client: APIClient,
    params: dict[str, Any],
    keep: Callable[[str], bool],
    *,
    stop_on_match: bool,
) -> dict[str, dict[str, Any]]:
    """Conversations in a filtered listing whose name passes `keep`, by id.

    The server's filter narrows the listing; `keep` is what decides. They are
    not the same test — `query=` is a substring, the fallback wants equality —
    and re-checking also means a server that ignores the filter yields a slow
    walk rather than whichever channel sits at the top of an unfiltered page.

    Keyed by id because the listing cursor is an offset into a list the server
    recomputes per page, so one channel can surface twice.
    """
    found: dict[str, dict[str, Any]] = {}
    for page in iter_pages(client, "/api/conversations/list", params, "conversations"):
        for conv in page:
            conv_id = conv.get("id")
            if conv_id and keep(conv.get("name") or ""):
                found.setdefault(str(conv_id), conv)
        # The whole page holding a match is read, so two matches on it are
        # caught; the pages after it are not worth a request each.
        if found and stop_on_match:
            break
    return found


def resolve_conversation(client: APIClient, ref: str) -> str:
    """Resolve #channel-name to UUID, or pass through UUIDs.

    Asks the server for the exact name first, and only falls back to a
    case-insensitive match when exactly one channel answers to it. Name
    uniqueness is a case-SENSITIVE equality, so "#Ops" and "#ops" can both
    exist as different channels; picking one of them was a silent wrong
    answer, and a write command aimed at the wrong channel is worse than an
    error. The same holds for two channels with the identical name, which a
    channel shared in from another workspace can produce: the listing is
    everything the caller is a member of, not one workspace's channels.
    """
    if _is_uuid(ref):
        return ref

    # Case-preserved, so the cache cannot answer "#Ops" with a cached "#ops".
    # The server's exact match is case-sensitive too, so the two agree.
    name = ref.lstrip("#")
    if not name:
        raise PopcornError(f"Channel not found: #{name}", error_code=ERROR_CODE_NOT_FOUND)

    cached = _cached(_channel_cache, name, CHANNEL_CACHE_TTL)
    if cached is not None:
        return cached

    # Archived AND hidden are asked for: a caller naming a channel explicitly
    # means that channel whatever its visibility, and hidden ones are excluded
    # by default — which left `channel list --include-hidden` displaying names
    # every other command then rejected as "Channel not found".
    visibility = listing_params(include_archived=True, include_hidden=True)

    matches = _matching(
        client, {**visibility, "name": name}, lambda n: n == name, stop_on_match=True
    )
    if len(matches) > 1:
        raise _ambiguous(ref, matches)

    if not matches:
        # A case variant is only decidable over every match, so this one reads
        # to the end — of the substring matches, not of the workspace.
        folded = name.lower()
        matches = _matching(
            client,
            {**visibility, "query": name},
            lambda n: n.lower() == folded,
            stop_on_match=False,
        )
        if not matches:
            raise PopcornError(f"Channel not found: #{name}", error_code=ERROR_CODE_NOT_FOUND)
        if len(matches) > 1:
            raise _ambiguous(ref, matches)

    matched_id = next(iter(matches))
    _channel_cache[name] = (matched_id, time.time())
    return matched_id


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
