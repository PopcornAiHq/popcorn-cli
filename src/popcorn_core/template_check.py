"""Offline structural checks for a channel-template bundle.

`popcorn flow validate` is the authority on whether a single flow's references
resolve, but it needs a channel and a live server, and it only ever sees one
file at a time. The defects this module exists for are the ones that span files,
or that every layer below accepts in silence:

- a fixture named `.yaml`, which the importer installs as a flow
- two files whose basenames collide once the zip is flattened
- a write to a column the manifest does not declare (the store accepts it and
  produces a column no reference can reach)
- an `output_schema` property that a later step dereferences but that is not
  listed in `required`, so the model may legally omit it
- a schedule or webhook naming a flow the bundle does not contain

None of those are bad references, so none of them fail validation. All of them
were live failures while the alerttracker bundle was written; see
`examples/alerttracker/GOTCHAS.md`.

To answer any of that it has to model the DSL's SHAPE — which is not the same as
modelling its catalog. It knows that a step is exactly one of `activity`,
`sleep_seconds`, `await_approval` or a nested `steps:` block; that a block's
inner ids are private and only its `outputs:` keys escape; that `collect:`
publishes a second readable name beside `output`; that a numeric path segment is
an array index; that `$trigger` has seven keys. It does not know what any
activity takes or returns, which is the server's to own.

The one thing it deliberately does NOT model is `when:`. That grammar has four
rails routed legacy-first, and mirroring the routing offline means
reimplementing the predicate parser — so `when:` gets its references checked and
its grammar left alone. A near-miss reimplementation is worse than no check: the
rule this module used to enforce ("exactly one `==`/`!=`, no boolean operators")
described a grammar the engine never had, and rejected 55 valid clauses across
the five templates the platform ships.

Every finding is a `Finding(level, code, where, message)`. The codes are a
stable contract: CI and agents branch on them, so rename one only with a
version bump.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ERROR = "error"
WARNING = "warning"

# Reserved by the importer's read_zip — never treated as flows.
RESERVED_NAMES = frozenset({"manifest.yaml", "AGENT.md", "README.md"})

# YAML that is bundle data rather than a flow. Mirrors `_NOT_A_FLOW` in
# popcorn_cli.commands.flow, which is what `flow validate <dir>` skips.
NOT_A_FLOW = frozenset({"manifest.yaml", "config.yaml", "strings.yaml"})

# Path segments the importer preserves instead of flattening to a basename.
PRESERVED_DIRS = frozenset({"prompts", "templates"})

# read_zip rejects any entry over 1 MiB.
MAX_ENTRY_BYTES = 1024 * 1024

# A reference is the whole string value, `$` then a dotted path. The server
# reported `malformed reference '$row.Last Seen'` for a value containing a
# space, which is how we know it takes the entire string rather than
# interpolating a prefix out of it.
_REF_RE = re.compile(r"^\$([A-Za-z_][A-Za-z0-9_.]*)$")

# A step is EXACTLY ONE of these (the DSL's Step model enforces it). The
# checker used to demand `activity:`, which made every block, every durable
# sleep and every approval gate a false error.
_STEP_ACTIONS = ("activity", "sleep_seconds", "await_approval", "steps")

# The triggering context the interpreter seeds under `$trigger`. A closed set,
# so a typo in it is checkable — unlike `$channel`, whose keys come from a
# per-channel config the bundle cannot see.
TRIGGER_KEYS = frozenset(
    {
        "thread_id",
        "message_id",
        # The human whose turn triggered the run — None for scheduled,
        # webhook and contact-scoped runs. Display-only identity; compose
        # flows sign drafts with it via feature.email.sender_identity,
        # which is what three shipped bundles were being flagged for
        # while this set was missing it.
        "user_id",
        "conversation_id",
        "thread_root",
        "contact_id",
        "workflow_id",
        "run_id",
    }
)

# `$channel` keys the interpreter seeds itself, so the manifest never declares
# them: the connected-integrations map and the same integrations as a list to
# fan out over.
_CHANNEL_RUNTIME_KEYS = frozenset({"integrations", "integration_list"})

# Suffixes stripped from a prompts/ or templates/ filename to get the key a
# flow reads it by. Mirrors the backend's `_PROMPT_SUFFIXES`; `Path.stem`
# alone is wrong because it strips only ONE suffix, leaving `foo.md` for
# `foo.md.j2`.
_FILE_KEY_SUFFIXES = (".md.j2", ".md.jinja", ".j2", ".jinja", ".md", ".txt")

# Quoted literals inside a `when:` expression, stripped before scanning for
# refs so `$a == '$literal'` does not report a reference to `$literal`.
_QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"")

# Any `$ref` embedded in a larger string. Used ONLY for `when:`, which is the
# one place a reference is not the whole value.
_EMBEDDED_REF_RE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_.]*")


# Activities whose args name a table and carry column names as keys.
_WRITE_ACTIVITIES = {
    "foundation.store.upsert_rows": "rows",
    "foundation.store.insert_rows": "rows",
    "foundation.store.patch_row": "patch",
}
_READ_ACTIVITIES = frozenset(
    {
        "foundation.store.list_rows",
        "foundation.store.get_row",
        "foundation.store.delete_row",
    }
)

# Activities that declare their result shape at the call site, making
# `$steps.<id>.output.<prop>` statically checkable.
_SCHEMA_ACTIVITIES = frozenset(
    {
        "foundation.agent.transform",
        "foundation.fields.extract",
    }
)


@dataclass(frozen=True)
class Finding:
    """One structural defect. `where` is bundle-relative, or 'bundle'."""

    level: str
    code: str
    where: str
    message: str

    def __str__(self) -> str:
        return f"{self.level} {self.code} [{self.where}]: {self.message}"


@dataclass
class Flow:
    """A parsed flow file, keyed by the `name:` inside it, not its filename."""

    path: str
    name: str
    doc: dict[str, Any]

    @property
    def steps(self) -> list[dict[str, Any]]:
        raw = self.doc.get("steps")
        return [s for s in raw if isinstance(s, dict)] if isinstance(raw, list) else []


@dataclass(frozen=True)
class _StepInfo:
    """What a completed step publishes, from the reader's point of view.

    Three mutually exclusive shapes, because the DSL has three: a plain step
    publishes `output`; a foreach with `collect: <name>` also publishes that
    name; a block publishes ONLY the keys in its `outputs:` map.
    """

    collect: str | None = None
    schema: dict[str, set[str]] | None = None
    published: set[str] | None = None


@dataclass
class _Scope:
    """What references may name at one point in a flow.

    A block's inner steps see the enclosing scope, but its inner ids are
    private to it — so scopes nest rather than accumulate, and `child()` is
    what keeps an inner id from leaking out.
    """

    inputs: set[str] = field(default_factory=set)
    integrations: set[str] = field(default_factory=set)
    steps: dict[str, _StepInfo] = field(default_factory=dict)
    items: set[str] = field(default_factory=set)

    def child(self) -> _Scope:
        return _Scope(
            inputs=self.inputs,
            integrations=self.integrations,
            steps=dict(self.steps),
            items=set(self.items),
        )


@dataclass
class BundleReport:
    directory: str
    flows: list[Flow] = field(default_factory=list)
    fixtures: list[str] = field(default_factory=list)
    manifest: dict[str, Any] | None = None
    findings: list[Finding] = field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level == ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.level == WARNING]

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "directory": self.directory,
            "ok": self.ok,
            "flows": [{"name": f.name, "file": f.path} for f in self.flows],
            "fixtures": self.fixtures,
            "has_manifest": self.manifest is not None,
            "findings": [
                {"level": f.level, "code": f.code, "where": f.where, "message": f.message}
                for f in self.findings
            ],
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
        }


class _Checker:
    def __init__(self, directory: Path) -> None:
        self.dir = directory
        self.report = BundleReport(directory=str(directory))
        self._prompt_stems: set[str] = set()
        self._template_stems: set[str] = set()

    # ── finding helpers ───────────────────────────────────────────────

    def err(self, code: str, where: str, message: str) -> None:
        self.report.findings.append(Finding(ERROR, code, where, message))

    def warn(self, code: str, where: str, message: str) -> None:
        self.report.findings.append(Finding(WARNING, code, where, message))

    # ── entry point ───────────────────────────────────────────────────

    def run(self) -> BundleReport:
        files = self._collect_files()
        self._check_collisions(files)
        self._check_nesting(files)
        self._load_manifest()
        self._load_flows(files)
        self._check_manifest_references()
        for flow in self.report.flows:
            self._check_flow(flow)
        self._check_scalar_collisions()
        return self.report

    # ── file layout ───────────────────────────────────────────────────

    def _collect_files(self) -> list[Path]:
        """Every entry the importer would see, in bundle order."""
        out: list[Path] = []
        for path in sorted(self.dir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(self.dir)
            # read_zip skips dotfiles and __MACOSX before anything else.
            if any(part.startswith(".") or part == "__MACOSX" for part in rel.parts):
                continue
            size = path.stat().st_size
            if size > MAX_ENTRY_BYTES:
                self.err(
                    "entry-too-large",
                    str(rel),
                    f"{size} bytes exceeds the importer's 1 MiB limit; the entry is rejected.",
                )
                continue
            if rel.parts[0] == "prompts" and len(rel.parts) > 1:
                self._prompt_stems.add(_file_key(path.name))
            elif rel.parts[0] == "templates" and len(rel.parts) > 1:
                self._template_stems.add(_file_key(path.name))
            elif path.suffix == ".json":
                self.report.fixtures.append(str(rel))
            out.append(path)
        return out

    def _importer_key(self, path: Path) -> str:
        """What the entry is called after read_zip flattens the zip.

        Everything collapses to its basename except entries under `prompts/`
        or `templates/`, whose one path segment is preserved.
        """
        rel = path.relative_to(self.dir)
        if rel.parts[0] in PRESERVED_DIRS and len(rel.parts) > 1:
            return f"{rel.parts[0]}/{path.name}"
        return path.name

    def _check_nesting(self, files: list[Path]) -> None:
        """A flow in a subdirectory means two different things, silently.

        The two readers disagree, and neither says so. The registry reads a
        template off disk and descends `prompts/` and `templates/` ONLY, so
        `flows/a.yaml` is not read at all — the flow just is not there. The zip
        reader flattens it to `a.yaml` and installs it. So the same bundle
        either has the flow or does not, depending on how it got in.

        Warning rather than error: a flat bundle is the only layout with one
        meaning, but nesting is legal and works under the zip reader.
        """
        for path in files:
            rel = path.relative_to(self.dir)
            if len(rel.parts) == 1 or path.suffix not in (".yaml", ".yml"):
                continue
            if rel.parts[0] in PRESERVED_DIRS:
                continue
            self.warn(
                "nested-flow-file",
                str(rel),
                f"'{rel}' is in a subdirectory. The registry reader descends only "
                f"{'/, '.join(sorted(PRESERVED_DIRS))}/ and would not see this flow at all; "
                f"the zip reader would flatten it to '{path.name}'. Move it to the bundle root.",
            )

    def _check_collisions(self, files: list[Path]) -> None:
        """Two entries flattening to one name: the later silently wins."""
        seen: dict[str, list[str]] = {}
        for path in files:
            seen.setdefault(self._importer_key(path), []).append(str(path.relative_to(self.dir)))
        for key, paths in sorted(seen.items()):
            if len(paths) > 1:
                self.err(
                    "basename-collision",
                    ", ".join(paths),
                    f"{len(paths)} entries flatten to '{key}'. The importer keys by basename, "
                    "so only the last one survives — the others are silently dropped.",
                )

    # ── manifest ──────────────────────────────────────────────────────

    def _load_manifest(self) -> None:
        path = self.dir / "manifest.yaml"
        if not path.is_file():
            return
        doc = self._parse_yaml(path)
        if doc is None:
            return
        if not isinstance(doc, dict):
            self.err("manifest-not-a-mapping", "manifest.yaml", "Manifest must be a YAML mapping.")
            return
        self.report.manifest = doc
        if not doc.get("app_type"):
            self.warn(
                "clears-app-type",
                "manifest.yaml",
                "No app_type declared. Installing this bundle CLEARS the channel's app_type, "
                "which changes the client's whole interface paradigm. Intentional for an "
                "untyped ops bundle; never import it into a channel running a real app.",
            )
        self._check_tables(doc)

    def _columns(self, table: Any) -> list[dict[str, Any]]:
        if not isinstance(table, dict):
            return []
        raw = table.get("columns")
        return [c for c in raw if isinstance(c, dict)] if isinstance(raw, list) else []

    def _table_columns(self) -> dict[str, dict[str, dict[str, Any]]]:
        """`{table_name: {column_name: column_def}}` from the manifest."""
        tables = (self.report.manifest or {}).get("tables")
        if not isinstance(tables, dict):
            return {}
        out: dict[str, dict[str, dict[str, Any]]] = {}
        for name, table in tables.items():
            cols = {}
            for col in self._columns(table):
                col_name = col.get("name")
                if isinstance(col_name, str):
                    cols[col_name] = col
            out[str(name)] = cols
        return out

    def _check_tables(self, manifest: dict[str, Any]) -> None:
        tables = manifest.get("tables")
        if not isinstance(tables, dict):
            return
        for table_name, table in tables.items():
            where = f"manifest.yaml:tables.{table_name}"
            cols = {}
            for col in self._columns(table):
                name = col.get("name")
                if not isinstance(name, str):
                    self.err("column-without-name", where, "A column declaration has no `name`.")
                    continue
                cols[name] = col
                if col.get("merge") == "concat" and col.get("type") != "string":
                    self.err(
                        "concat-requires-string",
                        where,
                        f"Column '{name}' is merge:concat but type:{col.get('type')}. "
                        "Concat appends to a string; it cannot accumulate onto another type.",
                    )
            self._check_merge_key(table, cols, where)

    def _check_merge_key(self, table: Any, cols: dict[str, dict[str, Any]], where: str) -> None:
        """merge_key columns must be indexed and string-typed.

        The OR-probe behind `any_of` only queries the text index, so a
        non-string or unindexed merge key never matches and every upsert
        inserts a new row instead of merging.
        """
        merge_key = table.get("merge_key") if isinstance(table, dict) else None
        if not isinstance(merge_key, dict):
            return
        any_of = merge_key.get("any_of")
        if not isinstance(any_of, list):
            return
        for name in any_of:
            col = cols.get(str(name))
            if col is None:
                self.err(
                    "merge-key-unknown-column",
                    where,
                    f"merge_key.any_of names '{name}', which is not a declared column.",
                )
                continue
            if col.get("type") != "string":
                self.err(
                    "merge-key-not-string",
                    where,
                    f"merge_key column '{name}' is type:{col.get('type')}. The OR-probe only "
                    "queries the text index, so a non-string key silently never matches.",
                )
            if not (col.get("unique") or col.get("indexed")):
                self.err(
                    "merge-key-not-indexed",
                    where,
                    f"merge_key column '{name}' is not indexed. Add `unique: true` — an "
                    "unindexed merge key silently never matches.",
                )

    def _check_manifest_references(self) -> None:
        """Schedules and webhooks address flows by `name:`, not by filename."""
        manifest = self.report.manifest
        if manifest is None:
            return
        names = {f.name for f in self.report.flows}

        schedules = manifest.get("schedules")
        if isinstance(schedules, list):
            for i, sched in enumerate(schedules):
                if not isinstance(sched, dict):
                    continue
                where = f"manifest.yaml:schedules.{i}"
                flow = sched.get("flow")
                if isinstance(flow, str) and flow not in names:
                    self.err(
                        "schedule-unknown-flow",
                        where,
                        f"Schedule targets flow '{flow}', which no flow in this bundle declares "
                        f"as its `name:`. Known: {', '.join(sorted(names)) or '(none)'}.",
                    )
                if not sched.get("interval") and not sched.get("cron"):
                    self.err(
                        "schedule-no-trigger",
                        where,
                        "Schedule has neither `interval:` nor `cron:`, so it can never fire.",
                    )
                # At MOST one, as well as at least one. A schedule spec
                # carrying both cadences has two, and the backend refuses
                # rather than guess which the author meant to keep — so
                # catching it here is the difference between a checker
                # finding and an install against a real channel failing.
                if sched.get("interval") and sched.get("cron"):
                    self.err(
                        "schedule-two-triggers",
                        where,
                        "Schedule declares both `interval:` and `cron:`. Exactly one "
                        "cadence is allowed — the installer cannot pick between them "
                        "and will refuse the schedule.",
                    )

        webhooks = manifest.get("webhooks")
        if isinstance(webhooks, list):
            for i, hook in enumerate(webhooks):
                if not isinstance(hook, dict):
                    continue
                flow = hook.get("flow")
                if isinstance(flow, str) and flow not in names:
                    self.err(
                        "webhook-unknown-flow",
                        f"manifest.yaml:webhooks.{i}",
                        f"Webhook targets flow '{flow}', which no flow in this bundle declares "
                        f"as its `name:`.",
                    )

    def _check_scalar_collisions(self) -> None:
        """A scalar a flow writes must not also be declared in the manifest.

        `scalars:` upserts on every install, so a declared key that a flow also
        writes is reset to its install-time value on each re-import.
        """
        manifest = self.report.manifest
        if manifest is None:
            return
        declared = manifest.get("scalars")
        if not isinstance(declared, dict):
            return
        for flow in self.report.flows:
            for step in flow.steps:
                if step.get("activity") != "foundation.store.set_scalar":
                    continue
                args = step.get("args")
                key = args.get("key") if isinstance(args, dict) else None
                if isinstance(key, str) and key in declared:
                    self.warn(
                        "runtime-state-in-scalars",
                        f"{flow.path}:{step.get('id')}",
                        f"This step writes scalar '{key}', which manifest.yaml also declares "
                        "under `scalars:`. Scalars UPSERT on every install, so re-importing "
                        "resets the value this flow maintains. Declare only install-time "
                        "configuration; let flows create their own runtime keys.",
                    )

    # ── flows ─────────────────────────────────────────────────────────

    def _parse_yaml(self, path: Path) -> Any:
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise RuntimeError("template checks require PyYAML") from exc
        rel = str(path.relative_to(self.dir))
        try:
            return yaml.safe_load(path.read_text())
        except yaml.YAMLError as exc:
            self.err("yaml-parse-error", rel, f"Not parseable as YAML: {exc}")
            return None

    def _load_flows(self, files: list[Path]) -> None:
        by_name: dict[str, list[str]] = {}
        for path in files:
            if path.suffix not in (".yaml", ".yml"):
                continue
            rel = path.relative_to(self.dir)
            if path.name in RESERVED_NAMES or path.name in NOT_A_FLOW:
                continue
            if rel.parts[0] in PRESERVED_DIRS and len(rel.parts) > 1:
                continue
            doc = self._parse_yaml(path)
            if doc is None:
                continue
            if not isinstance(doc, dict) or "name" not in doc or "steps" not in doc:
                in_fixtures = "fixtures" in rel.parts
                self.err(
                    "fixture-installed-as-flow" if in_fixtures else "yaml-is-not-a-flow",
                    str(rel),
                    "Every .yaml/.yml in the bundle that is not manifest/config/strings is "
                    "installed as a flow, and this file has no `name:`/`steps:`. "
                    + (
                        "Rename it to .json — fixtures are data, not flows."
                        if in_fixtures
                        else "Give it a `name:` and `steps:`, or change its extension."
                    ),
                )
                continue
            name = doc.get("name")
            if not isinstance(name, str) or not name:
                self.err("flow-without-name", str(rel), "Flow `name:` must be a non-empty string.")
                continue
            by_name.setdefault(name, []).append(str(rel))
            self.report.flows.append(Flow(path=str(rel), name=name, doc=doc))

        for name, paths in sorted(by_name.items()):
            if len(paths) > 1:
                self.err(
                    "duplicate-flow-name",
                    ", ".join(paths),
                    f"{len(paths)} files declare `name: {name}`. Flow identity is the name, not "
                    "the filename, so these upsert over each other and only one survives.",
                )

    def _check_flow(self, flow: Flow) -> None:
        steps = flow.steps
        if not steps:
            self.err("flow-without-steps", flow.path, "Flow declares no steps.")
            return

        inputs = flow.doc.get("inputs")
        integrations = flow.doc.get("required_integrations")
        scope = _Scope(
            inputs=set(inputs) if isinstance(inputs, dict) else set(),
            integrations=set(integrations) if isinstance(integrations, dict) else set(),
        )
        after = self._check_step_list(flow, steps, scope, prefix="")

        # The flow's own `outputs:` resolve AFTER every step, so they read the
        # final scope — not the empty one the first step saw.
        outputs = flow.doc.get("outputs")
        if outputs is not None:
            for path, value in _walk_strings(outputs, "outputs"):
                self._check_value(value, f"{flow.path}:{path}", after)

    def _check_step_list(
        self, flow: Flow, steps: list[dict[str, Any]], outer: _Scope, prefix: str
    ) -> _Scope:
        """Check one step list, threading lexical scope through it.

        Steps run in order, so each step sees only the ones before it. A block
        (`steps:`) is checked with the enclosing scope PLUS its earlier
        siblings — inner steps read the full outer scope — and its own inner
        ids are dropped afterwards, because they are private to the block. What
        the block contributes to the outer scope is its `outputs:` keys, and
        nothing else.
        """
        scope = outer.child()
        for index, step in enumerate(steps):
            step_id = step.get("id")
            label = step_id or f"steps.{index}"
            where = f"{flow.path}:{prefix}{label}"

            if not isinstance(step_id, str) or not step_id:
                self.err("step-without-id", where, "Every step needs an `id:`.")
            elif step_id in scope.steps:
                self.err(
                    "duplicate-step-id",
                    where,
                    f"Step id '{step_id}' is already used earlier in this scope; "
                    f"`$steps.{step_id}` becomes ambiguous.",
                )

            actions = [key for key in _STEP_ACTIONS if step.get(key) is not None]
            if len(actions) != 1:
                self.err(
                    "step-without-action",
                    where,
                    "A step is exactly one of "
                    + ", ".join(f"`{key}:`" for key in _STEP_ACTIONS)
                    + (f"; found {', '.join(actions)}." if actions else "; found none."),
                )

            # `foreach:` names the list, so it resolves in the scope BEFORE
            # this step — the alias cannot be in scope yet.
            if "foreach" in step:
                for path, value in _walk_strings(step["foreach"], "foreach"):
                    self._check_value(value, f"{where}.{path}", scope)

            inner = scope.child()
            if isinstance(step.get("as"), str):
                inner.items.add(step["as"])

            # On a foreach step `when:` is the PER-ITEM gate, evaluated once
            # per iteration with the alias bound — so `when: $p.apply` is not
            # only legal, it is the idiom for skipping individual items. On any
            # other step there is no alias and the two scopes are the same.
            when = step.get("when")
            if when is not None:
                gate = inner if "foreach" in step else scope
                for ref in _when_refs(str(when)):
                    self._check_value(ref, f"{where}.when", gate)

            if "args" in step:
                for path, value in _walk_strings(step["args"], "args"):
                    self._check_value(value, f"{where}.{path}", inner)

            block = step.get("steps")
            published: set[str] | None = None
            if isinstance(block, list):
                nested = [s for s in block if isinstance(s, dict)]
                self._check_step_list(flow, nested, inner, prefix=f"{label}.")
                published = self._check_block_outputs(flow, step, inner, nested, where)

            self._check_columns(flow, step, where)

            if isinstance(step_id, str) and step_id:
                scope.steps[step_id] = _StepInfo(
                    collect=step.get("collect") if isinstance(step.get("collect"), str) else None,
                    schema=self._step_schema(step),
                    published=published,
                )

        return scope

    def _check_block_outputs(
        self,
        flow: Flow,
        step: dict[str, Any],
        inner: _Scope,
        nested: list[dict[str, Any]],
        where: str,
    ) -> set[str]:
        """A block's `outputs:` map, resolved in the block's INNER scope.

        This is the seam that makes a block checkable at all. Everything inside
        is private; `outputs:` is the only thing the enclosing flow can read,
        so its keys are exactly what `$steps.<block>.output.*` may name — and
        its values are refs the inner steps must actually have produced.
        """
        outputs = step.get("outputs")
        if not isinstance(outputs, dict):
            return set()
        after = inner.child()
        for nested_step in nested:
            nested_id = nested_step.get("id")
            if isinstance(nested_id, str) and nested_id:
                collect = nested_step.get("collect")
                after.steps[nested_id] = _StepInfo(
                    collect=collect if isinstance(collect, str) else None,
                    schema=self._step_schema(nested_step),
                    published=None,
                )
        for path, value in _walk_strings(outputs, "outputs"):
            self._check_value(value, f"{where}.{path}", after)
        return set(outputs)

    def _step_schema(self, step: dict[str, Any]) -> dict[str, set[str]] | None:
        """`{"properties": {...}, "required": {...}}` for a call-site schema.

        Only activities that declare `output_schema` in their args produce a
        statically knowable shape; everything else stays unchecked.
        """
        if step.get("activity") not in _SCHEMA_ACTIVITIES:
            return None
        args = step.get("args")
        if not isinstance(args, dict):
            return None
        schema = args.get("output_schema")
        if not isinstance(schema, dict):
            return None
        props = schema.get("properties")
        required = schema.get("required")
        return {
            "properties": set(props) if isinstance(props, dict) else set(),
            "required": set(required) if isinstance(required, list) else set(),
        }

    # ── references ────────────────────────────────────────────────────

    def _check_value(self, value: str, where: str, scope: _Scope) -> None:
        if not value.startswith("$"):
            return
        if "[" in value or "]" in value:
            self.err(
                "bracket-index",
                where,
                f"'{value}' indexes with brackets. Arrays are indexed with dots — "
                f"'{value.replace('[', '.').replace(']', '')}' — and a bracket makes the whole "
                "reference a literal string.",
            )
            return
        match = _REF_RE.match(value)
        if match is None:
            if " " in value:
                self.err(
                    "space-in-reference",
                    where,
                    f"'{value}' contains a space. A reference path is [A-Za-z_][A-Za-z0-9_.]*, so "
                    "a column with a space in its name cannot be dereferenced — only written "
                    "(YAML key) or filtered on (JSON key). Rename the column to be space-free.",
                )
            else:
                self.err(
                    "malformed-reference",
                    where,
                    f"'{value}' is not a valid reference path.",
                )
            return

        parts = match.group(1).split(".")
        root = parts[0]

        # A foreach alias SHADOWS every global root — the interpreter resolves
        # aliases before `channel` and `trigger` precisely so `as: channel`
        # keeps working. Check it first for the same reason.
        if root in scope.items:
            return  # the item's shape is the iterated value; unknowable here
        if root == "inputs":
            self._check_input_ref(value, parts, where, scope)
        elif root == "steps":
            self._check_step_ref(value, parts, where, scope)
        elif root == "channel":
            self._check_channel_ref(value, parts, where, scope)
        elif root == "trigger":
            if len(parts) > 1 and parts[1] not in TRIGGER_KEYS:
                self.err(
                    "unknown-trigger-key",
                    where,
                    f"'{value}' reads '{parts[1]}', which the triggering context does not carry. "
                    f"Available: {', '.join(sorted(TRIGGER_KEYS))}.",
                )
        else:
            known = ["inputs", "steps", "channel", "trigger", *sorted(scope.items)]
            self.err(
                "unknown-reference-root",
                where,
                f"'{value}' starts from '${root}', which is not a reference root here. "
                f"Available: {', '.join('$' + k for k in known)}.",
            )

    def _check_input_ref(self, value: str, parts: list[str], where: str, scope: _Scope) -> None:
        if len(parts) < 2:
            self.err("malformed-reference", where, f"'{value}' names no input.")
        elif parts[1] not in scope.inputs:
            self.err(
                "undeclared-input",
                where,
                f"'{value}' reads input '{parts[1]}', which this flow does not declare. "
                f"Declared: {', '.join(sorted(scope.inputs)) or '(none)'}.",
            )
        elif len(parts) > 2:
            self.err(
                "object-input-subfield",
                where,
                f"'{value}' reaches into an input's sub-fields, which is statically "
                "unreachable — an input declaration cannot describe an object's properties. "
                "Get a typed shape first with foundation.fields.extract (when the fields are "
                "there) or foundation.agent.transform (when the answer must be derived).",
            )

    def _check_step_ref(self, value: str, parts: list[str], where: str, scope: _Scope) -> None:
        if len(parts) < 2:
            self.err("malformed-reference", where, f"'{value}' names no step.")
            return
        step_id = parts[1]
        info = scope.steps.get(step_id)
        if info is None:
            self.err(
                "unknown-step-reference",
                where,
                f"'{value}' refers to step '{step_id}', which is not defined earlier in this "
                f"scope. Steps run in order, and a block's inner ids are private to it. "
                f"Available: {', '.join(sorted(scope.steps)) or '(none)'}.",
            )
            return
        if len(parts) < 3:
            return  # `$steps.<id>` — the whole output, no path to check
        head = parts[2]
        if head == info.collect:
            return  # a foreach's collected list; per-item shape unknowable here
        if head != "output":
            available = "`output`" + (f" or `{info.collect}`" if info.collect else "")
            self.err(
                "step-ref-needs-output",
                where,
                f"'{value}' reads '{head}' off step '{step_id}', which publishes {available}. "
                "A name other than `output` only resolves when the step declares it with "
                "`collect:`.",
            )
            return
        rest = parts[3:]
        if not rest:
            return
        if info.published is not None:
            if rest[0] not in info.published:
                self.err(
                    "unknown-block-output",
                    where,
                    f"'{value}' reads '{rest[0]}' off block '{step_id}', which publishes "
                    f"{', '.join(sorted(info.published)) or '(nothing)'}. Everything else inside "
                    "a block is private to it.",
                )
            return
        if info.schema is None or rest[0].isdigit():
            # A numeric segment is an array index (the interpreter's own rule),
            # and `properties` describes an object — there is nothing to match
            # it against.
            return
        prop = rest[0]
        if prop not in info.schema["properties"]:
            self.err(
                "unknown-output-property",
                where,
                f"'{value}' reads '{prop}', which step '{step_id}' does not declare in its "
                f"output_schema.properties. Declared: "
                f"{', '.join(sorted(info.schema['properties'])) or '(none)'}.",
            )
        elif prop not in info.schema["required"]:
            self.err(
                "output-property-not-required",
                where,
                f"'{value}' reads '{prop}', which is declared but NOT in "
                f"output_schema.required. A declared-but-optional property is genuinely optional, "
                "and a missing key is a hard ReferenceError that fails the run — `on_error` "
                "cannot rescue it, because resolution precedes invocation. Add it to `required`.",
            )

    def _check_channel_ref(self, value: str, parts: list[str], where: str, scope: _Scope) -> None:
        if len(parts) < 2:
            self.err("malformed-reference", where, f"'{value}' names no channel key.")
            return
        key = parts[1]
        if key == "prompts":
            if len(parts) > 2 and parts[2] not in self._prompt_stems:
                self.err(
                    "unknown-prompt",
                    where,
                    f"'{value}' reads prompt '{parts[2]}', but no prompts/{parts[2]}.* exists "
                    "in the bundle.",
                )
            return
        if key == "templates":
            if len(parts) > 2 and parts[2] not in self._template_stems:
                self.err(
                    "unknown-template",
                    where,
                    f"'{value}' reads template '{parts[2]}', but no templates/{parts[2]}.* "
                    "exists in the bundle.",
                )
            return
        if key == "integrations":
            # Two declaration sites, and a bundle may legitimately use either.
            # A flow's own `required_integrations:` buys a pre-flight refusal to
            # start when the integration is unconnected. The manifest's
            # `connections[].config_name` buys the client binding the name on
            # the first connect click — which is why claimcoordinator declares
            # `leads_inbox` there and deliberately in no flow. Neither is wrong,
            # so neither is a finding; naming the integration NOWHERE is.
            if len(parts) > 2 and parts[2] not in (scope.integrations | self._manifest_names()):
                self.warn(
                    "undeclared-integration",
                    where,
                    f"'{value}' reads integration '{parts[2]}', which nothing in the bundle "
                    "declares — not this flow's `required_integrations:`, and not a manifest "
                    "`connections[].config_name`. Known: "
                    f"{', '.join(sorted(scope.integrations | self._manifest_names())) or '(none)'}"
                    ". Without a declaration the interpreter cannot refuse to start when the "
                    "integration is unconnected, so the flow fails mid-run instead.",
                )
            return
        if key in _CHANNEL_RUNTIME_KEYS:
            return
        manifest = self.report.manifest
        if manifest is None:
            return
        declared: set[str] = set()
        for section in ("channel_parameters", "scalars", "default_scalars"):
            block = manifest.get(section)
            if isinstance(block, dict):
                declared |= set(block)
        if key not in declared:
            self.warn(
                "undeclared-channel-key",
                where,
                f"'{value}' reads channel key '{key}', which the manifest does not declare under "
                "channel_parameters, scalars or default_scalars. Fine if a flow writes it at "
                "runtime; a typo otherwise.",
            )

    # ── column names ──────────────────────────────────────────────────

    def _manifest_names(self) -> set[str]:
        """Integration names the manifest's `connections:` block binds.

        `config_name` is the channel-config name a connection binds to, which
        is exactly what `$channel.integrations.<name>` reads. A connection
        without one names its own instances (repeatable slots like `gcal`,
        `gcal_2`), so the id doubles as the name.
        """
        manifest = self.report.manifest
        if manifest is None:
            return set()
        connections = manifest.get("connections")
        if not isinstance(connections, list):
            return set()
        names: set[str] = set()
        for entry in connections:
            if not isinstance(entry, dict):
                continue
            for key in ("config_name", "id"):
                value = entry.get(key)
                if isinstance(value, str) and value:
                    names.add(value)
        return names

    def _check_columns(self, flow: Flow, step: dict[str, Any], where: str) -> None:
        """Cross-check written/read column names against the manifest.

        The store accepts undeclared columns silently, producing a column no
        reference can reach, so a write to an unknown column is an error. Reads
        are warnings: a filter may legitimately target a column some other flow
        created at runtime.
        """
        activity = step.get("activity")
        args = step.get("args")
        if not isinstance(args, dict) or not isinstance(activity, str):
            return
        table_name = args.get("table_name")
        if not isinstance(table_name, str):
            return
        columns = self._table_columns().get(table_name)
        if columns is None:
            return

        def report(names: Any, level: str, code: str, kind: str) -> None:
            for name in names:
                if not isinstance(name, str) or name.startswith("_") or name in columns:
                    continue
                message = f"{kind} column '{name}' is not declared on table '{table_name}'. " + (
                    "The store accepts undeclared columns silently, producing a column no "
                    "$ref can reach."
                    if level == ERROR
                    else "A filter on a column that does not exist matches nothing, silently."
                )
                getattr(self, "err" if level == ERROR else "warn")(code, where, message)

        write_key = _WRITE_ACTIVITIES.get(activity)
        if write_key:
            payload = args.get(write_key)
            rows = payload if isinstance(payload, list) else [payload]
            for row in rows:
                if isinstance(row, dict):
                    report(row.keys(), ERROR, "undeclared-column", "Written")

        if activity in _READ_ACTIVITIES or write_key:
            filters = args.get("filter")
            if isinstance(filters, dict):
                report(filters.keys(), WARNING, "unknown-filter-column", "Filtered")
            dropped = args.get("drop_columns")
            if isinstance(dropped, list):
                report(dropped, WARNING, "unknown-drop-column", "Dropped")


def _file_key(filename: str) -> str:
    """The config key a prompts/ or templates/ file is read by.

    `Path.stem` strips one suffix, so `compose_chase.md.j2` becomes
    `compose_chase.md` and never matches `$channel.prompts.compose_chase`.
    Longest suffix first, so `.md.j2` wins over `.j2`.
    """
    for suffix in _FILE_KEY_SUFFIXES:
        if filename.endswith(suffix):
            return filename[: -len(suffix)]
    return filename.rsplit(".", 1)[0] if "." in filename else filename


def _when_refs(when: str) -> list[str]:
    """Every reference inside a `when:` string.

    No grammar check here, deliberately. `when:` has four rails — a compound
    boolean expression, a standalone `==`/`!=` comparison with lenient
    equality, a bare ref that is truth-tested, and a plain truthy literal —
    and which one a string takes is decided legacy-first by the server's own
    `routes_to_expression`. Mirroring that offline means reimplementing the
    predicate parser, and a near-miss reimplementation is a false-positive
    generator: the old rule here ("exactly one comparison, no boolean
    operators") rejected 55 `when:` clauses across the five shipped backend
    templates, every one of them valid.

    So the checker checks what it can check without a parser — that the paths
    named actually resolve — and leaves the grammar to `flow validate`, which
    calls the real one.
    """
    return _EMBEDDED_REF_RE.findall(_QUOTED_RE.sub("", when))


def _walk_strings(node: Any, path: str) -> list[tuple[str, str]]:
    """Every string leaf under `node`, with a dotted path to each."""
    out: list[tuple[str, str]] = []
    if isinstance(node, str):
        out.append((path, node))
    elif isinstance(node, dict):
        for key, value in node.items():
            out.extend(_walk_strings(value, f"{path}.{key}"))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            out.extend(_walk_strings(value, f"{path}.{i}"))
    return out


def check_bundle(directory: str | Path) -> BundleReport:
    """Run every offline structural check over a template bundle directory."""
    path = Path(directory)
    report = BundleReport(directory=str(path))
    if not path.exists():
        report.findings.append(Finding(ERROR, "bundle-not-found", str(path), "No such directory."))
        return report
    if not path.is_dir():
        report.findings.append(
            Finding(ERROR, "bundle-not-a-directory", str(path), "Not a directory.")
        )
        return report
    return _Checker(path).run()
