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

# Deepest legal nesting of `steps:` lists, counting a flow's top-level list as 1.
# A step carrying `steps:` at this depth is rejected by the DSL's own model
# validator, which is a failure at install rather than at authoring time.
MAX_BLOCK_DEPTH = 3

# The mutually exclusive actions a step may carry. Exclusivity lives in a model
# validator, so `flow_schema` cannot express it — a client reading only the JSON
# Schema would conclude a step may set all four. Order is the order the server's
# own error message lists them in.
STEP_ACTIONS = (
    "activity",
    "sleep_seconds",
    "await_approval",
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

# The zip reader's per-entry cap. The one value here that is not about
# classification: it is a real ceiling on an uploaded bundle, so a checker
# should warn before an author hits it.
MAX_ENTRY_BYTES = 1048576
