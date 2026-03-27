# VM Commands Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `popcorn vm {trace, monitor, usage, cancel, rollback}` commands for inspecting and managing workspace VM agent execution.

**Architecture:** Five new CLI commands under a `vm` subparser group. Business logic in `operations.py`, command handlers in `cli.py`, display helpers in `formatting.py`. All commands hit existing backend proxy endpoints (`/api/appchannels/...`) — no backend changes needed.

**Tech Stack:** Python, argparse, httpx (via existing APIClient)

**Spec:** `docs/superpowers/specs/2026-03-27-vm-commands-design.md`

---

## File Structure

| File | Role | Action |
|------|------|--------|
| `src/popcorn_core/operations.py` | VM business logic (API calls) | Modify: add `vm_*` functions at end |
| `src/popcorn_cli/formatting.py` | VM display helpers | Modify: add `fmt_vm_*` functions at end |
| `src/popcorn_cli/cli.py` | Command handlers + argparse | Modify: add `cmd_vm_*` handlers + `vm` subparser group |
| `tests/test_vm_operations.py` | Unit tests for operations | Create |
| `tests/test_vm_formatting.py` | Unit tests for formatting | Create |
| `tests/test_vm_parser.py` | Parser tests for vm subcommands | Create |

---

### Task 1: VM operations — trace, trace_list, monitor, usage

**Files:**
- Modify: `src/popcorn_core/operations.py` (add functions at end)
- Create: `tests/test_vm_operations.py`

- [ ] **Step 1: Write failing tests for vm operations**

Create `tests/test_vm_operations.py`:

```python
"""Tests for VM operations."""

from __future__ import annotations

from popcorn_core import operations


class TestVmMonitor:
    def test_vm_monitor(self, mock_client):
        mock_client.get.return_value = {
            "workers": [{"id": "my-channel", "state": "idle"}],
            "items": [],
            "total_cost": 0.0,
        }
        result = operations.vm_monitor(mock_client)
        mock_client.get.assert_called_once_with(
            "/api/appchannels/monitor", {}
        )
        assert result["workers"][0]["id"] == "my-channel"


class TestVmUsage:
    def test_vm_usage_defaults(self, mock_client):
        mock_client.get.return_value = {
            "total": {"count": 5, "total_cost_usd": 1.23},
            "by_queue": {},
            "by_model": {},
        }
        result = operations.vm_usage(mock_client)
        mock_client.get.assert_called_once_with(
            "/api/appchannels/usage", {}
        )
        assert result["total"]["count"] == 5

    def test_vm_usage_with_filters(self, mock_client):
        mock_client.get.return_value = {"total": {"count": 2}}
        operations.vm_usage(mock_client, hours=6, queue="my-channel", limit=5)
        mock_client.get.assert_called_once_with(
            "/api/appchannels/usage",
            {"hours": 6, "queue": "my-channel", "limit": 5},
        )


class TestVmTraceList:
    def test_vm_trace_list(self, mock_client):
        mock_client.get.return_value = {
            "recent_items": [
                {"item_id": "abc", "queue_id": "my-channel", "name": "build hero"}
            ],
            "recent_items_total": 1,
        }
        result = operations.vm_trace_list(mock_client, "my-channel")
        mock_client.get.assert_called_once_with(
            "/api/appchannels/usage",
            {"queue": "my-channel", "limit": 10},
        )
        assert result["recent_items"][0]["item_id"] == "abc"

    def test_vm_trace_list_custom_limit(self, mock_client):
        mock_client.get.return_value = {"recent_items": [], "recent_items_total": 0}
        operations.vm_trace_list(mock_client, "ch", limit=5)
        mock_client.get.assert_called_once_with(
            "/api/appchannels/usage",
            {"queue": "ch", "limit": 5},
        )


class TestVmTrace:
    def test_vm_trace(self, mock_client):
        mock_client.get.return_value = {
            "item_id": "abc",
            "queue_id": "my-channel",
            "name": "build hero",
            "status": "complete",
            "prompt": "Build a hero section",
            "events": [{"type": "tool_call", "tool": "Read"}],
        }
        result = operations.vm_trace(mock_client, "my-channel", "abc")
        mock_client.get.assert_called_once_with(
            "/api/appchannels/trace/my-channel/abc", {}
        )
        assert result["status"] == "complete"

    def test_vm_trace_latest(self, mock_client):
        """When no item_id given, fetch latest from usage then get trace."""
        mock_client.get.side_effect = [
            {
                "recent_items": [
                    {"item_id": "latest-id", "queue_id": "ch", "name": "task"}
                ],
            },
            {
                "item_id": "latest-id",
                "queue_id": "ch",
                "status": "complete",
                "prompt": "do thing",
                "events": [],
            },
        ]
        result = operations.vm_trace_latest(mock_client, "ch")
        assert mock_client.get.call_count == 2
        assert result["item_id"] == "latest-id"

    def test_vm_trace_latest_with_status(self, mock_client):
        """Filter latest by status."""
        mock_client.get.side_effect = [
            {
                "recent_items": [
                    {"item_id": "a", "queue_id": "ch", "status": "complete"},
                    {"item_id": "b", "queue_id": "ch", "status": "failed"},
                ],
            },
            {
                "item_id": "b",
                "queue_id": "ch",
                "status": "failed",
                "prompt": "oops",
                "events": [],
            },
        ]
        result = operations.vm_trace_latest(mock_client, "ch", status="failed")
        assert result["item_id"] == "b"

    def test_vm_trace_latest_no_items(self, mock_client):
        mock_client.get.return_value = {"recent_items": []}
        result = operations.vm_trace_latest(mock_client, "ch")
        assert result is None


class TestVmCancel:
    def test_vm_cancel(self, mock_client):
        mock_client.post.return_value = {"ok": True}
        result = operations.vm_cancel(mock_client, "my-channel", "abc")
        mock_client.post.assert_called_once_with(
            "/api/appchannels/queues/my-channel/items/abc/cancel"
        )
        assert result["ok"] is True

    def test_vm_cancel_latest(self, mock_client):
        """Find processing item from monitor, then cancel it."""
        mock_client.get.return_value = {
            "items": [
                {"queue_id": "ch", "item_id": "proc-1", "status": "processing"},
            ],
        }
        mock_client.post.return_value = {"ok": True}
        result = operations.vm_cancel_current(mock_client, "ch")
        mock_client.post.assert_called_once_with(
            "/api/appchannels/queues/ch/items/proc-1/cancel"
        )
        assert result is not None

    def test_vm_cancel_latest_no_processing(self, mock_client):
        mock_client.get.return_value = {"items": []}
        result = operations.vm_cancel_current(mock_client, "ch")
        assert result is None


class TestVmRollback:
    def test_vm_rollback(self, mock_client):
        mock_client.post.return_value = {"ok": True, "version": 3}
        result = operations.vm_rollback(mock_client, "my-channel")
        mock_client.post.assert_called_once_with(
            "/api/appchannels/sites/my-channel/rollback",
            data={},
        )
        assert result["version"] == 3

    def test_vm_rollback_specific_version(self, mock_client):
        mock_client.post.return_value = {"ok": True, "version": 2}
        result = operations.vm_rollback(mock_client, "my-channel", version=2)
        mock_client.post.assert_called_once_with(
            "/api/appchannels/sites/my-channel/rollback",
            data={"version": 2},
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/popcorn-cli && uv run pytest tests/test_vm_operations.py -v`
Expected: FAIL — `operations` has no `vm_*` functions

- [ ] **Step 3: Implement VM operations**

Add to end of `src/popcorn_core/operations.py`:

```python
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


def vm_trace_list(
    client: APIClient, queue_id: str, limit: int = 10
) -> dict[str, Any]:
    """List recent work items for a queue (from usage endpoint)."""
    return client.get(
        "/api/appchannels/usage",
        {"queue": queue_id, "limit": limit},
    )


def vm_trace(
    client: APIClient, queue_id: str, item_id: str
) -> dict[str, Any]:
    """Fetch full execution trace for a work item."""
    return client.get(f"/api/appchannels/trace/{queue_id}/{item_id}", {})


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
    return client.get(f"/api/appchannels/trace/{queue_id}/{item_id}", {})


def vm_cancel(
    client: APIClient, queue_id: str, item_id: str
) -> dict[str, Any]:
    """Cancel a specific work item."""
    return client.post(f"/api/appchannels/queues/{queue_id}/items/{item_id}/cancel")


def vm_cancel_current(
    client: APIClient, queue_id: str
) -> dict[str, Any] | None:
    """Cancel the currently processing item in a queue.

    Returns the cancel response, or None if no processing item found.
    """
    monitor = client.get("/api/appchannels/monitor", {})
    items = monitor.get("items", [])
    processing = [
        i for i in items
        if i.get("queue_id") == queue_id and i.get("status") == "processing"
    ]
    if not processing:
        return None
    item_id = processing[0]["item_id"]
    return client.post(f"/api/appchannels/queues/{queue_id}/items/{item_id}/cancel")


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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/popcorn-cli && uv run pytest tests/test_vm_operations.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
cd ~/popcorn-cli
git add src/popcorn_core/operations.py tests/test_vm_operations.py
git commit -m "feat: add VM operations (trace, monitor, usage, cancel, rollback)"
```

---

### Task 2: VM formatting helpers

**Files:**
- Modify: `src/popcorn_cli/formatting.py` (add functions at end)
- Create: `tests/test_vm_formatting.py`

- [ ] **Step 1: Write failing tests for formatting**

Create `tests/test_vm_formatting.py`:

```python
"""Tests for VM formatting helpers."""

from __future__ import annotations

from popcorn_cli.formatting import (
    fmt_vm_cost,
    fmt_vm_duration,
    fmt_vm_monitor,
    fmt_vm_tokens,
    fmt_vm_trace,
    fmt_vm_trace_event,
    fmt_vm_trace_list,
    fmt_vm_usage,
)


class TestFmtVmDuration:
    def test_seconds(self):
        assert fmt_vm_duration(45) == "45s"

    def test_minutes(self):
        assert fmt_vm_duration(154) == "2m 34s"

    def test_zero(self):
        assert fmt_vm_duration(0) == "0s"


class TestFmtVmTokens:
    def test_small(self):
        assert fmt_vm_tokens(500) == "500"

    def test_thousands(self):
        assert fmt_vm_tokens(45200) == "45.2k"

    def test_millions(self):
        assert fmt_vm_tokens(1200000) == "1.2M"


class TestFmtVmCost:
    def test_cost(self):
        assert fmt_vm_cost(0.0847) == "$0.08"

    def test_small_cost(self):
        assert fmt_vm_cost(0.001) == "$0.001"

    def test_zero(self):
        assert fmt_vm_cost(0) == "$0.00"


class TestFmtVmTraceEvent:
    def test_tool_call(self):
        event = {
            "type": "tool_call",
            "tool": "Read",
            "input": {"file_path": "/app/sites/my-channel/index.html"},
            "timestamp": "2026-03-27T14:23:01Z",
        }
        line = fmt_vm_trace_event(event, prev_timestamp=None)
        assert "Read" in line

    def test_non_tool_event_returns_none(self):
        event = {"type": "turn_start", "turn": 1}
        assert fmt_vm_trace_event(event, prev_timestamp=None) is None


class TestFmtVmTrace:
    def test_basic_trace(self):
        trace = {
            "name": "build hero",
            "queue_id": "my-channel",
            "status": "complete",
            "model": "claude-sonnet-4-20250514",
            "duration_seconds": 154,
            "prompt": "Build a hero section",
            "events": [
                {"type": "tool_call", "tool": "Read", "input": {}, "timestamp": "2026-03-27T14:23:01Z"},
            ],
            "files_written": ["index.html"],
            "text_output": "Built the hero section.",
            "usage": {
                "input_tokens": 45200,
                "output_tokens": 3100,
                "cache_read_tokens": 128400,
                "cache_write_tokens": 0,
                "total_cost_usd": 0.0847,
            },
        }
        output = fmt_vm_trace(trace)
        assert "build hero" in output
        assert "complete" in output
        assert "Build a hero section" in output
        assert "index.html" in output

    def test_trace_no_usage(self):
        trace = {
            "name": "test",
            "queue_id": "ch",
            "status": "failed",
            "prompt": "do thing",
            "events": [],
            "error": "something broke",
        }
        output = fmt_vm_trace(trace)
        assert "failed" in output
        assert "something broke" in output


class TestFmtVmTraceList:
    def test_trace_list(self):
        items = [
            {
                "item_id": "abc123",
                "name": "build hero",
                "status": "complete",
                "cost": 0.08,
                "duration_seconds": 154,
                "completed_at": "2026-03-27T14:25:35Z",
            },
        ]
        output = fmt_vm_trace_list("my-channel", items)
        assert "build hero" in output
        assert "abc123" in output


class TestFmtVmMonitor:
    def test_monitor_with_workers(self):
        data = {
            "workers": [
                {"id": "my-channel", "pid": 1234, "uptime_seconds": 720, "state": "build hero [Edit]"},
            ],
            "items": [
                {
                    "queue_id": "my-channel",
                    "item_id": "abc",
                    "name": "build hero",
                    "turn": 8,
                    "cost": 0.06,
                    "elapsed_seconds": 132,
                    "status": "processing",
                },
            ],
            "total_cost": 0.06,
        }
        output = fmt_vm_monitor(data)
        assert "my-channel" in output
        assert "build hero" in output

    def test_monitor_empty(self):
        data = {"workers": [], "items": [], "total_cost": 0}
        output = fmt_vm_monitor(data)
        assert "No active" in output or "idle" in output.lower() or output  # graceful empty


class TestFmtVmUsage:
    def test_usage(self):
        data = {
            "total": {
                "count": 47,
                "input_tokens": 1200000,
                "output_tokens": 89000,
                "cache_read_tokens": 4100000,
                "cache_write_tokens": 50000,
                "total_cost_usd": 3.82,
                "cache_hit_rate": 72.3,
                "cache_savings_usd": 1.24,
            },
            "by_queue": {
                "my-channel": {"count": 23, "cost": 2.14},
                "other": {"count": 18, "cost": 1.42},
            },
            "by_model": {
                "claude-sonnet": {"count": 41, "cost": 2.90},
            },
        }
        output = fmt_vm_usage(data)
        assert "47" in output
        assert "$3.82" in output
        assert "my-channel" in output
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/popcorn-cli && uv run pytest tests/test_vm_formatting.py -v`
Expected: FAIL — import errors

- [ ] **Step 3: Implement VM formatting helpers**

Add to end of `src/popcorn_cli/formatting.py`:

```python
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


def fmt_vm_trace_event(
    event: dict, prev_timestamp: str | None = None
) -> str | None:
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
        cache_rate = (
            f"{cache_r / total_input * 100:.1f}%" if total_input > 0 else "0%"
        )
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
        dim(f"  {'ID':<14s} {'Name':<34s} {'Status':<12s} {'Cost':<10s} {'Duration':<10s} {'Completed'}")
    )

    for item in items:
        item_id = str(item.get("item_id", "?"))[:12]
        name = (item.get("name") or item_id)[:32]
        status = item.get("status", "?")
        cost = fmt_vm_cost(item.get("cost", 0))
        dur = fmt_vm_duration(item.get("duration_seconds", 0))
        completed = format_timestamp(item.get("completed_at"))
        lines.append(f"  {item_id:<14s} {name:<34s} {status:<12s} {cost:<10s} {dur:<10s} {completed}")

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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/popcorn-cli && uv run pytest tests/test_vm_formatting.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
cd ~/popcorn-cli
git add src/popcorn_cli/formatting.py tests/test_vm_formatting.py
git commit -m "feat: add VM formatting helpers (trace, monitor, usage display)"
```

---

### Task 3: CLI command handlers and argparse

**Files:**
- Modify: `src/popcorn_cli/cli.py` (add handlers + `vm` subparser)
- Create: `tests/test_vm_parser.py`

- [ ] **Step 1: Write failing parser tests**

Create `tests/test_vm_parser.py`:

```python
"""Tests for VM subcommand parsing."""

from __future__ import annotations

import pytest

from popcorn_cli.cli import build_parser


@pytest.fixture()
def parser():
    return build_parser()


class TestVmTrace:
    def test_trace_channel_only(self, parser):
        args = parser.parse_args(["vm", "trace", "my-channel"])
        assert args.command == "vm"
        assert args.vm_command == "trace"
        assert args.channel == "my-channel"
        assert args.item_id is None

    def test_trace_with_item(self, parser):
        args = parser.parse_args(["vm", "trace", "my-channel", "abc123"])
        assert args.channel == "my-channel"
        assert args.item_id == "abc123"

    def test_trace_list(self, parser):
        args = parser.parse_args(["vm", "trace", "my-channel", "--list"])
        assert args.list is True

    def test_trace_watch(self, parser):
        args = parser.parse_args(["vm", "trace", "my-channel", "--watch"])
        assert args.watch is True

    def test_trace_status_filter(self, parser):
        args = parser.parse_args(["vm", "trace", "my-channel", "--status", "failed"])
        assert args.status == "failed"

    def test_trace_raw(self, parser):
        args = parser.parse_args(["vm", "trace", "my-channel", "--raw"])
        assert args.raw is True

    def test_trace_limit(self, parser):
        args = parser.parse_args(["vm", "trace", "my-channel", "--list", "--limit", "5"])
        assert args.limit == 5


class TestVmMonitor:
    def test_monitor_default(self, parser):
        args = parser.parse_args(["vm", "monitor"])
        assert args.command == "vm"
        assert args.vm_command == "monitor"

    def test_monitor_watch(self, parser):
        args = parser.parse_args(["vm", "monitor", "--watch"])
        assert args.watch is True

    def test_monitor_interval(self, parser):
        args = parser.parse_args(["vm", "monitor", "--watch", "-n", "10"])
        assert args.interval == 10

    def test_monitor_raw(self, parser):
        args = parser.parse_args(["vm", "monitor", "--raw"])
        assert args.raw is True


class TestVmUsage:
    def test_usage_default(self, parser):
        args = parser.parse_args(["vm", "usage"])
        assert args.command == "vm"
        assert args.vm_command == "usage"

    def test_usage_hours(self, parser):
        args = parser.parse_args(["vm", "usage", "--hours", "6"])
        assert args.hours == 6.0

    def test_usage_days(self, parser):
        args = parser.parse_args(["vm", "usage", "--days", "7"])
        assert args.days == 7

    def test_usage_queue(self, parser):
        args = parser.parse_args(["vm", "usage", "--queue", "my-channel"])
        assert args.queue == "my-channel"

    def test_usage_raw(self, parser):
        args = parser.parse_args(["vm", "usage", "--raw"])
        assert args.raw is True


class TestVmCancel:
    def test_cancel_channel(self, parser):
        args = parser.parse_args(["vm", "cancel", "my-channel"])
        assert args.vm_command == "cancel"
        assert args.channel == "my-channel"

    def test_cancel_with_item(self, parser):
        args = parser.parse_args(["vm", "cancel", "my-channel", "--item", "abc123"])
        assert args.item == "abc123"


class TestVmRollback:
    def test_rollback_channel(self, parser):
        args = parser.parse_args(["vm", "rollback", "my-channel"])
        assert args.vm_command == "rollback"
        assert args.channel == "my-channel"

    def test_rollback_version(self, parser):
        args = parser.parse_args(["vm", "rollback", "my-channel", "--version", "3"])
        assert args.version == 3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/popcorn-cli && uv run pytest tests/test_vm_parser.py -v`
Expected: FAIL — no `vm` subparser

- [ ] **Step 3: Add `vm` subparser group to `build_parser()`**

In `src/popcorn_cli/cli.py`, inside `build_parser()`, before the `# --- Shell & discovery ---` section (around line 2310), add:

```python
    # --- VM (workspace VM introspection) ---

    vm_parser = sub.add_parser("vm", help=_h)
    vm_sub = vm_parser.add_subparsers(dest="vm_command")

    vm_trace_p = vm_sub.add_parser("trace", help="Show agent execution trace")
    vm_trace_p.add_argument("channel", help="Channel/site name")
    vm_trace_p.add_argument("item_id", nargs="?", default=None, help="Specific item ID")
    vm_trace_p.add_argument("--list", action="store_true", help="List recent items")
    vm_trace_p.add_argument("--watch", action="store_true", help="Tail live trace")
    vm_trace_p.add_argument("--status", type=str, help="Filter by status (complete, failed, processing)")
    vm_trace_p.add_argument("--raw", action="store_true", help="Output raw JSON")
    vm_trace_p.add_argument("--limit", type=int, default=10, help="Max items for --list (default 10)")

    vm_monitor_p = vm_sub.add_parser("monitor", help="Show active workers and queue items")
    vm_monitor_p.add_argument("--watch", action="store_true", help="Poll and refresh")
    vm_monitor_p.add_argument("-n", "--interval", type=int, default=5, help="Poll interval in seconds (default 5)")
    vm_monitor_p.add_argument("--raw", action="store_true", help="Output raw JSON")

    vm_usage_p = vm_sub.add_parser("usage", help="Show token and cost analytics")
    vm_usage_p.add_argument("--hours", type=float, help="Filter to last N hours")
    vm_usage_p.add_argument("--days", type=int, help="Filter to last N days")
    vm_usage_p.add_argument("--queue", type=str, help="Filter by channel name")
    vm_usage_p.add_argument("--limit", type=int, default=20, help="Recent items limit (default 20)")
    vm_usage_p.add_argument("--raw", action="store_true", help="Output raw JSON")

    vm_cancel_p = vm_sub.add_parser("cancel", help="Cancel active agent task")
    vm_cancel_p.add_argument("channel", help="Channel/site name")
    vm_cancel_p.add_argument("--item", type=str, help="Specific item ID (default: current processing)")

    vm_rollback_p = vm_sub.add_parser("rollback", help="Roll back site to previous version")
    vm_rollback_p.add_argument("channel", help="Channel/site name")
    vm_rollback_p.add_argument("--version", type=int, help="Target version (default: previous)")
```

- [ ] **Step 4: Run parser tests to verify they pass**

Run: `cd ~/popcorn-cli && uv run pytest tests/test_vm_parser.py -v`
Expected: All PASS

- [ ] **Step 5: Add command handlers**

In `src/popcorn_cli/cli.py`, add the VM command handlers (before the `# Argparse` section, around line 1990). Also add the necessary imports from formatting at the top of the file.

Add to the imports block (around line 81):

```python
from .formatting import (
    fmt_activity,
    fmt_conversation,
    fmt_message,
    fmt_user,
    fmt_vm_cost,
    fmt_vm_monitor,
    fmt_vm_trace,
    fmt_vm_trace_event,
    fmt_vm_trace_list,
    fmt_vm_usage,
    format_timestamp,
    set_color,
)
```

Add handler functions:

```python
# ---------------------------------------------------------------------------
# VM (workspace VM introspection)
# ---------------------------------------------------------------------------


def _strip_hash(channel: str) -> str:
    """Strip leading # from channel name."""
    return channel.lstrip("#")


def cmd_vm_trace(args: argparse.Namespace) -> None:
    client = _get_client(args)
    channel = _strip_hash(args.channel)
    raw = getattr(args, "raw", False)

    if getattr(args, "list", False):
        resp = operations.vm_trace_list(client, channel, limit=args.limit)
        if raw:
            print(_json_ok(resp))
        else:
            items = resp.get("recent_items", [])
            print(fmt_vm_trace_list(channel, items))
        return

    if getattr(args, "watch", False):
        _vm_trace_watch(client, channel, args)
        return

    if args.item_id:
        resp = operations.vm_trace(client, channel, args.item_id)
    else:
        status_filter = getattr(args, "status", None)
        resp = operations.vm_trace_latest(client, channel, status=status_filter)
        if resp is None:
            msg = f"No items found for {channel}"
            if status_filter:
                msg += f" with status={status_filter}"
            print(msg, file=sys.stderr)
            sys.exit(1)

    if raw:
        print(_json_ok(resp))
    else:
        print(fmt_vm_trace(resp))


def _vm_trace_watch(client: APIClient, channel: str, args: argparse.Namespace) -> None:
    """Tail a live trace, printing new events as they arrive."""
    status_filter = getattr(args, "status", None) or "processing"
    resp = operations.vm_trace_latest(client, channel, status=status_filter)
    if resp is None:
        # Fall back to latest of any status
        resp = operations.vm_trace_latest(client, channel)
    if resp is None:
        print(f"No items found for {channel}", file=sys.stderr)
        sys.exit(1)

    item_id = resp["item_id"]
    seen_events = len(resp.get("events", []))

    # Print initial header + any existing events
    name = resp.get("name") or item_id
    _status(f"Watching: {name}  ({resp.get('status', '?')})")
    _status("")

    events = resp.get("events", [])
    prev_ts = None
    for event in events:
        line = fmt_vm_trace_event(event, prev_ts)
        if line:
            print(line, flush=True)
        if event.get("timestamp"):
            prev_ts = event["timestamp"]

    try:
        while True:
            time.sleep(3)
            resp = operations.vm_trace(client, channel, item_id)
            events = resp.get("events", [])
            new_events = events[seen_events:]
            seen_events = len(events)

            for event in new_events:
                line = fmt_vm_trace_event(event, prev_ts)
                if line:
                    print(line, flush=True)
                if event.get("timestamp"):
                    prev_ts = event["timestamp"]

            status = resp.get("status", "")
            if status in ("complete", "failed", "cancelled"):
                _status(f"\nFinished: {status}")
                if status == "failed" and resp.get("error"):
                    print(f"Error: {resp['error']}", file=sys.stderr)
                # Print final summary
                usage = resp.get("usage")
                if usage:
                    cost = fmt_vm_cost(usage.get("total_cost_usd", 0))
                    dur = resp.get("duration_seconds", 0)
                    from popcorn_cli.formatting import fmt_vm_duration
                    _status(f"Duration: {fmt_vm_duration(dur)}  |  Cost: {cost}")
                break
    except KeyboardInterrupt:
        _status("\nStopped watching.")


def cmd_vm_monitor(args: argparse.Namespace) -> None:
    client = _get_client(args)
    raw = getattr(args, "raw", False)

    if not getattr(args, "watch", False):
        resp = operations.vm_monitor(client)
        if raw:
            print(_json_ok(resp))
        else:
            print(fmt_vm_monitor(resp))
        return

    # Watch mode
    interval = getattr(args, "interval", 5)
    _status(f"Monitoring... (Ctrl+C to stop, polling every {interval}s)")
    try:
        while True:
            resp = operations.vm_monitor(client)
            if raw:
                print(_json_ok(resp), flush=True)
            else:
                # Clear screen and redraw
                print("\033[2J\033[H", end="", flush=True)
                print(fmt_vm_monitor(resp), flush=True)
            time.sleep(interval)
    except KeyboardInterrupt:
        _status("\nStopped monitoring.")


def cmd_vm_usage(args: argparse.Namespace) -> None:
    client = _get_client(args)
    raw = getattr(args, "raw", False)

    resp = operations.vm_usage(
        client,
        hours=getattr(args, "hours", None),
        days=getattr(args, "days", None),
        queue=getattr(args, "queue", None),
        limit=getattr(args, "limit", None),
    )
    if raw:
        print(_json_ok(resp))
    else:
        print(fmt_vm_usage(resp))


def cmd_vm_cancel(args: argparse.Namespace) -> None:
    client = _get_client(args)
    channel = _strip_hash(args.channel)
    item_id = getattr(args, "item", None)

    if item_id:
        resp = operations.vm_cancel(client, channel, item_id)
        print(f"Cancelled: {item_id} in {channel}")
    else:
        resp = operations.vm_cancel_current(client, channel)
        if resp is None:
            print(f"No active task in {channel}", file=sys.stderr)
            sys.exit(1)
        print(f"Cancelled active task in {channel}")

    if getattr(args, "json", False):
        print(_json_ok(resp))


def cmd_vm_rollback(args: argparse.Namespace) -> None:
    client = _get_client(args)
    channel = _strip_hash(args.channel)
    version = getattr(args, "version", None)

    resp = operations.vm_rollback(client, channel, version=version)
    if getattr(args, "json", False):
        print(_json_ok(resp))
    else:
        new_ver = resp.get("version", "?")
        print(f"Rolled back {channel} to v{new_ver}")
```

- [ ] **Step 6: Wire up the `vm` command dispatch in `main()`**

In the `main()` function, add the `vm` handling in the dispatch block (around line 2443, after the `webhook` handling):

```python
        elif args.command == "vm":
            vm_sub = {
                "trace": cmd_vm_trace,
                "monitor": cmd_vm_monitor,
                "usage": cmd_vm_usage,
                "cancel": cmd_vm_cancel,
                "rollback": cmd_vm_rollback,
            }
            handler = vm_sub.get(getattr(args, "vm_command", None) or "")
            if handler:
                handler(args)
            else:
                parser.parse_args(["vm", "--help"])
```

- [ ] **Step 7: Update `_COMMAND_CATEGORIES`, `_COMMAND_DESCRIPTIONS`, `_ALL_COMMAND_NAMES`**

Add to `_COMMAND_CATEGORIES`:
```python
    "vm": "vm",
```

Add to `_COMMAND_DESCRIPTIONS`:
```python
    "vm": "Workspace VM commands (trace, monitor, usage, cancel, rollback)",
```

Update `_ALL_COMMAND_NAMES` line to include `"vm"`:
```python
_ALL_COMMAND_NAMES.extend([*_COMMANDS.keys(), "auth", "workspace", "webhook", "vm"])
```

- [ ] **Step 8: Update epilog in `build_parser()`**

Add a VM section to the epilog string (before the `Auth & identity:` section):

```
VM:
  vm              Workspace VM commands (trace, monitor, usage, cancel, rollback)
```

- [ ] **Step 9: Update shell completions**

In `_BASH_COMPLETION`, add `vm` to the main popcorn completion list, and add a `vm)` case:
```bash
                vm) _values 'subcommand' trace monitor usage cancel rollback ;;
```

In `_ZSH_COMPLETION`, add to the commands array:
```
        'vm:Workspace VM commands'
```

And add a vm subcommand case.

- [ ] **Step 10: Run all tests**

Run: `cd ~/popcorn-cli && uv run pytest tests/test_vm_parser.py tests/test_vm_operations.py tests/test_vm_formatting.py -v`
Expected: All PASS

- [ ] **Step 11: Run full test suite**

Run: `cd ~/popcorn-cli && uv run pytest -q`
Expected: All PASS, no regressions

- [ ] **Step 12: Run lint and type checks**

Run: `cd ~/popcorn-cli && uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/`
Expected: Clean

- [ ] **Step 13: Commit**

```bash
cd ~/popcorn-cli
git add src/popcorn_cli/cli.py tests/test_vm_parser.py
git commit -m "feat: add popcorn vm commands (trace, monitor, usage, cancel, rollback)"
```

---

### Task 4: Update docstring and smoke test

**Files:**
- Modify: `src/popcorn_cli/cli.py` (module docstring, line 1)

- [ ] **Step 1: Update module docstring**

Add to the docstring at the top of `cli.py` (after the `popcorn log` line):

```
    popcorn vm trace <channel> [item] [--list] [--watch] [--raw]
    popcorn vm monitor [--watch] [-n INTERVAL] [--raw]
    popcorn vm usage [--hours N] [--days N] [--queue NAME] [--raw]
    popcorn vm cancel <channel> [--item ID]
    popcorn vm rollback <channel> [--version N]
```

- [ ] **Step 2: Run full test suite one final time**

Run: `cd ~/popcorn-cli && uv run pytest -q`
Expected: All PASS

- [ ] **Step 3: Run lint**

Run: `cd ~/popcorn-cli && uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/`
Expected: Clean

- [ ] **Step 4: Commit**

```bash
cd ~/popcorn-cli
git add src/popcorn_cli/cli.py
git commit -m "docs: add vm commands to CLI docstring"
```
