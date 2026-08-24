"""Tests for `popcorn app` publish — the diff, the refusals, the baseline.

The refusals are the point. Each mirrors something the server would reject
anyway, so what these prove is that an obvious mistake fails LOCALLY with a
message naming the fix.

The mirror image matters just as much and has its own tests: a path the
installer ignores must NOT be refused, because the server's own disk collector
(`bundle_file_tree`) filters those silently — `claimcoordinator` ships an
`evals/` directory that way — and refusing them would reject
`examples/alerttracker`, the layout the CLI's docs teach.
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
    read_baseline,
    write_baseline,
)
from popcorn_core.app_publish import (
    collect_tree,
    diff_tree,
    manifest_version,
    parse_semver,
    publish_payload,
    require_bump,
)
from popcorn_core.errors import PopcornError

_CONV = "11111111-2222-3333-4444-555555555555"


def _manifest(version: str = "0.2.0") -> str:
    return f'app_type: alerttracker\nversion: "{version}"\n'


def _files_response(files: dict[str, str], **over):
    payload = {
        "ok": True,
        "app": "alerttracker",
        "kind": "fork",
        "version_id": 7,
        "semver": "0.2.0",
        "files": [{"path": p, "content": c} for p, c in sorted(files.items())],
    }
    payload.update(over)
    return payload


def _checkout(directory: Path, files: dict[str, str], **over) -> Baseline:
    """A working copy plus the baseline it would have been checked out with."""
    from popcorn_core.app_publish import local_digest

    directory.mkdir(parents=True, exist_ok=True)
    for path, content in files.items():
        target = directory / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    fields = {
        "app": "alerttracker",
        "kind": "fork",
        "semver": "0.2.0",
        "base_version_id": 7,
        "tree_digest": local_digest(files),
        "conversation_id": _CONV,
    }
    fields.update(over)
    baseline = Baseline(**fields)
    write_baseline(directory, baseline)
    return baseline


# ---------------------------------------------------------------------------
# parse_semver
# ---------------------------------------------------------------------------


class TestParseSemver:
    def test_orders_numerically_not_as_text(self):
        assert parse_semver("1.10.0") > parse_semver("1.9.0")

    @pytest.mark.parametrize("bad", ["1.0", "1.0.0-rc1", "v1.0.0", "01.0.0", ""])
    def test_refuses_anything_but_major_minor_patch(self, bad):
        with pytest.raises(PopcornError):
            parse_semver(bad)

    def test_tolerates_surrounding_whitespace(self):
        assert parse_semver(" 1.2.3 ") == (1, 2, 3)


# ---------------------------------------------------------------------------
# manifest_version
# ---------------------------------------------------------------------------


class TestManifestVersion:
    def test_reads_a_quoted_version(self):
        assert manifest_version({"manifest.yaml": _manifest("1.2.3")}) == "1.2.3"

    def test_refuses_an_unquoted_version(self):
        """`version: 1.0` is a FLOAT in YAML and `1.0.0` is a string.

        The two look identical in the file, so refusing the float here is what
        keeps "1.0" out of the registry.
        """
        with pytest.raises(PopcornError) as exc:
            manifest_version({"manifest.yaml": "version: 1.0\n"})
        assert "quoted" in str(exc.value)

    def test_refuses_a_missing_version(self):
        with pytest.raises(PopcornError) as exc:
            manifest_version({"manifest.yaml": "app_type: x\n"})
        assert "version" in str(exc.value)

    def test_refuses_a_tree_with_no_manifest(self):
        with pytest.raises(PopcornError) as exc:
            manifest_version({"alert.yaml": "name: alert\n"})
        assert "manifest.yaml" in str(exc.value)

    def test_manifest_wins_over_the_legacy_config(self):
        files = {"manifest.yaml": _manifest("2.0.0"), "config.yaml": _manifest("1.0.0")}
        assert manifest_version(files) == "2.0.0"

    def test_accepts_the_legacy_config_alone(self):
        assert manifest_version({"config.yaml": _manifest("1.0.0")}) == "1.0.0"


# ---------------------------------------------------------------------------
# collect_tree
# ---------------------------------------------------------------------------


class TestCollectTree:
    def test_collects_the_installable_shape(self, tmp_path):
        _checkout(
            tmp_path,
            {
                "manifest.yaml": _manifest(),
                "alert_webhook.yaml": "name: alert_webhook\n",
                "AGENT.md": "notes\n",
                "README.md": "readme\n",
                "prompts/compose.md.j2": "hi\n",
                "templates/mail.md.j2": "hi\n",
            },
        )
        tree = collect_tree(tmp_path)
        assert tree.ignored == []
        assert set(tree.files) == {
            "manifest.yaml",
            "alert_webhook.yaml",
            "AGENT.md",
            "README.md",
            "prompts/compose.md.j2",
            "templates/mail.md.j2",
        }

    def test_skips_the_baseline_and_dotfiles_silently(self, tmp_path):
        _checkout(tmp_path, {"manifest.yaml": _manifest()})
        (tmp_path / ".DS_Store").write_text("junk")
        tree = collect_tree(tmp_path)
        assert BASELINE_FILE not in tree.files
        assert tree.ignored == []

    def test_skips_pycache_silently(self, tmp_path):
        _checkout(tmp_path, {"manifest.yaml": _manifest()})
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "x.pyc").write_bytes(b"\x00\x01")
        assert collect_tree(tmp_path).ignored == []

    def test_reports_a_misplaced_flow_directory(self, tmp_path):
        """`flows/alert.yaml` is the plausible wrong guess at the layout.

        Not published — `bundle_file_tree` skips it too — but reported, so a
        publish that changed nothing has a visible reason.
        """
        _checkout(tmp_path, {"manifest.yaml": _manifest(), "flows/alert.yaml": "x\n"})
        assert collect_tree(tmp_path).ignored == ["flows/"]

    def test_reports_a_stray_root_file(self, tmp_path):
        _checkout(tmp_path, {"manifest.yaml": _manifest(), "notes.txt": "x\n"})
        assert collect_tree(tmp_path).ignored == ["notes.txt"]

    def test_reports_a_prompt_nested_too_deep(self, tmp_path):
        _checkout(tmp_path, {"manifest.yaml": _manifest(), "prompts/a/b.md.j2": "x\n"})
        assert collect_tree(tmp_path).ignored == ["prompts/a/"]

    def test_refuses_a_binary_file_by_name(self, tmp_path):
        _checkout(tmp_path, {"manifest.yaml": _manifest()})
        (tmp_path / "prompts").mkdir()
        (tmp_path / "prompts" / "logo.png").write_bytes(b"\x89PNG\xff\xfe")
        with pytest.raises(PopcornError) as exc:
            collect_tree(tmp_path)
        assert "logo.png" in str(exc.value)


# ---------------------------------------------------------------------------
# diff_tree
# ---------------------------------------------------------------------------


class TestDiffTree:
    def test_classifies_added_changed_and_deleted(self):
        base = {"a.yaml": "1", "b.yaml": "2", "gone.yaml": "3"}
        local = {"a.yaml": "1", "b.yaml": "CHANGED", "new.yaml": "4"}
        diff = diff_tree(base, local)
        assert diff.added == ["new.yaml"]
        assert diff.changed == ["b.yaml"]
        assert diff.deletes == ["gone.yaml"]

    def test_omits_untouched_files_from_the_payload(self):
        """The endpoint passes untouched paths through byte-for-byte.

        Resending them would be harmless but would make every publish's
        changelog useless for seeing what actually moved.
        """
        diff = diff_tree({"a.yaml": "1", "b.yaml": "2"}, {"a.yaml": "1", "b.yaml": "X"})
        assert set(diff.files) == {"b.yaml"}

    def test_an_identical_tree_is_empty(self):
        assert diff_tree({"a.yaml": "1"}, {"a.yaml": "1"}).empty

    def test_a_deletion_alone_is_not_empty(self):
        assert not diff_tree({"a.yaml": "1"}, {}).empty

    def test_preserves_served_paths_this_cli_cannot_collect(self):
        """The version-skew guard, and the reason it is not paranoia.

        `collect_tree` filters the local side, so a bundle subdirectory the
        server has and this CLI predates would appear only in `base` — and
        without this it reads as "the user deleted all of it".
        """
        base = {"manifest.yaml": "1", "schemas/alert.json": "{}"}
        diff = diff_tree(base, {"manifest.yaml": "1"})
        assert diff.deletes == []
        assert diff.preserved == ["schemas/alert.json"]

    def test_a_recognized_absence_is_still_a_deletion(self):
        """The guard must not swallow the ordinary case."""
        base = {"manifest.yaml": "1", "prompts/a.md.j2": "x", "old.yaml": "y"}
        diff = diff_tree(base, {"manifest.yaml": "1"})
        assert diff.deletes == ["old.yaml", "prompts/a.md.j2"]
        assert diff.preserved == []


# ---------------------------------------------------------------------------
# require_bump
# ---------------------------------------------------------------------------


class TestRequireBump:
    def test_allows_a_forward_move(self):
        require_bump("0.2.1", "0.2.0")

    def test_compares_numerically(self):
        require_bump("1.10.0", "1.9.0")

    @pytest.mark.parametrize("local", ["0.2.0", "0.1.9"])
    def test_refuses_a_reused_or_lower_version(self, local):
        with pytest.raises(PopcornError) as exc:
            require_bump(local, "0.2.0")
        assert "0.2.0" in str(exc.value)


# ---------------------------------------------------------------------------
# publish_payload
# ---------------------------------------------------------------------------


class TestPublishPayload:
    def test_omits_an_absent_changelog(self):
        body = publish_payload(7, diff_tree({}, {"a.yaml": "1"}), None)
        assert "changelog" not in body

    def test_carries_the_base_version_id_verbatim(self):
        body = publish_payload(7, diff_tree({}, {"a.yaml": "1"}), "why")
        assert body["base_version_id"] == 7
        assert body["changelog"] == "why"


# ---------------------------------------------------------------------------
# The baseline's second format version
# ---------------------------------------------------------------------------


class TestBaselineV2:
    def test_round_trips_the_conversation(self, tmp_path):
        write_baseline(
            tmp_path,
            Baseline(
                app="alerttracker",
                semver="0.2.0",
                base_version_id=7,
                tree_digest="d",
                conversation_id=_CONV,
            ),
        )
        assert read_baseline(tmp_path).conversation_id == _CONV

    def test_a_v1_baseline_still_reads(self, tmp_path):
        """0.19.0 wrote no conversation_id and no v2 marker.

        It must parse rather than wedge every command — the fallback is an
        explicit --channel, not a rewrite of a file nobody asked us to touch.
        """
        (tmp_path / BASELINE_FILE).write_text(
            json.dumps(
                {
                    "version": 1,
                    "app": "alerttracker",
                    "kind": "fork",
                    "semver": "0.2.0",
                    "base_version_id": 7,
                    "tree_digest": "d",
                }
            )
        )
        baseline = read_baseline(tmp_path)
        assert baseline.version == 1
        assert baseline.conversation_id is None


# ---------------------------------------------------------------------------
# The commands
# ---------------------------------------------------------------------------


def _args(**over):
    base = {
        "channel": None,
        "directory": None,
        "changelog": None,
        "name": None,
        "json": False,
        "quiet": True,
        "no_color": True,
    }
    base.update(over)
    return argparse.Namespace(**base)


class _Recorder:
    """Captures what publish sent, and answers with a plausible response."""

    def __init__(self, **over):
        self.calls: list[tuple] = []
        self.response = {
            "ok": True,
            "app": "alerttracker",
            "version_id": 9,
            "semver": "0.2.1",
            "created": True,
            "install_workflow_id": "wf-1",
        }
        self.response.update(over)

    def __call__(self, client, conversation, payload):
        self.calls.append((conversation, payload))
        return self.response


def _run_publish(tmp_path, files_response, recorder, args):
    from popcorn_cli.commands import app as mod

    with (
        patch("popcorn_cli.cli._get_client", return_value=object()),
        patch("popcorn_cli.cli._output"),
        patch.object(operations, "get_channel_app_files", return_value=files_response),
        patch.object(operations, "publish_channel_app", recorder),
    ):
        mod._app_publish(args)


class TestPublishCommand:
    def test_publishes_the_diff_and_moves_the_baseline(self, tmp_path):
        base = {"manifest.yaml": _manifest("0.2.0"), "alert.yaml": "name: alert\n"}
        _checkout(tmp_path, base)
        (tmp_path / "manifest.yaml").write_text(_manifest("0.2.1"))
        (tmp_path / "alert.yaml").write_text("name: alert\nnew: yes\n")

        rec = _Recorder()
        _run_publish(tmp_path, _files_response(base), rec, _args(directory=str(tmp_path)))

        conversation, payload = rec.calls[0]
        assert conversation == _CONV
        assert set(payload["files"]) == {"manifest.yaml", "alert.yaml"}
        assert payload["deletes"] == []
        # Necessarily equal to the fetched binding here — publish refuses when
        # the two differ, so this path cannot tell them apart. Which one it
        # comes FROM is pinned by TestPublishPayload instead.
        assert payload["base_version_id"] == 7

        moved = read_baseline(tmp_path)
        assert (moved.semver, moved.base_version_id) == ("0.2.1", 9)

    def test_sends_a_deletion(self, tmp_path):
        base = {"manifest.yaml": _manifest("0.2.0"), "old.yaml": "name: old\n"}
        _checkout(tmp_path, base)
        (tmp_path / "manifest.yaml").write_text(_manifest("0.2.1"))
        (tmp_path / "old.yaml").unlink()

        rec = _Recorder()
        _run_publish(tmp_path, _files_response(base), rec, _args(directory=str(tmp_path)))
        assert rec.calls[0][1]["deletes"] == ["old.yaml"]

    def test_refuses_a_product_bound_checkout(self, tmp_path):
        """The fix is a different command, which the server's 409 cannot say."""
        _checkout(tmp_path, {"manifest.yaml": _manifest()}, kind="product")
        rec = _Recorder()
        with pytest.raises(PopcornError) as exc:
            _run_publish(tmp_path, _files_response({}), rec, _args(directory=str(tmp_path)))
        assert "app fork" in str(exc.value.hint or "")
        assert rec.calls == []

    def test_refuses_an_unbumped_version(self, tmp_path):
        base = {"manifest.yaml": _manifest("0.2.0")}
        _checkout(tmp_path, base)
        (tmp_path / "alert.yaml").write_text("name: alert\n")

        rec = _Recorder()
        with pytest.raises(PopcornError) as exc:
            _run_publish(tmp_path, _files_response(base), rec, _args(directory=str(tmp_path)))
        assert "0.2.0" in str(exc.value)
        assert rec.calls == []

    def test_refuses_an_empty_diff(self, tmp_path):
        base = {"manifest.yaml": _manifest("0.2.0")}
        _checkout(tmp_path, base)
        rec = _Recorder()
        with pytest.raises(PopcornError) as exc:
            _run_publish(tmp_path, _files_response(base), rec, _args(directory=str(tmp_path)))
        assert "nothing to publish" in str(exc.value)
        assert rec.calls == []

    def test_refuses_when_the_channel_moved_ahead(self, tmp_path):
        base = {"manifest.yaml": _manifest("0.2.0")}
        _checkout(tmp_path, base)
        (tmp_path / "manifest.yaml").write_text(_manifest("0.2.1"))

        rec = _Recorder()
        with pytest.raises(PopcornError) as exc:
            _run_publish(
                tmp_path,
                _files_response(base, version_id=11, semver="0.3.0"),
                rec,
                _args(directory=str(tmp_path)),
            )
        assert "moved to" in str(exc.value)
        assert "app checkout" in str(exc.value.hint or "")
        assert rec.calls == []

    def test_says_still_installing_when_the_channel_is_behind(self, tmp_path):
        """Our own publish landed but its install has not.

        Same "ids differ" fact as the case above, opposite cause — telling the
        user to re-checkout here would throw away the edits they just
        published.
        """
        base = {"manifest.yaml": _manifest("0.2.1")}
        _checkout(tmp_path, base, semver="0.2.1", base_version_id=9)
        (tmp_path / "manifest.yaml").write_text(_manifest("0.2.2"))

        rec = _Recorder()
        with pytest.raises(PopcornError) as exc:
            _run_publish(
                tmp_path,
                _files_response(base, version_id=7, semver="0.2.0"),
                rec,
                _args(directory=str(tmp_path)),
            )
        assert "has not landed" in str(exc.value)
        assert exc.value.retryable
        assert rec.calls == []

    def test_refuses_outside_a_checkout(self, tmp_path):
        rec = _Recorder()
        with pytest.raises(PopcornError) as exc:
            _run_publish(tmp_path, _files_response({}), rec, _args(directory=str(tmp_path)))
        assert BASELINE_FILE in str(exc.value)

    def test_a_v1_baseline_needs_an_explicit_channel(self, tmp_path):
        base = {"manifest.yaml": _manifest("0.2.0")}
        _checkout(tmp_path, base, conversation_id=None)
        (tmp_path / "manifest.yaml").write_text(_manifest("0.2.1"))

        rec = _Recorder()
        with pytest.raises(PopcornError) as exc:
            _run_publish(tmp_path, _files_response(base), rec, _args(directory=str(tmp_path)))
        assert "0.19.0" in str(exc.value)

        _run_publish(
            tmp_path,
            _files_response(base),
            rec,
            _args(directory=str(tmp_path), channel="#alerts"),
        )
        assert rec.calls[0][0] == "#alerts"


class TestStatusCommand:
    def _run(self, tmp_path, files_response, args):
        from popcorn_cli.commands import app as mod

        captured = {}

        def _capture(a, data, rendered):
            captured["data"] = data
            captured["rendered"] = rendered

        with (
            patch("popcorn_cli.cli._get_client", return_value=object()),
            patch("popcorn_cli.cli._output", _capture),
            patch.object(operations, "get_channel_app_files", return_value=files_response),
        ):
            mod._app_status(args)
        return captured

    def test_reports_a_clean_checkout(self, tmp_path):
        base = {"manifest.yaml": _manifest("0.2.0")}
        _checkout(tmp_path, base)
        out = self._run(tmp_path, _files_response(base), _args(directory=str(tmp_path)))
        assert out["data"]["in_sync"] is True
        assert out["data"]["dirty"] is False

    def test_reports_edits_without_refusing(self, tmp_path):
        base = {"manifest.yaml": _manifest("0.2.0")}
        _checkout(tmp_path, base)
        (tmp_path / "alert.yaml").write_text("name: alert\n")
        out = self._run(tmp_path, _files_response(base), _args(directory=str(tmp_path)))
        assert out["data"]["added"] == ["alert.yaml"]
        assert out["data"]["dirty"] is True

    def test_reports_a_moved_channel_instead_of_raising(self, tmp_path):
        """status is the command you run BECAUSE the two disagree."""
        base = {"manifest.yaml": _manifest("0.2.0")}
        _checkout(tmp_path, base)
        out = self._run(
            tmp_path,
            _files_response(base, version_id=11, semver="0.3.0"),
            _args(directory=str(tmp_path)),
        )
        assert out["data"]["in_sync"] is False
        assert "0.3.0" in out["rendered"]

    def test_flags_paths_that_will_not_be_published(self, tmp_path):
        base = {"manifest.yaml": _manifest("0.2.0")}
        _checkout(tmp_path, base)
        (tmp_path / "notes.txt").write_text("x")
        out = self._run(tmp_path, _files_response(base), _args(directory=str(tmp_path)))
        assert out["data"]["ignored"] == ["notes.txt"]
        assert "notes.txt" in out["rendered"]


class TestForkAndApplyCommands:
    def test_fork_passes_the_line_name_through(self):
        from popcorn_cli.commands import app as mod

        calls = []

        def _fork(client, conversation, fork_name):
            calls.append((conversation, fork_name))
            return {"ok": True, "status": "created", "app": "alerttracker", "semver": "0.2.0"}

        with (
            patch("popcorn_cli.cli._get_client", return_value=object()),
            patch("popcorn_cli.cli._output"),
            patch.object(operations, "fork_channel_app", _fork),
        ):
            mod._app_fork(_args(channel="#alerts", name="experiment"))
        assert calls == [("#alerts", "experiment")]

    def test_apply_reads_the_channel_from_the_baseline(self, tmp_path):
        from popcorn_cli.commands import app as mod

        _checkout(tmp_path, {"manifest.yaml": _manifest()})
        calls = []

        def _apply(client, conversation):
            calls.append(conversation)
            return {
                "ok": True,
                "status": "started",
                "app": "alerttracker",
                "target_semver": "0.2.1",
            }

        with (
            patch("popcorn_cli.cli._get_client", return_value=object()),
            patch("popcorn_cli.cli._output"),
            patch.object(operations, "apply_channel_app", _apply),
        ):
            mod._app_apply(_args(directory=str(tmp_path)))
        assert calls == [_CONV]
