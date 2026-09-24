#!/usr/bin/env python3
"""Regenerate `popcorn_core.flow_rules` from `GET /customer-flows/schema`.

`template check` needs the DSL's rules — the step union, the reference
grammar, the `$trigger` key set, the importer's filename and size rules — and
it needs them with no server and no channel, because that offline contract is
the whole reason the command exists beside `flow validate`. Before this script
it got them by hand-copying the values out of backend source, which is a copy
that drifts in silence: the reference grammar had been wrong for long enough
that `template check` passed bundles the interpreter rejects.

So the values are still local, but no longer authored. This fetches the
endpoint that serves them and writes `src/popcorn_core/flow_rules.py`; the
checker imports only that module and stays import-pure. A rule change then
arrives as a reviewable diff in a PR instead of as a divergence nobody sees,
and every machine gets identical findings for a bundle — which is what
`template check --strict` in CI has to guarantee and what fetching at check
time would have taken away.

    make sync-rules     # fetch and rewrite the module
    make check-rules    # fetch and fail if the committed module has drifted

The render is a pure function of the payload with no timestamp in it, so an
unchanged endpoint produces a byte-identical file. That is what makes
`--check` meaningful: any diff at all is a real change, either on the server
or from someone editing the generated file by hand.

`--check` **fails** when it cannot reach the endpoint; it never skips. A drift
gate that goes quiet when its source is unreachable is indistinguishable from
one that is passing, which is exactly how `tests/test_backend_templates.py`
stayed green and mute for a release after the bundles it reads moved.

Two things the payload carries are deliberately not transcribed. `flow_schema`
is `Flow.model_json_schema()`, and the checker's value is its cross-file
bundle findings, which no JSON Schema covers — mirroring it here would put a
few hundred lines of unread blob into every diff. And an unknown top-level key
is a hard error rather than a silent skip: a newly served rule should force a
decision about whether the checker consumes it, not slip past unnoticed.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

TARGET = _REPO / "src" / "popcorn_core" / "flow_rules.py"

# The endpoint field this script reads but does not transcribe, and why.
IGNORED_FIELDS = {
    "flow_schema": "the Flow JSON Schema — the checker models the bundle, not the envelope",
}

EXIT_DRIFT = 1
EXIT_UNUSABLE = 2


@dataclass(frozen=True)
class _Field:
    """One generated constant: where it comes from and what it means.

    `source` is the path into the payload — one key, or `("bundle", <key>)`
    for the importer's file rules. `comment` is authored here rather than in
    the generated module, so the module can be overwritten wholesale.
    """

    name: str
    source: tuple[str, ...]
    comment: str
    # Rendered before `=`. Only a nested value needs one: mypy infers a mixed
    # dict literal as `dict[str, object]`, which no consumer can index.
    annotation: str = ""
    # Validates a structured value beyond the list-of-strings rule every flat
    # field gets. Raises ValueError naming what it cannot read.
    check: Callable[[Any], None] | None = None


_FIELDS: tuple[_Field, ...] = (
    _Field(
        "MAX_BLOCK_DEPTH",
        ("max_block_depth",),
        "Deepest legal nesting of `steps:` lists, counting a flow's top-level list as 1.\n"
        "A step carrying `steps:` at this depth is rejected by the DSL's own model\n"
        "validator, which is a failure at install rather than at authoring time.",
    ),
    _Field(
        "STEP_ACTIONS",
        ("step_actions",),
        "The mutually exclusive actions a step may carry. Exclusivity lives in a model\n"
        "validator, so `flow_schema` cannot express it — a client reading only the JSON\n"
        "Schema would conclude a step may set every one of them. Order is the order the\n"
        "server's own error message lists them in.",
    ),
    _Field(
        "REFERENCE_PATTERN",
        ("reference_pattern",),
        "The whole reference grammar: `$` then a root, then an optional dotted path.\n"
        "Group 1 is the root and group 2 the remainder. Numbered rather than named\n"
        "because the server serves this string to a browser too, where Python's\n"
        "`(?P<...>)` is a syntax error.",
    ),
    _Field(
        "REFERENCE_ROOTS",
        ("reference_roots",),
        "Roots that resolve anywhere in a flow. A `foreach`'s `as:` alias is a fifth\n"
        "kind, per-step and author-chosen, so it is not here.",
    ),
    _Field(
        "TRIGGER_KEYS",
        ("trigger_keys",),
        "Everything the interpreter seeds under `$trigger`. A closed set, so a typo in\n"
        "it is checkable — unlike `$channel`, whose keys come from a per-channel config\n"
        "the bundle cannot see. Every key is always present; one that does not apply to\n"
        "a run's trigger kind carries null.",
    ),
    _Field(
        "STEP_ERROR_PROPERTIES",
        ("step_error_properties",),
        "What `$steps.<id>.error` carries. Every step has the key, null unless the step\n"
        "failed and `on_error: {policy: skip}` absorbed the failure. Each property is a\n"
        "string, so nothing is reachable below one.",
    ),
    _Field(
        "ACTIVITY_ROLES",
        ("activity_roles",),
        "Wire name -> what the activity does to its table's rows and to its own output.\n"
        "`column_args` lists each arg whose literal value names the table's columns:\n"
        "`holds` is `column_keys` for a mapping keyed by column (or a list of such\n"
        "mappings) and `column_names` for a list of columns; `side` is `write` when the\n"
        "column lands in the table and `read` when it only selects. `output_schema_arg`\n"
        "names the arg carrying an inline JSON Schema for the step's output, or is None\n"
        "when the output shape is fixed. An activity absent here declares no role, which\n"
        "is not a claim that it touches no rows.",
        annotation="dict[str, dict[str, Any]]",
        check=lambda value: _check_roles(value),
    ),
    # ── the importer's file rules ────────────────────────────────────
    _Field(
        "MANIFEST_FILENAMES",
        ("bundle", "manifest_filenames"),
        "The manifest, and its legacy alias. Reserved: never installed as a flow.",
    ),
    _Field(
        "AGENT_DOC_FILENAME",
        ("bundle", "agent_doc_filename"),
        "Agent-facing documentation, installed as an agent-store scalar. Reserved.",
    ),
    _Field(
        "README_FILENAME",
        ("bundle", "readme_filename"),
        "Human-facing documentation, installed as an agent-store file. Reserved.",
    ),
    _Field(
        "STRINGS_FILENAME",
        ("bundle", "strings_filename"),
        "The app's user-facing UI copy, locale-sectioned. Reserved.",
    ),
    _Field(
        "FLOW_SUFFIXES",
        ("bundle", "flow_suffixes"),
        "Extensions that make a bundle entry a candidate flow document. Anything else\n"
        "is carried as bundle data, or ignored.",
    ),
    _Field(
        "FLOWS_AT_ROOT_ONLY",
        ("bundle", "flows_at_root_only"),
        "Whether flows are read from the bundle root only. True means a flow in a\n"
        "subdirectory is not read at all by the tree reader — the flow silently is not\n"
        "there — while the zip reader flattens it to its basename and installs it.",
    ),
    _Field(
        "SUBDIRS",
        ("bundle", "subdirs"),
        "Directories whose files seed a channel parameter of the same name, instead of\n"
        "being ignored. Each is a flat `stem -> text` map.",
    ),
    _Field(
        "SUBDIR_PATH_DEPTH",
        ("bundle", "subdir_path_depth"),
        "How many path segments a file under one of SUBDIRS must have to be read.\n"
        "Exact, not a maximum: the tree reader tests the segment count, so a file one\n"
        "level deeper is ignored rather than nested deeper.",
    ),
    _Field(
        "FILE_KEY_SUFFIXES",
        ("bundle", "file_key_suffixes"),
        "Suffixes stripped from a SUBDIRS filename to get the key a flow reads it by.\n"
        "Longest first, because `Path.stem` strips only one and would leave `foo.md`\n"
        "for `foo.md.j2`.",
    ),
    _Field(
        "CODE_SUBDIR",
        ("bundle", "code_subdir"),
        "Directory holding custom code blocks, one directory per block. A THIRD\n"
        "classification beside root files and SUBDIRS: the tree reader descends it to any\n"
        "depth, because a block may be a small package rather than one file.",
    ),
    _Field(
        "CODE_BLOCK_NAME_PATTERN",
        ("bundle", "code_block_name_pattern"),
        "The block directory's own name — a slug, since it rides inside flow YAML as\n"
        "`code_name:` and through error messages. Publish refuses a tree whose block\n"
        "segment does not match.",
    ),
    _Field(
        "CODE_MIN_PATH_DEPTH",
        ("bundle", "code_min_path_depth"),
        "How many segments a block file needs at minimum — `code/<block>/<file>`. A\n"
        "FLOOR, unlike SUBDIR_PATH_DEPTH's exact count, so a file nested deeper is still\n"
        "read rather than ignored.",
    ),
    _Field(
        "CODE_PATH_SEGMENT_PATTERN",
        ("bundle", "code_path_segment_pattern"),
        "Applied to every segment BELOW the block. A hidden entry there is local cruft\n"
        "rather than block source, and publish refuses the tree carrying it.",
    ),
    _Field(
        "AGENTS_SUBDIR",
        ("bundle", "agents_subdir"),
        "Directory holding the app's agents, one directory per agent, named by the same\n"
        "slug rule as a code block (CODE_BLOCK_NAME_PATTERN). A FOURTH classification:\n"
        "its files are agent definitions, never flows, and keep their whole path.",
    ),
    _Field(
        "AGENT_FILENAMES",
        ("bundle", "agent_filenames"),
        "The files an agent directory may hold directly: `agents/<name>/<one of these>`.",
    ),
    _Field(
        "AGENT_SCHEMAS_SUBDIR",
        ("bundle", "agent_schemas_subdir"),
        "The one directory inside an agent's directory, holding its JSON schemas:\n"
        "`agents/<name>/<this>/<file><AGENT_SCHEMA_SUFFIX>`.",
    ),
    _Field(
        "AGENT_SCHEMA_SUFFIX",
        ("bundle", "agent_schema_suffix"),
        "The extension a file under AGENT_SCHEMAS_SUBDIR must carry to be read.",
    ),
    _Field(
        "MAX_ENTRY_BYTES",
        ("bundle", "max_entry_bytes"),
        "The zip reader's per-entry cap. The one value here that is not about\n"
        "classification: it is a real ceiling on an uploaded bundle, so a checker\n"
        "should warn before an author hits it.",
    ),
)

_HEADER = '''"""The flow rules `GET /customer-flows/schema` serves.

GENERATED — do not edit. Every value below is transcribed from that endpoint
by `scripts/sync_flow_rules.py`; run `make sync-rules` to refresh it and
`make check-rules` to fail on drift. Editing a value here by hand makes the
checker disagree with the platform silently, which is the failure this module
exists to end.

`popcorn template check` reads its rules from here and nowhere else, which is
what keeps it offline: no server, no channel, no credentials, and the same
findings for a bundle on every machine. The trade is that this can go stale —
but staleness is now something a command can detect, where a hand-copied
constant was not.

The endpoint's `flow_schema` is deliberately absent. See the script.
"""

from __future__ import annotations

from typing import Any
'''


def _lit(value: Any, depth: int = 0) -> str:
    """A Python literal for one payload value, formatted as ruff would.

    Strings go through `json.dumps` for the double quotes and correct
    backslash escaping — the reference pattern is mostly backslashes, and
    `repr` would emit single quotes the formatter then rewrites, so
    `ruff format --check` would fail on a freshly generated file.

    A list becomes a tuple and a dict stays a dict, both always exploded with
    a trailing comma, nested at four spaces per `depth`. The trailing comma is
    load-bearing: ruff keeps a magic-trailing-comma collection expanded, so
    the rendered form is stable under the formatter regardless of how long
    the values are. An empty list renders `()`, which ruff leaves alone.
    Dict order is the payload's, which keeps the render a pure function of it.
    """
    pad, close = "    " * (depth + 1), "    " * depth
    if value is None:
        return "None"
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        if not value:
            return "()"
        items = "".join(f"{pad}{_lit(item, depth + 1)},\n" for item in value)
        return f"(\n{items}{close})"
    if isinstance(value, dict):
        if not value:
            return "{}"
        items = "".join(
            f"{pad}{json.dumps(key)}: {_lit(item, depth + 1)},\n" for key, item in value.items()
        )
        return f"{{\n{items}{close}}}"
    raise TypeError(f"no literal for {type(value).__name__}")


# The role vocabulary `template check` knows how to apply. Anything else is a
# refusal, not a pass-through: a new `holds` or `side` would otherwise reach the
# checker as a value it silently matches against nothing.
ROLE_KEYS = frozenset({"writes_rows", "reads_rows", "column_args", "output_schema_arg"})
COLUMN_ARG_KEYS = frozenset({"arg", "holds", "side"})
HOLDS = frozenset({"column_keys", "column_names"})
SIDES = frozenset({"write", "read"})


def _check_roles(roles: Any) -> None:
    """Refuse an `activity_roles` value the checker could not read exactly."""
    if not isinstance(roles, dict):
        raise ValueError("activity_roles is not a mapping")
    for name, role in roles.items():
        if not isinstance(role, dict) or set(role) != ROLE_KEYS:
            got = sorted(role) if isinstance(role, dict) else type(role).__name__
            raise ValueError(
                f"activity_roles.{name} carries {got}, not {sorted(ROLE_KEYS)}. Decide "
                "whether `template check` consumes the change, then teach this script it."
            )
        if not isinstance(role["writes_rows"], bool) or not isinstance(role["reads_rows"], bool):
            raise ValueError(f"activity_roles.{name}: writes_rows/reads_rows are not booleans")
        if role["output_schema_arg"] is not None and not isinstance(role["output_schema_arg"], str):
            raise ValueError(f"activity_roles.{name}.output_schema_arg is not a string or null")
        if not isinstance(role["column_args"], list):
            raise ValueError(f"activity_roles.{name}.column_args is not a list")
        for column_arg in role["column_args"]:
            if not isinstance(column_arg, dict) or set(column_arg) != COLUMN_ARG_KEYS:
                raise ValueError(
                    f"activity_roles.{name}.column_args holds {column_arg!r}, not an entry "
                    f"with exactly {sorted(COLUMN_ARG_KEYS)}"
                )
            if not isinstance(column_arg["arg"], str):
                raise ValueError(f"activity_roles.{name}.column_args has a non-string arg")
            if column_arg["holds"] not in HOLDS or column_arg["side"] not in SIDES:
                raise ValueError(
                    f"activity_roles.{name}.column_args.{column_arg['arg']} is "
                    f"holds={column_arg['holds']!r}, side={column_arg['side']!r}; "
                    f"`template check` reads holds in {sorted(HOLDS)} and side in "
                    f"{sorted(SIDES)} only."
                )


def _resolve(payload: dict[str, Any], source: tuple[str, ...]) -> Any:
    node: Any = payload
    for key in source:
        if not isinstance(node, dict) or key not in node:
            raise KeyError(".".join(source))
        node = node[key]
    return node


def _check_shape(payload: dict[str, Any]) -> None:
    """Refuse a payload this script does not fully account for.

    A missing field means the endpoint dropped a rule the checker enforces.
    An unknown one means it grew a rule nobody has decided about — the case
    worth failing on, because the alternative is a new rule the checker
    quietly does not read.
    """
    consumed_top = {f.source[0] for f in _FIELDS}
    unknown = sorted(set(payload) - consumed_top - set(IGNORED_FIELDS) - {"ok"})
    if unknown:
        raise ValueError(
            f"the endpoint serves {', '.join(unknown)}, which this script does not "
            "transcribe. Decide whether `template check` consumes it, then add a "
            "_Field for it or list it in IGNORED_FIELDS."
        )
    bundle = payload.get("bundle")
    if isinstance(bundle, dict):
        consumed_bundle = {f.source[1] for f in _FIELDS if f.source[0] == "bundle"}
        extra = sorted(set(bundle) - consumed_bundle)
        if extra:
            raise ValueError(
                f"the endpoint's `bundle` serves {', '.join(extra)}, which this script "
                "does not transcribe. Add a _Field for it or list it in IGNORED_FIELDS."
            )
    for field in _FIELDS:
        try:
            value = _resolve(payload, field.source)
        except KeyError as exc:
            raise ValueError(
                f"the endpoint no longer serves {exc.args[0]}, which `template check` "
                f"reads as {field.name}"
            ) from exc
        if field.check is not None:
            field.check(value)
        elif isinstance(value, list) and not all(isinstance(item, str) for item in value):
            raise ValueError(f"{'.'.join(field.source)} is not a list of strings")


def render(payload: dict[str, Any]) -> str:
    """The whole generated module, as text.

    Pure: same payload in, same bytes out, no timestamp and no ordering that
    depends on anything but `_FIELDS`. `--check` compares these bytes against
    the committed file, so any nondeterminism here would report drift that is
    not there.
    """
    _check_shape(payload)
    parts = [_HEADER]
    for field in _FIELDS:
        comment = "\n".join(f"# {line}" for line in field.comment.split("\n"))
        target = f"{field.name}: {field.annotation}" if field.annotation else field.name
        parts.append(f"{comment}\n{target} = {_lit(_resolve(payload, field.source))}\n")
    return "\n".join(parts)


def fetch(get_client: Callable[[], Any] | None = None) -> dict[str, Any]:
    """The live payload, through the CLI's own client and stored credentials.

    Goes through `operations.get_flow_schema` rather than a raw URL so the
    endpoint path stays declared in one place, beside the activity-catalog
    fetch it mirrors.
    """
    from popcorn_core import operations

    if get_client is None:  # pragma: no cover - exercised by `make sync-rules`
        from argparse import Namespace

        from popcorn_cli.cli import _get_client

        client = _get_client(Namespace())
    else:
        client = get_client()
    return operations.get_flow_schema(client)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="Compare instead of writing; exit 1 on drift.",
    )
    args = parser.parse_args(argv)

    try:
        rendered = render(fetch())
    except Exception as exc:
        # Deliberately not a skip. An unreachable endpoint or a changed payload
        # leaves the committed snapshot unverified, and reporting that as
        # success is the whole failure mode this gate exists to avoid.
        print(f"could not build the snapshot: {exc}", file=sys.stderr)
        print(
            "check `popcorn auth status` — this needs workspace-member credentials.",
            file=sys.stderr,
        )
        return EXIT_UNUSABLE

    current = TARGET.read_text() if TARGET.is_file() else ""
    if args.check:
        if rendered == current:
            print(f"{TARGET.relative_to(_REPO)} matches the endpoint.")
            return 0
        diff = difflib.unified_diff(
            current.splitlines(keepends=True),
            rendered.splitlines(keepends=True),
            fromfile="committed",
            tofile="endpoint",
        )
        sys.stdout.writelines(diff)
        print(
            f"\n{TARGET.relative_to(_REPO)} has drifted from the endpoint. "
            "Run `make sync-rules`, then read the diff: a changed rule may need a "
            "checker change, not only a refresh.",
            file=sys.stderr,
        )
        return EXIT_DRIFT

    if rendered == current:
        print(f"{TARGET.relative_to(_REPO)} already matches the endpoint.")
        return 0
    TARGET.write_text(rendered)
    print(f"rewrote {TARGET.relative_to(_REPO)} — review the diff before committing.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
