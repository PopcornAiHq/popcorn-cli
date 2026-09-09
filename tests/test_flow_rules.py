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

import sys
from pathlib import Path
from typing import Any

import pytest

from popcorn_core import flow_rules

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

# Imported after the path insert above, which is what makes it importable.
import sync_flow_rules

# ── the pinned values ─────────────────────────────────────────────────


def test_the_step_union_is_the_four_the_dsl_enforces():
    """A step is exactly one of these; the checker's step-without-action
    finding lists them in this order because the server's own error does."""
    assert flow_rules.STEP_ACTIONS == ("activity", "sleep_seconds", "await_approval", "steps")


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
    until backend#1923 — which is why `template check` reported a false
    `basename-collision` on any bundle with two Python blocks.

    `CODE_MIN_PATH_DEPTH` is a floor where `SUBDIR_PATH_DEPTH` is exact: a
    block may be a small package, so a file nested deeper is still read.
    """
    assert flow_rules.CODE_SUBDIR == "code"
    assert flow_rules.CODE_BLOCK_NAME_PATTERN == r"^[a-z0-9][a-z0-9_-]{0,62}$"
    assert flow_rules.CODE_MIN_PATH_DEPTH == 3
    assert flow_rules.CODE_PATH_SEGMENT_PATTERN == r"^[^.][^/]*$"


# ── the generator ─────────────────────────────────────────────────────


def _payload() -> dict[str, Any]:
    """A payload that renders back to the committed module.

    Inverts `_FIELDS` over the module's own constants instead of restating
    them: the values are already pinned above, and what this needs to be is
    exactly what the endpoint last served, whatever that was.
    """
    payload: dict[str, Any] = {"ok": True, "flow_schema": {"title": "Flow"}}
    for field in sync_flow_rules._FIELDS:
        value = getattr(flow_rules, field.name)
        if isinstance(value, tuple):
            value = list(value)
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
