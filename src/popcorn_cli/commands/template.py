"""`popcorn template check` — the old name of `popcorn app validate`.

Kept, hidden, because scripts and CI jobs outside this repository still call
it by name. It runs the same handler with the same arguments and exits the
same way; the only difference is one line on stderr naming the new command.
It is absent from `--help`, completions and `commands --json`, so nothing new
learns the old name. Delete it once those callers have moved.
"""

from __future__ import annotations

import argparse
import sys

from ..registry import Command, Subcommand, register
from .app import VALIDATE_ARGUMENTS, _app_validate

DEPRECATION_NOTICE = "popcorn template check is now popcorn app validate"


def _template_check(args: argparse.Namespace) -> None:
    print(DEPRECATION_NOTICE, file=sys.stderr)
    _app_validate(args)


register(
    Command(
        name="template",
        category="flows",
        description="Deprecated: use 'popcorn app validate'",
        # Not `deprecated=`: that note reaches `commands --json`, and a hidden
        # family is absent from it. The stderr notice is how callers learn.
        hidden=True,
        subcommands=[
            Subcommand(
                "check",
                "Deprecated name of 'popcorn app validate'",
                _template_check,
                VALIDATE_ARGUMENTS,
            ),
        ],
    )
)
