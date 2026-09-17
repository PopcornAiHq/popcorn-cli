"""Late binding for handlers that still live in `cli.py`.

A surface-only migration declares a family here while its handler bodies stay
put. cli.py imports this package at module load to build the parser, so a
module-level import of a cli.py function from a family module would close a
cycle and fail — the lookup has to happen at call time instead.

Its own module, rather than `commands/__init__.py`, so that nothing depends on
the order in which that file imports the families.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable


def late_handler(name: str) -> Callable[[argparse.Namespace], None]:
    """A callable that resolves `cli.<name>` when invoked, not when declared."""

    def run(args: argparse.Namespace) -> None:
        from .. import cli

        getattr(cli, name)(args)

    run.__name__ = name
    return run
