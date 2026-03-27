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


def fmt_vm_duration(seconds: float) -> str:
    """Format seconds as compact duration string."""
    seconds = int(seconds)
    if seconds >= 60:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds}s"


def fmt_vm_tokens(n: int) -> str:
    """Format token count as compact string."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def fmt_vm_cost(usd: float) -> str:
    """Format USD cost."""
    if 0 < usd < 0.01:
        return f"${usd:.3f}"
    return f"${usd:.2f}"


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


def fmt_vm_trace_event(event: dict, prev_timestamp: str | None = None) -> str | None:
    """Format a single trace event as a display line.

    Returns None for non-displayable events (turn_start, turn_end, etc.).
    """
    etype = event.get("type")
    if etype != "tool_call":
        return None

    tool = event.get("tool", "?")
    args = _compact_tool_args(tool, event.get("input", {}))
    ts = event.get("timestamp", "")

    # Compute duration from previous event
    dur_str = ""
    if prev_timestamp and ts:
        try:
            from datetime import datetime

            prev = datetime.fromisoformat(prev_timestamp.replace("Z", "+00:00"))
            curr = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            dur = (curr - prev).total_seconds()
            if dur > 0:
                dur_str = fmt_vm_duration(dur)
        except (ValueError, TypeError):
            pass

    # Format timestamp as HH:MM:SS
    time_str = ""
    if ts:
        try:
            from datetime import datetime

            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            time_str = dt.strftime("%H:%M:%S")
        except (ValueError, TypeError):
            pass

    parts = []
    if time_str:
        parts.append(dim(time_str))
    parts.append(f"{tool:<8s}")
    if args:
        parts.append(dim(args[:60]))
    if dur_str:
        parts.append(dim(dur_str))

    return "  " + "  ".join(parts)


def fmt_vm_trace(trace: dict) -> str:
    """Format a full execution trace for display."""
    lines: list[str] = []

    name = trace.get("name") or trace.get("item_id", "?")
    queue = trace.get("queue_id", "?")
    status = trace.get("status", "?")
    model = trace.get("model", "")
    duration = trace.get("duration_seconds")
    usage = trace.get("usage")

    # Header
    lines.append(bold(f"Trace: {name}"))
    header_parts = [f"Queue: {queue}", f"Status: {status}"]
    if model:
        header_parts.append(f"Model: {model}")
    lines.append("  |  ".join(header_parts))

    if duration or usage:
        meta_parts = []
        if duration:
            meta_parts.append(f"Duration: {fmt_vm_duration(duration)}")
        if usage:
            meta_parts.append(f"Cost: {fmt_vm_cost(usage.get('total_cost_usd', 0))}")
        lines.append("  |  ".join(meta_parts))

    lines.append("")

    # Prompt
    prompt = trace.get("prompt", "")
    if prompt:
        lines.append(bold("Prompt:"))
        for pline in prompt.split("\n"):
            lines.append(f"  {pline}")
        lines.append("")

    # Tool calls
    events = trace.get("events", [])
    tool_events = [e for e in events if e.get("type") == "tool_call"]
    if tool_events:
        lines.append(bold(f"Tool calls ({len(tool_events)}):"))
        prev_ts = None
        for i, event in enumerate(tool_events):
            line = fmt_vm_trace_event(event, prev_ts)
            if line:
                lines.append(f"  {i + 1:>3}. {line.strip()}")
            prev_ts = event.get("timestamp")
        lines.append("")

    # Files written
    files = trace.get("files_written", [])
    if files:
        lines.append(bold("Files written:"))
        for f in files:
            lines.append(f"  {f}")
        lines.append("")

    # Error
    error = trace.get("error")
    if error:
        lines.append(yellow(bold("Error:")))
        lines.append(f"  {error}")
        lines.append("")

    # Result text
    text_output = trace.get("text_output", "")
    if text_output:
        lines.append(bold("Result:"))
        truncated = text_output[:500]
        if len(text_output) > 500:
            truncated += "..."
        for rline in truncated.split("\n"):
            lines.append(f"  {rline}")
        lines.append("")

    # Usage summary
    if usage:
        inp = usage.get("input_tokens", 0)
        out = usage.get("output_tokens", 0)
        cache_r = usage.get("cache_read_tokens", 0)
        cache_w = usage.get("cache_write_tokens", 0)
        total_input = inp + cache_r + cache_w
        cache_rate = f"{cache_r / total_input * 100:.1f}%" if total_input > 0 else "0%"
        lines.append(
            f"Tokens: {fmt_vm_tokens(inp)} in / {fmt_vm_tokens(out)} out"
            + (f" / {fmt_vm_tokens(cache_r)} cache read" if cache_r else "")
            + f"  |  Cache hit: {cache_rate}"
        )

    return "\n".join(lines)


def fmt_vm_trace_list(queue_id: str, items: list[dict]) -> str:
    """Format a list of recent trace items for display."""
    lines: list[str] = []
    lines.append(bold(f"Recent items for {queue_id}:"))
    lines.append("")

    if not items:
        lines.append("  (none)")
        return "\n".join(lines)

    # Header
    lines.append(
        dim(
            f"  {'ID':<14s} {'Name':<34s} {'Status':<12s}"
            f" {'Cost':<10s} {'Duration':<10s} {'Completed'}"
        )
    )

    for item in items:
        item_id = str(item.get("item_id", "?"))[:12]
        name = (item.get("name") or item_id)[:32]
        status = item.get("status", "?")
        cost = fmt_vm_cost(item.get("cost", 0))
        dur = fmt_vm_duration(item.get("duration_seconds", 0))
        completed = format_timestamp(item.get("completed_at"))
        lines.append(
            f"  {item_id:<14s} {name:<34s} {status:<12s} {cost:<10s} {dur:<10s} {completed}"
        )

    return "\n".join(lines)


def fmt_vm_monitor(data: dict) -> str:
    """Format monitor snapshot for display."""
    lines: list[str] = []
    workers = data.get("workers", [])
    items = data.get("items", [])
    total_cost = data.get("total_cost", 0)

    if workers:
        lines.append(bold("Workers:"))
        for w in workers:
            wid = w.get("id", "?")
            pid = w.get("pid", "?")
            uptime = fmt_vm_duration(w.get("uptime_seconds", 0))
            state = w.get("state", "idle")
            lines.append(f"  {wid:<20s} pid {pid:<8} up {uptime:<8s} {state}")
        lines.append("")

    if items:
        lines.append(bold("Active items:"))
        for item in items:
            qid = item.get("queue_id", "?")
            iid = str(item.get("item_id", ""))
            name = item.get("name") or iid
            turn = item.get("turn", 0)
            cost = fmt_vm_cost(item.get("cost", 0))
            elapsed = fmt_vm_duration(item.get("elapsed_seconds", 0))
            status = item.get("status", "?")
            if status == "processing":
                lines.append(f"  {qid}/{iid:<12s} {name:<30s} turn {turn:<4} {cost:<8s} {elapsed}")
            else:
                lines.append(f"  {qid}/{iid:<12s} {name:<30s} {status}")
        lines.append("")
        lines.append(f"Total cost: {fmt_vm_cost(total_cost)}")
    elif not workers:
        lines.append("No active workers or items.")

    return "\n".join(lines)


def fmt_vm_usage(data: dict) -> str:
    """Format usage analytics for display."""
    lines: list[str] = []
    total = data.get("total", {})
    by_queue = data.get("by_queue", {})
    by_model = data.get("by_model", {})

    count = total.get("count", 0)
    cost = total.get("total_cost_usd", 0)
    lines.append(bold("Usage:"))
    lines.append(f"  Tasks: {count}  |  Total cost: {fmt_vm_cost(cost)}")
    lines.append("")

    if by_queue:
        lines.append(bold("  By channel:"))
        for qid, info in by_queue.items():
            qcount = info.get("count", 0)
            qcost = fmt_vm_cost(info.get("cost", 0))
            lines.append(f"    {qid:<24s} {qcount:>4} tasks   {qcost}")
        lines.append("")

    if by_model:
        lines.append(bold("  By model:"))
        for model, info in by_model.items():
            mcount = info.get("count", 0)
            mcost = fmt_vm_cost(info.get("cost", 0))
            lines.append(f"    {model:<24s} {mcount:>4} tasks   {mcost}")
        lines.append("")

    # Token summary
    inp = total.get("input_tokens", 0)
    out = total.get("output_tokens", 0)
    cache_r = total.get("cache_read_tokens", 0)
    if inp or out:
        lines.append(
            f"  Tokens: {fmt_vm_tokens(inp)} in / {fmt_vm_tokens(out)} out"
            + (f" / {fmt_vm_tokens(cache_r)} cache read" if cache_r else "")
        )
    cache_rate = total.get("cache_hit_rate", 0)
    cache_savings = total.get("cache_savings_usd", 0)
    if cache_rate:
        lines.append(
            f"  Cache hit rate: {cache_rate}%  |  Cache savings: {fmt_vm_cost(cache_savings)}"
        )

    return "\n".join(lines)
