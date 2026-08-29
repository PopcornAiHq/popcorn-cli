"""Business operations for Popcorn messaging.

Every function takes an APIClient + plain parameters and returns raw data dicts.
No I/O, no formatting, no argparse — just business logic.
"""

from __future__ import annotations

import json
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
    params: dict[str, Any] = {"conversation": conv_id, "limit": limit}
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


def get_flow(client: APIClient, conversation: str, flow_id: str) -> dict[str, Any]:
    """Get a single flow definition by ID."""
    conv_id = resolve_conversation(client, conversation)
    return client.get(
        "/api/customer-flows/get",
        {"conversation_id": conv_id, "flow_id": flow_id},
    )


def _looks_like_uuid(value: str) -> bool:
    return len(value) == 36 and value.count("-") == 4


def resolve_flow_ref(client: APIClient, conversation: str, ref: str, lister: Any = None) -> str:
    """Turn a flow NAME into its id, leaving ids and unknown names alone.

    `flow list` prints names and the authoring docs use them, but a flow
    installed by `flow import` is a UUID-addressed row. The server's by-name
    path only covers flows bound through a channel_app, so a bundle installed
    ad-hoc 404s on its own name. Resolving here closes that gap.

    Best-effort by design: an unmatched name is passed through so the server's
    own by-name resolution still gets its turn, and a failed lookup falls back
    rather than breaking a run that would otherwise work.
    """
    if _looks_like_uuid(ref):
        return ref
    lookup = lister or list_flows
    try:
        flows = (lookup(client, conversation) or {}).get("flows") or []
    except Exception:  # convenience lookup, never fatal
        return ref
    for flow in flows:
        if flow.get("name") == ref and flow.get("id"):
            return str(flow["id"])
    return ref


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
    flow_id = resolve_flow_ref(client, conversation, flow_id)
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
) -> dict[str, Any]:
    """List Temporal workflow executions (flow runs) for a channel.

    ``status`` is one of ``all | running | failed | closed``. ``page_token``
    is the ``next_page_token`` cursor from a previous response.
    """
    conv_id = resolve_conversation(client, conversation)
    params: dict[str, Any] = {"conversation_id": conv_id, "limit": limit}
    if status:
        params["status"] = status
    if page_token:
        params["page_token"] = page_token
    return client.get("/api/customer-flow-runs/list", params)


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


# Mirrors the backend read_zip's per-entry ceiling so an oversized bundle fails
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

The backend's `app-bundles!` change removed the zip-install route, so
POST /api/customer-flows/import is 404 on dev and prod. There is no
client-reachable replacement: installable templates are a fixed set checked
into the backend repo and published to the bundle registry from inside the VPC.

To install a bundle:
  1. add it to popcorn-backend under lib/temporal/flows/<name>/ and register
     the name in `lib/temporal/flows/templates.py` -- CHANNEL_TEMPLATES
  2. deploy the backend, THEN publish the version from the intranet
     /app-bundles page. Publish AFTER the deploy: a bundle whose flows call a
     new activity must not become installable before the workers that can run
     it exist.
  3. install it by creating a channel with that template:
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
    server-side zip parser (``read_zip``) was retained for a future upload
    transport, and the packing rules are the checked half of that contract.
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

    Filtering is the SERVER's job (popcorn-backend#1848), not ours. Narrowing
    here would mean shipping a copy of the taxonomy in this package, and
    `category` cannot be validated offline at all — a domain exists exactly
    when an activity is registered under it, so only the server knows the set.
    An unknown value comes back as a 400 naming what would have matched, which
    is why a typo must reach the API rather than quietly matching zero rows.

    `name` is an exact wire name: a 404 if unregistered, and a 404 naming the
    current name if it is a pre-retier alias. `view="summary"` drops the JSON
    Schemas and keeps one line of each description — the whole catalog is
    ~500 KB, and a browse does not need the schemas.

    Omitted params are not sent, so against a backend predating #1848 this
    call is byte-identical to what it was.
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
    client: APIClient, conversation: str, limit: int = 50, cursor: str | None = None
) -> dict[str, Any]:
    """Recent data-store audit entries (`events`: operation, entity, changed_at)."""
    params: dict[str, Any] = {"limit": limit}
    if cursor:
        params["cursor"] = cursor
    return client.get(f"{_store_base(client, conversation)}/audit", params)


# ---------------------------------------------------------------------------
# App bundles (read)
# ---------------------------------------------------------------------------
#
# The user-JWT mirror of the agent surface's /apps reads (popcorn-backend
# #1801). `conversation_id` is required on every one of them and is what
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


def get_channel_app_tree(client: APIClient, conversation: str) -> dict[str, Any]:
    """Every file path in the channel's bound version (`paths`)."""
    conv_id = resolve_conversation(client, conversation)
    return client.get("/api/apps/tree", {"conversation_id": conv_id})


def get_channel_app_file(client: APIClient, conversation: str, path: str) -> dict[str, Any]:
    """One file's text from the channel's bound version."""
    conv_id = resolve_conversation(client, conversation)
    return client.get("/api/apps/file", {"conversation_id": conv_id, "path": path})


def get_channel_app_files(client: APIClient, conversation: str) -> dict[str, Any]:
    """The bound version's complete tree in one round trip.

    The checkout read. Bundle trees are tens of files and tens of KB, so this
    is one request rather than a tree listing plus N file reads.
    """
    conv_id = resolve_conversation(client, conversation)
    return client.get("/api/apps/files", {"conversation_id": conv_id})


# ---------------------------------------------------------------------------
# App bundles (write)
# ---------------------------------------------------------------------------
#
# The user-JWT mirror of the agent surface's writes (popcorn-backend #1803).
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
    """Publish edits as the next version on the channel's fork line.

    `payload` is `{base_version_id, files, deletes, changelog?}` — see
    `app_publish.publish_payload`. Publishing also starts the install that
    moves this channel onto the new version; other channels on the line catch
    up on their own auto-update tick.
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
# Five endpoints that predate any CLI coverage. The one shape to keep in mind:
# `PUT .../parameters` REPLACES the whole `channel_parameters` section, so
# per-key editing is a read-modify-write in the caller (see
# `channel_config.merge_parameters`) — not something this layer hides.


def inspect_channel_config(client: APIClient, conversation: str) -> dict[str, Any]:
    """The channel's config, its flows' `$channel.*` usage, and the diff.

    `comparison` is computed server-side (`compare_channel_usage`); the CLI
    renders it and must never recompute it.
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
