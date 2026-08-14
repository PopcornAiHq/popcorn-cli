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

from popcorn_core.template_check import check_bundle

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
    "expr,expected",
    [
        (
            "$steps.upsert.output.created == 1 && $steps.now.output.iso != ''",
            "when-unsupported-operator",
        ),
        ("$steps.upsert.output.created > 0", "when-unsupported-operator"),
        ("$steps.upsert.output.created >= 1", "when-unsupported-operator"),
        ("$steps.upsert.output.created", "when-not-a-comparison"),
    ],
)
def test_when_grammar_violations(tmp_path, expr, expected):
    flow = mutate(CLEAN_INTAKE, "post", when=expr)
    root = write_bundle(tmp_path / "b", flows={"intake": flow, "sweep": CLEAN_SWEEP})
    assert expected in codes(root)


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
