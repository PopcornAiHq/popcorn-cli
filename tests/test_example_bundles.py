"""Every bundle under `examples/` must survive the offline checker.

Nothing in CI used to parse the shipped examples, so a broken `alert_tick`
lived in `examples/alerttracker/` for a day without a single test noticing.
This is that gate. It is deliberately data-driven over the directory: a new
example is covered the moment it is added, with no test to remember to write.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from popcorn_core.template_check import check_bundle

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

BUNDLES = sorted(p for p in EXAMPLES.iterdir() if p.is_dir()) if EXAMPLES.is_dir() else []

# `clears-app-type` fires on every untyped bundle. Both shipped examples are
# untyped on purpose — they are ops bundles for a dedicated channel — and each
# says so in its README and manifest comments. Any OTHER warning is a finding
# the example should either fix or explain here.
ALLOWED_WARNINGS = {"clears-app-type"}


def test_examples_directory_is_not_empty():
    """Guards the parametrization: an empty glob would vacuously pass."""
    assert BUNDLES, f"no example bundles found under {EXAMPLES}"


@pytest.mark.parametrize("bundle", BUNDLES, ids=lambda p: p.name)
def test_example_bundle_has_no_structural_errors(bundle: Path):
    report = check_bundle(bundle)
    assert report.ok, "\n".join(str(f) for f in report.errors)


@pytest.mark.parametrize("bundle", BUNDLES, ids=lambda p: p.name)
def test_example_bundle_warnings_are_expected(bundle: Path):
    unexpected = [f for f in check_bundle(bundle).warnings if f.code not in ALLOWED_WARNINGS]
    assert not unexpected, "\n".join(str(f) for f in unexpected)


@pytest.mark.parametrize("bundle", BUNDLES, ids=lambda p: p.name)
def test_example_bundle_ships_a_readme(bundle: Path):
    assert (bundle / "README.md").is_file(), f"{bundle.name} has no README.md"
