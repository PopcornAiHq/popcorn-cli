"""Single source of truth for command surfaces.

A family declared here feeds every consumer that previously each carried its
own hand-maintained copy: argparse construction, dispatch, bash completion,
zsh completion, the `popcorn commands --json` schema, and the fuzzy-match
candidate list. `popcorn commands --json` top-level keys are frozen at 1.0.0
(SPEC.md), so a hand-maintained duplicate is a chance to drift a frozen
contract — hence one declaration.

Existing families still live in cli.py and are migrated incrementally; see
docs/architecture-commands.md.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

_DEST_SUFFIX = "_command"


@dataclass
class Argument:
    """One argparse argument, declared once.

    `name` uses either hyphens or underscores for flags (both become
    `--hyphen-case` with an underscore dest); positionals use `name` verbatim,
    so declare those with underscores.
    """

    name: str
    help: str
    required: bool = False
    type: type | None = None
    action: str | None = None
    choices: list[str] | None = None
    positional: bool = False

    def add_to(self, parser: argparse.ArgumentParser) -> None:
        kwargs: dict[str, Any] = {"help": self.help}
        if self.action:
            kwargs["action"] = self.action
        else:
            if self.type:
                kwargs["type"] = self.type
            if self.choices:
                kwargs["choices"] = self.choices
        if self.positional:
            parser.add_argument(self.name, **kwargs)
        else:
            if self.required:
                kwargs["required"] = True
            parser.add_argument(f"--{self.name.replace('_', '-')}", **kwargs)


@dataclass
class Subcommand:
    """A leaf subcommand (with a `handler`) or a group (with `subcommands`)."""

    name: str
    help: str
    handler: Callable[[argparse.Namespace], None] | None = None
    arguments: list[Argument] = field(default_factory=list)
    subcommands: list[Subcommand] = field(default_factory=list)


@dataclass
class Command:
    """A top-level command family, e.g. `popcorn flow`."""

    name: str
    category: str
    description: str
    subcommands: list[Subcommand] = field(default_factory=list)


COMMANDS: list[Command] = []


def register(command: Command) -> Command:
    COMMANDS.append(command)
    return command


def _nested_dest(dest: str, sub_name: str) -> str:
    """`flow_command` + `runs` → `flow_runs_command`. Recurses to any depth."""
    return f"{dest[: -len(_DEST_SUFFIX)]}_{sub_name}{_DEST_SUFFIX}"


def _add_subcommands(parent: Any, subs: list[Subcommand], dest: str) -> None:
    sub_parsers = parent.add_subparsers(dest=dest)
    for sub in subs:
        p = sub_parsers.add_parser(sub.name, help=sub.help)
        for arg in sub.arguments:
            arg.add_to(p)
        if sub.subcommands:
            _add_subcommands(p, sub.subcommands, _nested_dest(dest, sub.name))


def add_to_parser(subparsers: Any) -> None:
    """Build every registered family onto an existing subparsers object.

    `help` is SUPPRESS to match the hand-written families in cli.py — the
    top-level listing is rendered by the parser epilog, not argparse.
    """
    for cmd in COMMANDS:
        parser = subparsers.add_parser(cmd.name, help=argparse.SUPPRESS)
        _add_subcommands(parser, cmd.subcommands, f"{cmd.name}{_DEST_SUFFIX}")


def _usage_error(path: list[str], subs: list[Subcommand]) -> Exception:
    from popcorn_core.errors import PopcornError

    choices = "|".join(sorted(s.name for s in subs))
    return PopcornError(f"Usage: popcorn {' '.join(path)} [{choices}]", error_code="validation")


def dispatch(args: argparse.Namespace) -> bool:
    """Run the handler for `args`. Returns False if not a registered command.

    Walks the nesting until it reaches a leaf, raising a usage error at
    whatever level the user stopped short.
    """
    cmd = next((c for c in COMMANDS if c.name == getattr(args, "command", None)), None)
    if cmd is None:
        return False
    subs, dest, path = cmd.subcommands, f"{cmd.name}{_DEST_SUFFIX}", [cmd.name]
    while True:
        name = getattr(args, dest, None)
        sub = next((s for s in subs if s.name == name), None)
        if sub is None:
            raise _usage_error(path, subs)
        if sub.subcommands:
            subs, dest = sub.subcommands, _nested_dest(dest, sub.name)
            path.append(sub.name)
            continue
        assert sub.handler is not None, f"{' '.join([*path, sub.name])} has no handler"
        sub.handler(args)
        return True


def _sub_schema(sub: Subcommand) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": sub.name,
        "description": sub.help,
        "arguments": [
            {
                "name": a.name,
                "help": a.help,
                # argparse makes positionals required regardless of the flag.
                "required": a.required or a.positional,
                "positional": a.positional,
            }
            for a in sub.arguments
        ],
    }
    if sub.subcommands:
        entry["subcommands"] = [_sub_schema(s) for s in sub.subcommands]
    return entry


def schema() -> list[dict[str, Any]]:
    """The registry's own view of each family.

    `popcorn commands --json` takes `name`/`category`/`description` from here
    and the per-argument detail from argparse introspection (which knows
    types, choices and defaults). Kept as a whole-family view so a caller can
    diff the two and catch a family that never reached the parser.
    """
    return [
        {
            "name": c.name,
            "category": c.category,
            "description": c.description,
            "subcommands": [_sub_schema(s) for s in c.subcommands],
        }
        for c in COMMANDS
    ]


def completion_words(name: str) -> list[str]:
    """Subcommand names of one family, for a shell completion word list."""
    cmd = next((c for c in COMMANDS if c.name == name), None)
    return sorted(s.name for s in cmd.subcommands) if cmd else []


def completion_groups() -> list[tuple[str, list[str]]]:
    """Every completable word list: each family plus its nested groups.

    `[("flow", ["get", "list", "run", "runs"]), ("runs", ["get", "list"]), …]`
    — the shell completions key on the previous word alone, so nested groups
    are emitted flat under their own name.
    """
    groups: list[tuple[str, list[str]]] = []

    def walk(subs: list[Subcommand]) -> None:
        for sub in subs:
            if sub.subcommands:
                groups.append((sub.name, sorted(s.name for s in sub.subcommands)))
                walk(sub.subcommands)

    for cmd in COMMANDS:
        groups.append((cmd.name, sorted(s.name for s in cmd.subcommands)))
        walk(cmd.subcommands)
    return groups


def descriptions() -> dict[str, str]:
    return {c.name: c.description for c in COMMANDS}


def categories() -> dict[str, str]:
    return {c.name: c.category for c in COMMANDS}
