"""`popcorn template` — offline checks on a channel-template bundle.

`flow validate` asks the server whether one flow's references resolve.
`template check` asks, with no server and no channel, whether the *bundle*
holds together: whether the importer will install what you think it will, and
whether the pieces agree with each other across files.

The two are complementary and neither subsumes the other. Everything this
catches passes validation cleanly.
"""

from __future__ import annotations

import argparse

from popcorn_core.template_check import ERROR, BundleReport, check_bundle

from ..registry import Argument, Command, Subcommand, register

_LEVEL_LABEL = {"error": "error  ", "warning": "warning"}


def _summary_line(report: BundleReport) -> str:
    flows = len(report.flows)
    fixtures = len(report.fixtures)
    manifest = "manifest" if report.manifest is not None else "no manifest"
    return (
        f"Checked {report.directory} — {flows} flow{'s' if flows != 1 else ''}, "
        f"{fixtures} fixture{'s' if fixtures != 1 else ''}, {manifest}."
    )


def _render(report: BundleReport) -> str:
    lines = [_summary_line(report)]
    if report.flows:
        lines.append("  flows: " + ", ".join(sorted(f.name for f in report.flows)))
    if report.findings:
        lines.append("")
    # Errors first — a warning is advisory, an error means the bundle will not
    # do what it says.
    ordered = sorted(report.findings, key=lambda f: (f.level != ERROR, f.code, f.where))
    for finding in ordered:
        lines.append(f"  {_LEVEL_LABEL[finding.level]}  {finding.code}  [{finding.where}]")
        lines.append(f"      {finding.message}")
    lines.append("")
    errors, warnings = len(report.errors), len(report.warnings)
    lines.append(
        f"{errors} error{'s' if errors != 1 else ''}, "
        f"{warnings} warning{'s' if warnings != 1 else ''}"
    )
    return "\n".join(lines)


def _template_check(args: argparse.Namespace) -> None:
    from popcorn_core.errors import PopcornError

    from ..cli import _output

    report = check_bundle(args.directory)
    _output(args, report.to_dict(), _render(report))

    if report.errors:
        raise PopcornError(
            f"{len(report.errors)} structural error(s) in {args.directory}",
            error_code="validation",
        )
    # --strict is for CI, where an unreviewed warning is how a bundle drifts.
    if getattr(args, "strict", False) and report.warnings:
        raise PopcornError(
            f"{len(report.warnings)} warning(s) in {args.directory} (--strict)",
            error_code="validation",
        )


register(
    Command(
        name="template",
        category="flows",
        description="Channel-template bundle commands (offline structural check)",
        subcommands=[
            Subcommand(
                "check",
                "Check a bundle's structure offline — no channel, no server",
                _template_check,
                [
                    Argument("directory", "Bundle directory", positional=True),
                    Argument(
                        "strict",
                        "Exit non-zero on warnings as well as errors",
                        action="store_true",
                    ),
                ],
            ),
        ],
    )
)
