"""
Popcorn CLI — command-line interface for the Popcorn API.

Usage:
    popcorn auth login [--with-token] [--force]
    popcorn auth status|logout|token
    popcorn env [name]
    popcorn workspace list|switch [name]
    popcorn whoami
    popcorn search channels|dms|users [query]
    popcorn search messages <query>
    popcorn list-messages <conversation> [--thread ID] [--limit N]
    popcorn info <conversation>
    popcorn send-message <conversation> "message" [--thread ID] [--file PATH]
    popcorn react <conversation> <message_id> <emoji> [--remove]
    popcorn edit-message <conversation> <message_id> "content"
    popcorn download <file_key> [-o PATH]
    popcorn inbox [--unread|--read] [--limit N]
    popcorn watch <conversation> [--interval N]
    popcorn pop [NAME] [--context "..."] [--force]
    popcorn status [channel]
    popcorn log [channel] [--limit N]
    popcorn check-access <owner/repo>
    popcorn commands --json
    popcorn completion bash|zsh
    echo "msg" | popcorn send-message <conversation>
    cat batch.ndjson | popcorn send-message --batch --json

Flags: --json (JSON output), -q/--quiet (suppress status), --timeout N,
       -e/--env, --no-color, --workspace UUID
Conversations can be specified as #channel-name or UUID.

Custom environments can be configured via environment variables:
    POPCORN_API_URL          API base URL (default: https://api.popcorn.ai)
    POPCORN_CLERK_ISSUER     Clerk OIDC issuer URL
    POPCORN_CLERK_CLIENT_ID  Clerk OAuth client ID
"""

from __future__ import annotations

import argparse
import contextlib
import difflib
import json
import os
import re
import secrets
import sys
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from popcorn_cli import __version__
from popcorn_core import APIClient, load_config, operations, save_config
from popcorn_core.archive import create_tarball
from popcorn_core.auth import (
    CallbackHandler,
    discover_oidc,
    exchange_code_for_tokens,
    login_with_token,
    pkce_pair,
    run_callback_server,
)
from popcorn_core.config import OAUTH_CALLBACK_PORT, Profile, resolve_env
from popcorn_core.errors import (
    EXIT_INTERRUPT,
    EXIT_VALIDATION,
    APIError,
    AuthError,
    PopcornError,
)
from popcorn_core.validation import extract

from .formatting import (
    fmt_activity,
    fmt_conversation,
    fmt_message,
    fmt_user,
    format_timestamp,
    set_color,
)

# ---------------------------------------------------------------------------
# Quiet mode — suppresses informational stderr messages for agent consumption
# ---------------------------------------------------------------------------

_quiet = False


def _status(msg: str) -> None:
    """Print an informational message to stderr, unless --quiet is set."""
    if not _quiet:
        print(msg, file=sys.stderr)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_client(args: argparse.Namespace) -> APIClient:
    """Build an APIClient from stored config."""
    cfg = load_config()
    if getattr(args, "env", None):
        cfg.default_profile = args.env
    profile = cfg.active_profile()
    env = cfg.default_profile

    if not profile.id_token:
        e = AuthError("Not logged in")
        e.hint = "popcorn auth login"
        raise e
    if not profile.workspace_id:
        e = AuthError("No workspace selected")
        e.hint = "popcorn auth login --workspace <name>"
        raise e

    if getattr(args, "workspace", None):
        profile.workspace_id = args.workspace

    if sys.stderr.isatty():
        _status(f"[{env}] {profile.email} / {profile.workspace_name}")

    timeout = getattr(args, "timeout", None)
    debug = getattr(args, "debug", False)
    kwargs: dict[str, Any] = {}
    if timeout:
        kwargs["timeout"] = timeout
    if debug:
        kwargs["debug"] = True
    return APIClient(profile, **kwargs)


def _json_ok(data: Any) -> str:
    """Wrap data in the standard success envelope."""
    return json.dumps({"ok": True, "data": data}, indent=2, default=str)


def _json_err(error_dict: dict[str, Any]) -> str:
    """Wrap error in the standard error envelope."""
    return json.dumps({"ok": False, **error_dict}, indent=2, default=str)


def _output(args: argparse.Namespace, data: Any, formatted: str) -> None:
    """Print JSON (wrapped in envelope) or human-readable output."""
    if getattr(args, "json", False):
        print(_json_ok(data))
    else:
        print(formatted)


def _select_workspace(client: APIClient, profile: Profile, target: str | None = None) -> None:
    """Interactive workspace selection (auto-selects first when non-interactive)."""
    workspaces = operations.list_workspaces(client)

    if not workspaces:
        raise PopcornError("No workspaces found for this account")

    # Explicit --workspace flag: match by name or ID
    if target:
        for ws in workspaces:
            if ws["id"] == target or (ws.get("name") or "").lower() == target.lower():
                profile.workspace_id = ws["id"]
                profile.workspace_name = ws.get("name", "")
                _status(f"Selected workspace: {profile.workspace_name}")
                return
        raise PopcornError(f"Workspace not found: {target}")

    if len(workspaces) == 1:
        ws = workspaces[0]
        profile.workspace_id = ws["id"]
        profile.workspace_name = ws.get("name", "")
        _status(f"Auto-selected workspace: {ws.get('name', ws['id'])}")
        return

    # Non-interactive: auto-select first workspace so agents don't hang
    if not sys.stdin.isatty():
        ws = workspaces[0]
        profile.workspace_id = ws["id"]
        profile.workspace_name = ws.get("name", "")
        _status(f"Auto-selected workspace: {ws.get('name', ws['id'])} (first of {len(workspaces)})")
        return

    print("\nAvailable workspaces:")
    for i, ws in enumerate(workspaces, 1):
        active = " <- current" if ws["id"] == profile.workspace_id else ""
        print(f"  {i}. {ws.get('name', 'Unnamed')} ({ws['id']}){active}")
    while True:
        try:
            choice = input(f"\nSelect workspace [1-{len(workspaces)}]: ").strip()
            idx = int(choice) - 1
            if 0 <= idx < len(workspaces):
                ws = workspaces[idx]
                profile.workspace_id = ws["id"]
                profile.workspace_name = ws.get("name", "")
                break
        except (ValueError, EOFError):
            pass
        print("Invalid selection, try again.")


# ---------------------------------------------------------------------------
# Auth commands
# ---------------------------------------------------------------------------


def cmd_auth_login(args: argparse.Namespace) -> None:
    cfg = load_config()
    env_name = getattr(args, "env", None) or cfg.default_profile
    cfg.default_profile = env_name

    preset = resolve_env()
    profile = cfg.active_profile()

    # Skip if already logged in (unless --force or --with-token)
    if (
        profile.id_token
        and profile.email
        and not getattr(args, "force", False)
        and not getattr(args, "with_token", False)
    ):
        now = int(time.time())
        if profile.expires_at == 0 or profile.expires_at > now:
            print(f"Already logged in as {profile.email}")
            print(f"Workspace: {profile.workspace_name}")
            print("Run with --force to re-authenticate.")
            return

    profile.api_url = preset["api_url"]
    profile.clerk_issuer = preset["clerk_issuer"]

    # --with-token: headless/CI mode
    if args.with_token:
        token = sys.stdin.read().strip()
        if not token:
            raise AuthError("No token provided on stdin")

        result = login_with_token(token)
        profile.id_token = result["token"]
        profile.access_token = ""
        profile.refresh_token = ""
        profile.email = result["email"]
        profile.expires_at = result["exp"]

        save_config(cfg)
        print(f"Authenticated as {result['email']}")
        client = APIClient(profile)
        _select_workspace(client, profile, getattr(args, "workspace", None))
        save_config(cfg)
        print(f"\nLogged in as {result['email']} in workspace {profile.workspace_name}")
        return

    # Browser OAuth flow
    client_id = (
        os.environ.get("POPCORN_CLERK_CLIENT_ID")
        or profile.clerk_client_id
        or preset["clerk_client_id"]
    )
    if not client_id:
        raise PopcornError(
            "No Clerk OAuth client ID configured.\n"
            "Set POPCORN_CLERK_CLIENT_ID environment variable or add clerk_client_id "
            "to your profile in ~/.config/popcorn/auth.json"
        )
    profile.clerk_client_id = client_id

    print(f"Discovering OIDC endpoints for {profile.clerk_issuer}...")
    oidc = discover_oidc(profile.clerk_issuer)
    verifier, challenge = pkce_pair()

    # Reset any stale state from a prior login attempt
    CallbackHandler.auth_code = None
    CallbackHandler.error = None

    oauth_state = secrets.token_urlsafe(32)
    CallbackHandler.expected_state = oauth_state

    server = run_callback_server()
    redirect_uri = f"http://localhost:{OAUTH_CALLBACK_PORT}/callback"

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": oauth_state,
    }
    auth_url = f"{oidc['authorization_endpoint']}?{urlencode(params)}"

    print("Opening browser for authentication...")
    webbrowser.open(auth_url)
    print("Waiting for callback... (press Ctrl+C to cancel)")

    deadline = time.time() + 120
    while (
        CallbackHandler.auth_code is None
        and CallbackHandler.error is None
        and time.time() < deadline
    ):
        time.sleep(0.2)

    server.server_close()

    if CallbackHandler.error:
        raise AuthError(f"Authentication failed: {CallbackHandler.error}")
    if not CallbackHandler.auth_code:
        raise AuthError("Authentication timed out")

    code = CallbackHandler.auth_code
    CallbackHandler.auth_code = None
    CallbackHandler.error = None

    print("Exchanging authorization code...")
    tokens = exchange_code_for_tokens(
        oidc["token_endpoint"], code, redirect_uri, client_id, verifier
    )

    profile.id_token = tokens["id_token"]
    profile.access_token = tokens["access_token"]
    profile.refresh_token = tokens["refresh_token"]
    profile.email = tokens["email"]
    profile.expires_at = tokens["exp"]

    print("Fetching workspaces...")
    save_config(cfg)

    client = APIClient(profile)
    _select_workspace(client, profile)
    save_config(cfg)
    print(f"\nLogged in as {tokens['email']} in workspace {profile.workspace_name}")


def cmd_auth_status(args: argparse.Namespace) -> None:
    cfg = load_config()
    profile = cfg.active_profile()

    if not profile.email:
        print("Not logged in. Run: popcorn auth login")
        return

    now = int(time.time())
    status = "expired" if (profile.expires_at > 0 and profile.expires_at < now) else "valid"

    print(f"Profile:   {cfg.default_profile}")
    print(f"Email:     {profile.email}")
    print(f"Workspace: {profile.workspace_name} ({profile.workspace_id})")
    print(f"API:       {profile.api_url}")
    print(f"Token:     {status}")
    if profile.expires_at > 0:
        exp_dt = datetime.fromtimestamp(profile.expires_at, tz=timezone.utc)
        print(f"Expires:   {exp_dt.strftime('%Y-%m-%d %H:%M UTC')}")


def cmd_auth_token(args: argparse.Namespace) -> None:
    cfg = load_config()
    profile = cfg.active_profile()
    if not profile.id_token:
        raise AuthError("Not logged in. Run: popcorn auth login")
    sys.stdout.write(profile.id_token)
    if sys.stdout.isatty():
        sys.stdout.write("\n")


def cmd_auth_logout(args: argparse.Namespace) -> None:
    from popcorn_core.config import _KEYRING_FIELDS, _has_keyring, _keyring_delete

    cfg = load_config()
    profile_name = cfg.default_profile
    profile = cfg.active_profile()
    profile.access_token = ""
    profile.refresh_token = ""
    profile.id_token = ""
    profile.email = ""
    profile.expires_at = 0

    # Clear keyring entries if available
    if _has_keyring():
        for fld in _KEYRING_FIELDS:
            _keyring_delete(f"{profile_name}/{fld}")

    save_config(cfg)
    print(f"Logged out of profile: {profile_name}")


# ---------------------------------------------------------------------------
# Workspace commands
# ---------------------------------------------------------------------------


def cmd_workspace_list(args: argparse.Namespace) -> None:
    cfg = load_config()
    profile = cfg.active_profile()
    if not profile.id_token:
        raise AuthError("Not logged in. Run: popcorn auth login")

    client = APIClient(profile)
    workspaces = operations.list_workspaces(client)

    if getattr(args, "json", False):
        print(_json_ok({"workspaces": workspaces}))
        return

    for ws in workspaces:
        active = " <- current" if ws["id"] == profile.workspace_id else ""
        print(f"  {ws.get('name', 'Unnamed')} (id: {ws['id']}){active}")
    if not workspaces:
        print("No workspaces found.")


def cmd_workspace_switch(args: argparse.Namespace) -> None:
    cfg = load_config()
    profile = cfg.active_profile()
    if not profile.id_token:
        raise AuthError("Not logged in. Run: popcorn auth login")

    client = APIClient(profile)
    workspaces = operations.list_workspaces(client)

    if not workspaces:
        raise PopcornError("No workspaces found for this account")

    target = args.workspace if hasattr(args, "workspace") and args.workspace else None
    if target:
        for ws in workspaces:
            if ws["id"] == target or (ws.get("name") or "").lower() == target.lower():
                profile.workspace_id = ws["id"]
                profile.workspace_name = ws.get("name", "")
                save_config(cfg)
                print(f"Switched to: {profile.workspace_name} ({profile.workspace_id})")
                return
        raise PopcornError(f"Workspace not found: {target}")

    _select_workspace(client, profile)
    save_config(cfg)
    print(f"Switched to: {profile.workspace_name} ({profile.workspace_id})")


def cmd_env(args: argparse.Namespace) -> None:
    cfg = load_config()
    target = getattr(args, "target_env", None)

    if target:
        cfg.default_profile = target
        save_config(cfg)
        profile = cfg.active_profile()
        if profile.email:
            print(f"Switched to {target} ({profile.email} / {profile.workspace_name})")
        else:
            print(f"Switched to {target} (not logged in -- run: popcorn auth login)")
    else:
        if not cfg.profiles:
            print("No profiles configured. Run: popcorn auth login")
            return
        for name, profile in cfg.profiles.items():
            active = " <- current" if name == cfg.default_profile else ""
            if profile.email:
                print(f"  {name}: {profile.email} / {profile.workspace_name}{active}")
            else:
                print(f"  {name}: (not logged in){active}")


# ---------------------------------------------------------------------------
# Commands — each calls operations + formats output
# ---------------------------------------------------------------------------


def cmd_whoami(args: argparse.Namespace) -> None:
    client = _get_client(args)
    resp = operations.get_whoami(client)
    user = extract(resp, "user", label="whoami")
    ws = extract(resp, "workspace", label="whoami")

    # For JSON mode, include all workspaces for full agent bootstrapping
    if getattr(args, "json", False):
        workspaces = operations.list_workspaces(client)
        resp["workspaces"] = workspaces
        print(_json_ok(resp))
        return

    formatted = (
        f"User:      {user.get('display_name', '')} ({user.get('username', '')})\n"
        f"Email:     {user.get('email', '')}\n"
        f"User ID:   {user.get('id', '')}\n"
        f"Workspace: {ws.get('name', '')} (id: {ws.get('id', '')})\n"
        f"Role:      {(user.get('workspace_info') or {}).get('workspace_role', 'member')}"
    )
    print(formatted)


def cmd_search(args: argparse.Namespace) -> None:
    client = _get_client(args)
    search_type = args.search_type
    query = args.query or ""

    if search_type == "channels":
        resp = operations.search_channels(client, query)
        convs = resp.get("conversations", [])
        fmt = (
            "Channels:\n" + "\n".join(fmt_conversation(c) for c in convs)
            if convs
            else "No channels found."
        )
        _output(args, resp, fmt)

    elif search_type == "dms":
        resp = operations.search_dms(client, query)
        convs = resp.get("conversations", [])
        fmt = "DMs:\n" + "\n".join(fmt_conversation(c) for c in convs) if convs else "No DMs found."
        _output(args, resp, fmt)

    elif search_type == "users":
        resp = operations.search_users(client, query)
        users = resp.get("users", [])
        fmt = "Users:\n" + "\n".join(fmt_user(u) for u in users) if users else "No users found."
        _output(args, resp, fmt)

    elif search_type == "messages":
        resp = operations.search_messages(client, query)
        messages = resp.get("messages", [])
        lines = [fmt_message(item.get("message") or item) for item in messages]
        fmt = "Messages:\n" + "\n".join(lines) if lines else "No messages found."
        _output(args, resp, fmt)


def cmd_list_messages(args: argparse.Namespace) -> None:
    client = _get_client(args)
    resp = operations.read_messages(
        client,
        args.conversation,
        args.thread or "",
        args.limit or 25,
        latest=getattr(args, "before", "") or "",
        oldest=getattr(args, "after", "") or "",
    )
    messages = resp.get("messages", [])
    lines = [fmt_message(m) for m in messages]
    if resp.get("has_more"):
        lines.append("\n  ... more messages (use --limit to see more)")
    _output(args, resp, "\n".join(lines) if lines else "No messages.")


def cmd_list_threads(args: argparse.Namespace) -> None:
    client = _get_client(args)
    resp = operations.list_threads(
        client,
        args.conversation,
        limit=args.limit or 50,
        offset=getattr(args, "offset", 0) or 0,
    )
    threads = resp.get("threads", [])

    if getattr(args, "json", False):
        print(_json_ok(resp))
        return

    if not threads:
        print("No threads found.")
        return

    for t in threads:
        parent = t.get("parent_message", {})
        author = parent.get("author", {})
        name = author.get("display_name") or author.get("username") or "?"
        reply_count = t.get("reply_count", 0)
        last_reply = t.get("last_reply_at", "")
        preview = ""
        for part in parent.get("content", []):
            if part.get("type") == "text":
                preview = part.get("text", "")[:80]
                break
        print(
            f"  {parent.get('id', '?')}  {reply_count} replies  "
            f"last: {format_timestamp(last_reply)}  {name}: {preview}"
        )


def cmd_info(args: argparse.Namespace) -> None:
    client = _get_client(args)
    resp = operations.get_conversation_info(client, args.conversation)
    conv = resp.get("conversation", {})
    members = resp.get("members", [])

    conv_type = conv.get("type", "")
    type_label = conv_type.replace("_", " ").title() if conv_type else "Unknown"

    lines = [
        f"Name:        {conv.get('name', 'Unnamed')}",
        f"ID:          {conv.get('id', '')}",
        f"Type:        {type_label}",
    ]
    desc = conv.get("description", "")
    if desc:
        lines.append(f"Description: {desc}")
    lines.append(f"Created:     {format_timestamp(conv.get('created_at'))}")
    if conv.get("is_archived"):
        lines.append("Archived:    Yes")
    lines.append(f"Members ({len(members)}):")
    for m in members:
        name = m.get("display_name") or m.get("username") or "?"
        lines.append(f"  - {name} (id: {m.get('id', '?')})")

    _output(args, resp, "\n".join(lines))


def cmd_send_message(args: argparse.Namespace) -> None:
    if getattr(args, "batch", False):
        _cmd_send_batch(args)
        return

    client = _get_client(args)

    if not getattr(args, "conversation", None):
        e = PopcornError("conversation is required (or use --batch for NDJSON stdin)")
        e.hint = 'popcorn send-message <#channel> "message"'
        raise e

    message = getattr(args, "message", None)
    if message == "-" or (message is None and not sys.stdin.isatty()):
        message = sys.stdin.read().strip()

    file_path = getattr(args, "file", None)
    if not message and not file_path:
        e = PopcornError("Provide a message, --file, or pipe text via stdin")
        e.hint = 'popcorn send-message <#channel> "message"'
        raise e

    file_parts = []
    if file_path:
        _status(f"Uploading {file_path}...")
        file_parts.append(operations.upload_file(client, args.conversation, file_path))
        _status("Uploaded.")

    resp = operations.send_message(
        client, args.conversation, message or "", args.thread or "", file_parts
    )
    msg = resp.get("message", {})
    _output(args, resp, f"Sent (id: {msg.get('id', '?')})")


def _cmd_send_batch(args: argparse.Namespace) -> None:
    """Send messages from NDJSON stdin. Each line: {"conversation": "...", "message": "..."}."""
    client = _get_client(args)
    json_mode = getattr(args, "json", False)
    fail_fast = getattr(args, "fail_fast", False)
    results: list[dict[str, Any]] = []

    for line_num, line in enumerate(sys.stdin, 1):
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as e:
            results.append({"line": line_num, "error": f"Invalid JSON: {e}", "ok": False})
            if fail_fast:
                break
            continue

        conv = item.get("conversation")
        msg_text = item.get("message", "")
        thread = item.get("thread", "")

        if not conv:
            results.append({"line": line_num, "error": "Missing 'conversation' field", "ok": False})
            if fail_fast:
                break
            continue
        if not msg_text:
            results.append({"line": line_num, "error": "Missing 'message' field", "ok": False})
            if fail_fast:
                break
            continue

        try:
            resp = operations.send_message(client, conv, msg_text, thread, [])
            sent_msg = resp.get("message", {})
            results.append({"line": line_num, "ok": True, "message_id": sent_msg.get("id", "?")})
        except PopcornError as e:
            results.append({"line": line_num, "error": str(e), "ok": False})
            if fail_fast:
                break

    if json_mode:
        print(_json_ok({"results": results}))
    else:
        for r in results:
            if r["ok"]:
                print(f"Line {r['line']}: Sent (id: {r['message_id']})")
            else:
                print(f"Line {r['line']}: Error: {r['error']}")


def cmd_react(args: argparse.Namespace) -> None:
    client = _get_client(args)
    if args.remove:
        resp = operations.remove_reaction(client, args.conversation, args.message_id, args.emoji)
        _output(args, resp, f"Removed {args.emoji}")
    else:
        resp = operations.add_reaction(client, args.conversation, args.message_id, args.emoji)
        _output(args, resp, f"Added {args.emoji}")


def cmd_edit_message(args: argparse.Namespace) -> None:
    client = _get_client(args)
    resp = operations.edit_message(client, args.conversation, args.message_id, args.content)
    _output(args, resp, f"Edited (id: {args.message_id})")


def cmd_delete_message(args: argparse.Namespace) -> None:
    client = _get_client(args)
    resp = operations.delete_message(client, args.conversation, args.message_id)
    _output(args, resp, f"Deleted (id: {args.message_id})")


def cmd_get_message(args: argparse.Namespace) -> None:
    client = _get_client(args)
    resp = operations.get_message(client, args.message_id)
    msg = resp.get("message", resp)
    _output(args, resp, fmt_message(msg) if not getattr(args, "json", False) else "")


def cmd_download(args: argparse.Namespace) -> None:
    import httpx as _httpx

    client = _get_client(args)
    resp = operations.download_file(client, args.file_key)

    if getattr(args, "json", False):
        print(_json_ok(resp))
        return

    url = resp.get("download_url") or resp.get("url")
    if not url:
        raise PopcornError("No download URL in response — use --json to inspect")

    file_meta = resp.get("file_metadata") or resp.get("file_upload") or {}
    filename = file_meta.get("file_name") or args.file_key.rsplit("/", 1)[-1]
    output_path = args.output or filename

    dl = _httpx.get(url, follow_redirects=True, timeout=120.0)
    dl.raise_for_status()
    with open(output_path, "wb") as f:
        f.write(dl.content)
    print(f"Saved {output_path} ({len(dl.content)} bytes)")


# ---------------------------------------------------------------------------
# Conversation management commands
# ---------------------------------------------------------------------------


def cmd_create_channel(args: argparse.Namespace) -> None:
    client = _get_client(args)
    if_not_exists = getattr(args, "if_not_exists", False)

    # --if-not-exists: search for existing channel first
    if if_not_exists:
        existing = operations.search_channels(client, args.name)
        for conv in existing.get("conversations", []):
            if (conv.get("name") or "").lower() == args.name.lower():
                resp = {"conversation": conv, "already_existed": True}
                _output(
                    args,
                    resp,
                    f"Already exists: {conv.get('name', '')} (id: {conv.get('id', '?')})",
                )
                return

    member_ids = args.members.split(",") if getattr(args, "members", None) else None
    try:
        resp = operations.create_conversation(
            client,
            name=args.name,
            conv_type=getattr(args, "type", "public_channel") or "public_channel",
            member_ids=member_ids,
        )
    except APIError as e:
        # Handle race: channel created between our search and create (--if-not-exists)
        if if_not_exists and e.status_code == 409:
            existing = operations.search_channels(client, args.name)
            for conv in existing.get("conversations", []):
                if (conv.get("name") or "").lower() == args.name.lower():
                    resp = {"conversation": conv, "already_existed": True}
                    _output(
                        args,
                        resp,
                        f"Already exists: {conv.get('name', '')} (id: {conv.get('id', '?')})",
                    )
                    return
        raise
    conv = resp.get("conversation", resp)
    _output(args, resp, f"Created: {conv.get('name', '')} (id: {conv.get('id', '?')})")


def cmd_join_channel(args: argparse.Namespace) -> None:
    client = _get_client(args)
    resp = operations.join_conversation(client, args.conversation)
    _output(args, resp, f"Joined {args.conversation}")


def cmd_leave_channel(args: argparse.Namespace) -> None:
    client = _get_client(args)
    try:
        resp = operations.leave_conversation(client, args.conversation)
    except APIError as e:
        # Backend returns 404 with "Member" in the message when not a member.
        # Don't swallow 404s for missing conversations — only for membership.
        if e.status_code == 404 and "member" in str(e).lower():
            resp = {"ok": True, "already_left": True}
            _output(args, resp, f"Already not a member of {args.conversation}")
            return
        raise
    _output(args, resp, f"Left {args.conversation}")


def cmd_archive_channel(args: argparse.Namespace) -> None:
    client = _get_client(args)
    if getattr(args, "undo", False):
        resp = operations.unarchive_conversation(client, args.conversation)
        _output(args, resp, f"Unarchived {args.conversation}")
    else:
        resp = operations.archive_conversation(client, args.conversation)
        _output(args, resp, f"Archived {args.conversation}")


def cmd_invite(args: argparse.Namespace) -> None:
    client = _get_client(args)
    user_ids = [uid.strip() for uid in args.user_ids.split(",")]
    resp = operations.invite_to_conversation(client, args.conversation, user_ids)
    _output(args, resp, f"Invited {len(user_ids)} user(s) to {args.conversation}")


def cmd_kick(args: argparse.Namespace) -> None:
    client = _get_client(args)
    resp = operations.kick_from_conversation(client, args.conversation, args.user_id)
    _output(args, resp, f"Removed {args.user_id} from {args.conversation}")


def cmd_edit_channel(args: argparse.Namespace) -> None:
    client = _get_client(args)
    resp = operations.update_conversation(
        client,
        args.conversation,
        name=getattr(args, "name", "") or "",
        description=getattr(args, "description", "") or "",
    )
    _output(args, resp, f"Updated {args.conversation}")


def cmd_delete_channel(args: argparse.Namespace) -> None:
    client = _get_client(args)
    resp = operations.delete_conversation(client, args.conversation)
    _output(args, resp, f"Deleted {args.conversation}")


# ---------------------------------------------------------------------------
# Webhook commands
# ---------------------------------------------------------------------------


def cmd_webhook(args: argparse.Namespace) -> None:
    sub = getattr(args, "webhook_command", None)
    client = _get_client(args)

    if sub == "create":
        resp = operations.create_webhook(
            client,
            args.conversation,
            args.name,
            description=getattr(args, "description", None),
            avatar_url=getattr(args, "avatar_url", None),
            action_mode=getattr(args, "action_mode", None),
        )
        _output(args, resp, f"Created webhook '{args.name}' for {args.conversation}")
    elif sub == "list":
        resp = operations.list_webhooks(client, args.conversation)
        hooks = resp if isinstance(resp, list) else resp.get("webhooks", [resp])
        lines = [f"Webhooks for {args.conversation} ({len(hooks)}):"]
        for h in hooks:
            lines.append(f"  {h.get('id', '?')}  {h.get('name', '?')}")
        _output(args, resp, "\n".join(lines))
    elif sub == "deliveries":
        resp = operations.list_webhook_deliveries(
            client,
            args.conversation,
            limit=getattr(args, "limit", 50),
            since=getattr(args, "since", None),
            status=getattr(args, "status", None),
        )
        deliveries = resp if isinstance(resp, list) else resp.get("deliveries", [resp])
        lines = [f"Deliveries for {args.conversation} ({len(deliveries)}):"]
        for d in deliveries:
            wh_name = d.get("webhook_name", d.get("webhook_id", "?"))
            ts = d.get("created_at", "?")
            lines.append(f"  {d.get('id', '?')}  {wh_name}  {ts}")
        _output(args, resp, "\n".join(lines))
    else:
        raise PopcornError("Usage: popcorn webhook [create|list|deliveries]")


# ---------------------------------------------------------------------------
# Pop (push site resources to a channel)
# ---------------------------------------------------------------------------


def _write_local_json(path: Path, conversation_id: str, site_name: str) -> None:
    """Persist deploy state to .popcorn.local.json."""
    path.write_text(
        json.dumps({"conversation_id": conversation_id, "site_name": site_name}, indent=2)
    )


def _validate_channel(client: APIClient, conversation_id: str) -> bool:
    """Check if a conversation still exists.

    Returns True if valid, False if stale (404).
    Raises APIError for unexpected failures.
    """
    try:
        client.get("/api/conversations/info", {"conversation_id": conversation_id})
        return True
    except APIError as e:
        if e.status_code == 404:
            return False
        raise


def _resolve_conversation_id_from_local(args: argparse.Namespace, client: APIClient) -> str:
    """Resolve conversation_id from channel arg or .popcorn.local.json."""
    channel = getattr(args, "channel", None)
    if channel:
        from popcorn_core.resolve import resolve_conversation

        return resolve_conversation(client, channel)

    local_json = Path(".popcorn.local.json")
    if local_json.exists():
        data = json.loads(local_json.read_text())
        cid = data.get("conversation_id")
        if cid:
            return str(cid)

    raise PopcornError("No channel specified and no .popcorn.local.json found")


def _create_with_collision_retry(
    client: APIClient, site_name: str, json_mode: bool
) -> tuple[dict[str, Any], str]:
    """Create a deploy channel, retrying with random suffixes on 409.

    Returns (create_result, effective_site_name).
    """
    import random
    import string

    try:
        result = operations.deploy_create(client, site_name)
        return result, site_name
    except APIError as e:
        if e.status_code != 409:
            raise

    # Name taken — try up to 5 random suffixes
    attempted: list[str] = []
    for _ in range(5):
        suffix = "".join(random.choices(string.ascii_lowercase, k=4))
        candidate = f"{site_name}-{suffix}"
        attempted.append(candidate)
        try:
            result = operations.deploy_create(client, candidate)
            if not json_mode:
                _status(f"'{site_name}' is taken. Created as '{candidate}' instead.")
            return result, candidate
        except APIError as e2:
            if e2.status_code != 409:
                raise

    if json_mode:
        print(
            json.dumps(
                {
                    "error": f"Could not find available name for '{site_name}'",
                    "code": "PopcornError",
                    "retryable": False,
                    "attempted_names": attempted,
                }
            )
        )
        sys.exit(EXIT_VALIDATION)
    raise PopcornError(
        f"Could not find available name for '{site_name}'. Tried: {', '.join(attempted)}"
    )


def _publish_with_retry(
    client: APIClient,
    conversation_id: str,
    s3_key: str,
    context: str,
    force: bool,
    json_mode: bool,
) -> dict[str, Any]:
    """Call deploy_publish with retry on 502 (up to 3 retries, exponential backoff)."""
    import time

    max_retries = 3
    for attempt in range(max_retries + 1):
        try:
            return operations.deploy_publish(client, conversation_id, s3_key, context, force=force)
        except APIError as e:
            if e.status_code != 502 or attempt == max_retries:
                raise
            delay = 2**attempt  # 1, 2, 4
            if not json_mode:
                _status(f"Retrying publish (attempt {attempt + 2}/{max_retries + 1})...")
            time.sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


def _parse_vm_error(e: APIError) -> str | None:
    """Try to extract the real VM error from an APIError body."""
    if not e.body:
        return None
    try:
        body = json.loads(e.body)
    except (json.JSONDecodeError, TypeError):
        return None
    for key in ("vm_error", "upstream_error", "error"):
        val = body.get(key)
        if isinstance(val, str):
            return val
    return None


def cmd_pop(args: argparse.Namespace) -> None:
    client = _get_client(args)
    site_name = args.name or f"pop-{Path.cwd().name}"
    json_mode = getattr(args, "json", False)
    force = getattr(args, "force", False)
    verbose = getattr(args, "verbose", False)

    def _progress(msg: str) -> None:
        if verbose and not json_mode:
            print(msg, file=sys.stderr)

    # Read .popcorn.local.json
    local_json = Path(".popcorn.local.json")
    conversation_id = None
    if local_json.exists():
        data = json.loads(local_json.read_text())
        conversation_id = data.get("conversation_id")

    # Validate existing channel — detect stale .popcorn.local.json
    if conversation_id and not _validate_channel(client, conversation_id):
        if force:
            local_json.unlink(missing_ok=True)
            conversation_id = None
        elif json_mode:
            print(
                _json_err(
                    {
                        "error": "Stale channel configuration",
                        "code": "PopcornError",
                        "retryable": False,
                        "stale_config": True,
                        "conversation_id": conversation_id,
                    }
                ),
                file=sys.stderr,
            )
            sys.exit(EXIT_VALIDATION)
        elif sys.stdin.isatty():
            answer = input("Channel no longer exists. Create new? [Y/n] ")
            if answer.strip().lower() in ("n", "no"):
                return
            local_json.unlink(missing_ok=True)
            conversation_id = None
        else:
            # Non-interactive: auto-recreate like --force so agents don't hang
            _status("Stale channel configuration — auto-recreating.")
            local_json.unlink(missing_ok=True)
            conversation_id = None

    # Create tarball
    _progress("Packaging files...")
    tarball = create_tarball()
    suggested_name = None

    try:
        # Create channel with site (first deploy)
        if not conversation_id:
            _progress(f"Creating channel #{site_name}...")
            create_result, site_name = _create_with_collision_retry(client, site_name, json_mode)
            conversation_id = str(
                extract(create_result, "conversation", "id", label="deploy_create")
            )
            if site_name != (args.name or f"pop-{Path.cwd().name}"):
                suggested_name = site_name

            # Persist conversation_id immediately so retries don't hit 409
            _write_local_json(local_json, conversation_id, site_name)

        if not conversation_id:
            raise PopcornError("No conversation_id available for deploy")

        # Presign
        _progress("Requesting upload URL...")
        presign = operations.deploy_presign(client, conversation_id)
        upload_url = extract(presign, "upload_url", label="deploy_presign")
        upload_fields = extract(presign, "upload_fields", label="deploy_presign")
        s3_key = extract(presign, "s3_key", label="deploy_presign")

        # Upload to S3
        _progress("Uploading...")
        operations.deploy_upload(upload_url, upload_fields, tarball)

        # Publish with retry on 502 (items 1, 2)
        _progress("Publishing...")
        try:
            result = _publish_with_retry(
                client, conversation_id, s3_key, args.context, force, json_mode
            )
        except APIError as e:
            vm_error = _parse_vm_error(e)
            if vm_error:
                if json_mode:
                    err_data: dict[str, Any] = {
                        "error": str(e),
                        "code": "APIError",
                        "retryable": e.retryable,
                        "vm_error": vm_error,
                    }
                    if e.status_code:
                        err_data["status"] = e.status_code
                    if e.body:
                        with contextlib.suppress(json.JSONDecodeError, TypeError):
                            err_data["body"] = json.loads(e.body)
                    print(_json_err(err_data), file=sys.stderr)
                    sys.exit(e.exit_code)
                raise PopcornError(f"Publish failed: {vm_error}") from e
            raise
    finally:
        # Cleanup tarball
        os.unlink(tarball)

    # Update .popcorn.local.json with server-confirmed values

    result_conv_id = str(extract(result, "conversation_id", label="deploy_publish"))
    result_site_name = extract(result, "site_name", label="deploy_publish")
    _write_local_json(local_json, result_conv_id, result_site_name)

    # Add to .gitignore
    gitignore = Path(".gitignore")
    if gitignore.exists():
        content = gitignore.read_text()
        if ".popcorn.local.json" not in content:
            gitignore.write_text(content.rstrip() + "\n.popcorn.local.json\n")

    # Fetch site URL for output (non-fatal — URL is a convenience field)
    site_url = None
    try:
        site_status = operations.get_site_status(client, result_conv_id)
        site_url = site_status.get("url")
    except PopcornError:
        pass

    # Build output
    output_data: dict[str, Any] = {**result}
    if site_url:
        output_data["site_url"] = site_url
    if suggested_name:
        output_data["suggested_name"] = suggested_name

    human_line = f"Published to #{result_site_name} (v{result['version']})"
    if site_url:
        human_line += f"\n{site_url}"
    _output(args, output_data, human_line)


def cmd_status(args: argparse.Namespace) -> None:
    client = _get_client(args)
    conversation_id = _resolve_conversation_id_from_local(args, client)
    resp = operations.get_site_status(client, conversation_id)

    if getattr(args, "json", False):
        print(_json_ok(resp))
        return

    if resp.get("fallback"):
        conv = resp.get("conversation", {})
        name = conv.get("name", "—")
        lines = [
            f"Site:      {name}",
            "URL:       —",
            "Version:   —",
            "Commit:    —",
            "Deployed:  —",
            "(Detailed status not available)",
        ]
    else:
        lines = [
            f"Site:      {resp.get('site_name', '—')}",
            f"URL:       {resp.get('url', '—')}",
            f"Version:   {resp.get('version', '—')}",
            f"Commit:    {resp.get('commit_hash', '—')}",
            f"Deployed:  {resp.get('deployed_at', '—')} by {resp.get('deployed_by', '—')}",
        ]
    print("\n".join(lines))


def cmd_log(args: argparse.Namespace) -> None:
    client = _get_client(args)
    conversation_id = _resolve_conversation_id_from_local(args, client)
    resp = operations.get_site_log(client, conversation_id, limit=args.limit)

    if getattr(args, "json", False):
        print(_json_ok(resp))
        return

    if resp.get("fallback"):
        print("Version history not available yet")
        return

    versions = resp.get("versions") or resp.get("entries") or []
    if not versions:
        print("No versions found")
        return

    for v in versions:
        ver = v.get("version", "?")
        commit = v.get("commit_hash", "?")[:7]
        msg = v.get("message", "")
        author = v.get("author", "")
        ts = v.get("created_at", "")
        print(f"v{ver}  {commit}  {msg:<30s}  {author}  {ts}")


# ---------------------------------------------------------------------------
# Integrations
# ---------------------------------------------------------------------------


def cmd_check_access(args: argparse.Namespace) -> None:
    client = _get_client(args)
    resp = operations.check_access(client, args.repo)
    if resp.get("accessible"):
        formatted = f"Popcorn has access to {args.repo}"
    else:
        auth_url = resp.get("auth_url", "")
        formatted = f"Popcorn does not have access to {args.repo}."
        if auth_url:
            formatted += f" Authorize at: {auth_url}"
    _output(args, resp, formatted)


# ---------------------------------------------------------------------------
# Raw API escape hatch
# ---------------------------------------------------------------------------


def cmd_api(args: argparse.Namespace) -> None:
    client = _get_client(args)
    data = None
    if getattr(args, "data", None):
        try:
            data = json.loads(args.data)
        except json.JSONDecodeError as e:
            raise PopcornError(f"Invalid JSON in --data: {e}") from e

    params: dict[str, str] | None = None
    if getattr(args, "param", None):
        params = {}
        for item in args.param:
            if "=" not in item:
                raise PopcornError(f"Invalid --param format: {item!r} (expected KEY=VALUE)")
            k, v = item.split("=", 1)
            params[k] = v

    method = args.method or ("POST" if data else "GET")
    resp = operations.raw_api_call(client, method, args.path, data, params=params)
    if getattr(args, "raw", False) or not getattr(args, "json", False):
        print(json.dumps(resp, indent=2, default=str))
    else:
        print(_json_ok(resp))


def cmd_inbox(args: argparse.Namespace) -> None:
    client = _get_client(args)
    filter_type = "unread" if args.unread else ("read" if args.read else "all")
    resp = operations.get_inbox(client, filter_type, args.limit or 20)

    activity_data = extract(resp, "activity", label="inbox")
    activities = activity_data.get("activities", [])
    unread_count = activity_data.get("unread_count", 0)

    lines = [f"Unread: {unread_count}"]
    lines.extend(fmt_activity(act) for act in activities)
    if not activities:
        lines.append("  (no notifications)")
    _output(args, resp, "\n".join(lines))


def cmd_watch(args: argparse.Namespace) -> None:
    client = _get_client(args)
    interval = args.interval or 3
    max_count = getattr(args, "count", None) or 0
    max_wait = getattr(args, "max_wait", None)
    json_mode = getattr(args, "json", False)
    seen = 0
    start = time.monotonic()

    resp = operations.read_messages(client, args.conversation, limit=1)
    messages = resp.get("messages", [])
    last_seen_id = messages[0]["id"] if messages else None

    _status(f"Watching... (Ctrl+C to stop, polling every {interval}s)")

    try:
        while True:
            if max_wait and (time.monotonic() - start) >= max_wait:
                _status(f"Max wait ({max_wait}s) reached.")
                return
            time.sleep(interval)
            resp = operations.read_messages(client, args.conversation, limit=50)
            messages = resp.get("messages", [])

            # Messages come newest-first from API. Find where last_seen_id
            # sits and collect everything newer (before it in the list).
            new_msgs = []
            for msg in messages:
                if msg.get("id") == last_seen_id:
                    break
                new_msgs.append(msg)

            if new_msgs:
                # Print oldest-first
                for msg in reversed(new_msgs):
                    if json_mode:
                        print(json.dumps({"ok": True, "data": msg}, default=str), flush=True)
                    else:
                        print(fmt_message(msg), flush=True)
                    seen += 1
                    if max_count and seen >= max_count:
                        return
                # new_msgs[0] is the newest message (first in API response)
                last_seen_id = new_msgs[0]["id"]
    except KeyboardInterrupt:
        _status("\nStopped watching.")


# ---------------------------------------------------------------------------
# Shell completions
# ---------------------------------------------------------------------------

_BASH_COMPLETION = r"""
_popcorn_completions() {
    local cur prev
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"

    case "$prev" in
        popcorn)
            COMPREPLY=($(compgen -W "auth workspace env whoami search list-messages list-threads get-message info inbox watch send-message react edit-message delete-message create-channel join-channel leave-channel invite kick edit-channel archive-channel delete-channel webhook api check-access pop status log completion commands help version --json --workspace -e --env --no-color --quiet --timeout --debug" -- "$cur"))
            ;;
        auth)
            COMPREPLY=($(compgen -W "login status logout token" -- "$cur"))
            ;;
        workspace)
            COMPREPLY=($(compgen -W "list switch" -- "$cur"))
            ;;
        search)
            COMPREPLY=($(compgen -W "channels dms users messages" -- "$cur"))
            ;;
        webhook)
            COMPREPLY=($(compgen -W "create list deliveries" -- "$cur"))
            ;;
        completion)
            COMPREPLY=($(compgen -W "bash zsh" -- "$cur"))
            ;;
        -e|--env)
            ;;
    esac
}
complete -F _popcorn_completions popcorn
""".strip()

_ZSH_COMPLETION = r"""
#compdef popcorn

_popcorn() {
    local -a commands
    commands=(
        'auth:Authentication commands'
        'workspace:Workspace commands'
        'env:Show or switch environment'
        'whoami:Show current user and workspace'
        'search:Search channels, DMs, users, or messages'
        'list-messages:Read message history'
        'list-threads:List threads in a channel'
        'get-message:Get a single message by ID'
        'info:Show conversation info and members'
        'inbox:Show notifications'
        'watch:Watch a channel for new messages'
        'send-message:Send a message'
        'react:React to a message'
        'edit-message:Edit a message'
        'delete-message:Delete a message'
        'create-channel:Create a channel'
        'join-channel:Join a channel'
        'leave-channel:Leave a channel'
        'invite:Invite users to a channel'
        'kick:Remove a user from a channel'
        'edit-channel:Update channel name or description'
        'archive-channel:Archive a channel'
        'delete-channel:Delete a channel'
        'webhook:Manage webhooks'
        'api:Raw API call'
        'check-access:Check repo access'
        'pop:Publish site resources to a channel'
        'completion:Generate shell completions'
    )

    _arguments \
        '--json[Output raw JSON]' \
        '--workspace[Override workspace ID]:workspace:' \
        {-e,--env}'[Profile name]:env:' \
        '--no-color[Disable colors]' \
        '1:command:->cmds' \
        '*::arg:->args'

    case "$state" in
        cmds) _describe 'command' commands ;;
        args)
            case "${words[1]}" in
                auth) _values 'subcommand' login status logout token ;;
                workspace) _values 'subcommand' list switch ;;
                search) _values 'type' channels dms users messages ;;
                webhook) _values 'subcommand' create list deliveries ;;
                completion) _values 'shell' bash zsh ;;
            esac
            ;;
    esac
}

_popcorn "$@"
""".strip()


def cmd_completion(args: argparse.Namespace) -> None:
    shell = args.shell
    if shell == "bash":
        print(_BASH_COMPLETION)
    elif shell == "zsh":
        print(_ZSH_COMPLETION)
    else:
        raise PopcornError(f"Unknown shell: {shell}. Supported: bash, zsh")


def _introspect_parser(parser: argparse.ArgumentParser) -> list[dict[str, Any]]:
    """Extract argument metadata from an argparse parser."""
    args_out: list[dict[str, Any]] = []
    for action in parser._actions:
        if isinstance(
            action,
            argparse._HelpAction | argparse._VersionAction | argparse._SubParsersAction,
        ):
            continue
        entry: dict[str, Any] = {}
        if action.option_strings:
            entry["flags"] = action.option_strings
        else:
            entry["name"] = action.dest
        entry["required"] = (
            action.required if action.option_strings else action.nargs not in ("?", "*")
        )
        if action.help and action.help != argparse.SUPPRESS:
            entry["help"] = action.help
        if action.type is not None:
            entry["type"] = getattr(action.type, "__name__", str(action.type))
        if isinstance(action, argparse._StoreConstAction):
            entry["type"] = "bool"
        if action.choices:
            entry["choices"] = list(action.choices)
        if action.default is not None and action.default != argparse.SUPPRESS:
            entry["default"] = action.default
        args_out.append(entry)
    return args_out


_COMMAND_CATEGORIES: dict[str, str] = {
    "pop": "sites",
    "status": "sites",
    "log": "sites",
    "send-message": "messages",
    "list-messages": "messages",
    "list-threads": "messages",
    "get-message": "messages",
    "edit-message": "messages",
    "delete-message": "messages",
    "react": "messages",
    "search": "messages",
    "inbox": "messages",
    "download": "messages",
    "watch": "messages",
    "create-channel": "channels",
    "info": "channels",
    "join-channel": "channels",
    "leave-channel": "channels",
    "invite": "channels",
    "kick": "channels",
    "edit-channel": "channels",
    "archive-channel": "channels",
    "delete-channel": "channels",
    "webhook": "webhooks",
    "auth": "auth",
    "workspace": "auth",
    "env": "auth",
    "whoami": "auth",
    "api": "other",
    "check-access": "other",
    "completion": "other",
    "commands": "other",
}

_COMMAND_DESCRIPTIONS: dict[str, str] = {
    "auth": "Authentication commands (login, logout, status, token)",
    "workspace": "List or switch workspaces",
    "env": "Show or switch environment/profile",
    "whoami": "Show current user and workspace",
    "inbox": "Show notifications (mentions, replies, reactions)",
    "search": "Search channels, DMs, users, or messages",
    "list-messages": "Read message history from a channel or thread",
    "list-threads": "List threads in a channel with reply counts",
    "info": "Show conversation info and members",
    "get-message": "Get a single message by ID",
    "download": "Download a file attachment",
    "watch": "Watch a channel for new messages (polling)",
    "send-message": "Send a message to a channel or DM",
    "react": "Add or remove an emoji reaction",
    "edit-message": "Edit a message",
    "delete-message": "Delete a message",
    "create-channel": "Create a channel",
    "join-channel": "Join a channel",
    "leave-channel": "Leave a channel",
    "invite": "Invite users to a channel",
    "kick": "Remove a user from a channel",
    "edit-channel": "Update channel name or description",
    "archive-channel": "Archive or unarchive a channel",
    "delete-channel": "Delete a channel",
    "webhook": "Manage webhooks (create, list, deliveries)",
    "api": "Raw API call (escape hatch, like gh api)",
    "pop": "Push site resources to a channel",
    "status": "Show site deployment status",
    "log": "Show site version history",
    "check-access": "Check repository access",
    "completion": "Generate shell completions (bash, zsh)",
    "commands": "Dump CLI schema as JSON for programmatic discovery",
}


def cmd_commands(_args: argparse.Namespace) -> None:
    """Dump full CLI schema as JSON for agent bootstrapping."""
    parser = build_parser()
    # Find the subparsers action
    sub_action = None
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            sub_action = action
            break

    commands: list[dict[str, Any]] = []
    if sub_action:
        for name, sub_parser in sub_action.choices.items():
            cmd: dict[str, Any] = {"name": name}
            if name in _COMMAND_CATEGORIES:
                cmd["category"] = _COMMAND_CATEGORIES[name]
            if name in _COMMAND_DESCRIPTIONS:
                cmd["description"] = _COMMAND_DESCRIPTIONS[name]
            # Check for nested subcommands (auth, workspace, webhook)
            nested_sub = None
            for act in sub_parser._actions:
                if isinstance(act, argparse._SubParsersAction):
                    nested_sub = act
                    break
            if nested_sub:
                # Build help text lookup from _choices_actions
                sub_help = {ca.dest: ca.help for ca in nested_sub._choices_actions if ca.help}
                subcmds = []
                for sub_name, sub_sub_parser in nested_sub.choices.items():
                    sub_entry: dict[str, Any] = {"name": sub_name}
                    sub_args = _introspect_parser(sub_sub_parser)
                    if sub_args:
                        sub_entry["arguments"] = sub_args
                    if sub_name in sub_help:
                        sub_entry["description"] = sub_help[sub_name]
                    subcmds.append(sub_entry)
                cmd["subcommands"] = subcmds
            else:
                cmd_args = _introspect_parser(sub_parser)
                if cmd_args:
                    cmd["arguments"] = cmd_args
            commands.append(cmd)

    global_flags = _introspect_parser(parser)

    schema = {
        "version": __version__,
        "global_flags": global_flags,
        "commands": commands,
    }
    print(json.dumps(schema, indent=2, default=str))  # No envelope — this IS the schema


# ---------------------------------------------------------------------------
# Argparse
# ---------------------------------------------------------------------------

# All known command names for fuzzy matching (includes subcommand parents)
_ALL_COMMAND_NAMES: list[str] = []  # populated after _COMMANDS is defined


class PopcornParser(argparse.ArgumentParser):
    """ArgumentParser that suggests close matches for invalid commands."""

    def error(self, message: str) -> None:  # type: ignore[override]
        # Intercept "argument <command>: invalid choice: 'xyz'"
        m = re.search(r"invalid choice: '([^']+)'", message)
        if m and _ALL_COMMAND_NAMES:
            bad = m.group(1)
            close = difflib.get_close_matches(bad, _ALL_COMMAND_NAMES, n=2, cutoff=0.6)
            if close:
                hint = " or ".join(f'"{c}"' for c in close)
                message = f'unknown command "{bad}". Did you mean {hint}?'
            else:
                message = f'unknown command "{bad}". Run "popcorn --help" for available commands.'
        super().error(message)


def build_parser() -> PopcornParser:
    epilog = """\
Sites:
  pop             Deploy site to a channel
  status          Show site deployment status
  log             Show site version history

Messages:
  send-message    Send a message
  list-messages   Read message history
  list-threads    List threads in a channel
  get-message     Get a single message by ID
  edit-message    Edit a message
  delete-message  Delete a message
  react           React to a message
  search          Search channels, DMs, users, or messages
  inbox           Show notifications
  download        Download a file attachment
  watch           Watch for new messages

Channels:
  create-channel  Create a channel
  info            Show channel info and members
  join-channel    Join a channel
  leave-channel   Leave a channel
  invite          Invite users to a channel
  kick            Remove a user from a channel
  edit-channel    Update channel name or description
  archive-channel Archive a channel
  delete-channel  Delete a channel

Webhooks:
  webhook         Manage webhooks

Auth & identity:
  auth            Authentication commands
  workspace       Workspace commands
  env             Show or switch environment
  whoami          Show current user and workspace

Other:
  api             Raw API call (like gh api)
  check-access    Check repo access
  completion      Generate shell completions
  commands        Dump CLI schema as JSON"""

    parser = PopcornParser(
        prog="popcorn",
        description="Popcorn CLI — command-line interface for Popcorn",
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"popcorn {__version__}")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--workspace", type=str, help="Override workspace ID")
    parser.add_argument("-e", "--env", type=str, help="Profile/environment name to use")
    parser.add_argument("--no-color", action="store_true", help="Disable color output")
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="Suppress informational stderr messages"
    )
    parser.add_argument(
        "--timeout", type=float, default=None, help="HTTP request timeout in seconds (default: 30)"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Log HTTP requests and responses to stderr (may include sensitive data)",
    )

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # --- Auth & identity ---
    _h = argparse.SUPPRESS  # hide from default subparser listing; epilog handles display

    auth_parser = sub.add_parser("auth", help=_h)
    auth_sub = auth_parser.add_subparsers(dest="auth_command")
    login_p = auth_sub.add_parser("login", help="Log in via browser OAuth")
    login_p.add_argument("-e", "--env", type=str, help="Profile name for this login")
    login_p.add_argument("--with-token", action="store_true", help="Read token from stdin")
    login_p.add_argument("--force", action="store_true", help="Re-authenticate")
    login_p.add_argument(
        "--workspace", type=str, help="Select workspace by name or ID (skips interactive prompt)"
    )
    auth_sub.add_parser("status", help="Show current auth status")
    auth_sub.add_parser("logout", help="Clear stored tokens")
    auth_sub.add_parser("token", help="Print auth token to stdout")

    ws_parser = sub.add_parser("workspace", help=_h)
    ws_sub = ws_parser.add_subparsers(dest="ws_command")
    ws_sub.add_parser("list", help="List available workspaces")
    switch_p = ws_sub.add_parser("switch", help="Switch active workspace")
    switch_p.add_argument("workspace", nargs="?", default=None, help="Workspace name or UUID")

    env_p = sub.add_parser("env", help=_h)
    env_p.add_argument("target_env", nargs="?", default=None, help="Profile name to switch to")

    sub.add_parser("whoami", help=_h)

    # --- Reading ---

    inbox_p = sub.add_parser("inbox", help=_h)
    inbox_grp = inbox_p.add_mutually_exclusive_group()
    inbox_grp.add_argument("--unread", action="store_true", help="Show only unread")
    inbox_grp.add_argument("--read", action="store_true", help="Show only read")
    inbox_p.add_argument("--limit", type=int, help="Max results (default 20)")

    search_p = sub.add_parser("search", help=_h)
    search_p.add_argument(
        "search_type", choices=["channels", "dms", "users", "messages"], help="What to search"
    )
    search_p.add_argument("query", nargs="?", default="", help="Search query")

    read_p = sub.add_parser("list-messages", help=_h)
    read_p.add_argument("conversation", help="Channel name (#general) or UUID")
    read_p.add_argument("--thread", type=str, help="Thread ID to read replies")
    read_p.add_argument("--limit", type=int, help="Max messages (default 25)")
    read_p.add_argument("--before", type=str, help="Message ID — show messages before this")
    read_p.add_argument("--after", type=str, help="Message ID — show messages after this")

    threads_p = sub.add_parser("list-threads", help=_h)
    threads_p.add_argument("conversation", help="Channel name (#general) or UUID")
    threads_p.add_argument("--limit", type=int, help="Max threads (default 50)")
    threads_p.add_argument("--offset", type=int, help="Pagination offset")

    info_p = sub.add_parser("info", help=_h)
    info_p.add_argument("conversation", help="Channel name (#general) or UUID")

    getmsg_p = sub.add_parser("get-message", help=_h)
    getmsg_p.add_argument("message_id", help="Message UUID")

    dl_p = sub.add_parser("download", help=_h)
    dl_p.add_argument("file_key", help="File key (from message media part URL field)")
    dl_p.add_argument("-o", "--output", type=str, help="Output path (default: original filename)")

    watch_p = sub.add_parser("watch", help=_h)
    watch_p.add_argument("conversation", help="Channel name (#general) or UUID")
    watch_p.add_argument(
        "--interval", type=int, default=3, help="Poll interval in seconds (default 3)"
    )
    watch_p.add_argument("--count", type=int, default=None, help="Exit after receiving N messages")
    watch_p.add_argument(
        "--max-wait",
        type=float,
        default=None,
        help="Exit after N seconds even if no messages received",
    )

    # --- Writing ---

    send_p = sub.add_parser("send-message", help=_h)
    send_p.add_argument(
        "conversation", nargs="?", default=None, help="Channel name (#general) or UUID"
    )
    send_p.add_argument("message", nargs="?", default=None, help='Message text (use "-" for stdin)')
    send_p.add_argument("--thread", type=str, help="Reply to thread ID")
    send_p.add_argument("--file", type=str, help="File path to upload and attach")
    send_p.add_argument(
        "--batch",
        action="store_true",
        help='Read NDJSON from stdin: {"conversation": "...", "message": "..."}',
    )
    send_p.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop batch processing on first error",
    )

    react_p = sub.add_parser("react", help=_h)
    react_p.add_argument("conversation", help="Channel name (#general) or UUID")
    react_p.add_argument("message_id", help="Message UUID")
    react_p.add_argument("emoji", help='Emoji (e.g. "thumbs up")')
    react_p.add_argument("--remove", action="store_true", help="Remove reaction instead of adding")

    edit_p = sub.add_parser("edit-message", help=_h)
    edit_p.add_argument("conversation", help="Channel name (#general) or UUID")
    edit_p.add_argument("message_id", help="Message UUID")
    edit_p.add_argument("content", help="New message content")

    del_p = sub.add_parser("delete-message", help=_h)
    del_p.add_argument("conversation", help="Channel name (#general) or UUID")
    del_p.add_argument("message_id", help="Message UUID")

    # --- Channel management ---

    create_p = sub.add_parser("create-channel", help=_h)
    create_p.add_argument("name", help="Channel name")
    create_p.add_argument(
        "--type",
        choices=["public_channel", "private_channel"],
        default="public_channel",
        help="Conversation type",
    )
    create_p.add_argument("--members", type=str, help="Comma-separated user IDs")
    create_p.add_argument(
        "--if-not-exists",
        action="store_true",
        help="Return existing channel instead of failing on duplicate name",
    )

    join_p = sub.add_parser("join-channel", help=_h)
    join_p.add_argument("conversation", help="Channel name (#general) or UUID")

    leave_p = sub.add_parser("leave-channel", help=_h)
    leave_p.add_argument("conversation", help="Channel name (#general) or UUID")

    invite_p = sub.add_parser("invite", help=_h)
    invite_p.add_argument("conversation", help="Channel name (#general) or UUID")
    invite_p.add_argument("user_ids", help="Comma-separated user IDs")

    kick_p = sub.add_parser("kick", help=_h)
    kick_p.add_argument("conversation", help="Channel name (#general) or UUID")
    kick_p.add_argument("user_id", help="User UUID to remove")

    update_p = sub.add_parser("edit-channel", help=_h)
    update_p.add_argument("conversation", help="Channel name (#general) or UUID")
    update_p.add_argument("--name", type=str, help="New name")
    update_p.add_argument("--description", type=str, help="New description")

    archive_p = sub.add_parser("archive-channel", help=_h)
    archive_p.add_argument("conversation", help="Channel name (#general) or UUID")
    archive_p.add_argument("--undo", action="store_true", help="Unarchive instead")

    delconv_p = sub.add_parser("delete-channel", help=_h)
    delconv_p.add_argument("conversation", help="Channel name (#general) or UUID")

    # --- Webhooks ---

    wh_parser = sub.add_parser("webhook", help=_h)
    wh_sub = wh_parser.add_subparsers(dest="webhook_command")
    wh_create = wh_sub.add_parser("create", help="Create a webhook")
    wh_create.add_argument("conversation", help="Channel name or UUID")
    wh_create.add_argument("name", help="Webhook name")
    wh_create.add_argument("--description", type=str, help="Webhook description")
    wh_create.add_argument("--avatar-url", type=str, help="Avatar URL")
    wh_create.add_argument(
        "--action-mode",
        type=str,
        choices=["silent", "as_is", "ai_enhanced"],
        help="How deliveries are processed",
    )
    wh_list = wh_sub.add_parser("list", help="List webhooks for a channel")
    wh_list.add_argument("conversation", help="Channel name or UUID")
    wh_del = wh_sub.add_parser("deliveries", help="List webhook deliveries")
    wh_del.add_argument("conversation", help="Channel name or UUID")
    wh_del.add_argument("--limit", type=int, default=50, help="Max results (1-100)")
    wh_del.add_argument("--since", type=str, help="ISO timestamp — deliveries after this")
    wh_del.add_argument("--status", type=str, help="Filter: completed,ignored,failed,processing")

    # --- Escape hatch ---

    api_p = sub.add_parser("api", help=_h)
    api_p.add_argument("path", help="API path (e.g. /api/users/me)")
    api_p.add_argument(
        "-X",
        "--method",
        type=str,
        default=None,
        help="HTTP method (default: GET, or POST if --data)",
    )
    api_p.add_argument("--data", "-d", type=str, help="JSON request body")
    api_p.add_argument(
        "-p",
        "--param",
        action="append",
        metavar="KEY=VALUE",
        help="Query parameter (repeatable, e.g. -p file_key=abc)",
    )
    api_p.add_argument(
        "--raw",
        action="store_true",
        help="Output raw JSON without envelope (even with --json)",
    )

    # --- Pop ---

    pop_p = sub.add_parser("pop", help=_h)
    pop_p.add_argument("name", nargs="?", default=None, help="Site name (default: pop-<dirname>)")
    pop_p.add_argument("--context", type=str, default="", help="Deploy context message")
    pop_p.add_argument("--force", "-f", action="store_true", help="Skip checks and prompts")
    pop_p.add_argument("--verbose", "-v", action="store_true", help="Print progress steps")

    # --- Site status & log ---

    status_p = sub.add_parser("status", help=_h)
    status_p.add_argument("channel", nargs="?", default=None, help="Channel name or UUID")

    log_p = sub.add_parser("log", help=_h)
    log_p.add_argument("channel", nargs="?", default=None, help="Channel name or UUID")
    log_p.add_argument("--limit", type=int, default=10, help="Max versions (default 10)")

    # --- Integrations ---

    check_ra_p = sub.add_parser("check-access", help=_h)
    check_ra_p.add_argument("repo", help="Repository (owner/repo)")

    # --- Shell & discovery ---

    comp_p = sub.add_parser("completion", help=_h)
    comp_p.add_argument("shell", choices=["bash", "zsh"], help="Shell type")

    sub.add_parser("commands", help=_h)
    sub.add_parser("help", help=_h)
    sub.add_parser("version", help=_h)

    # Hide the auto-generated subparser list — the epilog handles display
    sub._choices_actions = []
    for ag in parser._action_groups:
        ag._group_actions = [
            a for a in ag._group_actions if not isinstance(a, argparse._SubParsersAction)
        ]

    return parser


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

_COMMANDS = {
    "whoami": cmd_whoami,
    "search": cmd_search,
    "list-messages": cmd_list_messages,
    "list-threads": cmd_list_threads,
    "info": cmd_info,
    "send-message": cmd_send_message,
    "react": cmd_react,
    "edit-message": cmd_edit_message,
    "delete-message": cmd_delete_message,
    "get-message": cmd_get_message,
    "download": cmd_download,
    "create-channel": cmd_create_channel,
    "join-channel": cmd_join_channel,
    "leave-channel": cmd_leave_channel,
    "archive-channel": cmd_archive_channel,
    "invite": cmd_invite,
    "kick": cmd_kick,
    "edit-channel": cmd_edit_channel,
    "delete-channel": cmd_delete_channel,
    "inbox": cmd_inbox,
    "watch": cmd_watch,
    "env": cmd_env,
    "completion": cmd_completion,
    "api": cmd_api,
    "check-access": cmd_check_access,
    "pop": cmd_pop,
    "status": cmd_status,
    "log": cmd_log,
    "commands": cmd_commands,
}

# Populate fuzzy-match candidates: _COMMANDS keys + subcommand parents
_ALL_COMMAND_NAMES.extend([*_COMMANDS.keys(), "auth", "workspace", "webhook"])


def _hoist_global_flags(argv: list[str] | None = None) -> list[str]:
    """Move global flags to before the subcommand so they're parsed correctly.

    Allows both ``popcorn --json read ...`` and ``popcorn read --json ...``,
    and similarly for ``--quiet``/``-q`` and ``--timeout N``.
    """
    args = list(argv if argv is not None else sys.argv[1:])
    hoisted: list[str] = []

    # Boolean flags
    for flag in ("--json", "--quiet", "-q", "--debug"):
        if flag in args:
            hoisted.append(flag)
            args = [a for a in args if a != flag]

    # Value flags (--flag VALUE)
    for flag in ("--timeout",):
        if flag in args:
            idx = args.index(flag)
            hoisted.append(args[idx])
            if idx + 1 < len(args):
                hoisted.append(args[idx + 1])
                args = args[:idx] + args[idx + 2 :]
            else:
                args = args[:idx]

    return hoisted + args


def main() -> None:
    global _quiet

    parser = build_parser()
    args = parser.parse_args(_hoist_global_flags())

    _quiet = getattr(args, "quiet", False)

    set_color(
        sys.stdout.isatty()
        and not os.environ.get("NO_COLOR")
        and not getattr(args, "no_color", False)
        and not getattr(args, "json", False)
    )

    if not args.command or args.command == "help":
        parser.print_help()
        sys.exit(0)
    if args.command == "version":
        print(f"popcorn {__version__}")
        sys.exit(0)

    try:
        if args.command == "auth":
            sub = {
                "login": cmd_auth_login,
                "status": cmd_auth_status,
                "logout": cmd_auth_logout,
                "token": cmd_auth_token,
            }
            handler = sub.get(getattr(args, "auth_command", None) or "")
            if handler:
                handler(args)
            else:
                parser.parse_args(["auth", "--help"])
        elif args.command == "workspace":
            sub = {"list": cmd_workspace_list, "switch": cmd_workspace_switch}
            handler = sub.get(getattr(args, "ws_command", None) or "")
            if handler:
                handler(args)
            else:
                parser.parse_args(["workspace", "--help"])
        elif args.command == "webhook":
            cmd_webhook(args)
        elif args.command in _COMMANDS:
            _COMMANDS[args.command](args)
        else:
            parser.print_help()
    except PopcornError as e:
        if getattr(args, "json", False):
            print(_json_err(e.to_dict()), file=sys.stderr)
        else:
            msg = f"Error: {e}"
            if e.hint:
                msg += f"\n  Run: {e.hint}"
            print(msg, file=sys.stderr)
        sys.exit(e.exit_code)
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        sys.exit(EXIT_INTERRUPT)


if __name__ == "__main__":
    main()
