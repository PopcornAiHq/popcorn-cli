"""Every bundle under `tests/fixtures/bundles/` must survive the offline checker.

Nothing in CI used to parse these, so a broken `alert_tick` lived in the tree
for a day without a single test noticing. This is that gate. It is
deliberately data-driven over the directory: a new bundle is covered the
moment it is added, with no test to remember to write.

**These bundles are test input, not documentation.** They used to live in
`examples/` and be offered to authors as reference templates, which was a
mistake on two counts: neither declared a `version:`, so neither could
actually complete a publish, and both had drifted from the bundles the
platform ships. Canonical bundle source now comes from the server —
`popcorn app checkout` — which cannot go stale. What is kept here is a corpus
broad enough to exercise the checker end to end on a whole bundle, and its
staleness relative to any shipped app is now irrelevant.

That corpus is deliberately narrow, and `test_backend_templates.py` explains
why it is not enough on its own: these two use none of the DSL's harder
features, so the real fixtures are the backend's own templates, read from a
checkout and skipped in CI. This file is the part that always runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from popcorn_core.template_check import check_bundle

BUNDLE_DIR = Path(__file__).resolve().parent / "fixtures" / "bundles"

BUNDLES = sorted(p for p in BUNDLE_DIR.iterdir() if p.is_dir()) if BUNDLE_DIR.is_dir() else []

# `clears-app-type` fires on every untyped bundle. Both of these are untyped
# on purpose — they model ops bundles for a dedicated channel — and each says
# so in its README and manifest comments. Any OTHER warning is a finding the
# bundle should either fix or explain here.
ALLOWED_WARNINGS = {"clears-app-type"}


def test_bundle_fixture_directory_is_not_empty():
    """Guards the parametrization: an empty glob would vacuously pass."""
    assert BUNDLES, f"no bundle fixtures found under {BUNDLE_DIR}"


@pytest.mark.parametrize("bundle", BUNDLES, ids=lambda p: p.name)
def test_bundle_fixture_has_no_structural_errors(bundle: Path):
    report = check_bundle(bundle)
    assert report.ok, "\n".join(str(f) for f in report.errors)


@pytest.mark.parametrize("bundle", BUNDLES, ids=lambda p: p.name)
def test_bundle_fixture_warnings_are_expected(bundle: Path):
    unexpected = [f for f in check_bundle(bundle).warnings if f.code not in ALLOWED_WARNINGS]
    assert not unexpected, "\n".join(str(f) for f in unexpected)


@pytest.mark.parametrize("bundle", BUNDLES, ids=lambda p: p.name)
def test_bundle_fixture_ships_a_readme(bundle: Path):
    assert (bundle / "README.md").is_file(), f"{bundle.name} has no README.md"
