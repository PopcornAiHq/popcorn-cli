"""Check the five shipped backend templates — opt-in, skipped without them.

**These are the fixtures that matter, and they cannot live in this repo.** The
checker was written against the two bundles now under `tests/fixtures/bundles/`,
and they happen to use none of: a nested `steps:` block, `collect:`, the `when:`
expression rail, an array-index ref, `required_integrations:`, `$trigger`, or a
`.md.j2` prompt.
Every one of those was a false positive — roughly 180 findings across the five
real templates, all of them wrong — and nothing in this repo could have caught
that, because nothing in this repo had ever been run against a bundle somebody
shipped.

Vendoring copies here would fix that for exactly as long as it took the backend
to change one, so this reads the real checkout instead: `popcorn-backend` at
`lib/temporal/flows/`, or wherever `POPCORN_BACKEND_FLOWS` points. Absent, the
module skips.

The consequence is real and worth stating plainly: **this does not run in CI.**
CI's guard is the grammar-feature coverage in `test_template_check.py`, which is
derived from what these templates do but is not the same as reading them. When
a backend template starts using a DSL feature nobody wrote a unit test for,
this file is what notices, and only if someone runs it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from popcorn_core.template_check import check_bundle

_ENV = "POPCORN_BACKEND_FLOWS"
_DEFAULT = Path.home() / "popcorn" / "backend" / "lib" / "temporal" / "flows"


def _bundles() -> list[Path]:
    root = Path(os.environ[_ENV]) if os.environ.get(_ENV) else _DEFAULT
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / "manifest.yaml").is_file())


_BUNDLES = _bundles()

pytestmark = pytest.mark.skipif(
    not _BUNDLES,
    reason=f"no backend template checkout ({_ENV} unset and {_DEFAULT} absent)",
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

    All five are warning-clean today, so any new warning is news: either a real
    defect in a template somebody shipped, or another gap in the checker's model
    of the DSL. Both want looking at; neither should be discovered by an author
    wading through noise.
    """
    report = check_bundle(bundle)
    assert report.warnings == [], [str(f) for f in report.warnings]
