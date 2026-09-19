"""Check the shipped server-side templates — opt-in, skipped without them.

**These are the fixtures that matter, and they cannot live in this repo.** The
checker was written against the two bundles now under `tests/fixtures/bundles/`,
and they happen to use none of: a nested `steps:` block, `collect:`, the `when:`
expression rail, an array-index ref, `required_integrations:`, `$trigger`, or a
`.md.j2` prompt.
Every one of those was a false positive — roughly 180 findings across the five
real templates, all of them wrong — and nothing in this repo could have caught
that, because nothing in this repo had ever been run against a bundle somebody
shipped.

Vendoring copies here would fix that for exactly as long as it took the server
to change one, so this reads a real checkout instead: point
`POPCORN_BACKEND_FLOWS` at the directory holding the shipped bundles — one
subdirectory per bundle, each with a `manifest.yaml`. Unset, the module skips.

That pointer is load-bearing and fails silently when it rots. The bundles have
moved within their own repo before, and this kept skipping, green and mute,
because a skip on an unset variable is indistinguishable from a skip on a stale
one. If this file has not reported on a real bundle in a while, check where the
variable points before believing the silence.

The consequence is real and worth stating plainly: **this does not run in CI.**
CI's guard is the grammar-feature coverage in `test_template_check.py`, which is
derived from what these templates do but is not the same as reading them. When
a shipped template starts using a DSL feature nobody wrote a unit test for,
this file is what notices, and only if someone runs it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from popcorn_core import flow_triggers
from popcorn_core.app_publish import collect_tree, unrecognized_code_paths
from popcorn_core.flow_rules import CODE_SUBDIR
from popcorn_core.template_check import check_bundle

_ENV = "POPCORN_BACKEND_FLOWS"


def _bundles() -> list[Path]:
    root = os.environ.get(_ENV)
    if not root or not Path(root).is_dir():
        return []
    return sorted(p for p in Path(root).iterdir() if p.is_dir() and (p / "manifest.yaml").is_file())


_BUNDLES = _bundles()

pytestmark = pytest.mark.skipif(
    not _BUNDLES,
    reason=f"no shipped-template checkout ({_ENV} unset or holding no bundles)",
)


@pytest.mark.parametrize("bundle", _BUNDLES, ids=lambda p: p.name)
def test_a_shipped_template_checks_clean(bundle: Path) -> None:
    """Errors mean the checker and the platform disagree about the DSL.

    Which of the two is wrong is the interesting question, and it has gone both
    ways: `when-not-a-comparison` was the checker describing a grammar the
    engine never had, while `nested-flow-file` is a real footgun the readers
    disagree about. Read the finding before believing either side.
    """
    report = check_bundle(bundle)
    assert report.errors == [], [str(f) for f in report.errors]


@pytest.mark.parametrize("bundle", _BUNDLES, ids=lambda p: p.name)
def test_a_shipped_template_has_no_warnings_either(bundle: Path) -> None:
    """Separate from errors because the verdict differs.

    Any new warning is news: a real defect in a template somebody shipped, or
    another gap in the checker's model of the DSL. Both want looking at;
    neither should be discovered by an author wading through noise.

    `path-not-published` is the exception, and it turned out to be a third case
    the original wording did not anticipate: authoring material living inside a
    bundle directory. One shipped template keeps an `evals/` tree of Python
    case files next to its bundle source. Publish leaves it behind, which is
    correct — the warning is accurate rather than a defect or a checker gap.
    Every other warning code still fails here.
    """
    report = check_bundle(bundle)
    unexpected = [f for f in report.warnings if f.code != "path-not-published"]
    assert unexpected == [], [str(f) for f in unexpected]


@pytest.mark.parametrize("bundle", _BUNDLES, ids=lambda p: p.name)
def test_a_shipped_templates_code_blocks_are_publishable(bundle: Path) -> None:
    """`app publish` must read the block source the server reads.

    The same argument as the checks above, applied to the publish path: a
    bundle shape nobody wrote a fixture for is one this CLI silently declines
    to publish. `codeblockshowcase` was exactly that — five blocks, eight
    files, the whole of `code/` landing in `ignored` — so an author could edit
    a block, publish, and be told it worked.

    Not asserted: `ignored == []`. `claimcoordinator` ships an `evals/`
    directory that both collectors skip by design, and demanding an empty
    `ignored` would call that a defect.
    """
    tree = collect_tree(bundle)
    assert [p for p in tree.ignored if p.startswith(CODE_SUBDIR)] == []
    assert unrecognized_code_paths(tree.files) == []
    on_disk = (bundle / CODE_SUBDIR).is_dir()
    collected = any(p.startswith(f"{CODE_SUBDIR}/") for p in tree.files)
    assert collected == on_disk


def _bundle_files(bundle: Path) -> dict[str, str]:
    return collect_tree(bundle).files


@pytest.mark.parametrize("bundle", _BUNDLES, ids=lambda p: p.name)
def test_every_flow_a_shipped_manifest_names_is_reported_as_triggered(bundle: Path) -> None:
    """A `flow:` in the manifest is a trigger the CLI must find.

    Webhooks, document uploads and state-graph edges all reach a flow by name
    from the manifest, and `flow get` now prints "nothing in this channel's
    bundle starts this flow" when it finds none — a verdict that is only safe
    if every manifest shape in use is actually read. The synthetic fixtures in
    `test_flow_triggers.py` are written from these; this is the check that
    they still describe them.
    """
    files = _bundle_files(bundle)
    manifest = yaml.safe_load(files.get("manifest.yaml") or "") or {}
    states = manifest.get("states") if isinstance(manifest, dict) else None

    named: set[str] = set()
    for section in ("webhooks", "documents"):
        for entry in manifest.get(section) or []:
            if isinstance(entry, dict) and entry.get("flow"):
                named.add(str(entry["flow"]))
    for edge in flow_triggers._state_edges(states):
        if edge.get("flow"):
            named.add(str(edge["flow"]))
        for consequence in edge.get("then") or []:
            if isinstance(consequence, dict) and consequence.get("flow"):
                named.add(str(consequence["flow"]))

    untriggered = [n for n in sorted(named) if not flow_triggers.analyze(n, files).has_trigger]
    assert untriggered == []


@pytest.mark.parametrize("bundle", _BUNDLES, ids=lambda p: p.name)
def test_a_shipped_state_edge_reports_its_event_name(bundle: Path) -> None:
    """`on:` is a YAML 1.1 boolean and parses to the key `True`.

    The failure mode is silent — every edge still resolves to the right flow,
    and only the event name degrades to "?" — so nothing but an assertion on a
    real graph catches it coming back.
    """
    files = _bundle_files(bundle)
    manifest = yaml.safe_load(files.get("manifest.yaml") or "") or {}
    states = manifest.get("states") if isinstance(manifest, dict) else None
    events = [flow_triggers._edge_event(e) for e in flow_triggers._state_edges(states)]
    assert all(events), "a shipped state edge parsed with no readable event name"


@pytest.mark.parametrize("bundle", _BUNDLES, ids=lambda p: p.name)
def test_a_shipped_bundles_literal_launches_are_all_attributed(bundle: Path) -> None:
    """Every `start_flow` step naming a flow outright becomes a caller edge.

    Counted from the bundle's own flow documents rather than from the
    analyzer, so a walk that stopped at the top level — blocks are where most
    of these live — fails here instead of quietly reporting fewer callers.
    """
    files = _bundle_files(bundle)
    docs = flow_triggers._flow_documents(files)
    expected: set[tuple[str, str]] = set()
    for caller, doc in docs.items():
        for step in flow_triggers._walk_steps(doc.get("steps")):
            if step.get("activity") != flow_triggers.START_FLOW_ACTIVITY:
                continue
            args = step.get("args")
            target = args.get("flow_name") if isinstance(args, dict) else None
            if isinstance(target, str) and not target.startswith("$"):
                expected.add((caller, target))

    found: set[tuple[str, str]] = set()
    for target in {t for _, t in expected}:
        for trigger in flow_triggers.analyze(target, files).of_kind("flow"):
            found.add((trigger.detail["flow"], target))
    assert found == expected
