"""Tests for `popcorn app lines` — the fork-line inventory.

Two halves of that feature are NOT here because the API cannot support them,
and the tests say so rather than leaving it to a reader to notice: there is no
endpoint that deletes a fork line, and none that reports how many channels
ride one. `TestStatedGaps` holds the command to admitting both, so a later
backend change has to come back through here.
"""

from __future__ import annotations

import argparse
import json
from unittest.mock import patch

from popcorn_core import operations


def _args(**over):
    base = {
        "channel": "#alerts",
        "app": None,
        "json": False,
        "quiet": True,
        "no_color": True,
    }
    base.update(over)
    return argparse.Namespace(**base)


def _listing(*lines: tuple[str, str, str], apps_extra: list | None = None) -> dict:
    """An `/apps/list` response: one product entry plus the given fork lines.

    Each line is ``(app, fork_name, semver)``.
    """
    apps: list[dict] = [
        {
            "app": "claimcoordinator",
            "kind": "product",
            "fork_name": None,
            "version_id": 1,
            "semver": "1.40.0",
            "published_at": "2026-09-01T10:00:00+00:00",
        }
    ]
    for index, (app, fork_name, semver) in enumerate(lines, start=2):
        apps.append(
            {
                "app": app,
                "kind": "fork",
                "fork_name": fork_name,
                "version_id": index,
                "semver": semver,
                "published_at": f"2026-09-{index:02d}T12:30:00+00:00",
            }
        )
    apps += apps_extra or []
    return {"ok": True, "apps": apps, "channel": None}


def _run(args, listing):
    from popcorn_cli.commands import app as mod

    captured = {}
    with (
        patch("popcorn_cli.cli._get_client", return_value=object()),
        patch(
            "popcorn_cli.cli._output",
            lambda a, data, rendered: captured.update(data=data, rendered=rendered),
        ),
        patch.object(operations, "list_channel_apps", return_value=listing),
    ):
        mod._app_lines(args)
    return captured


class TestListing:
    def test_it_lists_every_fork_line(self):
        out = _run(
            _args(),
            _listing(
                ("claimcoordinator", "default", "1.14.0"),
                ("claimcoordinator", "demo914", "1.22.0"),
                ("claimcoordinator", "probe", "1.37.0"),
            ),
        )
        assert [item["fork_name"] for item in out["data"]["lines"]] == [
            "default",
            "demo914",
            "probe",
        ]
        assert "3 fork lines in this workspace." in out["rendered"]

    def test_the_product_entry_is_not_a_line(self):
        """`app list` mixes product and fork rows; this command is the fork
        inventory, and a product head is not one."""
        out = _run(_args(), _listing(("claimcoordinator", "default", "1.14.0")))
        assert [item["kind"] for item in out["data"]["lines"]] == ["fork"]
        assert "1.40.0" not in out["rendered"]

    def test_it_reports_name_head_and_published_at(self):
        out = _run(_args(), _listing(("claimcoordinator", "demo914", "1.22.0")))
        rendered = out["rendered"]
        assert "demo914" in rendered
        assert "1.22.0" in rendered
        assert "2026-09-02 12:30" in rendered

    def test_app_filters_to_one_app(self):
        out = _run(
            _args(app="deploywatch"),
            _listing(
                ("claimcoordinator", "default", "1.14.0"),
                ("deploywatch", "probe", "0.4.0"),
            ),
        )
        assert [item["app"] for item in out["data"]["lines"]] == ["deploywatch"]
        assert "claimcoordinator" not in out["rendered"]

    def test_lines_are_sorted_by_app_then_name(self):
        out = _run(
            _args(),
            _listing(
                ("deploywatch", "probe", "0.4.0"),
                ("claimcoordinator", "pubtest", "1.35.0"),
                ("claimcoordinator", "demo914", "1.22.0"),
            ),
        )
        assert [(i["app"], i["fork_name"]) for i in out["data"]["lines"]] == [
            ("claimcoordinator", "demo914"),
            ("claimcoordinator", "pubtest"),
            ("deploywatch", "probe"),
        ]

    def test_an_empty_workspace_says_how_to_make_one(self):
        out = _run(_args(), _listing())
        assert out["data"]["lines"] == []
        assert "owns no fork lines" in out["rendered"]
        assert "--fork" in out["rendered"]

    def test_one_line_is_not_pluralised(self):
        out = _run(_args(), _listing(("claimcoordinator", "default", "1.14.0")))
        assert "1 fork line in this workspace." in out["rendered"]

    def test_json_carries_the_lines(self, capsys):
        from popcorn_cli.commands import app as mod

        with (
            patch("popcorn_cli.cli._get_client", return_value=object()),
            patch.object(
                operations,
                "list_channel_apps",
                return_value=_listing(("claimcoordinator", "demo914", "1.22.0")),
            ),
        ):
            mod._app_lines(_args(json=True))
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True
        assert payload["data"]["lines"][0]["fork_name"] == "demo914"


class TestChannelIsAuthOnly:
    def test_the_channel_is_passed_straight_through(self):
        """`--channel` is the API's authorization handle, not a filter: the
        lines returned are the workspace's, so nothing is scoped by it."""
        from popcorn_cli.commands import app as mod

        calls = []

        def _list(client, conversation):
            calls.append(conversation)
            return _listing(("claimcoordinator", "default", "1.14.0"))

        with (
            patch("popcorn_cli.cli._get_client", return_value=object()),
            patch("popcorn_cli.cli._output"),
            patch.object(operations, "list_channel_apps", _list),
        ):
            mod._app_lines(_args(channel="#somewhere-else"))
        assert calls == ["#somewhere-else"]


class TestStatedGaps:
    """The command says what it cannot tell you. Both are backend gaps."""

    def test_it_says_the_channel_count_is_missing(self):
        out = _run(_args(), _listing(("claimcoordinator", "default", "1.14.0")))
        assert "no per-line channel count" in out["rendered"]
        assert out["data"]["channel_counts_available"] is False

    def test_it_says_deleting_a_line_is_not_possible(self):
        out = _run(_args(), _listing(("claimcoordinator", "default", "1.14.0")))
        assert "Deleting a fork line is not possible yet" in out["rendered"]
        assert out["data"]["delete_supported"] is False

    def test_there_is_no_delete_subcommand(self):
        """A delete with no endpoint behind it would dead-end, so the surface
        must not advertise one. Drop this test when the API grows one."""
        from popcorn_cli.registry import completion_words

        assert "delete" not in completion_words("app")

    def test_the_channel_count_is_not_guessed_from_a_channel_sweep(self):
        """Approximating it by reading every reachable channel's binding would
        under-count — it sees only channels the caller can reach — and an
        undercount is the dangerous direction for a "safe to delete?" number.
        One request, no sweep."""
        from popcorn_cli.commands import app as mod

        with (
            patch("popcorn_cli.cli._get_client", return_value=object()),
            patch("popcorn_cli.cli._output"),
            patch.object(
                operations,
                "list_channel_apps",
                return_value=_listing(("claimcoordinator", "default", "1.14.0")),
            ),
            patch.object(operations, "search_channels") as sweep,
        ):
            mod._app_lines(_args())
        assert sweep.call_count == 0
