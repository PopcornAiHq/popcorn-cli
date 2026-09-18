"""The PR version gate's decision table.

`decide` is pure so every branch is reachable without a scratch repository —
the git plumbing around it (merge-base, diff, tag list) is thin and the part
worth pinning is which combinations are allowed to merge.

Both rejected cases here are things that actually happened: a run of versions
reached `main` untagged and so never reached anyone upgrading, and two PRs
branched from one `main` both claimed the same number, which git merged without
a conflict because each side set the same line to the same value.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from check_version import decide

TAGS = {"v0.37.0", "v0.38.0"}


class TestAccepted:
    def test_a_patch_bump(self):
        ok, _ = decide("0.38.1", "0.38.0", src_changed=True, existing_tags=TAGS)
        assert ok

    def test_a_minor_bump(self):
        ok, _ = decide("0.39.0", "0.38.0", src_changed=True, existing_tags=TAGS)
        assert ok

    def test_no_bump_when_src_is_untouched(self):
        """A docs or CI change ships nothing to users and needs no version."""
        ok, msg = decide("0.38.0", "0.38.0", src_changed=False, existing_tags=TAGS)
        assert ok
        assert "nothing to release" in msg

    def test_a_minor_bump_carrying_a_breaking_change(self):
        """Pre-1.0, breaking changes ride in minor — removing a family did."""
        ok, _ = decide("0.38.0", "0.37.0", src_changed=True, existing_tags={"v0.37.0"})
        assert ok


class TestRejected:
    def test_src_changed_without_a_bump(self):
        ok, msg = decide("0.38.0", "0.38.0", src_changed=True, existing_tags=TAGS)
        assert not ok
        assert "still 0.38.0" in msg

    def test_a_version_that_is_already_released(self):
        """The two-PRs-one-number case, seen from the second PR."""
        ok, msg = decide("0.38.0", "0.37.0", src_changed=True, existing_tags=TAGS)
        assert not ok
        assert "already released" in msg

    def test_a_version_that_goes_backwards(self):
        ok, msg = decide("0.36.0", "0.38.0", src_changed=True, existing_tags=TAGS)
        assert not ok
        assert "backwards" in msg

    def test_a_major_bump(self):
        """auto_tag.sh refuses these, so merging one would never release."""
        ok, msg = decide("1.0.0", "0.38.0", src_changed=True, existing_tags=TAGS)
        assert not ok
        assert "major bump" in msg

    def test_an_unparseable_version(self):
        ok, msg = decide("0.39.0rc1", "0.38.0", src_changed=True, existing_tags=TAGS)
        assert not ok
        assert "cannot compare" in msg


def test_the_released_check_precedes_the_ordering_check():
    """Order matters: a taken number that is also a major bump is reported as
    taken, which is the actionable half — pick another number."""
    ok, msg = decide("1.0.0", "0.38.0", src_changed=True, existing_tags={"v1.0.0"})
    assert not ok
    assert "already released" in msg


@pytest.mark.parametrize("version", ["0.38.1", "0.39.0", "0.38.0"])
def test_never_raises_on_plausible_input(version):
    decide(version, "0.38.0", src_changed=True, existing_tags=TAGS)
