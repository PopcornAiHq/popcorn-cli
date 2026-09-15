"""Tests for `popcorn app` publish — the diff, the refusals, the baseline.

The refusals are the point. Each mirrors something the server would reject
anyway, so what these prove is that an obvious mistake fails LOCALLY with a
message naming the fix.

The mirror image matters just as much and has its own tests: a path the
installer ignores must NOT be refused, because the server's own disk collector
(`bundle_file_tree`) filters those silently — `claimcoordinator` ships an
`evals/` directory that way — and refusing them would reject the ordinary
bundle layout, of which `tests/fixtures/bundles/alerttracker` is an example.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
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
    bump_manifest_text,
    collect_tree,
    diff_tree,
    fork_line_reach,
    manifest_changelog,
    manifest_version,
    next_version,
    parse_semver,
    publish_payload,
    require_bump,
    unrecognized_code_paths,
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

    def test_collects_a_code_block_at_any_depth(self, tmp_path):
        """The bug this fixes: `code/` was appended to `ignored` and dropped.

        An author could check a block out, edit it, publish, and see no
        change. Depth is a FLOOR under `code/`, not the exact two levels a
        prompts/ file gets, because a block may be a small package.
        """
        _checkout(
            tmp_path,
            {
                "manifest.yaml": _manifest(),
                "code/calc/main.py": "print(1)\n",
                "code/calc/lib/util.py": "x = 1\n",
                "code/other-block/index.js": "0\n",
            },
        )
        tree = collect_tree(tmp_path)
        assert tree.ignored == []
        assert set(tree.files) == {
            "manifest.yaml",
            "code/calc/main.py",
            "code/calc/lib/util.py",
            "code/other-block/index.js",
        }

    def test_skips_hidden_and_pycache_below_a_block(self, tmp_path):
        """The same filter the server's walker applies, and for the reason
        `template_check` documents: publish refuses a tree carrying a hidden
        block entry, so never collecting one keeps the CLI from minting it."""
        _checkout(
            tmp_path,
            {
                "manifest.yaml": _manifest(),
                "code/calc/main.py": "print(1)\n",
                "code/calc/.env": "SECRET=1\n",
                "code/calc/__pycache__/main.pyc": "junk\n",
            },
        )
        tree = collect_tree(tmp_path)
        assert set(tree.files) == {"manifest.yaml", "code/calc/main.py"}
        assert tree.ignored == []

    def test_keeps_a_misplaced_code_path_for_the_refusal(self, tmp_path):
        """Collected, not filtered — the server's `bundle_file_tree` keeps a
        loose `code/x.py` for the same reason: dropping it here would publish
        a half-block and report success."""
        _checkout(
            tmp_path,
            {
                "manifest.yaml": _manifest(),
                "code/loose.py": "x\n",
                "code/Calc/main.py": "x\n",
            },
        )
        tree = collect_tree(tmp_path)
        assert "code/loose.py" in tree.files
        assert "code/Calc/main.py" in tree.files
        assert unrecognized_code_paths(tree.files) == ["code/Calc/main.py", "code/loose.py"]

    def test_a_legal_block_tree_has_nothing_to_refuse(self, tmp_path):
        _checkout(
            tmp_path,
            {"manifest.yaml": _manifest(), "code/calc/lib/deep/util.py": "x\n"},
        )
        assert unrecognized_code_paths(collect_tree(tmp_path).files) == []

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

    def test_a_deleted_block_file_is_a_deletion(self):
        """`code/` is collected now, so its absence is a real deletion —
        before the fix every block path fell through to `preserved`."""
        base = {"manifest.yaml": "1", "code/calc/main.py": "x", "code/calc/lib/util.py": "y"}
        diff = diff_tree(base, {"manifest.yaml": "1", "code/calc/main.py": "x"})
        assert diff.deletes == ["code/calc/lib/util.py"]
        assert diff.preserved == []

    def test_a_served_block_shape_this_cli_cannot_classify_is_preserved(self):
        """Recognition stays stricter than collection under `code/`.

        Nothing served can reach this today — the server refuses to publish
        such a tree — but a block shape a stale `flow_rules` predates would,
        and preserving it beats deleting it.
        """
        base = {"manifest.yaml": "1", "code/singlefile.py": "x"}
        diff = diff_tree(base, {"manifest.yaml": "1"})
        assert diff.deletes == []
        assert diff.preserved == ["code/singlefile.py"]

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
# --bump: the arithmetic, and the manifest rewrite
# ---------------------------------------------------------------------------


class TestNextVersion:
    @pytest.mark.parametrize(
        ("part", "expected"),
        [("patch", "1.4.8"), ("minor", "1.5.0"), ("major", "2.0.0")],
    )
    def test_bumps_and_zeroes_everything_below(self, part, expected):
        assert next_version("1.4.7", part) == expected

    def test_carries_past_nine(self):
        assert next_version("1.9.9", "patch") == "1.9.10"

    def test_refuses_a_part_it_does_not_know(self):
        with pytest.raises(PopcornError):
            next_version("1.0.0", "build")

    def test_refuses_a_version_it_cannot_parse(self):
        with pytest.raises(PopcornError):
            next_version("1.0", "patch")


class TestBumpManifestText:
    """The rewrite, and the `sed` trap that is the reason it exists."""

    def test_the_naive_sed_pattern_matches_nothing(self):
        """KEW-2367's sharp edge, pinned so nobody "simplifies" back to it.

        A manifest quotes its version, so the obvious
        `sed -E 's/^version: [0-9.]+/…/'` matches no line and exits 0 —
        the bump looks applied, and only `app publish` refusing the unchanged
        version says otherwise, a round trip later.
        """
        text = 'app_type: alerttracker\nversion: "1.34.2"\n'
        assert re.search(r"^version: [0-9.]+$", text, re.MULTILINE) is None
        assert 'version: "1.34.3"' in bump_manifest_text(text, "1.34.3")

    def test_preserves_double_quotes(self):
        assert bump_manifest_text('version: "1.0.0"\n', "1.0.1") == 'version: "1.0.1"\n'

    def test_preserves_single_quotes(self):
        assert bump_manifest_text("version: '1.0.0'\n", "1.0.1") == "version: '1.0.1'\n"

    def test_quotes_a_bare_value_on_the_way_out(self):
        """`version: 1.0` is a float and `version: 1.0.0` a string, and the
        file cannot show you which — so the rewrite always emits the quoted
        form the rest of this module tells authors to write."""
        assert bump_manifest_text("version: 1.0.0\n", "1.1.0") == 'version: "1.1.0"\n'

    def test_keeps_a_trailing_comment(self):
        assert (
            bump_manifest_text('version: "1.0.0"  # bumped by hand\n', "1.0.1")
            == 'version: "1.0.1"  # bumped by hand\n'
        )

    def test_leaves_the_rest_of_the_document_alone(self):
        text = 'app_type: alerttracker\nversion: "0.2.0"\n\n# a comment\ntables:\n  - alerts\n'
        out = bump_manifest_text(text, "0.3.0")
        assert out == text.replace('"0.2.0"', '"0.3.0"')

    def test_ignores_a_nested_version_key(self):
        """Only the document's own version is the one a publish mints."""
        text = 'version: "0.2.0"\ndeps:\n  version: "9.9.9"\n'
        out = bump_manifest_text(text, "0.2.1")
        assert out == 'version: "0.2.1"\ndeps:\n  version: "9.9.9"\n'

    def test_raises_when_there_is_no_version_line(self):
        with pytest.raises(PopcornError) as exc:
            bump_manifest_text("app_type: alerttracker\n", "0.1.0")
        assert "version:" in str(exc.value)


class TestManifestChangelog:
    def test_reads_the_declared_field(self):
        files = {"manifest.yaml": 'version: "1.0.0"\nchangelog: last release\n'}
        assert manifest_changelog(files) == "last release"

    def test_none_when_undeclared(self):
        assert manifest_changelog({"manifest.yaml": 'version: "1.0.0"\n'}) is None

    def test_none_rather_than_raising_without_a_manifest(self):
        """Read only to warn, so it must never be the thing that fails a
        publish — `manifest_version` owns that refusal and says it better."""
        assert manifest_changelog({}) is None


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
        # `--changelog` is the deprecated ALIAS of this dest, not a second
        # one: the parser folds both spellings into `message`, so a namespace
        # that still carried `changelog` would be testing a shape the CLI
        # cannot produce. `test_parser` pins the folding itself.
        "message": None,
        "bump": None,
        "name": None,
        "json": False,
        "quiet": True,
        "no_color": True,
    }
    base.update(over)
    return argparse.Namespace(**base)


def _listing(*fork_names: str, app: str = "alerttracker", semver: str = "0.2.0") -> dict:
    """An `app list` response: one product entry, one per fork line."""
    return {
        "apps": [
            {"kind": "product", "app": app, "semver": "1.37.0"},
            *({"kind": "fork", "app": app, "fork_name": n, "semver": semver} for n in fork_names),
        ],
        "channel": {"app": app, "kind": "product", "semver": "1.37.0"},
    }


class _ForkRecorder:
    """Captures what fork was asked for, and answers plausibly."""

    def __init__(self, **over):
        self.calls: list[tuple] = []
        self.response = {
            "ok": True,
            "status": "adopting",
            "app": "alerttracker",
            "semver": "0.2.0",
        }
        self.response.update(over)

    def __call__(self, client, conversation, fork_name=None):
        self.calls.append((conversation, fork_name))
        return dict(self.response)


@contextlib.contextmanager
def _fork_env(rec: _ForkRecorder, listing: dict):
    """Patched fork/list operations; the yielded dict records list calls."""
    env: dict = {"listed": []}

    def _list(client, conversation):
        env["listed"].append(conversation)
        return listing

    with (
        patch("popcorn_cli.cli._get_client", return_value=object()),
        patch("popcorn_cli.cli._output"),
        patch.object(operations, "list_channel_apps", _list),
        patch.object(operations, "fork_channel_app", rec),
    ):
        yield env


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


class TestForkLineReach:
    """A publish is workspace-scoped in effect; the output has to say so."""

    def test_reports_the_other_channels_and_the_version(self):
        note = fork_line_reach({"other_channels_converging": 6, "semver": "0.2.1"})
        assert "6 other channels" in note
        assert "0.2.1" in note

    def test_says_channel_singular_for_one(self):
        note = fork_line_reach({"other_channels_converging": 1, "semver": "0.2.1"})
        assert "1 other channel " in note

    def test_silent_when_the_publisher_is_the_only_channel(self):
        assert fork_line_reach({"other_channels_converging": 0, "semver": "0.2.1"}) == ""

    def test_silent_when_the_server_did_not_send_a_count(self):
        """A popcorn newer than the API must not claim a reach of zero.

        "0 other channels" and "the server never told me" are different
        facts, and the first reads as "this affects only you".
        """
        assert fork_line_reach({"semver": "0.2.1"}) == ""
        assert fork_line_reach({"other_channels_converging": None}) == ""


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

    def test_the_moved_baseline_records_the_note_that_just_shipped(self, tmp_path):
        """So the NEXT bump is measured against what was published, not
        against whatever the last `app checkout` happened to see."""
        base = {"manifest.yaml": _manifest("0.2.0") + "changelog: The old note.\n"}
        _checkout(tmp_path, base)
        (tmp_path / "manifest.yaml").write_text(_manifest("0.2.1") + "changelog: The new note.\n")

        _run_publish(tmp_path, _files_response(base), _Recorder(), _args(directory=str(tmp_path)))
        assert read_baseline(tmp_path).changelog == "The new note."

    def test_sends_a_deletion(self, tmp_path):
        base = {"manifest.yaml": _manifest("0.2.0"), "old.yaml": "name: old\n"}
        _checkout(tmp_path, base)
        (tmp_path / "manifest.yaml").write_text(_manifest("0.2.1"))
        (tmp_path / "old.yaml").unlink()

        rec = _Recorder()
        _run_publish(tmp_path, _files_response(base), rec, _args(directory=str(tmp_path)))
        assert rec.calls[0][1]["deletes"] == ["old.yaml"]

    def test_sends_an_edited_code_block(self, tmp_path):
        """The end-to-end shape of the bug: checkout, edit a block, publish.

        Before this, the edit never reached `files` and the publish reported
        success having changed nothing.
        """
        base = {
            "manifest.yaml": _manifest("0.2.0"),
            "code/calc/main.py": "print(1)\n",
            "code/calc/lib/util.py": "x = 1\n",
        }
        _checkout(tmp_path, base)
        (tmp_path / "manifest.yaml").write_text(_manifest("0.2.1"))
        (tmp_path / "code" / "calc" / "main.py").write_text("print(2)\n")

        rec = _Recorder()
        _run_publish(tmp_path, _files_response(base), rec, _args(directory=str(tmp_path)))

        payload = rec.calls[0][1]
        assert set(payload["files"]) == {"manifest.yaml", "code/calc/main.py"}
        assert payload["files"]["code/calc/main.py"] == "print(2)\n"
        assert payload["deletes"] == []

    def test_refuses_a_misplaced_code_path_before_the_round_trip(self, tmp_path):
        """The server rejects the whole tree over one such path; its message
        cannot name the working copy the author is standing in."""
        base = {"manifest.yaml": _manifest("0.2.0")}
        _checkout(tmp_path, base)
        (tmp_path / "manifest.yaml").write_text(_manifest("0.2.1"))
        (tmp_path / "code").mkdir()
        (tmp_path / "code" / "loose.py").write_text("x\n")

        rec = _Recorder()
        with pytest.raises(PopcornError) as exc:
            _run_publish(tmp_path, _files_response(base), rec, _args(directory=str(tmp_path)))
        assert "code/loose.py" in str(exc.value)
        assert "code/<block>/" in str(exc.value.hint or "")
        assert rec.calls == []

    def test_refuses_a_block_name_that_is_not_a_slug(self, tmp_path):
        base = {"manifest.yaml": _manifest("0.2.0")}
        _checkout(tmp_path, base)
        (tmp_path / "manifest.yaml").write_text(_manifest("0.2.1"))
        (tmp_path / "code" / "Calc").mkdir(parents=True)
        (tmp_path / "code" / "Calc" / "main.py").write_text("x\n")

        rec = _Recorder()
        with pytest.raises(PopcornError) as exc:
            _run_publish(tmp_path, _files_response(base), rec, _args(directory=str(tmp_path)))
        assert "code/Calc/main.py" in str(exc.value)
        assert rec.calls == []

    def test_output_names_the_channels_a_publish_will_reach(self, tmp_path):
        base = {"manifest.yaml": _manifest("0.2.0")}
        _checkout(tmp_path, base)
        (tmp_path / "manifest.yaml").write_text(_manifest("0.2.1"))

        rec = _Recorder(other_channels_converging=6, semver="0.2.1")
        captured = {}
        from popcorn_cli.commands import app as mod

        with (
            patch("popcorn_cli.cli._get_client", return_value=object()),
            patch(
                "popcorn_cli.cli._output",
                lambda a, data, rendered: captured.update(data=data, rendered=rendered),
            ),
            patch.object(operations, "get_channel_app_files", return_value=_files_response(base)),
            patch.object(operations, "publish_channel_app", rec),
        ):
            mod._app_publish(_args(directory=str(tmp_path)))

        assert "6 other channels on this fork line" in captured["rendered"]
        # The raw count rides through to --json for an agent to branch on.
        assert captured["data"]["other_channels_converging"] == 6

    def test_output_stays_quiet_when_no_other_channel_is_on_the_line(self, tmp_path):
        base = {"manifest.yaml": _manifest("0.2.0")}
        _checkout(tmp_path, base)
        (tmp_path / "manifest.yaml").write_text(_manifest("0.2.1"))

        rec = _Recorder(other_channels_converging=0)
        captured = {}
        from popcorn_cli.commands import app as mod

        with (
            patch("popcorn_cli.cli._get_client", return_value=object()),
            patch(
                "popcorn_cli.cli._output",
                lambda a, data, rendered: captured.update(rendered=rendered),
            ),
            patch.object(operations, "get_channel_app_files", return_value=_files_response(base)),
            patch.object(operations, "publish_channel_app", rec),
        ):
            mod._app_publish(_args(directory=str(tmp_path)))

        assert "fork line" not in captured["rendered"]

    def test_refuses_a_product_bound_checkout(self, tmp_path):
        """The fix is a different command, which the server's 409 cannot say."""
        _checkout(tmp_path, {"manifest.yaml": _manifest()}, kind="product")
        rec = _Recorder()
        with pytest.raises(PopcornError) as exc:
            _run_publish(tmp_path, _files_response({}), rec, _args(directory=str(tmp_path)))
        # The one-command recovery (KEW-2362), not the old fork-then-checkout
        # pair: `app checkout --fork` does both and cannot be half-done.
        assert "app checkout" in str(exc.value.hint or "")
        assert "--fork" in str(exc.value.hint or "")
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

    def test_refuses_when_the_fork_line_moved_ahead(self, tmp_path):
        """Someone else published on the line since this checkout. The diff
        base is gone, and the server would refuse the stale base anyway."""
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
        assert "fork line moved to" in str(exc.value)
        assert "app checkout" in str(exc.value.hint or "")
        assert rec.calls == []

    def test_publishes_from_the_head_while_the_channel_is_behind(self, tmp_path):
        """The deadlock popcorn-backend #1985 removed.

        The head's install failed, so the channel still runs the previous
        version. The checkout IS the head, the publish is based on it, and
        nothing about the channel's state may stop it — the old client-side
        "install has not landed, wait" refusal was what wedged the line.
        """
        base = {"manifest.yaml": _manifest("0.2.0")}
        _checkout(tmp_path, base)
        (tmp_path / "manifest.yaml").write_text(_manifest("0.2.1"))

        rec = _Recorder()
        _run_publish(
            tmp_path,
            _files_response(base, ref="head", bound_version_id=5, bound_semver="0.1.0"),
            rec,
            _args(directory=str(tmp_path)),
        )
        assert len(rec.calls) == 1
        assert rec.calls[0][1]["base_version_id"] == 7

    def test_fetches_the_head_not_the_bound_version(self, tmp_path):
        base = {"manifest.yaml": _manifest("0.2.0")}
        _checkout(tmp_path, base)
        (tmp_path / "manifest.yaml").write_text(_manifest("0.2.1"))
        from popcorn_cli.commands import app as mod

        seen: dict = {}

        def _files(client, conversation, ref="head"):
            seen["ref"] = ref
            return _files_response(base)

        with (
            patch("popcorn_cli.cli._get_client", return_value=object()),
            patch("popcorn_cli.cli._output"),
            patch.object(operations, "get_channel_app_files", _files),
            patch.object(operations, "publish_channel_app", _Recorder()),
        ):
            mod._app_publish(_args(directory=str(tmp_path)))
        assert seen["ref"] == "head"

    def test_the_servers_stale_base_refusal_is_shown_verbatim(self, tmp_path):
        """The server owns the base check; its 409 already says what to do."""
        from popcorn_core.errors import APIError

        base = {"manifest.yaml": _manifest("0.2.0")}
        _checkout(tmp_path, base)
        (tmp_path / "manifest.yaml").write_text(_manifest("0.2.1"))
        detail = (
            "the checkout is alerttracker@0.2.0 but the fork line's head is 0.3.0 — "
            "check out the head (ref=head) and redo the edits on that tree"
        )

        def _refuse(client, conversation, payload):
            raise APIError(detail, status_code=409)

        with pytest.raises(PopcornError) as exc:
            _run_publish(tmp_path, _files_response(base), _refuse, _args(directory=str(tmp_path)))
        assert str(exc.value) == detail

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


def _run_publish_captured(tmp_path, recorder):
    """Publish a one-line manifest bump and return what was rendered."""
    from popcorn_cli.commands import app as mod

    base = {"manifest.yaml": _manifest("0.2.0")}
    _checkout(tmp_path, base)
    (tmp_path / "manifest.yaml").write_text(_manifest("0.2.1"))
    captured: dict = {}
    with (
        patch("popcorn_cli.cli._get_client", return_value=object()),
        patch(
            "popcorn_cli.cli._output",
            lambda a, data, rendered: captured.update(data=data, rendered=rendered),
        ),
        patch.object(operations, "get_channel_app_files", return_value=_files_response(base)),
        patch.object(operations, "publish_channel_app", recorder),
    ):
        mod._app_publish(_args(directory=str(tmp_path)))
    return captured


class TestPublishInstallStatus:
    """The publish succeeded in every case here; only the INSTALL differs."""

    def test_started_points_at_status(self, tmp_path):
        out = _run_publish_captured(tmp_path, _Recorder(install_status="started"))
        assert "Installing on this channel: wf-1" in out["rendered"]
        assert "popcorn app status" in out["rendered"]

    def test_locked_channel_says_so_and_points_at_apply(self, tmp_path):
        out = _run_publish_captured(
            tmp_path,
            _Recorder(install_status="blocked_app_updates_locked", install_workflow_id=None),
        )
        assert "Published alerttracker 0.2.1" in out["rendered"]
        assert (
            "Not applied to this channel: app updates are locked here — ask a "
            "member to unlock them, then run 'popcorn app apply'" in out["rendered"]
        )

    def test_running_install_points_at_apply(self, tmp_path):
        out = _run_publish_captured(
            tmp_path,
            _Recorder(install_status="blocked_install_in_progress", install_workflow_id=None),
        )
        assert "another install holds this channel's lock" in out["rendered"]
        assert "popcorn app apply" in out["rendered"]

    def test_not_requested_does_not_crash(self, tmp_path):
        """Cannot happen from this CLI (it always names the channel), but the
        value exists on the wire and must render, not raise."""
        out = _run_publish_captured(
            tmp_path, _Recorder(install_status="not_requested", install_workflow_id=None)
        )
        assert "Not applied to any channel." in out["rendered"]

    def test_an_older_api_with_only_a_workflow_id_still_reads_as_started(self, tmp_path):
        out = _run_publish_captured(tmp_path, _Recorder())
        assert "Installing on this channel: wf-1" in out["rendered"]


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

    def test_reports_a_moved_line_instead_of_raising(self, tmp_path):
        """status is the command you run BECAUSE the two disagree."""
        base = {"manifest.yaml": _manifest("0.2.0")}
        _checkout(tmp_path, base)
        out = self._run(
            tmp_path,
            _files_response(base, version_id=11, semver="0.3.0"),
            _args(directory=str(tmp_path)),
        )
        assert out["data"]["in_sync"] is False
        assert "Fork line moved to 0.3.0" in out["rendered"]

    def test_reports_a_channel_behind_the_head_without_refusing(self, tmp_path):
        """Baseline == head, channel behind: the install has not landed (or
        failed). Say so from the server's own fields and point at apply — no
        semver guesswork, no refusal."""
        base = {"manifest.yaml": _manifest("0.2.0")}
        _checkout(tmp_path, base)
        out = self._run(
            tmp_path,
            _files_response(base, ref="head", bound_version_id=5, bound_semver="0.1.0"),
            _args(directory=str(tmp_path)),
        )
        assert out["data"]["in_sync"] is True
        assert out["data"]["channel_behind"] is True
        assert (out["data"]["channel_version_id"], out["data"]["head_version_id"]) == (5, 7)
        assert "Channel still runs 0.1.0 (version 5)" in out["rendered"]
        assert "popcorn app apply" in out["rendered"]

    def test_reports_a_current_channel(self, tmp_path):
        base = {"manifest.yaml": _manifest("0.2.0")}
        _checkout(tmp_path, base)
        out = self._run(
            tmp_path,
            _files_response(base, ref="head", bound_version_id=7, bound_semver="0.2.0"),
            _args(directory=str(tmp_path)),
        )
        assert out["data"]["channel_behind"] is False
        assert "Channel runs the same version (7)." in out["rendered"]

    def test_flags_a_misplaced_code_path_without_refusing(self, tmp_path):
        """status must not refuse the way publish does — it is the command
        you run to find out why publish will."""
        base = {"manifest.yaml": _manifest("0.2.0")}
        _checkout(tmp_path, base)
        (tmp_path / "code").mkdir()
        (tmp_path / "code" / "loose.py").write_text("x")
        out = self._run(tmp_path, _files_response(base), _args(directory=str(tmp_path)))
        assert out["data"]["unpublishable"] == ["code/loose.py"]
        assert "code/loose.py" in out["rendered"]

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

    def test_a_named_fork_does_not_ask_the_server_which_lines_exist(self):
        """The extra round trip is only earned on the path that infers."""
        rec = _ForkRecorder()
        with _fork_env(rec, listing=_listing("default")) as env:
            from popcorn_cli.commands import app as mod

            mod._app_fork(_args(channel="#alerts", name="experiment"))
        assert env["listed"] == []
        assert rec.calls == [("#alerts", "experiment")]


class TestNamelessFork:
    """A nameless fork adopts whatever single line exists, wherever it has got
    to — 23 minor versions behind product, in the workspace that prompted
    KEW-2362. So it discloses the line first, on every path."""

    def test_discloses_and_confirms_the_line_it_would_adopt(self, capsys, tty):
        rec = _ForkRecorder()
        prompts = tty("y")
        with _fork_env(rec, listing=_listing("default", semver="1.14.0")):
            from popcorn_cli.commands import app as mod

            mod._app_fork(_args(channel="#alerts"))

        err = capsys.readouterr().err
        assert "default" in err and "1.14.0" in err
        assert prompts, "the inferred line must be confirmed, not just announced"
        assert rec.calls == [("#alerts", None)]

    def test_declining_forks_nothing(self, capsys, tty):
        rec = _ForkRecorder()
        tty("n")
        with _fork_env(rec, listing=_listing("default")):
            from popcorn_cli.commands import app as mod

            with pytest.raises(PopcornError) as exc:
                mod._app_fork(_args(channel="#alerts"))
        assert "cancelled" in str(exc.value)
        assert rec.calls == []

    def test_yes_still_prints_the_line_it_adopted(self, capsys):
        """`-y` answers the prompt; it must not silence the disclosure, which
        is the half that helps the agents most exposed to this."""
        rec = _ForkRecorder()
        with _fork_env(rec, listing=_listing("default", semver="1.14.0")):
            from popcorn_cli.commands import app as mod

            mod._app_fork(_args(channel="#alerts", yes=True, quiet=True))

        err = capsys.readouterr().err
        assert "default" in err and "1.14.0" in err
        assert rec.calls == [("#alerts", None)]

    def test_non_interactive_without_yes_fails_rather_than_hangs(self):
        """pytest's stdin is not a TTY, which is the case this covers."""
        rec = _ForkRecorder()
        with _fork_env(rec, listing=_listing("default")):
            from popcorn_cli.commands import app as mod

            with pytest.raises(PopcornError) as exc:
                mod._app_fork(_args(channel="#alerts"))
        assert "--yes" in str(exc.value)
        assert rec.calls == []

    def test_no_existing_line_needs_no_confirmation(self, capsys):
        """Nothing is being adopted — the server mints 'default'."""
        rec = _ForkRecorder()
        with _fork_env(rec, listing=_listing()):
            from popcorn_cli.commands import app as mod

            mod._app_fork(_args(channel="#alerts"))
        assert rec.calls == [("#alerts", None)]
        assert "adopting" not in capsys.readouterr().err.lower()

    def test_several_lines_are_left_to_the_server_to_refuse(self, capsys):
        """The 2+ refusal already names every line. Duplicating it here would
        be a second copy to drift out of step with the server's."""
        rec = _ForkRecorder()
        with _fork_env(rec, listing=_listing("default", "demo914")):
            from popcorn_cli.commands import app as mod

            mod._app_fork(_args(channel="#alerts"))
        assert rec.calls == [("#alerts", None)]
        assert capsys.readouterr().err == ""

    def test_ignores_fork_lines_of_other_apps(self, capsys, tty):
        """A workspace owns lines per app; only the channel's app is at stake,
        so one line of it plus one of something else is still an inference."""
        rec = _ForkRecorder()
        listing = _listing("default")
        listing["apps"].append({"kind": "fork", "app": "deploywatch", "fork_name": "other"})
        prompts = tty("y")
        with _fork_env(rec, listing=listing):
            from popcorn_cli.commands import app as mod

            mod._app_fork(_args(channel="#alerts"))
        assert prompts and "default" in prompts[0]
        assert "deploywatch" not in capsys.readouterr().err

    def test_an_unreported_line_is_not_guessed_to_be_default(self, capsys, tty):
        """The server is the only thing that knows the name. When it withholds
        it, saying "default" is not a safe guess but a wrong answer on every
        workspace that named its line — and right only by coincidence on the
        first one, because "default" is what the backend mints (KEW-2375)."""
        rec = _ForkRecorder()
        listing = _listing()
        listing["apps"].append({"kind": "fork", "app": "alerttracker", "semver": "1.14.0"})
        prompts = tty("y")
        with _fork_env(rec, listing=listing):
            from popcorn_cli.commands import app as mod

            mod._app_fork(_args(channel="#alerts"))

        err = capsys.readouterr().err
        assert "default" not in err
        assert "no name" in err and "1.14.0" in err
        assert prompts and "default" not in prompts[0]
        assert rec.calls == [("#alerts", None)]

    def test_declining_an_unreported_line_cannot_suggest_a_name(self, tty):
        """The cancel hint's whole job is to hand back a name to re-run with.
        With no name to hand back it must say so, not offer --name 'default'."""
        rec = _ForkRecorder()
        tty("n")
        listing = _listing()
        listing["apps"].append({"kind": "fork", "app": "alerttracker", "semver": "1.14.0"})
        with _fork_env(rec, listing=listing):
            from popcorn_cli.commands import app as mod

            with pytest.raises(PopcornError) as exc:
                mod._app_fork(_args(channel="#alerts"))
        assert "default" not in str(exc.value.hint)
        assert "--name" in str(exc.value.hint)
        assert rec.calls == []

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


# ---------------------------------------------------------------------------
# `app publish --bump` (KEW-2367)
# ---------------------------------------------------------------------------


class TestPublishBump:
    """`--bump` writes the manifest so no scripted loop has to.

    Every manifest here is the quoted form a real one uses, which is the
    shape the hand-rolled `sed` in every authoring script fails on.
    """

    def _edited(self, tmp_path, version="0.2.0"):
        """A checkout at `version` with one real edit and no version bump."""
        base = {"manifest.yaml": _manifest(version), "alert.yaml": "name: alert\n"}
        _checkout(tmp_path, base)
        (tmp_path / "alert.yaml").write_text("name: alert\nnew: yes\n")
        return base

    @pytest.mark.parametrize(
        ("part", "expected"),
        [("patch", "0.2.1"), ("minor", "0.3.0"), ("major", "1.0.0")],
    )
    def test_mints_the_next_version_off_the_lines_head(self, tmp_path, part, expected):
        base = self._edited(tmp_path)
        rec = _Recorder(semver=expected)
        _run_publish(
            tmp_path, _files_response(base), rec, _args(directory=str(tmp_path), bump=part)
        )

        payload = rec.calls[0][1]
        assert f'version: "{expected}"' in payload["files"]["manifest.yaml"]
        assert read_baseline(tmp_path).semver == expected

    def test_writes_the_bumped_manifest_to_disk(self, tmp_path):
        """The working copy must match what published, or the next `--bump`
        would count from a version the line has already moved past."""
        base = self._edited(tmp_path)
        _run_publish(
            tmp_path,
            _files_response(base),
            _Recorder(semver="0.2.1"),
            _args(directory=str(tmp_path), bump="patch"),
        )
        assert (tmp_path / "manifest.yaml").read_text() == _manifest("0.2.1")

    def test_leaves_the_manifest_alone_when_the_publish_fails(self, tmp_path):
        """So re-running the same `--bump patch` is the retry, not a second
        bump stacked on the first."""
        from popcorn_core.errors import APIError

        base = self._edited(tmp_path)
        before = (tmp_path / "manifest.yaml").read_text()

        def _refuse(client, conversation, payload):
            raise APIError("nope", status_code=500)

        with pytest.raises(PopcornError):
            _run_publish(
                tmp_path,
                _files_response(base),
                _refuse,
                _args(directory=str(tmp_path), bump="patch"),
            )
        assert (tmp_path / "manifest.yaml").read_text() == before

    def test_an_untouched_checkout_still_publishes_nothing(self, tmp_path):
        """`--bump` on a clean working copy must not mint a version whose
        only content is its own number — the wasted version KEW-2368 is
        about. Emptiness is judged before the bump, never after."""
        base = {"manifest.yaml": _manifest("0.2.0")}
        _checkout(tmp_path, base)
        rec = _Recorder()
        with pytest.raises(PopcornError) as exc:
            _run_publish(
                tmp_path, _files_response(base), rec, _args(directory=str(tmp_path), bump="patch")
            )
        assert "nothing to publish" in str(exc.value)
        assert rec.calls == []
        assert (tmp_path / "manifest.yaml").read_text() == _manifest("0.2.0")

    def test_refuses_when_the_manifest_already_advances(self, tmp_path):
        """Two answers, no way to tell which was meant — so neither is taken.

        The error names both candidates and the two one-keystroke ways out.
        """
        base = self._edited(tmp_path)
        (tmp_path / "manifest.yaml").write_text(_manifest("0.5.0"))

        rec = _Recorder()
        with pytest.raises(PopcornError) as exc:
            _run_publish(
                tmp_path, _files_response(base), rec, _args(directory=str(tmp_path), bump="patch")
            )
        assert "0.5.0" in str(exc.value) and "0.2.0" in str(exc.value)
        assert "0.2.1" in str(exc.value) and "0.5.1" in str(exc.value)
        assert "--bump" in str(exc.value.hint or "")
        # Refused before the round trip: nothing about this needs the server.
        assert rec.calls == []

    def test_a_hand_edited_version_publishes_without_the_flag(self, tmp_path):
        """The refusal above is about `--bump` only — editing by hand stays
        the supported path, since that is what KEW-2366 still has to catch."""
        base = self._edited(tmp_path)
        (tmp_path / "manifest.yaml").write_text(_manifest("0.5.0"))
        rec = _Recorder(semver="0.5.0")
        _run_publish(tmp_path, _files_response(base), rec, _args(directory=str(tmp_path)))
        assert read_baseline(tmp_path).semver == "0.5.0"

    def test_bumps_over_a_stale_manifest_that_never_advanced(self, tmp_path):
        """The case the ticket opens with: the `sed` no-opped, so the manifest
        still holds the published version. `--bump` is exactly the fix."""
        base = self._edited(tmp_path)
        assert (tmp_path / "manifest.yaml").read_text() == _manifest("0.2.0")
        rec = _Recorder(semver="0.2.1")
        _run_publish(
            tmp_path, _files_response(base), rec, _args(directory=str(tmp_path), bump="patch")
        )
        assert 'version: "0.2.1"' in rec.calls[0][1]["files"]["manifest.yaml"]


# ---------------------------------------------------------------------------
# `app publish --message` / `-m` (KEW-2368)
# ---------------------------------------------------------------------------


class TestPublishMessage:
    def _edited(self, tmp_path, manifest=None):
        base = {"manifest.yaml": manifest or _manifest("0.2.0")}
        _checkout(tmp_path, base)
        (tmp_path / "manifest.yaml").write_text(_manifest("0.2.1"))
        return base

    def test_the_message_reaches_the_wire_field(self, tmp_path):
        """The flag is `--message`; the API field stays `changelog`."""
        base = self._edited(tmp_path)
        rec = _Recorder()
        _run_publish(
            tmp_path, _files_response(base), rec, _args(directory=str(tmp_path), message="why")
        )
        assert rec.calls[0][1]["changelog"] == "why"

    def test_no_message_sends_no_field(self, tmp_path):
        base = self._edited(tmp_path)
        rec = _Recorder()
        _run_publish(tmp_path, _files_response(base), rec, _args(directory=str(tmp_path)))
        assert "changelog" not in rec.calls[0][1]

    def test_the_output_reads_the_recorded_message_back(self, tmp_path):
        """The only readback there is: no endpoint serves a published
        version's message, so this line is the whole of it."""
        base = self._edited(tmp_path)
        captured = {}
        from popcorn_cli.commands import app as mod

        with (
            patch("popcorn_cli.cli._get_client", return_value=object()),
            patch(
                "popcorn_cli.cli._output",
                lambda a, data, rendered: captured.update(data=data, rendered=rendered),
            ),
            patch.object(operations, "get_channel_app_files", return_value=_files_response(base)),
            patch.object(operations, "publish_channel_app", _Recorder()),
        ):
            mod._app_publish(_args(directory=str(tmp_path), message="dedupe by fingerprint"))

        assert "Message: dedupe by fingerprint" in captured["rendered"]
        # And for an agent, which cannot read the rendered text.
        assert captured["data"]["message"] == "dedupe by fingerprint"

    def test_the_output_says_so_when_nothing_was_recorded(self, tmp_path):
        out = _run_publish_captured(tmp_path, _Recorder())
        assert "No message recorded on this version" in out["rendered"]
        assert out["data"]["message"] is None

    def test_warns_that_a_manifest_changelog_records_nothing(self, tmp_path, capsys):
        """`/apps/publish` records the request's changelog and never falls
        back to the manifest on the fork path, so a manifest `changelog:`
        with no `-m` lands nowhere. Silent before this."""
        manifest = 'app_type: alerttracker\nversion: "0.2.0"\nchangelog: the previous release\n'
        base = self._edited(tmp_path, manifest=manifest)
        (tmp_path / "manifest.yaml").write_text(manifest.replace("0.2.0", "0.2.1"))

        _run_publish(tmp_path, _files_response(base), _Recorder(), _args(directory=str(tmp_path)))
        assert "is not recorded by a publish" in capsys.readouterr().err

    def test_quiet_when_the_flag_supplies_the_message(self, tmp_path, capsys):
        manifest = 'app_type: alerttracker\nversion: "0.2.0"\nchangelog: the previous release\n'
        base = self._edited(tmp_path, manifest=manifest)
        (tmp_path / "manifest.yaml").write_text(manifest.replace("0.2.0", "0.2.1"))

        _run_publish(
            tmp_path,
            _files_response(base),
            _Recorder(),
            _args(directory=str(tmp_path), message="what actually changed"),
        )
        assert "is not recorded" not in capsys.readouterr().err


class TestDeprecatedChangelogAlias:
    """`--changelog` still works, and says it has been renamed."""

    def test_detects_the_old_spelling(self):
        from popcorn_cli.commands.app import _deprecated_changelog_used

        assert _deprecated_changelog_used(["app", "publish", "--changelog", "why"])
        assert _deprecated_changelog_used(["app", "publish", "--changelog=why"])

    def test_ignores_the_new_spelling(self):
        from popcorn_cli.commands.app import _deprecated_changelog_used

        assert not _deprecated_changelog_used(["app", "publish", "-m", "why"])
        assert not _deprecated_changelog_used(["app", "publish", "--message", "why"])

    def test_the_notice_names_the_replacement(self, tmp_path, capsys):
        base = {"manifest.yaml": _manifest("0.2.0")}
        _checkout(tmp_path, base)
        (tmp_path / "manifest.yaml").write_text(_manifest("0.2.1"))

        rec = _Recorder()
        with patch.object(sys, "argv", ["popcorn", "app", "publish", "--changelog", "why"]):
            _run_publish(
                tmp_path, _files_response(base), rec, _args(directory=str(tmp_path), message="why")
            )

        err = capsys.readouterr().err
        assert "--changelog is deprecated" in err and "--message" in err
        # Deprecated, not broken: the message still reaches the wire.
        assert rec.calls[0][1]["changelog"] == "why"


# ---------------------------------------------------------------------------
# KEW-2370 — `app status --channel`, with no checkout
# ---------------------------------------------------------------------------


def _tree_response(**over) -> dict:
    """An `/apps/tree?ref=head` response: the line's head plus the binding."""
    payload = {
        "ok": True,
        "app": "alerttracker",
        "kind": "fork",
        "version_id": 7,
        "semver": "0.2.0",
        "ref": "head",
        "bound_version_id": 7,
        "bound_semver": "0.2.0",
        "paths": ["manifest.yaml"],
    }
    payload.update(over)
    return payload


def _binding(**over) -> dict:
    payload = {
        "ok": True,
        "apps": [],
        "channel": {
            "app": "alerttracker",
            "kind": "fork",
            "fork_name": "demo914",
            "version_id": 7,
            "semver": "0.2.0",
        },
    }
    payload.update(over)
    return payload


class TestChannelScopedStatus:
    """ "Has my publish landed?" answered without a checkout.

    Before this, `app status` required one, so every caller polled
    `app list --channel` and string-matched a semver out of its prose.
    """

    def _run(self, args, listing=None, tree=None):
        from popcorn_cli.commands import app as mod

        captured = {}
        with (
            patch("popcorn_cli.cli._get_client", return_value=object()),
            patch(
                "popcorn_cli.cli._output",
                lambda a, data, rendered: captured.update(data=data, rendered=rendered),
            ),
            patch.object(operations, "list_channel_apps", return_value=listing or _binding()),
            patch.object(operations, "get_channel_app_tree", return_value=tree or _tree_response()),
        ):
            mod._app_status(args)
        return captured

    def test_a_landed_install_reads_as_current(self, tmp_path):
        out = self._run(_args(directory=str(tmp_path), channel="#chan"))
        assert out["data"]["install_state"] == "current"
        assert out["data"]["channel_behind"] is False
        assert out["data"]["channel_version_id"] == out["data"]["head_version_id"] == 7
        assert "CURRENT" in out["rendered"]

    def test_a_channel_behind_its_line_reads_as_pending(self, tmp_path):
        out = self._run(
            _args(directory=str(tmp_path), channel="#chan"),
            tree=_tree_response(bound_version_id=5, bound_semver="0.1.0"),
        )
        assert out["data"]["install_state"] == "pending"
        assert out["data"]["channel_behind"] is True
        assert (out["data"]["channel_semver"], out["data"]["head_semver"]) == ("0.1.0", "0.2.0")
        assert "PENDING" in out["rendered"]
        assert "popcorn app apply --channel #chan" in out["rendered"]

    def test_pending_says_the_job_status_is_not_readable(self, tmp_path):
        """The honest half of KEW-2370: the API exposes no status for the
        install job, so a failed install and a running one look the same and
        the output must not imply otherwise."""
        out = self._run(
            _args(directory=str(tmp_path), channel="#chan"),
            tree=_tree_response(bound_version_id=5, bound_semver="0.1.0"),
        )
        assert "not readable from the API" in out["rendered"]

    def test_it_reads_the_line_head_not_the_bound_version(self, tmp_path):
        from popcorn_cli.commands import app as mod

        refs = []

        def _tree(client, conversation, ref="bound"):
            refs.append(ref)
            return _tree_response()

        with (
            patch("popcorn_cli.cli._get_client", return_value=object()),
            patch("popcorn_cli.cli._output"),
            patch.object(operations, "list_channel_apps", return_value=_binding()),
            patch.object(operations, "get_channel_app_tree", _tree),
        ):
            mod._app_status(_args(directory=str(tmp_path), channel="#chan"))
        assert refs == ["head"], "a status that reads ref=bound can never see a pending install"

    def test_it_names_the_fork_line(self, tmp_path):
        out = self._run(_args(directory=str(tmp_path), channel="#chan"))
        assert out["data"]["fork_name"] == "demo914"
        assert "line demo914" in out["rendered"]

    def test_a_channel_with_no_bundle_says_so(self, tmp_path):
        from popcorn_cli.commands import app as mod

        with (
            patch("popcorn_cli.cli._get_client", return_value=object()),
            patch.object(operations, "list_channel_apps", return_value=_binding(channel=None)),
            pytest.raises(PopcornError) as exc,
        ):
            mod._app_status(_args(directory=str(tmp_path), channel="#chan"))
        assert exc.value.error_code == "not_found"
        assert "does not run an app bundle" in str(exc.value)

    def test_an_older_api_without_bound_fields_falls_back_to_the_binding(self, tmp_path):
        """popcorn-backend before #1985 sends no `bound_*`; then the served
        version IS the bound one and the channel cannot read as behind."""
        tree = _tree_response()
        del tree["bound_version_id"]
        del tree["bound_semver"]
        out = self._run(_args(directory=str(tmp_path), channel="#chan"), tree=tree)
        assert out["data"]["install_state"] == "current"
        assert out["data"]["channel_semver"] == "0.2.0"

    def test_no_checkout_and_no_channel_points_at_the_flag(self, tmp_path):
        from popcorn_cli.commands import app as mod

        with pytest.raises(PopcornError) as exc:
            mod._app_status(_args(directory=str(tmp_path)))
        assert exc.value.error_code == "not_found"
        # Not merely that the hint mentions `--channel` — the old one did too,
        # while pointing at `app checkout`. It has to offer the checkout-free
        # read, which is the thing that did not exist before.
        assert "without a checkout" in (exc.value.hint or "")

    def test_a_checkout_keeps_its_own_behaviour_when_channel_is_passed(self, tmp_path):
        """MUST NOT CHANGE: inside a checkout `--channel` still names the
        channel to compare the working copy against — the one case a v1
        baseline (0.19.0, no channel recorded) depends on. This test passes
        with the feature reverted, which is the point of it."""
        from popcorn_cli.commands import app as mod

        base = {"manifest.yaml": _manifest("0.2.0")}
        _checkout(tmp_path, base, conversation_id=None)
        captured = {}
        with (
            patch("popcorn_cli.cli._get_client", return_value=object()),
            patch(
                "popcorn_cli.cli._output",
                lambda a, data, rendered: captured.update(data=data, rendered=rendered),
            ),
            patch.object(operations, "get_channel_app_files", return_value=_files_response(base)),
        ):
            mod._app_status(_args(directory=str(tmp_path), channel="#chan"))
        # The checkout view, not the channel view: it carries the working copy.
        assert "dirty" in captured["data"]
        assert "install_state" not in captured["data"]


class TestApplyReadsAsRecovery:
    """KEW-2373: publish → install converged on the first poll across roughly
    a dozen publishes, and `apply` was never needed once. Documenting it as a
    routine step in the loop is what invites it into scripts."""

    def _subcommand(self, name):
        from popcorn_cli.registry import COMMANDS

        app = next(c for c in COMMANDS if c.name == "app")
        return next(s for s in app.subcommands if s.name == name)

    def test_the_apply_help_names_it_as_a_retry(self):
        help_text = self._subcommand("apply").help.lower()
        assert "recovery" in help_text or "retry" in help_text
        assert "did not land" in help_text

    def test_the_documented_loop_does_not_end_in_apply(self):
        from popcorn_cli.commands import app as mod

        loop = next(
            ln for ln in (mod.__doc__ or "").splitlines() if ln.strip().startswith("app fork")
        )
        assert "app publish" in loop
        assert "app apply" not in loop

    def test_a_started_install_is_not_presented_as_an_outstanding_step(self):
        """It says how to confirm, not what to do next — nothing is required
        of the caller once the install is running."""
        from popcorn_cli.commands import app as mod

        rendered = "\n".join(mod._install_lines({"install_status": "started", "wf": None}))
        assert "Next:" not in rendered
        assert "converges on its own" in rendered

    def test_a_blocked_install_still_points_at_apply(self):
        """The inverse guard: apply is genuinely the fix here, and softening
        every mention of it would lose that."""
        from popcorn_cli.commands import app as mod

        for status in ("blocked_app_updates_locked", "blocked_install_in_progress"):
            rendered = "\n".join(mod._install_lines({"install_status": status}))
            assert "popcorn app apply" in rendered, status

    def test_an_unreported_line_is_not_rendered_as_default(self):
        """`_ForkRecorder`'s response carries no fork_name on purpose: it is
        what a server predating KEW-2375 returns on the already_fork path, and
        agent mode reads exactly this rendering."""
        from popcorn_cli.commands import app as mod

        rendered = "\n".join(
            mod._fork_lines({"status": "already_fork", "app": "a", "semver": "1.0.0"})
        )
        assert "default" not in rendered
        assert "not reported" in rendered

    def test_a_reported_line_is_still_named_plainly(self):
        """The inverse guard — the caveat must not leak onto the happy path."""
        from popcorn_cli.commands import app as mod

        rendered = "\n".join(
            mod._fork_lines(
                {"status": "already_fork", "app": "a", "semver": "1.0.0", "fork_name": "demo914"}
            )
        )
        assert "(line demo914)" in rendered
        assert "not reported" not in rendered

    def test_the_adopting_note_points_at_status_not_a_list_poll(self):
        """`app list` was the old answer and is what callers grepped a semver
        out of (KEW-2370)."""
        from popcorn_cli.commands import app as mod

        rendered = "\n".join(mod._fork_lines({"status": "adopting", "app": "a", "semver": "1.0.0"}))
        assert "popcorn app status --channel" in rendered
        assert "popcorn app list" not in rendered
