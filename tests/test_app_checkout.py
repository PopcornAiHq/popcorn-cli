"""Tests for `popcorn app` — the checkout core, its operations, and the command.

The load-bearing property is the round trip: what checkout writes must be byte
-identical to what the server served, because Phase 5's publish diffs the
working copy against that same tree. A checkout that silently normalized a
trailing newline would show up later as a spurious edit in every publish.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from popcorn_core import operations
from popcorn_core.app_checkout import (
    BASELINE_FILE,
    Baseline,
    baseline_from_response,
    files_from_response,
    occupied,
    read_baseline,
    tree_digest,
    write_baseline,
    write_tree,
)
from popcorn_core.errors import PopcornError

_CONV = "11111111-2222-3333-4444-555555555555"


def _files_response(files: dict[str, str], **over):
    payload = {
        "ok": True,
        "app": "alerttracker",
        "kind": "product",
        "version_id": 7,
        "semver": "0.2.0",
        "files": [{"path": p, "content": c} for p, c in sorted(files.items())],
    }
    payload.update(over)
    return payload


# ---------------------------------------------------------------------------
# tree_digest
# ---------------------------------------------------------------------------


class TestTreeDigest:
    def test_is_order_independent(self):
        a = tree_digest({"a.yaml": "one", "b.yaml": "two"})
        b = tree_digest({"b.yaml": "two", "a.yaml": "one"})
        assert a == b

    def test_changes_with_content(self):
        assert tree_digest({"a.yaml": "one"}) != tree_digest({"a.yaml": "two"})

    def test_changes_with_path(self):
        assert tree_digest({"a.yaml": "x"}) != tree_digest({"b.yaml": "x"})

    def test_framing_prevents_a_rename_colliding_with_an_edit(self):
        """Without length prefixes these two hash the same.

        `{"ab": "c"}` and `{"a": "bc"}` both concatenate to "abc"; the
        length-prefixed framing is the only thing separating them.
        """
        assert tree_digest({"ab": "c"}) != tree_digest({"a": "bc"})

    def test_empty_tree_is_stable(self):
        assert tree_digest({}) == tree_digest({})


# ---------------------------------------------------------------------------
# Materialization
# ---------------------------------------------------------------------------


class TestWriteTree:
    def test_round_trips_bytes_exactly(self, tmp_path):
        files = {
            "manifest.yaml": "version: '0.2.0'\nname: x\n",
            "no_trailing_newline.yaml": "a: 1",
            "unicode.md": "# Alert Tracker — ops\n",
        }
        write_tree(tmp_path, files)
        for path, content in files.items():
            assert (tmp_path / path).read_bytes() == content.encode("utf-8")

    def test_creates_nested_directories(self, tmp_path):
        write_tree(tmp_path, {"prompts/triage.md.j2": "hi"})
        assert (tmp_path / "prompts" / "triage.md.j2").read_text() == "hi"

    def test_returns_sorted_paths(self, tmp_path):
        written = write_tree(tmp_path, {"b.yaml": "1", "a.yaml": "2"})
        assert written == ["a.yaml", "b.yaml"]

    @pytest.mark.parametrize("bad", ["../escape.yaml", "a/../../escape.yaml", "/etc/passwd"])
    def test_refuses_paths_that_escape_the_directory(self, tmp_path, bad):
        """The server builds these paths, but it is still a remote string
        becoming a filesystem write."""
        with pytest.raises(PopcornError) as exc:
            write_tree(tmp_path, {bad: "x"})
        assert "unsafe path" in str(exc.value)


class TestOccupied:
    def test_missing_directory_is_free(self, tmp_path):
        assert occupied(tmp_path / "nope") is False

    def test_empty_directory_is_free(self, tmp_path):
        assert occupied(tmp_path) is False

    def test_directory_holding_only_a_baseline_is_free(self, tmp_path):
        """Re-checking out the same working copy is not a collision."""
        (tmp_path / BASELINE_FILE).write_text("{}")
        assert occupied(tmp_path) is False

    def test_directory_with_content_is_occupied(self, tmp_path):
        (tmp_path / "manifest.yaml").write_text("x")
        assert occupied(tmp_path) is True


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------


class TestBaseline:
    def test_round_trips(self, tmp_path):
        b = Baseline(
            app="alerttracker",
            semver="0.2.0",
            base_version_id=7,
            tree_digest="abc",
            kind="fork",
            fork_name="default",
        )
        write_baseline(tmp_path, b)
        back = read_baseline(tmp_path)
        assert back is not None
        assert (back.app, back.semver, back.base_version_id) == ("alerttracker", "0.2.0", 7)
        assert (back.kind, back.fork_name, back.tree_digest) == ("fork", "default", "abc")

    def test_absent_baseline_is_none(self, tmp_path):
        assert read_baseline(tmp_path) is None

    def test_corrupt_baseline_is_none_not_an_exception(self, tmp_path):
        """A corrupt baseline should send you back to checkout, not wedge
        every command that reads it."""
        (tmp_path / BASELINE_FILE).write_text("{not json")
        assert read_baseline(tmp_path) is None

    def test_baseline_without_a_version_id_is_none(self, tmp_path):
        (tmp_path / BASELINE_FILE).write_text('{"app": "x"}')
        assert read_baseline(tmp_path) is None

    def test_product_baseline_omits_fork_name(self, tmp_path):
        write_baseline(tmp_path, baseline_from_response(_files_response({"a": "b"}), {"a": "b"}))
        data = json.loads((tmp_path / BASELINE_FILE).read_text())
        assert "fork_name" not in data
        assert data["base_version_id"] == 7

    def test_digest_matches_the_written_tree(self, tmp_path):
        files = {"manifest.yaml": "v: 1\n"}
        b = baseline_from_response(_files_response(files), files)
        assert b.tree_digest == tree_digest(files)


class TestFilesFromResponse:
    def test_extracts_paths_and_content(self):
        resp = _files_response({"a.yaml": "one", "b.yaml": "two"})
        assert files_from_response(resp) == {"a.yaml": "one", "b.yaml": "two"}

    def test_tolerates_empty_content(self):
        resp = {"files": [{"path": "empty.yaml", "content": ""}]}
        assert files_from_response(resp) == {"empty.yaml": ""}

    def test_skips_entries_with_no_path(self):
        resp = {"files": [{"content": "orphan"}, {"path": "a", "content": "x"}]}
        assert files_from_response(resp) == {"a": "x"}


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


@pytest.fixture()
def _patch_resolve():
    with patch("popcorn_core.operations.resolve_conversation", side_effect=lambda _c, ref: ref):
        yield


@pytest.mark.usefixtures("_patch_resolve")
class TestOperations:
    def test_list_sends_conversation_id(self, mock_client):
        mock_client.get.return_value = {"apps": []}
        operations.list_channel_apps(mock_client, _CONV)
        mock_client.get.assert_called_once_with("/api/apps/list", {"conversation_id": _CONV})

    def test_files_sends_conversation_id(self, mock_client):
        mock_client.get.return_value = _files_response({})
        operations.get_channel_app_files(mock_client, _CONV)
        mock_client.get.assert_called_once_with("/api/apps/files", {"conversation_id": _CONV})

    def test_tree_sends_conversation_id(self, mock_client):
        mock_client.get.return_value = {"paths": []}
        operations.get_channel_app_tree(mock_client, _CONV)
        mock_client.get.assert_called_once_with("/api/apps/tree", {"conversation_id": _CONV})

    def test_file_sends_path_too(self, mock_client):
        mock_client.get.return_value = {"path": "a", "content": "b"}
        operations.get_channel_app_file(mock_client, _CONV, "AGENT.md")
        mock_client.get.assert_called_once_with(
            "/api/apps/file", {"conversation_id": _CONV, "path": "AGENT.md"}
        )


# ---------------------------------------------------------------------------
# The checkout command
# ---------------------------------------------------------------------------


def _args(**over):
    base = {
        "channel": "#alerts",
        "directory": None,
        "force": False,
        "json": False,
        "quiet": True,
        "no_color": True,
    }
    base.update(over)
    return argparse.Namespace(**base)


class TestCheckoutCommand:
    def _run(self, resp, args):
        from popcorn_cli.commands import app as mod

        with (
            patch("popcorn_cli.cli._get_client", return_value=object()),
            patch("popcorn_cli.cli._output"),
            patch.object(mod, "resolve_conversation", return_value=_CONV),
            patch.object(operations, "get_channel_app_files", return_value=resp),
        ):
            mod._app_checkout(args)

    def test_writes_tree_and_baseline(self, tmp_path):
        files = {"manifest.yaml": "version: '0.2.0'\n", "AGENT.md": "notes\n"}
        self._run(_files_response(files), _args(directory=str(tmp_path / "out")))

        out = tmp_path / "out"
        assert (out / "manifest.yaml").read_bytes() == b"version: '0.2.0'\n"
        base = read_baseline(out)
        assert base is not None and base.base_version_id == 7

    def test_defaults_the_directory_to_the_app_name(self, tmp_path, monkeypatch):
        """A bare checkout must not scatter bundle files over the cwd."""
        monkeypatch.chdir(tmp_path)
        self._run(_files_response({"manifest.yaml": "v: 1\n"}), _args())
        assert (tmp_path / "alerttracker" / "manifest.yaml").exists()

    def test_refuses_a_non_empty_directory(self, tmp_path):
        (tmp_path / "manifest.yaml").write_text("mine")
        with pytest.raises(PopcornError) as exc:
            self._run(_files_response({"manifest.yaml": "theirs"}), _args(directory=str(tmp_path)))
        assert "--force" in str(exc.value)
        assert (tmp_path / "manifest.yaml").read_text() == "mine"

    def test_force_overwrites(self, tmp_path):
        (tmp_path / "manifest.yaml").write_text("mine")
        self._run(
            _files_response({"manifest.yaml": "theirs"}),
            _args(directory=str(tmp_path), force=True),
        )
        assert (tmp_path / "manifest.yaml").read_text() == "theirs"

    def test_force_leaves_unrelated_local_files_alone(self, tmp_path):
        """--force overwrites the served files, it is not clean-and-replace."""
        (tmp_path / "manifest.yaml").write_text("mine")
        (tmp_path / "my-notes.md").write_text("keep me")
        self._run(
            _files_response({"manifest.yaml": "theirs"}),
            _args(directory=str(tmp_path), force=True),
        )
        assert (tmp_path / "my-notes.md").read_text() == "keep me"

    def test_re_checkout_over_only_a_baseline_needs_no_force(self, tmp_path):
        write_baseline(tmp_path, baseline_from_response(_files_response({}), {}))
        self._run(_files_response({"manifest.yaml": "v\n"}), _args(directory=str(tmp_path)))
        assert (tmp_path / "manifest.yaml").exists()

    def test_empty_tree_is_an_error_not_an_empty_directory(self, tmp_path):
        with pytest.raises(PopcornError) as exc:
            self._run(_files_response({}), _args(directory=str(tmp_path / "out")))
        assert "no files" in str(exc.value)


# ---------------------------------------------------------------------------
# End to end against a real bundle — the phase's "done when"
# ---------------------------------------------------------------------------

_EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def _read_bundle(directory: Path) -> dict[str, bytes]:
    """Bytes, not text — an encoding change must surface as a difference."""
    out = {}
    for p in sorted(directory.rglob("*")):
        if p.is_file() and not p.name.startswith("."):
            out[str(p.relative_to(directory))] = p.read_bytes()
    return out


@pytest.mark.parametrize("name", ["alerttracker", "deploywatch"])
def test_a_real_bundle_round_trips_and_still_passes_template_check(tmp_path, name):
    """Serve a real bundle, check it out, and prove nothing changed.

    Byte-equality is the point: `app publish` will diff the working copy
    against this tree, so any normalization here becomes a phantom edit in
    every future publish.
    """
    from popcorn_core.template_check import check_bundle

    source = _EXAMPLES / name
    if not source.is_dir():
        pytest.skip(f"{name} example not present")
    original_bytes = _read_bundle(source)
    original = {k: v.decode("utf-8") for k, v in original_bytes.items()}

    from popcorn_cli.commands import app as mod

    out = tmp_path / name
    with (
        patch("popcorn_cli.cli._get_client", return_value=object()),
        patch("popcorn_cli.cli._output"),
        patch.object(mod, "resolve_conversation", return_value=_CONV),
        patch.object(
            operations,
            "get_channel_app_files",
            return_value=_files_response(original, app=name),
        ),
    ):
        mod._app_checkout(_args(directory=str(out)))

    assert _read_bundle(out) == original_bytes, "checkout did not round-trip byte-identically"

    report = check_bundle(str(out))
    assert not report.errors, [f.code for f in report.errors]

    # The baseline must not look like bundle content to the checker or to a
    # future publish diff.
    assert (out / BASELINE_FILE).exists()
    assert BASELINE_FILE not in _read_bundle(out)
