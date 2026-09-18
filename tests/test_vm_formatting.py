"""Tests for VM formatting helpers."""

from __future__ import annotations

from popcorn_cli.formatting import (
    fmt_vm_cost,
    fmt_vm_duration,
    fmt_vm_tokens,
    fmt_vm_trace,
    fmt_vm_trace_event,
    fmt_vm_trace_list,
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
                {
                    "type": "tool_call",
                    "tool": "Read",
                    "input": {},
                    "timestamp": "2026-03-27T14:23:01Z",
                },
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
