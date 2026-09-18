"""Output formatting for Popcorn messages, conversations, users, and activities."""

from __future__ import annotations

from datetime import datetime
from typing import Any

# Color state — set by main() after arg parsing
_color_enabled = False


def set_color(enabled: bool) -> None:
    """Enable or disable color output."""
    global _color_enabled
    _color_enabled = enabled


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _color_enabled else s


def dim(s: str) -> str:
    return _c("2", s)


def bold(s: str) -> str:
    return _c("1", s)


def cyan(s: str) -> str:
    return _c("36", s)


def yellow(s: str) -> str:
    return _c("33", s)


def green(s: str) -> str:
    return _c("32", s)


def format_timestamp(iso_str: str | None) -> str:
    """Format ISO timestamp as YYYY-MM-DD HH:MM."""
    if not iso_str:
        return "????"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return iso_str[:16]


def format_author(msg: dict[str, Any]) -> str:
    """Extract author display name from message."""
    author = msg.get("author") or {}
    return author.get("display_name") or author.get("username") or author.get("email") or "Unknown"


def format_message_text(msg: dict[str, Any]) -> str:
    """Extract text content from message content dict."""
    content = msg.get("content") or {}
    parts = content.get("parts") or []
    texts = []
    for part in parts:
        if isinstance(part, dict):
            ptype = part.get("type")
            if ptype == "text":
                texts.append(part.get("content", ""))
            elif ptype == "media":
                fname = part.get("filename") or "file"
                texts.append(dim(f"[{fname}]"))
            elif ptype in ("file", "integration"):
                fname = part.get("filename") or part.get("title") or part.get("name") or "file"
                texts.append(dim(f"[{fname}]"))
            elif ptype == "system":
                texts.append(dim(f"(system: {part.get('content', '')})"))
    return " ".join(texts) if texts else dim("(no text content)")


def fmt_message(msg: dict[str, Any]) -> str:
    """Format a single message for display."""
    ts = format_timestamp(msg.get("created_at"))
    author = format_author(msg)
    text = format_message_text(msg)
    msg_id = str(msg.get("id", "?"))
    line = f"{dim('[' + ts + ']')} {dim('(id: ' + msg_id + ')')} {bold(author)}: {text}"

    reply_count = msg.get("reply_count") or msg.get("thread_reply_count") or 0
    if reply_count > 0:
        line += f"\n  \u21b3 {reply_count} replies"

    reactions = msg.get("reactions")
    if reactions:
        rxn_parts = []
        if isinstance(reactions, dict):
            for emoji, users in reactions.items():
                count = len(users) if isinstance(users, list) else 1
                rxn_parts.append(f"{emoji} {count}" if count > 1 else emoji)
        elif isinstance(reactions, list):
            for rxn in reactions:
                if isinstance(rxn, dict):
                    emoji = rxn.get("emoji", "?")
                    count = rxn.get("count", 1)
                    rxn_parts.append(f"{emoji} {count}" if count > 1 else emoji)
        if rxn_parts:
            line += f"\n  [{' '.join(rxn_parts)}]"

    return line


def app_type(conv: dict[str, Any]) -> str:
    """The channel's installed app type, or "" when it runs no app bundle.

    Written at install time to the conversation's ``metadata['app_type']``, so a
    channel that never had a bundle installed — and one whose bundle was
    published without an ``app_type`` declared, which clears the tag — both read
    as untagged here. `popcorn app list --channel <ch>` is the authoritative
    answer for a single channel; this is the cheap one that comes back with
    every channel in a list.
    """
    metadata = conv.get("metadata")
    if not isinstance(metadata, dict):
        return ""
    return str(metadata.get("app_type") or "")


def fmt_conversation(conv: dict[str, Any]) -> str:
    """Format a conversation for display."""
    name = conv.get("name") or "Unnamed"
    conv_type = conv.get("type") or conv.get("conversation_type") or ""
    cid = conv.get("id", "?")
    desc = conv.get("description") or ""

    if conv_type in ("dm", "group_dm"):
        participants = conv.get("other_participants") or []
        names = [p.get("display_name") or p.get("username") or "?" for p in participants]
        label = ", ".join(names) if names else name
        return f"  {bold(label)} {dim('(id: ' + cid + ')')}"
    else:
        prefix = "#" if "channel" in conv_type else ""
        line = f"  {cyan(prefix + name)} {dim('(id: ' + cid + ')')}"
        app = app_type(conv)
        if app:
            line += f" {yellow('[' + app + ']')}"
        if desc:
            line += f" \u2014 {desc[:80]}"
        return line


def fmt_user(user: dict[str, Any]) -> str:
    """Format a user for display."""
    name = user.get("display_name") or user.get("username") or "Unknown"
    uid = user.get("id", "?")
    email = user.get("email", "")
    line = f"  {name} (id: {uid})"
    if email:
        line += f" <{email}>"
    return line


def fmt_activity(act: dict[str, Any]) -> str:
    """Format an activity/notification for display."""
    ts = format_timestamp(act.get("last_message_at"))
    is_read = act.get("is_read", False)
    marker = "" if is_read else yellow(" [unread]")

    msg = act.get("display_message")
    if msg:
        snippet = format_message_text(msg)[:150]
        author = format_author(msg)
        label = f"{author}: {snippet}"
    else:
        label = "(no message)"

    conv_id = act.get("conversation_id", "")
    thread_id = act.get("thread_id", "")
    parts = [f"conversation: {conv_id}"]
    if thread_id:
        parts.append(f"thread: {thread_id}")

    return f"  [{ts}]{marker} {label} ({', '.join(parts)})"


# ---------------------------------------------------------------------------
# VM formatting
# ---------------------------------------------------------------------------


def _compact_tool_args(tool: str, inp: dict) -> str:
    """Extract compact summary of tool call arguments."""
    if not isinstance(inp, dict):
        return ""
    t = tool.lower()
    if t == "bash":
        return (inp.get("command") or "")[:120]
    if t in ("read", "write", "edit"):
        path = inp.get("path") or inp.get("file_path") or ""
        if path:
            parts = path.rsplit("/", 2)
            return "/".join(parts[-2:]) if len(parts) > 2 else path
        return ""
    if t in ("glob", "grep"):
        return (inp.get("pattern") or "")[:60]
    for v in inp.values():
        if isinstance(v, str) and v:
            return v[:60]
    return ""
