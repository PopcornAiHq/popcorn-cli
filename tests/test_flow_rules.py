"""The generated flow-rules snapshot, and the script that generates it.

Two different jobs here, and only one of them can run in CI.

**Pinning the values** is what this file can do offline, and it is what the
vendored constants never had: every value the checker reads is asserted
longhand below, so removing or changing one fails a test instead of quietly
changing what `template check` accepts. Written out rather than derived from
the module under test — a test that loops over the constant it guards passes no
matter what is deleted from it, which has already bitten this repo three times.

**Proving the values match the platform** needs the endpoint, and the endpoint
needs workspace-member credentials, so `make check-rules` owns that and no test
here pretends to. What these tests do cover is the machinery around it: that
the render is deterministic and rejects a payload it does not fully account
for, and that a fetch it cannot complete comes back as a failure rather than a
shrug.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from popcorn_core import flow_rules

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

# Imported after the path insert above, which is what makes it importable.
import sync_flow_rules

# ── the pinned values ─────────────────────────────────────────────────


def test_the_step_union_is_the_five_the_dsl_enforces():
    """A step is exactly one of these; the checker's step-without-action
    finding lists them in this order because the server's own error does.
    `call_flow` was served before the checker read it, which is why every
    shipped bundle calling a child flow reported `step-without-action`."""
    assert flow_rules.STEP_ACTIONS == (
        "activity",
        "sleep_seconds",
        "await_approval",
        "call_flow",
        "steps",
    )


def test_a_step_error_carries_message_and_type():
    """Hand-maintained in the checker until the endpoint served it, so a
    property the server added would have been reported as a typo."""
    assert flow_rules.STEP_ERROR_PROPERTIES == ("message", "type")


def _column_args(name: str) -> list[tuple[str, str, str]]:
    return [
        (c["arg"], c["holds"], c["side"]) for c in flow_rules.ACTIVITY_ROLES[name]["column_args"]
    ]


def test_the_activities_carrying_column_names():
    """Every activity the column checks fire on, and the args they read.

    The hand-written list these replace named two activities the platform has
    never had (an `insert_rows` and a `get_row`), so the checks keyed on them
    never fired, and missed `merge_on` — a column a table does not declare can
    never match, so every upsert adds a row instead of merging.
    """
    with_columns = {n for n, r in flow_rules.ACTIVITY_ROLES.items() if r["column_args"]}
    assert with_columns == {
        "foundation.store.upsert_rows",
        "foundation.store.patch_row",
        "foundation.store.list_rows",
    }
    assert _column_args("foundation.store.upsert_rows") == [
        ("rows", "column_keys", "write"),
        ("merge_on", "column_names", "write"),
    ]
    assert _column_args("foundation.store.patch_row") == [("patch", "column_keys", "write")]
    assert _column_args("foundation.store.list_rows") == [
        ("filter", "column_keys", "read"),
        ("drop_columns", "column_names", "read"),
    ]


def test_the_activities_declaring_their_output_at_the_call_site():
    """`feature.email.extract` takes its schema as `schema`, not
    `output_schema` — the reason the arg name is served rather than assumed,
    and why its steps' output references went unchecked before."""
    served = {
        n: r["output_schema_arg"]
        for n, r in flow_rules.ACTIVITY_ROLES.items()
        if r["output_schema_arg"]
    }
    assert served == {
        "feature.email.extract": "schema",
        "foundation.agent.transform": "output_schema",
        "foundation.fields.extract": "output_schema",
    }


def test_the_reference_grammar_is_root_plus_optional_dotted_path():
    """Pinned as the literal pattern, because the two-group split is what the
    checker's `_ref_parts` depends on and what the flat one-group form it used
    to carry got wrong: that one accepted `$a.`, `$a..b` and `$a.1b`, all three
    of which the interpreter rejects."""
    assert flow_rules.REFERENCE_PATTERN == (
        r"^\$([A-Za-z_][A-Za-z0-9_]*)(?:\.([A-Za-z_][A-Za-z0-9_.]*))?$"
    )


def test_the_reference_roots_are_the_four_the_checker_models():
    """Each of these has its own branch in `_check_value`.

    A fifth root would land in the branch that accepts an unmodelled root
    rather than reporting it — the safe direction, but it means the new root's
    keys go unchecked. This assertion is how that gets noticed and closed
    deliberately instead of by an author reading a wrong finding.
    """
    assert flow_rules.REFERENCE_ROOTS == ("channel", "inputs", "steps", "trigger")


def test_the_trigger_scope_is_a_closed_set_of_eight_keys():
    """`user_id` was missing from the vendored copy for long enough that three
    shipped bundles reported `unknown-trigger-key` against a key the
    interpreter really does seed — a false positive telling an author their
    correct flow was broken."""
    assert flow_rules.TRIGGER_KEYS == (
        "thread_id",
        "message_id",
        "user_id",
        "conversation_id",
        "thread_root",
        "contact_id",
        "workflow_id",
        "run_id",
    )


def test_the_block_nesting_cap_is_three():
    """Counting a flow's own top-level list as 1, so one block inside another
    is the deepest legal shape. Unenforced here until this snapshot landed: a
    block nested past it passed `template check` and failed at install."""
    assert flow_rules.MAX_BLOCK_DEPTH == 3


def test_the_reserved_filenames_are_the_manifest_its_alias_and_three_docs():
    assert flow_rules.MANIFEST_FILENAMES == ("manifest.yaml", "config.yaml")
    assert flow_rules.AGENT_DOC_FILENAME == "AGENT.md"
    assert flow_rules.README_FILENAME == "README.md"
    assert flow_rules.STRINGS_FILENAME == "strings.yaml"


def test_the_bundle_layout_rules():
    """`SUBDIR_PATH_DEPTH` is exact, not a maximum — the tree reader tests the
    segment count, so `prompts/a/b.md` is ignored rather than read one level
    deeper, and a checker that reads it as a prompt claims a
    `$channel.prompts.b` that nothing ever seeds."""
    assert flow_rules.FLOW_SUFFIXES == (".yaml", ".yml")
    assert flow_rules.FLOWS_AT_ROOT_ONLY is True
    assert flow_rules.SUBDIRS == ("prompts", "templates")
    assert flow_rules.SUBDIR_PATH_DEPTH == 2
    assert flow_rules.FILE_KEY_SUFFIXES == (
        ".md.j2",
        ".md.jinja",
        ".j2",
        ".jinja",
        ".md",
        ".txt",
    )
    assert flow_rules.MAX_ENTRY_BYTES == 1024 * 1024


def test_the_code_block_rules():
    """The third classification, and the one the payload carried nothing about
    until a server-side change — which is why `template check` reported a false
    `basename-collision` on any bundle with two Python blocks.

    `CODE_MIN_PATH_DEPTH` is a floor where `SUBDIR_PATH_DEPTH` is exact: a
    block may be a small package, so a file nested deeper is still read.
    """
    assert flow_rules.CODE_SUBDIR == "code"
    assert flow_rules.CODE_BLOCK_NAME_PATTERN == r"^[a-z0-9][a-z0-9_-]{0,62}$"
    assert flow_rules.CODE_MIN_PATH_DEPTH == 3
    assert flow_rules.CODE_PATH_SEGMENT_PATTERN == r"^[^.][^/]*$"


def test_the_agent_rules():
    """The fourth classification. Every agent directory holds the same
    filenames, so a checker without this rule reports each pair of agents as
    a basename collision, and each `agent.yaml` as a flow with no steps."""
    assert flow_rules.AGENTS_SUBDIR == "agents"
    assert flow_rules.AGENT_FILENAMES == ("agent.yaml", "prompt.md")
    assert flow_rules.AGENT_SCHEMAS_SUBDIR == "schemas"
    assert flow_rules.AGENT_SCHEMA_SUFFIX == ".json"


# ── the generator ─────────────────────────────────────────────────────


def _as_json(value: Any) -> Any:
    """A rendered constant back in the payload's own types: tuples to lists."""
    if isinstance(value, tuple):
        return [_as_json(item) for item in value]
    if isinstance(value, dict):
        return {key: _as_json(item) for key, item in value.items()}
    return value


def _payload() -> dict[str, Any]:
    """A payload that renders back to the committed module.

    Inverts `_FIELDS` over the module's own constants instead of restating
    them: the values are already pinned above, and what this needs to be is
    exactly what the endpoint last served, whatever that was.
    """
    payload: dict[str, Any] = {"ok": True, "flow_schema": {"title": "Flow"}}
    for field in sync_flow_rules._FIELDS:
        value = _as_json(getattr(flow_rules, field.name))
        node = payload
        for key in field.source[:-1]:
            node = node.setdefault(key, {})
        node[field.source[-1]] = value
    return payload


def test_the_committed_module_is_exactly_what_the_generator_produces():
    """So a hand-edit to the generated file fails here rather than surviving
    until someone runs `make check-rules`.

    Not a claim that the values are current — nothing offline can make that
    claim. It is the weaker and still useful one: this file is a faithful
    render, so its diff history is a history of the endpoint's answers.
    """
    assert sync_flow_rules.render(_payload()) == sync_flow_rules.TARGET.read_text()


def test_the_render_is_deterministic():
    """`--check` compares rendered bytes to the committed file, so any
    nondeterminism — a timestamp, a set iteration — would report drift that is
    not there and train everyone to ignore the gate."""
    assert sync_flow_rules.render(_payload()) == sync_flow_rules.render(_payload())


def test_a_newly_served_rule_is_an_error_not_a_silent_skip():
    """The case worth failing on. A rule the endpoint grows and this script
    ignores is a rule `template check` does not enforce, and nothing else would
    ever say so."""
    payload = _payload()
    payload["when_grammar"] = {"rails": 4}
    with pytest.raises(ValueError, match="when_grammar"):
        sync_flow_rules.render(payload)


def test_a_newly_served_bundle_rule_is_an_error_too():
    """`code_entrypoints` is the real candidate, which is why it stands in here.

    The entrypoint convention (`main.py` for Python, `index.js` or a
    `package.json` `main` for Node) is declared in the code runner, which the
    API image does not ship — so serving it needs the declaration hoisted into
    a shared package first. If it ever does arrive, this test is what makes the
    CLI notice instead of quietly not enforcing it.
    """
    payload = _payload()
    payload["bundle"]["code_entrypoints"] = {"python": "main.py"}
    with pytest.raises(ValueError, match="code_entrypoints"):
        sync_flow_rules.render(payload)


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda r: r["foundation.store.list_rows"].update(scans=True), "scans"),
        (
            lambda r: r["foundation.store.list_rows"]["column_args"][0].update(holds="column_json"),
            "column_json",
        ),
        (
            lambda r: r["foundation.store.list_rows"]["column_args"][0].update(side="both"),
            "both",
        ),
        (lambda r: r["foundation.store.list_rows"].update(reads_rows="yes"), "booleans"),
        (
            lambda r: r["foundation.agent.transform"].update(output_schema_arg=3),
            "output_schema_arg",
        ),
    ],
    ids=["new-role-key", "new-holds", "new-side", "non-bool-flag", "non-string-schema-arg"],
)
def test_a_role_the_checker_cannot_read_is_refused(mutate, match):
    """A new `holds` or `side` would reach the checker as a value it matches
    against nothing, so a bundle breaking the new rule would read as clean —
    the silent failure the whole snapshot exists to end."""
    payload = _payload()
    mutate(payload["activity_roles"])
    with pytest.raises(ValueError, match=match):
        sync_flow_rules.render(payload)


def test_a_withdrawn_rule_names_the_constant_that_reads_it():
    payload = _payload()
    del payload["max_block_depth"]
    with pytest.raises(ValueError, match="MAX_BLOCK_DEPTH"):
        sync_flow_rules.render(payload)


def test_a_list_rule_must_hold_strings():
    payload = _payload()
    payload["step_actions"] = ["activity", 4]
    with pytest.raises(ValueError, match="step_actions"):
        sync_flow_rules.render(payload)


def test_an_unreachable_endpoint_fails_rather_than_skipping(monkeypatch, capsys):
    """The trap this gate exists to avoid.

    `tests/test_backend_templates.py` points at a backend checkout and skips
    when it cannot find one; the bundles it reads moved, and it stayed green
    and mute for a release because a skip on a missing checkout looks exactly
    like a skip on a moved one. A drift check that cannot reach its source has
    verified nothing, and must say so.
    """

    def boom() -> dict[str, Any]:
        raise RuntimeError("Token refresh failed")

    monkeypatch.setattr(sync_flow_rules, "fetch", boom)
    assert sync_flow_rules.main(["--check"]) == sync_flow_rules.EXIT_UNUSABLE
    assert "Token refresh failed" in capsys.readouterr().err


def test_check_passes_when_the_endpoint_agrees(monkeypatch, capsys):
    monkeypatch.setattr(sync_flow_rules, "fetch", _payload)
    assert sync_flow_rules.main(["--check"]) == 0
    assert "matches the endpoint" in capsys.readouterr().out


def test_check_reports_drift_with_the_diff(monkeypatch, capsys):
    def drifted() -> dict[str, Any]:
        payload = _payload()
        payload["max_block_depth"] = 4
        return payload

    monkeypatch.setattr(sync_flow_rules, "fetch", drifted)
    assert sync_flow_rules.main(["--check"]) == sync_flow_rules.EXIT_DRIFT
    out = capsys.readouterr()
    assert "-MAX_BLOCK_DEPTH = 3" in out.out
    assert "+MAX_BLOCK_DEPTH = 4" in out.out
    assert "make sync-rules" in out.err


def test_a_sync_writes_the_new_values(monkeypatch, tmp_path, capsys):
    def drifted() -> dict[str, Any]:
        payload = _payload()
        payload["trigger_keys"] = [*flow_rules.TRIGGER_KEYS, "campaign_id"]
        return payload

    target = tmp_path / "flow_rules.py"
    monkeypatch.setattr(sync_flow_rules, "TARGET", target)
    monkeypatch.setattr(sync_flow_rules, "_REPO", tmp_path)
    monkeypatch.setattr(sync_flow_rules, "fetch", drifted)
    assert sync_flow_rules.main([]) == 0
    assert '"campaign_id",' in target.read_text()
    assert "rewrote" in capsys.readouterr().out


def test_a_sync_from_a_saved_body_never_fetches(monkeypatch, tmp_path, capsys):
    """`--from` is how the post-deploy refresh runs with no credentials, so it
    must read only the file — a fetch there would fail for want of a login."""

    def no_fetch() -> dict[str, Any]:
        raise AssertionError("--from must not fetch")

    body = _payload()
    body["trigger_keys"] = [*flow_rules.TRIGGER_KEYS, "campaign_id"]
    source = tmp_path / "schema.json"
    source.write_text(json.dumps(body))
    target = tmp_path / "flow_rules.py"
    monkeypatch.setattr(sync_flow_rules, "TARGET", target)
    monkeypatch.setattr(sync_flow_rules, "_REPO", tmp_path)
    monkeypatch.setattr(sync_flow_rules, "fetch", no_fetch)
    assert sync_flow_rules.main(["--from", str(source)]) == 0
    assert '"campaign_id",' in target.read_text()


def test_an_unreadable_saved_body_fails_without_blaming_credentials(tmp_path, capsys):
    source = tmp_path / "schema.json"
    source.write_text("not json")
    assert sync_flow_rules.main(["--check", "--from", str(source)]) == sync_flow_rules.EXIT_UNUSABLE
    err = capsys.readouterr().err
    assert "could not build the snapshot" in err
    assert "auth status" not in err


def test_a_generated_module_is_importable_and_formatted():
    """The generated file goes through the same lint and format gates as
    handwritten source, so the renderer has to emit what ruff would: double
    quotes, and a magic trailing comma that keeps a collection exploded."""
    rendered = sync_flow_rules.render(_payload())
    namespace: dict[str, Any] = {}
    exec(compile(rendered, "flow_rules.py", "exec"), namespace)
    assert namespace["MAX_BLOCK_DEPTH"] == flow_rules.MAX_BLOCK_DEPTH
    # Double quotes, and the trailing comma that stops the formatter collapsing
    # the tuple onto one line — both are what keeps `ruff format --check` green
    # on a file nobody hand-formats.
    assert '    "activity",\n' in rendered
    assert "'activity'" not in rendered
    assert rendered.endswith("\n")
