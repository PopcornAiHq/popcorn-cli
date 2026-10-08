"""`popcorn app checkout <app>[@<semver>]` — a line named without a channel.

The server reads one of the workspace's lines by name for workspace admins:
the product line's head (what the workspace's release track offers), or a
fork line's head or any version on it by id. Versions are addressed by id,
never semver, so `@<semver>` is held against what was served and refused,
with nothing written, when it names any other version.
"""

from __future__ import annotations

import argparse
from unittest.mock import patch

import pytest

from popcorn_core import operations
from popcorn_core.app_checkout import (
    BASELINE_FILE,
    Baseline,
    historical_guide_text,
    read_baseline,
    write_baseline,
)
from popcorn_core.errors import APIError, PopcornError

_TREE = {"manifest.yaml": 'version: "0.34.0"\n', "alert.yaml": "name: alert\n"}


def _files_response(files: dict[str, str] | None = None, **over):
    payload = {
        "ok": True,
        "app": "example-app",
        "kind": "product",
        "fork_name": None,
        "version_id": 12,
        "semver": "0.34.0",
        "published_at": None,
        "files": [{"path": p, "content": c} for p, c in sorted((files or _TREE).items())],
        "complete": True,
        "unmatched": [],
    }
    payload.update(over)
    return payload


def _fork_files(**over):
    base = {"kind": "fork", "fork_name": "demo", "version_id": 20, "semver": "1.2.0"}
    base.update(over)
    return _files_response(**base)


def _args(**over):
    base = {
        "channel": None,
        "app": "example-app",
        "directory": None,
        "line": None,
        "fork": None,
        "version": None,
        "force": False,
        "json": False,
        "quiet": True,
        "no_color": True,
    }
    base.update(over)
    return argparse.Namespace(**base)


def _run(args, files=None, head=None, files_error=None, captured=None):
    """Run `_app_checkout` against a fake client; returns what it output.

    Fakes the HTTP layer rather than the operations, so the served-version
    guard inside `get_app_line_files` is exercised.
    """
    from popcorn_cli.commands import app as mod

    if captured is None:
        captured = {}
    captured.setdefault("gets", [])

    class _Client:
        def get(self, path, params):
            captured["gets"].append((path, params))
            if path == "/api/apps/line/files":
                if files_error is not None:
                    raise files_error
                return files if files is not None else _files_response()
            if path == "/api/apps/line/tree":
                return head
            raise AssertionError(f"unexpected read {path}")

    with (
        patch("popcorn_cli.cli._get_client", return_value=_Client()),
        patch(
            "popcorn_cli.cli._output",
            lambda a, data, rendered: captured.update(data=data, rendered=rendered),
        ),
    ):
        mod._app_checkout(args)
    return captured


# ---------------------------------------------------------------------------
# The reads
# ---------------------------------------------------------------------------


class TestLineReads:
    def test_the_product_line_sends_only_the_app(self, mock_client):
        """No fork_name is the product line; sending an empty one would not be."""
        mock_client.get.return_value = _files_response()
        operations.get_app_line_files(mock_client, "example-app")
        mock_client.get.assert_called_once_with("/api/apps/line/files", {"app": "example-app"})

    def test_a_fork_line_and_version(self, mock_client):
        mock_client.get.return_value = _fork_files(version_id=18)
        operations.get_app_line_files(mock_client, "example-app", "demo", 18)
        mock_client.get.assert_called_once_with(
            "/api/apps/line/files", {"app": "example-app", "fork_name": "demo", "version_id": 18}
        )

    def test_refuses_a_response_that_is_not_the_requested_version(self, mock_client):
        mock_client.get.return_value = _fork_files(version_id=20)
        with pytest.raises(PopcornError) as exc:
            operations.get_app_line_files(mock_client, "example-app", "demo", 18)
        assert "nothing was written" in str(exc.value)

    def test_the_head_tree(self, mock_client):
        mock_client.get.return_value = {"version_id": 20}
        operations.get_app_line_tree(mock_client, "example-app", "demo")
        mock_client.get.assert_called_once_with(
            "/api/apps/line/tree", {"app": "example-app", "fork_name": "demo"}
        )


# ---------------------------------------------------------------------------
# The product line
# ---------------------------------------------------------------------------


class TestProductLine:
    def test_checks_out_the_head_into_app(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        out = _run(_args())

        target = tmp_path / "example-app"
        assert (target / "alert.yaml").read_text() == "name: alert\n"
        base = read_baseline(target)
        assert (base.app, base.kind, base.semver, base.base_version_id) == (
            "example-app",
            "product",
            "0.34.0",
            12,
        )
        assert base.conversation_id is None and base.historical is False
        assert out["gets"] == [("/api/apps/line/files", {"app": "example-app"})]
        assert out["data"]["base_version_id"] == 12
        # No channel, so nothing about one is reported.
        assert "channel_version_id" not in out["data"]
        assert "Checked out example-app 0.34.0 (product)" in out["rendered"]
        assert "Next: popcorn app validate example-app" in out["rendered"]

    def test_at_the_heads_semver_checks_it_out(self, tmp_path):
        target = tmp_path / "co"
        _run(_args(app="example-app@0.34.0", directory=str(target)))
        assert read_baseline(target).semver == "0.34.0"

    def test_at_another_semver_is_refused_and_nothing_is_written(self, tmp_path):
        """The server serves the product line's head and nothing older, so an
        older semver must not land on disk under the head's content."""
        target = tmp_path / "co"
        with pytest.raises(PopcornError) as exc:
            _run(_args(app="example-app@0.33.0", directory=str(target)))
        message = str(exc.value)
        assert "example-app@0.33.0 cannot be checked out without a channel" in message
        assert "0.34.0 (version 12)" in message
        assert "nothing was written" in message
        assert exc.value.error_code == "not_found"
        assert "--channel" in (exc.value.hint or "")
        assert not target.exists()

    def test_an_older_product_version_by_id_says_what_is_readable(self, tmp_path):
        err = APIError("version 9 is not a version of the example-app product line", 404)
        with pytest.raises(APIError) as exc:
            _run(_args(version=9, directory=str(tmp_path / "x")), files_error=err)
        assert "serves only its head" in (exc.value.hint or "")
        assert not (tmp_path / "x").exists()


# ---------------------------------------------------------------------------
# Fork lines
# ---------------------------------------------------------------------------


class TestForkLine:
    def test_checks_out_the_head_and_records_the_line(self, tmp_path):
        target = tmp_path / "co"
        out = _run(_args(line="demo", directory=str(target)), files=_fork_files())

        base = read_baseline(target)
        assert (base.kind, base.fork_name, base.base_version_id) == ("fork", "demo", 20)
        assert base.conversation_id is None
        assert out["gets"] == [
            ("/api/apps/line/files", {"app": "example-app", "fork_name": "demo"})
        ]
        assert out["data"]["fork_name"] == "demo"
        assert "(fork line 'demo')" in out["rendered"]
        # What the missing channel means for the commands after this one.
        assert "need --channel" in out["rendered"]

    def test_a_past_version_by_id_is_historical(self, tmp_path):
        target = tmp_path / "old"
        out = _run(
            _args(line="demo", version=18, directory=str(target)),
            files=_fork_files(version_id=18, semver="1.1.0"),
            head={"version_id": 20, "semver": "1.2.0"},
        )
        assert read_baseline(target).historical is True
        assert [p for p, _ in out["gets"]] == ["/api/apps/line/files", "/api/apps/line/tree"]
        assert (out["data"]["head_version_id"], out["data"]["historical"]) == (20, True)
        assert "not the line's head (1.2.0, version 20)" in out["rendered"]
        assert "Next: popcorn app validate" not in out["rendered"]

    def test_a_past_version_defaults_to_app_and_semver(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _run(
            _args(line="demo", version=18),
            files=_fork_files(version_id=18, semver="1.1.0"),
            head={"version_id": 20, "semver": "1.2.0"},
        )
        assert (tmp_path / "example-app-1.1.0" / "manifest.yaml").exists()

    def test_the_head_by_id_is_not_historical(self, tmp_path):
        target = tmp_path / "co"
        _run(
            _args(line="demo", version=20, directory=str(target)),
            files=_fork_files(),
            head={"version_id": 20, "semver": "1.2.0"},
        )
        assert read_baseline(target).historical is False

    def test_at_another_semver_points_at_version_ids(self, tmp_path):
        with pytest.raises(PopcornError) as exc:
            _run(
                _args(app="example-app@1.1.0", line="demo", directory=str(tmp_path / "x")),
                files=_fork_files(),
            )
        assert "read by id only" in str(exc.value)
        assert "--line demo --version <id>" in (exc.value.hint or "")
        assert not (tmp_path / "x").exists()

    def test_a_historical_snapshot_republishes_through_the_line(self, tmp_path):
        """Its guide and publish refusal name the line, since there is no
        channel to name."""
        from popcorn_cli.commands import app as mod

        base = Baseline(
            app="example-app",
            semver="1.1.0",
            base_version_id=18,
            tree_digest="d",
            kind="fork",
            fork_name="demo",
            historical=True,
        )
        assert "popcorn app checkout example-app --line demo --dir" in historical_guide_text(base)
        with pytest.raises(PopcornError) as exc:
            mod._refuse_historical_publish(base, tmp_path)
        assert "popcorn app checkout example-app --line demo --dir" in (exc.value.hint or "")


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


class TestRefusals:
    def test_a_non_admin_is_told_to_go_through_a_channel(self, tmp_path):
        err = APIError("User is not authorized to perform this operation.", 403)
        with pytest.raises(APIError) as exc:
            _run(_args(directory=str(tmp_path / "x")), files_error=err)
        assert exc.value.error_code == "forbidden"
        hint = exc.value.hint or ""
        assert "workspace admins" in hint and "--channel" in hint
        assert not (tmp_path / "x").exists()

    def test_an_unreadable_line_points_at_app_list(self, tmp_path):
        err = APIError("this workspace has no readable example-app fork line 'nope'", 404)
        with pytest.raises(APIError) as exc:
            _run(_args(line="nope", directory=str(tmp_path / "x")), files_error=err)
        assert "'popcorn app list'" in (exc.value.hint or "")

    def test_an_older_server_says_it_predates_the_read(self, tmp_path):
        with pytest.raises(APIError) as exc:
            _run(_args(directory=str(tmp_path / "x")), files_error=APIError("Not Found", 404))
        assert "predates checkout without a channel" in (exc.value.hint or "")

    def test_an_unknown_404_keeps_its_own_message(self, tmp_path):
        with pytest.raises(APIError) as exc:
            _run(_args(directory=str(tmp_path / "x")), files_error=APIError("gone", 404))
        assert exc.value.hint is None

    @pytest.mark.parametrize(
        "over,needle",
        [
            ({"app": None}, "name an app"),
            ({"fork": ""}, "needs --channel"),
            ({"fork": "demo"}, "needs --channel"),
            ({"app": "example-app@"}, "is not <app> or <app>@<semver>"),
            ({"app": "@1.0.0"}, "is not <app> or <app>@<semver>"),
            ({"app": "example-app@12"}, "is not a semver"),
            ({"app": "example-app@1.0.0", "version": 3}, "both name a version"),
            ({"version": 0}, "whole number from 1"),
        ],
    )
    def test_bad_requests_are_refused_before_any_request(self, tmp_path, over, needle):
        captured: dict = {}
        with pytest.raises(PopcornError) as exc:
            _run(_args(directory=str(tmp_path / "x"), **over), captured=captured)
        assert needle in str(exc.value)
        assert exc.value.error_code == "validation"
        assert captured["gets"] == []

    def test_existing_directory_protection_still_applies(self, tmp_path):
        (tmp_path / "manifest.yaml").write_text("mine")
        with pytest.raises(PopcornError) as exc:
            _run(_args(directory=str(tmp_path)))
        assert "--force" in str(exc.value)
        assert (tmp_path / "manifest.yaml").read_text() == "mine"
        assert not (tmp_path / BASELINE_FILE).exists()


# ---------------------------------------------------------------------------
# The channel form, and what follows a channel-less checkout
# ---------------------------------------------------------------------------


class TestChannelForm:
    def test_the_first_positional_is_still_the_directory(self):
        from popcorn_cli.commands import app as mod

        args = _args(channel="#alerts", app="/tmp/co")
        mod._channel_checkout_positionals(args)
        assert (args.app, args.directory) == (None, "/tmp/co")

    def test_line_is_refused_with_a_channel(self):
        from popcorn_cli.commands import app as mod

        with pytest.raises(PopcornError) as exc:
            mod._channel_checkout_positionals(_args(channel="#alerts", app=None, line="demo"))
        assert "--line" in str(exc.value)

    def test_an_app_and_a_directory_are_refused_with_a_channel(self):
        from popcorn_cli.commands import app as mod

        with pytest.raises(PopcornError) as exc:
            mod._channel_checkout_positionals(
                _args(channel="#alerts", app="example-app", directory="/tmp/co")
            )
        assert "name no app" in str(exc.value)

    def test_a_channel_less_baseline_asks_for_a_channel_by_name(self, tmp_path):
        """Not the 0.19.0 message, which would send the author to upgrade."""
        from popcorn_cli.commands import app as mod

        write_baseline(
            tmp_path,
            Baseline(app="a", semver="1.2.0", base_version_id=20, tree_digest="d", kind="fork"),
        )
        with pytest.raises(PopcornError) as exc:
            mod._channel_of(argparse.Namespace(channel=None), read_baseline(tmp_path))
        assert "taken without one" in str(exc.value)
        assert "0.19.0" not in str(exc.value)
