"""The flow rules `GET /customer-flows/schema` serves.

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

# Deepest legal nesting of `steps:` lists, counting a flow's top-level list as 1.
# A step carrying `steps:` at this depth is rejected by the DSL's own model
# validator, which is a failure at install rather than at authoring time.
MAX_BLOCK_DEPTH = 3

# The mutually exclusive actions a step may carry. Exclusivity lives in a model
# validator, so `flow_schema` cannot express it — a client reading only the JSON
# Schema would conclude a step may set every one of them. Order is the order the
# server's own error message lists them in.
STEP_ACTIONS = (
    "activity",
    "sleep_seconds",
    "await_approval",
    "call_flow",
    "steps",
)

# The whole reference grammar: `$` then a root, then an optional dotted path.
# Group 1 is the root and group 2 the remainder. Numbered rather than named
# because the server serves this string to a browser too, where Python's
# `(?P<...>)` is a syntax error.
REFERENCE_PATTERN = "^\\$([A-Za-z_][A-Za-z0-9_]*)(?:\\.([A-Za-z_][A-Za-z0-9_.]*))?$"

# Roots that resolve anywhere in a flow. A `foreach`'s `as:` alias is a fifth
# kind, per-step and author-chosen, so it is not here.
REFERENCE_ROOTS = (
    "channel",
    "inputs",
    "steps",
    "trigger",
)

# Everything the interpreter seeds under `$trigger`. A closed set, so a typo in
# it is checkable — unlike `$channel`, whose keys come from a per-channel config
# the bundle cannot see. Every key is always present; one that does not apply to
# a run's trigger kind carries null.
TRIGGER_KEYS = (
    "thread_id",
    "message_id",
    "user_id",
    "conversation_id",
    "thread_root",
    "contact_id",
    "workflow_id",
    "run_id",
)

# What `$steps.<id>.error` carries. Every step has the key, null unless the step
# failed and `on_error: {policy: skip}` absorbed the failure. Each property is a
# string, so nothing is reachable below one.
STEP_ERROR_PROPERTIES = (
    "message",
    "type",
)

# Wire name -> what the activity does to its table's rows and to its own output.
# `column_args` lists each arg whose literal value names the table's columns:
# `holds` is `column_keys` for a mapping keyed by column (or a list of such
# mappings) and `column_names` for a list of columns; `side` is `write` when the
# column lands in the table and `read` when it only selects. `output_schema_arg`
# names the arg carrying an inline JSON Schema for the step's output, or is None
# when the output shape is fixed. An activity absent here declares no role, which
# is not a claim that it touches no rows.
ACTIVITY_ROLES: dict[str, dict[str, Any]] = {
    "feature.email.extract": {
        "writes_rows": False,
        "reads_rows": False,
        "column_args": (),
        "output_schema_arg": "schema",
    },
    "foundation.agent.transform": {
        "writes_rows": False,
        "reads_rows": False,
        "column_args": (),
        "output_schema_arg": "output_schema",
    },
    "foundation.fields.extract": {
        "writes_rows": False,
        "reads_rows": False,
        "column_args": (),
        "output_schema_arg": "output_schema",
    },
    "foundation.store.claim_row": {
        "writes_rows": True,
        "reads_rows": True,
        "column_args": (),
        "output_schema_arg": None,
    },
    "foundation.store.delete_row": {
        "writes_rows": True,
        "reads_rows": False,
        "column_args": (),
        "output_schema_arg": None,
    },
    "foundation.store.get_record": {
        "writes_rows": False,
        "reads_rows": True,
        "column_args": (),
        "output_schema_arg": None,
    },
    "foundation.store.list_rows": {
        "writes_rows": False,
        "reads_rows": True,
        "column_args": (
            {
                "arg": "filter",
                "holds": "column_keys",
                "side": "read",
            },
            {
                "arg": "drop_columns",
                "holds": "column_names",
                "side": "read",
            },
        ),
        "output_schema_arg": None,
    },
    "foundation.store.patch_row": {
        "writes_rows": True,
        "reads_rows": False,
        "column_args": (
            {
                "arg": "patch",
                "holds": "column_keys",
                "side": "write",
            },
        ),
        "output_schema_arg": None,
    },
    "foundation.store.upsert_rows": {
        "writes_rows": True,
        "reads_rows": False,
        "column_args": (
            {
                "arg": "rows",
                "holds": "column_keys",
                "side": "write",
            },
            {
                "arg": "merge_on",
                "holds": "column_names",
                "side": "write",
            },
        ),
        "output_schema_arg": None,
    },
}

# The manifest, and its legacy alias. Reserved: never installed as a flow.
MANIFEST_FILENAMES = (
    "manifest.yaml",
    "config.yaml",
)

# Agent-facing documentation, installed as an agent-store scalar. Reserved.
AGENT_DOC_FILENAME = "AGENT.md"

# Human-facing documentation, installed as an agent-store file. Reserved.
README_FILENAME = "README.md"

# The app's user-facing UI copy, locale-sectioned. Reserved.
STRINGS_FILENAME = "strings.yaml"

# Extensions that make a bundle entry a candidate flow document. Anything else
# is carried as bundle data, or ignored.
FLOW_SUFFIXES = (
    ".yaml",
    ".yml",
)

# Whether flows are read from the bundle root only. True means a flow in a
# subdirectory is not read at all by the tree reader — the flow silently is not
# there — while the zip reader flattens it to its basename and installs it.
FLOWS_AT_ROOT_ONLY = True

# Directories whose files seed a channel parameter of the same name, instead of
# being ignored. Each is a flat `stem -> text` map.
SUBDIRS = (
    "prompts",
    "templates",
)

# How many path segments a file under one of SUBDIRS must have to be read.
# Exact, not a maximum: the tree reader tests the segment count, so a file one
# level deeper is ignored rather than nested deeper.
SUBDIR_PATH_DEPTH = 2

# Suffixes stripped from a SUBDIRS filename to get the key a flow reads it by.
# Longest first, because `Path.stem` strips only one and would leave `foo.md`
# for `foo.md.j2`.
FILE_KEY_SUFFIXES = (
    ".md.j2",
    ".md.jinja",
    ".j2",
    ".jinja",
    ".md",
    ".txt",
)

# Directory holding custom code blocks, one directory per block. A THIRD
# classification beside root files and SUBDIRS: the tree reader descends it to any
# depth, because a block may be a small package rather than one file.
CODE_SUBDIR = "code"

# The block directory's own name — a slug, since it rides inside flow YAML as
# `code_name:` and through error messages. Publish refuses a tree whose block
# segment does not match.
CODE_BLOCK_NAME_PATTERN = "^[a-z0-9][a-z0-9_-]{0,62}$"

# How many segments a block file needs at minimum — `code/<block>/<file>`. A
# FLOOR, unlike SUBDIR_PATH_DEPTH's exact count, so a file nested deeper is still
# read rather than ignored.
CODE_MIN_PATH_DEPTH = 3

# Applied to every segment BELOW the block. A hidden entry there is local cruft
# rather than block source, and publish refuses the tree carrying it.
CODE_PATH_SEGMENT_PATTERN = "^[^.][^/]*$"

# Directory holding the app's agents, one directory per agent, named by the same
# slug rule as a code block (CODE_BLOCK_NAME_PATTERN). A FOURTH classification:
# its files are agent definitions, never flows, and keep their whole path.
AGENTS_SUBDIR = "agents"

# The files an agent directory may hold directly: `agents/<name>/<one of these>`.
AGENT_FILENAMES = (
    "agent.yaml",
    "prompt.md",
)

# The one directory inside an agent's directory, holding its JSON schemas:
# `agents/<name>/<this>/<file><AGENT_SCHEMA_SUFFIX>`.
AGENT_SCHEMAS_SUBDIR = "schemas"

# The extension a file under AGENT_SCHEMAS_SUBDIR must carry to be read.
AGENT_SCHEMA_SUFFIX = ".json"

# The zip reader's per-entry cap. The one value here that is not about
# classification: it is a real ceiling on an uploaded bundle, so a checker
# should warn before an author hits it.
MAX_ENTRY_BYTES = 1048576
