"""Unit tests for the offline bundle checker.

Each test starts from a bundle that checks clean and breaks exactly one thing,
so a passing assertion means the check fired *because* of the defect rather
than because something else in the fixture was already wrong.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from popcorn_core.template_check import TRIGGER_KEYS, check_bundle

# ── a bundle that checks clean ────────────────────────────────────────

CLEAN_MANIFEST: dict[str, Any] = {
    "display_name": "Widgets",
    "app_type": "custom",
    "channel_parameters": {"stale_hours": 6},
    "tables": {
        "widgets": {
            "columns": [
                {"name": "Fingerprint", "type": "string", "unique": True},
                {"name": "Title", "type": "string"},
                {"name": "Status", "type": "string"},
                {"name": "Last Seen", "type": "datetime"},
                {"name": "Seen At", "type": "string", "merge": "concat"},
                {"name": "PostMessageId", "type": "string", "internal": True},
            ],
            "merge_key": {"any_of": ["Fingerprint"], "on_conflict": "merge"},
        }
    },
    "schedules": [{"flow": "sweep", "slug": "sweep", "interval": 300}],
    "webhooks": [{"name": "Intake", "flow": "intake"}],
}

CLEAN_INTAKE: dict[str, Any] = {
    "name": "intake",
    "version": 1,
    "inputs": {"conversation_id": {"type": "string"}, "payload": {"type": "object"}},
    "steps": [
        {"id": "now", "activity": "foundation.workflow.now"},
        {
            "id": "fields",
            "activity": "foundation.fields.extract",
            "args": {
                "data": "$inputs.payload",
                "mapping": {"title": "Title", "key": "Key"},
                "output_schema": {
                    "type": "object",
                    "required": ["title", "key"],
                    "properties": {"title": {"type": "string"}, "key": {"type": "string"}},
                },
            },
        },
        {
            "id": "upsert",
            "activity": "foundation.store.upsert_rows",
            "args": {
                "conversation_id": "$inputs.conversation_id",
                "table_name": "widgets",
                "merge_on": ["Fingerprint"],
                "rows": [
                    {
                        "Fingerprint": "$steps.fields.output.key",
                        "Title": "$steps.fields.output.title",
                        "Status": "firing",
                        "Last Seen": "$steps.now.output.iso",
                    }
                ],
            },
        },
        {
            "id": "post",
            "activity": "foundation.channel.post",
            "when": "$steps.upsert.output.created == 1",
            "args": {
                "channel_id": "$inputs.conversation_id",
                "text": "$steps.fields.output.title",
            },
        },
    ],
    "outputs": {"key": "$steps.fields.output.key"},
}

CLEAN_SWEEP: dict[str, Any] = {
    "name": "sweep",
    "version": 1,
    "inputs": {"conversation_id": {"type": "string"}},
    "steps": [
        {"id": "now", "activity": "foundation.workflow.now"},
        {
            "id": "cutoff",
            "activity": "foundation.math.offset",
            "args": {
                "iso": "$steps.now.output.iso",
                "direction": "subtract",
                "hours": "$channel.stale_hours",
            },
        },
        {
            "id": "stale",
            "activity": "foundation.store.list_rows",
            "args": {
                "conversation_id": "$inputs.conversation_id",
                "table_name": "widgets",
                "filter": {
                    "Status": "firing",
                    "Last Seen": {"$lt": "$steps.cutoff.output.iso"},
                    "PostMessageId": {"$exists": True},
                },
            },
        },
        {
            "id": "close",
            "activity": "foundation.channel.edit",
            "foreach": "$steps.stale.output.rows",
            "as": "row",
            "args": {
                "channel_id": "$inputs.conversation_id",
                "message_id": "$row.PostMessageId",
                "text": "$row.Title",
            },
        },
    ],
}


def write_bundle(
    root: Path,
    *,
    manifest: dict[str, Any] | None = None,
    flows: dict[str, dict[str, Any]] | None = None,
    fixtures: dict[str, Any] | None = None,
) -> Path:
    """Materialize a bundle; each arg defaults to the clean version."""
    root.mkdir(parents=True, exist_ok=True)
    resolved_manifest = CLEAN_MANIFEST if manifest is None else manifest
    if resolved_manifest is not None:
        (root / "manifest.yaml").write_text(yaml.safe_dump(resolved_manifest))
    for name, doc in (flows or {"intake": CLEAN_INTAKE, "sweep": CLEAN_SWEEP}).items():
        (root / f"{name}.yaml").write_text(yaml.safe_dump(doc))
    for name, doc in (fixtures or {}).items():
        (root / "fixtures").mkdir(exist_ok=True)
        (root / "fixtures" / name).write_text(json.dumps(doc))
    return root


def bare_manifest(**extra: Any) -> dict[str, Any]:
    """The clean manifest minus anything that names a flow.

    Single-flow fixtures below would otherwise trip `schedule-unknown-flow` on
    the clean manifest's `sweep` schedule — a real check firing for a reason
    that has nothing to do with what the test is about.
    """
    return {
        "display_name": "Widgets",
        "app_type": "custom",
        "channel_parameters": {"stale_hours": 6},
        "tables": json.loads(json.dumps(CLEAN_MANIFEST["tables"])),
        **extra,
    }


def codes(root: Path) -> set[str]:
    return {f.code for f in check_bundle(root).findings}


def mutate(doc: dict[str, Any], step_id: str, **changes: Any) -> dict[str, Any]:
    """Deep-copy a flow and replace keys on one step."""
    clone = json.loads(json.dumps(doc))
    for step in clone["steps"]:
        if step.get("id") == step_id:
            step.update(changes)
    return clone


# ── the clean baseline ────────────────────────────────────────────────


def test_clean_bundle_has_no_findings(tmp_path):
    report = check_bundle(write_bundle(tmp_path / "b"))
    assert report.findings == [], [str(f) for f in report.findings]
    assert report.ok
    assert {f.name for f in report.flows} == {"intake", "sweep"}


def test_missing_directory_is_an_error(tmp_path):
    report = check_bundle(tmp_path / "nope")
    assert not report.ok
    assert [f.code for f in report.findings] == ["bundle-not-found"]


# ── the importer's contract ───────────────────────────────────────────


def test_yaml_fixture_would_be_installed_as_a_flow(tmp_path):
    root = write_bundle(tmp_path / "b")
    (root / "fixtures").mkdir(exist_ok=True)
    (root / "fixtures" / "sample.yaml").write_text(yaml.safe_dump({"AlarmName": "x"}))
    assert "fixture-installed-as-flow" in codes(root)


def test_json_fixture_is_fine(tmp_path):
    root = write_bundle(tmp_path / "b", fixtures={"sample.json": {"AlarmName": "x"}})
    report = check_bundle(root)
    assert report.findings == []
    assert report.fixtures == ["fixtures/sample.json"]


def test_basename_collision_is_an_error(tmp_path):
    root = write_bundle(tmp_path / "b")
    (root / "old").mkdir()
    (root / "old" / "intake.yaml").write_text(yaml.safe_dump(CLEAN_INTAKE))
    found = codes(root)
    assert "basename-collision" in found
    assert "duplicate-flow-name" in found


def test_prompts_dir_is_preserved_not_flattened(tmp_path):
    """`prompts/` keeps its segment, so it cannot collide with a root file."""
    root = write_bundle(tmp_path / "b")
    (root / "prompts").mkdir()
    (root / "prompts" / "intake.md").write_text("hello")
    assert "basename-collision" not in codes(root)


def test_duplicate_flow_name_across_files(tmp_path):
    root = write_bundle(
        tmp_path / "b",
        flows={"a": CLEAN_INTAKE, "b": {**CLEAN_INTAKE}, "sweep": CLEAN_SWEEP},
    )
    assert "duplicate-flow-name" in codes(root)


def test_oversized_entry_is_rejected(tmp_path):
    root = write_bundle(tmp_path / "b")
    (root / "fixtures").mkdir(exist_ok=True)
    (root / "fixtures" / "big.json").write_text("x" * (1024 * 1024 + 1))
    assert "entry-too-large" in codes(root)


# ── references ────────────────────────────────────────────────────────


def test_bracket_indexing_is_an_error(tmp_path):
    flow = mutate(
        CLEAN_INTAKE,
        "post",
        args={"channel_id": "$inputs.conversation_id", "text": "$steps.upsert.output.ids[0]"},
    )
    root = write_bundle(tmp_path / "b", flows={"intake": flow, "sweep": CLEAN_SWEEP})
    assert "bracket-index" in codes(root)


def test_space_in_reference_is_an_error(tmp_path):
    """A column with a space can be written and filtered, never dereferenced."""
    flow = mutate(
        CLEAN_SWEEP,
        "close",
        args={"channel_id": "$inputs.conversation_id", "text": "$row.Last Seen"},
    )
    root = write_bundle(tmp_path / "b", flows={"intake": CLEAN_INTAKE, "sweep": flow})
    assert "space-in-reference" in codes(root)


def test_forward_step_reference_is_an_error(tmp_path):
    """`now` runs after `early`, so `early` cannot read it."""
    flow = json.loads(json.dumps(CLEAN_INTAKE))
    flow["steps"].insert(
        0,
        {
            "id": "early",
            "activity": "foundation.channel.post",
            "args": {"channel_id": "$inputs.conversation_id", "text": "$steps.now.output.iso"},
        },
    )
    root = write_bundle(tmp_path / "b", flows={"intake": flow, "sweep": CLEAN_SWEEP})
    assert "unknown-step-reference" in codes(root)


def test_undeclared_input_is_an_error(tmp_path):
    flow = mutate(
        CLEAN_INTAKE,
        "post",
        args={"channel_id": "$inputs.conversation_id", "text": "$inputs.headers"},
    )
    root = write_bundle(tmp_path / "b", flows={"intake": flow, "sweep": CLEAN_SWEEP})
    assert "undeclared-input" in codes(root)


def test_object_input_subfield_is_unreachable(tmp_path):
    flow = mutate(
        CLEAN_INTAKE,
        "post",
        args={"channel_id": "$inputs.conversation_id", "text": "$inputs.payload.AlarmName"},
    )
    root = write_bundle(tmp_path / "b", flows={"intake": flow, "sweep": CLEAN_SWEEP})
    assert "object-input-subfield" in codes(root)


def test_foreach_item_scope_is_per_step(tmp_path):
    """`$row` is legal inside the step that declares `as: row`, nowhere else."""
    flow = mutate(
        CLEAN_SWEEP,
        "stale",
        args={
            "conversation_id": "$inputs.conversation_id",
            "table_name": "widgets",
            "filter": {"Status": "$row.Status"},
        },
    )
    root = write_bundle(tmp_path / "b", flows={"intake": CLEAN_INTAKE, "sweep": flow})
    assert "unknown-reference-root" in codes(root)


def test_unknown_reference_root(tmp_path):
    flow = mutate(
        CLEAN_INTAKE,
        "post",
        args={"channel_id": "$inputs.conversation_id", "text": "$workspace.id"},
    )
    root = write_bundle(tmp_path / "b", flows={"intake": flow, "sweep": CLEAN_SWEEP})
    assert "unknown-reference-root" in codes(root)


def test_step_ref_must_go_through_output(tmp_path):
    flow = mutate(
        CLEAN_INTAKE,
        "post",
        args={"channel_id": "$inputs.conversation_id", "text": "$steps.now.iso"},
    )
    root = write_bundle(tmp_path / "b", flows={"intake": flow, "sweep": CLEAN_SWEEP})
    assert "step-ref-needs-output" in codes(root)


def test_undeclared_channel_key_warns(tmp_path):
    flow = mutate(
        CLEAN_SWEEP,
        "cutoff",
        args={"iso": "$steps.now.output.iso", "direction": "subtract", "hours": "$channel.typo"},
    )
    root = write_bundle(tmp_path / "b", flows={"intake": CLEAN_INTAKE, "sweep": flow})
    report = check_bundle(root)
    assert [f.code for f in report.warnings] == ["undeclared-channel-key"]
    assert report.ok  # advisory: a flow may write the key at runtime


# ── output schemas ────────────────────────────────────────────────────


def test_dereferenced_property_must_be_required(tmp_path):
    """Declared-but-optional means the model may legally omit it."""
    flow = json.loads(json.dumps(CLEAN_INTAKE))
    for step in flow["steps"]:
        if step["id"] == "fields":
            step["args"]["output_schema"]["required"] = ["key"]
    root = write_bundle(tmp_path / "b", flows={"intake": flow, "sweep": CLEAN_SWEEP})
    assert "output-property-not-required" in codes(root)


def test_dereferenced_property_must_be_declared(tmp_path):
    flow = mutate(
        CLEAN_INTAKE,
        "post",
        args={"channel_id": "$inputs.conversation_id", "text": "$steps.fields.output.nope"},
    )
    root = write_bundle(tmp_path / "b", flows={"intake": flow, "sweep": CLEAN_SWEEP})
    assert "unknown-output-property" in codes(root)


def test_permissive_activity_output_is_not_checked(tmp_path):
    """upsert_rows declares no schema, so any path off it has to pass.

    This is a real limit, not an oversight: activities with permissive result
    schemas resolve to nothing at runtime for a wrong path, and nothing offline
    can tell the difference. Verify those against the live response.
    """
    flow = mutate(
        CLEAN_INTAKE,
        "post",
        args={
            "channel_id": "$inputs.conversation_id",
            "text": "$steps.upsert.output.no_such_field.nested",
        },
    )
    root = write_bundle(tmp_path / "b", flows={"intake": flow, "sweep": CLEAN_SWEEP})
    assert check_bundle(root).findings == []


# ── when: grammar ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "expr",
    [
        # The expression rail: compounds and ordering comparisons. The checker
        # used to reject all of these. 55 clauses across the five shipped
        # backend templates use them.
        "$steps.upsert.output.created == 1 && $steps.now.output.iso != ''",
        "$steps.upsert.output.created > 0",
        "$steps.upsert.output.created >= 1",
        "!$steps.upsert.output.created",
        "($steps.upsert.output.created == 1) || ($steps.now.output.iso != '')",
        # A bare ref is truth-tested — the second-most-common form in the
        # shipped templates, and previously reported as "not a comparison".
        "$steps.upsert.output.created",
        # Legacy standalone equality, which is its own rail (lenient) and
        # always worked.
        "$steps.upsert.output.created == 1",
        # A plain literal is truthy as-is.
        "yes",
    ],
)
def test_real_when_grammar_is_accepted(tmp_path, expr):
    """`when:` has four rails, not one.

    The old rule here — exactly one `==`/`!=`, no boolean operators, no
    ordering — described a grammar the engine has not had. Mirroring the real
    routing offline means reimplementing the predicate parser, so the checker
    checks the references and leaves the grammar to `flow validate`.
    """
    flow = mutate(CLEAN_INTAKE, "post", when=expr)
    root = write_bundle(tmp_path / "b", flows={"intake": flow, "sweep": CLEAN_SWEEP})
    assert check_bundle(root).findings == []


def test_when_still_validates_refs_inside_an_expression(tmp_path):
    """Dropping the grammar rule must not drop the reference check with it —
    a compound is exactly where a typo'd path is easiest to miss."""
    flow = mutate(
        CLEAN_INTAKE,
        "post",
        when="$steps.upsert.output.created == 1 && $steps.nosuch.output.x != ''",
    )
    root = write_bundle(tmp_path / "b", flows={"intake": flow, "sweep": CLEAN_SWEEP})
    assert "unknown-step-reference" in codes(root)


def test_a_ref_inside_a_quoted_literal_is_not_a_ref(tmp_path):
    """`$literal` on the right of a comparison is a string, not a path."""
    flow = mutate(CLEAN_INTAKE, "post", when="$steps.upsert.output.created == '$nope.x'")
    root = write_bundle(tmp_path / "b", flows={"intake": flow, "sweep": CLEAN_SWEEP})
    assert check_bundle(root).findings == []


def test_when_validates_its_own_references(tmp_path):
    flow = mutate(CLEAN_INTAKE, "post", when="$steps.nosuch.output.x == 1")
    root = write_bundle(tmp_path / "b", flows={"intake": flow, "sweep": CLEAN_SWEEP})
    assert "unknown-step-reference" in codes(root)


# ── columns ───────────────────────────────────────────────────────────


def test_write_to_undeclared_column_is_an_error(tmp_path):
    """The store accepts it silently and no $ref can ever reach it."""
    flow = json.loads(json.dumps(CLEAN_INTAKE))
    for step in flow["steps"]:
        if step["id"] == "upsert":
            step["args"]["rows"][0]["Post Message Id"] = "x"
    root = write_bundle(tmp_path / "b", flows={"intake": flow, "sweep": CLEAN_SWEEP})
    assert "undeclared-column" in codes(root)


def test_filter_on_undeclared_column_warns(tmp_path):
    flow = json.loads(json.dumps(CLEAN_SWEEP))
    for step in flow["steps"]:
        if step["id"] == "stale":
            step["args"]["filter"]["Nope"] = "x"
    root = write_bundle(tmp_path / "b", flows={"intake": CLEAN_INTAKE, "sweep": flow})
    report = check_bundle(root)
    assert [f.code for f in report.warnings] == ["unknown-filter-column"]
    assert report.ok


def test_underscore_columns_are_store_internals(tmp_path):
    flow = json.loads(json.dumps(CLEAN_SWEEP))
    for step in flow["steps"]:
        if step["id"] == "stale":
            step["args"]["filter"]["_record_id"] = "x"
    assert (
        check_bundle(
            write_bundle(tmp_path / "b", flows={"intake": CLEAN_INTAKE, "sweep": flow})
        ).findings
        == []
    )


def test_columns_unchecked_when_manifest_omits_the_table(tmp_path):
    manifest = {k: v for k, v in CLEAN_MANIFEST.items() if k != "tables"}
    root = write_bundle(tmp_path / "b", manifest=manifest)
    assert "undeclared-column" not in codes(root)


# ── manifest ──────────────────────────────────────────────────────────


def test_untyped_bundle_warns_about_app_type(tmp_path):
    manifest = {k: v for k, v in CLEAN_MANIFEST.items() if k != "app_type"}
    report = check_bundle(write_bundle(tmp_path / "b", manifest=manifest))
    assert [f.code for f in report.warnings] == ["clears-app-type"]
    assert report.ok  # intentional for an untyped bundle


def test_schedule_naming_an_unknown_flow(tmp_path):
    manifest = {**CLEAN_MANIFEST, "schedules": [{"flow": "swept", "interval": 300}]}
    assert "schedule-unknown-flow" in codes(write_bundle(tmp_path / "b", manifest=manifest))


def test_schedule_without_a_trigger_can_never_fire(tmp_path):
    manifest = {**CLEAN_MANIFEST, "schedules": [{"flow": "sweep", "slug": "s"}]}
    assert "schedule-no-trigger" in codes(write_bundle(tmp_path / "b", manifest=manifest))


def test_schedule_declaring_both_cadences_is_rejected(tmp_path):
    # The backend's resolve_schedule refuses a spec carrying both rather
    # than silently dropping one, so a bundle declaring both cannot
    # install. Without this check the author only found out during an
    # install against a real channel (KEW-2155).
    manifest = {
        **CLEAN_MANIFEST,
        "schedules": [{"flow": "sweep", "slug": "s", "interval": 300, "cron": "0 9 * * *"}],
    }
    found = codes(write_bundle(tmp_path / "b", manifest=manifest))
    assert "schedule-two-triggers" in found
    # The two cadence checks are independent: naming both must not also
    # trip the "can never fire" one, which would read as contradictory
    # advice in the same report.
    assert "schedule-no-trigger" not in found


def test_one_cadence_alone_trips_neither_cadence_check(tmp_path):
    for i, cadence in enumerate(({"interval": 300}, {"cron": "0 9 * * *"})):
        manifest = {
            **CLEAN_MANIFEST,
            "schedules": [{"flow": "sweep", "slug": "s", **cadence}],
        }
        found = codes(write_bundle(tmp_path / f"b{i}", manifest=manifest))
        assert "schedule-two-triggers" not in found, cadence
        assert "schedule-no-trigger" not in found, cadence


def test_webhook_naming_an_unknown_flow(tmp_path):
    manifest = {**CLEAN_MANIFEST, "webhooks": [{"name": "In", "flow": "intak"}]}
    assert "webhook-unknown-flow" in codes(write_bundle(tmp_path / "b", manifest=manifest))


def test_concat_column_must_be_a_string(tmp_path):
    manifest = json.loads(json.dumps(CLEAN_MANIFEST))
    for col in manifest["tables"]["widgets"]["columns"]:
        if col["name"] == "Seen At":
            col["type"] = "datetime"
    assert "concat-requires-string" in codes(write_bundle(tmp_path / "b", manifest=manifest))


def test_merge_key_must_be_string_typed(tmp_path):
    """A non-string merge key silently never matches the text-index probe."""
    manifest = json.loads(json.dumps(CLEAN_MANIFEST))
    for col in manifest["tables"]["widgets"]["columns"]:
        if col["name"] == "Fingerprint":
            col["type"] = "number"
    assert "merge-key-not-string" in codes(write_bundle(tmp_path / "b", manifest=manifest))


def test_merge_key_must_be_indexed(tmp_path):
    manifest = json.loads(json.dumps(CLEAN_MANIFEST))
    for col in manifest["tables"]["widgets"]["columns"]:
        if col["name"] == "Fingerprint":
            col.pop("unique")
    assert "merge-key-not-indexed" in codes(write_bundle(tmp_path / "b", manifest=manifest))


def test_merge_key_naming_an_unknown_column(tmp_path):
    manifest = json.loads(json.dumps(CLEAN_MANIFEST))
    manifest["tables"]["widgets"]["merge_key"]["any_of"] = ["Nope"]
    assert "merge-key-unknown-column" in codes(write_bundle(tmp_path / "b", manifest=manifest))


def test_runtime_scalar_declared_in_manifest_warns(tmp_path):
    """`scalars:` upserts every install, resetting whatever the flow maintains."""
    manifest = {**CLEAN_MANIFEST, "scalars": {"widget_summary": "0"}}
    flow = json.loads(json.dumps(CLEAN_SWEEP))
    flow["steps"].append(
        {
            "id": "summary",
            "activity": "foundation.store.set_scalar",
            "args": {
                "conversation_id": "$inputs.conversation_id",
                "key": "widget_summary",
                "value": "$steps.now.output.unix_str",
            },
        }
    )
    root = write_bundle(
        tmp_path / "b", manifest=manifest, flows={"intake": CLEAN_INTAKE, "sweep": flow}
    )
    report = check_bundle(root)
    assert [f.code for f in report.warnings] == ["runtime-state-in-scalars"]


def test_unparseable_yaml_is_reported(tmp_path):
    root = write_bundle(tmp_path / "b")
    (root / "broken.yaml").write_text("name: x\n  steps: [\n")
    assert "yaml-parse-error" in codes(root)


def test_report_serializes_for_json_output(tmp_path):
    report = check_bundle(write_bundle(tmp_path / "b"))
    payload = json.loads(json.dumps(report.to_dict()))
    assert payload["ok"] is True
    assert payload["error_count"] == 0
    assert sorted(f["name"] for f in payload["flows"]) == ["intake", "sweep"]


# ── blocks, and the other step shapes ─────────────────────────────────
#
# A step is exactly one of `activity`, `sleep_seconds`, `await_approval` or a
# nested `steps:` block. Demanding `activity:` made 22 steps across the shipped
# backend templates false errors — and, worse, meant nothing inside a block was
# ever checked at all.


def _block_flow(*, outputs=None, inner=None, reader=None) -> dict[str, Any]:
    """A flow whose second step is a `when:`-gated block."""
    block: dict[str, Any] = {
        "id": "maybe",
        "when": "$inputs.go",
        "steps": inner
        or [
            {
                "id": "inside",
                "activity": "foundation.channel.post",
                "args": {"channel_id": "$inputs.conversation_id", "text": "hi"},
            }
        ],
    }
    if outputs is not None:
        block["outputs"] = outputs
    steps: list[dict[str, Any]] = [
        {"id": "now", "activity": "foundation.workflow.now"},
        block,
    ]
    if reader is not None:
        steps.append(
            {
                "id": "after",
                "activity": "foundation.channel.post",
                "args": {"channel_id": "$inputs.conversation_id", "text": reader},
            }
        )
    return {
        "name": "blocky",
        "version": 1,
        "inputs": {"conversation_id": {"type": "string"}, "go": {"type": "boolean"}},
        "steps": steps,
    }


def test_a_block_is_a_legal_step(tmp_path):
    root = write_bundle(tmp_path / "b", flows={"blocky": _block_flow()}, manifest=bare_manifest())
    assert check_bundle(root).findings == []


@pytest.mark.parametrize(
    "step",
    [
        {"id": "wait", "sleep_seconds": 30},
        {"id": "ask", "await_approval": {"prompt": "ok?", "contact_id": "$inputs.conversation_id"}},
    ],
)
def test_the_other_actionless_step_shapes_are_legal(tmp_path, step):
    flow = {
        "name": "waity",
        "version": 1,
        "inputs": {"conversation_id": {"type": "string"}},
        "steps": [step],
    }
    root = write_bundle(tmp_path / "b", flows={"waity": flow}, manifest=bare_manifest())
    assert check_bundle(root).findings == []


def test_a_step_with_no_action_at_all_is_still_an_error(tmp_path):
    flow = {
        "name": "empty",
        "version": 1,
        "inputs": {},
        "steps": [{"id": "nothing", "when": "yes"}],
    }
    root = write_bundle(tmp_path / "b", flows={"empty": flow}, manifest=bare_manifest())
    assert "step-without-action" in codes(root)


def test_refs_inside_a_block_are_checked(tmp_path):
    """The old checker stopped at the block, so a broken ref inside one was
    invisible — the expensive kind of false negative."""
    inner = [
        {
            "id": "inside",
            "activity": "foundation.channel.post",
            "args": {"channel_id": "$inputs.conversation_id", "text": "$inputs.nosuch"},
        }
    ]
    root = write_bundle(
        tmp_path / "b", flows={"blocky": _block_flow(inner=inner)}, manifest=bare_manifest()
    )
    assert "undeclared-input" in codes(root)


def test_a_block_sees_the_enclosing_scope(tmp_path):
    inner = [
        {
            "id": "inside",
            "activity": "foundation.channel.post",
            "args": {"channel_id": "$inputs.conversation_id", "text": "$steps.now.output.iso"},
        }
    ]
    root = write_bundle(
        tmp_path / "b", flows={"blocky": _block_flow(inner=inner)}, manifest=bare_manifest()
    )
    assert check_bundle(root).findings == []


def test_a_blocks_inner_ids_are_private_to_it(tmp_path):
    root = write_bundle(
        tmp_path / "b",
        flows={"blocky": _block_flow(reader="$steps.inside.output.message_id")},
        manifest=bare_manifest(),
    )
    assert "unknown-step-reference" in codes(root)


def test_a_block_publishes_only_its_declared_outputs(tmp_path):
    flow = _block_flow(
        outputs={"posted": "$steps.inside.output"},
        reader="$steps.maybe.output.posted",
    )
    root = write_bundle(tmp_path / "b", flows={"blocky": flow}, manifest=bare_manifest())
    assert check_bundle(root).findings == []


def test_reading_an_undeclared_block_output_is_an_error(tmp_path):
    flow = _block_flow(
        outputs={"posted": "$steps.inside.output"},
        reader="$steps.maybe.output.something_else",
    )
    root = write_bundle(tmp_path / "b", flows={"blocky": flow}, manifest=bare_manifest())
    assert "unknown-block-output" in codes(root)


def test_a_blocks_outputs_resolve_in_its_inner_scope(tmp_path):
    """`outputs:` is evaluated after the inner steps ran, so it may name them —
    and only them."""
    flow = _block_flow(outputs={"posted": "$steps.nosuch.output"})
    root = write_bundle(tmp_path / "b", flows={"blocky": flow}, manifest=bare_manifest())
    assert "unknown-step-reference" in codes(root)


# ── collect, indexes, foreach ─────────────────────────────────────────


def _foreach_flow(*, ref: str, when: str | None = None) -> dict[str, Any]:
    step: dict[str, Any] = {
        "id": "fan",
        "activity": "foundation.channel.post",
        "foreach": "$steps.rows.output.rows",
        "as": "row",
        "collect": "posted",
        "args": {"channel_id": "$inputs.conversation_id", "text": "$row.Title"},
    }
    if when is not None:
        step["when"] = when
    return {
        "name": "fanout",
        "version": 1,
        "inputs": {"conversation_id": {"type": "string"}},
        "steps": [
            {
                "id": "rows",
                "activity": "foundation.store.list_rows",
                "args": {"conversation_id": "$inputs.conversation_id", "table_name": "widgets"},
            },
            step,
            {
                "id": "report",
                "activity": "foundation.channel.post",
                "args": {"channel_id": "$inputs.conversation_id", "text": ref},
            },
        ],
    }


def test_a_collected_list_is_readable_by_its_declared_name(tmp_path):
    """`collect: posted` publishes `$steps.fan.posted` alongside `.output` —
    19 refs across the shipped templates use this and were all errors."""
    root = write_bundle(
        tmp_path / "b",
        manifest=bare_manifest(),
        flows={"fanout": _foreach_flow(ref="$steps.fan.posted")},
    )
    assert check_bundle(root).findings == []


def test_a_name_that_is_neither_output_nor_the_collect_name_is_an_error(tmp_path):
    root = write_bundle(
        tmp_path / "b",
        manifest=bare_manifest(),
        flows={"fanout": _foreach_flow(ref="$steps.fan.gathered")},
    )
    assert "step-ref-needs-output" in codes(root)


def test_a_foreach_when_is_a_per_item_gate(tmp_path):
    """On a foreach step `when:` runs once per item with the alias bound, so
    naming the alias there is the idiom for skipping individual items."""
    root = write_bundle(
        tmp_path / "b",
        manifest=bare_manifest(),
        flows={"fanout": _foreach_flow(ref="$steps.fan.posted", when="$row.Status == 'firing'")},
    )
    assert check_bundle(root).findings == []


def test_a_non_foreach_when_has_no_item_alias(tmp_path):
    flow = mutate(CLEAN_INTAKE, "post", when="$row.Status == 'firing'")
    root = write_bundle(tmp_path / "b", flows={"intake": flow, "sweep": CLEAN_SWEEP})
    assert "unknown-reference-root" in codes(root)


def test_a_numeric_segment_is_an_array_index(tmp_path):
    """`$steps.fields.output.0.title` indexes a list. `properties` describes an
    object, so there is nothing to match '0' against — the old code reported it
    as an undeclared property."""
    flow = mutate(CLEAN_INTAKE, "post", args={"text": "$steps.fields.output.0.title"})
    root = write_bundle(tmp_path / "b", flows={"intake": flow, "sweep": CLEAN_SWEEP})
    assert check_bundle(root).findings == []


def test_a_named_property_is_still_checked(tmp_path):
    flow = mutate(CLEAN_INTAKE, "post", args={"text": "$steps.fields.output.nope"})
    root = write_bundle(tmp_path / "b", flows={"intake": flow, "sweep": CLEAN_SWEEP})
    assert "unknown-output-property" in codes(root)


# ── $trigger ──────────────────────────────────────────────────────────


def test_trigger_is_a_reference_root(tmp_path):
    """70 refs across the shipped templates read `$trigger`; every one was an
    unknown-reference-root error."""
    flow = mutate(CLEAN_INTAKE, "post", args={"text": "$trigger.thread_root"})
    root = write_bundle(tmp_path / "b", flows={"intake": flow, "sweep": CLEAN_SWEEP})
    assert check_bundle(root).findings == []


def test_an_unknown_trigger_key_is_an_error(tmp_path):
    """The trigger scope is a closed set, unlike $channel — so a typo in it is
    one of the few things the checker can be strict about."""
    flow = mutate(CLEAN_INTAKE, "post", args={"text": "$trigger.thread"})
    root = write_bundle(tmp_path / "b", flows={"intake": flow, "sweep": CLEAN_SWEEP})
    assert "unknown-trigger-key" in codes(root)


def test_trigger_keys_match_what_the_interpreter_seeds(tmp_path):
    """Guards the vendored TRIGGER_KEYS against backend drift.

    `user_id` was missing here for long enough that three shipped bundles
    reported `unknown-trigger-key` against a key the interpreter really
    does seed — a false positive telling an author their correct flow was
    broken. tests/test_backend_templates.py is what caught it, and that
    file does not run in CI, so the closed set is pinned here too.

    The expected set is written out rather than derived from
    TRIGGER_KEYS: a test that loops over the set under test passes no
    matter what is removed from it. Transcribed from the interpreter's
    own `trigger={...}` dict; when that grows a key this fails until
    someone copies it across. The durable fix is serving the shape
    instead of vendoring it (KEW-2150).
    """
    assert {
        "thread_id",
        "message_id",
        "user_id",
        "conversation_id",
        "thread_root",
        "contact_id",
        "workflow_id",
        "run_id",
    } == TRIGGER_KEYS
    # And each one is actually accepted by the ref checker, not merely
    # present in the constant.
    for key in sorted(TRIGGER_KEYS):
        flow = mutate(CLEAN_INTAKE, "post", args={"text": f"$trigger.{key}"})
        root = write_bundle(tmp_path / f"b-{key}", flows={"intake": flow, "sweep": CLEAN_SWEEP})
        assert "unknown-trigger-key" not in codes(root), key


def test_a_foreach_alias_shadows_a_global_root(tmp_path):
    """The interpreter resolves aliases before `channel` and `trigger`
    precisely so `as: trigger` keeps working."""
    flow = _foreach_flow(ref="$steps.fan.posted")
    for step in flow["steps"]:
        if step["id"] == "fan":
            step["as"] = "trigger"
            step["args"]["text"] = "$trigger.Title"
    root = write_bundle(tmp_path / "b", manifest=bare_manifest(), flows={"fanout": flow})
    assert check_bundle(root).findings == []


# ── integrations ──────────────────────────────────────────────────────


def _integration_flow(name: str = "gcal", *, declared: bool = True) -> dict[str, Any]:
    flow: dict[str, Any] = {
        "name": "booker",
        "version": 1,
        "inputs": {"conversation_id": {"type": "string"}},
        "steps": [
            {
                "id": "book",
                "activity": "foundation.channel.post",
                "args": {
                    "channel_id": "$inputs.conversation_id",
                    "integration_id": f"$channel.integrations.{name}.id",
                },
            }
        ],
    }
    if declared:
        flow["required_integrations"] = {name: {"description": "cal", "provider": "google"}}
    return flow


def test_required_integrations_declares_a_channel_integration(tmp_path):
    root = write_bundle(
        tmp_path / "b", manifest=bare_manifest(), flows={"booker": _integration_flow()}
    )
    assert check_bundle(root).findings == []


def test_a_manifest_connection_config_name_also_declares_it(tmp_path):
    """claimcoordinator declares `leads_inbox` in the manifest and
    deliberately in no flow — stating it there is what lets the client bind
    the name on the first connect click."""
    manifest = bare_manifest(connections=[{"id": "gmail", "config_name": "leads_inbox", "min": 1}])
    root = write_bundle(
        tmp_path / "b",
        manifest=manifest,
        flows={"booker": _integration_flow("leads_inbox", declared=False)},
    )
    assert check_bundle(root).findings == []


def test_an_integration_declared_nowhere_warns(tmp_path):
    root = write_bundle(
        tmp_path / "b",
        manifest=bare_manifest(),
        flows={"booker": _integration_flow("ghost", declared=False)},
    )
    report = check_bundle(root)
    assert [f.code for f in report.warnings] == ["undeclared-integration"]
    assert report.ok, "an undeclared integration can still resolve at runtime"


def test_integration_list_needs_no_declaration(tmp_path):
    """The interpreter seeds it itself, as an array to fan out over."""
    flow = _integration_flow(declared=False)
    flow["steps"][0]["args"]["integration_id"] = "$channel.integration_list"
    root = write_bundle(tmp_path / "b", manifest=bare_manifest(), flows={"booker": flow})
    assert check_bundle(root).findings == []


# ── prompts and templates ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "filename",
    [
        "compose.md.j2",
        "compose.md.jinja",
        "compose.j2",
        "compose.jinja",
        "compose.md",
        "compose.txt",
    ],
)
def test_a_prompt_is_found_under_every_real_suffix(tmp_path, filename):
    """`Path.stem` strips ONE suffix, so `compose.md.j2` was read as
    `compose.md` and never matched `$channel.prompts.compose`. Every shipped
    template's prompts use `.md.j2`."""
    flow = mutate(CLEAN_INTAKE, "post", args={"text": "$channel.prompts.compose"})
    root = write_bundle(tmp_path / "b", flows={"intake": flow, "sweep": CLEAN_SWEEP})
    (root / "prompts").mkdir()
    (root / "prompts" / filename).write_text("hello")
    assert check_bundle(root).findings == []


def test_a_missing_prompt_is_still_an_error(tmp_path):
    flow = mutate(CLEAN_INTAKE, "post", args={"text": "$channel.prompts.absent"})
    root = write_bundle(tmp_path / "b", flows={"intake": flow, "sweep": CLEAN_SWEEP})
    (root / "prompts").mkdir()
    (root / "prompts" / "compose.md.j2").write_text("hello")
    assert "unknown-prompt" in codes(root)
