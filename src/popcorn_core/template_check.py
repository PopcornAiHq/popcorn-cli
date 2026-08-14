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
were live failures while the `examples/alerttracker/` bundle was written; see
its `GOTCHAS.md`.

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
                self._prompt_stems.add(path.stem)
            elif rel.parts[0] == "templates" and len(rel.parts) > 1:
                self._template_stems.add(path.stem)
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
        declared_inputs = set(inputs) if isinstance(inputs, dict) else set()

        seen_ids: list[str] = []
        schema_props = self._output_schemas(flow)

        for index, step in enumerate(steps):
            step_id = step.get("id")
            where = f"{flow.path}:{step_id or f'steps.{index}'}"
            if not isinstance(step_id, str) or not step_id:
                self.err("step-without-id", where, "Every step needs an `id:`.")
            elif step_id in seen_ids:
                self.err(
                    "duplicate-step-id",
                    where,
                    f"Step id '{step_id}' is already used earlier in this flow; "
                    "`$steps.{step_id}` becomes ambiguous.",
                )
            if not step.get("activity"):
                self.err("step-without-activity", where, "Every step needs an `activity:`.")

            scope = set(seen_ids)
            item_names = {step["as"]} if isinstance(step.get("as"), str) else set()

            when = step.get("when")
            if when is not None:
                self._check_when(str(when), where, declared_inputs, scope, item_names, schema_props)

            for key in ("foreach", "args", "collect_into"):
                if key in step:
                    for path, value in _walk_strings(step[key], key):
                        self._check_value(
                            value,
                            f"{where}.{path}",
                            declared_inputs,
                            scope,
                            item_names,
                            schema_props,
                        )

            self._check_columns(flow, step, where)

            if isinstance(step_id, str) and step_id:
                seen_ids.append(step_id)

        outputs = flow.doc.get("outputs")
        if outputs is not None:
            for path, value in _walk_strings(outputs, "outputs"):
                self._check_value(
                    value,
                    f"{flow.path}:{path}",
                    declared_inputs,
                    set(seen_ids),
                    set(),
                    schema_props,
                )

    def _output_schemas(self, flow: Flow) -> dict[str, dict[str, Any]]:
        """`{step_id: {"required": {...}, "properties": {...}}}`.

        Only activities that declare `output_schema` at the call site produce a
        statically knowable shape; everything else stays unchecked.
        """
        out: dict[str, dict[str, Any]] = {}
        for step in flow.steps:
            if step.get("activity") not in _SCHEMA_ACTIVITIES:
                continue
            step_id = step.get("id")
            args = step.get("args")
            if not isinstance(step_id, str) or not isinstance(args, dict):
                continue
            schema = args.get("output_schema")
            if not isinstance(schema, dict):
                continue
            props = schema.get("properties")
            required = schema.get("required")
            out[step_id] = {
                "properties": set(props) if isinstance(props, dict) else set(),
                "required": set(required) if isinstance(required, list) else set(),
            }
        return out

    # ── references ────────────────────────────────────────────────────

    def _check_when(
        self,
        when: str,
        where: str,
        inputs: set[str],
        steps: set[str],
        items: set[str],
        schemas: dict[str, dict[str, Any]],
    ) -> None:
        """`when:` is exactly one `==`/`!=` comparison. No boolean algebra."""
        for token in ("&&", "||", " and ", " or "):
            if token in when:
                self.err(
                    "when-unsupported-operator",
                    where,
                    f"`when:` has no boolean operators; found '{token.strip()}'. Push a real "
                    "predicate into a query filter, which supports $lt/$gte/$in/$exists.",
                )
                return
        for token in (">=", "<=", ">", "<"):
            if token in when:
                self.err(
                    "when-unsupported-operator",
                    where,
                    f"`when:` has no ordering comparisons; found '{token}'. Use a query filter — "
                    "ISO-8601 compares correctly there because it is lexicographic.",
                )
                return
        count = when.count("==") + when.count("!=")
        if count == 0:
            self.err(
                "when-not-a-comparison",
                where,
                f"`when: {when}` is not a comparison. The whole grammar is `$ref == value` or "
                "`$ref != value`.",
            )
            return
        if count > 1:
            self.err(
                "when-multiple-comparisons",
                where,
                f"`when: {when}` has {count} comparisons; only one is supported.",
            )
            return
        op = "==" if "==" in when else "!="
        lhs, _, rhs = when.partition(op)
        for side in (lhs.strip(), rhs.strip()):
            if side.startswith("$"):
                self._check_value(side, f"{where}.when", inputs, steps, items, schemas)

    def _check_value(
        self,
        value: str,
        where: str,
        inputs: set[str],
        steps: set[str],
        items: set[str],
        schemas: dict[str, dict[str, Any]],
    ) -> None:
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

        if root == "inputs":
            if len(parts) < 2:
                self.err("malformed-reference", where, f"'{value}' names no input.")
            elif parts[1] not in inputs:
                self.err(
                    "undeclared-input",
                    where,
                    f"'{value}' reads input '{parts[1]}', which this flow does not declare. "
                    f"Declared: {', '.join(sorted(inputs)) or '(none)'}.",
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
        elif root == "steps":
            self._check_step_ref(value, parts, where, steps, schemas)
        elif root == "channel":
            self._check_channel_ref(value, parts, where)
        elif root in items:
            pass  # a foreach item; its shape is the row, unknowable here
        else:
            known = ["inputs", "steps", "channel", *sorted(items)]
            self.err(
                "unknown-reference-root",
                where,
                f"'{value}' starts from '${root}', which is not a reference root here. "
                f"Available: {', '.join('$' + k for k in known)}.",
            )

    def _check_step_ref(
        self,
        value: str,
        parts: list[str],
        where: str,
        steps: set[str],
        schemas: dict[str, dict[str, Any]],
    ) -> None:
        if len(parts) < 2:
            self.err("malformed-reference", where, f"'{value}' names no step.")
            return
        step_id = parts[1]
        if step_id not in steps:
            self.err(
                "unknown-step-reference",
                where,
                f"'{value}' refers to step '{step_id}', which is not defined earlier in this "
                f"flow. Steps run in order, so a later step cannot be read. "
                f"Available: {', '.join(sorted(steps)) or '(none)'}.",
            )
            return
        if len(parts) < 3 or parts[2] != "output":
            self.err(
                "step-ref-needs-output",
                where,
                f"'{value}' must read through `.output` — the grammar is $steps.{step_id}.output.*",
            )
            return
        schema = schemas.get(step_id)
        if schema is None or len(parts) < 4:
            return
        prop = parts[3]
        if prop not in schema["properties"]:
            self.err(
                "unknown-output-property",
                where,
                f"'{value}' reads '{prop}', which step '{step_id}' does not declare in its "
                f"output_schema.properties. Declared: "
                f"{', '.join(sorted(schema['properties'])) or '(none)'}.",
            )
        elif prop not in schema["required"]:
            self.err(
                "output-property-not-required",
                where,
                f"'{value}' reads '{prop}', which is declared but NOT in "
                f"output_schema.required. A declared-but-optional property is genuinely optional, "
                "and a missing key is a hard ReferenceError that fails the run — `on_error` "
                "cannot rescue it, because resolution precedes invocation. Add it to `required`.",
            )

    def _check_channel_ref(self, value: str, parts: list[str], where: str) -> None:
        if len(parts) < 2:
            self.err("malformed-reference", where, f"'{value}' names no channel key.")
            return
        manifest = self.report.manifest
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
