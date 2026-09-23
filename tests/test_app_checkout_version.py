"""`popcorn app checkout --version N` — reading one past version of a line.

Two properties carry the weight here:

- An older server ignores `version_id` and answers with the bound tree. That
  tree must never reach disk under the requested version's name, so the read
  refuses any response that does not say it IS that version, and a refused
  checkout writes nothing at all.
- A checkout of a past version is not a publish base. Its baseline says so,
  `app publish` refuses it before any round trip, and the refusal says how to
  republish the content rather than "the line moved".
"""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

import pytest

from popcorn_core import operations
from popcorn_core.app_checkout import (
    BASELINE_FILE,
    Baseline,
    read_baseline,
    write_baseline,
)
from popcorn_core.errors import APIError, PopcornError

_CONV = "00000000-0000-4000-8000-000000000001"
_NOT_A_VERSION = "version 58 is not a version of this channel's alerttracker line"


def _files_response(files: dict[str, str], **over):
    payload = {
        "ok": True,
        "app": "alerttracker",
        "kind": "fork",
        "ref": "version",
        "version_id": 5,
        "semver": "0.1.0",
        "bound_version_id": 7,
        "bound_semver": "0.2.0",
        "files": [{"path": p, "content": c} for p, c in sorted(files.items())],
    }
    payload.update(over)
    return payload


def _head(version_id: int = 7, semver: str = "0.2.0") -> dict:
    return {"ok": True, "ref": "head", "version_id": version_id, "semver": semver, "paths": []}


def _args(**over):
    base = {
        "channel": "#alerts",
        "directory": None,
        "fork": None,
        "version": 5,
        "force": False,
        "json": False,
        "quiet": True,
        "no_color": True,
    }
    base.update(over)
    return argparse.Namespace(**base)


# ---------------------------------------------------------------------------
# The read
# ---------------------------------------------------------------------------


@pytest.fixture
def _patch_resolve():
    with patch("popcorn_core.operations.resolve_conversation", return_value=_CONV):
        yield


@pytest.mark.usefixtures("_patch_resolve")
class TestVersionRead:
    def test_sends_version_id_in_place_of_ref(self, mock_client):
        """`version_id` overrides `ref` server-side; sending both would make
        an older server's answer depend on which one it happened to honour."""
        mock_client.get.return_value = _files_response({})
        operations.get_channel_app_files(mock_client, _CONV, version_id=5)
        mock_client.get.assert_called_once_with(
            "/api/apps/files", {"conversation_id": _CONV, "version_id": 5}
        )

    def test_a_plain_read_is_unchanged(self, mock_client):
        mock_client.get.return_value = _files_response({}, ref="head")
        operations.get_channel_app_files(mock_client, _CONV)
        mock_client.get.assert_called_once_with(
            "/api/apps/files", {"conversation_id": _CONV, "ref": "head"}
        )

    @pytest.mark.parametrize(
        "served",
        [
            # An older server: the parameter ignored, the bound tree served.
            {"ref": "bound", "version_id": 7},
            # Older still: no `ref` in the response at all.
            {"ref": None, "version_id": 7},
            # Ignored, and the bound version happens to be the one asked for —
            # the id matches, so only `ref` can tell.
            {"ref": "bound", "version_id": 5},
            # Says "version" but serves another one.
            {"ref": "version", "version_id": 6},
        ],
    )
    def test_refuses_a_response_that_is_not_the_requested_version(self, mock_client, served):
        mock_client.get.return_value = _files_response({"manifest.yaml": "v\n"}, **served)
        with pytest.raises(PopcornError) as exc:
            operations.get_channel_app_files(mock_client, _CONV, version_id=5)
        assert "does not support reading a specific version" in str(exc.value)
        assert "nothing was written" in str(exc.value)


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def _run(args, files=None, head=None, files_error=None):
    """Run `_app_checkout` against a patched client; returns what it output.

    Patches the operation's HTTP call rather than the operation itself, so
    the served-version guard inside `get_channel_app_files` is exercised.
    """
    from popcorn_cli.commands import app as mod

    captured: dict = {"gets": []}

    class _Client:
        def get(self, path, params):
            captured["gets"].append((path, params))
            if path == "/api/apps/files":
                if files_error is not None:
                    raise files_error
                return files
            if path == "/api/apps/tree":
                return head if head is not None else _head()
            raise AssertionError(f"unexpected read {path}")

    with (
        patch("popcorn_cli.cli._get_client", return_value=_Client()),
        patch(
            "popcorn_cli.cli._output",
            lambda a, data, rendered: captured.update(data=data, rendered=rendered),
        ),
        patch.object(mod, "resolve_conversation", return_value=_CONV),
        patch("popcorn_core.operations.resolve_conversation", return_value=_CONV),
    ):
        mod._app_checkout(args)
    return captured


_TREE = {"manifest.yaml": 'version: "0.1.0"\n', "alert.yaml": "name: alert\n"}


class TestCheckoutVersion:
    def test_a_past_version_writes_a_historical_baseline(self, tmp_path):
        target = tmp_path / "old"
        out = _run(_args(directory=str(target)), files=_files_response(_TREE))

        assert (target / "alert.yaml").read_text() == "name: alert\n"
        base = read_baseline(target)
        assert base is not None
        # The id the files came from, so `status` and the digest describe
        # them — the flag, not the id, is what keeps it from being a base.
        assert (base.base_version_id, base.semver, base.historical) == (5, "0.1.0", True)
        assert out["data"]["historical"] is True
        assert (out["data"]["head_version_id"], out["data"]["head_semver"]) == (7, "0.2.0")
        assert "not the line's head (0.2.0, version 7)" in out["rendered"]
        assert "'app publish' refuses it" in out["rendered"]
        # Nothing to publish from here, so no publish loop is suggested.
        assert "Next: popcorn template check" not in out["rendered"]

    def test_the_head_by_id_is_an_ordinary_checkout(self, tmp_path):
        """Naming the head's id is the same checkout a plain one would write,
        and refusing to publish from it would be a refusal with no reason."""
        target = tmp_path / "head"
        out = _run(
            _args(directory=str(target), version=7),
            files=_files_response(_TREE, version_id=7, semver="0.2.0"),
            head=_head(7, "0.2.0"),
        )
        assert read_baseline(target).historical is False
        assert out["data"]["historical"] is False
        assert "Next: popcorn template check" in out["rendered"]

    def test_the_baseline_file_marks_it_and_a_plain_one_does_not(self, tmp_path):
        """Written only when true, so a head checkout's file is unchanged."""
        target = tmp_path / "old"
        _run(_args(directory=str(target)), files=_files_response(_TREE))
        assert '"historical": true' in (target / BASELINE_FILE).read_text()

        plain = tmp_path / "plain"
        plain.mkdir()
        write_baseline(plain, Baseline(app="a", semver="0.2.0", base_version_id=7, tree_digest="d"))
        assert "historical" not in (plain / BASELINE_FILE).read_text()

    def test_defaults_the_directory_to_app_and_semver(self, tmp_path, monkeypatch):
        """So an old version lands beside the working copy at ./<app>, and two
        versions land side by side for a diff, without either colliding."""
        monkeypatch.chdir(tmp_path)
        _run(_args(), files=_files_response(_TREE))
        assert (tmp_path / "alerttracker-0.1.0" / "manifest.yaml").exists()
        assert not (tmp_path / "alerttracker").exists()

    def test_an_older_server_is_refused_and_nothing_is_written(self, tmp_path):
        """The safety-critical case: the parameter ignored, the bound tree
        served. It must not land on disk under the requested version."""
        target = tmp_path / "old"
        with pytest.raises(PopcornError) as exc:
            _run(
                _args(directory=str(target)),
                files=_files_response(_TREE, ref="bound", version_id=7, semver="0.2.0"),
            )
        assert "does not support reading a specific version" in str(exc.value)
        assert not target.exists()

    def test_an_older_server_leaves_an_existing_checkout_untouched(self, tmp_path):
        (tmp_path / "manifest.yaml").write_text("mine")
        with pytest.raises(PopcornError):
            _run(
                _args(directory=str(tmp_path), force=True),
                files=_files_response(_TREE, ref="bound", version_id=7),
            )
        assert (tmp_path / "manifest.yaml").read_text() == "mine"
        assert not (tmp_path / BASELINE_FILE).exists()

    def test_an_unreadable_version_says_where_ids_come_from(self, tmp_path):
        """One 404 for every unreadable id, by design — the hint must not
        guess which case it was, only where a valid id can be found."""
        err = APIError(_NOT_A_VERSION, status_code=404)
        with pytest.raises(APIError) as exc:
            _run(_args(directory=str(tmp_path / "x"), version=58), files_error=err)
        assert str(exc.value) == _NOT_A_VERSION
        assert exc.value.error_code == "not_found"
        hint = exc.value.hint or ""
        assert "readable only from this channel's own line" in hint
        assert "app publish" in hint and "app status" in hint
        for guess in ("another workspace", "does not exist", "product"):
            assert guess not in hint
        assert not (tmp_path / "x").exists()

    def test_a_different_404_keeps_its_own_message(self, tmp_path):
        err = APIError("channel runs no app bundle", status_code=404)
        with pytest.raises(APIError) as exc:
            _run(_args(directory=str(tmp_path / "x")), files_error=err)
        assert exc.value.hint is None

    @pytest.mark.parametrize("bad", [0, -3])
    def test_a_non_positive_id_is_refused_before_any_request(self, tmp_path, bad):
        with pytest.raises(PopcornError) as exc:
            _run(_args(directory=str(tmp_path / "x"), version=bad), files=_files_response(_TREE))
        assert "whole number from 1" in str(exc.value)

    def test_existing_directory_protection_still_applies(self, tmp_path):
        (tmp_path / "manifest.yaml").write_text("mine")
        with pytest.raises(PopcornError) as exc:
            _run(_args(directory=str(tmp_path)), files=_files_response(_TREE))
        assert "--force" in str(exc.value)
        assert (tmp_path / "manifest.yaml").read_text() == "mine"

    def test_a_plain_checkout_reads_no_tree_and_adds_no_fields(self, tmp_path):
        out = _run(
            _args(directory=str(tmp_path / "p"), version=None),
            files=_files_response(_TREE, ref="head", version_id=7, semver="0.2.0"),
        )
        assert [p for p, _ in out["gets"]] == ["/api/apps/files"]
        assert "historical" not in out["data"]
        assert read_baseline(tmp_path / "p").historical is False


class TestParser:
    def test_version_parses_as_an_int(self):
        from popcorn_cli.cli import build_parser

        ns = build_parser().parse_args(["app", "checkout", "--channel", "x", "--version", "12"])
        assert ns.version == 12

    def test_version_and_fork_are_exclusive(self, capsys):
        """A fork re-binds the channel and then reads its head; a version read
        is of one fixed version. Together they have no single meaning."""
        from popcorn_cli.cli import build_parser

        with pytest.raises(SystemExit):
            build_parser().parse_args(
                ["app", "checkout", "--channel", "x", "--fork=line", "--version", "3"]
            )
        assert "not allowed with" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# What the other commands make of a historical checkout
# ---------------------------------------------------------------------------


def _historical_checkout(directory: Path) -> None:
    from popcorn_core.app_publish import local_digest

    directory.mkdir(parents=True, exist_ok=True)
    for path, content in _TREE.items():
        (directory / path).write_text(content)
    write_baseline(
        directory,
        Baseline(
            app="alerttracker",
            kind="fork",
            semver="0.1.0",
            base_version_id=5,
            tree_digest=local_digest(dict(_TREE)),
            conversation_id=_CONV,
            historical=True,
        ),
    )


def _publish_args(directory: Path, **over):
    base = {
        "channel": None,
        "directory": str(directory),
        "message": "restore",
        "bump": None,
        "json": False,
        "quiet": True,
        "no_color": True,
    }
    base.update(over)
    return argparse.Namespace(**base)


class TestPublishFromHistorical:
    @pytest.mark.parametrize("bump", [None, "patch"])
    def test_refused_before_any_round_trip(self, tmp_path, bump):
        from popcorn_cli.commands import app as mod

        _historical_checkout(tmp_path)
        (tmp_path / "manifest.yaml").write_text('version: "0.2.1"\n')
        calls: list = []
        with (
            patch("popcorn_cli.cli._get_client", return_value=object()),
            patch("popcorn_cli.cli._output"),
            patch.object(operations, "get_channel_app_files", lambda *a, **k: calls.append(a)),
            patch.object(operations, "publish_channel_app", lambda *a, **k: calls.append(a)),
            pytest.raises(PopcornError) as exc,
        ):
            mod._app_publish(_publish_args(tmp_path, bump=bump))

        assert calls == []
        assert exc.value.error_code == "conflict"
        msg, hint = str(exc.value), exc.value.hint or ""
        assert "a past version rather than its line's head" in msg
        # Not the "line moved" wording, whose hint — check out again — would
        # discard the very tree the author checked out on purpose.
        assert "moved" not in msg
        assert f"app checkout --channel {_CONV} --dir" in hint and "--bump patch" in hint
        # A failed publish leaves the working copy as the author left it.
        assert (tmp_path / "manifest.yaml").read_text() == 'version: "0.2.1"\n'
        assert read_baseline(tmp_path).historical is True

    def test_a_product_checkout_keeps_its_own_refusal(self, tmp_path):
        """Product is the more fundamental problem — forking comes first."""
        from popcorn_cli.commands import app as mod

        _historical_checkout(tmp_path)
        base = read_baseline(tmp_path)
        base.kind = "product"
        write_baseline(tmp_path, base)
        with (
            patch("popcorn_cli.cli._get_client", return_value=object()),
            pytest.raises(PopcornError) as exc,
        ):
            mod._app_publish(_publish_args(tmp_path))
        assert "PRODUCT version" in str(exc.value)


class TestStatusOfHistorical:
    def test_says_past_version_not_moved_line(self, tmp_path):
        from popcorn_cli.commands import app as mod

        _historical_checkout(tmp_path)
        captured: dict = {}
        head = _files_response(
            {"manifest.yaml": 'version: "0.2.0"\n', "alert.yaml": "name: alert\n"},
            ref="head",
            version_id=7,
            semver="0.2.0",
        )
        with (
            patch("popcorn_cli.cli._get_client", return_value=object()),
            patch(
                "popcorn_cli.cli._output",
                lambda a, data, rendered: captured.update(data=data, rendered=rendered),
            ),
            patch.object(operations, "get_channel_app_files", return_value=head),
            patch.object(operations, "get_channel_app_file", return_value={"content": "a: 1\n"}),
        ):
            mod._app_status(argparse.Namespace(directory=str(tmp_path), channel=None, json=False))

        assert captured["data"]["historical"] is True
        assert captured["data"]["in_sync"] is False
        # Untouched since checkout: not dirty, yet different from the head.
        assert captured["data"]["dirty"] is False
        assert captured["data"]["changed"] == ["manifest.yaml"]
        rendered = captured["rendered"]
        assert "A past version (checked out with --version)" in rendered
        assert "Fork line moved" not in rendered
        assert "Differs from the fork line's head:" in rendered
        assert "Uncommitted edits" not in rendered


class TestTemplateCheckOfHistorical:
    def test_an_untouched_past_version_is_not_told_to_bump(self, tmp_path):
        """Nothing publishes from it, so "will a publish accept this version?"
        has no answer; an old tree must pass the check it would always pass."""
        from popcorn_core.template_check import check_bundle

        _historical_checkout(tmp_path)
        (tmp_path / "manifest.yaml").write_text(
            'app_type: alerttracker\nversion: "0.1.0"\ndescription: x\n'
        )
        codes = {f.code for f in check_bundle(str(tmp_path)).findings}
        assert "version-not-advanced" not in codes

    def test_the_same_tree_as_a_head_checkout_still_is(self, tmp_path):
        from popcorn_core.template_check import check_bundle

        _historical_checkout(tmp_path)
        (tmp_path / "manifest.yaml").write_text(
            'app_type: alerttracker\nversion: "0.1.0"\ndescription: x\n'
        )
        base = read_baseline(tmp_path)
        base.historical = False
        write_baseline(tmp_path, base)
        codes = {f.code for f in check_bundle(str(tmp_path)).findings}
        assert "version-not-advanced" in codes
