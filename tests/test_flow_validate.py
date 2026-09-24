"""`flow validate <dir>`: which bundle files are sent as flows."""

from __future__ import annotations

import argparse
from unittest.mock import patch

from popcorn_cli.commands import flow as mod


def test_a_directory_sends_only_root_flows(tmp_path):
    """Reserved names and subdirectory files are never read as flows by the
    bundle reader, so validating them would report failures that cannot
    happen on install."""
    for name in (
        "intake.yaml",
        "digest.yml",
        "manifest.yaml",
        "config.yaml",
        "strings.yaml",
        "AGENT.md",
        "README.md",
    ):
        (tmp_path / name).write_text("steps: []\n")
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "nested.yaml").write_text("steps: []\n")

    sent: list[str] = []

    def _validate(client, channel, text):
        sent.append(text)
        return {"valid": True, "steps": []}

    args = argparse.Namespace(path=str(tmp_path), channel="#alerts")
    with (
        patch("popcorn_cli.cli._get_client", return_value=object()),
        patch("popcorn_cli.cli._output") as output,
        patch.object(mod.operations, "validate_flow_yaml", _validate),
    ):
        mod._flow_validate(args)

    files = [r["file"] for r in output.call_args.args[1]["results"]]
    assert files == [str(tmp_path / "digest.yml"), str(tmp_path / "intake.yaml")]
    assert len(sent) == 2
