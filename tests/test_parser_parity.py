"""Parsed-argument parity across a registry migration.

Moving a family from `cli.py` to `registry.py` re-declares its parser by hand,
and the hand-written form is deleted in the same commit — so afterwards there
is nothing left to diff against. Both losses found so far slipped through
exactly that gap: `webhook`'s mutually exclusive flow flags (caught late, by an
unrelated test), and `webhook deliveries --limit`, whose `default=50` the
registry could not express at all. That one shipped, and the command failed
against the server for anyone who did not pass `--limit`.

`fixtures/parser_parity.json` closes it. Every subcommand of every family still
declared in `cli.py` is recorded twice — once invoked minimally, once naming
every optional argument — as the exact `Namespace` the pre-migration parser
produced. Re-declaring a family has to reproduce it byte for byte.

`vm` and `site` are deprecated and are not migration candidates. They are
recorded anyway: both lean on the dual-spelled `--channel` mechanism that the
remaining migrations have to touch, so their rows are what would catch a change
to that mechanism breaking a family nobody was looking at.

Regenerate the fixture ONLY to add newly-declared arguments, never to make a
failure go away: a diff here is the alarm working. A genuinely intended change
(a renamed flag, a new default) should show up as a reviewable edit to the
recorded namespace in the same commit that causes it.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from popcorn_cli.cli import build_parser

_FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "parser_parity.json"
_CASES = json.loads(_FIXTURE.read_text())


def _normalise(namespace: object) -> dict:
    """Round-trip through JSON so the comparison matches the fixture's types."""
    plain = {
        k: v for k, v in sorted(vars(namespace).items()) if not k.startswith("_") and k != "func"
    }
    return json.loads(json.dumps(plain, default=str))


@pytest.fixture(scope="module")
def parser():
    return build_parser()


@pytest.mark.parametrize(
    "case", _CASES, ids=lambda c: " ".join(c["argv"][:2]) + f" ({len(c['argv'])})"
)
def test_parsed_namespace_is_unchanged(case, parser):
    assert _normalise(parser.parse_args(case["argv"])) == case["namespace"]


def test_the_fixture_covers_every_unmigrated_family():
    """A family dropped from the fixture would silently lose its safety net."""
    import popcorn_cli.commands  # noqa: F401  — registers the families
    from popcorn_cli import registry

    registered = {c.name for c in registry.COMMANDS}
    recorded = {c["argv"][0] for c in _CASES}
    parser_families = set()
    import argparse

    p = build_parser()
    top = next(a for a in p._actions if isinstance(a, argparse._SubParsersAction))
    for name, sub in top.choices.items():
        if any(isinstance(a, argparse._SubParsersAction) for a in sub._actions):
            parser_families.add(name)

    # A family leaves the fixture only by being migrated — at which point its
    # rows have already done their job and proved the new declaration matches.
    missing = parser_families - registered - recorded
    assert not missing, f"unmigrated families with no parity coverage: {sorted(missing)}"
