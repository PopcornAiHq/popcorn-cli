"""Business operations for Popcorn messaging.

Every function takes an APIClient + plain parameters and returns raw data dicts.
No I/O, no formatting, no argparse — just business logic.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

import httpx

from .errors import APIError, PopcornError
from .resolve import resolve_conversation

if TYPE_CHECKING:
    from .client import APIClient


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def get_whoami(client: APIClient) -> dict[str, Any]:
    """Get current user and workspace info."""
    return client.get("/api/users/me")


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


def search_channels(client: APIClient, query: str = "") -> dict[str, Any]:
    """Search channels, optionally filtering by name."""
    resp = client.get("/api/conversations/list", {"types": _CHANNEL_TYPES, "limit": 1000})
    convs = resp.get("conversations", [])
    if query:
        q = query.lower()
        convs = [c for c in convs if q in (c.get("name") or "").lower()]
    return {"conversations": convs}


def search_dms(client: APIClient, query: str = "") -> dict[str, Any]:
    """Search DMs, optionally filtering by participant name."""
    resp = client.get("/api/conversations/list", {"types": "dm,group_dm", "limit": 1000})
    convs = resp.get("conversations", [])
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
    resp = client.get("/api/users/list", {"limit": 1000})
    users = resp.get("users", [])
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


def search_messages(client: APIClient, query: str) -> dict[str, Any]:
    """Full-text search across messages."""
    if not query:
        raise PopcornError(
            "Query required for message search. Usage: popcorn search messages <query>"
        )
    return client.get("/api/search/", {"query": query, "limit": 50})


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def read_messages(
    client: APIClient,
    conversation: str,
    thread_id: str = "",
    limit: int = 25,
) -> dict[str, Any]:
    """Read message history from a channel, DM, or thread."""
    conv_id = resolve_conversation(client, conversation)
    if thread_id:
        return client.get(
            "/api/messages/thread",
            {"thread_ts": thread_id, "limit": limit, "conversation_id": conv_id},
        )
    return client.get(
        "/api/messages/history",
        {"limit": limit, "conversation_id": conv_id},
    )


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
    info = client.get("/api/conversations/info", {"conversation_id": conv_id})
    members = client.get("/api/conversations/members", {"conversation_id": conv_id})
    return {
        "conversation": info.get("conversation", {}),
        "members": members.get("members", []),
    }


def create_conversation(
    client: APIClient,
    name: str,
    conv_type: str = "public_channel",
    description: str = "",
    members: list[str] | None = None,
) -> dict[str, Any]:
    """Create a new conversation (channel or DM)."""
    body: dict[str, Any] = {"name": name, "type": conv_type}
    if description:
        body["description"] = description
    if members:
        body["members"] = members
    return client.post("/api/conversations/create", data=body)


def join_conversation(client: APIClient, conversation: str) -> dict[str, Any]:
    """Join a conversation."""
    conv_id = resolve_conversation(client, conversation)
    return client.post("/api/conversations/join", data={"conversation_id": conv_id})


def leave_conversation(client: APIClient, conversation: str) -> dict[str, Any]:
    """Leave a conversation."""
    conv_id = resolve_conversation(client, conversation)
    return client.post("/api/conversations/leave", data={"conversation_id": conv_id})


def archive_conversation(client: APIClient, conversation: str) -> dict[str, Any]:
    """Archive a conversation."""
    conv_id = resolve_conversation(client, conversation)
    return client.post("/api/conversations/archive", data={"conversation_id": conv_id})


def unarchive_conversation(client: APIClient, conversation: str) -> dict[str, Any]:
    """Unarchive a conversation."""
    conv_id = resolve_conversation(client, conversation)
    return client.post("/api/conversations/unarchive", data={"conversation_id": conv_id})


def update_conversation(
    client: APIClient,
    conversation: str,
    name: str = "",
    description: str = "",
    conv_type: str = "",
) -> dict[str, Any]:
    """Update conversation details."""
    conv_id = resolve_conversation(client, conversation)
    body: dict[str, Any] = {"conversation_id": conv_id}
    if name:
        body["name"] = name
    if description:
        body["description"] = description
    if conv_type:
        body["type"] = conv_type
    return client.post("/api/conversations/update", data=body)


def invite_to_conversation(
    client: APIClient, conversation: str, user_ids: list[str]
) -> dict[str, Any]:
    """Invite users to a conversation."""
    conv_id = resolve_conversation(client, conversation)
    return client.post(
        "/api/conversations/invite",
        data={"conversation_id": conv_id, "user_ids": user_ids},
    )


def kick_from_conversation(client: APIClient, conversation: str, user_id: str) -> dict[str, Any]:
    """Remove a user from a conversation."""
    conv_id = resolve_conversation(client, conversation)
    return client.post(
        "/api/conversations/kick",
        data={"conversation_id": conv_id, "user_id": user_id},
    )


def delete_conversation(client: APIClient, conversation: str) -> dict[str, Any]:
    """Delete a conversation."""
    conv_id = resolve_conversation(client, conversation)
    return client.post("/api/conversations/delete", data={"conversation_id": conv_id})


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


def get_inbox(client: APIClient, filter_type: str = "all", limit: int = 20) -> dict[str, Any]:
    """Fetch notifications (mentions, replies, reactions)."""
    params: dict[str, Any] = {"limit": limit}
    if filter_type == "unread":
        params["is_read"] = "false"
    elif filter_type == "read":
        params["is_read"] = "true"
    return client.get("/api/activities/get", params)


def get_unread_count(client: APIClient) -> dict[str, Any]:
    """Get unread notification count."""
    return client.get("/api/activities/get-unread-count")


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


def list_conversation_files(client: APIClient, conversation: str) -> dict[str, Any]:
    """List files in a conversation."""
    conv_id = resolve_conversation(client, conversation)
    return client.get("/api/file-uploads/list-conversation-files", {"conversation_id": conv_id})


def download_file(client: APIClient, file_key: str) -> dict[str, Any]:
    """Get a presigned download URL for a file."""
    return client.get("/api/file-uploads/download", {"file_key": file_key})


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


def get_user(client: APIClient, user_id: str) -> dict[str, Any]:
    """Get info about a specific user."""
    return client.get(f"/api/users/{user_id}")


def update_profile(client: APIClient, **fields: Any) -> dict[str, Any]:
    """Update current user's profile (display_name, etc.)."""
    return client.patch("/api/users/profile", data=fields)


def update_status(client: APIClient, status: str, emoji: str = "") -> dict[str, Any]:
    """Update current user's status."""
    body: dict[str, Any] = {"status": status}
    if emoji:
        body["emoji"] = emoji
    return client.patch("/api/users/status", data=body)


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------


def get_sidebar(client: APIClient) -> dict[str, Any]:
    """Get sidebar with categorized conversations."""
    return client.post("/api/sidebar/get")


def get_sidebar_list(client: APIClient) -> dict[str, Any]:
    """Get flat list of conversations in sidebar."""
    return client.get("/api/sidebar/get-sidebar-list")


def sidebar_add_conversation(
    client: APIClient, conversation: str, category: str = ""
) -> dict[str, Any]:
    """Add a conversation to the sidebar."""
    conv_id = resolve_conversation(client, conversation)
    body: dict[str, Any] = {"conversation_id": conv_id}
    if category:
        body["category"] = category
    return client.post("/api/sidebar/add-conversation", data=body)


def sidebar_remove_conversation(client: APIClient, conversation: str) -> dict[str, Any]:
    """Remove a conversation from the sidebar."""
    conv_id = resolve_conversation(client, conversation)
    return client.post("/api/sidebar/remove-conversation", data={"conversation_id": conv_id})


def sidebar_create_category(client: APIClient, name: str) -> dict[str, Any]:
    """Create a new sidebar category."""
    return client.post("/api/sidebar/create-category", data={"name": name})


def sidebar_delete_category(client: APIClient, category_id: str) -> dict[str, Any]:
    """Delete a sidebar category."""
    return client.post("/api/sidebar/delete-category", data={"category_id": category_id})


def sidebar_rename_category(client: APIClient, category_id: str, name: str) -> dict[str, Any]:
    """Rename a sidebar category."""
    return client.post(
        "/api/sidebar/rename-category",
        data={"category_id": category_id, "name": name},
    )


def sidebar_reorder_categories(client: APIClient, category_ids: list[str]) -> dict[str, Any]:
    """Reorder sidebar categories."""
    return client.post("/api/sidebar/reorder-categories", data={"category_ids": category_ids})


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------


def create_webhook(
    client: APIClient, conversation: str, url: str, events: list[str] | None = None
) -> dict[str, Any]:
    """Create a webhook for a conversation."""
    conv_id = resolve_conversation(client, conversation)
    body: dict[str, Any] = {"conversation_id": conv_id, "url": url}
    if events:
        body["events"] = events
    return client.post("/api/webhooks/create", data=body)


def list_webhooks(client: APIClient, conversation: str) -> dict[str, Any]:
    """List webhooks for a conversation."""
    conv_id = resolve_conversation(client, conversation)
    return client.get("/api/webhooks/list", {"conversation_id": conv_id})


def list_webhook_deliveries(client: APIClient, webhook_id: str) -> dict[str, Any]:
    """List deliveries for a webhook."""
    return client.get("/api/webhooks/deliveries", {"webhook_id": webhook_id})


# ---------------------------------------------------------------------------
# Prototypes
# ---------------------------------------------------------------------------


def get_prototype(
    client: APIClient, workspace_id: str, prototype_id: str, path: str = ""
) -> dict[str, Any]:
    """Proxy a request to a prototype."""
    route = f"/prototype/{workspace_id}/{prototype_id}"
    if path:
        route = f"{route}/{path.lstrip('/')}"
    return client.get(route)


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
