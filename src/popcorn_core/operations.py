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


def search_messages(
    client: APIClient, query: str, limit: int = 50, offset: int = 0
) -> dict[str, Any]:
    """Full-text search across messages."""
    if not query:
        raise PopcornError(
            "Query required for message search. Usage: popcorn search messages <query>"
        )
    params: dict[str, Any] = {"query": query, "limit": limit}
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
) -> dict[str, Any]:
    """Create a new conversation (channel or DM)."""
    body: dict[str, Any] = {"name": name, "conversation_type": conv_type}
    if member_ids:
        body["member_ids"] = member_ids
    return client.post("/api/conversations/create", data=body)


def join_conversation(client: APIClient, conversation: str) -> dict[str, Any]:
    """Join a conversation."""
    conv_id = resolve_conversation(client, conversation)
    return client.post("/api/conversations/join", data={"conversation": conv_id})


# ---------------------------------------------------------------------------
# VM (workspace VM agent execution)
# ---------------------------------------------------------------------------


def vm_monitor(client: APIClient) -> dict[str, Any]:
    """Fetch active workers and queue items from workspace VM."""
    return client.get("/api/appchannels/monitor", {})


def vm_usage(
    client: APIClient,
    hours: float | None = None,
    days: int | None = None,
    queue: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Fetch token/cost usage analytics from workspace VM."""
    params: dict[str, Any] = {}
    if hours is not None:
        params["hours"] = hours
    if days is not None:
        params["days"] = days
    if queue:
        params["queue"] = queue
    if limit is not None:
        params["limit"] = limit
    return client.get("/api/appchannels/usage", params)


def vm_trace_list(client: APIClient, queue_id: str, limit: int = 10) -> dict[str, Any]:
    """List recent work items for a queue (from usage endpoint)."""
    return client.get(
        "/api/appchannels/usage",
        {"queue": queue_id, "limit": limit},
    )


def _normalize_item_id(item_id: str) -> str:
    """Strip queue prefix from item_id (e.g. 'project-foo/slug' → 'slug')."""
    return item_id.split("/")[-1] if "/" in item_id else item_id


def vm_trace(client: APIClient, queue_id: str, item_id: str) -> dict[str, Any]:
    """Fetch full execution trace for a work item."""
    return client.get(f"/api/appchannels/trace/{queue_id}/{_normalize_item_id(item_id)}", {})


def vm_trace_current(client: APIClient, queue_id: str) -> dict[str, Any] | None:
    """Fetch the trace for the currently active item in a queue, or None."""
    from popcorn_core.errors import APIError

    try:
        return client.get(f"/api/appchannels/trace/{queue_id}/current", {})
    except APIError as e:
        if e.status_code == 404:
            return None
        raise


def vm_trace_latest(
    client: APIClient,
    queue_id: str,
    status: str | None = None,
) -> dict[str, Any] | None:
    """Fetch the latest trace for a queue, optionally filtered by status."""
    usage = client.get(
        "/api/appchannels/usage",
        {"queue": queue_id, "limit": 20},
    )
    items = usage.get("recent_items", [])
    if status:
        items = [i for i in items if i.get("status") == status]
    if not items:
        return None
    latest = items[0]
    item_id = latest["item_id"]
    return client.get(f"/api/appchannels/trace/{queue_id}/{_normalize_item_id(item_id)}", {})


def vm_cancel(client: APIClient, queue_id: str, item_id: str) -> dict[str, Any]:
    """Cancel a specific work item."""
    return client.post(
        f"/api/appchannels/queues/{queue_id}/items/{_normalize_item_id(item_id)}/cancel"
    )


def vm_cancel_current(client: APIClient, queue_id: str) -> dict[str, Any] | None:
    """Cancel the currently processing item in a queue.

    Returns the cancel response, or None if no processing item found.
    """
    monitor = client.get("/api/appchannels/monitor", {})
    items = monitor.get("items", [])
    processing = [
        i for i in items if i.get("queue_id") == queue_id and i.get("status") == "processing"
    ]
    if not processing:
        return None
    item_id = processing[0]["item_id"]
    return client.post(
        f"/api/appchannels/queues/{queue_id}/items/{_normalize_item_id(item_id)}/cancel"
    )


def vm_rollback(
    client: APIClient,
    site_name: str,
    version: int | None = None,
) -> dict[str, Any]:
    """Roll back a site to a previous version."""
    data: dict[str, Any] = {}
    if version is not None:
        data["version"] = version
    return client.post(
        f"/api/appchannels/sites/{site_name}/rollback",
        data=data,
    )


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
) -> dict[str, Any]:
    """Create a webhook for a conversation."""
    conv_id = resolve_conversation(client, conversation)
    body: dict[str, Any] = {"name": name}
    if description:
        body["description"] = description
    if avatar_url:
        body["avatar_url"] = avatar_url
    if action_mode:
        body["action_mode"] = action_mode
    return client.post("/api/webhooks/create", data=body, params={"conversation": conv_id})


def list_webhooks(client: APIClient, conversation: str) -> dict[str, Any]:
    """List webhooks for a conversation."""
    conv_id = resolve_conversation(client, conversation)
    return client.get("/api/webhooks/list", {"conversation": conv_id})


def list_webhook_deliveries(
    client: APIClient,
    conversation: str,
    limit: int = 50,
    since: str | None = None,
    after: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """List webhook deliveries for a conversation."""
    conv_id = resolve_conversation(client, conversation)
    params: dict[str, Any] = {"conversation": conv_id, "limit": limit}
    if since:
        params["since"] = since
    if after:
        params["after"] = after
    if status:
        params["status"] = status
    return client.get("/api/webhooks/deliveries", params)


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


def deploy_create(client: APIClient, site_name: str) -> dict[str, Any]:
    """Create a channel with a provisioned site."""
    return client.post(
        "/api/conversations/create",
        data={
            "name": site_name,
            "conversation_type": "workspace_channel",
            "site_name": site_name,
        },
    )


def deploy_presign(client: APIClient, conversation_id: str) -> dict[str, Any]:
    """Get a presigned S3 upload URL for the conversation's site."""
    return client.post(
        "/api/conversations/presigned-url",
        data={"conversation_id": conversation_id, "method": "PUT"},
    )


def deploy_publish(
    client: APIClient,
    conversation_id: str,
    s3_key: str,
    context: str = "",
    force: bool = False,
    verify: bool = False,
) -> dict[str, Any]:
    """Publish a tarball from S3 to the conversation's site."""
    data: dict[str, Any] = {"conversation_id": conversation_id, "s3_key": s3_key}
    if context:
        data["context"] = context
    if force:
        data["force"] = True
    if verify:
        data["verify"] = True
    return client.post("/api/conversations/publish", data=data)


def deploy_upload(
    upload_url: str,
    upload_fields: dict[str, str],
    tarball_path: str,
) -> None:
    """Upload a tarball to a presigned S3 URL."""
    path = Path(tarball_path)
    if not path.is_file():
        raise PopcornError(f"Tarball not found: {tarball_path}")
    file_data = path.read_bytes()
    try:
        resp = httpx.post(
            upload_url,
            data=upload_fields,
            files={"file": ("push.tar.gz", file_data, "application/gzip")},
            timeout=120.0,
        )
    except httpx.TimeoutException as e:
        raise APIError(f"Deploy upload timed out ({len(file_data)} bytes)") from e
    except httpx.HTTPError as e:
        raise APIError(f"Deploy upload network error: {e}") from e
    if resp.status_code not in (200, 201, 204):
        raise APIError(f"Deploy upload failed: HTTP {resp.status_code}\n{resp.text[:300]}")


# ---------------------------------------------------------------------------
# Site status
# ---------------------------------------------------------------------------


def deploy_verify_status(
    client: APIClient, conversation_id: str, task_id: str, site_name: str
) -> dict[str, Any]:
    """Poll the verify task status after a publish with verify=true."""
    return client.get(
        "/api/conversations/verify-status",
        {"task_id": task_id, "site_name": site_name, "conversation": conversation_id},
    )


def site_url_from_subdomain(subdomain: str, api_url: str) -> str:
    """Construct a public site URL from a subdomain and the current API URL.

    Dev environments (api hostname contains '.dev.') use .dev.popcorn.ing,
    production uses .popcorn.ing.
    """
    host = urlparse(api_url).hostname or ""
    if host.startswith("dev.") or ".dev." in host:
        return f"https://{subdomain}.dev.popcorn.ing"
    return f"https://{subdomain}.popcorn.ing"


def site_url_from_metadata(metadata: dict[str, Any], api_url: str) -> str | None:
    """Extract subdomain from conversation metadata and build the site URL."""
    subdomain = metadata.get("subdomain")
    if subdomain:
        return site_url_from_subdomain(subdomain, api_url)
    return None


def get_site_url(client: APIClient, conversation_id: str) -> str | None:
    """Derive the public site URL from conversation metadata, if available."""
    try:
        info = client.get("/api/conversations/info", {"conversation": conversation_id})
        metadata = info.get("conversation", {}).get("metadata", {})
        return site_url_from_metadata(metadata, client.profile.api_url)
    except (APIError, PopcornError):
        pass
    return None


def export_site(
    client: APIClient,
    conversation_id: str,
    version: str | None = None,
) -> dict[str, Any]:
    """Export site code from VM as a downloadable tarball.

    Returns dict with download_url, s3_key, version, commit_hash.
    """
    data: dict[str, Any] = {}
    if version is not None:
        data["version"] = version
    return client.post(
        f"/api/conversations/{conversation_id}/site/export",
        data=data,
    )


def get_site_status(client: APIClient, conversation_id: str) -> dict[str, Any]:
    """Get site deployment status, falling back to conversation info."""
    try:
        return client.get(f"/api/conversations/{conversation_id}/site/status")
    except APIError as e:
        if e.status_code == 404:
            info = client.get("/api/conversations/info", {"conversation": conversation_id})
            return {"conversation": info.get("conversation", {}), "fallback": True}
        raise


def get_site_log(client: APIClient, conversation_id: str, limit: int = 10) -> dict[str, Any]:
    """Get site version history."""
    try:
        return client.get(f"/api/conversations/{conversation_id}/site/log", {"limit": limit})
    except APIError as e:
        if e.status_code == 404:
            return {"versions": [], "fallback": True}
        raise


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
