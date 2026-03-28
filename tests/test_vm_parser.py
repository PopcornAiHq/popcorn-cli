"""Tests for VM and site subcommand parsing."""

from __future__ import annotations

import pytest

from popcorn_cli.cli import build_parser


@pytest.fixture()
def parser():
    return build_parser()


class TestSiteTrace:
    def test_trace_channel_only(self, parser):
        args = parser.parse_args(["site", "trace", "my-channel"])
        assert args.command == "site"
        assert args.site_command == "trace"
        assert args.channel == "my-channel"
        assert args.item_id is None

    def test_trace_with_item(self, parser):
        args = parser.parse_args(["site", "trace", "my-channel", "abc123"])
        assert args.channel == "my-channel"
        assert args.item_id == "abc123"

    def test_trace_list(self, parser):
        args = parser.parse_args(["site", "trace", "my-channel", "--list"])
        assert args.list is True

    def test_trace_watch(self, parser):
        args = parser.parse_args(["site", "trace", "my-channel", "--watch"])
        assert args.watch is True

    def test_trace_status_filter(self, parser):
        args = parser.parse_args(["site", "trace", "my-channel", "--status", "failed"])
        assert args.status == "failed"

    def test_trace_raw(self, parser):
        args = parser.parse_args(["site", "trace", "my-channel", "--raw"])
        assert args.raw is True

    def test_trace_limit(self, parser):
        args = parser.parse_args(["site", "trace", "my-channel", "--list", "--limit", "5"])
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


class TestSiteCancel:
    def test_cancel_channel(self, parser):
        args = parser.parse_args(["site", "cancel", "my-channel"])
        assert args.site_command == "cancel"
        assert args.channel == "my-channel"

    def test_cancel_with_item(self, parser):
        args = parser.parse_args(["site", "cancel", "my-channel", "--item", "abc123"])
        assert args.item == "abc123"


class TestSiteRollback:
    def test_rollback_channel(self, parser):
        args = parser.parse_args(["site", "rollback", "my-channel"])
        assert args.site_command == "rollback"
        assert args.channel == "my-channel"

    def test_rollback_version(self, parser):
        args = parser.parse_args(["site", "rollback", "my-channel", "--version", "3"])
        assert args.version == 3
