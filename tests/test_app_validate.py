"""`popcorn app validate` — the offline checks, plus the server's publish checks.

What carries weight here, with the HTTP call mocked throughout:

- Server findings are rendered beside the offline ones, in the same format
  and the same exit-code scheme: any error exits 1, `--strict` adds warnings.
- When the server checks cannot run — not a fork checkout, logged out, no
  edits, server unreachable — the command says so in one line and neither
  fails nor guesses. The offline findings stand alone.
- `popcorn template check` is the same command under its old name: it prints
  one notice on stderr and otherwise behaves identically, and no listing
  shows it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from popcorn_cli import registry
from popcorn_cli.cli import build_parser, cmd_commands, cmd_completion, main
from popcorn_cli.commands.template import DEPRECATION_NOTICE
from popcorn_core.app_publish import collect_tree, file_sha256
from popcorn_core.errors import APIError, AuthError, PopcornError
from tests.test_template_check import CLEAN_MANIFEST, write_baseline_file, write_bundle

_CONV = "00000000-0000-4000-8000-000000000001"
_CLIENT = object()

_TABLE_FINDING = {
    "message": "tables.widgets: column 'Seen At': merge='concat' requires a string column",
    "rule": "value_error",
    "table": "widgets",
    "column": "Seen At",
}
_BUNDLE_FINDING = {
    "message": "flow 'intake' does not parse: extra key",
    "rule": "unparseable_flow",
}


def _checkout(tmp_path: Path, *, kind: str = "fork", manifest: Any = None) -> Path:
    """A fork checkout of 1.0.0 whose manifest now says 1.0.1."""
    root = write_bundle(
        tmp_path / "co",
        manifest=manifest or {**CLEAN_MANIFEST, "version": "1.0.1", "changelog": "Edited."},
    )
    write_baseline_file(root, "1.0.0", kind=kind, conversation_id=_CONV)
    return root


def _base_hashes(root: Path, *, edited: bool = True) -> dict[str, str]:
    """The line head's hashes: the working copy, less the manifest edit."""
    hashes = {p: file_sha256(t) for p, t in collect_tree(root).files.items()}
    if edited:
        hashes["manifest.yaml"] = "0" * 64
    return hashes


def _run(
    monkeypatch,
    capsys,
    argv: list[str],
    *,
    findings: list[dict] | None = None,
    client: Any = _CLIENT,
    base: Any = None,
    validate_error: Exception | None = None,
) -> dict[str, Any]:
    """Run `popcorn <argv>` through `main`, returning exit code, output and calls."""
    monkeypatch.setenv("POPCORN_NO_UPDATE_CHECK", "1")
    monkeypatch.setattr(sys, "argv", ["popcorn", *argv])
    calls: list[dict] = []
    channels: list[str] = []

    def get_client(args):
        if isinstance(client, Exception):
            raise client
        return client

    def fetch_base(c, conversation, baseline):
        channels.append(conversation)
        if isinstance(base, Exception):
            raise base
        return {}, base

    def validate(c, conversation, payload):
        calls.append(payload)
        channels.append(conversation)
        if validate_error is not None:
            raise validate_error
        return {"ok": not findings, "findings": findings or []}

    code = 0
    with (
        patch("popcorn_cli.cli._get_client", side_effect=get_client),
        patch("popcorn_cli.commands.app._fetch_base", side_effect=fetch_base),
        patch("popcorn_core.operations.validate_app_bundle", side_effect=validate),
    ):
        try:
            main()
        except SystemExit as exc:
            code = int(exc.code or 0)
    out = capsys.readouterr()
    return {"code": code, "out": out.out, "err": out.err, "calls": calls, "channels": channels}


# ---------------------------------------------------------------------------
# The server ran
# ---------------------------------------------------------------------------


def test_server_findings_are_rendered_and_fail_the_check(tmp_path, monkeypatch, capsys):
    root = _checkout(tmp_path)
    result = _run(
        monkeypatch,
        capsys,
        ["app", "validate", str(root)],
        findings=[_TABLE_FINDING, _BUNDLE_FINDING],
        base=_base_hashes(root),
    )

    assert result["code"] == 1
    out = result["out"]
    assert "server checks: ran" in out
    assert "publish-refused (value_error)  [manifest.yaml:tables.widgets]" in out
    assert _TABLE_FINDING["message"] in out
    assert "publish-refused (unparseable_flow)  [bundle]" in out
    assert "2 errors, 0 warnings" in out
    # Exactly the body `app publish` would send: the edit over the baseline.
    (payload,) = result["calls"]
    # The channel authorizes the call; by default the baseline's.
    assert result["channels"] == [_CONV, _CONV]
    assert payload["base_version_id"] == 41
    assert list(payload["files"]) == ["manifest.yaml"]
    assert payload["deletes"] == []


def test_a_clean_server_answer_passes(tmp_path, monkeypatch, capsys):
    root = _checkout(tmp_path)
    result = _run(
        monkeypatch, capsys, ["app", "validate", str(root)], findings=[], base=_base_hashes(root)
    )
    assert result["code"] == 0
    assert "server checks: ran" in result["out"]
    assert "0 errors" in result["out"]


def test_server_findings_reach_json_with_their_rule(tmp_path, monkeypatch, capsys):
    root = _checkout(tmp_path)
    result = _run(
        monkeypatch,
        capsys,
        ["app", "validate", str(root), "--json"],
        findings=[_TABLE_FINDING],
        base=_base_hashes(root),
    )
    assert result["code"] == 1
    data = json.loads(result["out"])["data"]
    assert data["server"] == {"status": "ran"}
    (finding,) = [f for f in data["findings"] if f["code"] == "publish-refused"]
    assert finding == {
        "level": "error",
        "code": "publish-refused",
        "where": "manifest.yaml:tables.widgets",
        "message": _TABLE_FINDING["message"],
        "rule": "value_error",
    }
    # Offline findings keep their shape: no `rule` key.
    assert all("rule" not in f for f in data["findings"] if f["code"] != "publish-refused")


def test_a_moved_line_is_a_refusal_not_a_skip(tmp_path, monkeypatch, capsys):
    """Publish refuses a stale base, so validate must not pass one."""
    root = _checkout(tmp_path)
    moved = PopcornError("the fork line moved to widgets 1.0.2", error_code="conflict")
    result = _run(monkeypatch, capsys, ["app", "validate", str(root)], base=moved)
    assert result["code"] == 1
    assert "publish-refused (stale_base)" in result["out"]
    assert result["calls"] == []


# ---------------------------------------------------------------------------
# The server could not run: say why, never fail on it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "setup,reason",
    [
        ("logged_out", "server checks: skipped (not logged in)"),
        ("unreachable", "server checks: skipped (the server did not answer: Cannot connect"),
        ("no_edits", "server checks: skipped (no edits since checkout"),
        ("not_a_checkout", "server checks: skipped (not an app checkout"),
        ("product", "server checks: skipped (a product checkout"),
    ],
)
def test_a_skipped_server_check_is_said_and_does_not_fail(
    tmp_path, monkeypatch, capsys, setup, reason
):
    root = _checkout(tmp_path, kind="product" if setup == "product" else "fork")
    kwargs: dict[str, Any] = {"base": _base_hashes(root)}
    if setup == "logged_out":
        kwargs["client"] = AuthError("Not logged in")
    elif setup == "unreachable":
        kwargs["validate_error"] = APIError("Cannot connect to https://api.example.invalid")
    elif setup == "no_edits":
        kwargs["base"] = _base_hashes(root, edited=False)
    elif setup == "not_a_checkout":
        (root / ".popcorn-app.json").unlink()

    result = _run(monkeypatch, capsys, ["app", "validate", str(root), "--strict"], **kwargs)

    assert reason in result["out"]
    # Even under --strict: a skip is the absence of an answer, not a warning.
    assert result["code"] == 0, result["out"] + result["err"]
    if setup != "unreachable":
        assert result["calls"] == []


def test_channel_flag_overrides_the_baseline(tmp_path, monkeypatch, capsys):
    root = _checkout(tmp_path)
    result = _run(
        monkeypatch,
        capsys,
        ["app", "validate", str(root), "--channel", "#example-ops"],
        findings=[],
        base=_base_hashes(root),
    )
    assert result["code"] == 0
    assert result["channels"] == ["#example-ops", "#example-ops"]


def test_no_channel_anywhere_skips_the_server_checks(tmp_path, monkeypatch, capsys):
    """A baseline that records no channel, and no --channel: say so, like
    logged out, and never call the server without its authorization."""
    root = _checkout(tmp_path)
    write_baseline_file(root, "1.0.0", kind="fork")
    result = _run(
        monkeypatch,
        capsys,
        ["app", "validate", str(root), "--strict"],
        findings=[_TABLE_FINDING],
        base=_base_hashes(root),
    )
    assert result["code"] == 0, result["out"]
    assert "server checks: skipped (no channel" in result["out"]
    assert "pass --channel" in result["out"]
    assert result["calls"] == [] and result["channels"] == []


def test_offline_errors_still_fail_when_the_server_is_skipped(tmp_path, monkeypatch, capsys):
    manifest = {
        **CLEAN_MANIFEST,
        "version": "1.0.1",
        "webhooks": [{"name": "In", "flow": "intak"}],
    }
    root = _checkout(tmp_path, manifest=manifest)
    result = _run(
        monkeypatch,
        capsys,
        ["app", "validate", str(root)],
        client=AuthError("Not logged in"),
    )
    assert result["code"] == 1
    assert "webhook-unknown-flow" in result["out"]
    assert "server checks: skipped (not logged in)" in result["out"]


# ---------------------------------------------------------------------------
# `template check`, the old name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("findings,code", [([], 0), ([_TABLE_FINDING], 1)])
def test_template_check_is_app_validate_with_a_notice(
    tmp_path, monkeypatch, capsys, findings, code
):
    root = _checkout(tmp_path)
    new = _run(
        monkeypatch,
        capsys,
        ["app", "validate", str(root)],
        findings=findings,
        base=_base_hashes(root),
    )
    old = _run(
        monkeypatch,
        capsys,
        ["template", "check", str(root)],
        findings=findings,
        base=_base_hashes(root),
    )

    assert old["code"] == new["code"] == code
    assert old["out"] == new["out"]
    assert old["err"].splitlines()[0] == DEPRECATION_NOTICE
    assert DEPRECATION_NOTICE not in new["err"]


def test_template_check_keeps_its_arguments():
    args = build_parser().parse_args(["template", "check", "--dir", "x", "--strict"])
    assert (args.command, args.template_command) == ("template", "check")
    assert (args.directory, args.strict) == ("x", True)


def test_template_is_listed_nowhere(capsys):
    parser = build_parser()
    help_text = parser.format_help()
    assert "\n  template " not in help_text
    assert "validate" in next(ln for ln in help_text.splitlines() if ln.strip().startswith("app "))

    cmd_commands(argparse.Namespace(command="commands", groups=None))
    schema = json.loads(capsys.readouterr().out)
    names = {c["name"] for c in schema["commands"]}
    assert "template" not in names
    app = next(c for c in schema["commands"] if c["name"] == "app")
    assert "validate" in {s["name"] for s in app["subcommands"]}

    assert "template" not in {name for name, _ in registry.completion_groups()}
    for shell in ("bash", "zsh"):
        cmd_completion(argparse.Namespace(shell=shell))
        out = capsys.readouterr().out
        # `templates` (a channel subcommand) is fine; the family word is not.
        words = set(out.replace("'", " ").replace('"', " ").replace(":", " ").split())
        assert "template" not in words, shell
        assert "validate" in words, shell


def test_a_typo_never_suggests_the_hidden_name(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["popcorn", "templat"])
    with pytest.raises(SystemExit):
        build_parser().parse_args(["templat"])
    assert '"template"' not in capsys.readouterr().err
