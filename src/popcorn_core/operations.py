"""Business operations for Popcorn messaging.

Every function takes an APIClient + plain parameters and returns raw data dicts.
No I/O, no formatting, no argparse — just business logic.
"""

from __future__ import annotations

import json
import mimetypes
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

import httpx

from .errors import APIError, PopcornError
from .paging import fetch_all, listing_params
from .resolve import resolve_conversation, resolve_user

if TYPE_CHECKING:
    from .client import APIClient


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def get_whoami(client: APIClient) -> dict[str, Any]:
    """Get current user and workspace info."""
    return client.get("/api/users/current-user")


def list_workspaces(client: APIClient) -> list[dict[str, Any]]:
    """List workspaces for the current user."""
    resp = client.get("/api/users/my-workspaces")
    workspaces: list[dict[str, Any]] = resp.get("workspaces", [])
    return workspaces


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

_CHANNEL_TYPES = (
    "workspace_channel,public_channel,private_channel,shared_channel,shared_private_channel"
)


def search_channels(
    client: APIClient,
    query: str = "",
    *,
    include_archived: bool = False,
    include_hidden: bool = False,
) -> dict[str, Any]:
    """Search channels, optionally filtering by name.

    The name filter is applied here because neither listing endpoint takes a
    query — the server can only be asked for the whole list.
    """
    params = {
        "types": _CHANNEL_TYPES,
        **listing_params(include_archived=include_archived, include_hidden=include_hidden),
    }
    convs = fetch_all(client, "/api/conversations/list", params, "conversations")
    if query:
        q = query.lower()
        convs = [c for c in convs if q in (c.get("name") or "").lower()]
    return {"conversations": convs}


def search_dms(
    client: APIClient,
    query: str = "",
    *,
    include_archived: bool = False,
    include_hidden: bool = False,
) -> dict[str, Any]:
    """Search DMs, optionally filtering by participant name."""
    params = {
        "types": "dm,group_dm",
        **listing_params(include_archived=include_archived, include_hidden=include_hidden),
    }
    convs = fetch_all(client, "/api/conversations/list", params, "conversations")
    if query:
        q = query.lower()
        convs = [
            c
            for c in convs
            if any(
                q in (p.get("display_name") or p.get("username") or "").lower()
                for p in c.get("other_participants") or []
            )
        ]
    return {"conversations": convs}


def search_users(client: APIClient, query: str = "") -> dict[str, Any]:
    """Search users, optionally filtering by name/email."""
    users = fetch_all(client, "/api/users/list", {}, "users")
    if query:
        q = query.lower()
        users = [
            u
            for u in users
            if q in (u.get("display_name") or "").lower()
            or q in (u.get("username") or "").lower()
            or q in (u.get("email") or "").lower()
        ]
    return {"users": users}


# `/api/search/` is a UNIFIED index — messages, files, conversations and link
# content — and it searches files by default. Left at its defaults, a command
# called `message search` reports a `total` and an `index_counts` that describe
# results it never renders, so an agent deciding whether to page reads a number
# that does not describe what it is iterating. Pinning every index off but
# messages makes both describe the messages actually returned. Rendering the
# other buckets is a different command, not a wider default here.
#
# This does NOT make a page exactly `limit` long. The server asks the index for
# `limit` hits and then drops any it cannot hydrate — a message deleted since it
# was indexed, or one in a conversation the caller cannot read — so a short page
# is normal and carries no information about whether more results exist. Page
# until `has_more` is false; never until a page comes back short.
_MESSAGE_ONLY_INDEXES = {
    "search_messages": "true",
    "search_files": "false",
    "search_conversations": "false",
    "search_links": "false",
}

SORT_OPTIONS = ("relevance", "date_asc", "date_desc")

# The filters the server accepts in place of a query. Naming them here keeps
# the CLI's own "what may I omit a query for" answer identical to the
# server's, instead of a guess that drifts from it.
_QUERY_SUBSTITUTE_FILTERS = (
    "conversations",
    "from_users",
    "created_after",
    "created_before",
    "has",
)


def _resolve_refs(client: APIClient, refs: str, resolver: Callable[[APIClient, str], str]) -> str:
    """Resolve a comma-separated list of names to a comma-separated id list."""
    resolved = [resolver(client, ref.strip()) for ref in refs.split(",") if ref.strip()]
    return ",".join(resolved)


def search_messages(
    client: APIClient,
    query: str,
    limit: int = 50,
    offset: int = 0,
    *,
    conversations: str = "",
    from_users: str = "",
    created_after: str = "",
    created_before: str = "",
    sort_by: str = "",
    has: str = "",
) -> dict[str, Any]:
    """Full-text search across messages.

    `conversations` and `from_users` take channel names and usernames as well
    as ids, comma-separated; both are resolved here so a caller never has to
    look an id up to filter by a name it already knows.
    """
    params: dict[str, Any] = {"query": query, "limit": limit, **_MESSAGE_ONLY_INDEXES}

    if conversations:
        params["conversations"] = _resolve_refs(client, conversations, resolve_conversation)
    if from_users:
        params["from_users"] = _resolve_refs(client, from_users, resolve_user)
    if created_after:
        params["created_after"] = created_after
    if created_before:
        params["created_before"] = created_before
    if has:
        params["has"] = has
    if sort_by:
        params["sort_by"] = sort_by

    if not query and not any(params.get(f) for f in _QUERY_SUBSTITUTE_FILTERS):
        raise PopcornError(
            "Query required for message search, unless you filter instead "
            "(--in, --from, --since, --until, --has).\n"
            '   Usage: popcorn message search "<query>"'
        )

    if offset:
        params["offset"] = offset
    return client.get("/api/search/", params)


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def list_threads(
    client: APIClient,
    conversation: str,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """List threads in a conversation, ordered by most recent reply."""
    conv_id = resolve_conversation(client, conversation)
    params: dict[str, Any] = {"conversation": conv_id, "limit": limit}
    if offset:
        params["offset"] = offset
    return client.get("/api/messages/threads", params)


def read_messages(
    client: APIClient,
    conversation: str,
    thread_id: str = "",
    limit: int = 25,
    latest: str = "",
    oldest: str = "",
) -> dict[str, Any]:
    """Read message history from a channel, DM, or thread."""
    conv_id = resolve_conversation(client, conversation)
    params: dict[str, Any] = {"limit": limit, "conversation": conv_id}
    if latest:
        params["latest"] = latest
    if oldest:
        params["oldest"] = oldest
    if thread_id:
        params["thread_ts"] = thread_id
        return client.get("/api/messages/thread", params)
    return client.get("/api/messages/history", params)


def send_message(
    client: APIClient,
    conversation: str,
    text: str = "",
    thread_id: str = "",
    file_parts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Send a message to a channel or DM."""
    conv_id = resolve_conversation(client, conversation)
    parts: list[dict[str, Any]] = list(file_parts or [])
    if text:
        parts.append({"type": "text", "content": text})
    if not parts:
        raise PopcornError("Nothing to send — provide text or file")

    body: dict[str, Any] = {
        "conversation": conv_id,
        "content": {"parts": parts},
    }
    if thread_id:
        body["thread_id"] = thread_id
    return client.post("/api/messages/post", data=body)


def add_reaction(
    client: APIClient, conversation: str, message_id: str, emoji: str
) -> dict[str, Any]:
    """Add an emoji reaction to a message."""
    conv_id = resolve_conversation(client, conversation)
    return client.post(
        "/api/messages/reactions-add",
        data={"conversation": conv_id, "message": message_id, "emoji": emoji},
    )


def remove_reaction(
    client: APIClient, conversation: str, message_id: str, emoji: str
) -> dict[str, Any]:
    """Remove an emoji reaction from a message."""
    conv_id = resolve_conversation(client, conversation)
    return client.post(
        "/api/messages/reactions-remove",
        data={"conversation": conv_id, "message": message_id, "emoji": emoji},
    )


def edit_message(
    client: APIClient, conversation: str, message_id: str, content: str
) -> dict[str, Any]:
    """Edit a previously sent message."""
    conv_id = resolve_conversation(client, conversation)
    return client.post(
        "/api/messages/edit",
        data={
            "conversation": conv_id,
            "message": message_id,
            "content": {"parts": [{"type": "text", "content": content}]},
        },
    )


def delete_message(client: APIClient, conversation: str, message_id: str) -> dict[str, Any]:
    """Delete a message."""
    conv_id = resolve_conversation(client, conversation)
    return client.post(
        "/api/messages/delete",
        data={"conversation": conv_id, "message": message_id},
    )


def get_message(client: APIClient, message_id: str) -> dict[str, Any]:
    """Get a single message by ID."""
    return client.get("/api/messages/get", {"message": message_id})


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------


def get_conversation_info(client: APIClient, conversation: str) -> dict[str, Any]:
    """Get conversation details and member list."""
    conv_id = resolve_conversation(client, conversation)
    info = client.get("/api/conversations/info", {"conversation": conv_id})
    members = client.get("/api/conversations/members", {"conversation": conv_id})
    return {
        "conversation": info.get("conversation", {}),
        "members": members.get("members", []),
    }


def create_conversation(
    client: APIClient,
    name: str,
    conv_type: str = "public_channel",
    member_ids: list[str] | None = None,
    template: str | None = None,
) -> dict[str, Any]:
    """Create a new conversation (channel or DM), optionally from a template.

    `template` names a registry template (what `channel templates` lists) and
    is the ONLY way to install one -- the install runs server-side, in the
    worker, after the channel exists. An unknown name is rejected up front with
    a 400 rather than creating a channel whose install silently no-ops.
    """
    body: dict[str, Any] = {"name": name, "conversation_type": conv_type}
    if member_ids:
        body["member_ids"] = member_ids
    if template:
        body["template"] = template
    return client.post("/api/conversations/create", data=body)


def join_conversation(client: APIClient, conversation: str) -> dict[str, Any]:
    """Join a conversation."""
    conv_id = resolve_conversation(client, conversation)
    return client.post("/api/conversations/join", data={"conversation": conv_id})


# ---------------------------------------------------------------------------
# VM (workspace VM agent execution)
# ---------------------------------------------------------------------------


def _normalize_item_id(item_id: str) -> str:
    """Strip queue prefix from item_id (e.g. 'project-foo/slug' → 'slug')."""
    return item_id.split("/")[-1] if "/" in item_id else item_id


def vm_trace_current(client: APIClient, queue_id: str) -> dict[str, Any] | None:
    """Fetch the trace for the currently active item in a queue, or None."""
    from popcorn_core.errors import APIError

    try:
        return client.get(f"/api/appchannels/trace/{queue_id}/current", {})
    except APIError as e:
        if e.status_code == 404:
            return None
        raise


def leave_conversation(client: APIClient, conversation: str) -> dict[str, Any]:
    """Leave a conversation."""
    conv_id = resolve_conversation(client, conversation)
    return client.post("/api/conversations/leave", data={"conversation": conv_id})


def archive_conversation(client: APIClient, conversation: str) -> dict[str, Any]:
    """Archive a conversation."""
    conv_id = resolve_conversation(client, conversation)
    return client.post("/api/conversations/archive", data={"conversation": conv_id})


def unarchive_conversation(client: APIClient, conversation: str) -> dict[str, Any]:
    """Unarchive a conversation."""
    conv_id = resolve_conversation(client, conversation)
    return client.post("/api/conversations/unarchive", data={"conversation": conv_id})


def update_conversation(
    client: APIClient,
    conversation: str,
    name: str = "",
    description: str = "",
    conv_type: str = "",
    site_name: str = "",
) -> dict[str, Any]:
    """Update conversation details."""
    conv_id = resolve_conversation(client, conversation)
    body: dict[str, Any] = {"conversation": conv_id}
    if name:
        body["name"] = name
    if description:
        body["description"] = description
    if conv_type:
        body["conversation_type"] = conv_type
    if site_name:
        body["site_name"] = site_name
    return client.post("/api/conversations/update", data=body)


def invite_to_conversation(
    client: APIClient, conversation: str, user_ids: list[str]
) -> dict[str, Any]:
    """Invite users to a conversation."""
    conv_id = resolve_conversation(client, conversation)
    return client.post(
        "/api/conversations/invite",
        data={"conversation": conv_id, "users": user_ids},
    )


def kick_from_conversation(client: APIClient, conversation: str, user_id: str) -> dict[str, Any]:
    """Remove a user from a conversation."""
    conv_id = resolve_conversation(client, conversation)
    return client.post(
        "/api/conversations/kick",
        data={"conversation": conv_id, "user": user_id},
    )


def delete_conversation(client: APIClient, conversation: str) -> dict[str, Any]:
    """Delete a conversation."""
    conv_id = resolve_conversation(client, conversation)
    return client.post("/api/conversations/delete", data={"conversation": conv_id})


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


def get_inbox(
    client: APIClient, filter_type: str = "all", limit: int = 20, offset: int = 0
) -> dict[str, Any]:
    """Fetch notifications (mentions, replies, reactions)."""
    params: dict[str, Any] = {"limit": limit}
    if offset:
        params["offset"] = offset
    if filter_type == "unread":
        params["is_read"] = "false"
    elif filter_type == "read":
        params["is_read"] = "true"
    return client.get("/api/activities/get", params)


# ---------------------------------------------------------------------------
# File uploads
# ---------------------------------------------------------------------------


def upload_file(client: APIClient, conversation: str, file_path: str) -> dict[str, Any]:
    """Upload a file via presigned URL. Returns a media content part dict."""
    conv_id = resolve_conversation(client, conversation)
    path = Path(file_path)
    if not path.is_file():
        raise PopcornError(f"File not found: {file_path}")

    file_data = path.read_bytes()
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    file_type = "image" if mime_type.startswith("image/") else "document"

    resp = client.post(
        "/api/file-uploads/upload",
        data={
            "conversation_id": conv_id,
            "file_type": file_type,
            "file_name": path.name,
            "file_size": len(file_data),
            "mime_type": mime_type,
        },
    )

    try:
        file_key = resp["file_upload"]["file_key"]
        upload_url = resp["upload_url"]
        upload_fields = {
            k: v for k, v in resp["upload_fields"].items() if not k.startswith("x-amz-meta-")
        }
    except (KeyError, TypeError) as e:
        raise APIError(f"Unexpected response from file upload API (missing {e})") from e

    try:
        s3_resp = httpx.post(
            upload_url,
            data=upload_fields,
            files={"file": (path.name, file_data, mime_type)},
            timeout=120.0,
        )
    except httpx.TimeoutException as e:
        raise APIError(f"File upload timed out for {path.name} ({len(file_data)} bytes)") from e
    except httpx.HTTPError as e:
        raise APIError(f"File upload network error: {e}") from e
    if s3_resp.status_code not in (200, 201, 204):
        raise APIError(f"File upload failed: HTTP {s3_resp.status_code}\n{s3_resp.text[:300]}")

    return {
        "type": "media",
        "mime_type": mime_type,
        "url": file_key,
        "filename": path.name,
        "size_bytes": len(file_data),
        "media_metadata": {},
    }


def download_file(client: APIClient, file_key: str) -> dict[str, Any]:
    """Get a presigned download URL for a file."""
    return client.get("/api/file-uploads/download", {"file_key": file_key})


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------


def create_webhook(
    client: APIClient,
    conversation: str,
    name: str,
    description: str | None = None,
    avatar_url: str | None = None,
    action_mode: str | None = None,
    trigger_flow_id: str | None = None,
    trigger_flow_name: str | None = None,
) -> dict[str, Any]:
    """Create a webhook for a conversation.

    When ``action_mode`` is ``trigger_workflow`` the API needs to know which
    flow to start, named either way:

    - ``trigger_flow_id`` — the flow's UUID primary key.
    - ``trigger_flow_name`` — its name on this conversation.

    Both exist because the two are not interchangeable in practice. The flow
    listing reports a flow's NAME in its ``id`` field, so the identifier the
    CLI hands you for a bundle flow (``alert_webhook``) is not a UUID and the
    id form rejects it. Name-bound creation is also get-or-create per
    (conversation, flow): an already-bound webhook comes back instead of a
    duplicate.

    The API treats the two as mutually exclusive; the parser enforces that
    before the request is built.
    """
    conv_id = resolve_conversation(client, conversation)
    body: dict[str, Any] = {"name": name}
    if description:
        body["description"] = description
    if avatar_url:
        body["avatar_url"] = avatar_url
    if action_mode:
        body["action_mode"] = action_mode
    if trigger_flow_id:
        body["trigger_flow_id"] = trigger_flow_id
    if trigger_flow_name:
        body["trigger_flow_name"] = trigger_flow_name
    return client.post("/api/webhooks/create", data=body, params={"conversation": conv_id})


def webhook_event_types(client: APIClient) -> dict[str, Any]:
    """List valid webhook event sources and action modes."""
    return client.get("/api/webhooks/event-types")


def list_webhooks(client: APIClient, conversation: str) -> dict[str, Any]:
    """List webhooks for a conversation."""
    conv_id = resolve_conversation(client, conversation)
    return client.get("/api/webhooks/list", {"conversation": conv_id})


def get_webhook(client: APIClient, webhook_id: str) -> dict[str, Any]:
    """Get one webhook by UUID.

    Unlike ``list_webhooks`` this needs no conversation: the server authorizes
    against the webhook's own channel, which it looks up from the id.
    """
    return client.get(f"/api/webhooks/{webhook_id}")


def update_webhook(
    client: APIClient,
    webhook_id: str,
    name: str | None = None,
    description: str | None = None,
    avatar_url: str | None = None,
    is_active: bool | None = None,
    enforce_hmac: bool | None = None,
    action_mode: str | None = None,
) -> dict[str, Any]:
    """Update a webhook's settings. Omitted fields are left unchanged.

    The delivery binding is deliberately absent. A webhook's flow is fixed at
    creation and bound by NAME, so there is nothing here to re-point it with:
    the server rejects the id form outright, and the name form is a create-time
    argument. Rebinding means creating a new webhook.

    ``enforce_hmac=True`` is conditional on the server side — a webhook with no
    HMAC secret has enforcement silently written back as False rather than
    erroring, so callers must read the result rather than assume it took.
    """
    body: dict[str, Any] = {}
    if name is not None:
        body["name"] = name
    if description is not None:
        body["description"] = description
    if avatar_url is not None:
        body["avatar_url"] = avatar_url
    if is_active is not None:
        body["is_active"] = is_active
    if enforce_hmac is not None:
        body["enforce_hmac"] = enforce_hmac
    if action_mode is not None:
        body["action_mode"] = action_mode
    return client.patch(f"/api/webhooks/{webhook_id}", data=body)


def delete_webhook(client: APIClient, webhook_id: str) -> dict[str, Any]:
    """Delete a webhook. The server soft-deletes; deliveries stop either way."""
    return client.delete(f"/api/webhooks/{webhook_id}")


def get_webhook_override_rules(client: APIClient, webhook_id: str) -> dict[str, Any]:
    """Get a webhook's per-event override rules."""
    return client.get(f"/api/webhooks/{webhook_id}/override-rules")


def set_webhook_override_rules(
    client: APIClient, webhook_id: str, rules: dict[str, Any]
) -> dict[str, Any]:
    """Replace a webhook's override rules wholesale.

    A PUT, not a merge: rules absent from ``rules`` are dropped. Keys are
    ``event_type.action`` patterns (``*`` wildcards); values patch the
    provider's event config — ``ignore``, ``skip_llm``, ``sentiment``.
    """
    return client.put(f"/api/webhooks/{webhook_id}/override-rules", data={"rules": rules})


def is_webhook_url(target: str) -> bool:
    """True when a `webhook send` target is already an ingest URL."""
    return target.startswith(("http://", "https://"))


def _lookup_webhook(
    client: APIClient,
    target: str,
    conversation: str | None = None,
) -> dict[str, Any]:
    """Find one webhook by UUID or by name.

    A UUID answers on its own through the by-id lookup. A NAME needs
    ``conversation``: names are only unique within a channel, and listing that
    channel's webhooks is the one place a name is matched at all. A UUID given
    *with* a channel takes the listing path too, which is harmless and keeps
    one code path for the "not in this channel" message.
    """
    if _looks_like_uuid(target) and not conversation:
        return (get_webhook(client, target) or {}).get("webhook") or {}
    if not conversation:
        raise PopcornError(
            f"'{target}' is a webhook name, and matching one needs a channel: "
            "names are only unique within a channel. "
            "Pass --channel, or give the webhook's UUID instead.",
            error_code="validation",
            hint="popcorn webhook list '#my-channel'",
        )
    resp = list_webhooks(client, conversation)
    hooks: list[dict[str, Any]] = resp if isinstance(resp, list) else resp.get("webhooks", [])
    wanted = target.lower()
    for hook in hooks:
        if str(hook.get("id", "")) == target or str(hook.get("name", "")).lower() == wanted:
            return hook
    known = ", ".join(str(h.get("name", h.get("id", "?"))) for h in hooks) or "none"
    raise PopcornError(
        f"No webhook '{target}' in {conversation} (has: {known})",
        error_code="not_found",
        hint=f"popcorn webhook list {conversation}",
    )


def resolve_webhook_url(
    client: APIClient,
    target: str,
    conversation: str | None = None,
) -> str:
    """Turn a webhook reference into its ingest URL.

    ``target`` is an ingest URL (returned untouched), a webhook UUID, or a
    webhook name matched case-insensitively.
    """
    if is_webhook_url(target):
        return target
    hook = _lookup_webhook(client, target, conversation)
    url = hook.get("url")
    if not url:
        raise PopcornError(
            f"Webhook '{target}' has no ingest URL to post to",
            error_code="not_found",
        )
    return str(url)


def resolve_webhook_id(
    client: APIClient,
    target: str,
    conversation: str | None = None,
) -> str:
    """Turn a webhook reference into its UUID, which every by-id route needs.

    A UUID is returned without a request. A name costs one listing, which is
    what lets the lifecycle commands be driven by the name `webhook list`
    prints rather than by an id a caller has to carry around.
    """
    if _looks_like_uuid(target):
        return target
    hook = _lookup_webhook(client, target, conversation)
    hook_id = hook.get("id")
    if not hook_id:
        raise PopcornError(
            f"Webhook '{target}' came back without an id",
            error_code="not_found",
        )
    return str(hook_id)


def send_webhook(url: str, payload: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
    """POST a payload to a webhook's ingest URL.

    Deliberately not routed through ``APIClient``: the ingest host is not the
    API host and the endpoint is unauthenticated, so the request carries a
    content type and nothing else. Sending it through the client would attach
    the caller's bearer token to a different host.
    """
    try:
        resp = httpx.post(
            url,
            content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )
    except httpx.TimeoutException as e:
        raise APIError(f"Webhook send timed out for {url}") from e
    except httpx.HTTPError as e:
        raise APIError(f"Webhook send network error: {e}") from e

    if not 200 <= resp.status_code < 300:
        raise APIError(
            f"Webhook send failed: HTTP {resp.status_code}\n{resp.text[:1000]}",
            status_code=resp.status_code,
            body=resp.text,
        )
    try:
        body: Any = resp.json()
    except ValueError:
        body = resp.text
    return {"url": url, "status": resp.status_code, "response": body}


def list_webhook_deliveries(
    client: APIClient,
    conversation: str,
    limit: int = 50,
    since: str | None = None,
    after: str | None = None,
    status: str | None = None,
    include: str | None = None,
) -> dict[str, Any]:
    """List webhook deliveries for a conversation.

    ``include`` is a comma-separated list of optional fields to hydrate on
    each delivery. Currently supported: ``payload_raw``.
    """
    conv_id = resolve_conversation(client, conversation)
    params: dict[str, Any] = {"conversation": conv_id}
    if limit is not None:
        params["limit"] = limit
    if since:
        params["since"] = since
    if after:
        params["after"] = after
    if status:
        params["status"] = status
    if include:
        params["include"] = include
    return client.get("/api/webhooks/deliveries", params)


# ---------------------------------------------------------------------------
# Customer flows (Temporal workflow automations per channel)
# ---------------------------------------------------------------------------


def list_flows(
    client: APIClient,
    conversation: str,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """List the flows defined in a channel."""
    conv_id = resolve_conversation(client, conversation)
    params: dict[str, Any] = {"conversation_id": conv_id, "limit": limit}
    if offset:
        params["offset"] = offset
    return client.get("/api/customer-flows/list", params)


def get_flow(
    client: APIClient, conversation: str, flow_id: str, include_triggers: bool = False
) -> dict[str, Any]:
    """Get a single flow definition by ID.

    `include_triggers` asks the server to add its report of what starts the
    flow on this channel under `triggers`. A server that predates the report
    ignores the parameter and leaves `triggers` absent or null, so the caller
    must treat a missing report as "not checked", never as "nothing runs it".
    """
    conv_id = resolve_conversation(client, conversation)
    params: dict[str, Any] = {"conversation_id": conv_id, "flow_id": flow_id}
    if include_triggers:
        params["include_triggers"] = True
    return client.get("/api/customer-flows/get", params)


def _looks_like_uuid(value: str) -> bool:
    return len(value) == 36 and value.count("-") == 4


def with_conversation_id(inputs: dict[str, Any] | None, conversation_id: str) -> dict[str, Any]:
    """Default `conversation_id` into a run's inputs.

    Practically every flow declares it, and omitting it fails at RUNTIME with
    `ReferenceError: $inputs.conversation_id: key not found` — the run starts,
    reports success, and only then dies. Since the caller already addressed a
    channel, filling it in removes a whole class of confusing failure. An
    explicit value always wins: a flow may target another conversation.
    """
    merged = dict(inputs or {})
    merged.setdefault("conversation_id", conversation_id)
    return merged


def run_flow(
    client: APIClient,
    conversation: str,
    flow_id: str,
    inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Start a flow run, returning its Temporal workflow_id/run_id."""
    conv_id = resolve_conversation(client, conversation)
    inputs = with_conversation_id(inputs, conv_id)
    body: dict[str, Any] = {"conversation_id": conv_id, "flow_id": flow_id}
    if inputs:
        body["inputs"] = inputs
    return client.post(
        "/api/customer-flows/run",
        data=body,
        params={"conversation_id": conv_id},
    )


def list_flow_runs(
    client: APIClient,
    conversation: str,
    status: str | None = None,
    limit: int = 50,
    page_token: str | None = None,
    flow_name: str | None = None,
) -> dict[str, Any]:
    """List Temporal workflow executions (flow runs) for a channel.

    ``status`` is one of ``all | running | failed | closed``. ``page_token``
    is the ``next_page_token`` cursor from a previous response.

    ``flow_name`` narrows the list to one flow's runs. The server applies it
    in the query that selects the page, so a page is still ``limit`` long and
    the cursor stays exact — as long as the same ``flow_name`` rides along on
    every page.

    An API older than the filter ignores the parameter and answers with every
    flow's runs. That response is refused rather than trimmed here: trimming
    would leave a short or empty page with more pages behind it and a
    ``count`` that describes the unfiltered page, and a warning about it would
    go to stderr, which agent mode silences. See ``_check_flow_filter_applied``.
    """
    if flow_name is not None and not flow_name.strip():
        raise PopcornError(
            "--flow needs a flow name",
            error_code="validation",
            hint="popcorn flow runs list --channel <conv> --flow <name>",
        )
    conv_id = resolve_conversation(client, conversation)
    params: dict[str, Any] = {"conversation_id": conv_id, "limit": limit}
    if status:
        params["status"] = status
    if page_token:
        params["page_token"] = page_token
    if flow_name is not None:
        params["flow_name"] = flow_name
    try:
        resp = client.get("/api/customer-flow-runs/list", params)
    except APIError as e:
        # The server's message names the rejected value but not the rule it
        # broke, so say it: the name travels inside a quoted query string.
        if e.status_code == 400 and _api_error_label(e) == "invalid_flow_name":
            e.hint = "a flow name cannot contain a double quote or a backslash"
        raise
    if flow_name is not None:
        _check_flow_filter_applied(resp, flow_name)
    return resp


def _api_error_label(err: APIError) -> str | None:
    """The machine-readable `error` label of a structured API error, if any."""
    try:
        body = json.loads(err.body or "")
    except (json.JSONDecodeError, TypeError):
        return None
    detail = body.get("detail") if isinstance(body, dict) else None
    label = detail.get("error") if isinstance(detail, dict) else None
    return label if isinstance(label, str) else None


# How many foreign flow names the old-server refusal lists before summarising;
# a busy channel's unfiltered page can name many flows.
_MAX_NAMES_SHOWN = 5


def _check_flow_filter_applied(resp: dict[str, Any], flow_name: str) -> None:
    """Refuse a filtered list that came back carrying another flow's runs.

    A server that filters by flow never returns a run of a different flow, or
    one with no flow name at all, so either means the filter was not applied.
    An empty page proves nothing either way, and needs nothing: if every run
    was returned and there were none, there are none of this flow either.
    """
    others = sorted(
        {
            str(e.get("flow_name") or "<none>")
            for e in resp.get("executions") or []
            if e.get("flow_name") != flow_name
        }
    )
    if others:
        shown = ", ".join(others[:_MAX_NAMES_SHOWN])
        if len(others) > _MAX_NAMES_SHOWN:
            shown += f" (+{len(others) - _MAX_NAMES_SHOWN} more)"
        raise PopcornError(
            f"The server ignored --flow {flow_name!r}: its response includes "
            f"runs of {shown}. This API predates the flow filter, "
            "so the list cannot be narrowed to one flow",
            error_code="validation",
            hint="list without --flow and read the flow_name of each run",
        )


def get_flow_run(
    client: APIClient,
    conversation: str,
    workflow_id: str,
    run_id: str | None = None,
    include_errors: bool = False,
) -> dict[str, Any]:
    """Get a single flow run's detail by workflow_id (optionally run_id)."""
    conv_id = resolve_conversation(client, conversation)
    params: dict[str, Any] = {"conversation_id": conv_id, "workflow_id": workflow_id}
    if run_id:
        params["run_id"] = run_id
    if include_errors:
        params["include_errors"] = True
    return client.get("/api/customer-flow-runs/get", params)


def cancel_flow_runs(
    client: APIClient,
    conversation: str,
    workflow_id: str | None = None,
    run_id: str | None = None,
    flow_name: str | None = None,
    force: bool = False,
    reason: str | None = None,
    page_token: str | None = None,
) -> dict[str, Any]:
    """Stop one flow run, or every Running run of one flow, on a channel.

    Exactly one of ``workflow_id`` (one run; ``run_id`` pins a specific run,
    else the latest) or ``flow_name`` (every Running run of that flow — the
    brake for a driver that launched dozens of runs and is itself long
    done). A cancel is cooperative and lands at the run's next activity
    boundary; ``force`` terminates on the spot, for a run that will not
    cancel. The bulk form is one page of 200: ``page_token`` is the
    ``next_page_token`` of the previous response, and it has to be a token
    rather than "ask again" — the runs a page touched are still Running
    until they reach that boundary, so a fresh query would select the same
    page every time.

    The response's ``cancelled`` entries carry an ``action``:
    ``cancel_requested``, ``terminated``, or ``already_closed`` (with the
    status it closed as — not an error, a sweep racing a finishing run
    must be told).
    """
    if bool(workflow_id) == bool(flow_name):
        raise PopcornError(
            "Pass exactly one of a workflow id or --flow",
            error_code="validation",
            hint="popcorn flow runs cancel <workflow_id> --channel <conv>, "
            "or popcorn flow runs cancel --flow <name> --channel <conv>",
        )
    conv_id = resolve_conversation(client, conversation)
    body: dict[str, Any] = {"force": force}
    if workflow_id:
        body["workflow_id"] = workflow_id
        if run_id:
            body["run_id"] = run_id
    else:
        body["flow_name"] = flow_name
        if page_token:
            body["page_token"] = page_token
    if reason:
        body["reason"] = reason
    return client.post(
        "/api/customer-flow-runs/cancel", data=body, params={"conversation_id": conv_id}
    )


# ---------------------------------------------------------------------------
# Scheduled flows (read-only)
# ---------------------------------------------------------------------------


def list_scheduled_flows(client: APIClient, conversation: str) -> dict[str, Any]:
    """List a channel's scheduled flows — the LIVE schedule set.

    This is the authoritative cadence, which a bundle manifest is not: a
    manifest declares what an install creates, while `set_app_mode` and the
    deterministic de-peak offsets both rewrite the installed schedule in
    place. Reading the manifest to answer "how often does this run" is how
    you get an answer that is wrong by a factor of five.
    """
    conv_id = resolve_conversation(client, conversation)
    return client.get("/api/customer-scheduled-flows/list", {"conversation_id": conv_id})


def resolve_schedule_ref(client: APIClient, conversation: str, ref: str) -> str:
    """Resolve a schedule slug or flow id to a full `schedule_id`.

    A `schedule_id` is a 60-odd-character composite
    (`channel:<uuid>:flow:<flow_id>:<slug>`), so requiring one verbatim would
    make `get` unusable without a preceding `list` and a copy-paste. Anything
    already carrying the composite's `:` separator passes through untouched;
    everything else is matched against the channel's schedules by `slug`
    first, then `flow_id`.
    """
    if ":" in ref:
        return ref
    resp = list_scheduled_flows(client, conversation)
    items = resp.get("scheduled_flows") or []
    for key in ("slug", "flow_id"):
        for item in items:
            if item.get(key) == ref:
                schedule_id = item.get("schedule_id")
                if schedule_id:
                    return str(schedule_id)
    known = ", ".join(sorted(filter(None, (i.get("slug") for i in items)))) or "none"
    raise PopcornError(
        f"No schedule '{ref}' in {conversation} (have: {known})",
        error_code="not_found",
    )


def get_scheduled_flow(client: APIClient, conversation: str, schedule_ref: str) -> dict[str, Any]:
    """Get one scheduled flow by `schedule_id`, slug, or flow id."""
    conv_id = resolve_conversation(client, conversation)
    schedule_id = resolve_schedule_ref(client, conversation, schedule_ref)
    return client.get(
        "/api/customer-scheduled-flows/get",
        {"conversation_id": conv_id, "schedule_id": schedule_id},
    )


# Mirrors the server importer's per-entry ceiling so an oversized bundle fails
# locally with a clear message instead of as an opaque 400.
_MAX_TEMPLATE_ENTRY_BYTES = 1024 * 1024


def pack_template_dir(path: str) -> bytes:
    """Zip a template directory the way the importer expects to read it.

    Skips the same cruft the server skips (dotfiles/dotdirs, ``__MACOSX``) and
    enforces the same per-entry ceiling. Requires a manifest: a bundle without
    one installs flows with no tables, schedules or webhooks, which is almost
    never what the author meant.
    """
    import io
    import zipfile

    root = Path(path)
    if not root.is_dir():
        raise PopcornError(f"Not a directory: {path}", error_code="validation")

    entries: list[tuple[Path, str]] = []
    for file in sorted(root.rglob("*")):
        if not file.is_file():
            continue
        rel = file.relative_to(root)
        if any(p.startswith(".") or p == "__MACOSX" for p in rel.parts):
            continue
        size = file.stat().st_size
        if size > _MAX_TEMPLATE_ENTRY_BYTES:
            raise PopcornError(
                f"{rel} is {size} bytes, over the 1 MiB per-file limit",
                error_code="validation",
            )
        entries.append((file, rel.as_posix()))

    if not any(name in ("manifest.yaml", "config.yaml") for _, name in entries):
        raise PopcornError(f"No manifest.yaml in {path}", error_code="validation")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for file, name in entries:
            zf.write(file, name)
    return buf.getvalue()


# Why `flow import` no longer exists. Kept as a module constant so the library
# raise and the CLI subcommand cannot drift apart on the one thing an author
# needs from this error: where installs actually happen now.
TEMPLATE_INSTALL_REMOVED = """\
Installing a bundle from a local directory is no longer supported.

A server-side change removed the zip-install route, so
POST /api/customer-flows/import is 404 on dev and prod. There is no
client-reachable replacement: installable templates are a fixed set published
to the bundle registry server-side.

To install a bundle:
  1. have the bundle published to the registry. That half is internal to the
     Popcorn team, and it happens AFTER the servers that will run the flows
     are updated, because a bundle whose flows call a new activity must not
     become installable before the workers that can run it exist.
  2. install it by creating a channel with that template:
       popcorn channel templates                       # is it published yet?
       popcorn channel create '#chan' --template <name>

Authoring locally still works with no server and no channel:
  popcorn template check <dir>"""


def import_template(
    client: APIClient,
    conversation: str,
    dir_path: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Fenced: raises. There is no endpoint left to install a bundle through.

    Raises BEFORE packing or uploading, which is the point. The old body zipped
    the directory and uploaded it to the target channel, then posted the file
    key to a route that now 404s -- so every attempt left a stray zip in the
    channel and reported nothing an author could act on. Failing here names the
    real publish path instead; see :data:`TEMPLATE_INSTALL_REMOVED`.

    The signature is unchanged so a caller reaches the explanation rather than
    an AttributeError. :func:`pack_template_dir` is deliberately kept: the
    server-side zip parser was retained for a future upload transport, and the
    packing rules are the checked half of that contract.
    """
    raise PopcornError(TEMPLATE_INSTALL_REMOVED, error_code="validation")


def validate_flow_yaml(client: APIClient, conversation: str, yaml_text: str) -> dict[str, Any]:
    """Parse + statically validate one flow YAML. Never persists.

    A semantically invalid flow is a **200** with ``valid: false`` and an
    ``issues`` list — not an error status — so callers must branch on
    ``valid``, not on an exception. Only a malformed request 422s.
    """
    conv_id = resolve_conversation(client, conversation)
    return client.post(
        "/api/customer-flows/validate",
        data={"yaml_text": yaml_text},
        params={"conversation_id": conv_id},
    )


def get_flow_schema(client: APIClient) -> dict[str, Any]:
    """The flow document's rules — what a flow may CONTAIN.

    The complement to the activity catalog, which answers what an activity
    takes and returns. Static and identical for every workspace, so it is
    workspace-member gated with no conversation scope.

    Read by `scripts/sync_flow_rules.py`, not at check time: `template check`
    is offline by contract and reads the generated `popcorn_core.flow_rules`
    snapshot instead, so its findings do not depend on credentials or network.
    """
    return client.get("/api/customer-flows/schema")


def list_activity_catalog(
    client: APIClient,
    conversation: str | None = None,
    *,
    name: str | None = None,
    tier: str | None = None,
    status: str | None = None,
    category: str | None = None,
    view: str | None = None,
) -> dict[str, Any]:
    """The global DSL activity catalog (identical for every workspace).

    Workspace-member gated with no conversation scope — the catalog is derived
    in-process from the activity registries, so `conversation` is accepted and
    ignored for signature symmetry with the other flow operations.

    Filtering is the SERVER's job, not ours. Narrowing
    here would mean shipping a copy of the taxonomy in this package, and
    `category` cannot be validated offline at all — a domain exists exactly
    when an activity is registered under it, so only the server knows the set.
    An unknown value comes back as a 400 naming what would have matched, which
    is why a typo must reach the API rather than quietly matching zero rows.

    `name` is an exact wire name: a 404 if unregistered, and a 404 naming the
    current name if it is a pre-retier alias. `view="summary"` drops the JSON
    Schemas and keeps one line of each description — the whole catalog is
    ~500 KB, and a browse does not need the schemas.

    Omitted params are not sent, so against a server predating server-side
    filtering this call is byte-identical to what it was.
    """
    params = {
        k: v
        for k, v in (
            ("name", name),
            ("tier", tier),
            ("status", status),
            ("category", category),
            ("view", view),
        )
        if v is not None
    }
    return client.get("/api/customer-flows/activity-catalog", params=params)


def list_channel_templates(client: APIClient) -> dict[str, Any]:
    """List the channel templates available in the workspace."""
    return client.get("/api/conversations/templates")


# ---------------------------------------------------------------------------
# Integrations
# ---------------------------------------------------------------------------


def check_access(client: APIClient, repo: str) -> dict[str, Any]:
    """Check if the user's integration can access a repository."""
    parts = repo.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise PopcornError(
            f"Invalid repo format: {repo!r}. Expected owner/repo (e.g. acme/widgets)"
        )
    owner, name = parts
    return client.post(
        "/api/integrations/check-access",
        data={"provider": "github", "owner": owner, "repo": name},
    )


# ---------------------------------------------------------------------------
# Deploy
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Site status
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Raw API access
# ---------------------------------------------------------------------------


def raw_api_call(
    client: APIClient,
    method: str,
    path: str,
    data: dict[str, Any] | None = None,
    params: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Direct API call — used by `popcorn api` escape hatch."""
    # Parse query string embedded in path (e.g. /api/foo?bar=baz)
    parsed = urlparse(path)
    if parsed.query:
        embedded = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
        params = {**embedded, **(params or {})}
        path = parsed.path
    return client.request(method, path, params=params, data=data)


# ---------------------------------------------------------------------------
# Agent-store data-store (tables, records, scalars, audit)
#
# The user-JWT surface at /api/v1/conversations/{conversation_id}/data-store/…
# The conversation is a *path* segment here, not a query param, so every
# operation resolves the channel ref up front and bakes it into the path.
# ---------------------------------------------------------------------------


def _store_base(client: APIClient, conversation: str) -> str:
    conv_id = resolve_conversation(client, conversation)
    return f"/api/v1/conversations/{conv_id}/data-store"


def list_tables(client: APIClient, conversation: str) -> dict[str, Any]:
    """List the data-store tables in a channel (`tables`: name, record_count)."""
    return client.get(f"{_store_base(client, conversation)}/tables")


def get_table(client: APIClient, conversation: str, name: str) -> dict[str, Any]:
    """Get one table (`table.schema_version.schema_def.columns` holds the schema)."""
    return client.get(f"{_store_base(client, conversation)}/tables/{name}")


def list_records(
    client: APIClient,
    conversation: str,
    name: str,
    filter: dict[str, Any] | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> dict[str, Any]:
    """List rows (`records`). `filter` is sent JSON-encoded, as the API expects."""
    params: dict[str, Any] = {"limit": limit}
    if filter:
        params["filter"] = json.dumps(filter)
    if cursor:
        params["cursor"] = cursor
    return client.get(f"{_store_base(client, conversation)}/tables/{name}/records", params)


def get_record(
    client: APIClient, conversation: str, name: str, record_id: int | str
) -> dict[str, Any]:
    """Get one row by record id."""
    return client.get(f"{_store_base(client, conversation)}/tables/{name}/records/{record_id}")


def patch_record(
    client: APIClient,
    conversation: str,
    name: str,
    record_id: int | str,
    data: dict[str, Any],
) -> dict[str, Any]:
    """Patch one row's columns. Columns go inside a `data` envelope."""
    return client.patch(
        f"{_store_base(client, conversation)}/tables/{name}/records/{record_id}",
        data={"data": data},
    )


def delete_record(
    client: APIClient, conversation: str, name: str, record_id: int | str
) -> dict[str, Any]:
    """Delete one row. The API answers 204, so this returns an empty dict."""
    return client.delete(f"{_store_base(client, conversation)}/tables/{name}/records/{record_id}")


def list_scalars(
    client: APIClient, conversation: str, limit: int = 50, cursor: str | None = None
) -> dict[str, Any]:
    """List the channel's data-store scalars (`scalars`: key, value, timestamps)."""
    params: dict[str, Any] = {"limit": limit}
    if cursor:
        params["cursor"] = cursor
    return client.get(f"{_store_base(client, conversation)}/scalars", params)


def get_scalar(client: APIClient, conversation: str, key: str) -> dict[str, Any]:
    """Read one scalar (`scalar.value`)."""
    return client.get(f"{_store_base(client, conversation)}/scalars/{key}")


def set_scalar(client: APIClient, conversation: str, key: str, value: str) -> dict[str, Any]:
    """Write one scalar. Scalars are strings on the wire."""
    return client.put(
        f"{_store_base(client, conversation)}/scalars/{key}",
        data={"value": value},
    )


def list_store_audit(
    client: APIClient,
    conversation: str,
    limit: int = 50,
    cursor: str | None = None,
    *,
    entity_type: str | None = None,
    entity_id: str | None = None,
    since: str | None = None,
) -> dict[str, Any]:
    """Recent data-store audit entries (`events`: operation, entity, changed_at).

    The two questions an audit trail is opened for — what happened to this row,
    and what changed since some moment — are both server-side filters, so they
    go on the wire rather than narrowing the page here: filtering a page that
    was already truncated by `limit` would answer from whatever happened to be
    in it and silently miss older matches.

    `entity_type` and `entity_id` are exact matches (the pair together is one
    entity's history; `entity_type` alone is one class of them). `since` is an
    ISO 8601 datetime and is inclusive — events at or after it, still ordered
    newest-first. The server owns both taxonomies, so an unknown entity_type
    matches nothing there rather than failing offline here, and a malformed
    `since` comes back as a 400 naming the parse error.

    Omitted filters are not sent, so a call that passes none is byte-identical
    to what it was before they existed.
    """
    params: dict[str, Any] = {"limit": limit}
    for key, value in (
        ("entity_type", entity_type),
        ("entity_id", entity_id),
        ("since", since),
    ):
        if value is not None:
            params[key] = value
    if cursor:
        params["cursor"] = cursor
    return client.get(f"{_store_base(client, conversation)}/audit", params)


# ---------------------------------------------------------------------------
# App bundles (read)
# ---------------------------------------------------------------------------
#
# The user-JWT mirror of the agent surface's /apps reads. `conversation_id`
# is required on every one of them and is what
# authorizes the call — the human surface never reads
# X-Active-Conversation-ID — so these look like every other channel-scoped
# operation here and need nothing special from APIClient.


def list_channel_apps(client: APIClient, conversation: str) -> dict[str, Any]:
    """Each app's lineage heads, plus this channel's current binding.

    One "product" entry per app and one "fork" entry per fork line the
    workspace owns. `channel` is null when the channel runs no bundle.
    """
    conv_id = resolve_conversation(client, conversation)
    return client.get("/api/apps/list", {"conversation_id": conv_id})


def get_channel_app_tree(
    client: APIClient, conversation: str, ref: str = "bound"
) -> dict[str, Any]:
    """Every file path in the selected version (`paths`).

    A current server also sends `sha256` — `{path: hash of the file's raw
    bytes}` — which is what lets `app publish` and `app status` diff a working
    copy without downloading the tree; an older one omits it.

    `ref` picks the version the same way `get_channel_app_files` does, and the
    response carries both sides of it: `version_id`/`semver` for the version
    served, `bound_version_id`/`bound_semver` for what the channel runs. That
    pair is the cheapest read that answers "has the publish landed here?" —
    the files endpoint answers it too, but ships every file's content to do so.

    Defaults to "bound" rather than "head" so an existing caller keeps the
    version it already got; the files reader defaults the other way because
    its caller is a checkout, which must be based on the head.
    """
    conv_id = resolve_conversation(client, conversation)
    return client.get("/api/apps/tree", {"conversation_id": conv_id, "ref": ref})


def get_channel_app_file(client: APIClient, conversation: str, path: str) -> dict[str, Any]:
    """One file's text from the channel's bound version."""
    conv_id = resolve_conversation(client, conversation)
    return client.get("/api/apps/file", {"conversation_id": conv_id, "path": path})


def get_channel_app_files(
    client: APIClient,
    conversation: str,
    ref: str = "head",
    version_id: int | None = None,
) -> dict[str, Any]:
    """One version's complete tree in one round trip.

    The checkout read. Bundle trees are tens of files and tens of KB, so this
    is one request rather than a tree listing plus N file reads.

    `ref` picks the version: "head" is the latest on the channel's fork line
    and is what a publish must be based on; "bound" is what the channel runs.
    The two differ only while the channel lags its line — a head whose install
    has not landed, or failed — which is exactly when a checkout of the bound
    tree would produce an edit no publish can accept.
    The response carries both: `version_id`/`semver` for the served version
    and `bound_version_id`/`bound_semver` for the channel's own.

    `version_id` reads one specific version of the channel's own line instead,
    and replaces `ref` rather than accompanying it. A server that predates the
    parameter ignores it and answers with the bound tree, which would be
    written to disk under the requested version's name — so the response is
    checked here, at the read, and refused unless it says it IS that version
    (`require_version_served`). Every caller gets the check by asking.
    """
    conv_id = resolve_conversation(client, conversation)
    if version_id is None:
        return client.get("/api/apps/files", {"conversation_id": conv_id, "ref": ref})
    resp = client.get("/api/apps/files", {"conversation_id": conv_id, "version_id": version_id})
    require_version_served(resp, version_id)
    return resp


def require_version_served(resp: dict[str, Any], version_id: int) -> None:
    """Refuse a response that is not the version that was asked for.

    Both fields, because either alone can be satisfied by accident: a server
    that ignores `version_id` still reports a `version_id` (the bound one,
    which may happen to equal the request), and `ref` alone says a version
    was served without saying which.
    """
    served_ref = resp.get("ref")
    served_id = resp.get("version_id")
    if served_ref == "version" and served_id == version_id and not isinstance(served_id, bool):
        return
    raise PopcornError(
        f"this server does not support reading a specific version (asked for "
        f"version {version_id}, it answered with ref={served_ref!r}, "
        f"version {served_id}) — nothing was written",
        error_code="validation",
        hint="the server predates --version; check out without it to read what "
        "the channel runs, or retry once the server is upgraded",
    )


# ---------------------------------------------------------------------------
# App bundles (write)
# ---------------------------------------------------------------------------
#
# The user-JWT mirror of the agent surface's writes.
# Same `conversation_id`-authorizes-the-call shape as the reads, so these are
# three-liners too. One asymmetry worth knowing at the call site: `publish` is
# workspace-ADMIN only while fork and apply also accept a channel member, so a
# member gets a 403 on publish alone.


def fork_channel_app(
    client: APIClient, conversation: str, fork_name: str | None = None
) -> dict[str, Any]:
    """Give this workspace its own fork line of the channel's app.

    `status` is "created" (line minted, channel re-bound), "already_fork" (a
    no-op) or "adopting" (the workspace's existing line is being applied by
    the install workflow, asynchronously).
    """
    conv_id = resolve_conversation(client, conversation)
    body = {"fork_name": fork_name} if fork_name else {}
    return client.post("/api/apps/fork", body, {"conversation_id": conv_id})


def publish_channel_app(
    client: APIClient, conversation: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Publish edits as the next version on the checkout's fork line.

    `payload` is `{base_version_id, files, deletes, changelog?}` — see
    `app_publish.publish_payload`. The publish is a LINE operation: the server
    checks `base_version_id` against the line's head and nothing about what
    the channel runs. `conversation_id` only names the
    channel to install on right away; other channels on the line catch up on
    their own auto-update tick. `install_status` in the response says how that
    install went: "started", "blocked_install_in_progress",
    "blocked_app_updates_locked", or "not_requested".
    """
    conv_id = resolve_conversation(client, conversation)
    return client.post("/api/apps/publish", payload, {"conversation_id": conv_id})


def apply_channel_app(client: APIClient, conversation: str) -> dict[str, Any]:
    """Bring this channel up to its lineage head. Publishes nothing.

    `status` is "started", "already_current", or
    "blocked_install_in_progress" — the last is another install holding the
    channel's lock, and the retry is this same command.
    """
    conv_id = resolve_conversation(client, conversation)
    return client.post("/api/apps/apply", {}, {"conversation_id": conv_id})


# ---------------------------------------------------------------------------
# Channel config
# ---------------------------------------------------------------------------
#
# The one shape to keep in mind: `PUT .../parameters` REPLACES the whole
# `channel_parameters` section, and `PATCH .../parameters` edits individual
# keys. A per-key edit must go through the PATCH — built on the PUT, it is a
# client-side read-modify-write, and two concurrent edits each write back the
# same snapshot and the later one drops the earlier one's keys.


def inspect_channel_config(client: APIClient, conversation: str) -> dict[str, Any]:
    """The channel's config, its flows' `$channel.*` usage, and the diff.

    `comparison` is computed server-side; the CLI renders it and must never
    recompute it.
    """
    conv_id = resolve_conversation(client, conversation)
    return client.get("/api/customer-flows/channel-config", {"conversation_id": conv_id})


def replace_channel_parameters(
    client: APIClient, conversation: str, parameters: dict[str, Any]
) -> dict[str, Any]:
    """Replace the ENTIRE `channel_parameters` section.

    Named `replace_` rather than `update_` because that is what it does: any
    key absent from `parameters` is gone after this call.
    """
    conv_id = resolve_conversation(client, conversation)
    return client.put(
        "/api/customer-flows/channel-config/parameters",
        {"parameters": parameters},
        {"conversation_id": conv_id},
    )


def patch_channel_parameters(
    client: APIClient,
    conversation: str,
    set_: dict[str, Any] | None = None,
    unset: list[str] | None = None,
) -> dict[str, Any]:
    """Set and unset individual `channel_parameters` keys, keeping the rest.

    The server merges the edit into the stored section under a row lock, so
    no read is needed first. `unset` of a key that is not set is not an
    error — the response reports the section as written, not what changed.

    No `If-Match`: that header is for a caller whose patch was computed from
    a read. `params set tone=crisp` is self-contained, and the server-side
    merge already protects every key the patch does not name.
    """
    conv_id = resolve_conversation(client, conversation)
    return client.patch(
        "/api/customer-flows/channel-config/parameters",
        {"set": set_ or {}, "unset": unset or []},
        {"conversation_id": conv_id},
    )


def set_channel_integration(
    client: APIClient, conversation: str, name: str, integration_id: str
) -> dict[str, Any]:
    """Bind `$channel.integrations.<name>` to one of the CALLER's accounts.

    A foreign or unknown `integration_id` is the same 404 — existence is
    deliberately undisclosed. A 409 means the channel's flows declare a
    `provider:` this account does not match.
    """
    conv_id = resolve_conversation(client, conversation)
    return client.post(
        "/api/customer-flows/channel-config/integrations/set",
        {"name": name, "integration_id": integration_id},
        {"conversation_id": conv_id},
    )


def unset_channel_integration(client: APIClient, conversation: str, name: str) -> dict[str, Any]:
    """Remove a named integration binding. Leaves the OAuth grant in place."""
    conv_id = resolve_conversation(client, conversation)
    return client.post(
        "/api/customer-flows/channel-config/integrations/unset",
        {"name": name},
        {"conversation_id": conv_id},
    )


def list_own_integrations(client: APIClient) -> dict[str, Any]:
    """The calling user's connected accounts — the ids `set` needs.

    Not workspace-wide and not channel-scoped: the set endpoint requires the
    caller's OWN account, so this is the only list that can feed it.
    """
    return client.get("/api/integrations/list")
